"""The conversation provider catalog (helpers/convo_model_catalog.py): which providers Convo can send audio to, where each is
reached and whose key it uses, the endpoint it derives, its readiness rows and the probe's pure parts. Nothing here contacts a
provider or reads a key; the explicit actions (key_copy, audio_probe) are tested on temporary settings in test_setup.py."""
import base64
import io
import json
import re
import sys
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from plugins._convo.helpers import convo_model_catalog as catalog
from plugins._convo.helpers import setup_store
from plugins._convo.helpers.convo_contract import ContractError, validate_audio, validate_settings

PLUGIN = Path(__file__).resolve().parents[1]
SETUP_JS = PLUGIN / 'webui' / 'conversation-setup.js'
HOST_PROVIDERS = PLUGIN.parents[1] / 'conf' / 'model_providers.yaml'
# role -> the address it is reached at; tests/model-type-ui.test.mjs checks the browser mirror against the same table.
URL_CASES = [
    ({'provider': 'openai'}, 'https://api.openai.com/v1'),
    ({'provider': 'openrouter'}, 'https://openrouter.ai/api/v1'),
    ({'provider': 'google'}, 'https://generativelanguage.googleapis.com/v1beta/openai'),
    ({'provider': 'other'}, ''),
    ({'provider': 'other', 'api_base': ' http://host.docker.internal:8080/v1/ '}, 'http://host.docker.internal:8080/v1'),
    ({'provider': 'openai', 'api_base': 'https://proxy.example/v1//'}, 'https://proxy.example/v1'),
    ({'provider': '', 'url': 'http://host.docker.internal:8080/v1', 'api_base': 'ignored'}, 'http://host.docker.internal:8080/v1'),
    ({'provider': 'lm_studio', 'api_base': 'http://host.docker.internal:1234/v1'}, ''),
    ({'provider': 'made_up'}, ''),
]


def host_chat_providers():
    """{id: {'endpoint_url':..., 'default_base':...}} from Agent Zero's provider list (the chat section), read as text."""
    text = HOST_PROVIDERS.read_text(encoding='utf-8')
    chat = text.split('\nchat:\n', 1)[1].split('\nembedding:\n', 1)[0]
    providers = {}
    for block in re.split(r'\n(?=  [a-z0-9_]+:\n)', '\n' + chat):
        name = re.match(r'\n?  ([a-z0-9_]+):', block)
        if name:
            fields = dict(re.findall(r'\n\s+(endpoint_url|default_base): "?([^"\n]+)"?', block))
            providers[name.group(1)] = fields
    return providers


class ProviderTable(unittest.TestCase):
    def test_only_the_audio_capable_routes_are_selectable_and_each_other_known_provider_says_why_not(self):
        self.assertEqual(set(catalog.PROVIDERS), {'openai', 'openrouter', 'google', 'llama_cpp', 'vllm', 'other'})
        for name in catalog.PROVIDERS:
            self.assertEqual(catalog.status(name), (True, ''), name)
        self.assertEqual(catalog.status(''), (True, ''))                                    # the endpoint exactly as entered
        for name in ('lm_studio', 'ollama', 'groq', 'mistral', 'anthropic', 'azure', 'bedrock', 'github_copilot'):
            selectable, reason = catalog.status(name)
            self.assertFalse(selectable, name)
            self.assertGreater(len(reason), 20, name)
        self.assertIn('format', catalog.status('mistral')[1])
        self.assertEqual(catalog.status('made_up'), (True, catalog.UNKNOWN_COMPATIBLE))        # e.g. a provider another plugin adds
        self.assertEqual(catalog.status('Not An Id!'), (False, catalog.UNKNOWN))
        self.assertFalse(set(catalog.PROVIDERS) & set(catalog.DISABLED))
        self.assertEqual(catalog.DISABLED, {name: catalog.REASONS[kind]['reason'] for name, kind in catalog.DISABLED_KIND.items()})
        for spec in [*catalog.REASONS.values(), catalog.UNKNOWN_SPEC]:
            self.assertTrue(0 < len(spec['short']) <= 30 and len(spec['reason']) > 20, spec)

    def test_each_provider_is_reached_at_its_openai_compatible_base_or_the_override(self):
        for role, url in URL_CASES:
            self.assertEqual(catalog.resolve_url(role), url, role)
        self.assertEqual(catalog.resolve_url(None), '')

    def test_named_providers_use_agent_zeros_key_and_a_custom_endpoint_keeps_convos(self):
        self.assertEqual({name: catalog.host_key({'provider': name}) for name in catalog.PROVIDERS},
                         {'openai': 'openai', 'openrouter': 'openrouter', 'google': 'google', 'llama_cpp': 'llama_cpp', 'vllm': 'vllm', 'other': ''})
        self.assertEqual((catalog.host_key({}), catalog.host_key({'provider': 'lm_studio'}), catalog.host_key(None)), ('', '', ''))

    def test_the_ids_and_bases_agree_with_the_v211_hosts_provider_list(self):
        # Verbatim ids and addresses from the running v2.11 test host (model_config_get, incl. providers other plugins add);
        # the on-disk conf of this checkout is an older Agent Zero and lacks several of them (llama_cpp, vllm, ...).
        host = json.loads((PLUGIN / 'tests' / 'fixtures' / 'host-chat-providers-v2.11.json').read_text(encoding='utf-8'))['providers']
        self.assertEqual(set(host) - set(catalog.PROVIDERS) - set(catalog.DISABLED), set())       # nothing falls to "unknown"
        self.assertEqual(set(catalog.PROVIDERS) - set(host), set())
        for name in ('openai', 'openrouter'):
            self.assertEqual(catalog.PROVIDERS[name]['base'] + '/models', host[name]['endpoint_url'], name)
        self.assertTrue(catalog.PROVIDERS['google']['base'].startswith(host['google']['default_base'] + '/'))
        for name in ('llama_cpp', 'vllm'):
            self.assertEqual(catalog.PROVIDERS[name]['base'], host[name]['api_base'], name)

    @unittest.skipUnless(HOST_PROVIDERS.is_file() and 'llama_cpp:' in (HOST_PROVIDERS.read_text(encoding='utf-8') if HOST_PROVIDERS.is_file() else ''),
                         'Agent Zero v2.11 provider list not in this checkout (it is in the image)')
    def test_the_ids_and_bases_agree_with_agent_zeros_own_provider_list(self):
        host = host_chat_providers()
        self.assertGreater(len(host), 10)
        # Every Agent Zero chat provider is either one Convo can use or one listed with its reason: none falls to "unknown".
        self.assertEqual(set(host) - set(catalog.PROVIDERS) - set(catalog.DISABLED), set())
        self.assertEqual(set(catalog.PROVIDERS) - set(host), set())
        # The bases are the providers' own OpenAI-compatible roots: Agent Zero lists models below the same root ...
        for name in ('openai', 'openrouter'):
            self.assertEqual(catalog.PROVIDERS[name]['base'] + '/models', host[name]['endpoint_url'], name)
        # ... and Gemini's OpenAI-compatible route sits under Google's own API host.
        self.assertTrue(catalog.PROVIDERS['google']['base'].startswith(host['google']['default_base'] + '/'))
        self.assertEqual(catalog.PROVIDERS['google']['base'].rsplit('/', 1)[1], 'openai')
        for spec in catalog.PROVIDERS.values():
            self.assertTrue(spec['base'] == '' or spec['base'].startswith('https://') or spec['base'].startswith('http://host.docker.internal:'), spec)
        # The local servers use Agent Zero's own default address for them.
        for name in ('llama_cpp', 'vllm'):
            self.assertEqual(catalog.PROVIDERS[name]['base'], host[name]['default_base'] + '/v1', name)

    def test_the_browser_catalog_is_identical_to_this_one(self):
        source = SETUP_JS.read_text(encoding='utf-8')
        table = json.loads(re.search(r'export const CONVERSATION_CATALOG = (\{.*?\n\});', source, re.S).group(1))
        self.assertEqual(table, {'providers': catalog.PROVIDERS, 'disabled': catalog.DISABLED_KIND, 'reasons': catalog.REASONS,
                                 'unknown': catalog.UNKNOWN_SPEC})


class Normalize(unittest.TestCase):
    def test_a_provider_writes_the_runtime_endpoint_and_the_endpoint_only_setup_keeps_its_url(self):
        def normalized(role):
            return catalog.normalize({'roles': {'conversation': dict(role)}})['roles']['conversation']['url']
        self.assertEqual(normalized({'provider': 'openrouter', 'url': 'http://host.docker.internal:8080/v1'}), 'https://openrouter.ai/api/v1')
        self.assertEqual(normalized({'provider': 'other', 'api_base': 'http://192.168.1.20:8080/v1/', 'url': 'x'}), 'http://192.168.1.20:8080/v1')
        self.assertEqual(normalized({'provider': 'other', 'url': 'http://host.docker.internal:8080/v1'}), '')     # Other needs its API base
        self.assertEqual(normalized({'provider': 'lm_studio', 'url': 'http://host.docker.internal:1234/v1'}), '')   # nothing invented
        self.assertEqual(normalized({'provider': '', 'url': 'http://127.0.0.1:8080/v1', 'api_base': 'http://elsewhere/v1'}), 'http://127.0.0.1:8080/v1')
        for config in ({}, {'roles': {}}, {'roles': {'conversation': 'not a role'}}, None):
            self.assertIs(catalog.normalize(config), config)

    def test_old_configurations_come_through_every_writer_unchanged(self):
        for legacy in ({}, {'roles': {'conversation': {'url': 'http://127.0.0.1:8080/v1', 'model': 'm', 'token_env': 'X'}}},
                       {'roles': {'conversation': {'url': 'https://openrouter.ai/api/v1', 'remote': True, 'model': 'g'}}}):
            self.assertEqual(setup_store.normalize(json.loads(json.dumps(legacy))), validate_settings(json.loads(json.dumps(legacy))), legacy)

    def test_a_cloud_provider_is_stored_only_with_remote_consent_and_then_reaches_its_own_base(self):
        with self.assertRaisesRegex(ContractError, 'remote'):
            setup_store.normalize({'roles': {'conversation': {'provider': 'google', 'model': 'gemini-2.5-flash'}}})
        stored = setup_store.normalize({'roles': {'conversation': {'provider': 'google', 'model': 'gemini-2.5-flash', 'remote': True}}})
        self.assertEqual(stored['roles']['conversation']['url'], catalog.PROVIDERS['google']['base'])
        local = setup_store.normalize({'roles': {'conversation': {'provider': 'other', 'api_base': 'http://host.docker.internal:8080/v1'}}})
        self.assertEqual(local['roles']['conversation']['url'], 'http://host.docker.internal:8080/v1')      # local: no consent needed


class Readiness(unittest.TestCase):
    def rows(self, **role):
        return {row['id']: row for row in catalog.readiness_rows({'roles': {'conversation': role}})}

    def test_the_rows_say_where_requests_go_and_whose_key_is_used_without_reading_any_key(self):
        guard = types.ModuleType('models')
        guard.get_api_key = lambda provider: (_ for _ in ()).throw(AssertionError('readiness read a key'))
        with patch.dict(sys.modules, {'models': guard}):
            self.assertEqual(catalog.readiness_rows({'roles': {'conversation': {'url': 'http://127.0.0.1:8080/v1'}}}), [])
            openai = self.rows(provider='openai', url='https://api.openai.com/v1')
            self.assertEqual(openai['conversation_provider']['state'], 'ready')
            self.assertIn("Agent Zero's OpenAI key", openai['conversation_provider']['detail'])
            self.assertIn('https://api.openai.com/v1', openai['conversation_provider']['detail'])
            self.assertNotIn('conversation_key_route', openai)
            other = self.rows(provider='other', api_base='http://host.docker.internal:8080/v1')
            self.assertIn("Convo's own key", other['conversation_provider']['detail'])
            self.assertIn('environment reference CONVO_X', self.rows(provider='openai', token_env='CONVO_X')['conversation_provider']['detail'])
            self.assertEqual(self.rows(provider='other')['conversation_provider']['state'], 'missing')
            unusable = self.rows(provider='lm_studio')['conversation_provider']
            self.assertEqual(unusable['state'], 'missing')
            self.assertIn(catalog.DISABLED['lm_studio'], unusable['detail'])
            self.assertEqual(self.rows(provider='made_up')['conversation_provider']['state'], 'missing')        # no API base of its own
            routed = self.rows(provider='local_router', api_base='http://192.168.0.20:4000/v1')['conversation_provider']
            self.assertEqual(routed['state'], 'unverified')
            self.assertIn(catalog.UNKNOWN_COMPATIBLE, routed['detail'])
            self.assertIn("Agent Zero's local_router key", routed['detail'])

    def test_an_unknown_compatible_provider_is_reached_at_its_api_base_with_agent_zeros_key_for_that_id(self):
        self.assertEqual(catalog.resolve_url({'provider': 'local_router'}), '')
        self.assertEqual(catalog.resolve_url({'provider': 'local_router', 'api_base': 'http://192.168.0.20:4000/v1/'}), 'http://192.168.0.20:4000/v1')
        self.assertEqual(catalog.host_key({'provider': 'local_router'}), 'local_router')
        self.assertEqual((catalog.resolve_url({'provider': 'lm_studio', 'api_base': 'http://x/v1'}), catalog.host_key({'provider': 'lm_studio'})), ('', ''))

    def test_a_warning_says_when_agent_zeros_key_would_go_to_a_custom_api_base(self):
        route = self.rows(provider='openrouter', api_base='https://proxy.example/v1', remote=True)['conversation_key_route']
        self.assertEqual(route['state'], 'warning')
        self.assertIn('proxy.example', route['detail'])
        self.assertIn("Agent Zero's OpenRouter key", route['detail'])
        for quiet in ({'key_source': 'convo'}, {'token_env': 'CONVO_KEY'}, {'api_base': ''}, {'api_base': 'https://openrouter.ai/api/v1/'}):
            role = {'provider': 'openrouter', 'api_base': 'https://proxy.example/v1', **quiet}
            self.assertNotIn('conversation_key_route', self.rows(**role), quiet)
        self.assertNotIn('conversation_key_route', self.rows(provider='other', api_base='https://proxy.example/v1'))  # Convo's own key there


class ProbeParts(unittest.TestCase):
    def test_the_synthetic_clip_is_a_short_valid_completed_turn_and_always_the_same(self):
        clip = catalog._clip()
        self.assertEqual(validate_audio(clip), clip)
        with wave.open(io.BytesIO(base64.b64decode(clip))) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 16000))
            self.assertAlmostEqual(wav.getnframes() / wav.getframerate(), catalog.PROBE_SECONDS, places=2)
        self.assertEqual(catalog._clip(), clip)

    def test_an_answer_is_judged_by_status_and_shape_and_the_providers_own_text_is_never_repeated(self):
        leak = 'Incorrect API key provided: sk-abc***xyz'
        ok = catalog.verdict(200, {'choices': [{'message': {'content': 'OK'}}]}, 'OpenAI')
        self.assertEqual((ok['passed'], ok['code'], ok['reply']), (True, 'passed', 'OK'))
        long = catalog.verdict(200, {'choices': [{'message': {'content': [{'type': 'text', 'text': 'word ' * 40}]}}]}, 'OpenAI')
        self.assertLessEqual(len(long['reply']), 60)
        self.assertEqual(catalog.verdict(200, {'error': leak}, 'OpenAI')['code'], 'format')
        self.assertEqual(catalog.verdict(200, None, 'OpenAI')['code'], 'format')
        for status, words in ((401, 'key'), (403, 'key'), (404, 'model name'), (429, 'quota'), (400, 'audio input'), (422, 'audio input'), (500, 'error')):
            answer = catalog.verdict(status, {'error': {'message': leak}}, 'OpenAI')
            self.assertFalse(answer['passed'], status)
            self.assertIn(words, answer['message'], status)
            self.assertNotIn('sk-abc', json.dumps(answer), status)


if __name__ == '__main__':
    unittest.main()
