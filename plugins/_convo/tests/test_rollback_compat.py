"""Rollback compatibility: settings written by this version still load, save and run in the versions before it.

tests/fixtures/contract_v0_1 holds frozen copies of that version's convo_contract.py, convo_policy.py and speech_providers.py;
tests/fixtures/contract_v0_2 the same files of the version before the Classifier card (2026-10-19), plus its
convo_model_catalog.py (the write chain's conversation normalizer). Both are verbatim except one line: the old contract's lazy
`from .convo_system_one import readiness` imports the live module instead (it runs only when System One is selected). Never
edit the frozen copies; the hash checks say if anyone did. When a later version changes the settings format again, freeze this
version beside them rather than updating these files."""
import copy
import hashlib
import json
import unittest
from pathlib import Path

from plugins._convo.helpers import convo_contract as current
from plugins._convo.helpers import convo_classifier_catalog as catalog
from plugins._convo.helpers import setup_store
from plugins._convo.tests.fixtures.contract_v0_1 import convo_contract as old
from plugins._convo.tests.fixtures.contract_v0_1 import speech_providers as old_speech
from plugins._convo.tests.fixtures.contract_v0_2 import convo_contract as v2
from plugins._convo.tests.fixtures.contract_v0_2 import convo_model_catalog as v2_catalog
from plugins._convo.tests.fixtures.contract_v0_2 import speech_providers as v2_speech

FROZEN = Path(__file__).resolve().parent / 'fixtures' / 'contract_v0_1'
# sha256 with line endings normalized to \n (git may check files out either way)
HASHES = {'convo_contract.py': '4c8c84c4acab1688e74c38f4a55f2f8b66309b4ad87b3f1cb3ae63ffa7dc3028',
          'convo_policy.py': 'd593fc9d2356af939409817440b7568002f0ad04168be61a255f4e78645dd0ba',
          'speech_providers.py': '47e4f2b4ca7303106de89a6da6aef8699b5d9d233a574deff0dce0a754a3bb58'}
NEW_KEYS = ('voice_message_draft', 'decider', 'roles.conversation.provider', 'roles.conversation.key_source', 'roles.conversation.api_base',
            'roles.tts.provider', 'roles.tts.connection', 'roles.tts.key_source', 'roles.tts.provider_saved')


def at(config, path):
    for part in path.split('.'):
        config = config[part]
    return config


def everything_set():
    """A configuration this version can write, with every new setting away from its default."""
    return current.validate_settings({
        'voice_message_draft': 'continuous',
        'decider': {'source': 'convo', 'kind': 'd1_llama_cpp', 'url': 'http://host.docker.internal:9931', 'model': 'd1-omni-600M'},
        'roles': {
            'conversation': {'provider': 'openrouter', 'key_source': 'convo', 'api_base': 'https://openrouter.ai/api/v1', 'remote': True,
                             'url': 'https://openrouter.ai/api/v1', 'model': 'google/gemini-2.5-flash'},
            'tts': {'provider': 'audiocpp', 'connection': 'managed', 'key_source': 'convo', 'backend': 'managed_audio', 'voice': 'clone:jarvis',
                    'saved': {'openai': {'voice': 'alloy'}},
                    'provider_saved': {'audiocpp': {'connection': 'external', 'url': 'http://host.docker.internal:8890/v1', 'voice': 'clone:jarvis'},
                                       'openai_tts': {'model': 'gpt-4o-mini-tts', 'voice': 'alloy', 'remote': True, 'key_source': 'provider'}}}}})


class RollbackCompatibility(unittest.TestCase):
    def test_the_frozen_copies_are_unchanged(self):
        for name, digest in HASHES.items():
            self.assertEqual(hashlib.sha256((FROZEN / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest(), digest, name)

    def test_new_configurations_validate_in_the_old_contract_with_every_new_key_kept(self):
        for config in (current.validate_settings({}), copy.deepcopy(current.DEFAULTS), everything_set()):
            loaded = old.validate_settings(copy.deepcopy(config))
            for path in NEW_KEYS:
                if path.endswith('provider_saved') and 'provider_saved' not in config['roles']['tts']:
                    continue
                self.assertEqual(at(loaded, path), at(config, path), path)
            self.assertEqual(loaded['roles']['tts']['backend'], config['roles']['tts']['backend'])     # the runtime protocol old code reads
            self.assertEqual(loaded['roles']['conversation']['url'], config['roles']['conversation']['url'])

    def test_an_old_editor_saving_a_new_configuration_keeps_every_new_key(self):
        stored = everything_set()
        browser = old.validate_settings(copy.deepcopy(stored))                       # what the old interface loads and edits
        browser['style'] = 'edited after a rollback'
        saved = old.validate_settings(old.merge(stored, browser))                    # the old shared save path
        for path in NEW_KEYS:
            self.assertEqual(at(saved, path), at(stored, path), path)
        self.assertEqual(saved['style'], 'edited after a rollback')
        again = current.validate_settings(saved)                                     # and upgrading again reads it unchanged
        for path in NEW_KEYS:
            self.assertEqual(at(again, path), at(stored, path), path)

    def test_the_old_runtime_reads_what_it_knows_from_a_new_configuration(self):
        config = old.validate_settings(everything_set())
        settings = old.public_settings(config)
        self.assertNotIn('voice_message_draft', settings)                            # old browsers simply never see it
        self.assertFalse(settings['text_input_required'])
        self.assertTrue(settings['remote_processing'])
        self.assertEqual(old.input_modality_conflict(config), '')
        self.assertEqual(old.endpoint(config['roles']['conversation']), 'https://openrouter.ai/api/v1')
        self.assertEqual(old_speech.validate_saved(config['roles']['tts']['saved']), {'openai': {'voice': 'alloy'}})
        self.assertNotIn('provider_saved', json.dumps(old_speech.describe(config)))  # the old descriptors know nothing of providers

    def test_a_configuration_the_old_version_wrote_loads_here_unchanged(self):
        written = old.validate_settings({'roles': {'tts': {'backend': 'host_kokoro'}, 'conversation': {'url': 'http://127.0.0.1:8080/v1'}}, 'style': 's'})
        config = current.validate_settings(copy.deepcopy(written))
        for key, value in written.items():
            if key != 'roles':
                self.assertEqual(config[key], value, key)
        self.assertEqual((config['roles']['tts']['backend'], config['roles']['tts']['provider'], config['roles']['conversation']['provider']), ('host_kokoro', '', ''))


FROZEN_V2 = Path(__file__).resolve().parent / 'fixtures' / 'contract_v0_2'
HASHES_V2 = {'convo_contract.py': '3e3cd46635174b2dda32a2c3c814eb24d1a897b7a331d7c8a617c271653a228c',
             'convo_model_catalog.py': '5e79fabb4799bda126533a88dc54440598b23f59f7f4132f24883582b24b2d03',
             'convo_policy.py': 'd593fc9d2356af939409817440b7568002f0ad04168be61a255f4e78645dd0ba',
             'speech_providers.py': '23a226172873359d87fbd721f717009ee927fa6196bd42abcb6d908e7ecc549a'}
CLASSIFIER_KEYS = ('roles.classifier.provider', 'roles.classifier.key_source', 'roles.classifier.api_base', 'roles.classifier.context_window',
                   'roles.classifier.kind')
RUNTIME_KEYS = ('system_one', 'decider', 'roles.classifier.url', 'roles.classifier.remote')       # what the old runtime reads
TEXT = {'conversation': {'capabilities': {'audio_input': False, 'cancellation': True, 'tool_calls': True}}}


def classifier_settings():
    """What this version writes (through its whole normalizer chain) for every kind of Classifier card choice."""
    legacy = {'system_one': {'enabled': True, 'backend': 'd1_llama_cpp'}, 'decider': {'source': 'convo', 'url': 'http://host.docker.internal:9931', 'model': 'd1'}}
    cases = {
        'defaults': {},
        'toggle on': {'decider': {'source': 'system_one'}},
        'llama.cpp decision': {'roles': {'classifier': {'provider': 'llama_cpp', 'kind': 'decision', 'model': 'd1-omni-600M', 'context_window': 16384}}},
        'LAN decision': {'roles': {'classifier': {'provider': 'vllm', 'kind': 'decision', 'api_base': 'http://192.168.1.5:8000/v1'}}},
        'decision under a path': {'roles': {'classifier': {'provider': 'other', 'kind': 'decision', 'api_base': 'http://localhost:7000/prefix/v1'}}},
        'Jev with consent': {'roles': {'classifier': {'provider': 'jev', 'remote': True, 'key_source': 'convo'}, **TEXT}},
        'OpenRouter chat': {'roles': {'classifier': {'provider': 'openrouter', 'remote': True, 'model': 'google/gemini-2.5-flash'}}},
        'local chat': {'roles': {'classifier': {'provider': 'llama_cpp', 'kind': 'chat', 'api_base': 'http://host.docker.internal:8081/v1'}}},
        'disabled provider': {'roles': {'classifier': {'provider': 'anthropic'}}},
        'plugin provider': {'roles': {'classifier': {'provider': 'a_plugin_provider', 'api_base': 'http://192.168.1.7:4000/v1'}}},
        'no address': {'roles': {'classifier': {'provider': 'unsloth_studio'}}},
        'legacy separate decider': legacy,
        'its migration view, saved': {**legacy, 'roles': {'classifier': catalog.migration_view(current.validate_settings(legacy))}},
    }
    return {name: setup_store.normalize(copy.deepcopy(case)) for name, case in cases.items()}


def v2_save(value):
    """The previous version's shared write chain (setup_store.normalize then): validate, conversation catalog, speech catalog."""
    return v2.validate_settings(v2_speech.normalize(v2_catalog.normalize(v2.validate_settings(value))))


class ClassifierRollback(unittest.TestCase):
    def test_the_frozen_copies_are_unchanged(self):
        for name, digest in HASHES_V2.items():
            self.assertEqual(hashlib.sha256((FROZEN_V2 / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest(), digest, name)

    def test_every_classifier_choice_loads_in_both_previous_versions_with_every_key_kept(self):
        for name, config in classifier_settings().items():
            for version in (v2, old):
                with self.subTest(case=name, version=version.__name__):
                    loaded = version.validate_settings(copy.deepcopy(config))
                    for path in CLASSIFIER_KEYS + RUNTIME_KEYS:
                        self.assertEqual(at(loaded, path), at(config, path), path)

    def test_decider_url_is_only_ever_written_in_the_form_the_previous_version_accepts(self):
        for name, config in classifier_settings().items():
            with self.subTest(case=name):
                if config['decider']['url']:
                    self.assertEqual(v2.decider_endpoint(config['decider']['url']), config['decider']['url'])
        settings = classifier_settings()
        self.assertEqual(settings['llama.cpp decision']['decider']['url'], 'http://host.docker.internal:8080')    # mirrored
        for name in ('LAN decision', 'decision under a path', 'Jev with consent'):
            self.assertEqual(settings[name]['decider']['url'], '', name)          # derived at call time; the old plugin uses its default

    def test_an_old_editor_saving_a_new_configuration_keeps_every_new_key_and_the_runtime(self):
        for name, stored in classifier_settings().items():
            with self.subTest(case=name):
                browser = v2.validate_settings(copy.deepcopy(stored))
                browser['style'] = 'edited after a rollback'
                saved = v2_save(v2.merge(stored, browser))
                for path in CLASSIFIER_KEYS + RUNTIME_KEYS:
                    self.assertEqual(at(saved, path), at(stored, path), path)
                again = setup_store.normalize(copy.deepcopy(saved))                   # and upgrading again derives the same runtime
                for path in CLASSIFIER_KEYS + RUNTIME_KEYS:
                    self.assertEqual(at(again, path), at(stored, path), path)

    def test_a_configuration_the_previous_version_wrote_keeps_its_runtime_here(self):
        for written in (v2_save({'system_one': {'enabled': True, 'backend': 'd1_llama_cpp'}, 'decider': {'source': 'convo', 'url': 'http://localhost:9940'}}),
                        v2_save({'roles': {'classifier': {'url': 'http://127.0.0.1:8081/v1', 'model': 'policy'}}}),
                        v2_save({'system_one': {'enabled': True, 'backend': 'd1_llama_cpp'}, 'decider': {'source': 'system_one'}})):
            normalized = setup_store.normalize(copy.deepcopy(written))
            for path in RUNTIME_KEYS:
                self.assertEqual(at(normalized, path), at(written, path), path)
            self.assertEqual(normalized['roles']['classifier']['provider'], '')


if __name__ == '__main__':
    unittest.main()
