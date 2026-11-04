"""Plugin-owned persistence bridge for enhanced Kokoro settings.

The host settings modal differs across supported A0 releases. This endpoint
accepts the complete enhanced contract and writes it to both the plugin and
the active Kokoro provider config, avoiding host-private modal state.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile

from helpers.api import ApiHandler, Request


def _config_helper():
    plugin_root = Path(__file__).resolve().parent.parent
    helper_path = plugin_root / "helpers" / "config.py"
    spec = importlib.util.spec_from_file_location("agentspine_convo_config_api", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Enhanced Speech configuration helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gpu_helper():
    helper_path = Path(__file__).resolve().parent.parent / "helpers" / "gpu.py"
    spec = importlib.util.spec_from_file_location("agentspine_convo_gpu_config_api", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Enhanced Speech GPU helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _with_device_metadata(config: dict) -> dict:
    result = dict(config)
    if result.get("remote_enabled"):
        result["effective_device"] = "remote"
        result["effective_device_label"] = "Remote worker"
    else:
        device, label = _gpu_helper().resolve_local_device(str(result.get("device") or "auto"))
        result["effective_device"] = device
        result["effective_device_label"] = label
    result["cuda_devices"] = _gpu_helper().get_cuda_devices()
    return result


def _notify_saved_config(config: dict) -> None:
    """Surface a single user-facing confirmation for the plugin-owned save."""
    try:
        from helpers.notification import (
            NotificationManager,
            NotificationPriority,
            NotificationType,
        )

        primary = str(config.get("voice") or config.get("primary_voice") or "").strip()
        secondary = str(config.get("secondary_voice") or "").strip()
        voice = f"{primary} + {secondary}" if secondary else primary
        mode = str(config.get("effective_device_label") or config.get("device") or "Auto")
        NotificationManager.send_notification(
            NotificationType.SUCCESS,
            NotificationPriority.NORMAL,
            f"Kokoro settings saved: {voice} on {mode}.",
            title="Enhanced Speech",
            display_time=5,
            group="enhanced-speech-config",
        )
    except Exception:
        # A settings save remains successful if an older host does not expose
        # the notification manager during its startup sequence.
        return


def _runtime_root() -> Path:
    source = Path(__file__).resolve()
    # A built-in package lives at ``/a0/plugins/...`` while the portable A0
    # package lives at ``/a0/usr/plugins/...``. Locate the real host root from
    # its stable ``plugins`` + ``usr`` layout instead of assuming a fixed parent
    # depth, which would otherwise treat ``/a0/usr`` as the host root.
    for candidate in source.parents:
        if (candidate / "plugins").is_dir() and (candidate / "usr").is_dir():
            return candidate
    raise RuntimeError("Unable to locate the Agent Zero runtime root")


def _plugin_config_path(plugin_name: str) -> Path:
    return _runtime_root() / "usr" / "plugins" / plugin_name / "config.json"


def _read_config(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_config_atomically(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _kokoro_enabled() -> bool:
    """Resolve the provider toggle without re-entering the host config API."""
    root = _runtime_root()
    enabled = True
    for plugin_root in (
        root / "plugins" / "_kokoro_tts",
        root / "usr" / "plugins" / "_kokoro_tts",
    ):
        if enabled:
            enabled = not (
                (plugin_root / ".toggle-0").is_file()
                or (plugin_root / ".disabled").is_file()
            )
        elif (
            (plugin_root / ".toggle-1").is_file()
            or (plugin_root / ".enabled").is_file()
        ):
            enabled = True
    return enabled


def _runtime_config(helper) -> dict:
    enhanced = _read_config(_plugin_config_path("_convo"))
    tts = enhanced.get("tts") if isinstance(enhanced.get("tts"), dict) else {}
    enhanced_kokoro = tts.get("kokoro") if isinstance(tts.get("kokoro"), dict) else {}
    provider = _read_config(_plugin_config_path("_kokoro_tts"))
    merged = dict(enhanced_kokoro)
    merged.update(provider)
    return helper.normalize_kokoro_config(merged)


def _save_runtime_config(helper, requested: dict) -> dict:
    with helper._transactions().lock():
        return _save_runtime_config_locked(helper, requested)


def _save_runtime_config_locked(helper, requested: dict) -> dict:
    normalized = helper.normalize_kokoro_config({**_runtime_config(helper), **requested})

    provider_path = _plugin_config_path("_kokoro_tts")
    provider = _read_config(provider_path)
    provider.update(
        {
            "voice": normalized["voice"],
            "primary_voice": normalized["primary_voice"],
            "secondary_voice": normalized["secondary_voice"],
            "voice_blend": normalized["voice_blend"],
            "speed": normalized["speed"],
            "device": normalized["device"],
            "remote_enabled": normalized["remote_enabled"],
            "remote_url": normalized["remote_url"],
            "remote_token": normalized["remote_token"],
            "remote_timeout": normalized["remote_timeout"],
            "remote_url_candidates": normalized["remote_url_candidates"],
        }
    )
    _write_config_atomically(provider_path, provider)

    enhanced_path = _plugin_config_path("_convo")
    enhanced = _read_config(enhanced_path)
    tts = enhanced.setdefault("tts", {})
    if not isinstance(tts, dict):
        tts = enhanced["tts"] = {}
    tts["provider"] = "kokoro"
    tts["kokoro"] = helper._deep_merge(tts.get("kokoro", {}), normalized)
    _write_config_atomically(enhanced_path, enhanced)
    return normalized


class SpeechConfig(ApiHandler):
    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST"]

    async def process(self, input: dict, request: Request) -> dict:
        helper = _config_helper()
        if not isinstance(input, dict) or not input:
            return {
                "success": True,
                "enabled": _kokoro_enabled(),
                "config": _with_device_metadata(_runtime_config(helper)),
            }

        requested = {
            key: input[key]
            for key in (
                "voice", "primary_voice", "secondary_voice", "voice_blend",
                "speed", "device", "remote_enabled", "remote_url",
                "remote_token", "remote_timeout",
            )
            if key in input
        }
        normalized = _save_runtime_config(helper, requested)
        enriched = _with_device_metadata(normalized)
        _notify_saved_config(enriched)
        return {
            "success": True,
            "enabled": _kokoro_enabled(),
            "config": enriched,
        }
