"""Speech output: the backend control table, the provider catalog the editor offers, the normalization that derives the runtime
backend and `capabilities.instruction_control`, both stashes, and honest availability."""
import copy
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from plugins._convo.helpers import speech_providers as sp
from plugins._convo.helpers import setup_store
from plugins._convo.helpers.convo_contract import ContractError, merge, validate_settings
from plugins._convo.tests.fixtures.contract_v0_1 import convo_contract as old_contract

CATALOG_JS = Path(__file__).resolve().parents[1] / 'webui' / 'speech-catalog.js'
CASES = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'speech-normalize-cases.json').read_text(encoding='utf-8'))
MISSING = object()


def js_table(name):
    return json.loads(re.search(r'export const ' + name + r' = ([\[{].*?\n[\]}]);', CATALOG_JS.read_text(encoding='utf-8'), re.S).group(1))


def at(value, path):
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def tts(**fields):
    """roles.tts as a validated configuration holds it."""
    return validate_settings({'roles': {'tts': fields}})['roles']['tts']


def fake_config(kokoro=None, whisper=None, fail=False):
    module = types.ModuleType('plugins._convo.helpers.config')
    def effective():
        if fail:
            raise RuntimeError('host unavailable')
        return {'tts': {'kokoro': kokoro or {}}, 'stt': {'whisper': whisper or {}}}
    module.get_effective_config = effective
    return patch.dict(sys.modules, {'plugins._convo.helpers.config': module})


class ControlTable(unittest.TestCase):
    def test_every_backend_declares_known_controls_and_owns_only_its_fields(self):
        for name, spec in sp.BACKENDS.items():
            self.assertTrue(set(spec['controls']) <= set(sp.CONTROLS), name)
            self.assertTrue(spec['label'] and spec['voices'])
        self.assertEqual(sp.BACKENDS['host_kokoro']['controls'], ('native',))          # nothing about models, clones, tuning or PCM
        self.assertEqual(sp.BACKENDS['host_kokoro']['fields'], ())
        self.assertNotIn('url', sp.BACKENDS['managed_audio']['controls'])              # reached through the managed runtime, no address
        self.assertNotIn('key', sp.BACKENDS['managed_audio']['controls'])
        self.assertIn('tuning', sp.BACKENDS['managed_audio']['controls'])
        self.assertNotIn('tuning', sp.BACKENDS['openai']['controls'])
        self.assertEqual(set(sp.STASH_FIELDS), {'url', 'model', 'voice', 'token_env', 'remote', 'instructions', 'tuning_profile'})
        self.assertEqual(sp.STASHED_BACKENDS, ('openai', 'managed_audio'))

    def test_the_browser_fallback_table_is_identical_to_this_one(self):
        table = js_table('TTS_BACKENDS')
        for name, spec in sp.BACKENDS.items():
            self.assertEqual(table[name], {'label': spec['label'], 'controls': list(spec['controls']), 'fields': list(spec['fields'])}, name)
        self.assertEqual(set(table), set(sp.BACKENDS))
        stash = js_table('TTS_STASH_DEFAULTS')
        for name in sp.STASHED_BACKENDS:
            self.assertEqual(set(stash[name]), set(sp.BACKENDS[name]['fields']), name)
        defaults_js = (CATALOG_JS.parent / 'defaults.js').read_text(encoding='utf-8')
        self.assertIn("from './speech-catalog.js'", defaults_js)                       # still importable from defaults.js

    def test_the_browser_provider_catalog_and_groups_are_identical_to_these(self):
        table = js_table('SPEECH_PROVIDERS')
        self.assertEqual(list(table), list(sp.PROVIDERS))                              # the editor lists them in this order
        self.assertEqual(table, json.loads(json.dumps(sp.PROVIDERS)))
        self.assertEqual([group['id'] for group in js_table('SPEECH_GROUPS')], list(sp.GROUPS))
        self.assertTrue(all(group['label'] for group in js_table('SPEECH_GROUPS')))


class ProviderCatalog(unittest.TestCase):
    def test_the_catalog_names_the_planned_providers_and_each_resolves_to_a_runtime_backend(self):
        self.assertEqual(list(sp.PROVIDERS), ['kokoro_native', 'audiocpp', 'llamacpp', 'unsloth_studio', 'openai_tts', 'openai_compatible'])
        self.assertEqual({name: spec['group'] for name, spec in sp.PROVIDERS.items()},
                         {'kokoro_native': 'builtin', 'audiocpp': 'local', 'llamacpp': 'local', 'unsloth_studio': 'local', 'openai_tts': 'cloud', 'openai_compatible': 'other'})
        for name, spec in sp.PROVIDERS.items():
            self.assertIn(spec['group'], sp.GROUPS, name)
            for connection in spec['connections'] or ('',):
                self.assertIn(sp.backend_for(name, connection), sp.BACKENDS, name)
                self.assertIn(sp.per(spec['verify'], connection), ('native', 'managed', 'audiocpp', 'speech_route', 'models'), name)
        self.assertEqual((sp.backend_for('audiocpp', 'external'), sp.backend_for('audiocpp', 'managed'), sp.backend_for('audiocpp')), ('openai', 'managed_audio', 'openai'))
        self.assertEqual((sp.backend_for('kokoro_native'), sp.backend_for('openai_tts'), sp.backend_for('strata')), ('host_kokoro', 'openai', None))
        self.assertNotIn('strata', sp.PROVIDERS)                                          # Strata is not a speech provider
        self.assertEqual({name: spec['default_url'] for name, spec in sp.PROVIDERS.items()},
                         {'kokoro_native': '', 'audiocpp': 'http://host.docker.internal:8890/v1', 'llamacpp': 'http://host.docker.internal:8080/v1',
                          'unsloth_studio': '', 'openai_tts': 'https://api.openai.com/v1', 'openai_compatible': ''})   # Unsloth: no documented default

    def test_openai_offers_its_documented_models_with_each_models_own_voices(self):
        models = sp.PROVIDERS['openai_tts']['models']
        self.assertEqual(list(models), ['gpt-4o-mini-tts', 'tts-1', 'tts-1-hd'])
        self.assertEqual(set(models['tts-1']), {'alloy', 'ash', 'coral', 'echo', 'fable', 'onyx', 'nova', 'sage', 'shimmer'})
        self.assertEqual(models['tts-1'], models['tts-1-hd'])
        self.assertEqual(set(models['gpt-4o-mini-tts']) - set(models['tts-1']), {'ballad', 'verse', 'marin', 'cedar'})
        self.assertEqual(tuple(sp.PROVIDERS['openai_tts']['instructions']), ('gpt-4o-mini-tts',))   # "does not work with tts-1 or tts-1-hd"
        self.assertFalse(any(voice.startswith('clone:') for voices in models.values() for voice in voices))
        self.assertEqual([name for name, spec in sp.PROVIDERS.items() if spec['models']], ['openai_tts'])   # everyone else lists what the server has

    def test_each_provider_owns_its_backends_fields_and_where_they_apply_the_declaration_and_pcm(self):
        self.assertEqual(sp.connection_fields('kokoro_native', ''), ())
        self.assertEqual(sp.connection_fields('audiocpp', 'external'), ('url', 'model', 'voice', 'token_env', 'remote', 'instructions', 'accepts_instructions', 'native_pcm'))
        self.assertEqual(sp.connection_fields('audiocpp', 'managed'), ('model', 'voice', 'tuning_profile', 'instructions', 'native_pcm'))
        for name in ('llamacpp', 'unsloth_studio'):
            self.assertEqual(sp.connection_fields(name, ''), ('url', 'model', 'voice', 'token_env', 'remote', 'instructions', 'accepts_instructions'))
        self.assertEqual(sp.connection_fields('openai_tts', ''), ('url', 'model', 'voice', 'token_env', 'remote', 'instructions'))
        self.assertEqual(sp.connection_fields('openai_compatible', ''), ('url', 'model', 'voice', 'token_env', 'remote', 'instructions', 'accepts_instructions', 'native_pcm'))
        self.assertEqual([name for name in sp.PROVIDERS if sp.pcm_supported(name, 'external')], ['audiocpp', 'openai_compatible'])
        self.assertTrue(sp.pcm_supported('audiocpp', 'managed'))
        self.assertEqual(sp.STASHED_PROVIDERS, ('audiocpp', 'llamacpp', 'unsloth_studio', 'openai_tts', 'openai_compatible'))

    def test_an_older_configuration_means_a_provider_by_its_backend_never_by_its_url(self):
        self.assertEqual(sp.effective_provider({'backend': 'openai', 'url': 'http://host.docker.internal:8890/v1'}), ('openai_compatible', ''))
        self.assertEqual(sp.effective_provider({'backend': 'managed_audio'}), ('audiocpp', 'managed'))
        self.assertEqual(sp.effective_provider({'backend': 'host_kokoro'}), ('kokoro_native', ''))
        self.assertEqual(sp.effective_provider({}), ('openai_compatible', ''))
        self.assertEqual(sp.effective_provider({'provider': 'audiocpp', 'connection': 'managed', 'backend': 'openai'}), ('audiocpp', 'managed'))
        self.assertEqual(sp.effective_provider({'provider': 'audiocpp', 'connection': 'bogus'}), ('audiocpp', 'external'))
        self.assertEqual(sp.effective_provider({'provider': 'openai_tts', 'connection': 'managed'}), ('openai_tts', ''))

    def test_only_an_older_openai_configuration_on_the_known_local_port_is_offered_as_audiocpp(self):
        for url in ('http://127.0.0.1:8890/v1', 'http://localhost:8890/v1', 'http://host.docker.internal:8890/v1', 'http://[::1]:8890/v1'):
            self.assertEqual(sp.legacy_audiocpp_offer(tts(backend='openai', url=url)), url, url)
        for value in (tts(backend='openai', url='http://127.0.0.1:8881/v1'), tts(backend='openai', url='http://192.168.1.20:8890/v1'),
                      tts(backend='managed_audio', url='http://127.0.0.1:8890/v1'), tts(backend='host_kokoro', url='http://127.0.0.1:8890/v1'),
                      tts(provider='openai_compatible', backend='openai', url='http://127.0.0.1:8890/v1'),
                      tts(provider='audiocpp', connection='external', url='http://127.0.0.1:8890/v1')):
            self.assertEqual(sp.legacy_audiocpp_offer(value), '', value)
        self.assertEqual(sp.effective_provider(tts(backend='openai', url='http://127.0.0.1:8890/v1')), ('openai_compatible', ''))   # an offer, never a guess

    def test_only_the_openai_cloud_provider_uses_an_agent_zero_key(self):
        self.assertEqual({name: sp.host_key({'provider': name}) for name in sp.PROVIDERS if sp.host_key({'provider': name})}, {'openai_tts': 'openai'})
        self.assertEqual((sp.host_key({}), sp.host_key({'provider': 'unknown'}), sp.host_key(None)), ('', '', ''))


class DeliveryInstructions(unittest.TestCase):
    def supported(self, **fields):
        role = tts(**fields)
        return sp.instructions_supported(role, *sp.effective_provider(role))

    def test_a_base_model_or_a_clone_voice_never_takes_delivery_instructions(self):
        for model in ('qwen3-tts-1.7b-base-bf16', 'Qwen3-TTS-12Hz-1.7B-Base', 'base-f16', 'base', 'my.base.model'):
            self.assertTrue(sp.is_base_clone(model, ''), model)
        self.assertTrue(sp.is_base_clone('', 'clone:2098c329bc9d'))
        for model in ('qwen3-tts-1.7b-customvoice', 'database-tts', 'baseline', 'kokoro', ''):
            self.assertFalse(sp.is_base_clone(model, 'af_bella'), model)

    def test_instructions_are_supported_exactly_where_the_provider_and_model_honour_them(self):
        matrix = [
            (dict(provider='openai_tts', model='gpt-4o-mini-tts', voice='coral'), True),
            (dict(provider='openai_tts', model='gpt-4o-mini-tts-2025-12-15', voice='coral'), True),
            (dict(provider='openai_tts', model='tts-1', voice='alloy'), False),
            (dict(provider='openai_tts', model='tts-1-hd', voice='alloy', accepts_instructions=True), False),   # a declaration cannot overrule OpenAI
            (dict(provider='openai_tts', model='gpt-4o-mini-tts-x', voice='coral'), True),
            (dict(provider='openai_tts', model='gpt-4o-mini-ttsx', voice='coral'), False),
            (dict(provider='audiocpp', connection='external', model='qwen3-tts-1.7b-customvoice', voice='vivian', accepts_instructions=True), True),
            (dict(provider='audiocpp', connection='external', model='qwen3-tts-1.7b-customvoice', voice='vivian'), False),
            (dict(provider='audiocpp', connection='external', model='qwen3-tts-1.7b-base-bf16', voice='vivian', accepts_instructions=True), False),
            (dict(provider='audiocpp', connection='external', model='qwen3-tts-1.7b-customvoice', voice='clone:2098c329bc9d', accepts_instructions=True), False),
            (dict(provider='audiocpp', connection='managed', model='custom', voice='vivian', accepts_instructions=True), False),   # never managed
            (dict(provider='kokoro_native', accepts_instructions=True), False),                                                  # never Kokoro
            (dict(provider='llamacpp', model='orpheus', voice='tara', accepts_instructions=True), True),
            (dict(provider='unsloth_studio', model='orpheus', voice='tara', accepts_instructions=True), True),
            (dict(provider='unsloth_studio', model='orpheus', voice='tara'), False),
            (dict(provider='openai_compatible', model='kokoro-82m', voice='af_bella', accepts_instructions=True), True),
            (dict(provider='openai_compatible', model='kokoro-82m', voice='af_bella', capabilities={'instruction_control': True}), False),
            (dict(backend='openai', model='kokoro-82m', voice='af_bella', capabilities={'instruction_control': True}), True),   # an older config's flag
            (dict(backend='managed_audio', model='custom', voice='vivian', capabilities={'instruction_control': True}), False),
            (dict(backend='host_kokoro', capabilities={'instruction_control': True}), False),
        ]
        for fields, expected in matrix:
            self.assertEqual(self.supported(**fields), expected, fields)


class Normalization(unittest.TestCase):
    def test_the_shared_cases_derive_the_same_fields_the_browser_derives(self):
        self.assertEqual(CASES['paths'], ['provider', 'connection', 'backend', 'url', 'instructions', 'capabilities.instruction_control', 'capabilities.native_pcm', 'provider_saved'])
        for case in CASES['cases']:
            with self.subTest(case=case['name']):
                config = validate_settings({'roles': {'tts': copy.deepcopy(case['input'])}})
                normalized = sp.normalize(config)
                for path, expected in case['expect'].items():
                    value = at(normalized['roles']['tts'], path)
                    self.assertEqual(None if value is MISSING else value, expected, path)
                    if expected is None:
                        self.assertIs(value, MISSING, path + ' is absent, not null')
                self.assertEqual(validate_settings(normalized), normalized)                 # what is stored still validates as it is
                self.assertEqual(sp.normalize(normalized), normalized)                      # and normalizing twice changes nothing
                self.assertEqual(config['roles']['tts'], validate_settings({'roles': {'tts': case['input']}})['roles']['tts'])   # the input is untouched

    def test_only_the_speech_role_is_touched(self):
        config = validate_settings({'style': 'kept', 'roles': {'tts': {'provider': 'llamacpp', 'url': ''}, 'conversation': {'model': 'm'}}})
        normalized = sp.normalize(config)
        self.assertIsNot(normalized, config)
        self.assertEqual({key: value for key, value in normalized.items() if key != 'roles'}, {key: value for key, value in config.items() if key != 'roles'})
        self.assertEqual((normalized['roles']['conversation'], normalized['roles']['classifier']), (config['roles']['conversation'], config['roles']['classifier']))
        self.assertEqual(config['roles']['tts']['url'], '')

    def test_a_declaration_must_be_true_or_false(self):
        for value in ('yes', 1, None, ['true']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sp.normalize(validate_settings({'roles': {'tts': {'provider': 'openai_compatible', 'accepts_instructions': value}}}))

    def test_every_settings_writer_stores_the_derived_backend_and_the_previous_version_reads_it(self):
        stored = setup_store.normalize({'roles': {'tts': {'provider': 'audiocpp', 'connection': 'managed', 'backend': 'openai', 'model': 'base-f16'}}})
        self.assertEqual((stored['roles']['tts']['backend'], stored['roles']['tts']['connection']), ('managed_audio', 'managed'))
        for provider, backend in (('kokoro_native', 'host_kokoro'), ('openai_tts', 'openai'), ('llamacpp', 'openai')):
            fields = {'provider': provider, 'url': '', 'remote': provider == 'openai_tts'}
            stored = setup_store.normalize({'roles': {'tts': fields}})
            self.assertEqual(stored['roles']['tts']['backend'], backend, provider)
            loaded = old_contract.validate_settings(copy.deepcopy(stored))                # rollback: the old contract loads it and reads the protocol
            self.assertEqual(loaded['roles']['tts']['backend'], backend, provider)
        self.assertEqual(stored['roles']['tts']['url'], 'http://host.docker.internal:8080/v1')
        with self.assertRaises(ContractError):
            setup_store.normalize({'roles': {'tts': {'provider': 'openai_tts', 'url': ''}}})   # OpenAI is remote: consent is required to store it


class ProviderStash(unittest.TestCase):
    def test_a_provider_stash_holds_only_each_catalog_providers_own_bounded_fields(self):
        good = {'audiocpp': {'connection': 'managed', 'url': 'http://host.docker.internal:8890/v1', 'voice': 'clone:jarvis', 'tuning_profile': 'calm', 'key_source': 'convo'},
                'openai_tts': {'model': 'gpt-4o-mini-tts', 'voice': 'alloy', 'remote': True, 'instructions': 'warm', 'key_source': 'provider'}}
        self.assertEqual(sp.validate_provider_saved(good), good)                          # the earlier flat audio.cpp record is still read
        nested = {'audiocpp': {'connection': 'external', 'key_source': 'provider',
                               'external': {'url': 'http://127.0.0.1:8890/v1', 'model': 'qwen3-tts-1.7b-base-bf16', 'voice': 'clone:2098c329bc9d', 'token_env': '',
                                            'remote': False, 'instructions': '', 'accepts_instructions': False, 'native_pcm': True},
                               'managed': {'model': 'base-f16', 'voice': 'clone:one', 'tuning_profile': 'quality', 'instructions': '', 'native_pcm': False}},
                  'llamacpp': {'url': 'http://host.docker.internal:8080/v1', 'accepts_instructions': True, 'key_source': 'provider'},
                  'openai_compatible': {'native_pcm': True, 'accepts_instructions': False}}
        self.assertEqual(sp.validate_provider_saved(nested), nested)
        self.assertEqual((sp.validate_provider_saved(None), sp.validate_provider_saved({})), ({}, {}))
        self.assertNotIn('kokoro_native', sp.STASHED_PROVIDERS)
        bad = [{'kokoro_native': {}}, {'openai': {}}, {'strata': {}}, 'text', {'audiocpp': 'text'}, {'llamacpp': {'tuning_profile': 'x'}},
               {'llamacpp': {'connection': 'external'}}, {'audiocpp': {'connection': 'remote'}}, {'audiocpp': {'connection': ['managed']}},
               {'openai_tts': {'key_source': 'host'}}, {'openai_tts': {'remote': 'yes'}}, {'openai_tts': {'voice': 5}}, {'openai_tts': {'voice': 'x' * 2001}},
               {'openai_tts': {'accepts_instructions': True}}, {'openai_tts': {'native_pcm': True}}, {'llamacpp': {'native_pcm': True}},
               {'openai_compatible': {'accepts_instructions': 'yes'}}, {'audiocpp': {'external': 'text'}}, {'audiocpp': {'external': {'tuning_profile': 'x'}}},
               {'audiocpp': {'managed': {'url': 'http://x'}}}, {'audiocpp': {'managed': {'accepts_instructions': True}}}, {'audiocpp': {'remote': {}}},
               {'audiocpp': {'external': {'native_pcm': 'on'}}}, {'llamacpp': {'external': {}}}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                sp.validate_provider_saved(value)
        config = validate_settings({'roles': {'tts': {'provider_saved': nested}}})
        self.assertEqual(config['roles']['tts']['provider_saved'], nested)
        with self.assertRaises(ContractError):
            validate_settings({'roles': {'tts': {'provider_saved': {'audiocpp': {'surprise': 'x'}}}}})


class Readiness(unittest.TestCase):
    def rows(self, sidecar=None, **fields):
        config = validate_settings({'roles': {'tts': fields}, 'sidecar': sidecar or {}})
        return {row['id']: row for row in sp.readiness_rows(config)}

    def test_the_speech_provider_row_says_what_is_chosen_and_what_is_still_missing(self):
        rows = self.rows(provider='audiocpp', connection='managed')
        self.assertEqual(rows['speech_provider']['state'], 'missing')
        self.assertIn('Manage runtime', rows['speech_provider']['detail'])                   # managed shows its reason while the runtime is off
        self.assertEqual(self.rows(sidecar={'managed': True}, provider='audiocpp', connection='managed')['speech_provider']['state'], 'ready')
        self.assertIn('Kokoro', self.rows(provider='kokoro_native')['speech_provider']['detail'])
        unsloth = self.rows(provider='unsloth_studio', url='')['speech_provider']
        self.assertEqual(unsloth['state'], 'missing')
        self.assertIn('server URL', unsloth['detail'])
        llama = self.rows(provider='llamacpp', url='http://host.docker.internal:8080/v1')['speech_provider']
        self.assertEqual(llama['state'], 'ready')
        self.assertIn('no documented OpenAI-style speech route', llama['detail'])
        cloud = self.rows(provider='openai_tts', url='https://api.openai.com/v1', model='tts-1', voice='alloy', remote=True)['speech_provider']
        self.assertEqual(cloud['state'], 'ready')
        clone = self.rows(provider='openai_tts', url='https://api.openai.com/v1', model='tts-1', voice='clone:2098c329bc9d', remote=True)['speech_provider']
        self.assertEqual(clone['state'], 'missing')
        self.assertIn('clone:2098c329bc9d', clone['detail'])                                 # a clone voice is not an OpenAI voice
        mini = self.rows(provider='openai_tts', url='https://api.openai.com/v1', model='tts-1', voice='marin', remote=True)['speech_provider']
        self.assertIn('marin', mini['detail'])                                               # each model has its own voices
        self.assertEqual(mini['state'], 'missing')

    def test_an_older_configuration_is_described_as_derived_and_the_known_port_is_only_suggested(self):
        rows = self.rows(backend='openai', url='http://127.0.0.1:8890/v1', model='qwen3-tts-1.7b-base-bf16', voice='clone:2098c329bc9d')
        self.assertIn('Derived from the saved speech backend', rows['speech_provider']['detail'])
        self.assertEqual(rows['speech_provider_offer']['state'], 'unverified')
        self.assertIn('audio.cpp', rows['speech_provider_offer']['detail'])
        self.assertNotIn('speech_provider_offer', self.rows(backend='openai', url='http://127.0.0.1:8881/v1'))

    def test_delivery_instructions_that_are_kept_but_not_sent_are_said_to_be(self):
        rows = self.rows(provider='openai_tts', url='https://api.openai.com/v1', model='tts-1', voice='alloy', remote=True, provider_saved={'openai_tts': {'instructions': 'warm'}})
        self.assertEqual(rows['speech_instructions']['state'], 'idle')
        self.assertIn('gpt-4o-mini-tts', rows['speech_instructions']['detail'])
        self.assertNotIn('speech_instructions', self.rows(provider='openai_tts', url='https://api.openai.com/v1', model='tts-1', voice='alloy', remote=True))

    def test_rows_are_pure_reads(self):
        config = validate_settings({'roles': {'tts': {'provider': 'llamacpp'}}})
        frozen = copy.deepcopy(config)
        with patch('socket.socket', side_effect=AssertionError('no network')), patch('builtins.open', side_effect=AssertionError('no file access')):
            sp.readiness_rows(config)
        self.assertEqual(config, frozen)


class Descriptors(unittest.TestCase):
    def describe(self, convo=None, toggles=None, **config):
        toggles = toggles or {}
        with patch.object(sp, '_toggle', side_effect=lambda name: toggles.get(name, 'disabled')), fake_config(**config):
            return sp.describe(convo or {'sidecar': {'managed': False}})

    def test_availability_is_reported_honestly_and_never_blocks_the_read(self):
        found = {item['id']: item for item in self.describe(toggles={'_kokoro_tts': 'enabled'})['providers']}
        self.assertEqual([found[k]['available'] for k in ('openai', 'managed_audio', 'host_kokoro')], [True, False, True])
        self.assertIn('Manage runtime', found['managed_audio']['reason'])
        off = {item['id']: item for item in self.describe()['providers']}
        self.assertFalse(off['host_kokoro']['available'])
        self.assertIn('Native Kokoro is off', off['host_kokoro']['reason'])
        on = {item['id']: item for item in self.describe({'sidecar': {'managed': True}})['providers']}
        self.assertTrue(on['managed_audio']['available'])
        for item in found.values():
            self.assertEqual(item['kind'], 'tts')
            self.assertEqual(item['source'], 'adapter')
        self.assertEqual(found['host_kokoro']['settings_owner'], 'native')
        self.assertEqual(found['openai']['settings_owner'], 'convo')

    def test_native_summaries_hold_choices_and_never_credentials(self):
        kokoro = {'voice_weights': {'af_bella': 6.0, 'am_adam': 4.0}, 'voice': 'af_bella,am_adam', 'speed': 1.1, 'device': 'remote',
                  'remote_enabled': True, 'remote_url': 'http://worker', 'remote_token': 'TOKEN-123', 'remote_timeout': 20}
        result = self.describe(toggles={'_kokoro_tts': 'enabled', '_whisper_stt': 'enabled'}, kokoro=kokoro,
                               whisper={'model_size': 'base', 'device': 'auto', 'language': 'en', 'message_mode': 'send', 'api_token': 'SECRET'})
        native = result['native']
        self.assertEqual(native['kokoro']['summary'], {'voices': [{'id': 'af_bella', 'share': .6}, {'id': 'am_adam', 'share': .4}], 'speed': 1.1, 'device': 'remote', 'remote': True})
        self.assertEqual(native['whisper']['summary'], {'model_size': 'base', 'device': 'auto', 'language': 'en', 'message_mode': 'send'})
        self.assertNotIn('TOKEN-123', json.dumps(result))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertTrue(native['kokoro']['enabled'] and native['whisper']['enabled'])

    def test_a_single_voice_and_an_unreadable_host_degrade_quietly(self):
        single = self.describe(kokoro={'voice': 'bm_lewis', 'speed': 1.0, 'device': 'auto'})
        self.assertEqual(single['native']['kokoro']['summary']['voices'], [{'id': 'bm_lewis', 'share': 1.0}])       # a custom voice is shown as it is
        broken = self.describe(fail=True)
        self.assertEqual((broken['native']['kokoro']['summary'], broken['native']['whisper']['summary']), (None, None))
        self.assertEqual(len(broken['providers']), 3)                              # settings stay readable

    def test_describing_changes_nothing_and_calls_nothing_else(self):
        with patch.object(sp, '_toggle', return_value='enabled') as toggle, fake_config(kokoro={'voice': 'af_heart'}), \
             patch('subprocess.Popen', side_effect=AssertionError('no process')), patch('builtins.open', side_effect=AssertionError('no file access of its own')):
            sp.describe({'sidecar': {'managed': True}})
        self.assertEqual(sorted(call.args[0] for call in toggle.call_args_list), ['_kokoro_tts', '_whisper_stt'])


class SavedStash(unittest.TestCase):
    def test_a_stash_holds_only_each_backends_own_bounded_fields(self):
        good = {'openai': {'url': 'http://api/v1', 'model': 'tts-1', 'voice': 'alloy', 'token_env': '', 'remote': False, 'instructions': ''},
                'managed_audio': {'model': 'qwen', 'voice': 'clone:abc', 'tuning_profile': 'calm', 'instructions': 'softly'}}
        self.assertEqual(sp.validate_saved(good), good)
        self.assertEqual(sp.validate_saved(None), {})
        self.assertEqual(sp.validate_saved({}), {})
        bad = [{'host_kokoro': {}}, {'unknown': {}}, 'text', {'openai': 'text'}, {'openai': {'capabilities': {}}}, {'managed_audio': {'url': 'http://x'}},
               {'openai': {'voice': 5}}, {'openai': {'voice': 'x' * 2001}}, {'openai': {'remote': 'yes'}}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                sp.validate_saved(value)

    def test_the_stash_survives_validation_and_a_merge_and_old_configs_still_load(self):
        saved = {'openai': {'url': 'http://api/v1', 'voice': 'alloy'}}
        config = validate_settings({'roles': {'tts': {'backend': 'managed_audio', 'model': 'qwen', 'saved': saved}}})
        self.assertEqual(config['roles']['tts']['saved'], saved)
        self.assertEqual(merge(config, {'roles': {'tts': {'voice': 'clone:x'}}})['roles']['tts']['saved'], saved)
        legacy = validate_settings({'roles': {'tts': {'backend': 'host_kokoro'}}})                    # a config written before the stash existed
        self.assertNotIn('saved', legacy['roles']['tts'])
        with self.assertRaises(ContractError):
            validate_settings({'roles': {'tts': {'saved': {'openai': {'surprise': 'x'}}}}})
        # the capability record is untouched: unknown capability keys still fail closed
        with self.assertRaises(ContractError):
            validate_settings({'roles': {'tts': {'capabilities': {'saved': True}}}})
        normalized = sp.normalize(config)
        self.assertEqual(normalized['roles']['tts']['saved'], saved)                                  # normalization never writes the older stash


if __name__ == '__main__':
    unittest.main()
