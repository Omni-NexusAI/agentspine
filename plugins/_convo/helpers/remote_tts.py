from __future__ import annotations

import os
import copy
import importlib
from pathlib import Path
import socket
import urllib.request
from urllib.parse import urlparse


DEFAULT_WORKER_URL = "http://kokoro-worker:8891"
REMOTE_DEVICE_VALUE = "remote"


def _should_probe_default_worker() -> bool:
    if os.getenv("A0_ENABLE_REMOTE_TTS", "false").lower() == "true":
        return True
    if os.getenv("A0_TTS_REMOTE_WORKER", "false").lower() == "true":
        return True
    try:
        from helpers.build_type import BuildType, get_build_type

        return get_build_type() == BuildType.HYBRID_GPU
    except Exception:
        return False


def _normalize_url(url: str | None) -> str:
    value = (url or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    return value


def _configured_url(settings: dict | None = None) -> str:
    settings = settings or {}
    for key in (
        "tts_kokoro_remote_url",
        "kokoro_remote_url",
        "tts_remote_url",
    ):
        value = _normalize_url(settings.get(key))
        if value:
            return value
    for key in (
        "A0_TTS_KOKORO_REMOTE_URL",
        "A0_SET_TTS_KOKORO_REMOTE_URL",
        "KOKORO_WORKER_URL",
        "KOKORO_GPU_WORKER_URL",
        "A0_TTS_REMOTE_URL",
    ):
        value = _normalize_url(os.getenv(key))
        if value:
            return value
    return ""


def _host_port_reachable(url: str, timeout: float = 0.45) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _health_reachable(url: str, timeout: float = 0.75) -> bool:
    health_url = url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(health_url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except Exception:
        return _host_port_reachable(url)


def detect_remote_tts_url(settings: dict | None = None, probe: bool = True) -> str:
    configured = _configured_url(settings)
    if configured:
        return configured
    if not probe or not _should_probe_default_worker():
        return ""
    return DEFAULT_WORKER_URL if _health_reachable(DEFAULT_WORKER_URL) else ""


def ensure_remote_tts_option(options: list[dict], settings: dict | None = None) -> list[dict]:
    result = [dict(option) for option in (options or [])]
    current_values = {option.get("value") for option in result}
    if detect_remote_tts_url(settings) and REMOTE_DEVICE_VALUE not in current_values:
        result.append(
            {
                "value": REMOTE_DEVICE_VALUE,
                "label": "Remote Kokoro worker / endpoint",
                "description": "Use the Kokoro worker sidecar or configured remote TTS endpoint.",
            }
        )
    return result


def apply_remote_tts_defaults(defaults: dict, settings: dict | None = None) -> dict:
    result = dict(defaults or {})
    remote_url = detect_remote_tts_url(settings)
    if not remote_url:
        return result
    result.setdefault("tts_kokoro_remote_url", remote_url)
    if not result.get("tts_kokoro_remote_url"):
        result["tts_kokoro_remote_url"] = remote_url
    if result.get("tts_device") in (None, "", "auto", "cpu"):
        result["tts_device"] = REMOTE_DEVICE_VALUE
    return result


def apply_remote_tts_runtime_settings(settings: dict | None) -> dict | None:
    if not isinstance(settings, dict):
        return settings
    remote_url = detect_remote_tts_url(settings)
    if not remote_url:
        return settings
    settings.setdefault("tts_kokoro", True)
    settings.setdefault("tts_kokoro_remote_url", remote_url)
    if not settings.get("tts_kokoro_remote_url"):
        settings["tts_kokoro_remote_url"] = remote_url
    if settings.get("tts_device") in (None, "", "auto", "cpu"):
        settings["tts_device"] = REMOTE_DEVICE_VALUE
    return settings


def _lifecycle():
    prefix = 'usr.plugins' if Path(__file__).resolve().parents[2].parent.name == 'usr' else 'plugins'
    return importlib.import_module(prefix + '._convo.helpers.lifecycle')


@_lifecycle().owned_patch('remote', [
    ('helpers.build_type', ('get_tts_device_options', 'get_tts_defaults')),
    ('helpers.settings', ('get_tts_device_options', 'get_tts_defaults', 'get_settings', '_agentspine_convo_patched',
                          '_agentspine_enhanced_speech_patched'))])
def patch_runtime() -> bool:
    """Install optional remote-worker defaults when this host exposes them.

    Speech must remain a no-op on hosts without the older settings helpers.
    """
    try:
        import helpers.build_type as build_type
        import helpers.settings as settings_module
    except Exception:
        return False

    if getattr(settings_module, "_agentspine_convo_patched", False):
        return True

    original_options = settings_module.get_tts_device_options
    original_defaults = settings_module.get_tts_defaults
    original_get_settings = settings_module.get_settings

    def patched_options(*args, **kwargs):
        current = getattr(settings_module, "_settings", None)
        return ensure_remote_tts_option(original_options(*args, **kwargs), current)

    def patched_defaults(*args, **kwargs):
        current = getattr(settings_module, "_settings", None)
        return apply_remote_tts_defaults(original_defaults(*args, **kwargs), current)

    def patched_get_settings(*args, **kwargs):
        return apply_remote_tts_runtime_settings(copy.deepcopy(original_get_settings(*args, **kwargs)))

    build_type.get_tts_device_options = patched_options
    build_type.get_tts_defaults = patched_defaults
    settings_module.get_tts_device_options = patched_options
    settings_module.get_tts_defaults = patched_defaults
    settings_module.get_settings = patched_get_settings
    settings_module._agentspine_convo_patched = True
    settings_module._agentspine_enhanced_speech_patched = True

    return True
