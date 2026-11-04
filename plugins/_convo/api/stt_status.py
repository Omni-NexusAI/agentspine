"""Plugin-owned status bridge for the public Whisper STT WebUI store."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from helpers.api import ApiHandler, Request


WHISPER_PLUGIN = "_whisper_stt"


def _config_helper():
    helper_path = Path(__file__).resolve().parent.parent / "helpers" / "config.py"
    spec = importlib.util.spec_from_file_location(
        "agentspine_convo_stt_status", helper_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Enhanced Speech configuration helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gpu_helper():
    helper_path = Path(__file__).resolve().parent.parent / "helpers" / "gpu.py"
    spec = importlib.util.spec_from_file_location(
        "agentspine_convo_stt_gpu", helper_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Enhanced Speech GPU helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _whisper_is_available() -> bool:
    return (_runtime_root() / "plugins" / WHISPER_PLUGIN / "plugin.yaml").is_file()


def _whisper_is_enabled() -> bool:
    if not _whisper_is_available():
        return False
    # Match A0's built-in/user toggle precedence without calling its extension
    # pipeline.  Status is rendered from the WebUI and must not wait on other
    # optional plugins' config hooks.
    enabled = True
    for plugin_root in (
        _runtime_root() / "plugins" / WHISPER_PLUGIN,
        _runtime_root() / "usr" / "plugins" / WHISPER_PLUGIN,
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


def _runtime_root() -> Path:
    source = Path(__file__).resolve()
    # A0 custom packages are rooted below ``/a0/usr/plugins`` whereas Spine
    # built-ins are below ``/a0/plugins``. Find the actual host root so the
    # status bridge always resolves the built-in Whisper provider correctly.
    for candidate in source.parents:
        if (candidate / "plugins").is_dir() and (candidate / "usr").is_dir():
            return candidate
    raise RuntimeError("Unable to locate the Agent Zero runtime root")


def _read_config(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _whisper_config() -> tuple[dict, str]:
    """The host's Whisper values (message_mode send|draft) and Convo's drafting flag (single|continuous)."""
    helper = _config_helper()
    root = _runtime_root() / "usr" / "plugins"
    enhanced = _read_config(root / "_convo" / "config.json")
    stt = enhanced.get("stt") if isinstance(enhanced.get("stt"), dict) else {}
    enhanced_whisper = stt.get("whisper") if isinstance(stt.get("whisper"), dict) else {}
    provider = _read_config(root / WHISPER_PLUGIN / "config.json")
    merged = dict(enhanced_whisper)
    merged.update(provider)
    return helper.normalize_whisper_config(merged), helper.stored_voice_message_draft(enhanced)


def _with_device_metadata(config: dict) -> dict:
    result = dict(config)
    effective, label = _gpu_helper().resolve_local_device(
        str(result.get("device") or "auto")
    )
    result["effective_device"] = effective
    result["effective_device_label"] = label
    result["cuda_devices"] = _gpu_helper().get_cuda_devices()
    return result


class SttStatus(ApiHandler):
    """Return the stable STT toggle/config contract without host status coupling."""

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST"]

    async def process(self, input: dict, request: Request) -> dict:
        config, voice_message_draft = _whisper_config()
        # The host store copies `config`, so it keeps the host's own values; Convo's flag sits beside it.
        return {
            "success": True,
            "enabled": _whisper_is_enabled(),
            "config": _with_device_metadata(config),
            "voice_message_draft": voice_message_draft,
        }
