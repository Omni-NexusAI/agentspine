from __future__ import annotations

import importlib.util
from pathlib import Path

from helpers.api import ApiHandler, Request, Response


VOICE_OPTIONS = [
    *({"value": name, "label": f"US | Female | American | {name}"} for name in (
        "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
        "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    )),
    *({"value": name, "label": f"US | Male | American | {name}"} for name in (
        "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
        "am_onyx", "am_puck", "am_santa",
    )),
    *({"value": name, "label": f"UK | Female | British | {name}"} for name in (
        "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    )),
    *({"value": name, "label": f"UK | Male | British | {name}"} for name in (
        "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    )),
]


def _gpu_helper():
    helper_path = Path(__file__).resolve().parent.parent / "helpers" / "gpu.py"
    spec = importlib.util.spec_from_file_location("agentspine_convo_gpu_voices", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Enhanced Speech GPU helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Voices(ApiHandler):
    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST"]

    async def process(self, input: dict, request: Request) -> dict | Response:
        values = [
            str(input.get(key, "") or "").strip()
            for key in ("voice", "primary_voice", "secondary_voice")
        ]
        seen = {item["value"] for item in VOICE_OPTIONS}
        options = list(VOICE_OPTIONS)
        for value in values:
            if value and value not in seen:
                seen.add(value)
                options.append({"value": value, "label": value})
        cuda_devices = _gpu_helper().get_cuda_devices()
        return {
            "success": True,
            "voices": options,
            "cuda_available": bool(cuda_devices),
            "cuda_devices": cuda_devices,
        }
