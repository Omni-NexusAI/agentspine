"""The classifier catalog (helpers/convo_classifier_catalog.py): its shape as setup_read delivers it, the normalizer that derives
the runtime keys from the Classifier card (decision, chat, the System One toggle, the read-time migration view), the locality
rule with the classifier's remote consent, the readiness rows, and Check server, which refuses a public address without consent
before sending anything and never returns a key. Pure tests: no host, no network (an httpx MockTransport stands in)."""
import copy
import json
import types
import unittest
from unittest.mock import patch

import httpx

from plugins._convo.helpers import convo_classifier_catalog as catalog
from plugins._convo.helpers import convo_model_catalog, convo_setup, setup_store
from plugins._convo.helpers import convo_system_one as bridge
from plugins._convo.helpers.convo_contract import AddressRefused, ContractError, classifier_url, decider_endpoint, endpoint, validate_settings

# The shared locality table (also in the decision plugin's tests): the conversation model's rule.
LOCAL = ('http://host.docker.internal:9931', 'http://localhost:9931/', 'http://127.0.0.1:1', 'http://[::1]:9931', 'http://LOCALHOST:9931',
         'http://192.168.0.178:9931', 'http://10.0.0.2', 'https://localhost:9931', 'https://172.16.0.4:8443', 'http://convo-runtime:8090',
         'http://100.101.102.103:9931', 'http://my-desktop.tail1234.ts.net:9931', 'http://desktop:9931', 'http://nas.local:9931')
PUBLIC_HTTPS = ('https://api.typesafe.ai', 'https://example.com:9931', 'https://localhost.evil.com:1', 'https://8.8.8.8')
PUBLIC_HTTP = ('http://example.com:9931', 'http://localhost.evil.com:1', 'http://0.0.0.0:9931', 'http://169.254.0.5:9931', 'http://100.128.0.1:9931')
INVALID = ('http://user:pw@localhost:1', 'http://localhost:1/?a=b', 'ftp://localhost:1', 'ftp://example.com', 'http://localhost:1#top',
           'http://localhost:abc', 'http://localhost:99999', 'localhost:9931', '', None, 5)
DECISION_ANSWER = {'answers': {'check': {'choice': 'yes', 'confidence': .9, 'probabilities': {'yes': .9, 'no': .1}}}}


def config(**convo):
    return validate_settings(convo)


def classifier(**fields):
    return {'roles': {'classifier': fields}}


class Catalog(unittest.TestCase):
    def test_the_catalog_has_the_contracted_shape_and_entries(self):
        data = catalog.catalog()
        self.assertEqual(list(data), ['providers', 'disabled', 'unknown_compatible', 'unknown'])
        self.assertEqual(json.loads(json.dumps(data)), data)                                   # plain JSON for setup_read
        fields = {'label': str, 'group': str, 'kinds': list, 'base': str, 'host_key': str, 'verified': bool, 'text_only': bool, 'note': str}
        for name, spec in data['providers'].items():
            self.assertEqual({key: type(value) for key, value in spec.items()}, fields, name)
            self.assertIn(spec['group'], ('decision_local', 'decision_cloud', 'chat', 'other'), name)
            self.assertTrue(spec['kinds'] and set(spec['kinds']) <= {'decision', 'chat'}, name)
            self.assertTrue(spec['label'] and spec['note'], name)
        groups = {}
        for name, spec in data['providers'].items():
            groups.setdefault(spec['group'], []).append(name)
        self.assertEqual(groups, {'decision_local': ['llama_cpp', 'lm_studio', 'unsloth_studio', 'ollama', 'vllm', 'omlx'],
                                  'decision_cloud': ['jev'], 'chat': ['openai', 'openrouter', 'google'], 'other': ['other']})
        self.assertEqual(data['providers']['llama_cpp'], {'label': 'llama.cpp', 'group': 'decision_local', 'kinds': ['decision', 'chat'],
                         'base': 'http://host.docker.internal:8080/v1', 'host_key': 'llama_cpp', 'verified': True, 'text_only': False,
                         'note': 'llama-server serves d1 models on /v1/systemone'})
        expected = {'lm_studio': ('LM Studio', 'http://host.docker.internal:1234/v1'), 'unsloth_studio': ('Unsloth Studio', ''),
                    'ollama': ('Ollama', 'http://host.docker.internal:11434/v1'), 'vllm': ('vLLM', 'http://host.docker.internal:8000/v1'),
                    'omlx': ('oMLX', 'http://host.docker.internal:8000/v1')}
        for name, (label, base) in expected.items():
            spec = data['providers'][name]
            self.assertEqual((spec['label'], spec['base'], spec['kinds'], spec['verified']), (label, base, ['decision', 'chat'], False), name)
            self.assertEqual(spec['note'], 'Check server confirms whether it answers the System One decision route', name)
        jev = data['providers']['jev']
        self.assertEqual((jev['label'], jev['kinds'], jev['base'], jev['host_key'], jev['text_only'], jev['verified']),
                         ('TypeSafe Jev', ['decision'], 'https://api.typesafe.ai', '', True, False))
        for name in ('openai', 'openrouter', 'google'):
            spec = data['providers'][name]
            self.assertEqual((spec['kinds'], spec['base'], spec['host_key']), (['chat'], convo_model_catalog.PROVIDERS[name]['base'], name), name)
        self.assertEqual({key: data['providers']['other'][key] for key in ('label', 'kinds', 'base', 'host_key')},
                         {'label': 'Other', 'kinds': ['decision', 'chat'], 'base': '', 'host_key': ''})

    def test_disabled_providers_are_the_conversation_catalogs_minus_the_ones_offered_here(self):
        data = catalog.catalog()
        self.assertEqual(data['disabled'], {name: reason for name, reason in convo_model_catalog.DISABLED.items() if name not in data['providers']})
        self.assertFalse(set(data['disabled']) & set(data['providers']))
        self.assertIn('anthropic', data['disabled'])
        self.assertNotIn('lm_studio', data['disabled'])                                       # a decision server here
        self.assertTrue(data['unknown'] and data['unknown_compatible'])
        catalog.catalog()['providers']['llama_cpp']['base'] = 'changed'                        # a copy each time
        self.assertEqual(catalog.PROVIDERS['llama_cpp']['base'], 'http://host.docker.internal:8080/v1')

    def test_status_kinds_and_effective_kind(self):
        self.assertEqual(catalog.status(''), (True, ''))
        self.assertEqual(catalog.status('jev'), (True, ''))
        self.assertEqual(catalog.status('anthropic'), (False, convo_model_catalog.DISABLED['anthropic']))
        self.assertEqual(catalog.status('a_plugin_provider'), (True, catalog.UNKNOWN_COMPATIBLE))  # the browser offered it as OpenAI-compatible
        self.assertEqual(catalog.status('Bad Id'), (False, catalog.UNKNOWN))
        cases = ((('llama_cpp', 'auto'), 'decision'), (('llama_cpp', 'chat'), 'chat'), (('jev', 'auto'), 'decision'), (('jev', 'chat'), ''),
                 (('openai', 'auto'), 'chat'), (('openai', 'decision'), ''), (('a_plugin_provider', 'auto'), 'chat'),
                 (('a_plugin_provider', 'decision'), ''), (('anthropic', 'auto'), ''), (('', 'auto'), ''), (('other', 'chat'), 'chat'))
        for (provider, kind), expected in cases:
            self.assertEqual(catalog.effective_kind({'provider': provider, 'kind': kind}), expected, (provider, kind))

    def test_bases_roots_and_host_keys(self):
        self.assertEqual(catalog.resolve_base({'provider': 'llama_cpp'}), 'http://host.docker.internal:8080/v1')
        self.assertEqual(catalog.resolve_base({'provider': 'llama_cpp', 'api_base': ' http://192.168.1.5:9931/v1/ '}), 'http://192.168.1.5:9931/v1')
        self.assertEqual(catalog.resolve_base({'provider': 'unsloth_studio'}), '')
        # A bare server address (Agent Zero's own local defaults have no path, e.g. Ollama's) gets the provider's /v1.
        self.assertEqual(catalog.resolve_base({'provider': 'ollama', 'api_base': 'http://host.docker.internal:11434'}), 'http://host.docker.internal:11434/v1')
        self.assertEqual(catalog.resolve_base({'provider': 'lm_studio', 'api_base': 'http://192.168.0.5:1234/'}), 'http://192.168.0.5:1234/v1')
        self.assertEqual(catalog.resolve_base({'provider': 'ollama', 'api_base': 'http://h:11434/custom'}), 'http://h:11434/custom')   # an explicit path stays
        self.assertEqual(catalog.resolve_base({'provider': 'a_plugin_provider', 'api_base': 'http://h:4000'}), 'http://h:4000')         # no catalog base to follow
        self.assertEqual(catalog.resolve_base({'provider': 'a_plugin_provider'}), '')               # no default for an unknown provider
        self.assertEqual(catalog.resolve_base({'provider': 'anthropic', 'api_base': 'http://localhost:1/v1'}), '')
        self.assertEqual(catalog.resolve_base({'provider': '', 'url': 'http://localhost:8081/v1/'}), 'http://localhost:8081/v1')
        for base, root in (('http://h:1/v1', 'http://h:1'), ('http://h:1/v1/', 'http://h:1'), ('https://api.typesafe.ai', 'https://api.typesafe.ai'),
                           ('http://h:1/p/v1', 'http://h:1/p'), ('http://h:1/v1/systemone', 'http://h:1'), ('https://openrouter.ai/api/v1', 'https://openrouter.ai/api')):
            self.assertEqual(catalog.server_root(base), root, base)
        for provider, key in (('llama_cpp', 'llama_cpp'), ('openai', 'openai'), ('jev', ''), ('other', ''), ('unsloth_studio', ''),
                              ('a_plugin_provider', 'a_plugin_provider'), ('anthropic', ''), ('', '')):
            self.assertEqual(catalog.host_key({'provider': provider}), key, provider)


class Normalizer(unittest.TestCase):
    def save(self, convo):
        return setup_store.normalize(copy.deepcopy(convo))

    def runtime(self, saved):
        return (saved['system_one'], {k: saved['decider'][k] for k in ('source', 'url', 'model')}, saved['roles']['classifier']['url'])

    def test_the_matrix_of_decision_chat_and_the_toggle(self):
        off = {'enabled': False, 'backend': ''}
        d1 = {'enabled': True, 'backend': 'd1_llama_cpp'}
        cases = (
            ({}, (off, {'source': '', 'url': '', 'model': ''}, '')),
            ({'decider': {'source': 'system_one'}, **classifier(provider='openrouter', remote=True)}, (d1, {'source': 'system_one', 'url': '', 'model': ''}, '')),
            ({'decider': {'source': 'system_one'}}, (d1, {'source': 'system_one', 'url': '', 'model': ''}, '')),
            (classifier(provider='llama_cpp', model='d1'), (d1, {'source': 'convo', 'url': 'http://host.docker.internal:8080', 'model': 'd1'}, '')),
            (classifier(provider='llama_cpp', kind='decision', api_base='http://localhost:9931/v1/'), (d1, {'source': 'convo', 'url': 'http://localhost:9931', 'model': ''}, '')),
            (classifier(provider='llama_cpp', kind='decision', api_base='http://192.168.1.5:9931/v1'), (d1, {'source': 'convo', 'url': '', 'model': ''}, '')),
            (classifier(provider='jev', remote=True, model='jev-latest'), (d1, {'source': 'convo', 'url': '', 'model': 'jev-latest'}, '')),
            (classifier(provider='llama_cpp', kind='chat'), (off, {'source': '', 'url': '', 'model': ''}, 'http://host.docker.internal:8080/v1')),
            (classifier(provider='openai', remote=True), (off, {'source': '', 'url': '', 'model': ''}, 'https://api.openai.com/v1')),
            (classifier(provider='a_plugin_provider', api_base='http://192.168.1.7:4000/v1'), (off, {'source': '', 'url': '', 'model': ''}, 'http://192.168.1.7:4000/v1')),
            (classifier(provider='anthropic', url='http://localhost:8081/v1'), (off, {'source': '', 'url': '', 'model': ''}, '')),
            (classifier(provider='other', kind='decision'), (off, {'source': '', 'url': '', 'model': ''}, '')),   # no address: none invented
            (classifier(provider='', url='http://localhost:8081/v1'), (off, {'source': '', 'url': '', 'model': ''}, 'http://localhost:8081/v1')),
        )
        for convo, expected in cases:
            with self.subTest(convo=convo):
                self.assertEqual(self.runtime(self.save(convo)), expected)

    def test_provider_empty_and_toggle_off_derives_nothing(self):
        legacy = {'system_one': {'enabled': True, 'backend': 'd1_llama_cpp'}, 'decider': {'source': 'convo', 'url': 'http://localhost:9940', 'model': 'm'},
                  **classifier(url='http://localhost:8081/v1', model='policy')}
        before = config(**copy.deepcopy(legacy))
        self.assertEqual(self.save(legacy), before)
        chat = {**classifier(url='http://localhost:8081/v1')}
        self.assertEqual(self.save(chat), config(**copy.deepcopy(chat)))
        self.assertEqual(catalog.normalize({'roles': 'odd'}), {'roles': 'odd'})                 # anything malformed is left to validation

    def test_a_public_decision_server_needs_the_classifiers_consent_and_https(self):
        with self.assertRaises(AddressRefused) as caught:
            self.save(classifier(provider='jev'))
        self.assertEqual(caught.exception.code, 'remote')
        with self.assertRaises(ContractError):
            self.save(classifier(provider='other', kind='decision', api_base='http://decide.example.com/v1', remote=True))
        with self.assertRaises(ContractError):
            self.save({**classifier(provider='jev'), 'roles': {'classifier': {'provider': 'jev'}, 'conversation': {'remote': True}}})  # another role's consent
        self.assertEqual(self.save(classifier(provider='jev', remote=True))['decider']['source'], 'convo')

    def test_the_migration_view_shows_a_separate_decider_as_llama_cpp_and_saving_it_keeps_the_runtime(self):
        for decider, base in (({'source': 'convo', 'url': 'http://localhost:9940', 'model': 'd1'}, 'http://localhost:9940/v1'),
                              ({'source': 'convo', 'url': '', 'model': ''}, 'http://host.docker.internal:9931/v1'),
                              ({'source': '', 'url': '', 'model': ''}, 'http://host.docker.internal:9931/v1')):
            legacy = config(system_one={'enabled': True, 'backend': 'd1_llama_cpp'}, decider=decider)
            view = catalog.migration_view(legacy)
            self.assertEqual((view['provider'], view['kind'], view['api_base']), ('llama_cpp', 'decision', base), decider)
            saved = self.save({**legacy, 'roles': {**legacy['roles'], 'classifier': {**legacy['roles']['classifier'], **view}}})
            self.assertEqual(saved['system_one'], legacy['system_one'])
            self.assertEqual(saved['decider']['url'] or catalog.DEFAULT_DECIDER_URL, (decider['url'] or catalog.DEFAULT_DECIDER_URL))
            self.assertEqual(catalog.decision_root(saved)['url'], catalog.decision_root(legacy)['url'])     # the same server either way
        chat = config(**classifier(url='http://localhost:8081/v1', model='policy'))
        self.assertEqual(catalog.migration_view(chat), {'provider': '', 'kind': 'chat', 'api_base': '', 'model': 'policy'})
        chosen = config(**classifier(provider='openai', kind='auto', remote=True))
        self.assertEqual(catalog.migration_view(chosen)['provider'], 'openai')                   # already a card choice: shown as it is
        inherit = config(decider={'source': 'system_one'})
        self.assertEqual(catalog.migration_view(inherit)['provider'], '')
        self.assertEqual(json.dumps(chat), json.dumps(config(**classifier(url='http://localhost:8081/v1', model='policy'))))   # nothing written

    def test_routes_roots_and_the_key_address(self):
        cases = ((config(), 'chat', 'http://host.docker.internal:9931', ''),
                 (config(system_one={'enabled': True, 'backend': 'other'}), 'backend', 'http://host.docker.internal:9931', ''),
                 (config(system_one={'enabled': True, 'backend': 'd1_llama_cpp'}), 'plugin', 'http://host.docker.internal:9931', ''),
                 (self.save({'decider': {'source': 'system_one'}}), 'system_one', 'http://host.docker.internal:9931', ''),
                 (self.save(classifier(provider='vllm', kind='decision', api_base='http://192.168.1.5:8000/v1')), 'decision', 'http://192.168.1.5:8000', 'http://192.168.1.5:8000'),
                 (self.save(classifier(provider='openai', remote=True)), 'chat', '', 'https://api.openai.com/v1'))
        for value, route, root, address in cases:
            self.assertEqual((catalog.route(value), catalog.decision_root(value)['url'], catalog.key_address(value)), (route, root, address))
        self.assertEqual(catalog.decision_root({'decider': {'url': 'http://localhost:1/'}}), {'url': 'http://localhost:1', 'path': False})


class Locality(unittest.TestCase):
    def outcome(self, url, remote=False, path=True):
        try:
            classifier_url(url, remote, path)
            return None
        except AddressRefused as refused:
            return refused.code

    def test_the_consent_matrix(self):
        for url in LOCAL:
            self.assertEqual((self.outcome(url), self.outcome(url, True)), (None, None), url)
        for url in PUBLIC_HTTPS:
            self.assertEqual((self.outcome(url), self.outcome(url, True)), ('remote', None), url)
        for url in PUBLIC_HTTP:
            self.assertEqual((self.outcome(url), self.outcome(url, True)), ('remote', 'invalid'), url)
        for url in INVALID:
            self.assertEqual((self.outcome(url), self.outcome(url, True)), ('invalid', 'invalid'), url)
        self.assertEqual((self.outcome('http://localhost:1/x'), self.outcome('http://localhost:1/x', path=False)), (None, 'invalid'))
        self.assertEqual(self.outcome('https://example.com', remote=1), 'remote')               # consent is exactly true

    def test_it_is_the_conversation_models_rule_and_every_copy_agrees(self):
        for url in LOCAL + PUBLIC_HTTPS + PUBLIC_HTTP + ('http://user:pw@localhost:1', 'ftp://localhost:1', 'http://localhost:1#top'):
            for remote in (False, True):
                try:
                    endpoint({'url': url, 'remote': remote})
                    conversation = None
                except ContractError:
                    conversation = 'refused'
                self.assertEqual(conversation, 'refused' if self.outcome(url, remote) else None, (url, remote))
                refusal = bridge.url_refusal(url, remote, path=True)
                self.assertEqual(refusal[0] if refusal else None, self.outcome(url, remote), (url, remote))
                try:
                    decider_endpoint(url, remote)
                    stored = None
                except AddressRefused as refused:
                    stored = refused.code
                self.assertEqual(stored, self.outcome(url, remote, path=False), (url, remote))

    def test_the_settings_contract_applies_the_classifiers_own_consent(self):
        self.assertEqual(config(**classifier(api_base='https://api.typesafe.ai', remote=True))['roles']['classifier']['api_base'], 'https://api.typesafe.ai')
        for convo in (classifier(api_base='https://api.typesafe.ai'), classifier(api_base='http://api.typesafe.ai', remote=True),
                      {'decider': {'url': 'https://decide.example.com'}}, {'decider': {'url': 'https://decide.example.com'}, 'roles': {'conversation': {'remote': True}}}):
            with self.subTest(convo=convo), self.assertRaises(ContractError):
                config(**convo)
        self.assertEqual(config(decider={'url': 'https://decide.example.com'}, **classifier(remote=True))['decider']['url'], 'https://decide.example.com')


class ReadinessRows(unittest.TestCase):
    def rows(self, convo):
        return {row['id']: row for row in catalog.readiness_rows(setup_store.normalize(copy.deepcopy(convo)))}

    def test_rows_say_what_each_choice_does_without_contacting_anything(self):
        with patch.object(httpx, 'AsyncClient', side_effect=AssertionError('no network')):
            self.assertEqual(self.rows({}), {})                                                 # the endpoint as entered: other rows say it
            self.assertEqual(self.rows({'decider': {'source': 'system_one'}, **classifier(provider='llama_cpp')}), {})
            verified = self.rows(classifier(provider='llama_cpp'))
            self.assertEqual((verified['classifier_provider']['state'], verified['classifier_provider']['kind']), ('ready', 'decision'))
            self.assertIn('host.docker.internal:8080', verified['classifier_provider']['detail'])
            unverified = self.rows(classifier(provider='lm_studio'))
            self.assertEqual(unverified['classifier_provider']['state'], 'unverified')
            self.assertIn('Check server confirms', unverified['classifier_provider']['detail'])
            chat = self.rows(classifier(provider='openrouter', remote=True))
            self.assertEqual((chat['classifier_provider']['state'], chat['classifier_provider']['kind']), ('ready', 'chat'))
            self.assertEqual(self.rows(classifier(provider='anthropic'))['classifier_provider']['state'], 'missing')
            self.assertIn('needs an API base', self.rows(classifier(provider='unsloth_studio'))['classifier_provider']['detail'])
            self.assertIn('does not offer a decision', self.rows(classifier(provider='openai', kind='decision', remote=True))['classifier_provider']['detail'])
            jev = self.rows(classifier(provider='jev', remote=True))
            self.assertEqual(jev['classifier_text_only']['state'], 'unsupported')               # an audio conversation cannot use it
            text = self.rows({'roles': {'classifier': {'provider': 'jev', 'remote': True}, 'conversation': {'capabilities': {'audio_input': False}}}})
            self.assertNotIn('classifier_text_only', text)
            routed = self.rows(classifier(provider='openai', remote=True, api_base='https://proxy.example/v1'))
            self.assertEqual(routed['classifier_key_route']['state'], 'warning')
            self.assertNotIn('classifier_key_route', self.rows(classifier(provider='openai', remote=True, api_base='https://proxy.example/v1', key_source='convo')))
            env = self.rows(classifier(provider='openai', remote=True, token_env='MY_REF'))
            self.assertIn('MY_REF', env['classifier_provider']['detail'])


class CheckServer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved = config()
        self.keys = {}
        for name, value in (('_read_config', lambda: (self.saved, 'rev')), ('_secrets', lambda: types.SimpleNamespace(load_secrets=lambda: dict(self.keys)))):
            patcher = patch.object(convo_setup, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.seen = []

    def server(self, decide=200, models=200, health=200, answer=DECISION_ANSWER, listing=None, error=None):
        def handler(request):
            self.seen.append((request.method, str(request.url), request.headers.get('authorization')))
            if error:
                raise error
            if request.url.path.endswith('/systemone'):
                return httpx.Response(decide, json=answer if decide == 200 else {'error': {'message': 'key sk-secret-text rejected'}})
            if request.url.path.endswith('/health'):
                return httpx.Response(health, json={'status': 'ok'})
            return httpx.Response(models, json=listing if listing is not None else {'data': [{'id': 'd1-omni-600M'}]})
        return httpx.MockTransport(handler)

    async def check(self, transport=None, **request):
        return await catalog.check(request, transport=transport or self.server())

    async def test_a_public_address_without_consent_is_refused_before_anything_is_sent(self):
        for request in ({'provider': 'jev'}, {'provider': 'openrouter'}, {'provider': 'other', 'api_base': 'https://decide.example.com/v1'},
                        {'provider': 'llama_cpp', 'api_base': 'https://decide.example.com/v1'}):
            result = await self.check(**request)
            self.assertEqual((result['ok'], result['code'], result['state']), (False, 'remote', 'refused'), request)
            self.assertIn('Allow remote processing', result['message'])
        result = await self.check(provider='other', api_base='http://decide.example.com/v1')
        self.assertEqual(result['code'], 'remote')
        self.saved = config(**classifier(remote=True))
        result = await self.check(provider='other', api_base='http://decide.example.com/v1')
        self.assertEqual(result['code'], 'invalid')                                              # consent never permits plain http
        self.assertEqual(self.seen, [])

    async def test_invalid_requests_are_refused_typed(self):
        for request, code in (({'provider': ''}, 'invalid'), ({'provider': 'anthropic'}, 'not_usable'), ({'provider': 'openai', 'kind': 'decision'}, 'invalid'),
                              ({'provider': 'unsloth_studio'}, 'not_configured'), ({'provider': 'llama_cpp', 'kind': 'mystery'}, 'invalid'),
                              ({'provider': 'llama_cpp', 'model': 5}, 'invalid'), ({'provider': 'llama_cpp', 'api_base': 'x' * 2001}, 'invalid')):
            result = await self.check(**request)
            self.assertEqual((result['ok'], result['code']), (False, code), request)
        self.assertEqual((await catalog.check('nope'))['code'], 'invalid')
        self.assertEqual(self.seen, [])

    async def test_a_decision_server_is_listed_then_asked_one_tiny_text_decision(self):
        result = await self.check(provider='llama_cpp', api_base='http://localhost:9931/v1', model='d1-omni-600M')
        self.assertEqual([(method, url) for method, url, _ in self.seen],
                         [('GET', 'http://localhost:9931/v1/models'), ('GET', 'http://localhost:9931/health'), ('POST', 'http://localhost:9931/v1/systemone')])
        self.assertEqual((result['ok'], result['state'], result['models'], result['host']), (True, 'decision', ['d1-omni-600M'], 'localhost:9931'))
        self.assertIn('no audio was sent', result['message'])

    async def test_a_cloud_decision_server_with_consent_gets_its_root_routes_and_the_key_only_server_side(self):
        self.saved = setup_store.normalize(classifier(provider='jev', remote=True, key_source='convo'))
        self.keys = {'CONVO_CLASSIFIER_API_KEY': 'classifier-secret', 'CONVO_CLASSIFIER_API_KEY_URL': 'https://api.typesafe.ai'}
        result = await self.check(provider='jev')
        self.assertEqual([(method, url) for method, url, _ in self.seen],
                         [('GET', 'https://api.typesafe.ai/health'), ('GET', 'https://api.typesafe.ai/v1/models'), ('POST', 'https://api.typesafe.ai/v1/systemone')])
        self.assertEqual({authorization for _, _, authorization in self.seen}, {'Bearer classifier-secret'})
        self.assertEqual(result['state'], 'decision')
        self.assertIn('text only', result['message'])
        self.assertNotIn('classifier-secret', json.dumps(result))

    async def test_each_answer_is_described_honestly_and_no_server_text_is_echoed(self):
        cases = ((dict(decide=404), 'chat'), (dict(decide=200, answer={'answers': {}}), 'chat'), (dict(error=httpx.ConnectError('refused')), 'unreachable'),
                 (dict(decide=401, models=401, health=401), 'refused'), (dict(health=503), 'unverified'),
                 (dict(decide=500, models=500, health=500), 'unverified'))
        for options, state in cases:
            self.seen = []
            result = await self.check(self.server(**options), provider='llama_cpp')
            self.assertEqual(result['state'], state, options)
            self.assertNotIn('sk-secret', json.dumps(result))
            if options == dict(health=503):
                self.assertNotIn('POST', [method for method, _, _ in self.seen])               # still loading: no decision is sent
            if state == 'unreachable':
                self.assertNotIn('POST', [method for method, _, _ in self.seen])
        named = await self.check(provider='llama_cpp', model='another-model')
        self.assertEqual(named['state'], 'decision')
        self.assertIn('not “another-model”', named['message'])
        unlisted = await self.check(self.server(decide=404), provider='llama_cpp', model='another-model')
        self.assertEqual(unlisted['state'], 'unverified')

    async def test_a_chat_classifier_is_only_asked_for_its_model_list(self):
        self.saved = config(**classifier(remote=True))
        result = await self.check(provider='openrouter', model='d1-omni-600M')
        self.assertEqual([(method, url) for method, url, _ in self.seen], [('GET', 'https://openrouter.ai/api/v1/models')])
        self.assertEqual(result['state'], 'chat')
        self.seen = []
        result = await self.check(provider='llama_cpp', kind='chat')
        self.assertEqual([(method, url) for method, url, _ in self.seen], [('GET', 'http://host.docker.internal:8080/v1/models')])

    async def test_a_redirect_is_not_followed_and_one_check_runs_at_a_time(self):
        def handler(request):
            self.seen.append(str(request.url))
            return httpx.Response(302, headers={'location': 'https://elsewhere.example/v1/models'})
        result = await self.check(httpx.MockTransport(handler), provider='llama_cpp', kind='chat')
        self.assertEqual(self.seen, ['http://host.docker.internal:8080/v1/models'])
        self.assertEqual(result['state'], 'unverified')
        catalog._checking = True
        try:
            self.assertEqual((await self.check(provider='llama_cpp'))['code'], 'busy')
        finally:
            catalog._checking = False

    async def test_unreadable_settings_or_key_store_send_nothing(self):
        with patch.object(convo_setup, '_read_config', side_effect=RuntimeError('store unavailable')):
            self.assertEqual((await self.check(provider='llama_cpp'))['code'], 'unavailable')
        with patch.object(convo_setup, '_secrets', side_effect=RuntimeError('key store unavailable')):
            self.assertEqual((await self.check(provider='llama_cpp'))['code'], 'unavailable')
        self.assertEqual(self.seen, [])
        self.assertFalse(catalog._checking)


if __name__ == '__main__':
    unittest.main()
