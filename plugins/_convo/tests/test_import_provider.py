"""An imported speech backend or conversation URL must survive the provider normalizers: importing selects the generic endpoint."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plugins._convo.helpers import convo_setup  # noqa: E402


class ImportClearsTheStoredProvider(unittest.TestCase):
    def test_an_imported_speech_backend_clears_the_speech_provider(self):
        values, _ = convo_setup._import_values({'roles': {'tts': {'backend': 'managed_audio', 'model': 'm'}}})
        self.assertEqual(values['roles.tts.provider'], '')
        self.assertEqual(values['roles.tts.backend'], 'managed_audio')

    def test_an_imported_conversation_url_clears_the_conversation_provider(self):
        values, _ = convo_setup._import_values({'llm_base_url': 'http://host.docker.internal:8080/v1'})
        self.assertEqual(values['roles.conversation.provider'], '')

    def test_nothing_is_cleared_when_neither_is_imported(self):
        values, _ = convo_setup._import_values({'roles': {'tts': {'voice': 'clone:abc'}, 'conversation': {'model': 'x'}}})
        self.assertNotIn('roles.tts.provider', values)
        self.assertNotIn('roles.conversation.provider', values)


if __name__ == '__main__':
    unittest.main()
