"""Validate Convo settings before the host writes them; retain unknown fields."""
import importlib
from pathlib import Path


def save_plugin_config(settings=None, **kwargs):
    root = Path(__file__).resolve().parent
    prefix = "usr.plugins" if root.parent.parent.name == "usr" else "plugins"
    store = importlib.import_module(prefix + "._convo.helpers.setup_store")
    store.native_save(settings, kwargs.get('project_name'), kwargs.get('agent_profile'))
    # We committed atomically. Returning None suppresses the host's second write.
    return None


def get_plugin_config(default=None, **kwargs):
    root = Path(__file__).resolve().parent
    prefix = 'usr.plugins' if root.parent.parent.name == 'usr' else 'plugins'
    store = importlib.import_module(prefix + '._convo.helpers.setup_store')
    return store.native_read(default, kwargs.get('project_name'), kwargs.get('agent_profile'))


def pre_update(**kwargs):
    root = Path(__file__).resolve().parent
    prefix = 'usr.plugins' if root.parent.parent.name == 'usr' else 'plugins'
    importlib.import_module(prefix + '._convo.helpers.lifecycle').state().disable()


uninstall = pre_update
