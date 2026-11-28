from helpers.api import ApiHandler, Request, Response
from plugins._convo.helpers.runtime_capabilities import get_capabilities


class EnhancedSpeechCapabilities(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        return get_capabilities()

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST"]
