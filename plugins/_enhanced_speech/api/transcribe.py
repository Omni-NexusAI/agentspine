from helpers.api import ApiHandler, Request, Response
from plugins._convo.helpers.runtime_capabilities import transcribe


class EnhancedSpeechTranscribe(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        audio = input.get("audio")
        if not audio:
            return {"text": "", "error": "Missing audio payload"}

        result = await transcribe(
            audio=audio,
            mime_type=input.get("mime_type", ""),
            model_name=input.get("model_name") or None,
        )
        return result
