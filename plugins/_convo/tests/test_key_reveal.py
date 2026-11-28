"""The eye on a key field (convo_setup.key_reveal): one saved Convo key, on request, whole only behind a login."""
import importlib
import unittest
from unittest import mock

setup = importlib.import_module('plugins._convo.helpers.convo_setup')


class Lock:
    def __enter__(self): return self
    def __exit__(self, *args): return False


class Manager:
    def __init__(self, values): self.values = values
    def load_secrets(self): return dict(self.values)


class Reveal(unittest.TestCase):
    def ask(self, values, login, role='conversation'):
        with mock.patch.object(setup, '_secrets', lambda: Manager(values)), mock.patch.object(setup.store, 'lock', lambda: Lock()), \
                mock.patch.object(setup, '_login_set', lambda: login):
            return setup.key_reveal(role)

    def test_whole_key_only_behind_a_login(self):
        values = {'CONVO_CONVERSATION_API_KEY': 'test-key-0123456789abcd', 'CONVO_TTS_API_KEY': 'other-speech-key-zzzz'}
        self.assertEqual(self.ask(values, True), {'ok': True, 'whole': True, 'key': 'test-key-0123456789abcd', 'ending': ''})
        self.assertEqual(self.ask(values, False), {'ok': True, 'whole': False, 'key': '', 'ending': 'abcd'})      # no login: the ending only
        self.assertEqual(self.ask(values, False, 'tts')['ending'], 'zzzz')                                      # one card's key, never another's

    def test_a_short_key_shows_nothing_without_a_login(self):
        self.assertEqual(self.ask({'CONVO_CLASSIFIER_API_KEY': 'short-key'}, False, 'classifier'), {'ok': True, 'whole': False, 'key': '', 'ending': ''})

    def test_no_saved_key_and_unknown_cards_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'no saved key'):
            self.ask({}, True)
        with self.assertRaisesRegex(ValueError, 'no saved key'):
            self.ask({'CONVO_CONVERSATION_API_KEY_URL': 'https://example.invalid/v1'}, True)                   # a binding is not a key
        for role in ('sidecar', 'CONVERSATION', '', None, 'API_KEY_OPENAI'):
            with self.assertRaisesRegex(ValueError, 'Unknown model role'):
                self.ask({'CONVO_CONVERSATION_API_KEY': 'test-key-0123456789abcd'}, True, role)

    def test_a_login_check_that_fails_counts_as_no_login(self):
        with mock.patch.dict('sys.modules', {'helpers.login': None}):
            self.assertFalse(setup._login_set())


if __name__ == '__main__':
    unittest.main()
