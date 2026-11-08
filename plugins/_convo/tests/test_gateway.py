"""Import the actual headless supervisor without touching a GPU or host storage."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


ROOT = Path(__file__).resolve().parents[1] / 'sidecar/audiocpp'


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result; spec.loader.exec_module(result)
    return result


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def assert_guard_rejects_load(self, *, telemetry=None, failure=None, reason):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'
            config.write_text((ROOT / 'qwen3-tts-base-f16.json').read_text())
            # A persisted standalone bypass must not weaken Convo admission.
            bypass = Path(tmp) / 'candidate_gpu_guard.json'
            bypass.write_text(json.dumps({'mode':'disabled', 'load_min_free_mib':0,
                                          'load_max_utilization_percent':100}))
            original_policy = bypass.read_bytes()
            path_before = list(sys.path)
            try:
                with patch.dict(sys.modules), patch.dict(os.environ, {
                    'AUDIO_CPP_CONFIG_TEMPLATE':str(config), 'VOICE_LIBRARY_DIR':tmp,
                    'CONVO_AUDIO_TOKEN':'unit-test-only-audio-token-000000'}), \
                        patch('subprocess.Popen', side_effect=AssertionError('No processes permitted')) as process, \
                        patch('subprocess.check_output', return_value=telemetry, side_effect=failure) as gpu, \
                        patch('logging.basicConfig'):
                    supervisor = module('supervisor', 'supervisor.py')
                    module('downloads', 'downloads.py')
                    gateway = module('convo_gateway_admission_test', 'gateway.py')
                    self.assertEqual(supervisor.gpu_guard_settings['mode'], 'enforced')
                    self.assertEqual(bypass.read_bytes(), original_policy)
                    await supervisor.startup()
                    model_id = next(iter(supervisor._models()))
                    with patch.object(supervisor, '_start_engine', side_effect=AssertionError('Blocked load reached engine')) as start:
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url='http://audio') as client:
                            response = await client.post('/v1/backend/models/switch',
                                headers={'Authorization':'Bearer unit-test-only-audio-token-000000'},
                                json={'model_key':model_id})
                    self.assertEqual(response.status_code, 409, response.text)
                    detail = response.json()['detail']
                    self.assertFalse(detail['ok'])
                    self.assertEqual(detail['state'], 'blocked')
                    self.assertIn(reason, detail['reason'])
                    self.assertNotEqual(supervisor.state['state'], 'loading')
                    self.assertIsNone(supervisor.engine)
                    self.assertEqual(supervisor.EVENTS[-1]['action'], 'gpu-blocked-load')
                    gpu.assert_called_once()
                    start.assert_not_called(); process.assert_not_called()
            finally:
                sys.path[:] = path_before

    async def test_low_vram_blocks_before_engine_start(self):
        await self.assert_guard_rejects_load(telemetry='0, 0', reason='GPU load blocked')

    async def test_busy_gpu_blocks_before_engine_start(self):
        await self.assert_guard_rejects_load(telemetry='65536, 100', reason='GPU busy')

    async def test_missing_gpu_telemetry_blocks_before_engine_start(self):
        await self.assert_guard_rejects_load(failure=FileNotFoundError('mock telemetry unavailable'), reason='GPU status unavailable')

    async def test_malformed_gpu_telemetry_blocks_before_engine_start(self):
        await self.assert_guard_rejects_load(telemetry='N/A, N/A', reason='GPU status unavailable')

    async def test_auth_and_route_allowlist_do_not_start_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'
            config.write_text((ROOT / 'qwen3-tts-base-f16.json').read_text())
            path_before = list(sys.path)
            try:
                with patch.dict(sys.modules), patch.dict(os.environ, {'AUDIO_CPP_CONFIG_TEMPLATE':str(config), 'VOICE_LIBRARY_DIR':tmp, 'CONVO_AUDIO_TOKEN':'unit-test-only-audio-token-000000'}), patch('subprocess.Popen', side_effect=AssertionError('No processes permitted')), patch('logging.basicConfig'):
                    supervisor = module('supervisor', 'supervisor.py')
                    module('downloads', 'downloads.py')
                    gateway = module('convo_gateway_test', 'gateway.py')
                    self.assertEqual(supervisor.gpu_guard_settings['mode'], 'enforced')
                    await supervisor.startup()
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url='http://audio') as client:
                        response = await client.get('/convo/catalog')
                        self.assertEqual(response.status_code, 401)
                        headers = {'Authorization':'Bearer unit-test-only-audio-token-000000'}
                        response = await client.get('/convo/catalog', headers=headers)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(len(response.json()['models']), 2)
                        response = await client.post('/control/gpu-guard', headers=headers, json={'mode':'disabled'})
                        self.assertEqual(response.status_code, 404)
                        response = await client.post('/v1/voice-studio/llamacpp-audio-turn/stream', headers=headers, json={})
                        self.assertEqual(response.status_code, 404)
                    self.assertEqual(supervisor.state['state'], 'unloaded')
            finally: sys.path[:] = path_before


if __name__ == '__main__': unittest.main()
