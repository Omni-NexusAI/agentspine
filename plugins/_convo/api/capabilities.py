from helpers.api import ApiHandler, Request, Response
import importlib
from pathlib import Path


class EnhancedSpeechCapabilities(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        root = Path(__file__).resolve().parent.parent
        prefix = 'usr.plugins' if root.parent.parent.name == 'usr' else 'plugins'
        return importlib.import_module(prefix + '._convo.helpers.runtime_capabilities').get_capabilities()

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST"]
