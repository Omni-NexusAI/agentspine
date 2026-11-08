"""The provision API reloads an updated provisioner (a scoped update while the host keeps running) and never registers twice."""
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1]))


class ReloadOnUpdate(unittest.TestCase):
    def setUp(self):
        api = types.ModuleType('helpers.api')
        api.ApiHandler = type('ApiHandler', (), {})
        api.Request = object
        helpers = types.ModuleType('helpers'); helpers.__path__ = []; helpers.api = api
        host = patch.dict(sys.modules, {'helpers': helpers, 'helpers.api': api})
        host.start(); self.addCleanup(host.stop)
        self.provision = importlib.import_module('plugins.convo_d1_decider.api.provision')
        self.provisioner = importlib.import_module('plugins.convo_d1_decider.helpers.provisioner')

    def test_a_current_module_is_used_as_it_is(self):
        self.assertIs(self.provision.core(), self.provisioner)

    def test_a_stale_module_is_unregistered_reloaded_and_registered_again_by_the_new_code(self):
        calls = []
        stale = self.provisioner
        with patch.object(stale, 'SOURCE_STAMP', -1), \
             patch.object(stale, 'status', lambda: {'registered_here': True}), \
             patch.object(stale, 'unregister', lambda: calls.append('unregister')):
            with patch('importlib.reload', side_effect=lambda module: calls.append('reload') or types.SimpleNamespace(
                    ensure_registered=lambda: calls.append('register'), SOURCE_STAMP=0)):
                fresh = self.provision.core()
        self.assertEqual(calls, ['unregister', 'reload', 'register'])
        self.assertIsNot(fresh, stale)

    def test_a_stale_module_that_was_not_registered_is_only_reloaded(self):
        calls = []
        stale = self.provisioner
        with patch.object(stale, 'SOURCE_STAMP', -1), \
             patch.object(stale, 'status', lambda: {'registered_here': False}), \
             patch.object(stale, 'unregister', lambda: calls.append('unregister')):
            with patch('importlib.reload', side_effect=lambda module: calls.append('reload') or types.SimpleNamespace(
                    ensure_registered=lambda: calls.append('register'), SOURCE_STAMP=0)):
                self.provision.core()
        self.assertEqual(calls, ['unregister', 'reload'])


if __name__ == '__main__':
    unittest.main()
