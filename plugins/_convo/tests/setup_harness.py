"""Shared set-up for the settings tests: a temporary plugin config, a fake host (plugins, runtime), stubbed secrets, HTTP
that fails the test if it is reached, and Agent Zero's key store as a dict. The helpers load under a private package, so
nothing installed, no host settings and no real key store are touched."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '_convo_setup_regression'


class FakeSecrets:
    def __init__(self, path):
        self.path = path

    def read_secrets_raw(self):
        return self.path.read_text() if self.path.exists() else ''

    def load_secrets(self):
        return {key: json.loads(value) for key, value in
                (line.split('=', 1) for line in self.read_secrets_raw().splitlines() if line)}

    def save_secrets(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(value)


class SetupHarness:
    """Mix into a TestCase before it: `self.setup`, `self.store`, `self.contract` are the helper modules, `self.path` the
    plugin config, `self.secrets` Convo's key file, `self.host_keys` Agent Zero's keys by provider id (unset answers the
    host's literal 'None'), `self.http` the guarded GET and `self.manager` the managed runtime that reads must not start."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'plugin' / 'config.json'
        self.secrets = FakeSecrets(self.path.parent / 'data' / 'connections.env')
        host = types.ModuleType('helpers'); host.__path__ = []
        plugins = types.ModuleType('helpers.plugins')
        plugins.CONFIG_FILE_NAME = 'config.json'
        plugins.determine_plugin_asset_path = lambda *args: str(self.path)
        plugins.get_plugin_config = lambda *args: self.store.read(self.path)
        plugins.get_toggle_state = lambda *args: 'enabled'
        runtime = types.ModuleType('helpers.runtime')
        host.plugins = plugins; host.runtime = runtime
        managed = types.ModuleType(PACKAGE + '.managed_runtime')
        self.manager = types.SimpleNamespace(public=Mock(return_value={'state':'ready'}), start=Mock(side_effect=AssertionError('read started runtime')))
        managed.instance = lambda: self.manager
        self.host_keys = {}
        models = types.ModuleType('models')
        models.get_api_key = lambda provider: self.host_keys.get(provider, 'None')
        package = types.ModuleType(PACKAGE); package.__path__ = [str(ROOT / 'helpers')]
        self.modules = patch.dict(sys.modules, {'helpers':host, 'helpers.plugins':plugins, 'helpers.runtime':runtime, 'models':models,
            PACKAGE:package, PACKAGE+'.managed_runtime':managed})
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.loaded = []
        for name in ('convo_contract', 'setup_store', 'studio_admission', 'convo_setup'):
            spec = importlib.util.spec_from_file_location(PACKAGE+'.'+name, ROOT/'helpers'/(name+'.py'))
            module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module
            self.loaded.append(spec.name); spec.loader.exec_module(module)
        self.addCleanup(lambda: [sys.modules.pop(name, None) for name in self.loaded])
        self.store = sys.modules[PACKAGE+'.setup_store']
        self.setup = sys.modules[PACKAGE+'.convo_setup']
        self.contract = sys.modules[PACKAGE+'.convo_contract']
        self.secret_patch = patch.object(self.setup, '_secrets', return_value=self.secrets)
        self.secret_patch.start(); self.addCleanup(self.secret_patch.stop)
        self.http_patch = patch.object(self.setup, '_get', new=AsyncMock(side_effect=AssertionError('unexpected HTTP')))
        self.http = self.http_patch.start(); self.addCleanup(self.http_patch.stop)
        self.initial = {'native':{'voice':'preserve'}, 'future_top':{'enabled':True}, 'convo':{
            'enabled':True, 'sidecar':{'managed':True}, 'style':'existing persona', 'future_option':{'keep':17},
            'roles':{'conversation':{'url':'http://127.0.0.1:8080/v1','model':'local-model'},
                     'classifier':{'url':'http://127.0.0.1:8081/v1','model':'policy-model'},
                     'tts':{'url':'http://127.0.0.1:8881/v1','model':'tts-model','voice':'clone:old','backend':'openai'}}}}
        self.store.atomic_write(self.path, self.initial)
