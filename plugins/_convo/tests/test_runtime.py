import asyncio
import base64
import io
import json
import os
import sys
import threading
import types
import tempfile
import unittest
import wave
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

import httpx

from plugins._convo.helpers.convo_contract import DEFAULTS, ContractError, merge, public_settings, validate_settings
from plugins._convo.helpers.convo_host import Compactor, Dispatcher, Host
from plugins._convo.helpers.convo_store import Store
from plugins._convo.sidecar.app import app, input_content


def wav():
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000); out.writeframes(b'\0\0' * 160)
    return base64.b64encode(buffer.getvalue()).decode()


class ConfigTests(unittest.TestCase):
    def test_public_status_excludes_credentials_and_unknown_fields(self):
        settings = validate_settings({'secret': 'never', 'roles': {'conversation': {'token_env': 'OTHER_SECRET'}}})
        result = json.dumps(public_settings(settings))
        self.assertNotIn('OTHER_SECRET', result)
        self.assertNotIn('never', result)
        self.assertNotIn('token_env', result)

    def test_nonfinite_and_illtyped_config_rejected(self):
        for setting in ({'direct_timeout': float('nan')}, {'compaction': {'target': 'bad'}}, {'roles': []}, {'activation_names': 'jarvis'}, {'compaction': {'recent_exchanges': 6.5}}, {'hotkey': {}}, {'sidecar': {'remote': 'false'}}):
            with self.assertRaises(ContractError):
                validate_settings(setting)

    def test_defaults_match_browser_and_packaged_config(self):
        root = Path(__file__).resolve().parents[1]
        raw = (root / 'webui/defaults.js').read_text().split('export const defaults = ', 1)[1].split(';\n', 1)[0]
        self.assertEqual(json.loads(raw), DEFAULTS)
        raw = (root / 'default_config.yaml').read_text().split('\nconvo: ', 1)[1]
        self.assertEqual(json.loads(raw), DEFAULTS)

    def test_text_only_slot_does_not_require_a_fourth_interpreter(self):
        self.assertEqual(input_content({'audio': wav(), 'transcript': 'Hello'}, {'capabilities': {}}), 'Hello')
        with self.assertRaises(ContractError):
            input_content({'audio': wav()}, {'capabilities': {}})
        self.assertEqual(input_content({'audio': wav()}, {'capabilities': {'audio_input': True}})[0]['type'], 'input_audio')


class JournalTests(unittest.TestCase):
    def test_only_completed_playback_enters_working_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'history.db')
            try:
                session = store.start('browser', 'a'); sid = session['id']
                store.accept_turn(sid, 'browser', 0, 't', {'transcript': 'Question'})
                store.settle(sid, 'browser', 0, 't', 'Generated but not heard')
                self.assertNotIn('assistant', store.context('a')['turns'][0]['content'])
                self.assertTrue(store.delivered(sid, 'browser', 0, 't', 'phrase', 'Heard sentence'))
                self.assertFalse(store.delivered(sid, 'browser', 0, 't', 'phrase', 'Heard sentence'))
                store.interrupt(sid, 'browser')
                self.assertFalse(store.delivered(sid, 'browser', 0, 't', 'late', 'Stale audio'))
                self.assertEqual(store.context('a')['turns'][0]['content']['assistant'], 'Heard sentence')
            finally:
                store.close()

    def test_interrupted_turns_do_not_permanently_block_compaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'history.db')
            try:
                s = store.start('browser', 'a')
                for n in range(12):
                    store.accept_turn(s['id'], 'browser', s['epoch'], str(n), {'note': 'Pending thought'})
                    s = store.interrupt(s['id'], 'browser')
                snap = store.snapshot(s['id'], 'browser')
                self.assertTrue(store.splice(snap, 'Interrupted thoughts; no promised actions'))
            finally:
                store.close()


class FakeHost:
    def __init__(self):
        self.busy = True; self.begun = []; self.future = Future(); self.cancelled = []
    def available(self, target): return not self.busy
    def begin(self, job): self.begun.append(job); self.busy = True; return self.future
    def cancel(self, job, future): self.cancelled.append(job['id']); future.cancel()
    def steer(self, job, future, text): self.steered = text
    def log(self, *args): pass


class DispatcherTests(unittest.TestCase):
    def test_busy_target_is_quiet_and_voice_stop_does_not_cancel_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'jobs.db')
            try:
                s = store.start('browser', 'a'); host = FakeHost(); dispatcher = Dispatcher(store, host)
                job = store.submit(s['id'], 'browser', 0, 't', 'request', 'Authorized work')
                for _ in range(20): dispatcher.tick()
                self.assertEqual(len(store.events('a')), 1)
                self.assertEqual(host.begun, [])
                host.busy = False; dispatcher.tick(); store.stop(s['id'], 'browser')
                self.assertEqual(store.job(job['id'])['status'], 'running')
                self.assertEqual(host.cancelled, [])
                host.future.set_result('Done'); dispatcher.tick()
                self.assertEqual(store.job(job['id'])['status'], 'completed')
                dispatcher.tick(); self.assertEqual(len(host.begun), 1)
            finally: store.close()

    def test_cancel_does_not_claim_side_effects_were_undone(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'jobs.db')
            try:
                s = store.start('browser', 'a'); host = FakeHost(); host.busy = False
                dispatcher = Dispatcher(store, host)
                job = store.submit(s['id'], 'browser', 0, 't', 'request', 'Authorized work')
                dispatcher.tick(); dispatcher.cancel(job['id']); dispatcher.tick()
                self.assertEqual(store.job(job['id'])['status'], 'uncertain')
            finally: store.close()


class SidecarTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token = 'test-only-not-a-real-token-000000'
        self.environment = patch.dict(os.environ, {'CONVO_SIDECAR_TOKEN': self.token})
        self.environment.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://sidecar')
        self.headers = {'Authorization': 'Bearer ' + self.token}

    async def asyncTearDown(self):
        await self.client.aclose(); self.environment.stop()

    async def test_auth_required_on_all_routes(self):
        for path in ('/health', '/policy', '/answer', '/speech', '/studio', '/preview', '/freeze-speech'):
            response = await self.client.request('GET' if path == '/health' else 'POST', path, json={})
            self.assertEqual(response.status_code, 401, path)
        self.assertEqual((await self.client.get('/health', headers=self.headers)).status_code, 200)

    async def test_remote_ambient_denied_before_any_provider_request(self):
        response = await self.client.post('/policy', headers=self.headers, json={'role': {'url': 'https://provider.example', 'remote': True, 'model': 'classifier'}, 'ambient': True})
        self.assertEqual(response.status_code, 403)

    async def test_provider_wire_contract_and_streamed_tools(self):
        captured = []
        def handle(request):
            captured.append(json.loads(request.content))
            lines = [
                {'choices': [{'delta': {'content': 'Draft'}}]},
                {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'delegate_task', 'arguments': '{"task":"Do work"}'}}]}}]},
            ]
            return httpx.Response(200, text=''.join('data: ' + json.dumps(v) + '\n\n' for v in lines) + 'data: [DONE]\n\n')
        original = httpx.AsyncClient
        with patch('plugins._convo.sidecar.app.httpx.AsyncClient', side_effect=lambda **kw: original(**{**kw, 'transport': httpx.MockTransport(handle)})):
            role = merge(DEFAULTS['roles']['conversation'], {'model': 'test'})
            response = await self.client.post('/answer', headers=self.headers, json={'role': role, 'system': 'Voice rules', 'history': [], 'audio': wav(), 'allow_tools': True})
        self.assertIn('tool.proposal', response.text)
        self.assertIn('input_audio', json.dumps(captured))
        self.assertEqual(captured[0]['tools'][0]['function']['name'], 'delegate_task')

    async def test_bad_text_only_request_is_rejected_without_network(self):
        response = await self.client.post('/answer', headers=self.headers, json={'role': {'url': 'http://localhost', 'model': 'test', 'capabilities': {}}, 'system': '', 'audio': wav()})
        self.assertEqual(response.status_code, 400)


class NativeSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_detached_native_synthesis_does_not_block_loop_or_overlap_next_request(self):
        entered, released = threading.Event(), threading.Event()
        async def native(*args):
            entered.set(); released.wait(2)
            return base64.b64encode(b'wave').decode()
        runtime = types.SimpleNamespace()
        package = types.ModuleType('plugins._kokoro_tts.helpers'); package.runtime = runtime
        adapter = types.ModuleType('plugins._convo.helpers.kokoro_adapter'); adapter._synthesize_local = native
        host = Host()
        with patch.dict(sys.modules, {'plugins._kokoro_tts.helpers':package,'plugins._convo.helpers.kokoro_adapter':adapter}):
            task = asyncio.create_task(host.speech({'host_config':{}}, 'Preview'))
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task, .5)
                self.assertFalse(host.speech_future.done())
                with self.assertRaises(ValueError): await host.speech({'host_config':{}}, 'No overlapping generation')
            finally:
                released.set()
                for _ in range(100):
                    if host.speech_future and host.speech_future.done(): break
                    await asyncio.sleep(.01)


if __name__ == '__main__': unittest.main()
