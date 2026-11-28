"""Resume confirmed queued jobs only; uncertain work is never replayed."""
import importlib
from pathlib import Path
from helpers.extension import Extension


class ConvoJobs(Extension):
    def execute(self, **kwargs):
        root = Path(__file__).resolve().parents[3]
        prefix = "usr.plugins" if root.parent.parent.name == "usr" else "plugins"
        module = importlib.import_module(prefix + "._convo.helpers.convo_service")
        database = module.runtime_root() / "usr/plugins/_convo/data/convo.sqlite3"
        if database.is_file():
            module.service().dispatcher.start()
