from __future__ import annotations

import copy
import importlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from helpers import plugins


PLUGIN_NAME = "_convo"
LEGACY_PLUGIN_NAME = "enhanced_speech"
LEGACY_BUILTIN_NAME = "_enhanced_speech"
KOKORO_PLUGIN = "_kokoro_tts"
WHISPER_PLUGIN = "_whisper_stt"
DEFAULT_REMOTE_URL = "http://kokoro-worker:8891"
REMOTE_URL_CANDIDATES = (
    "http://kokoro-worker:8891",
    "http://kokoro-gpu-worker:8891",
    "http://agentspine-kokoro-gpu:8891",
    "http://host.docker.internal:51101",
)
DEVICE_ALIASES = {
    "gpu": "cuda",
    "remote_worker": "remote",
}
CUDA_DEVICE_RE = re.compile(r"^cuda:\d+$")
VOICE_ID_RE = re.compile(r"^[a-z]{2}_[a-z0-9_]+$")

DEFAULTS: dict[str, Any] = {
    "tts": {
        "provider": "kokoro",
        "kokoro": {
            "primary_voice": "am_michael",
            "secondary_voice": "",
            "voice_blend": 50,
            "speed": 1.1,
            "device": "auto",
            "remote": {
                "enabled": False,
                "url": DEFAULT_REMOTE_URL,
                "token": "",
                "timeout": 20,
            },
        },
    },
    "stt": {
        "provider": "whisper",
        "whisper": {
            "model_size": "base",
            "device": "auto",
            "language": "en",
            "message_mode": "send",
            "silence_threshold": 0.3,
            "silence_duration": 1000,
            "waiting_timeout": 2000,
        },
    },
}

KOKORO_KEYS = {
    "primary_voice": "voice",
    "secondary_voice": "secondary_voice",
    "voice_blend": "voice_blend",
    "speed": "speed",
    "device": "device",
}

WHISPER_KEYS = (
    "model_size",
    "device",
    "language",
    "message_mode",
    "silence_threshold",
    "silence_duration",
    "waiting_timeout",
)

# Agent Zero's Whisper stores only send|draft (its browser store turns anything else into send). "Draft in composer
# (continuous)" is therefore the host's draft plus Convo's own `voice_message_draft` flag (convo_contract): `single` leaves
# one phrase in the composer as the host does, `continuous` keeps adding phrases until the user sends or stops.
VOICE_MESSAGE_DRAFT = ("single", "continuous")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any] | None) -> dict[str, Any]:
    result = copy.deepcopy(base)
    if not isinstance(overlay, dict):
        return result
    for key, value in overlay.items():
        if key != "voice_weights" and isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _as_float(value: Any, default: float, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and result < minimum:
        return default
    return result


def _as_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _plugin_config(plugin_name: str) -> dict[str, Any]:
    """Read resolved plugin state without re-entering provider hooks.

    Current Kokoro and Whisper hooks normalize configuration through their
    runtime modules.  Enhanced Speech replaces those runtime normalizers, so
    calling ``plugins.get_plugin_config()`` from this adapter forms a cycle:
    host config -> provider hook -> Enhanced Speech runtime -> host config.
    Resolve the same precedence-aware config asset path, then read its JSON
    directly.  This remains plugin-local and preserves the user-over-built-in
    configuration precedence without invoking any host hook.
    """
    try:
        path = _config_path(plugin_name)
        if path is None or not path.is_file():
            return {}
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _config_path(plugin_name: str) -> Path | None:
    try:
        return Path(plugins.determine_plugin_asset_path(plugin_name, "", "", plugins.CONFIG_FILE_NAME))
    except Exception:
        return None


def _write_plugin_config(plugin_name: str, settings: dict[str, Any]) -> None:
    path = _config_path(plugin_name)
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Status refreshes can read this file while the settings API is saving.
    # Replacing a complete sibling file avoids exposing the zero-byte interval
    # created by an in-place write, which otherwise surfaces as JSONDecodeError.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(settings, temporary, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def normalize_kokoro_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    cfg = _deep_merge(DEFAULTS["tts"]["kokoro"], source)

    # ``primary_voice`` is an Enhanced Speech alias.  A host Kokoro settings
    # panel only sends ``voice``; do not let the default alias hide it.
    if "voice" in source:
        cfg["primary_voice"] = source.get("voice")
    if "tts_kokoro_voice" in cfg and "voice" not in source and "voice_weights" not in source:
        cfg["primary_voice"] = cfg.get("tts_kokoro_voice")
    if "tts_kokoro_voice_secondary" in cfg:
        cfg["secondary_voice"] = cfg.get("tts_kokoro_voice_secondary")
    if "tts_kokoro_voice_blend" in cfg:
        cfg["voice_blend"] = cfg.get("tts_kokoro_voice_blend")
    if "tts_kokoro_speed" in cfg:
        cfg["speed"] = cfg.get("tts_kokoro_speed")
    if "tts_device" in cfg:
        cfg["device"] = cfg.get("tts_device")

    remote = cfg.get("remote") if isinstance(cfg.get("remote"), dict) else {}
    if cfg.get("tts_kokoro_remote_url") is not None:
        remote["url"] = cfg.get("tts_kokoro_remote_url")
    if cfg.get("tts_kokoro_remote_token") is not None:
        remote["token"] = cfg.get("tts_kokoro_remote_token")
    if cfg.get("tts_kokoro_remote_timeout") is not None:
        remote["timeout"] = cfg.get("tts_kokoro_remote_timeout")
    if cfg.get("remote_url") is not None:
        remote["url"] = cfg.get("remote_url")
    if cfg.get("remote_token") is not None:
        remote["token"] = cfg.get("remote_token")
    if cfg.get("remote_timeout") is not None:
        remote["timeout"] = cfg.get("remote_timeout")
    if cfg.get("remote_enabled") is not None:
        remote["enabled"] = bool(cfg.get("remote_enabled"))

    primary = str(cfg.get("primary_voice") or DEFAULTS["tts"]["kokoro"]["primary_voice"]).strip()
    secondary = str(cfg.get("secondary_voice") or "").strip()
    device = DEVICE_ALIASES.get(str(cfg.get("device") or "auto").strip().lower(), str(cfg.get("device") or "auto").strip().lower()) or "auto"
    if device not in {"auto", "cpu", "cuda", "remote"} and not CUDA_DEVICE_RE.fullmatch(device):
        device = "auto"
    remote_url = str(remote.get("url") or DEFAULT_REMOTE_URL).strip().rstrip("/")

    result = {
        **source,
        "voice": primary,
        "primary_voice": primary,
        "secondary_voice": secondary,
        "voice_blend": _as_int(cfg.get("voice_blend"), 50, 1, 99),
        "speed": _as_float(cfg.get("speed"), 1.1, 0.1),
        "device": device,
        # v1.20 uses the compute mode as the single source of truth. Retain the
        # legacy flag in persisted config, but never let it override a new mode.
        "remote_enabled": device == "remote",
        "remote_url": remote_url,
        "remote_token": str(remote.get("token") or ""),
        "remote_timeout": _as_float(remote.get("timeout"), 20.0, 1.0),
        "remote_url_candidates": list(REMOTE_URL_CANDIDATES),
    }
    # Presence (including {}) is authoritative; removed weights stay removed.
    weights = {}
    if "voice_weights" in source:
        entries = source.get("voice_weights")
        for voice_id, raw_weight in entries.items() if isinstance(entries, dict) else ():
            voice_id = str(voice_id).strip()
            try:
                weight = float(raw_weight)
            except (TypeError, ValueError):
                continue
            if VOICE_ID_RE.fullmatch(voice_id) and math.isfinite(weight) and weight > 0:
                weights[voice_id] = weight
    elif secondary and "," not in primary:
        ratio = result["voice_blend"] / 10.0
        if VOICE_ID_RE.fullmatch(primary) and VOICE_ID_RE.fullmatch(secondary):
            weights[primary] = ratio
            weights[secondary] = weights.get(secondary, 0) + 10.0 - ratio
    result["voice_weights"] = weights
    result["voice"] = ",".join(weights) if weights else primary
    result["primary_voice"] = result["voice"].split(",")[0]
    ids = [value.strip() for value in result["voice"].split(",") if value.strip()]
    result["secondary_voice"] = ids[1] if len(ids) == 2 else ""
    result["voice_blend"] = 100 * weights[ids[0]] / sum(weights.values()) if len(ids) == 2 and weights else 50
    return result


def voice_summary(cfg: dict[str, Any]) -> str:
    weights = cfg.get("voice_weights") or {}
    total = sum(weights.values())
    if total > 0:
        return " + ".join(f"{voice} ({weight / total:.0%})" for voice, weight in weights.items())
    return str(cfg.get("voice") or "")


def normalize_whisper_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _deep_merge(DEFAULTS["stt"]["whisper"], raw or {})
    model_size = str(cfg.get("model_size") or "base").strip().lower()
    if model_size not in {"tiny", "base", "small", "medium", "large", "turbo"}:
        model_size = "base"
    language = str(cfg.get("language") or "en").strip() or "en"
    message_mode = str(cfg.get("message_mode") or "send").strip().lower()
    if message_mode not in {"send", "draft"}:
        message_mode = "send"
    device = DEVICE_ALIASES.get(
        str(cfg.get("device") or "auto").strip().lower(),
        str(cfg.get("device") or "auto").strip().lower(),
    ) or "auto"
    if device not in {"auto", "cpu", "cuda"} and not CUDA_DEVICE_RE.fullmatch(device):
        device = "auto"
    return {
        "model_size": model_size,
        "device": device,
        "language": language,
        "message_mode": message_mode,
        "silence_threshold": max(0.0, min(1.0, _as_float(cfg.get("silence_threshold"), 0.3))),
        "silence_duration": _as_int(cfg.get("silence_duration"), 1000, 100),
        "waiting_timeout": _as_int(cfg.get("waiting_timeout"), 2000, 100),
    }


def split_message_mode(value: Any) -> tuple[str, str]:
    """A requested voice-message mode as (host message_mode, Convo voice_message_draft). The host value is never 'continuous'."""
    mode = str(value or "").strip().lower()
    if mode == "continuous":
        return "draft", "continuous"
    return ("draft" if mode == "draft" else "send"), "single"


def stored_voice_message_draft(enhanced: dict[str, Any] | None) -> str:
    """Convo's drafting flag as its own settings node stores it ('single' when absent or unknown)."""
    node = enhanced.get("convo") if isinstance(enhanced, dict) else None
    value = node.get("voice_message_draft") if isinstance(node, dict) else None
    return value if value in VOICE_MESSAGE_DRAFT else "single"


def with_voice_message_draft(enhanced: dict[str, Any], flag: str) -> dict[str, Any]:
    """The whole Convo plugin file with its drafting flag set, for the same single write as the Whisper values. The Convo node
    is stored normalized, as every settings writer stores it, and the shared write epoch advances so open Convo editors notice.
    Unchanged when the flag already holds. Raises (before anything is written) when the stored Convo settings are invalid."""
    if flag not in VOICE_MESSAGE_DRAFT:
        raise ValueError("Voice message drafting must be single or continuous")
    if stored_voice_message_draft(enhanced) == flag:
        return enhanced
    node = dict(enhanced.get("convo")) if isinstance(enhanced.get("convo"), dict) else {}
    node["voice_message_draft"] = flag
    return {**enhanced, "convo": _transactions().normalize(node),
            "_convo_write_epoch": int(enhanced.get("_convo_write_epoch", 0) or 0) + 1}


def get_effective_config() -> dict[str, Any]:
    legacy = _deep_merge(_plugin_config(LEGACY_PLUGIN_NAME), _plugin_config(LEGACY_BUILTIN_NAME))
    enhanced = _deep_merge(_deep_merge(DEFAULTS, legacy), _plugin_config(PLUGIN_NAME))
    kokoro = normalize_kokoro_config(_deep_merge(enhanced["tts"]["kokoro"], _plugin_config(KOKORO_PLUGIN)))
    whisper = normalize_whisper_config(_deep_merge(enhanced["stt"]["whisper"], _plugin_config(WHISPER_PLUGIN)))
    return {
        **enhanced,
        "tts": _deep_merge(enhanced["tts"], {"provider": "kokoro", "kokoro": kokoro}),
        "stt": _deep_merge(enhanced["stt"], {"provider": "whisper", "whisper": whisper}),
    }


def kokoro_runtime_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    effective = get_effective_config()["tts"]["kokoro"]
    if raw:
        effective = normalize_kokoro_config(_deep_merge(effective, raw))
    return effective


def whisper_runtime_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    effective = get_effective_config()["stt"]["whisper"]
    if raw:
        effective = normalize_whisper_config(_deep_merge(effective, raw))
    return effective


def provider_kokoro_config(settings: dict[str, Any]) -> dict[str, Any]:
    cfg = normalize_kokoro_config(settings)
    result = dict(settings or {})
    result["voice"] = cfg["voice"]
    result["voice_weights"] = cfg["voice_weights"]
    result["speed"] = cfg["speed"]
    result["secondary_voice"] = cfg["secondary_voice"]
    result["voice_blend"] = cfg["voice_blend"]
    result["device"] = cfg["device"]
    result["remote_enabled"] = cfg["remote_enabled"]
    result["remote_url"] = cfg["remote_url"]
    result["remote_token"] = cfg["remote_token"]
    result["remote_timeout"] = cfg["remote_timeout"]
    result["remote_url_candidates"] = cfg["remote_url_candidates"]
    return result


def provider_whisper_config(settings: dict[str, Any]) -> dict[str, Any]:
    cfg = normalize_whisper_config(settings)
    result = dict(settings or {})
    result.update(cfg)
    return result


def _transactions():
    parent = Path(__file__).resolve().parents[2].parent
    prefix = 'usr.plugins' if parent.name == 'usr' else 'plugins'
    return importlib.import_module(prefix + '._convo.helpers.setup_store')


def sync_enhanced_from_provider(plugin_name: str, settings: dict[str, Any]) -> None:
    with _transactions().lock():
        _sync_enhanced_from_provider(plugin_name, settings)


def _sync_enhanced_from_provider(plugin_name: str, settings: dict[str, Any]) -> None:
    current = _deep_merge(DEFAULTS, _plugin_config(PLUGIN_NAME))
    if plugin_name == KOKORO_PLUGIN:
        merged = _deep_merge(current["tts"]["kokoro"], settings)
        if "voice_weights" not in settings and any(key in settings for key in ("voice", "primary_voice", "secondary_voice", "voice_blend")):
            merged.pop("voice_weights", None)
        current["tts"]["kokoro"] = normalize_kokoro_config(merged)
    elif plugin_name == WHISPER_PLUGIN:
        settings = dict(settings or {})
        flag = None
        if "message_mode" in settings:
            # A save through Agent Zero's generic route stores the modal's value as given, "continuous" included, which the
            # host would read as "send". It is the host's draft plus Convo's flag here too, and the host file is put right.
            settings["message_mode"], flag = split_message_mode(settings["message_mode"])
        current["stt"]["whisper"] = _deep_merge(current["stt"]["whisper"], normalize_whisper_config(settings))
        if flag == "continuous":
            provider = _plugin_config(WHISPER_PLUGIN)
            if provider.get("message_mode") != "draft":       # first, so even a refused flag never leaves the host on "send"
                _write_plugin_config(WHISPER_PLUGIN, {**provider, "message_mode": "draft"})
        if flag is not None:
            current = with_voice_message_draft(current, flag)
    elif plugin_name in {PLUGIN_NAME, LEGACY_PLUGIN_NAME}:
        # This bridge owns native speech only, never a stale Convo node.
        current = _deep_merge(current, {key: settings[key] for key in ('tts', 'stt') if key in settings})
    else:
        return
    _write_plugin_config(PLUGIN_NAME, current)


def sync_providers_from_enhanced() -> None:
    # This path is invoked by the plugin-owned speech-config endpoint after it
    # has written a complete Enhanced Speech configuration.  Do not call
    # ``get_effective_config`` here: that deliberately gives an existing host
    # provider file precedence for normal reads, and would therefore write the
    # provider's *old* voice/speed back over the just-saved values.
    enhanced = _deep_merge(
        _deep_merge(DEFAULTS, _plugin_config(LEGACY_PLUGIN_NAME)),
        _plugin_config(PLUGIN_NAME),
    )
    kokoro = normalize_kokoro_config(enhanced["tts"]["kokoro"])
    whisper = normalize_whisper_config(enhanced["stt"]["whisper"])
    kokoro_current = _plugin_config(KOKORO_PLUGIN)
    whisper_current = _plugin_config(WHISPER_PLUGIN)
    _write_plugin_config(KOKORO_PLUGIN, provider_kokoro_config(_deep_merge(kokoro_current, kokoro)))
    _write_plugin_config(WHISPER_PLUGIN, provider_whisper_config(_deep_merge(whisper_current, whisper)))


def provider_config_for(plugin_name: str, settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if plugin_name == KOKORO_PLUGIN:
        return provider_kokoro_config(_deep_merge(settings or {}, get_effective_config()["tts"]["kokoro"]))
    if plugin_name == WHISPER_PLUGIN:
        return provider_whisper_config(_deep_merge(settings or {}, get_effective_config()["stt"]["whisper"]))
    if plugin_name in {PLUGIN_NAME, LEGACY_PLUGIN_NAME}:
        return get_effective_config()
    return None
