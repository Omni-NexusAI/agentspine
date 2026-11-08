from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from helpers.extension import Extension


logger = logging.getLogger("enhanced_speech")


def _load_helper(name: str):
    plugin_root = Path(__file__).resolve().parents[3]
    helper_path = plugin_root / "helpers" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"agentspine_convo_{name}", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load enhanced speech helper from {helper_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnhancedSpeechInit(Extension):
    def execute(self, **kwargs):
        # The Kokoro adapter owns voice blending and must be installed even on
        # hosts that no longer expose the legacy remote-TTS settings helpers.
        # Keep optional integrations isolated so a missing one cannot prevent
        # the other from patching the provider runtime.
        for helper_name in ("kokoro_adapter", "whisper_adapter", "remote_tts"):
            try:
                _load_helper(helper_name).patch_runtime()
            except Exception:
                # Host lifecycle and provider packages differ between A0
                # targets. Loading this optional integration must never
                # disable native TTS.
                logger.debug("Enhanced Speech %s adapter unavailable", helper_name, exc_info=True)
        if getattr(self, "agent", None):
            self.agent.set_data("enhanced_speech_loaded", True)
