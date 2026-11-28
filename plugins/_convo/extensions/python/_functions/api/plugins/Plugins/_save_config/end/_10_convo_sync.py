from __future__ import annotations

import importlib.util
from pathlib import Path

from helpers.extension import Extension


def _notify(message: str, group: str) -> None:
    try:
        from helpers.notification import NotificationManager, NotificationPriority, NotificationType

        NotificationManager.send_notification(
            type=NotificationType.SUCCESS,
            priority=NotificationPriority.NORMAL,
            message=message,
            title="Kokoro TTS",
            display_time=3,
            group=group,
        )
    except Exception:
        pass


def _load_config_helper():
    plugin_root = Path(__file__).resolve().parents[8]
    helper_path = plugin_root / "helpers" / "config.py"
    spec = importlib.util.spec_from_file_location("agentspine_convo_config", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load enhanced speech config helper from {helper_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnhancedSpeechSaveSync(Extension):
    def execute(self, data=None, **kwargs):
        if not isinstance(data, dict):
            return
        args = data.get("args") or ()
        if len(args) < 2 or not isinstance(args[1], dict):
            return
        payload = args[1]
        plugin_name = payload.get("plugin_name", "")
        settings = payload.get("settings", {})
        # Convo's general settings edit models/personality, not native voice
        # settings. A stale aggregate must never overwrite a voice selected in
        # the native Kokoro panel. Explicit legacy speech_config writes still
        # use their dedicated provider bridge.
        if plugin_name == '_convo':
            payload.pop('_agentspine_speech_before', None)
            return
        if plugin_name not in {"_convo", "enhanced_speech", "_kokoro_tts", "_whisper_stt"}:
            return
        before = payload.pop("_agentspine_speech_before", None)
        helper = _load_config_helper()
        if plugin_name in {"_convo", "enhanced_speech"}:
            helper.sync_providers_from_enhanced()
            raw_after = settings.get("tts", {}).get("kokoro", {}) if isinstance(settings, dict) else {}
            after = helper.normalize_kokoro_config(raw_after)
        else:
            helper.sync_enhanced_from_provider(plugin_name, settings if isinstance(settings, dict) else {})
            after = helper.normalize_kokoro_config(settings) if plugin_name == "_kokoro_tts" else None

        if isinstance(before, dict) and isinstance(after, dict):
            voice_changed = any(
                before.get(key) != after.get(key)
                for key in ("voice", "primary_voice", "secondary_voice", "voice_blend")
            )
            if voice_changed:
                secondary = str(after.get("secondary_voice") or "").strip()
                if secondary:
                    ratio = int(after.get("voice_blend") or 50)
                    _notify(
                        f"Kokoro voice changed to {after.get('voice')} ({ratio}%) + {secondary} ({100 - ratio}%).",
                        "kokoro-voice",
                    )
                else:
                    _notify(f"Kokoro voice changed to {after.get('voice')}.", "kokoro-voice")
            if before.get("speed") != after.get("speed"):
                _notify(f"Kokoro voice speed changed to {after.get('speed')}.", "kokoro-speed")
