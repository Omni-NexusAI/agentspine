"""The settings and diagnostics actions of api/conversation.py, on a stubbed host API base class. Owned actions answer with
typed data, refusals have no side effect, and errors come back typed instead of raising into the host."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

# Imported before the host stubs are installed, so the module cache other tests rely on is left exactly as it was.
from plugins._convo.helpers import convo_metrics, convo_model_catalog, convo_service, convo_setup, convo_system_one  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


class Routes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        host = types.ModuleType('helpers'); host.__path__ = []
        api = types.ModuleType('helpers.api')
        api.ApiHandler = type('ApiHandler', (), {}); api.Request = object
        host.api = api
        modules = patch.dict(sys.modules, {'helpers': host, 'helpers.api': api})
        modules.start(); self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location('_convo_conversation_route_test', ROOT / 'api' / 'conversation.py')
        route = importlib.util.module_from_spec(spec); spec.loader.exec_module(route)
        self.handler = route.Conversation()

    async def call(self, **payload):
        return await self.handler.process(payload, None)

    async def test_setup_patch_hands_the_changes_to_the_settings_module_and_types_its_errors(self):
        changes = [{'path': 'style', 'base': '', 'value': 'x'}]
        with patch.object(convo_setup, 'patch', return_value={'ok': True, 'applied': ['style'], 'conflicts': []}) as write:
            self.assertEqual((await self.call(action='setup_patch', changes=changes))['applied'], ['style'])
        write.assert_called_once_with(changes)
        with patch.object(convo_setup, 'patch', side_effect=ValueError('Unknown or invalid setting path: x')):
            self.assertEqual(await self.call(action='setup_patch', changes=[]), {'ok': False, 'code': 'invalid', 'error': 'Unknown or invalid setting path: x'})
        with patch.object(convo_setup, 'patch', side_effect=convo_setup.store.Conflict('Settings changed in another view.')):
            self.assertEqual((await self.call(action='setup_patch', changes=[]))['code'], 'conflict')

    async def test_the_owned_actions_refuse_with_a_typed_reason_and_touch_nothing_without_a_host(self):
        # Outside a host (no key store, no provider, no decider address) each refuses with a typed code; none writes settings.
        with patch.object(convo_setup, 'save', side_effect=AssertionError('no settings write')), \
             patch.object(convo_setup, '_secrets', side_effect=AssertionError('no key store access')):
            expected = {'conversation_key_copy': 'unavailable', 'audio_probe': 'unavailable', 'decider_check': 'invalid',
                        'classifier_check': 'unavailable'}
            for action, code in expected.items():
                result = await self.call(action=action, provider='openai')
                self.assertEqual((result['ok'], result['code']), (False, code), action)
                self.assertNotIn('key', {k.lower() for k in result}, action)
            for request, code in (({'provider': 'anthropic'}, 'not_usable'), ({'provider': ''}, 'invalid'), ({'provider': 'other'}, 'not_configured'),
                                  ({'provider': 'openai', 'kind': 'decision'}, 'invalid')):
                result = await self.call(action='classifier_check', **request)                 # refused before settings or keys are read
                self.assertEqual((result['ok'], result['code'], result['state']), (False, code, 'refused'), request)

    async def test_an_owned_action_may_be_asynchronous_and_receives_the_request(self):
        seen = []
        async def probe(request):
            seen.append(request)
            return {'ok': True, 'verification': {'state': 'unverified'}}
        with patch.object(convo_model_catalog, 'audio_probe', probe):
            self.assertEqual((await self.call(action='audio_probe', model='omni'))['ok'], True)
        self.assertEqual(seen, [{'action': 'audio_probe', 'model': 'omni'}])
        with patch.object(convo_system_one, 'decider_check', side_effect=ValueError('The decider must be on this machine')):
            self.assertEqual((await self.call(action='decider_check'))['code'], 'invalid')
        from plugins._convo.helpers import convo_classifier_catalog
        checked = []
        async def check(request):
            checked.append(request)
            return {'ok': True, 'state': 'decision', 'message': 'm', 'models': [], 'host': 'h'}
        with patch.object(convo_classifier_catalog, 'check', check):
            self.assertEqual((await self.call(action='classifier_check', provider='llama_cpp'))['state'], 'decision')
        self.assertEqual(checked, [{'action': 'classifier_check', 'provider': 'llama_cpp'}])

    async def test_metrics_pass_the_cursor_and_refuse_a_bad_one_as_data(self):
        convo_metrics.LOG.reset(); self.addCleanup(convo_metrics.LOG.reset)
        with patch.object(convo_service, 'settings', return_value={'developer_mode': True}):
            start = (await self.call(action='metrics'))['cursor']
            convo_metrics.LOG.mark('route-test', 'policy.start')
            newer = await self.call(action='metrics', cursor=start)
            self.assertEqual([r['turn'] for r in newer['turns']], ['route-test'])
            self.assertEqual(newer['cursor'], start + 1)
            self.assertEqual((await self.call(action='metrics', cursor=-1))['code'], 'invalid')
        with patch.object(convo_service, 'settings', return_value={'developer_mode': False}):
            self.assertEqual((await self.call(action='metrics', cursor=0))['code'], 'invalid')


if __name__ == '__main__':
    unittest.main()
