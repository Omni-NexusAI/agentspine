"""Plugin-owned persistence bridge for Whisper STT settings.

The host Whisper modal has changed across compatible Agent Zero releases. This
route owns only Enhanced Speech's mirrored provider settings so a selected
processing device survives a native modal save without touching host-core
configuration. A requested ``message_mode`` of ``continuous`` ("Draft in
composer (continuous)") is stored as the host's ``draft`` plus Convo's
``voice_message_draft`` flag, in one Convo settings transaction.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path

from helpers.api import ApiHandler, Request


WHISPER_PLUGIN = "_whisper_stt"
WHISPER_SETTING_KEYS = (
    "model_size",
    "device",
    "language",
    "message_mode",
    "silence_threshold",
    "silence_duration",
    "waiting_timeout",
)


def _helper_from(filename: str, module_name: str):
    helper_path = Path(__file__).resolve().parent.parent / "helpers" / filename
    spec = importlib.util.spec_from_file_location(module_name, helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Enhanced Speech helper is unavailable: {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config_helper():
    return _helper_from("config.py", "agentspine_convo_stt_config")


def _gpu_helper():
    return _helper_from("gpu.py", "agentspine_convo_stt_config_gpu")


def _runtime_root() -> Path:
    source = Path(__file__).resolve()
    # This package may be a Spine built-in or an A0 custom plugin. The latter
    # adds an extra ``usr`` path segment, so discover the host root rather than
    # using the built-in package's fixed parent depth.
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
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _whisper_enabled() -> bool:
    root = _runtime_root()
    enabled = True
    for plugin_root in (
        root / "plugins" / WHISPER_PLUGIN,
        root / "usr" / "plugins" / WHISPER_PLUGIN,
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
    stt = enhanced.get("stt") if isinstance(enhanced.get("stt"), dict) else {}
    enhanced_whisper = (
        stt.get("whisper") if isinstance(stt.get("whisper"), dict) else {}
    )
    provider = _read_config(_plugin_config_path(WHISPER_PLUGIN))
    merged = dict(enhanced_whisper)
    merged.update(provider)
    return helper.normalize_whisper_config(merged)


def _with_device_metadata(config: dict) -> dict:
    result = dict(config)
    effective, label = _gpu_helper().resolve_local_device(
        str(result.get("device") or "auto")
    )
    result["effective_device"] = effective
    result["effective_device_label"] = label
    result["cuda_devices"] = _gpu_helper().get_cuda_devices()
    return result


def _save_runtime_config(helper, requested: dict) -> tuple[dict, str, str]:
    with helper._transactions().lock():
        return _save_runtime_config_locked(helper, requested)


def _save_runtime_config_locked(helper, requested: dict) -> tuple[dict, str, str]:
    """Returns (Whisper values, Convo's drafting flag, Convo's settings revision) as saved."""
    flag = None
    if "message_mode" in requested:
        # Agent Zero's Whisper keeps only send|draft: "continuous" is its draft plus Convo's own flag, never a host value.
        host_mode, flag = helper.split_message_mode(requested["message_mode"])
        requested = {**requested, "message_mode": host_mode}
    normalized = helper.normalize_whisper_config({**_runtime_config(helper), **requested})
    transactions = helper._transactions()
    enhanced_path = _plugin_config_path("_convo")

    def update(enhanced: dict) -> dict:
        stt = enhanced.get("stt") if isinstance(enhanced.get("stt"), dict) else {}
        enhanced = {**enhanced, "stt": {**stt, "provider": "whisper", "whisper": helper._deep_merge(stt.get("whisper", {}), normalized)}}
        return helper.with_voice_message_draft(enhanced, flag) if flag else enhanced

    # Convo's settings are checked before anything is written: invalid ones refuse the change and both files stay as they were.
    update(transactions.read(enhanced_path))
    provider_path = _plugin_config_path(WHISPER_PLUGIN)
    provider = _read_config(provider_path)
    provider.update(helper.provider_whisper_config(normalized))
    _write_config_atomically(provider_path, provider)
    # One settings transaction for the Whisper values and the flag; a changed flag advances Convo's revision.
    saved = transactions.transaction(enhanced_path, update)
    return normalized, helper.stored_voice_message_draft(saved), transactions.settings_revision(saved)


def _notify_saved_config(config: dict) -> None:
    try:
        from helpers.notification import (
            NotificationManager,
            NotificationPriority,
            NotificationType,
        )

        NotificationManager.send_notification(
            NotificationType.SUCCESS,
            NotificationPriority.NORMAL,
            "Whisper settings saved: "
            f"{config.get('model_size', 'base')} on "
            f"{config.get('effective_device_label', config.get('device', 'Auto'))}.",
            title="Enhanced Speech",
            display_time=5,
            group="enhanced-speech-stt-config",
        )
    except Exception:
        return


class SttConfig(ApiHandler):
    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST"]

    async def process(self, input: dict, request: Request) -> dict:
        helper = _config_helper()
        if not isinstance(input, dict) or not input:
            return {
                "success": True,
                "enabled": _whisper_enabled(),
                "config": _with_device_metadata(_runtime_config(helper)),
                "voice_message_draft": helper.stored_voice_message_draft(_read_config(_plugin_config_path("_convo"))),
            }

        requested = {key: input[key] for key in WHISPER_SETTING_KEYS if key in input}
        saved, flag, revision = _save_runtime_config(helper, requested)
        config = _with_device_metadata(saved)
        _notify_saved_config(config)
        # `config.message_mode` stays the host's send|draft (the host store copies it); the flag and revision sit beside it.
        return {
            "success": True,
            "enabled": _whisper_enabled(),
            "config": config,
            "voice_message_draft": flag,
            "convo_revision": revision,
        }
