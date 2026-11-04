from __future__ import annotations

import base64
import inspect
import importlib
import os
import tempfile
from pathlib import Path
from typing import Any

from helpers import files, settings


_MIME_SUFFIXES = {
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "video/webm": ".webm",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/flac": ".flac",
}

_model = None
_model_name = ""
_model_device = ""


def _option(value: str, label: str) -> dict[str, str]:
    return {"value": value, "label": label}


def _build_metadata() -> dict[str, str]:
    metadata = {
        "build_variant": os.getenv("BUILD_VARIANT", "").strip(),
        "pytorch_variant": os.getenv("PYTORCH_VARIANT", "").strip(),
        "git_ref": os.getenv("GIT_REF", "").strip(),
        "release_channel": os.getenv("RELEASE_CHANNEL", "").strip(),
        "build_version": "",
    }

    for path in ("/a0_build_version.txt", files.get_abs_path("a0_build_version.txt")):
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except Exception:
            value = ""
        if value:
            metadata["build_version"] = value
            break

    return metadata


def _torch_status() -> dict[str, Any]:
    try:
        import torch
    except Exception as exc:
        return {
            "torch_available": False,
            "cuda_available": False,
            "cuda_device_count": 0,
            "cuda_devices": [],
            "error": str(exc),
        }

    result: dict[str, Any] = {
        "torch_available": True,
        "torch_version": getattr(torch, "__version__", ""),
        "cuda_available": bool(torch.cuda.is_available()),
        "cuda_device_count": 0,
        "cuda_devices": [],
    }
    if not result["cuda_available"]:
        return result

    try:
        result["cuda_device_count"] = int(torch.cuda.device_count())
        for index in range(result["cuda_device_count"]):
            props = torch.cuda.get_device_properties(index)
            result["cuda_devices"].append(
                {
                    "index": index,
                    "name": props.name,
                    "memory_total": f"{props.total_memory / (1024 ** 3):.1f} GB",
                    "compute_capability": f"{props.major}.{props.minor}",
                }
            )
    except Exception as exc:
        result["cuda_available"] = False
        result["cuda_device_count"] = 0
        result["cuda_devices"] = []
        result["error"] = str(exc)

    return result


def _module_available(module_name: str) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


def _device_options(cuda_status: dict[str, Any]) -> list[dict[str, str]]:
    options = [_option("auto", "Auto (recommended)"), _option("cpu", "CPU")]
    if cuda_status.get("cuda_available"):
        options.append(_option("cuda:auto", "CUDA: Auto"))
        for device in cuda_status.get("cuda_devices", []):
            options.append(
                _option(
                    f"cuda:{device['index']}",
                    f"CUDA: GPU {device['index']} - {device['name']} ({device['memory_total']})",
                )
            )
    return options


def get_capabilities() -> dict[str, Any]:
    metadata = _build_metadata()
    torch_status = _torch_status()
    build_variant = metadata["build_variant"].lower()
    pytorch_variant = metadata["pytorch_variant"].lower()
    cuda_available = bool(torch_status.get("cuda_available"))
    warnings: list[str] = []

    if (build_variant in {"fullgpu", "gpu"} or pytorch_variant == "cuda") and not cuda_available:
        warnings.append("GPU build settings were detected, but CUDA is not available at runtime.")

    stt_options = _device_options(torch_status)
    tts_options = list(stt_options)

    try:
        prefix = 'usr.plugins' if Path(__file__).resolve().parents[2].parent.name == 'usr' else 'plugins'
        remote_tts = importlib.import_module(prefix + '._convo.helpers.remote_tts')

        tts_options = remote_tts.ensure_remote_tts_option(tts_options, getattr(settings, "_settings", None))
    except Exception:
        pass

    default_stt_device = "cuda:auto" if cuda_available else "auto"
    default_tts_device = "cuda:auto" if cuda_available else "auto"

    return {
        **metadata,
        **torch_status,
        "whisper_available": _module_available("whisper"),
        "kokoro_available": _module_available("kokoro"),
        "default_stt_device": default_stt_device,
        "default_tts_device": default_tts_device,
        "stt_device_options": stt_options,
        "tts_device_options": tts_options,
        "warnings": warnings,
    }


def get_stt_device_options() -> list[dict[str, str]]:
    return get_capabilities()["stt_device_options"]


def get_tts_device_options(options: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    capabilities = get_capabilities()
    runtime_options = capabilities["tts_device_options"]
    if not options:
        return runtime_options

    merged = list(runtime_options)
    known = {item["value"] for item in merged}
    for option in options:
        if option.get("value") not in known:
            merged.append(option)
            known.add(option.get("value", ""))
    return merged


def get_stt_defaults(defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(defaults or {})
    result.setdefault("stt_device", get_capabilities()["default_stt_device"])
    return result


def get_tts_defaults(defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(defaults or {})
    result.setdefault("tts_device", get_capabilities()["default_tts_device"])
    return result


def _suffix_from_mime_type(mime_type: str | None) -> str:
    value = (mime_type or "").split(";", 1)[0].strip().lower()
    return _MIME_SUFFIXES.get(value, ".wav")


def _strip_data_url(audio_bytes_b64: str) -> str:
    if "," in audio_bytes_b64 and audio_bytes_b64.lstrip().startswith("data:"):
        return audio_bytes_b64.split(",", 1)[1]
    return audio_bytes_b64


def _resolve_device(policy: str) -> str:
    try:
        from helpers.device_utils import resolve_device

        return resolve_device(policy)[0]
    except Exception:
        pass

    capabilities = get_capabilities()
    if policy == "cpu":
        return "cpu"
    if policy.startswith("cuda") and capabilities["cuda_available"]:
        return "cuda:0" if policy == "cuda:auto" else policy
    if policy == "auto" and capabilities["cuda_available"]:
        return "cuda:0"
    return "cpu"


async def transcribe(audio: str, mime_type: str | None = None, model_name: str | None = None) -> dict[str, Any]:
    from helpers import whisper as core_whisper

    selected_model = model_name or settings.get_settings().get("stt_model_size", "base")
    try:
        signature = inspect.signature(core_whisper.transcribe)
        if "mime_type" in signature.parameters:
            return await core_whisper.transcribe(selected_model, audio, mime_type=mime_type)
    except Exception:
        pass

    return await _transcribe_portable(selected_model, audio, mime_type)


async def _transcribe_portable(model_name: str, audio: str, mime_type: str | None) -> dict[str, Any]:
    global _model, _model_name, _model_device

    import whisper

    current = settings.get_settings()
    device = _resolve_device(current.get("stt_device", "auto"))
    if _model is None or _model_name != model_name or _model_device != device:
        _model = whisper.load_model(
            name=model_name,
            device=device,
            download_root=files.get_abs_path("/tmp/models/whisper"),
        )
        _model_name = model_name
        _model_device = device

    audio_bytes = base64.b64decode(_strip_data_url(audio))
    suffix = _suffix_from_mime_type(mime_type)

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as audio_file:
        audio_file.write(audio_bytes)
        temp_path = audio_file.name

    try:
        try:
            import torch

            use_fp16 = "cuda" in device.lower() and torch.cuda.is_available()
        except Exception:
            use_fp16 = False
        return _model.transcribe(temp_path, fp16=use_fp16)
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass


def _lifecycle():
    prefix = 'usr.plugins' if Path(__file__).resolve().parents[2].parent.name == 'usr' else 'plugins'
    return importlib.import_module(prefix + '._convo.helpers.lifecycle')


@_lifecycle().owned_patch('capabilities', [('helpers.settings', (
    'get_stt_device_options', 'get_tts_device_options', 'get_stt_defaults', 'get_tts_defaults'))])
def patch_runtime() -> None:
    try:
        from helpers import settings as settings_module

        settings_module.get_stt_device_options = get_stt_device_options
        settings_module.get_tts_device_options = get_tts_device_options
        settings_module.get_stt_defaults = get_stt_defaults
        settings_module.get_tts_defaults = get_tts_defaults
    except Exception:
        pass
