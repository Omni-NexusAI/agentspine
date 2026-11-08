from __future__ import annotations

import base64
import importlib
import importlib.util
import io
import math
from pathlib import Path
from typing import Any

import soundfile as sf

def _load_helper(name: str):
    helper_path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"agentspine_convo_{name}", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Enhanced Speech helper: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


speech_config = _load_helper("config")
remote_worker = _load_helper("remote_worker")
gpu = _load_helper("gpu")
lifecycle = _load_helper("lifecycle")


def _notify(kind: str, message: str, *, group: str, detail: str = "") -> None:
    try:
        from helpers.notification import NotificationManager, NotificationPriority, NotificationType

        notification_type = {
            "info": NotificationType.INFO,
            "success": NotificationType.SUCCESS,
            "error": NotificationType.ERROR,
        }[kind]
        NotificationManager.send_notification(
            type=notification_type,
            priority=NotificationPriority.HIGH if kind == "error" else NotificationPriority.NORMAL,
            message=message,
            detail=detail,
            title="Kokoro TTS",
            display_time=5 if kind == "error" else 3,
            group=group,
        )
    except Exception:
        pass


def _notify_remote_state(runtime: Any, success: bool, remote_url: str, error: str = "") -> None:
    state = (success, remote_url, error if not success else "")
    if getattr(runtime, "_agentspine_remote_notification_state", None) == state:
        return
    runtime._agentspine_remote_notification_state = state
    if success:
        _notify("success", f"Remote Kokoro worker ready at {remote_url}", group="kokoro-remote")
    else:
        _notify("error", "Remote Kokoro worker is unavailable.", group="kokoro-remote", detail=error)


def _resolve_local_device(policy: str) -> tuple[str, str]:
    requested = (policy or "auto").strip().lower()
    if requested == "remote":
        return "remote", "Remote worker"
    return gpu.resolve_local_device(requested)


@lifecycle.owned_patch('kokoro', [('plugins._kokoro_tts.helpers.runtime', (
    'normalize_config', 'get_config', 'synthesize_sentences', 'is_downloaded',
    '_agentspine_convo_patched', '_agentspine_original_normalize_config',
    '_agentspine_enhanced_speech_patched',
    '_agentspine_original_get_config', '_agentspine_original_synthesize_sentences',
    '_agentspine_original_is_downloaded'))])
def patch_runtime() -> bool:
    try:
        runtime = importlib.import_module("plugins._kokoro_tts.helpers.runtime")
    except Exception:
        # A0 releases without the Kokoro provider retain their native speech
        # behavior; Enhanced Speech stays loaded but intentionally inert.
        return False

    if getattr(runtime, "_agentspine_convo_patched", False):
        return True

    runtime._agentspine_original_normalize_config = getattr(runtime, "normalize_config", None)
    runtime._agentspine_original_get_config = getattr(runtime, "get_config", None)
    runtime._agentspine_original_synthesize_sentences = getattr(runtime, "synthesize_sentences", None)
    runtime._agentspine_original_is_downloaded = getattr(runtime, "is_downloaded", None)

    def normalize_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = speech_config.kokoro_runtime_config(config)
        effective_device, effective_label = _resolve_local_device(str(cfg.get("device") or "auto"))
        cfg["effective_device"] = effective_device
        cfg["effective_device_label"] = "Remote worker" if cfg.get("remote_enabled") else effective_label
        return cfg

    def get_config() -> dict[str, Any]:
        return normalize_config()

    async def synthesize_sentences(sentences: list[str], config: dict[str, Any] | None = None) -> str:
        cfg = normalize_config(config)
        if cfg.get("remote_enabled"):
            remote_url = str(cfg.get("remote_url") or "")
            try:
                voice, secondary, blend = remote_voice_pair(cfg)
                result = await remote_worker.synthesize(
                    sentences=sentences,
                    voice=voice,
                    secondary_voice=secondary,
                    voice_blend=blend,
                    speed=float(cfg.get("speed") or 1.1),
                    remote_url=remote_url,
                    remote_token=str(cfg.get("remote_token") or ""),
                    remote_timeout=float(cfg.get("remote_timeout") or 20),
                )
                _notify_remote_state(runtime, True, remote_url)
                return result
            except Exception as exc:
                _notify_remote_state(runtime, False, remote_url, str(exc))
                raise
        return await _synthesize_local(runtime, sentences, cfg)

    async def is_downloaded() -> bool:
        cfg = normalize_config()
        if cfg.get("remote_enabled"):
            status = await _worker_status_async(cfg)
            return bool(status.get("success"))
        original = original_is_downloaded
        if callable(original):
            return bool(await original())
        return getattr(runtime, "_pipeline", None) is not None

    original_is_downloaded = runtime._agentspine_original_is_downloaded
    runtime.normalize_config = normalize_config
    runtime.get_config = get_config
    runtime.synthesize_sentences = synthesize_sentences
    runtime.is_downloaded = is_downloaded
    runtime._agentspine_convo_patched = True
    runtime._agentspine_enhanced_speech_patched = True
    return True


async def _worker_status_async(cfg: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    return await asyncio.to_thread(
        remote_worker.check_worker,
        str(cfg.get("remote_url") or ""),
        str(cfg.get("remote_token") or ""),
        float(cfg.get("remote_timeout") or 3),
    )


async def _ensure_pipeline_for_device(runtime: Any, cfg: dict[str, Any]) -> None:
    if cfg.get('_convo_no_load'):
        if getattr(runtime, 'is_updating_model', False) or getattr(runtime, '_pipeline', None) is not cfg.get('_convo_pipeline'):
            raise ValueError('Kokoro changed during this response; reselect after native loading completes')
        voices = getattr(runtime._pipeline, 'voices', {})
        required = list(cfg.get('voice_weights') or {}) or [item.strip() for item in str(cfg.get('voice') or '').split(',') if item.strip()]
        if not isinstance(voices, dict) or any(voice and voice not in voices for voice in required):
            raise ValueError('Prepare the selected Kokoro voices through native preview first; Convo does not download voice embeddings')
        return
    if getattr(runtime, "is_updating_model", False):
        import asyncio

        while getattr(runtime, "is_updating_model", False):
            await asyncio.sleep(0.1)

    device, _label = _resolve_local_device(str(cfg.get("device") or "auto"))
    current_device = getattr(runtime, "_agentspine_current_device", None)
    if getattr(runtime, "_pipeline", None) is not None and current_device == device:
        return

    runtime.is_updating_model = True
    _notify("info", f"Loading Kokoro TTS model on {device}...", group="kokoro-preload")
    try:
        from kokoro import KPipeline

        runtime._pipeline = KPipeline(
            lang_code="a",
            repo_id="hexgrad/Kokoro-82M",
            device=device,
        )
        runtime._agentspine_current_device = device
        _notify("success", f"Kokoro TTS model loaded on {device}.", group="kokoro-preload")
    except Exception as exc:
        _notify("error", "Failed to load the Kokoro TTS model.", group="kokoro-preload", detail=str(exc))
        raise
    finally:
        runtime.is_updating_model = False


async def _synthesize_local(runtime: Any, sentences: list[str], cfg: dict[str, Any]) -> str:
    await _ensure_pipeline_for_device(runtime, cfg)
    pipeline = cfg.get('_convo_pipeline') if cfg.get('_convo_no_load') else runtime._pipeline
    combined_audio: list[float] = []
    voice = str(cfg.get("voice") or "am_michael")
    speed = float(cfg.get("speed") or 1.1)
    voice_value: Any = voice

    weights = cfg.get("voice_weights") or {}
    resolver = getattr(runtime, "_resolve_voice", None)
    if callable(resolver):
        voice_value = resolver(pipeline, voice, weights)
    elif weights:
        load_voice = getattr(pipeline, "load_voice", None)
        if not callable(load_voice):
            # Older Kokoro releases used this name.  Current Kokoro exposes
            # `load_voice`; prefer it so current runtime and worker behavior
            # stay aligned without removing v1.2-era compatibility.
            load_voice = getattr(pipeline, "load_single_voice", None)
        if not callable(load_voice):
            raise RuntimeError("The installed Kokoro runtime cannot load voice embeddings.")
        total = sum(weights.values())
        voice_value = None
        for voice_id, weight in weights.items():
            pack = load_voice(voice_id) * (weight / total)
            voice_value = pack if voice_value is None else voice_value + pack

    for sentence in sentences:
        if not sentence.strip():
            continue
        segments = pipeline(sentence.strip(), voice=voice_value, speed=speed)
        for segment in list(segments):
            # Current Kokoro yields `(graphemes, phonemes, audio)` tuples;
            # earlier integrations returned segment objects with `.audio`.
            audio_tensor = segment[-1] if isinstance(segment, tuple) else getattr(segment, "audio", segment)
            audio_numpy = audio_tensor.detach().cpu().numpy()
            combined_audio.extend(audio_numpy.tolist())

    if not combined_audio:
        return ""

    buffer = io.BytesIO()
    sf.write(buffer, combined_audio, 24000, format="WAV")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def remote_voice_pair(cfg: dict[str, Any]) -> tuple[str, str, int]:
    """Reject blends the existing integer-percent worker cannot represent."""
    weights = cfg.get("voice_weights") or dict.fromkeys(
        (item.strip() for item in str(cfg.get("voice") or "").split(",") if item.strip()), 1.0
    )
    if not 1 <= len(weights) <= 2:
        raise ValueError("Remote Kokoro supports one or two voices. Select at most two voices or use local mode.")
    voices = list(weights)
    if len(voices) == 1:
        return voices[0], "", 100
    percentage = 100.0 * weights[voices[0]] / sum(weights.values())
    blend = round(percentage)
    if not 1 <= blend <= 99 or not math.isclose(percentage, blend, rel_tol=0, abs_tol=1e-9):
        raise ValueError("Remote Kokoro supports whole-number blend percentages from 1 to 99. Use an exact ratio or local mode.")
    return voices[0], voices[1], blend
