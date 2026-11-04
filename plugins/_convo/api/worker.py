from __future__ import annotations

from helpers.api import ApiHandler, Request, Response

import importlib
from pathlib import Path


class Worker(ApiHandler):
    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST"]

    async def process(self, input: dict, request: Request) -> dict | Response:
        root = Path(__file__).resolve().parent.parent
        prefix = 'usr.plugins' if root.parent.parent.name == 'usr' else 'plugins'
        remote_worker = importlib.import_module(prefix + '._convo.helpers.remote_worker')
        timeout = input.get("remote_timeout", input.get("timeout", 3))
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError):
            timeout_value = 3.0
        return remote_worker.check_worker(
            remote_url=str(input.get("remote_url", "") or ""),
            remote_token=str(input.get("remote_token", "") or ""),
            remote_timeout=timeout_value,
        )
