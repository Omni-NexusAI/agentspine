"""Attach the reversible Studio route when the native host page is initialized."""
import importlib
from pathlib import Path
from helpers.extension import Extension


class ConvoStudioRoute(Extension):
    def execute(self, data=None, **kwargs):
        args = (data or {}).get('args', ())
        if not args or not hasattr(args[0], 'runtime'):
            return
        root = next(p for p in Path(__file__).resolve().parents if p.name == '_convo')
        prefix = 'usr.plugins' if root.parent.parent.name == 'usr' else 'plugins'
        importlib.import_module(prefix + '._convo.helpers.studio_proxy').attach(args[0].runtime.webapp)
