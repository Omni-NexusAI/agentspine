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


class EnhancedSpeechRemoteTTSStartup(Extension):
    def execute(self, **kwargs):
        # Install the provider adapter before the optional remote-settings
        # adapter.  The former supplies blended local synthesis, while the
        # latter only augments hosts that expose the old settings APIs.
        for helper_name in ("kokoro_adapter", "whisper_adapter", "remote_tts", "runtime_capabilities"):
            try:
                _load_helper(helper_name).patch_runtime()
            except Exception:
                logger.debug("Enhanced Speech %s adapter unavailable", helper_name, exc_info=True)
