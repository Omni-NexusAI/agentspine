from helpers.api import ApiHandler, Request, Response
import importlib
from pathlib import Path


class EnhancedSpeechTranscribe(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        audio = input.get("audio")
        if not audio:
            return {"text": "", "error": "Missing audio payload"}

        root = Path(__file__).resolve().parent.parent
        prefix = 'usr.plugins' if root.parent.parent.name == 'usr' else 'plugins'
        transcribe = importlib.import_module(prefix + '._convo.helpers.runtime_capabilities').transcribe
        result = await transcribe(
            audio=audio,
            mime_type=input.get("mime_type", ""),
            model_name=input.get("model_name") or None,
        )
        return result
