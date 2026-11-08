"""Plugin-local CUDA policy adapter for A0's built-in Whisper runtime."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any


def _load_config_helper():
    helper_path = Path(__file__).resolve().parent / "config.py"
    spec = importlib.util.spec_from_file_location("agentspine_convo_whisper_config", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Enhanced Speech configuration helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


speech_config = _load_config_helper()


def _load_gpu_helper():
    helper_path = Path(__file__).resolve().parent / "gpu.py"
    spec = importlib.util.spec_from_file_location("agentspine_convo_whisper_gpu", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Enhanced Speech GPU helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gpu = _load_gpu_helper()


def _resolve_device(policy: str) -> tuple[str, str]:
    return gpu.resolve_local_device(policy)


def _lifecycle():
    prefix = 'usr.plugins' if Path(__file__).resolve().parents[2].parent.name == 'usr' else 'plugins'
    return importlib.import_module(prefix + '._convo.helpers.lifecycle')


@_lifecycle().owned_patch('whisper', [('plugins._whisper_stt.helpers.runtime', (
    'normalize_config', 'whisper.load_model', '_agentspine_convo_whisper_patched',
    '_agentspine_enhanced_speech_whisper_patched'))])
def patch_runtime() -> bool:
    """Make Whisper placement explicit without changing the host plugin."""
    try:
        runtime = importlib.import_module("plugins._whisper_stt.helpers.runtime")
    except Exception:
        return False

    if getattr(runtime, "_agentspine_convo_whisper_patched", False):
        return True

    original_normalize = runtime.normalize_config
    original_load_model = runtime.whisper.load_model

    def normalize_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = original_normalize(config)
        effective = speech_config.whisper_runtime_config(config)
        device, label = _resolve_device(str(effective.get("device") or "auto"))
        normalized["device"] = str(effective.get("device") or "auto")
        normalized["effective_device"] = device
        normalized["effective_device_label"] = label
        return normalized

    def load_model(*args: Any, **kwargs: Any) -> Any:
        cfg = speech_config.whisper_runtime_config()
        device, _label = _resolve_device(str(cfg.get("device") or "auto"))
        kwargs.setdefault("device", device)
        runtime._agentspine_convo_whisper_device = kwargs["device"]
        return original_load_model(*args, **kwargs)

    runtime.normalize_config = normalize_config
    runtime.whisper.load_model = load_model
    runtime._agentspine_convo_whisper_patched = True
    runtime._agentspine_enhanced_speech_whisper_patched = True
    return True
