"""Authenticated host API: status, register, unregister. No test hooks, no secrets."""
from __future__ import annotations

import importlib
from pathlib import Path

from helpers.api import ApiHandler, Request


def core():
    """The provisioner, reloaded when its file changed since it was loaded (or was loaded by code that predates the stamp): a
    scoped update while the host keeps running must not leave the previous code registered. The old registration is removed
    first and re-made by the new code if it existed, so the backend is never registered twice."""
    root = Path(__file__).resolve().parent.parent
    prefix = "usr.plugins" if root.parent.parent.name == "usr" else "plugins"
    module = importlib.import_module(prefix + ".convo_d1_decider.helpers.provisioner")
    current = (root / "helpers" / "provisioner.py").stat().st_mtime_ns
    if getattr(module, "SOURCE_STAMP", None) != current:
        registered = False
        try:
            registered = bool(module.status().get("registered_here"))
            module.unregister()
        except Exception:
            pass
        module = importlib.reload(module)
        if registered:
            try:
                module.ensure_registered()
            except Exception:
                pass
    return module


class Provision(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        action = input.get('action', 'status')
        c = core()
        try:
            if action == 'status':
                return {'ok': True, **c.status()}
            if action == 'register':
                return {'ok': True, **c.ensure_registered()}
            if action == 'unregister':
                return {'ok': True, **c.unregister()}
        except Exception as error:
            return {'ok': False, 'error': type(error).__name__ + ': ' + str(error)[:300]}
        return {'ok': False, 'error': 'Unknown action'}
