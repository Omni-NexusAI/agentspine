"""Leave nothing registered when the plugin is disabled, updated or uninstalled."""
import importlib
from pathlib import Path


def pre_update(**kwargs):
    root = Path(__file__).resolve().parent
    prefix = "usr.plugins" if root.parent.parent.name == "usr" else "plugins"
    try:
        importlib.import_module(prefix + ".convo_d1_decider.helpers.provisioner").unregister()
    except Exception:
        pass


uninstall = pre_update
