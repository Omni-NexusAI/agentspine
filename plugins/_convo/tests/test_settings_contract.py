"""The provider, classifier, decision-source and drafting settings: additive keys with fail-closed validation, whose defaults keep
the previous behaviour. Mirrors of DEFAULTS in webui/defaults.js and default_config.yaml are checked by test_runtime."""
import copy
import json
import re
import unittest
from pathlib import Path

from plugins._convo.helpers.convo_contract import DEFAULTS, HOTKEY_RESERVED, ContractError, decider_endpoint, public_settings, validate_settings
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



class Hotkeys(unittest.TestCase):
    """The configurable hotkeys (`hotkeys`: one combination, or nothing, per action): additive, validated as a whole, and told to the browser.
    tests/fixtures/hotkey_cases.json is what webui/hotkeys.js is held to as well (tests/hotkeys.test.mjs)."""
    ACTIONS = ('toggle_conversation', 'mute', 'push_to_talk', 'dictate', 'stop_reply', 'open_panel', 'open_settings', 'run_action', 'dismiss_action')
    CASES = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'hotkey_cases.json').read_text(encoding='utf-8'))

    def test_the_defaults_are_few_usable_and_different_from_each_other_and_from_the_older_shortcut(self):
        config = validate_settings({})
        self.assertEqual(tuple(DEFAULTS['hotkeys']), self.ACTIONS)
        self.assertEqual(config['hotkeys'], DEFAULTS['hotkeys'])
        self.assertIsNot(config['hotkeys'], DEFAULTS['hotkeys'])
        used = [value for value in config['hotkeys'].values() if value]
        self.assertEqual(len(used), 5)
        self.assertEqual(len(set(used + [config['hotkey']])), 6)

    def test_an_older_configuration_gets_the_defaults_and_one_action_saved_keeps_the_others(self):
        self.assertEqual(validate_settings({'enabled': True, 'hotkey': 'Ctrl+Alt+K'})['hotkeys'], DEFAULTS['hotkeys'])
        config = validate_settings({'hotkeys': {'open_panel': 'Alt+O', 'mute': ''}})
        self.assertEqual(config['hotkeys'], {**DEFAULTS['hotkeys'], 'open_panel': 'Alt+O', 'mute': ''})
        self.assertEqual(validate_settings(config)['hotkeys'], config['hotkeys'])                      # saved and loaded unchanged
        self.assertEqual(set(validate_settings({'hotkeys': {action: '' for action in self.ACTIONS}})['hotkeys'].values()), {''})

    def test_what_a_combination_may_be_is_the_browsers_table(self):
        for value in self.CASES['usable']:
            with self.subTest(value):
                self.assertEqual(validate_settings({'hotkeys': {**dict.fromkeys(self.ACTIONS, ''), 'open_panel': value}})['hotkeys']['open_panel'], value)
        for kind, words in (('unknown', 'must be empty or a combination'), ('needs_modifier', 'needs Ctrl, Alt or Meta'), ('reserved', 'is used by the browser or by Agent Zero')):
            for value in self.CASES[kind]:
                with self.subTest(kind, value=value), self.assertRaises(ContractError) as caught:
                    validate_settings({'hotkeys': {'open_panel': value}})
                self.assertIn('The hotkey for open_panel ' + words, str(caught.exception))
        source = (Path(__file__).resolve().parents[1] / 'webui' / 'hotkeys.js').read_text(encoding='utf-8')
        for value in HOTKEY_RESERVED:
            if not re.fullmatch(r'Ctrl\+\d', value):                                                    # those ten are written as one line there
                self.assertIn("'" + value + "'", source, value)

    def test_two_actions_never_share_a_combination_however_it_is_spelled(self):
        for one, other in [('Alt+O', 'Alt+O')] + [tuple(pair) for pair in self.CASES['same']]:
            with self.subTest(one), self.assertRaises(ContractError) as caught:
                validate_settings({'hotkeys': {'toggle_conversation': '', 'open_panel': one, 'open_settings': other}})
            self.assertEqual(str(caught.exception), 'The hotkeys for open_panel and open_settings are the same')
        with self.assertRaises(ContractError):
            validate_settings({'hotkeys': {'open_panel': DEFAULTS['hotkeys']['mute']}})
        # The older single shortcut is not refused for being the same: a saved one must keep loading, and both start and stop.
        self.assertEqual(validate_settings({'hotkey': 'Alt+Shift+C'})['hotkeys'], DEFAULTS['hotkeys'])

    def test_a_wrong_shape_is_refused_and_the_words_repeat_nothing_of_it(self):
        for value in ('Alt+M', None, [], 7, {'mute': None}, {'mute': 1}, {'mute': ['Alt+M']}, {'shout': 'Alt+S'}, {'mute': {'key': 'M'}}):
            with self.subTest(value=value), self.assertRaises(ContractError) as caught:
                validate_settings({'hotkeys': value})
            self.assertEqual(str(caught.exception), 'hotkeys must hold one combination (or nothing) for each of: ' + ', '.join(self.ACTIONS))
        with self.assertRaises(ContractError) as caught:
            validate_settings({'hotkeys': {'mute': 'PRIVATE-VALUE'}})
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_the_planning_hotkey_is_beside_the_table_and_never_makes_the_settings_unreadable(self):
        self.assertEqual(validate_settings({})['planning_hotkey'], '')
        self.assertEqual(validate_settings({'planning_hotkey': 'Shift+Alt+P'})['planning_hotkey'], 'Alt+Shift+P')     # one spelling
        self.assertEqual(public_settings(validate_settings({'planning_hotkey': 'Alt+P'}))['planning_hotkey'], 'Alt+P')
        # One another action has (an older Convo does not know this setting), or one that cannot be used, is off.
        for value in ('Alt+Shift+M', 'Shift+Alt+M', 'Ctrl+Shift+Space', 'Ctrl+W', 'P', 'Alt+Nothing'):
            with self.subTest(value):
                self.assertEqual(validate_settings({'planning_hotkey': value})['planning_hotkey'], '')
        self.assertEqual(validate_settings({'planning_hotkey': 'Alt+O', 'hotkeys': {'open_panel': 'Alt+O'}})['planning_hotkey'], '')
        for value in (7, None, ['Alt+P'], 'Alt+' * 20 + 'P'):
            with self.assertRaises(ContractError):
                validate_settings({'planning_hotkey': value})

    def test_the_browser_runtime_is_told_the_hotkeys(self):
        settings = public_settings(validate_settings({'hotkeys': {'open_panel': 'F9'}}))
        self.assertEqual(settings['hotkeys'], {**DEFAULTS['hotkeys'], 'open_panel': 'F9'})


class VoiceTools(unittest.TestCase):
    """What the voice agent may do (`voice_tools`: one switch per group of tools) and where its conversation and its tasks belong
    (`conversation_scope`, `task_chat`): additive settings whose defaults keep the previous behaviour."""
    GROUPS = ('web', 'memory', 'tasks', 'system', 'timers', 'chats', 'senses', 'interface')

    def test_there_are_exactly_eight_switches_all_on_by_default(self):
        config = validate_settings({})
        self.assertEqual(config['voice_tools'], {group: True for group in self.GROUPS})
        self.assertEqual(tuple(DEFAULTS['voice_tools']), self.GROUPS)
        from plugins._convo.helpers import convo_tools
        self.assertEqual(set(config['voice_tools']), set(convo_tools.GROUPS))                  # one switch for every group a tool belongs to
        self.assertEqual({tool['group'] for tool in convo_tools.CATALOG.values()} - {'always'}, set(self.GROUPS))

    def test_an_older_configuration_without_the_switches_gets_the_defaults(self):
        older = {'enabled': True, 'style': 'Calm.', 'roles': {'conversation': {'url': 'http://127.0.0.1:8080/v1', 'model': 'm'}}}
        config = validate_settings(copy.deepcopy(older))
        self.assertEqual(config['voice_tools'], DEFAULTS['voice_tools'])
        self.assertEqual((config['conversation_scope'], config['task_chat']), ('chat', 'new'))
        self.assertEqual(config['style'], 'Calm.')
        self.assertIsNot(config['voice_tools'], DEFAULTS['voice_tools'])                       # never the shared default object itself

    def test_switches_saved_one_at_a_time_keep_the_others(self):
        config = validate_settings({'voice_tools': {'web': False, 'tasks': False}})
        self.assertEqual(config['voice_tools'], {**{group: True for group in self.GROUPS}, 'web': False, 'tasks': False})
        everything_off = validate_settings({'voice_tools': {group: False for group in self.GROUPS}})
        self.assertEqual(set(everything_off['voice_tools'].values()), {False})
        self.assertEqual(validate_settings(everything_off)['voice_tools'], everything_off['voice_tools'])    # saved and loaded unchanged

    def test_a_wrong_shape_is_refused_never_read_as_on_or_off(self):
        on = {group: True for group in self.GROUPS}
        bad = {
            'not an object': ['all', None, True, 1, ['web'], list(self.GROUPS)],
            'a switch that is not true or false': [{**on, 'web': 1}, {**on, 'web': 0}, {**on, 'web': 'yes'}, {**on, 'web': None}, {**on, 'web': 'false'},
                                                    {**on, 'web': {'enabled': True}}],
            'a switch for no group': [{**on, 'shell': True}, {**on, 'Web': True}, {**on, '': False}, {'everything': True}],
        }
        for name, values in bad.items():
            for value in values:
                with self.subTest(name, value=value), self.assertRaises(ContractError):
                    validate_settings({'voice_tools': value})

    def test_the_refusal_names_the_switches_and_nothing_of_the_value(self):
        with self.assertRaises(ContractError) as caught:
            validate_settings({'voice_tools': {'web': 'PRIVATE-VALUE'}})
        self.assertEqual(str(caught.exception), 'voice_tools must hold one true/false switch for each of: ' + ', '.join(self.GROUPS))

    def test_where_the_conversation_and_its_tasks_belong(self):
        for scope in ('chat', 'global'):
            for chat in ('new', 'selected'):
                config = validate_settings({'conversation_scope': scope, 'task_chat': chat})
                self.assertEqual((config['conversation_scope'], config['task_chat']), (scope, chat))
        for key, values in (('conversation_scope', ['', 'Global', 'all', 'chats', None, 1, True, ['global']]),
                            ('task_chat', ['', 'New', 'current', 'chat', None, 0, ['new']])):
            for value in values:
                with self.subTest(key, value=value), self.assertRaises(ContractError):
                    validate_settings({key: value})

    def test_the_browser_runtime_sees_where_the_conversation_belongs(self):
        settings = public_settings(validate_settings({'conversation_scope': 'global', 'task_chat': 'selected'}))
        self.assertEqual((settings['conversation_scope'], settings['task_chat']), ('global', 'selected'))


class SoundAndVoice(unittest.TestCase):
    """The sound panel's defaults (the panel keeps its own per device), who the assistant is, and the developer history views."""
    def test_defaults_keep_todays_behaviour(self):
        self.assertEqual({key: DEFAULTS[key] for key in ('barge_in_seconds', 'end_of_turn_seconds', 'noise_gate_db', 'voice_name',
                                                         'voice_profile', 'show_diagnostic_events', 'show_rejected_responses')},
                         {'barge_in_seconds': 1.0, 'end_of_turn_seconds': 0.9, 'noise_gate_db': -50, 'voice_name': '',
                          'voice_profile': '', 'show_diagnostic_events': False, 'show_rejected_responses': False})

    def test_bounds_are_enforced(self):
        good = validate_settings({'barge_in_seconds': .4, 'end_of_turn_seconds': 2.0, 'noise_gate_db': -66, 'voice_name': 'J.A.R.V.I.S',
                                  'voice_profile': 'jarvis', 'show_diagnostic_events': True, 'show_rejected_responses': True})
        self.assertEqual((good['voice_name'], good['voice_profile']), ('J.A.R.V.I.S', 'jarvis'))
        for path, value in (('barge_in_seconds', .3), ('end_of_turn_seconds', .4), ('end_of_turn_seconds', 2.5), ('noise_gate_db', -2),
                            ('noise_gate_db', -70), ('voice_name', 'two' + chr(10) + 'lines'), ('voice_name', 'x' * 65), ('voice_profile', '../agent0'),
                            ('voice_profile', 'a b'), ('show_diagnostic_events', 1), ('show_rejected_responses', 'yes')):
            with self.subTest(path=path, value=value), self.assertRaises(ContractError):
                validate_settings(with_value(path, value))

    def test_the_browser_sees_the_sound_and_history_settings_but_not_the_profile_path(self):
        public = public_settings(validate_settings({'voice_name': 'Jarvis'}))
        for key in ('barge_in_seconds', 'end_of_turn_seconds', 'noise_gate_db', 'voice_name', 'show_diagnostic_events', 'show_rejected_responses'):
            self.assertIn(key, public)


if __name__ == '__main__':
    unittest.main()
