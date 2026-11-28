"""Start only the explicitly configured, Convo-owned gateway after host startup."""
import importlib
from pathlib import Path
from helpers.extension import Extension


class ConvoRuntime(Extension):
    def execute(self, **kwargs):
        root = Path(__file__).resolve().parents[3]
        prefix = 'usr.plugins' if root.parent.parent.name == 'usr' else 'plugins'
        service = importlib.import_module(prefix + '._convo.helpers.convo_service')
        importlib.import_module(prefix + '._convo.helpers.managed_runtime').configure(service.settings())
