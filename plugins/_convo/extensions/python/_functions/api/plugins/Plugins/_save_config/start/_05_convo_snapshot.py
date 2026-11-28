from __future__ import annotations

import importlib.util
from pathlib import Path

from helpers.extension import Extension


def _load_config_helper():
    plugin_root = Path(__file__).resolve().parents[8]
    helper_path = plugin_root / "helpers" / "config.py"
    spec = importlib.util.spec_from_file_location("agentspine_convo_config_snapshot", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load enhanced speech config helper from {helper_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnhancedSpeechSaveSnapshot(Extension):
    def execute(self, data=None, **kwargs):
        if not isinstance(data, dict):
            return
        args = data.get("args") or ()
        if len(args) < 2 or not isinstance(args[1], dict):
            return
        payload = args[1]
        plugin_name = payload.get("plugin_name", "")
        if plugin_name not in {"_convo", "enhanced_speech", "_kokoro_tts"}:
            return
        helper = _load_config_helper()
        if plugin_name == "_kokoro_tts":
            before = helper.normalize_kokoro_config(helper._plugin_config("_kokoro_tts"))
        else:
            before = helper.get_effective_config()["tts"]["kokoro"]
        payload["_agentspine_speech_before"] = before
