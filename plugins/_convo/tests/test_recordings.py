"""Developer "Record raw and sent" (helpers/convo_recordings.py, the conversation API): written only through recording_save, in developer
mode, to the plugin's data folder; listed and deletable; the analysis finds the clipped start and the silenced stretches."""
import asyncio
import base64
import importlib
import io
import math
import struct
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from plugins._convo.helpers import convo_recordings as recordings


def wav(samples, rate):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(rate)
        writer.writeframes(b''.join(struct.pack('<h', max(-32768, min(32767, int(value * 32767)))) for value in samples))
    return base64.b64encode(buffer.getvalue()).decode()


def tone(seconds, rate, level=.2):
    return [level * math.sin(2 * math.pi * 220 * n / rate) for n in range(int(seconds * rate))]


class Analysis(unittest.TestCase):
    def test_clipped_start_and_a_silenced_stretch_are_found_aligned_on_the_ends(self):
        # Raw: 0.2 s quiet, then 1.0 s of voice. Sent: the same last 1.0 s, with its first 0.15 s and a stretch in the middle zeroed.
        rate = 16000
        raw = [0.0] * int(.2 * rate) + tone(1.0, rate)
        sent = tone(1.0, rate)
        for n in range(int(.15 * rate)): sent[n] = 0.0
        for n in range(int(.5 * rate), int(.7 * rate)): sent[n] = 0.0
        result = recordings.analyse(rate, raw, rate, sent)
        self.assertEqual((result['raw_s'], result['sent_s']), (1.2, 1.0))
        self.assertEqual(result['clipped_start_ms'], 150)
        self.assertEqual([item for item in result['silenced']], [{'from_s': 0.0, 'to_s': 0.15}, {'from_s': 0.5, 'to_s': 0.7}])
        self.assertGreater(result['raw_peak_db'], -20)
        nothing = recordings.analyse(rate, raw, rate, raw[-rate:])
        self.assertEqual((nothing['clipped_start_ms'], nothing['silenced']), (0, []))


class Storage(unittest.TestCase):
    def test_saved_listed_and_deleted_and_bad_audio_refused(self):
        with tempfile.TemporaryDirectory() as data:
            item, analysis = recordings.save(data, wav(tone(.5, 48000), 48000), wav(tone(.4, 16000), 16000), .4, 'Chrome on Windows', 'call')
            folder = Path(data) / 'recordings'
            self.assertTrue((folder / (item + '-raw.wav')).exists() and (folder / (item + '-sent.wav')).exists() and (folder / (item + '.json')).exists())
            listed = recordings.list_recordings(data)
            self.assertEqual([(entry['id'], entry['device'], entry['mode']) for entry in listed], [(item, 'Chrome on Windows', 'call')])
            self.assertEqual(analysis['pre_roll_s'], .4)
            self.assertTrue(recordings.delete(data, item))
            self.assertEqual(list(folder.glob(item + '*')), [])
            self.assertFalse(recordings.delete(data, item))
            with self.assertRaises(ValueError): recordings.delete(data, '../x')
            with self.assertRaises(ValueError): recordings.save(data, 'not base64!', wav(tone(.1, 16000), 16000))
            with self.assertRaises(ValueError): recordings.save(data, wav(tone(.1, 48000), 48000), wav(tone(.1, 48000), 48000))   # sent is 16 kHz only
            with self.assertRaises(ValueError): recordings.save(data, wav(tone(32, 8000), 8000), wav(tone(.1, 16000), 16000))     # too long


class Route(unittest.IsolatedAsyncioTestCase):
    async def test_the_api_writes_only_on_recording_save_and_only_in_developer_mode(self):
        with tempfile.TemporaryDirectory() as root:
            config = {'developer_mode': False}
            setup = types.SimpleNamespace(store=types.SimpleNamespace(config_path=lambda: Path(root) / 'config.json'))
            helpers = types.SimpleNamespace(__package__='plugins._convo.helpers', settings=lambda: config)
            api_stub = types.ModuleType('helpers.api')
            api_stub.ApiHandler = type('ApiHandler', (), {'__init__': lambda self, *a, **k: None})
            api_stub.Request = object
            with patch.dict(sys.modules, {'helpers.api': api_stub}):
                sys.modules.pop('plugins._convo.api.conversation', None)
                module = importlib.import_module('plugins._convo.api.conversation')
            self.addCleanup(sys.modules.pop, 'plugins._convo.api.conversation', None)   # built on a stand-in host API: never left behind
            real_import = importlib.import_module
            def fake_import(name, *args):
                return setup if name.endswith('.convo_setup') else real_import(name, *args)
            handler = module.Conversation() if hasattr(module, 'Conversation') else next(
                value for value in vars(module).values() if isinstance(value, type) and issubclass(value, api_stub.ApiHandler) and value is not api_stub.ApiHandler)()
            with patch.object(module, 'module', return_value=helpers), patch.object(module.importlib, 'import_module', side_effect=fake_import):
                refused = await handler.process({'action': 'recording_save', 'raw': '', 'sent': ''}, None)
                self.assertFalse(refused['ok'])
                self.assertIn('developer mode', refused['error'])
                self.assertFalse((Path(root) / 'data').exists(), 'nothing is written without developer mode')
                config['developer_mode'] = True
                saved = await handler.process({'action': 'recording_save', 'raw': wav(tone(.3, 48000), 48000), 'sent': wav(tone(.3, 16000), 16000)}, None)
                self.assertTrue(saved['ok'])
                self.assertNotIn('raw_levels', saved['analysis'])
                listed = await handler.process({'action': 'recording_list'}, None)
                self.assertEqual([item['id'] for item in listed['recordings']], [saved['id']])
                self.assertTrue((await handler.process({'action': 'recording_delete', 'id': saved['id']}, None))['ok'])
                self.assertEqual((await handler.process({'action': 'recording_list'}, None))['recordings'], [])


if __name__ == '__main__':
    unittest.main()
