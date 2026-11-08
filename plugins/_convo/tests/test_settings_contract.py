"""The provider, classifier, decision-source and drafting settings: additive keys with fail-closed validation, whose defaults keep
the previous behaviour. Mirrors of DEFAULTS in webui/defaults.js and default_config.yaml are checked by test_runtime."""
import copy
import json
import unittest

from plugins._convo.helpers.convo_contract import DEFAULTS, ContractError, decider_endpoint, public_settings, validate_settings
from plugins._convo.helpers import speech_providers


def with_value(path, value):
    """A settings document that sets one dotted path."""
    document, node = {}, None
    parts = path.split('.')
    node = document
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return document


def at(config, path):
    for part in path.split('.'):
        config = config[part]
    return config


class NewSettings(unittest.TestCase):
    GOOD = {
        'voice_message_draft': ['single', 'continuous'],
        'roles.conversation.provider': ['', 'openai', 'openrouter', 'google', 'other', 'lm_studio', 'a0_venice'],
        'roles.conversation.key_source': ['provider', 'convo'],
        'roles.conversation.api_base': ['', 'http://host.docker.internal:8080/v1', 'http://192.168.1.20:8080/v1'],
        'roles.tts.provider': [''] + list(speech_providers.PROVIDERS),
        'roles.tts.connection': ['', 'external', 'managed'],
        'roles.tts.key_source': ['provider', 'convo'],
        'decider.source': ['', 'system_one', 'convo'],
        'decider.kind': ['d1_llama_cpp'],
        'decider.url': ['', 'http://host.docker.internal:9931', 'http://localhost:9931/', 'http://127.0.0.1:9931', 'http://[::1]:9931',
                        'https://host.docker.internal:9931', 'http://192.168.1.20:9931'],      # this machine and private networks
        'decider.model': ['', 'd1-omni-600M', 'x' * 2000],
        'roles.classifier.provider': ['', 'llama_cpp', 'jev', 'openrouter', 'other', 'a_plugin_provider'],
        'roles.classifier.key_source': ['provider', 'convo'],
        'roles.classifier.api_base': ['', 'http://host.docker.internal:9931/v1', 'http://192.168.1.20:8080/v1', 'http://localhost:7000/prefix/v1'],
        'roles.classifier.context_window': [1024, 8192, 1_000_000],
        'roles.classifier.kind': ['auto', 'decision', 'chat'],
    }
    BAD = {
        'voice_message_draft': ['send', 'draft', '', None, 1, ['single']],
        'roles.conversation.provider': ['Open AI', 'OpenAI', 'a.b', 'x' * 65, 5, None, ['openai']],
        'roles.conversation.key_source': ['host', '', None, 1, ['provider']],
        'roles.conversation.api_base': ['https://api.openai.com/v1', 'ftp://host.docker.internal/v1', 'http://user:pw@localhost:1/v1',
                                        'http://localhost:1/v1?x=1', 'x' * 2001, 5, None],
        'roles.tts.provider': ['openai', 'kokoro', 'strata', 'host_kokoro', 5, None],
        'roles.tts.connection': ['remote', 'local', 1, None, ['managed']],
        'roles.tts.key_source': ['host', '', None],
        'decider.source': ['d1', 'System One', None, 0],
        'decider.kind': ['', 'jev', 'llama_decision', None],
        'decider.url': ['https://example.com:9931', 'http://example.com:9931', 'http://host.docker.internal:9931/v1',
                        'http://u:p@localhost:9931', 'http://localhost:9931?x=1', 'http://localhost:9931#x', 'not a url', 5,
                        'http://localhost:abc', 'http://localhost:99999'],
        'decider.model': [5, None, 'x' * 2001],
        'roles.classifier.provider': ['Open AI', 'a.b', 'x' * 65, 5, None],
        'roles.classifier.key_source': ['host', '', None],
        'roles.classifier.api_base': ['https://api.typesafe.ai', 'http://u:p@localhost:1/v1', 'http://localhost:1/v1?x=1', 'x' * 2001, 5, None],
        'roles.classifier.context_window': [1023, 1_000_001, 8192.0, '8192', True, None],
        'roles.classifier.kind': ['', 'd1_llama_cpp', 'Decision', None, 1],
    }

    def test_every_new_setting_has_a_default_that_keeps_the_previous_behaviour(self):
        config = validate_settings({})
        self.assertEqual({path: at(config, path) for path in self.GOOD}, {
            'voice_message_draft': 'single', 'roles.conversation.provider': '', 'roles.conversation.key_source': 'provider',
            'roles.conversation.api_base': '', 'roles.tts.provider': '', 'roles.tts.connection': '', 'roles.tts.key_source': 'provider',
            'decider.source': '', 'decider.kind': 'd1_llama_cpp', 'decider.url': '', 'decider.model': '',
            'roles.classifier.provider': '', 'roles.classifier.key_source': 'provider', 'roles.classifier.api_base': '',
            'roles.classifier.context_window': 8192, 'roles.classifier.kind': 'auto'})
        self.assertNotIn('provider_saved', config['roles']['tts'])                            # a stash exists only once used
        for key in ('capabilities',):
            self.assertNotIn('provider', config['roles']['conversation'][key])                 # never inside capabilities{}
            self.assertNotIn('provider', config['roles']['tts'][key])
            self.assertFalse({'provider', 'key_source', 'api_base', 'context_window', 'kind'} & set(config['roles']['classifier'][key]))
        self.assertEqual(set(DEFAULTS['decider']), {'source', 'kind', 'url', 'model'})

    def test_good_values_validate_and_bad_values_are_refused(self):
        for path, values in self.GOOD.items():
            for value in values:
                with self.subTest(path=path, value=value):
                    self.assertEqual(at(validate_settings(with_value(path, value)), path), value)
        for path, values in self.BAD.items():
            for value in values:
                with self.subTest(path=path, value=value), self.assertRaises(ContractError):
                    validate_settings(with_value(path, value))

    def test_an_api_base_follows_the_endpoint_rules_including_remote_consent(self):
        remote = {'roles': {'conversation': {'api_base': 'https://api.openai.com/v1', 'remote': True}}}
        self.assertEqual(validate_settings(remote)['roles']['conversation']['api_base'], 'https://api.openai.com/v1')
        with self.assertRaises(ContractError):
            validate_settings({'roles': {'conversation': {'api_base': 'http://api.openai.com/v1', 'remote': True}}})   # remote needs HTTPS
        self.assertEqual(decider_endpoint('http://localhost:9931/'), 'http://localhost:9931')
        # the classifier: the same rule, its own consent (one role's consent never covers another's address)
        consent = {'roles': {'classifier': {'api_base': 'https://api.typesafe.ai', 'remote': True}, 'conversation': {'remote': False}}}
        self.assertEqual(validate_settings(consent)['roles']['classifier']['api_base'], 'https://api.typesafe.ai')
        self.assertEqual(decider_endpoint('https://api.typesafe.ai', True), 'https://api.typesafe.ai')
        for url, remote in (('https://api.typesafe.ai', False), ('http://api.typesafe.ai', True), ('https://api.typesafe.ai/v1', True)):
            with self.subTest(url=url, remote=remote), self.assertRaises(ContractError):
                decider_endpoint(url, remote)
        with self.assertRaises(ContractError):
            validate_settings({'decider': {'url': 'https://api.typesafe.ai'}, 'roles': {'conversation': {'remote': True}}})

    def test_the_decider_object_and_the_speech_stash_fail_closed(self):
        for value in ('convo', [], None, 5):
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_settings({'decider': value})
        with self.assertRaises(ContractError):
            validate_settings({'roles': {'tts': {'provider_saved': {'openai_tts': {'voice': 5}}}}})
        stash = {'audiocpp': {'connection': 'external', 'url': 'http://host.docker.internal:8890/v1'}}
        self.assertEqual(validate_settings({'roles': {'tts': {'provider_saved': stash}}})['roles']['tts']['provider_saved'], stash)

    def test_old_configurations_still_load_and_the_backend_stays_the_runtime_protocol(self):
        legacy = {'roles': {'conversation': {'url': 'http://127.0.0.1:8080/v1', 'model': 'm'}, 'tts': {'backend': 'managed_audio', 'saved': {'openai': {'voice': 'alloy'}}}}}
        config = validate_settings(copy.deepcopy(legacy))
        self.assertEqual((config['roles']['tts']['backend'], config['roles']['tts']['provider'], config['roles']['tts']['saved']), ('managed_audio', '', {'openai': {'voice': 'alloy'}}))
        self.assertEqual(config['roles']['conversation']['url'], 'http://127.0.0.1:8080/v1')
        # a provider never changes the stored backend by itself: deriving it is the speech catalog's job on save
        self.assertEqual(validate_settings({'roles': {'tts': {'provider': 'kokoro_native'}}})['roles']['tts']['backend'], 'openai')

    def test_the_browser_runtime_gets_the_drafting_mode_and_still_no_endpoint_or_key(self):
        settings = public_settings(validate_settings({'voice_message_draft': 'continuous', 'decider': {'source': 'convo', 'url': 'http://localhost:9931'},
                                                      'roles': {'conversation': {'provider': 'openai', 'api_base': 'http://localhost:1/v1'},
                                                                'classifier': {'provider': 'vllm', 'api_base': 'http://localhost:2/v1', 'token_env': 'SECRET_REF'}}}))
        self.assertEqual(settings['voice_message_draft'], 'continuous')
        text = json.dumps(settings)
        for leak in ('localhost:9931', 'localhost:1', 'localhost:2', 'SECRET_REF', 'api_base', 'decider', 'key_source', 'token_env', 'url'):
            self.assertNotIn(leak, text)


if __name__ == '__main__':
    unittest.main()
