import base64
import io
import json
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

from plugins._convo.sidecar.studio_adapter import StudioAdapter, read_archive, validate_wav, ContractError, tuning_reference_seconds, TUNING_REFERENCE_MAX
from plugins._convo.sidecar.gradio_studio import preview_from_request


def wav():
    out = io.BytesIO()
    with wave.open(out, 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
        stream.writeframes(b'\0\0' * 2400)
    return out.getvalue()


def archive(members=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for key, value in (members or {'voice/meta.json': json.dumps({'name':'Test', 'task_type':'Base', 'ref_text':'Reference', 'ref_audio_filename':'ref_audio.wav'}), 'voice/ref_audio.wav':wav()}).items():
            z.writestr(key, value)
    return out.getvalue()


class ArchiveTests(unittest.TestCase):
    def test_original_format_reuses_metadata_but_not_identity(self):
        payload = read_archive(archive())[0]
        self.assertEqual(payload['task_type'], 'Base')
        self.assertNotIn('profile_id', payload)
        self.assertEqual(base64.b64decode(payload['ref_audio']), wav())

    def test_traversal_and_foreign_types_refused(self):
        for key in ('../voice/meta.json', '/voice/meta.json', 'C:/voice/meta.json', 'voice\\meta.json'):
            with self.assertRaises(ContractError): read_archive(archive({key:'{}'}))
        with self.assertRaises(ContractError):
            read_archive(archive({'v/meta.json':json.dumps({'task_type':'CustomVoice'}), 'v/ref_audio.wav':wav()}))

    def test_duplicate_entries_and_bomb_refused(self):
        import warnings
        out = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(out, 'w') as z:
                z.writestr('v/meta.json', '{}'); z.writestr('v/meta.json', '{}')
        with self.assertRaises(ContractError): read_archive(out.getvalue())
        with self.assertRaises(ContractError): read_archive(archive({'huge':b'x' * (13 * 1024**2)}))

    def test_invalid_wav_refused(self):
        for value in (b'', b'not audio', wav()[:-3]):
            with self.assertRaises(ContractError): validate_wav(value)

    def test_tuning_duration_bounds_are_not_upload_duration_bounds(self):
        self.assertEqual(TUNING_REFERENCE_MAX, 30)
        for value in (1, 1.0, 30): self.assertEqual(tuning_reference_seconds(value), int(value))
        for value in (0, 31, 90, True, '30', None, 1.5, float('nan')):
            with self.assertRaises(ContractError): tuning_reference_seconds(value)
        out = io.BytesIO()
        with wave.open(out, 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
            stream.writeframes(b'\0\0' * 8000 * 45)
        self.assertEqual(validate_wav(out.getvalue()), out.getvalue())


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.control = AsyncMock()
        self.adapter = StudioAdapter(self.root, control=self.control)

    async def test_create_routes_only_fixed_control(self):
        path = self.root / 'ref.wav'; path.write_bytes(wav())
        await self.adapter.create('Test', 'English', str(path), 'Reference')
        args, kwargs = self.control.call_args
        self.assertEqual(args[0], 'create_profile'); self.assertTrue(kwargs['confirmed'])
        self.assertEqual(args[1]['ref_text'], 'Reference')
        with self.assertRaises(ContractError): self.adapter.upload(str(self.root.parent / 'outside.wav'))

    async def test_export_import_roundtrip_allocates_new_profile(self):
        self.control.return_value = {'name':'Test', 'language':'English', 'ref_text':'Reference', 'ref_audio':base64.b64encode(wav()).decode()}
        path = await self.adapter.export_profile('voice')
        self.control.reset_mock()
        await self.adapter.import_profiles(path)
        args, kwargs = self.control.call_args
        self.assertEqual(args[0], 'create_profile'); self.assertNotIn('profile_id', args[1])

    async def test_invalid_archive_never_partially_writes(self):
        path = self.root / 'bad.zip'; path.write_bytes(archive({'voice/meta.json':'{}'}))
        with self.assertRaises(ContractError): await self.adapter.import_profiles(str(path))
        self.control.assert_not_called()

    async def test_tuning_copy_preserves_source_and_revision_required(self):
        await self.adapter.tuning_create('New', 'quality')
        self.assertEqual(self.control.call_args.args[1]['clone_from'], 'quality')
        with self.assertRaises(ContractError): await self.adapter.tuning_save('custom', {'gpu_guard':'disabled'})

    async def test_preview_uses_frozen_boundary_and_no_auto_selection(self):
        preview = AsyncMock(return_value={'audio':base64.b64encode(wav()).decode()})
        self.adapter.preview_speech = preview
        path = await self.adapter.preview('voice', 'Hello', 'shared-model', 'custom-tuning')
        self.assertEqual(Path(path).read_bytes(), wav())
        preview.assert_awaited_once_with({'voice':'clone:voice', 'text':'Hello', 'model':'shared-model', 'tuning_profile':'custom-tuning'})
        self.control.assert_not_called()

    async def test_preview_callback_forwards_both_host_snapshot_fields(self):
        adapter = AsyncMock()
        await preview_from_request(adapter, 'voice', 'Hello', {
            'x-convo-tts-model':'shared-model', 'x-convo-tts-tuning-profile':'custom-tuning'})
        adapter.preview.assert_awaited_once_with('voice', 'Hello', 'shared-model', 'custom-tuning')

    async def test_invalid_tuning_reference_never_reaches_control(self):
        for value in (31, 90, True, 2.5):
            with self.assertRaises(ContractError):
                await self.adapter.tuning_save('custom', {'revision':1, 'max_reference_seconds':value})
        self.control.assert_not_called()
        await self.adapter.tuning_save('custom', {'revision':1, 'max_reference_seconds':30})
        self.assertEqual(self.control.call_args.args[1]['max_reference_seconds'], 30)


if __name__ == '__main__': unittest.main()
