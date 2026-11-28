import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import httpx

from plugins._convo.sidecar import studio
from plugins._convo.sidecar.audiocpp import downloads


MODEL = 'qwen3-tts-1.7b-base-bf16'
DATA = {'config.json': b'{}', 'model.safetensors': b'small-test-model'}


def metadata():
    return {'sha': 'a' * 40, 'cardData': {'license': 'apache-2.0'}, 'siblings': [
        {'rfilename': name, 'lfs': {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}} for name, data in DATA.items()]}


class ManifestTests(unittest.TestCase):
    def test_metadata_is_pinned_and_complete(self):
        plan = downloads.manifest(MODEL, metadata())
        self.assertEqual(plan['revision'], 'a' * 40)
        self.assertEqual(plan['size'], sum(map(len, DATA.values())))
        for key in ('sha', 'cardData', 'siblings'):
            invalid = metadata(); invalid.pop(key)
            with self.assertRaises(ValueError): downloads.manifest(MODEL, invalid)

    def test_paths_and_unknown_presets_are_rejected(self):
        for name in ('../model', '/model', 'a/../../model', 'a\\file', 'C:/model', '.git/config'):
            self.assertFalse(downloads.valid_file(name), name)
        self.assertTrue(downloads.valid_file('speech_tokenizer/model.safetensors'))


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.manager = downloads.Downloads(self.root)
        self.plan = downloads.manifest(MODEL, metadata()); self.manager.previews[MODEL] = self.plan
        self.requests = []
        def handle(request):
            self.requests.append(request)
            name = request.url.path.split('/')[-1]; data = DATA[name]
            offset = int(request.headers.get('range', 'bytes=0-').split('=')[1].split('-')[0])
            return httpx.Response(206 if offset else 200, content=data[offset:], headers={'content-range': f'bytes {offset}-{len(data)-1}/{len(data)}'})
        original = httpx.AsyncClient
        self.http = patch.object(downloads.httpx, 'AsyncClient', side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw)); self.http.start()
        self.space = patch.object(self.manager, 'headroom'); self.space.start()

    async def asyncTearDown(self):
        for model in self.manager.tasks: await self.manager.cancel(model)
        self.http.stop(); self.space.stop(); self.tmp.cleanup()

    async def test_download_needs_exact_approval_then_verifies_before_registration(self):
        with self.assertRaises(ValueError): self.manager.start(MODEL, 'wrong')
        self.assertEqual(self.requests, [])
        self.manager.start(MODEL, self.plan['id']); await self.manager.tasks[MODEL]
        self.assertEqual(self.manager.status(MODEL)['state'], 'complete')
        for name, content in DATA.items(): self.assertEqual((self.root / self.plan['directory'] / name).read_bytes(), content)
        self.assertTrue(all('authorization' not in r.headers for r in self.requests))

    async def test_partial_download_resumes_and_existing_models_are_not_overwritten(self):
        partial = self.root / '.downloads' / self.plan['id'] / 'model.safetensors'
        partial.parent.mkdir(parents=True); partial.write_bytes(DATA['model.safetensors'][:4])
        self.manager.start(MODEL, self.plan['id']); await self.manager.tasks[MODEL]
        self.assertTrue(any(r.headers.get('range') == 'bytes=4-' for r in self.requests))
        self.assertEqual(self.manager.status(MODEL)['state'], 'complete')
        count = len(self.requests)
        self.manager.start(MODEL, self.plan['id']); await self.manager.tasks[MODEL]
        self.assertEqual(len(self.requests), count)
        self.assertEqual(self.manager.status(MODEL)['state'], 'failed')

    async def test_corruption_is_quarantined_and_not_registered(self):
        partial = self.root / '.downloads' / self.plan['id'] / 'model.safetensors'
        partial.parent.mkdir(parents=True); partial.write_bytes(b'x' * len(DATA['model.safetensors']))
        self.manager.start(MODEL, self.plan['id']); await self.manager.tasks[MODEL]
        self.assertEqual(self.manager.status(MODEL)['state'], 'failed')
        self.assertFalse((self.root / self.plan['directory']).exists())
        self.assertFalse(partial.exists())
        self.assertTrue(list((self.root / '.rejected').rglob('*')))

    async def test_cancel_retains_partial_without_registering(self):
        entered = asyncio.Event()
        async def blocked(*args, **kwargs): entered.set(); await asyncio.Event().wait()
        with patch.object(downloads, 'public_get', side_effect=blocked):
            self.manager.start(MODEL, self.plan['id']); await asyncio.wait_for(entered.wait(), 2)
            await self.manager.cancel(MODEL)
        self.assertEqual(self.manager.status(MODEL)['state'], 'paused')
        self.assertFalse((self.root / self.plan['directory']).exists())


class StudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_cannot_target_external_services_or_override_gpu_guard(self):
        for operation in ('http://localhost:8881/stop', 'gpu_guard', 'delete_all'):
            with self.assertRaises(ValueError): await studio.control(operation, confirmed=True)
        with self.assertRaises(ValueError): await studio.control('load', {'model_key':MODEL})
        with self.assertRaises(ValueError): await studio.control('profile', item_id='../private')

    async def test_clone_and_engine_identity_are_frozen(self):
        profile = {'profile_id':'voice','content_revision':1,'content_hash':'f'*64,'ref_audio':'encoded','ref_text':'Reference','reference_excerpts':[]}
        tuning = {'id':'quality','revision':1,'max_reference_seconds':30,'first_block_frames':25,'steady_block_frames':25,'left_context_frames':25,'text_lookahead':128,'phrase_flush_ms':400,'temperature':0.8,'top_k':50,'top_p':1,'repetition_penalty':1.05,'seed':None}
        async def control(operation, payload=None, **kwargs):
            if operation == 'resolve_tuning': return {'profile':tuning}
            return {'loaded_models':[MODEL], 'engineEpoch':4, 'supervisorInstanceId':'instance'} if operation == 'models' else profile
        with patch.object(studio, 'control', side_effect=control):
            role = await studio.freeze({'model':MODEL,'voice':'clone:voice','instructions':'unsupported'})
        profile['ref_text'] = 'Edited after snapshot'
        self.assertEqual(role['clone_snapshot']['ref_text'], 'Reference')
        self.assertEqual(role['expected_engine_epoch'], 4)
        self.assertEqual(role['url'], 'http://convo-audio:8080/v1')
        self.assertEqual(role['instructions'], '')
        tuning['temperature'] = 1.2
        self.assertEqual(role['tuning']['effective']['temperature'], 0.8)
        self.assertEqual(role['tuning']['effective']['seed'], role['tuning']['overrides']['seed'])
        self.assertIsInstance(role['tuning']['effective']['seed'], int)


if __name__ == '__main__': unittest.main()
