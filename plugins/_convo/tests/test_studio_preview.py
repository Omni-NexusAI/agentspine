"""Shared preview snapshot regression: no Gradio, network, model or GPU required."""
import importlib.util
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins._convo.sidecar.studio_adapter import StudioAdapter
from plugins._convo.sidecar.gradio_studio import preview_from_request


@unittest.skipIf(importlib.util.find_spec('fastapi') is None, 'FastAPI unavailable in this interpreter')
class SharedPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_tuning_seed_and_reference_limit_survive_entire_preview_path(self):
        from plugins._convo.sidecar import app
        profile = {'profile_id':'voice', 'content_revision':2, 'content_hash':'fingerprint',
                   'ref_audio':'reference', 'ref_text':'Transcript', 'reference_excerpts':[]}
        tuning = {'id':'custom', 'revision':3, 'max_reference_seconds':12,
                  'first_block_frames':4, 'steady_block_frames':12, 'left_context_frames':25,
                  'text_lookahead':64, 'phrase_flush_ms':500, 'temperature':1,
                  'top_k':50, 'top_p':.95, 'repetition_penalty':1.05, 'seed':123}
        async def control(operation, payload=None, **kwargs):
            if operation == 'models': return {'loaded_models':['shared-model'], 'engineEpoch':7, 'supervisorInstanceId':'owned'}
            if operation == 'profile': return profile
            if operation == 'resolve_tuning':
                self.assertEqual(payload['profile_id'], 'custom')
                self.assertEqual(payload['scope'], 'realtime')
                return {'profile':tuning, 'temporaryOverrides':{}}
            self.fail('Unexpected operation ' + operation)
        speech = AsyncMock(return_value=SimpleNamespace(body=b'bounded-wave'))
        with tempfile.TemporaryDirectory() as root, patch.object(app.studio, 'control', side_effect=control), patch.object(app, 'speech', speech):
            adapter = StudioAdapter(root, preview=app.preview_speech)
            await preview_from_request(adapter, 'voice', 'Hello', {
                'x-convo-tts-model':'shared-model', 'x-convo-tts-tuning-profile':'custom'})
        role = speech.call_args.args[0]['role']
        self.assertEqual(role['tuning']['profile_id'], 'custom')
        self.assertEqual(role['tuning']['profile_revision'], 3)
        self.assertEqual(role['tuning']['effective']['max_reference_seconds'], 12)
        self.assertEqual(role['tuning']['overrides']['seed'], 123)
        self.assertEqual(role['expected_engine_epoch'], 7)
        tuning['max_reference_seconds'] = 30
        self.assertEqual(role['tuning']['effective']['max_reference_seconds'], 12)


if __name__ == '__main__': unittest.main()
