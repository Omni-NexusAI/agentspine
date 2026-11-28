"""Developer test clips for the decision-model bake-off, and the build identity a stale page compares against."""
import base64
import importlib
import io
import os
import tempfile
import unittest
import wave
from pathlib import Path

clips = importlib.import_module('plugins._convo.helpers.convo_clips')
build = importlib.import_module('plugins._convo.helpers.convo_build')


def wav(seconds, rate=16000):
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(rate)
        writer.writeframes(b'\x01\x00' * int(seconds * rate))
    return base64.b64encode(output.getvalue()).decode()


class Clips(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def test_a_clip_is_saved_listed_and_deleted_with_its_label_and_length(self):
        clip = clips.save(self.data, 'unfinished', wav(1.5), device='Phone  mic')
        listed = clips.list_clips(self.data)
        self.assertEqual([(c['id'], c['label'], c['seconds'], c['device']) for c in listed], [(clip, 'unfinished', 1.5, 'Phone mic')])
        self.assertTrue((clips.folder(self.data) / (clip + '.wav')).is_file())
        self.assertTrue(clips.delete(self.data, clip))
        self.assertEqual(clips.list_clips(self.data), [])
        self.assertFalse((clips.folder(self.data) / (clip + '.wav')).exists())

    def test_bad_labels_audio_and_ids_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'label'):
            clips.save(self.data, 'anything', wav(1))
        with self.assertRaisesRegex(ValueError, 'at most'):
            clips.save(self.data, 'noise', wav(clips.MAX_SECONDS + 1))
        with self.assertRaises(ValueError):
            clips.save(self.data, 'noise', 'not audio')
        with self.assertRaisesRegex(ValueError, 'Unknown clip'):
            clips.delete(self.data, '../index')
        self.assertEqual(clips.list_clips(self.data), [])


class Build(unittest.TestCase):
    def test_the_build_changes_with_a_browser_file_and_not_with_anything_else(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'webui')); os.makedirs(os.path.join(root, 'helpers'))
            Path(root, 'webui', 'main.html').write_text('<div>one</div>')
            first = build.build_id(root)
            Path(root, 'helpers', 'server.py').write_text('x = 1')
            self.assertEqual(build.build_id(root), first)
            Path(root, 'webui', 'main.html').write_text('<div>two</div>')
            self.assertNotEqual(build.build_id(root), first)


if __name__ == '__main__':
    unittest.main()
