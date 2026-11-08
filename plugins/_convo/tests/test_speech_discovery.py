"""Speech output discovery and Test connection (helpers/speech_discovery.py), reached through the settings action on temporary
settings with a mocked host, managed runtime and HTTP (see setup_harness). Servers are fakes answering by path; every request
they receive is recorded, so the tests prove that only read-only GETs from a fixed list are ever sent and that the user's own
audio.cpp server is never told to switch, load or unload a model. Nothing is loaded, started, selected or synthesized."""
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

if __package__:
    from .setup_harness import PACKAGE, SetupHarness
else:
    from setup_harness import PACKAGE, SetupHarness

JARVIS = {'id': '2098c329bc9d', 'voice': 'clone:2098c329bc9d', 'name': 'J.A.R.V.I.S', 'task': 'Base', 'language': 'English',
          'provider': 'qwen3tts-audiocpp', 'content_revision': 1, 'content_hash': 'abc'}
# Shapes as the audio.cpp voice-studio supervisor answers them (sidecar/audiocpp/supervisor.py, the adaptation of the user's container).
AUDIOCPP = {
    '/v1/models': (200, {'object': 'list', 'data': [
        {'id': 'qwen3-tts-1.7b-base-bf16', 'object': 'model', 'owned_by': 'engine', 'family': 'qwen3_tts', 'task': 'tts', 'mode': 'base', 'active': True},
        {'id': 'qwen3-tts-0.6b-base-bf16', 'object': 'model', 'owned_by': 'engine', 'family': 'qwen3_tts', 'task': 'tts', 'mode': 'base', 'active': False}]}),
    '/v1/backend/models': (200, {'available': ['qwen3-tts-1.7b-base-bf16', 'qwen3-tts-0.6b-base-bf16'], 'current': 'qwen3-tts-1.7b-base-bf16',
                                 'loaded_models': ['qwen3-tts-1.7b-base-bf16'], 'state': 'loaded', 'last_error': None, 'runtime': {'sample_rate': 24000},
                                 'events': [], 'engineEpoch': 4, 'supervisorInstanceId': 'test-only', 'singleResident': True}),
    '/v1/voices': (200, {'object': 'list', 'data': [JARVIS, {**JARVIS, 'id': '16d9bb336799', 'voice': 'clone:16d9bb336799', 'name': 'Narrator'}]}),
    '/v1/tuning/profiles': (200, {'version': 2, 'selected': {'qwen3tts-audiocpp': 'balanced'},
                                  'selections': {'voice-studio': {'qwen3tts-audiocpp': 'quality'}, 'realtime': {'qwen3tts-audiocpp': 'low-latency'}},
                                  'profiles': {'quality': {'id': 'quality', 'name': 'Quality'}, 'balanced': {'id': 'balanced', 'name': 'Balanced'},
                                               'low-latency': {'id': 'low-latency', 'name': 'Low Latency'}}}),
    '/v1/audio/speech': (405, {'detail': 'Method Not Allowed'}),                    # a POST-only route answering a read-only GET
}
AUDIOCPP_DRAFT = {'provider': 'audiocpp', 'connection': 'external', 'backend': 'openai', 'url': 'http://127.0.0.1:8890/v1',
                  'model': 'qwen3-tts-1.7b-base-bf16', 'voice': 'clone:2098c329bc9d', 'remote': False, 'token_env': ''}
LIFECYCLE_WORDS = ('switch', 'unload', 'load', 'select', 'clone', 'reset', 'import', 'delete')


class FakeServer:
    """Answers by URL path; an Exception class raises it (no answer); anything unlisted is 404. Records every request."""
    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def handler(self, request):
        self.requests.append(request)
        answer = self.routes.get(request.url.path)
        if isinstance(answer, type) and issubclass(answer, Exception):
            raise answer('no answer', request=request)
        if answer is None:
            return httpx.Response(404, json={'detail': 'Not Found'})
        status, body = answer
        return httpx.Response(status, content=body) if isinstance(body, bytes) else httpx.Response(status, json=body)

    def paths(self):
        return [request.url.path for request in self.requests]


class SpeechDiscoveryTests(SetupHarness, unittest.IsolatedAsyncioTestCase):
    def serve(self, routes):
        server = FakeServer(routes)
        module = self.setup.speech_discovery
        factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(server.handler), timeout=8, follow_redirects=False, trust_env=False)
        patcher = patch.object(module, '_client', side_effect=factory)
        patcher.start(); self.addCleanup(patcher.stop)
        return server

    def assert_read_only(self, server):
        for request in server.requests:
            self.assertEqual((request.method, request.content), ('GET', b''), str(request.url))
            self.assertIn(request.url.path.removeprefix('/v1'), self.setup.speech_discovery.READ_ROUTES, str(request.url))
            for word in LIFECYCLE_WORDS:
                self.assertNotIn(word, request.url.path.lower(), str(request.url))

    async def test_your_audiocpp_server_is_read_route_by_route_and_never_told_to_switch_load_or_unload(self):
        server = self.serve(AUDIOCPP)
        before = self.path.read_bytes()
        result = await self.setup.discover('tts', dict(AUDIOCPP_DRAFT), check=True)
        self.assertEqual(server.paths(), ['/v1/models', '/v1/backend/models', '/v1/voices', '/v1/tuning/profiles', '/v1/audio/speech'])
        self.assert_read_only(server)
        self.assertEqual([check['status'] for check in result['checks']], [200, 200, 200, 200, 405])
        self.assertEqual([check['path'] for check in result['checks']], ['/v1/models', '/v1/backend/models', '/v1/voices', '/v1/tuning/profiles', '/v1/audio/speech'])
        self.assertIn('speech route', result['checks'][-1]['answer'])
        self.assertEqual([m['id'] for m in result['models']], ['qwen3-tts-1.7b-base-bf16', 'qwen3-tts-0.6b-base-bf16'])
        self.assertEqual(result['voices'][:2], [{'id': 'clone:2098c329bc9d', 'label': 'J.A.R.V.I.S'}, {'id': 'clone:16d9bb336799', 'label': 'Narrator'}])
        self.assertEqual([item['id'] for item in result['tuning']], ['quality', 'balanced', 'low-latency'])
        self.assertEqual(result['server'], {'model': 'qwen3-tts-1.7b-base-bf16', 'loaded': True, 'state': 'loaded',
                                            'tuning': {'id': 'low-latency', 'label': 'Low Latency'}})   # the server's own current selection, shown as it is
        self.assertEqual(result['verification']['state'], 'ready')
        self.assertIn('No audio was generated', result['verification']['message'])
        self.assertEqual(self.path.read_bytes(), before)                                   # settings untouched; only the check evidence is recorded
        evidence = self.store.read(self.path.parent / 'data' / 'connections.json')['tts']
        self.assertEqual(evidence['state'], 'ready')                                        # for the exact provider tested (this unsaved draft)
        self.assertEqual({item['id']: item for item in self.setup.read()['readiness']}['tts_connection']['state'], 'unverified')   # not the saved one
        self.http.assert_not_called()

    async def test_a_model_your_server_has_not_loaded_is_reported_and_never_loaded(self):
        routes = dict(AUDIOCPP, **{'/v1/backend/models': (200, {**AUDIOCPP['/v1/backend/models'][1], 'loaded_models': [], 'state': 'unloaded'})})
        server = self.serve(routes)
        result = await self.setup.discover('tts', dict(AUDIOCPP_DRAFT))
        self.assert_read_only(server)
        self.assertEqual(result['verification']['state'], 'unverified')
        self.assertIn('never loads, switches or unloads', result['verification']['message'])
        self.assertEqual(result['server']['loaded'], False)
        other = dict(AUDIOCPP, **{'/v1/backend/models': (200, {**AUDIOCPP['/v1/backend/models'][1], 'current': 'qwen3-tts-0.6b-base-bf16',
                                                               'loaded_models': ['qwen3-tts-0.6b-base-bf16']})})
        self.serve(other)
        result = await self.setup.discover('tts', dict(AUDIOCPP_DRAFT))
        self.assertEqual(result['verification']['state'], 'unverified')
        self.assertIn('qwen3-tts-0.6b-base-bf16', result['verification']['message'])        # what the server has loaded instead

    async def test_llamacpp_says_exactly_what_the_server_answered_and_stays_not_verified(self):
        server = self.serve({'/v1/models': (200, {'object': 'list', 'data': [{'id': 'orpheus-3b-0.1-ft-q4_k_m.gguf'}]})})
        result = await self.setup.discover('tts', {'provider': 'llamacpp', 'url': 'http://host.docker.internal:8080/v1', 'model': 'orpheus-3b-0.1-ft-q4_k_m.gguf', 'voice': 'tara'})
        self.assertEqual(server.paths(), ['/v1/models', '/v1/voices', '/v1/audio/speech'])
        self.assert_read_only(server)
        self.assertEqual([(check['path'], check['status']) for check in result['checks']], [('/v1/models', 200), ('/v1/voices', 404), ('/v1/audio/speech', 404)])
        self.assertEqual(result['verification']['state'], 'unverified')
        self.assertIn('no speech route', result['verification']['message'])
        self.assertIn('llama.cpp', result['verification']['message'])
        self.assertIn('Voice discovery unavailable; your saved voice is unchanged.', result['warnings'])

    async def test_a_generic_endpoint_with_a_post_only_speech_route_and_a_listed_model_is_verified(self):
        server = self.serve({'/v1/models': (200, {'data': [{'id': 'tts-model'}, {'id': 'other'}]}),
                             '/v1/voices': (200, {'voices': [{'id': 'clone:jarvis', 'name': 'J.A.R.V.I.S'}]}),
                             '/v1/audio/speech': (405, {'detail': 'Method Not Allowed'})})
        result = await self.setup.discover('tts')                                          # the saved, older configuration: derived, not guessed
        self.assertEqual(server.paths(), ['/v1/models', '/v1/voices', '/v1/audio/speech'])
        self.assertEqual(str(server.requests[0].url), 'http://127.0.0.1:8881/v1/models')
        self.assertEqual(([m['id'] for m in result['models']], result['voices']), (['tts-model', 'other'], [{'id': 'clone:jarvis', 'label': 'J.A.R.V.I.S'}]))
        self.assertEqual(result['verification']['state'], 'ready')
        self.serve({'/v1/models': (200, {'data': [{'id': 'other'}]}), '/v1/audio/speech': (405, None)})
        result = await self.setup.discover('tts')
        self.assertEqual(result['verification']['state'], 'unverified')
        self.assertIn('saved model is not listed', result['verification']['message'])

    async def test_unsloth_studio_needs_its_address_before_anything_is_contacted(self):
        server = self.serve(AUDIOCPP)
        result = await self.setup.discover('tts', {'provider': 'unsloth_studio', 'url': ''})
        self.assertEqual(server.requests, [])
        self.assertEqual(result['verification']['state'], 'unavailable')
        self.assertIn('no documented default address', result['verification']['message'])

    async def test_a_server_that_does_not_answer_is_unavailable_and_nothing_else_is_tried(self):
        server = self.serve({'/v1/models': httpx.ConnectError})
        result = await self.setup.discover('tts', check=True)
        self.assertEqual(server.paths(), ['/v1/models'])
        self.assertEqual(result['checks'][0]['status'], None)
        self.assertEqual(result['verification']['state'], 'unavailable')
        self.assertIn('no fallback', result['verification']['message'])
        checks = {item['id']: item for item in self.setup.read()['readiness']}
        self.assertEqual(checks['tts_connection']['state'], 'unavailable')                  # the check is recorded for this exact provider

    async def test_openai_is_asked_only_whether_it_accepts_the_key_and_the_key_never_comes_back(self):
        self.host_keys['openai'] = 'host-openai-key'
        server = self.serve({'/v1/models': (200, {'object': 'list', 'data': [{'id': 'gpt-4o-mini-tts'}, {'id': 'tts-1'}, {'id': 'gpt-4.1'}]})})
        draft = {'provider': 'openai_tts', 'url': '', 'remote': True, 'model': 'gpt-4o-mini-tts', 'voice': 'coral'}
        result = await self.setup.discover('tts', draft)
        self.assertEqual([str(request.url) for request in server.requests], ['https://api.openai.com/v1/models'])
        self.assertEqual(server.requests[0].headers['authorization'], 'Bearer host-openai-key')   # Agent Zero's openai key
        self.assert_read_only(server)
        self.assertEqual(result['verification']['state'], 'ready')
        self.assertNotIn('host-openai-key', json.dumps(result))
        self.serve({'/v1/models': (401, {'error': {'message': 'Incorrect API key provided'}})})
        refused = await self.setup.discover('tts', draft)
        self.assertEqual(refused['verification']['state'], 'unavailable')
        self.assertIn('refused the key', refused['verification']['message'])

    async def test_openai_without_a_key_or_without_consent_is_never_contacted(self):
        server = self.serve({'/v1/models': (200, {'data': []})})
        result = await self.setup.discover('tts', {'provider': 'openai_tts', 'url': '', 'remote': True, 'model': 'tts-1', 'voice': 'alloy'})
        self.assertEqual(result['verification']['state'], 'unavailable')
        self.assertIn('No OpenAI key', result['verification']['message'])
        with self.assertRaises(ValueError):                                                 # the settings route answers this as "invalid"
            await self.setup.discover('tts', {'provider': 'openai_tts', 'url': '', 'remote': False, 'model': 'tts-1', 'voice': 'alloy'})
        self.assertEqual(server.requests, [])

    async def test_managed_discovery_uses_real_supervisor_available_and_voices_shapes(self):
        # Mirrors supervisor.backend_models(), not an invented OpenAI shape.
        models = {'available': ['base-f16', 'base-q8'], 'current': 'base-f16',
                  'loaded_models': ['base-f16'], 'state': 'loaded', 'runtime': {},
                  'engineEpoch': 3, 'supervisorInstanceId': 'test-only', 'singleResident': True}
        for draft in ({'backend': 'managed_audio', 'model': 'base-f16'}, {'provider': 'audiocpp', 'connection': 'managed', 'model': 'base-f16'}):
            studio = AsyncMock(side_effect=[models, {'voices': [
                {'profile_id': 'voice_one', 'name': 'One'}, {'id': 'clone:voice_two', 'name': 'Two'}]},
                {'profiles': [{'id': 'quality', 'name': 'Quality'}]}])
            transport = types.ModuleType(PACKAGE + '.convo_transport')
            transport.Sidecar = Mock(return_value=types.SimpleNamespace(studio=studio))
            service = types.ModuleType(PACKAGE + '.convo_service')
            service.settings = Mock(return_value={'sidecar': {'managed': True}})
            server = self.serve({})
            before = self.path.read_bytes()
            with patch.dict(sys.modules, {PACKAGE + '.convo_transport': transport, PACKAGE + '.convo_service': service}):
                result = await self.setup.discover('tts', draft)
            self.assertEqual([item['id'] for item in result['models']], ['base-f16', 'base-q8'])
            self.assertEqual([item['id'] for item in result['voices']], ['clone:voice_one', 'clone:voice_two'])
            self.assertEqual(result['verification']['state'], 'ready')
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(server.requests, [])
            self.http.assert_not_called()

    async def test_managed_audiocpp_with_its_runtime_off_contacts_nothing_and_says_why(self):
        studio = AsyncMock(side_effect=AssertionError('the managed runtime must not be contacted'))
        transport = types.ModuleType(PACKAGE + '.convo_transport')
        transport.Sidecar = Mock(return_value=types.SimpleNamespace(studio=studio))
        service = types.ModuleType(PACKAGE + '.convo_service')
        service.settings = Mock(return_value={'sidecar': {'managed': False}})
        with patch.dict(sys.modules, {PACKAGE + '.convo_transport': transport, PACKAGE + '.convo_service': service}):
            result = await self.setup.discover('tts', {'provider': 'audiocpp', 'connection': 'managed'})
        self.assertEqual(result['verification']['state'], 'unavailable')
        self.assertIn('Manage runtime', result['verification']['message'])
        studio.assert_not_awaited()

    async def test_native_kokoro_discovers_nothing_and_contacts_nothing(self):
        server = self.serve(AUDIOCPP)
        for draft in ({'backend': 'host_kokoro'}, {'provider': 'kokoro_native'}):
            result = await self.setup.discover('tts', draft)
            self.assertEqual((result['models'], result['voices'], result['verification']['state']), ([], [], 'unverified'))
        self.assertEqual(server.requests, [])
        self.http.assert_not_called()


class ReadOnlyProbe(unittest.IsolatedAsyncioTestCase):
    """The one GET speech discovery sends: what the server answered, bounded, never an exception, never a redirect followed."""
    async def asyncSetUp(self):
        from plugins._convo.helpers import speech_discovery
        self.module = speech_discovery

    async def probe(self, handler):
        factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=8, follow_redirects=False, trust_env=False)
        with patch.object(self.module, '_client', side_effect=factory):
            return await self.module._probe('http://127.0.0.1:8890/v1/models', {})

    async def test_statuses_bodies_and_failures_come_back_as_data(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={'location': 'http://elsewhere.example/v1/models'})
        self.assertEqual(await self.probe(handler), {'status': 302, 'data': None, 'error': ''})
        self.assertEqual(len(seen), 1)                                                     # a redirect is reported, never followed
        self.assertEqual(await self.probe(lambda request: httpx.Response(200, content=b'<html>')), {'status': 200, 'data': None, 'error': 'not_json'})
        self.assertEqual((await self.probe(lambda request: httpx.Response(200, content=b'x' * (3 * 1024 ** 2))))['error'], 'too_large')
        self.assertEqual(await self.probe(lambda request: httpx.Response(200, json={'data': []})), {'status': 200, 'data': {'data': []}, 'error': ''})
        def slow(request):
            raise httpx.ReadTimeout('slow', request=request)
        answer = await self.probe(slow)                       # the failure itself comes back too, for the card's hints (never sent on)
        self.assertIsInstance(answer.pop('failure'), httpx.ReadTimeout)
        self.assertEqual(answer, {'status': None, 'data': None, 'error': 'timeout'})
        def refused(request):
            raise httpx.ConnectError('refused', request=request)
        answer = await self.probe(refused)
        self.assertIsInstance(answer.pop('failure'), httpx.ConnectError)
        self.assertEqual(answer, {'status': None, 'data': None, 'error': 'unreachable'})

    def test_only_read_routes_are_known(self):
        self.assertEqual(self.module.READ_ROUTES, ('/models', '/backend/models', '/voices', '/tuning/profiles', '/audio/speech'))
        for plan in self.module.PLANS.values():
            self.assertTrue(set(plan) <= set(self.module.READ_ROUTES))
        for route in self.module.READ_ROUTES:
            for word in LIFECYCLE_WORDS:
                self.assertNotIn(word, route)


if __name__ == '__main__':
    unittest.main()
