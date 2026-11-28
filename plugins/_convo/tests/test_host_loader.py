"""Exercise the real host basename loader, not package-style API imports."""
import importlib.util
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT.parents[1]


def immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError('Stubbed API unexpectedly suspended')


class HostLoaderTests(unittest.TestCase):
    def test_basename_loaded_apis_use_their_own_install_namespace(self):
        host = types.ModuleType('helpers')
        files = types.ModuleType('helpers.files')
        files.get_abs_path = lambda path: str(path)
        api = types.ModuleType('helpers.api')
        api.ApiHandler = type('ApiHandler', (), {})
        api.Request = type('Request', (), {})
        api.Response = type('Response', (), {})
        ws = types.ModuleType('helpers.ws')
        ws.WsHandler = type('WsHandler', (), {})
        with patch.dict(sys.modules, {'helpers': host, 'helpers.files': files, 'helpers.api': api, 'helpers.ws': ws}):
            spec = importlib.util.spec_from_file_location('host_modules_test', HOST / 'helpers/modules.py')
            loader = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loader)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {
                'helpers': host, 'helpers.files': files, 'helpers.api': api, 'helpers.ws': ws}):
                for namespace in ('plugins', 'usr.plugins'):
                    with self.subTest(namespace=namespace):
                        target = Path(tmp).joinpath(*namespace.split('.'), '_convo', 'api')
                        target.mkdir(parents=True)
                        calls = []
                        async def transcribe(**kwargs):
                            calls.append(('transcribe', kwargs))
                            return {'text': namespace}
                        capabilities = types.SimpleNamespace(get_capabilities=lambda: {'namespace': namespace}, transcribe=transcribe)
                        worker = types.SimpleNamespace(check_worker=lambda **kwargs: calls.append(('worker', kwargs)) or {'success': True})
                        speech = types.SimpleNamespace(settings=lambda: self.fail('Unexpected service/settings initialization'))
                        resolved = {
                            namespace + '._convo.helpers.runtime_capabilities': capabilities,
                            namespace + '._convo.helpers.remote_worker': worker,
                            namespace + '._convo.helpers.convo_service': speech,
                            namespace + '._convo.helpers.convo_policy': object(),
                            namespace + '._convo.helpers.convo_transport': object(),
                        }
                        # The opposite installation must never be used as fallback.
                        def resolve(name):
                            self.assertIn(name, resolved)
                            return resolved[name]
                        loaded = {}
                        for name in ('capabilities', 'transcribe', 'worker', 'realtime'):
                            shutil.copy2(ROOT / 'api' / (name + '.py'), target / (name + '.py'))
                            loaded[name] = loader.import_module(str(target / (name + '.py')))
                            self.assertEqual(loaded[name].__package__, '')
                        with patch('importlib.import_module', side_effect=resolve):
                            result = immediate(loaded['capabilities'].EnhancedSpeechCapabilities().process({}, None))
                            self.assertEqual(result['namespace'], namespace)
                            result = immediate(loaded['transcribe'].EnhancedSpeechTranscribe().process({'audio': 'fake', 'mime_type': 'audio/wav'}, None))
                            self.assertEqual(result['text'], namespace)
                            result = immediate(loaded['worker'].Worker().process({'remote_timeout': '4'}, None))
                            self.assertTrue(result['success'])
                            self.assertIs(loaded['realtime'].modules()[0], speech)
                            self.assertEqual(loaded['realtime'].Realtime().connections, {})
                        self.assertEqual(calls, [('transcribe', {'audio': 'fake', 'mime_type': 'audio/wav', 'model_name': None}),
                                                 ('worker', {'remote_url': '', 'remote_token': '', 'remote_timeout': 4.0})])


if __name__ == '__main__':
    unittest.main()
