"""Release while Convo is still discoverable, before the host writes OFF."""
import importlib
from pathlib import Path
from helpers.extension import Extension


class ConvoRelease(Extension):
    def execute(self, data=None, **kwargs):
        args = (data or {}).get('args', ())
        values = (data or {}).get('kwargs', {})
        plugin = values.get('plugin_name', args[0] if args else None)
        enabled = values.get('enabled', args[1] if len(args) > 1 else None)
        if plugin != '_convo' or enabled is not False:
            return
        # Global cleanup is safe for scoped OFF too; the next permitted session
        # explicitly reinstalls its adapters. Never alter native toggle files.
        root = next(p for p in Path(__file__).resolve().parents if p.name == '_convo')
        prefix = 'usr.plugins' if root.parent.parent.name == 'usr' else 'plugins'
        importlib.import_module(prefix + '._convo.helpers.lifecycle').state().disable()
