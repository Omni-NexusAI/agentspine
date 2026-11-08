"""System One's Decider as Convo sees it: read-only, truthful about what is and is not usable, never a credential. And Convo's
classifier decision source built on it: the verdict for every System One kind, the readiness rows, session readiness (with the
classifier's locality rule and remote consent), and the previous Check server request shape (`decider_check`)."""
import json
import sys
import types
import unittest
from unittest.mock import patch

import httpx

if __package__:
    from .test_system_one_consumer import load_sdk
else:
    from test_system_one_consumer import load_sdk

from plugins._convo.helpers import convo_system_one as bridge
from plugins._convo.helpers.convo_contract import validate_settings


def host(meta=True, toggle='enabled', config=None, broken=False):
    helpers = types.ModuleType('helpers'); helpers.__path__ = []
    plugins = types.ModuleType('helpers.plugins')
    def get_plugin_meta(name):
        if broken:
            raise RuntimeError('host unavailable')
        return {'name': name} if meta else None
    plugins.get_plugin_meta = get_plugin_meta
    plugins.get_toggle_state = lambda name: toggle
    plugins.get_plugin_config = lambda name: config or {}
    runtime = types.ModuleType('helpers.runtime')
    helpers.plugins, helpers.runtime = plugins, runtime
    return patch.dict(sys.modules, {'helpers': helpers, 'helpers.plugins': plugins, 'helpers.runtime': runtime})


SYSTEM_ONE = {'decider': {'backend': 'openrouter', 'provider': 'openrouter', 'model': 'typesafe/jev-latest', 'endpoint': 'https://api.typesafe.ai/v1/systemone',
                          'token_env': 'SECRET_ENV_NAME', 'context_window': 32000}, 'consumers': {}}


class WithoutABackend(unittest.TestCase):
    def reference(self, config=None, **options):
        with host(**options):
            return bridge.decider_reference(validate_settings(config or {}))

    def test_not_installed_and_not_registered_are_said_plainly(self):
        result = self.reference(meta=False)
        self.assertEqual((result['installed'], result['enabled'], result['ready']), (False, False, False))
        self.assertEqual(result['registered'], [])
        self.assertTrue(any('not installed' in reason for reason in result['reasons']))
        self.assertTrue(any('No decision backend is registered' in reason for reason in result['reasons']))

    def test_installed_but_off_or_not_opted_in_explains_which(self):
        off = self.reference(toggle='disabled', config=SYSTEM_ONE)
        self.assertTrue(off['installed'] and not off['enabled'])
        self.assertTrue(any('switched off' in reason for reason in off['reasons']))
        self.assertFalse(any('consumer opt-in' in reason for reason in off['reasons']))
        on = self.reference(config=None, toggle='enabled')                                    # no System One config at all
        self.assertTrue(any('consumer opt-in' in reason for reason in on['reasons']))
        opted = self.reference(toggle='enabled', config=None)
        with host(config={**SYSTEM_ONE, 'consumers': {'convo': {'enabled': True}}}):
            opted = bridge.decider_reference(validate_settings({}))
        self.assertTrue(opted['consumer_enabled'])
        self.assertFalse(any('consumer opt-in' in reason for reason in opted['reasons']))

    def test_the_configured_decider_is_described_without_secrets_or_paths(self):
        with host(config=SYSTEM_ONE):
            result = bridge.decider_reference(validate_settings({}))
        self.assertEqual((result['provider'], result['model'], result['host'], result['context_window']), ('openrouter', 'typesafe/jev-latest', 'api.typesafe.ai', 32000))
        text = json.dumps(result)
        self.assertNotIn('SECRET_ENV_NAME', text)                                              # no credential reference
        self.assertNotIn('/v1/systemone', text)                                                # a host name only, never an endpoint path
        self.assertNotIn('token', ''.join(result))
        # a configured decider is not a registered backend: nothing about it makes Convo's classifier selectable
        self.assertEqual(result['registered'], [])
        self.assertFalse(result['ready'])

    def test_odd_values_and_an_unreadable_host_never_raise(self):
        weird = {'decider': {'context_window': True, 'endpoint': 'http://[bad', 'model': 5, 'provider': None}, 'consumers': 'x'}
        with host(config=weird):
            result = bridge.decider_reference(validate_settings({}))
        self.assertEqual((result['context_window'], result['host'], result['consumer_enabled']), (None, '', False))
        broken = self.reference(broken=True)
        self.assertEqual((broken['installed'], broken['provider'], broken['ready']), (False, '', False))
        self.assertEqual(self.reference(config={'system_one': {'enabled': False, 'backend': 'x'}})['selected'], '')

    def test_selecting_an_unregistered_backend_reports_why_it_is_not_ready(self):
        with host(config=SYSTEM_ONE):
            result = bridge.decider_reference(validate_settings({'enabled': True, 'system_one': {'enabled': True, 'backend': 'ghost'}}))
        self.assertEqual((result['selected'], result['ready']), ('ghost', False))
        self.assertTrue(any('not registered' in reason for reason in result['reasons']))


class WithARegisteredBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sdk = load_sdk()

    def setUp(self):
        runtime = types.ModuleType('helpers.runtime')
        helpers = types.ModuleType('helpers'); helpers.__path__ = []; helpers.runtime = runtime
        plugins = types.ModuleType('helpers.plugins')
        plugins.get_plugin_meta = lambda name: {'name': name}
        plugins.get_toggle_state = lambda name: 'enabled'
        self.native = {**SYSTEM_ONE, 'enabled': True, 'consumers': {'convo': {'enabled': True}}}
        plugins.get_plugin_config = lambda name: self.native
        helpers.plugins = plugins
        patcher = patch.dict(sys.modules, {'helpers': helpers, 'helpers.runtime': runtime, 'helpers.plugins': plugins})
        patcher.start(); self.addCleanup(patcher.stop)

    def register(self, *combos, **limits):
        caps = self.sdk.MediaCapabilities(frozenset({'choice'}), modality_combinations=frozenset(map(frozenset, combos)), max_images=2, max_image_pixels=16,
                                          max_media_bytes=1_000_000, audio_sample_rates=frozenset({16000}), audio_channels=frozenset({1}), max_audio_seconds=30)
        backend = self.sdk.NativeDecisionClient(backend='stub', capabilities=caps, evaluate=lambda request: {'answers': {}})
        return bridge.register_backend('d1', backend, sdk=self.sdk, config_provider=lambda: self.native)

    def test_a_registered_backend_is_listed_with_what_it_declared_and_becomes_selectable(self):
        self.register(('text',), ('text', 'audio'))
        config = validate_settings({'enabled': True, 'system_one': {'enabled': True, 'backend': 'd1'}})
        result = bridge.decider_reference(config)
        (listed,) = result['registered']
        self.assertEqual(listed['id'], 'd1')
        self.assertEqual(listed['capabilities']['modality_combinations'], [['audio', 'text'], ['text']])
        self.assertEqual((listed['capabilities']['max_questions'], listed['capabilities']['max_audio_seconds'], listed['remote']), (32, 30, False))
        self.assertEqual((result['selected'], result['ready'], result['consumer_enabled']), ('d1', True, True))
        self.assertEqual(result['reasons'], [])                                                # nothing left to explain
        self.native['consumers']['convo']['enabled'] = False                                   # the opt-in is read live, never cached
        revoked = bridge.decider_reference(config)
        self.assertFalse(revoked['ready'])
        self.assertTrue(revoked['reasons'])

    def test_the_opt_in_is_judged_by_the_selected_backends_own_gate_not_only_by_the_plugin_file(self):
        """A provisioner may source the consumer opt-in itself; this view must agree with readiness, never contradict it."""
        self.native = {**SYSTEM_ONE, 'enabled': True, 'consumers': {}}                         # System One's own file has no opt-in
        gate = {'enabled': True, 'consumers': {'convo': {'enabled': True}}}                    # the backend's gate does
        caps = self.sdk.MediaCapabilities(frozenset({'choice'}), modality_combinations=frozenset({frozenset({'text'})}))
        backend = self.sdk.NativeDecisionClient(backend='stub', capabilities=caps, evaluate=lambda request: {'answers': {}})
        registration = bridge.register_backend('d1', backend, sdk=self.sdk, config_provider=lambda: gate)
        config = validate_settings({'enabled': True, 'system_one': {'enabled': True, 'backend': 'd1'}})
        result = bridge.decider_reference(config)
        self.assertEqual((result['ready'], result['consumer_enabled']), (True, True))
        self.assertFalse(any('opt-in' in reason for reason in result['reasons']))
        gate['consumers']['convo']['enabled'] = False                                          # revoked at the gate, live
        revoked = bridge.decider_reference(config)
        self.assertEqual((revoked['ready'], revoked['consumer_enabled']), (False, False))
        self.assertTrue(any('opt-in' in reason or 'disabled' in reason for reason in revoked['reasons']))
        gate['consumers'] = 'broken'                                                           # an unreadable gate is a refusal, never a crash
        self.assertFalse(bridge.decider_reference(config)['consumer_enabled'])
        bridge.unregister_backend('d1', registration)


# ---- Convo's classifier decision source (decider.source / kind / url / model)

# The same table as the decision plugin's tests and test_classifier_catalog.py: the conversation model's rule. Local and private
# addresses as they are; a public one only with the classifier's consent and over HTTPS; never credentials, a query or a fragment.
GOOD = ('http://host.docker.internal:9931', 'http://localhost:9931/', 'http://127.0.0.1:1', 'http://[::1]:9931', 'http://LOCALHOST:9931',
        'http://192.168.0.178:9931', 'http://10.0.0.2', 'https://localhost:9931')
PUBLIC_HTTPS = ('https://api.typesafe.ai', 'https://example.com:9931', 'https://localhost.evil.com:1')
PUBLIC_HTTP = ('http://example.com:9931', 'http://localhost.evil.com:1', 'http://0.0.0.0:9931')
INVALID = ('http://user:pw@localhost:1', 'http://localhost:1/x', 'http://localhost:1/?a=b', 'ftp://localhost:1',
           'http://localhost:1#top', 'http://localhost:abc', 'http://localhost:99999', 'localhost:9931', '')
AUDIO_DECIDER = {'backend': 'd1_llama_cpp', 'model': 'd1-omni-600M', 'endpoint': 'http://host.docker.internal:9944/v1/systemone', 'context_window': 4096}


class Host:
    """A stubbed host with System One's and the decision plugin's state, and Convo's backend registry (helpers.runtime)."""

    def __init__(self, test, system_one=None, installed=True, plugin='enabled'):
        self.system_one, self.installed, self.plugin = system_one, installed, plugin
        helpers = types.ModuleType('helpers'); helpers.__path__ = []
        plugins = types.ModuleType('helpers.plugins')
        plugins.get_plugin_meta = lambda name: ({'name': name} if self.installed else None) if name == 'system_1' else ({'name': name} if self.plugin else None)
        plugins.get_toggle_state = lambda name: 'enabled' if name == 'system_1' else (self.plugin or 'disabled')
        plugins.get_plugin_config = lambda name: self.system_one if name == 'system_1' else None
        helpers.plugins, helpers.runtime = plugins, types.ModuleType('helpers.runtime')
        modules = patch.dict(sys.modules, {'helpers': helpers, 'helpers.plugins': plugins, 'helpers.runtime': helpers.runtime})
        modules.start(); test.addCleanup(modules.stop)

    def register(self, name='d1_llama_cpp'):
        """A registered backend with typed, audio-capable declarations; no SDK or model is involved."""
        caps = types.SimpleNamespace(operations=frozenset({'choice'}), max_questions=32, modality_combinations=frozenset({frozenset({'audio', 'text'}), frozenset({'text'})}),
                                     max_images=0, max_image_pixels=0, max_audio_seconds=30, audio_sample_rates=frozenset({16000}), audio_channels=frozenset({1}), max_media_bytes=1)
        consumer = type('Consumer', (), {'__init__': lambda self, consumer, config_provider, backend: setattr(self, 'capabilities', backend.capabilities)})
        backend = types.SimpleNamespace(evaluate_request=lambda request: None, capabilities=caps)
        gate = lambda: {'enabled': True, 'consumers': {'convo': {'enabled': True}}}
        return bridge.register_backend(name, backend, sdk=types.SimpleNamespace(ConsumerDecisionClient=consumer), config_provider=gate)


def chosen(source, url='', backend='d1_llama_cpp', enabled=True, classifier=None, conversation=None):
    roles = {'classifier': classifier or {}, 'conversation': conversation or {}}
    return validate_settings({'enabled': True, 'system_one': {'enabled': enabled, 'backend': backend}, 'roles': roles,
                              'decider': {'source': source, 'kind': 'd1_llama_cpp', 'url': url, 'model': ''}})


class InheritVerdict(unittest.TestCase):
    def verdict(self, decider, installed=True, consent=False):
        Host(self, {'decider': decider} if decider is not None else {}, installed=installed)
        return bridge.decider_reference(validate_settings({'roles': {'classifier': {'remote': consent}}}))

    def test_every_text_only_system_one_kind_says_decides_from_text(self):
        for decider in ({'backend': 'jev', 'provider': 'jev', 'model': 'jev-latest', 'endpoint': 'https://api.typesafe.ai/v1/systemone'},
                        {'backend': 'openrouter', 'provider': 'openrouter', 'model': 'typesafe/jev-latest'},
                        {'backend': 'llama_decision', 'provider': 'llama_decision', 'model': 'local', 'endpoint': 'http://localhost:8080'},
                        {'backend': 'chat', 'provider': 'openai', 'model': 'gpt-4.1-mini'},
                        {'provider': 'llama_cpp', 'model': 'local'},                # older files: the kind follows from the provider, as in System One's editor
                        {'provider': 'anthropic', 'model': 'x'}):
            result = self.verdict(decider)
            self.assertEqual(result['audio']['state'], 'text_only', decider)
            self.assertEqual(result['audio']['label'], 'Decides from text — turn off “Use System One\'s decider”')
        self.assertEqual(self.verdict({'provider': 'llama_cpp'})['kind'], 'llama_decision')
        self.assertEqual(self.verdict({'provider': 'openai'})['kind'], 'chat')

    def test_an_audio_kind_is_usable_locally_and_on_a_public_address_only_with_consent(self):
        self.assertEqual(self.verdict(AUDIO_DECIDER)['audio']['state'], 'audio')
        for endpoint, consent, state in (('http://192.168.0.178:9944/v1/systemone', False, 'audio'), ('https://localhost:9944/v1/systemone', False, 'audio'),
                                         ('https://api.example.com/v1/systemone', False, 'remote'), ('https://api.example.com/v1/systemone', True, 'audio'),
                                         ('http://api.example.com/v1/systemone', True, 'invalid'), ('http://localhost:9944/other', True, 'invalid'),
                                         ('', True, 'not_configured')):
            self.assertEqual(self.verdict({**AUDIO_DECIDER, 'endpoint': endpoint}, consent=consent)['audio']['state'], state, (endpoint, consent))

    def test_unknown_missing_and_absent_deciders_are_never_usable(self):
        self.assertEqual(self.verdict({'backend': 'mystery', 'endpoint': 'http://localhost:1'})['audio']['state'], 'unknown')
        self.assertEqual(self.verdict(None)['audio']['state'], 'not_configured')
        self.assertEqual(self.verdict(AUDIO_DECIDER, installed=False)['audio']['state'], 'not_installed')
        for state, (label, detail) in bridge.VERDICTS.items():
            self.assertTrue(label and len(detail) > 30, state)
            self.assertEqual(state == 'audio', 'turn off “Use System One\'s decider”' not in label, state)
            self.assertNotIn('decider for Convo', label + detail)                    # the old choice no longer exists by that name

    def test_the_browser_gets_a_verdict_and_a_host_name_never_the_address_the_plugin_would_call(self):
        result = self.verdict(AUDIO_DECIDER)
        self.assertEqual((result['host'], result['kind']), ('host.docker.internal', 'd1_llama_cpp'))
        self.assertNotIn('9944', json.dumps(result))
        self.assertNotIn('/v1/systemone', json.dumps(result))
        Host(self, {'decider': AUDIO_DECIDER})
        self.assertEqual(bridge.inherited_decider()['url'], 'http://host.docker.internal:9944')              # server side only
        Host(self, {'decider': {**AUDIO_DECIDER, 'backend': 'jev'}})
        self.assertEqual(bridge.inherited_decider()['url'], '')


class UrlRule(unittest.TestCase):
    def test_local_and_private_addresses_pass_and_a_public_one_needs_consent_and_https(self):
        for url in GOOD:
            self.assertIsNone(bridge.url_refusal(url), url)
        for url in PUBLIC_HTTPS + PUBLIC_HTTP:
            code, message = bridge.url_refusal(url)
            self.assertEqual(code, 'remote', url)
            self.assertIn('microphone audio', message)
            self.assertIn('Allow remote processing', message)
        for url in PUBLIC_HTTPS:
            self.assertIsNone(bridge.url_refusal(url, True), url)
        for url in PUBLIC_HTTP + INVALID:
            self.assertEqual(bridge.url_refusal(url, True)[0], 'invalid', url)
        self.assertIsNone(bridge.url_refusal('http://localhost:1/x', path=True))

    def test_the_rule_agrees_with_the_saved_settings_contract(self):
        for url in GOOD:
            self.assertEqual(validate_settings({'decider': {'url': url}})['decider']['url'], url)
        for url in PUBLIC_HTTPS + PUBLIC_HTTP + ('http://localhost:1/x',):
            with self.assertRaises(ValueError, msg=url):
                validate_settings({'decider': {'url': url}})
        for url in PUBLIC_HTTPS:                                                          # the classifier's consent allows it
            self.assertEqual(validate_settings({'decider': {'url': url}, 'roles': {'classifier': {'remote': True}}})['decider']['url'], url)
        for url in PUBLIC_HTTP:
            with self.assertRaises(ValueError, msg=url):
                validate_settings({'decider': {'url': url}, 'roles': {'classifier': {'remote': True}}})


class DeciderRows(unittest.TestCase):
    def rows(self, config, **host):
        """The rows on a fresh stubbed host (its backend registry starts empty)."""
        Host(self, host.pop('system_one', {'decider': {'backend': 'openrouter', 'model': 'typesafe/jev-latest'}}), **host)
        return {row['id']: row for row in bridge.decider_rows(config)}

    def test_no_choice_is_one_quiet_row_that_says_where_decisions_come_from_now(self):
        rows = self.rows(validate_settings({'enabled': True, 'system_one': {'enabled': True, 'backend': 'd1_llama_cpp'}}))
        self.assertEqual(list(rows), ['decider_source'])
        self.assertEqual(rows['decider_source']['state'], 'idle')
        self.assertIn('decision plugin', rows['decider_source']['detail'])
        legacy = self.rows(validate_settings({}))
        self.assertIn('classifier endpoint as entered', legacy['decider_source']['detail'])
        self.assertEqual(legacy['decider_source']['label'], 'Classifier')

    def test_inheriting_a_text_only_decider_is_unsupported_with_its_verdict(self):
        rows = self.rows(chosen('system_one'))
        self.assertEqual((rows['decider_source']['state'], rows['decider_source']['verdict'], rows['decider_source']['source']), ('unsupported', 'text_only', 'system_one'))
        self.assertIn('Decides from text', rows['decider_source']['detail'])
        usable = self.rows(chosen('system_one'), system_one={'decider': AUDIO_DECIDER})
        self.assertEqual((usable['decider_source']['state'], usable['decider_source']['verdict']), ('ready', 'audio'))

    def test_convos_own_decision_classifier_is_described_by_host_and_not_claimed_checked(self):
        rows = self.rows(chosen('convo', 'http://localhost:9940'))
        self.assertEqual((rows['decider_source']['state'], rows['decider_source']['verdict']), ('unverified', 'configured'))
        self.assertIn('localhost:9940', rows['decider_source']['detail'])
        self.assertIn('host.docker.internal:9931', self.rows(chosen('convo'))['decider_source']['detail'])
        derived = self.rows(chosen('convo', classifier={'provider': 'vllm', 'kind': 'decision', 'api_base': 'http://192.168.1.5:8000/v1'}))
        self.assertIn('192.168.1.5:8000', derived['decider_source']['detail'])                   # the root the catalog derives
        public = self.rows(chosen('convo', classifier={'provider': 'jev'}))                      # saved before consent was withdrawn
        self.assertEqual((public['decider_source']['state'], public['decider_source']['verdict']), ('missing', 'remote'))
        missing = self.rows(chosen('convo', classifier={'provider': 'unsloth_studio', 'kind': 'decision'}))
        self.assertEqual((missing['decider_source']['state'], missing['decider_source']['verdict']), ('missing', 'not_configured'))

    def test_a_saved_choice_that_is_not_in_effect_says_so(self):
        for config in (chosen('convo', enabled=False), chosen('convo', backend='other')):
            self.assertEqual(self.rows(config)['decider_source']['state'], 'missing')

    def test_the_decision_plugin_and_its_backend_are_reported(self):
        for plugin, state in (('enabled', 'ready'), ('disabled', 'unavailable'), (None, 'missing')):
            self.assertEqual(self.rows(chosen('convo'), plugin=plugin)['decider_provisioner']['state'], state, plugin)
        self.assertEqual(self.rows(chosen('convo'))['decider_backend']['state'], 'missing')
        host = Host(self, {})
        registration = host.register()
        self.addCleanup(bridge.unregister_backend, 'd1_llama_cpp', registration)
        rows = {row['id']: row for row in bridge.decider_rows(chosen('convo'))}
        self.assertEqual(rows['decider_backend']['state'], 'ready')

    def test_rows_never_contact_anything(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('no network')), patch.object(httpx, 'AsyncClient', side_effect=AssertionError('no network')):
            for config in (chosen('system_one'), chosen('convo'), validate_settings({})):
                self.assertTrue(self.rows(config))


class SessionReadiness(unittest.TestCase):
    def test_inheriting_a_decider_that_cannot_take_audio_refuses_start_with_the_verdict(self):
        host = Host(self, {'decider': {'backend': 'openrouter', 'model': 'typesafe/jev-latest'}})
        registration = host.register()
        self.addCleanup(bridge.unregister_backend, 'd1_llama_cpp', registration)
        state = bridge.readiness(chosen('system_one'))
        self.assertFalse(state['ready'])
        self.assertIn('Decides from text', state['detail'])
        reference = bridge.decider_reference(chosen('system_one'))
        self.assertFalse(reference['ready'])
        self.assertTrue(any('Decides from text' in reason for reason in reference['reasons']))
        self.assertTrue(bridge.readiness(chosen('convo'))['ready'])                       # a separate decider is judged by registration
        self.assertTrue(bridge.readiness(chosen(''))['ready'])                            # no choice: exactly as before
        host.system_one = {'decider': AUDIO_DECIDER}
        self.assertTrue(bridge.readiness(chosen('system_one'))['ready'])

    def test_the_verdict_is_the_reason_even_before_the_backend_is_registered(self):
        Host(self, {'decider': {'backend': 'chat', 'provider': 'openai'}})
        state = bridge.readiness(chosen('system_one'))
        self.assertEqual((state['ready'], state['state']), (False, 'unavailable'))
        self.assertIn('Decides from text', state['detail'])
        self.assertIn('not registered', bridge.readiness(chosen('convo'))['detail'])                # a separate decider: registration is the reason

    def test_the_choice_only_governs_the_backend_it_names(self):
        host = Host(self, {'decider': {'backend': 'jev'}})
        registration = host.register('other')
        self.addCleanup(bridge.unregister_backend, 'other', registration)
        self.assertTrue(bridge.readiness(chosen('system_one', backend='other'))['ready'])

    def test_convos_own_decision_classifier_needs_an_allowed_address_and_a_public_one_is_remote(self):
        host = Host(self, {})
        registration = host.register()
        self.addCleanup(bridge.unregister_backend, 'd1_llama_cpp', registration)
        lan = bridge.readiness(chosen('convo', classifier={'provider': 'llama_cpp', 'kind': 'decision', 'api_base': 'http://192.168.1.5:9931/v1'}))
        self.assertEqual((lan['ready'], lan['remote']), (True, False))                      # a private network is local
        refused = bridge.readiness(chosen('convo', classifier={'provider': 'jev'}, conversation={'capabilities': {'audio_input': False}}))
        self.assertFalse(refused['ready'])
        self.assertIn('Allow remote processing', refused['detail'])                         # public without consent: start refuses
        public = bridge.readiness(chosen('convo', classifier={'provider': 'jev', 'remote': True}, conversation={'capabilities': {'audio_input': False}}))
        self.assertEqual((public['ready'], public['remote']), (True, True))                 # with consent: remote processing, said so
        audio = bridge.readiness(chosen('convo', classifier={'provider': 'jev', 'remote': True}))
        self.assertFalse(audio['ready'])
        self.assertIn('decides from text only', audio['detail'])                            # Jev cannot hear an audio conversation
        nowhere = bridge.readiness(chosen('convo', classifier={'provider': 'other', 'kind': 'decision'}))
        self.assertFalse(nowhere['ready'])
        self.assertIn('no decision server address', nowhere['detail'])                      # never the default address instead

    async def _ambient(self, config):
        return await bridge.policy({'ambient': True, 'transcript': 'hello'}, config=config)

    def test_a_public_decision_server_needs_the_separate_remote_ambient_consent(self):
        host = Host(self, {})
        registration = host.register()
        self.addCleanup(bridge.unregister_backend, 'd1_llama_cpp', registration)
        import asyncio
        config = chosen('convo', classifier={'provider': 'jev', 'remote': True}, conversation={'capabilities': {'audio_input': False}})
        with self.assertRaisesRegex(bridge.PolicyUnavailable, 'Remote ambient policy requires explicit Convo consent'):
            asyncio.run(self._ambient(config))


class PreviousCheckRequest(unittest.IsolatedAsyncioTestCase):
    """`decider_check` ({url, model, kind}, an older page's request) is Check server for a llama.cpp decision server at that
    address: the same rule (with the saved classifier consent), the same requests and answer as classifier_check."""

    def setUp(self):
        from plugins._convo.helpers import convo_setup
        self.saved = validate_settings({})
        for name, value in (('_read_config', lambda: (self.saved, 'rev')), ('_secrets', lambda: types.SimpleNamespace(load_secrets=lambda: {}))):
            patcher = patch.object(convo_setup, name, value)
            patcher.start(); self.addCleanup(patcher.stop)

    def server(self):
        self.seen = []
        def handler(request):
            self.seen.append((request.method, str(request.url), request.headers.get('authorization')))
            if request.url.path == '/v1/systemone':
                return httpx.Response(200, json={'answers': {'check': {'choice': 'yes', 'confidence': .9, 'probabilities': {'yes': .9, 'no': .1}}}})
            return httpx.Response(200, json={'status': 'ok'} if request.url.path == '/health' else {'data': [{'id': 'd1-omni-600M'}]})
        return httpx.MockTransport(handler)

    async def check(self, transport, **request):
        return await bridge.decider_check(request, transport=transport)

    async def test_a_public_or_malformed_address_is_refused_typed_and_nothing_is_sent(self):
        transport = self.server()
        for url, code in (('https://example.com:9931', 'remote'), ('http://user:pw@localhost:1', 'invalid'), ('http://localhost:1/v1', 'invalid')):
            result = await self.check(transport, url=url)
            self.assertEqual((result['ok'], result['code'], result['state']), (False, code, 'refused'), url)
        self.assertEqual((await self.check(transport, model='x'))['code'], 'invalid')                 # no address: say which one
        self.assertEqual((await self.check(transport, url='http://localhost:1', kind='jev'))['code'], 'invalid')
        self.assertEqual(self.seen, [])

    async def test_the_default_address_is_checked_as_a_llama_cpp_decision_server(self):
        result = await self.check(self.server(), url='', model='d1-omni-600M')
        self.assertEqual([(method, url) for method, url, _ in self.seen],
                         [('GET', 'http://host.docker.internal:9931/v1/models'), ('GET', 'http://host.docker.internal:9931/health'),
                          ('POST', 'http://host.docker.internal:9931/v1/systemone')])
        self.assertEqual((result['ok'], result['state'], result['models'], result['host']), (True, 'decision', ['d1-omni-600M'], 'host.docker.internal:9931'))
        self.assertTrue(all(authorization is None for _, _, authorization in self.seen))             # no key is saved here


if __name__ == '__main__':
    unittest.main()
