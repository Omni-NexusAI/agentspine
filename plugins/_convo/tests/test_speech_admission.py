"""Real ASGI disconnects and mock-only provider IO; no services or model loads."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from plugins._convo.sidecar import app, speech_admission as admission


TOKEN = 'test-only-never-real-speech-token'
ROLE = {'url': 'http://localhost:8881/v1', 'model': 'test-model', 'voice': 'test-voice',
        'capabilities': {'audio_output': True}}
PAYLOAD = {'role': ROLE, 'text': 'private test phrase'}


class DelayedBody(httpx.AsyncByteStream):
    def __init__(self):
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.closed, self.terminal = False, False

    async def __aiter__(self):
        self.started.set()
        yield b'first-audio'
        await self.release.wait()
        yield b'late-audio'
        self.terminal = True

    async def aclose(self):
        self.closed = True


class TruncatedBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'private upstream message'
        raise httpx.RemoteProtocolError('private provider address/key')


class SpeechAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name) / 'data'
        self.path = self.data / 'speech-admission.json'
        self.calls = []
        self.provider_reply = lambda request: httpx.Response(200, content=b'complete-audio')
        environment = patch.dict(os.environ, {'CONVO_DATA_DIR': str(self.data), 'CONVO_SIDECAR_TOKEN': TOKEN})
        environment.start()
        self.addCleanup(environment.stop)
        self.original_client = httpx.AsyncClient
        self.transport = httpx.MockTransport(self.provider)
        clients = patch.object(httpx, 'AsyncClient', side_effect=lambda **kwargs: self.original_client(**{**kwargs, 'transport': self.transport}))
        clients.start()
        self.addCleanup(clients.stop)

    async def provider(self, request):
        self.assertEqual(str(request.url), 'http://localhost:8881/v1/audio/speech')
        # Admission is durably present before the first possible network send.
        record = self.path.read_text()
        self.assertIn('operation_id', json.loads(record))
        self.assertNotIn('private', record)
        self.assertNotIn('localhost', record)
        self.calls.append(request)
        return self.provider_reply(request)

    async def post(self, payload=PAYLOAD, route='/speech'):
        async with self.original_client(transport=httpx.ASGITransport(app=app.app), base_url='http://sidecar') as client:
            return await client.post(route, headers={'Authorization': 'Bearer ' + TOKEN}, json=payload)

    def asgi_request(self, route='/speech', payload=PAYLOAD):
        """An actual receive queue, unlike ASGITransport's completion-only disconnect."""
        incoming, outgoing = asyncio.Queue(), []
        body = json.dumps(payload).encode()
        incoming.put_nowait({'type': 'http.request', 'body': body, 'more_body': False})
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                 'http_version': '1.1', 'method': 'POST', 'scheme': 'http', 'path': route,
                 'raw_path': route.encode(), 'query_string': b'', 'root_path': '',
                 'headers': [(b'host', b'sidecar'), (b'content-type', b'application/json'),
                             (b'authorization', ('Bearer ' + TOKEN).encode()),
                             (b'content-length', str(len(body)).encode())],
                 'server': ('sidecar', 80), 'client': ('127.0.0.1', 12345)}

        async def send(message):
            outgoing.append(message)

        return asyncio.create_task(app.app(scope, incoming.get, send)), incoming, outgoing

    def fresh_admission(self):
        spec = importlib.util.spec_from_file_location('convo_test_restarted_admission', admission.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def assert_uncertain(self, response, expected=502):
        self.assertEqual(response.status_code, expected, response.text)
        self.assertTrue(self.path.is_file())
        self.assertEqual(admission.status()['state'], 'uncertain')
        self.assertTrue(self.fresh_admission().status()['blocked'])
        self.assertNotIn('private', response.text)
        self.assertNotIn('localhost', response.text)
        before = len(self.calls)
        blocked = await self.post()
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(len(self.calls), before)

    async def test_actual_asgi_disconnect_detaches_and_drains_without_overlap_or_late_audio(self):
        delayed = DelayedBody()
        self.provider_reply = lambda request: httpx.Response(200, stream=delayed)
        caller, incoming, outgoing = self.asgi_request()
        await asyncio.wait_for(delayed.started.wait(), 1)
        operation = admission._active[str(self.path)]
        started = time.monotonic()
        incoming.put_nowait({'type': 'http.disconnect'})
        await asyncio.wait_for(caller, 1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(delayed.closed)
        self.assertFalse(operation.task.done())
        self.assertEqual(admission.status()['state'], 'draining')
        # Even a new module/process view cannot claim while the old worker drains.
        self.assertEqual(self.fresh_admission().status()['state'], 'uncertain')
        second = await self.post()
        self.assertEqual(second.status_code, 409)
        self.assertEqual(len(self.calls), 1)
        delayed.release.set()
        await asyncio.wait_for(asyncio.shield(operation.task), 1)
        self.assertTrue(delayed.terminal)
        self.assertTrue(delayed.closed)
        self.assertFalse(self.path.exists())
        output = b''.join(item.get('body', b'') for item in outgoing)
        self.assertNotIn(b'first-audio', output)
        self.assertNotIn(b'late-audio', output)
        self.provider_reply = lambda request: httpx.Response(200, content=b'new-response-only')
        third = await self.post()
        self.assertEqual(third.content, b'new-response-only')
        self.assertEqual(len(self.calls), 2)

    async def test_caller_task_cancel_does_not_cancel_provider_worker(self):
        delayed = DelayedBody()
        self.provider_reply = lambda request: httpx.Response(200, stream=delayed)
        caller = asyncio.create_task(app.speech(PAYLOAD))
        await asyncio.wait_for(delayed.started.wait(), 1)
        operation = admission._active[str(self.path)]
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 1)
        self.assertFalse(delayed.closed)
        self.assertEqual((await self.post()).status_code, 409)
        delayed.release.set()
        await asyncio.wait_for(asyncio.shield(operation.task), 1)
        self.assertFalse(admission.status()['blocked'])
        self.assertEqual(len(self.calls), 1)

    async def test_preview_asgi_disconnect_also_detaches_without_provider_cancel(self):
        delayed = DelayedBody()
        self.provider_reply = lambda request: httpx.Response(200, stream=delayed)
        with patch.object(app.studio, 'freeze', return_value=ROLE):
            caller, incoming, outgoing = self.asgi_request('/preview', {'text': 'hello'})
            await asyncio.wait_for(delayed.started.wait(), 1)
            operation = admission._active[str(self.path)]
            incoming.put_nowait({'type': 'http.disconnect'})
            await asyncio.wait_for(caller, 1)
        self.assertFalse(delayed.closed)
        delayed.release.set()
        await asyncio.wait_for(asyncio.shield(operation.task), 1)
        self.assertFalse(admission.status()['blocked'])

    async def test_transport_timeout_remains_uncertain_across_new_module(self):
        def timeout(request):
            raise httpx.ReadTimeout('private provider address/key', request=request)
        self.provider_reply = timeout
        await self.assert_uncertain(await self.post(), 504)

    async def test_absolute_drain_deadline_preserves_uncertainty(self):
        delayed = DelayedBody()
        self.provider_reply = lambda request: httpx.Response(200, stream=delayed)
        with patch.object(admission, 'DEADLINE_SECONDS', .03):
            response = await self.post()
        self.assertTrue(delayed.closed)
        self.assertFalse(delayed.terminal)
        await self.assert_uncertain(response, 504)

    async def test_incomplete_4xx_is_not_definitive_rejection(self):
        self.provider_reply = lambda request: httpx.Response(409, stream=TruncatedBody())
        await self.assert_uncertain(await self.post())

    async def test_provider_5xx_remains_uncertain(self):
        self.provider_reply = lambda request: httpx.Response(500, content=b'private provider message')
        await self.assert_uncertain(await self.post())

    async def test_oversized_response_remains_uncertain(self):
        self.provider_reply = lambda request: httpx.Response(200, content=b'x' * (16 * 1024**2 + 1))
        await self.assert_uncertain(await self.post())

    async def test_complete_rejection_and_success_release_matching_claim(self):
        self.provider_reply = lambda request: httpx.Response(409, content=b'private provider message')
        rejected = await self.post()
        self.assertEqual(rejected.status_code, 409)
        self.assertFalse(admission.status()['blocked'])
        self.assertNotIn('private', rejected.text)
        self.provider_reply = lambda request: httpx.Response(200, content=b'complete-audio')
        success = await self.post()
        self.assertEqual(success.status_code, 200)
        self.assertEqual(success.content, b'complete-audio')
        self.assertIn('x-convo-speech-operation', success.headers)
        self.assertGreaterEqual(int(success.headers['x-convo-speech-elapsed-ms']), 0)
        self.assertFalse(self.path.exists())

    async def test_missing_configuration_validation_and_health_do_not_write_or_dispatch(self):
        self.assertEqual(admission.status()['state'], 'idle')
        await app.health()
        self.assertFalse(self.data.exists())
        invalid = await self.post({'role': ROLE, 'text': ''})
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(self.data.exists())
        with patch.dict(os.environ, {'CONVO_DATA_DIR': ''}):
            unavailable = await self.post()
            self.assertEqual(unavailable.status_code, 503)
            self.assertEqual((await app.health())['speech_admission']['state'], 'configuration_required')
        self.assertFalse(self.data.exists())
        self.assertEqual(self.calls, [])

    async def test_corrupt_persisted_record_never_dispatches_or_auto_repairs(self):
        self.data.mkdir()
        self.path.write_bytes(b'{partial-record')
        before = self.path.read_bytes()
        response = await self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [])

    async def test_worker_shutdown_cancellation_leaves_durable_unknown(self):
        delayed = DelayedBody()
        self.provider_reply = lambda request: httpx.Response(200, stream=delayed)
        caller = asyncio.create_task(app.speech(PAYLOAD))
        await asyncio.wait_for(delayed.started.wait(), 1)
        operation = admission._active[str(self.path)]
        operation.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(self.path.exists())
        self.assertEqual(self.fresh_admission().status()['state'], 'uncertain')
        self.assertEqual((await self.post()).status_code, 409)
        self.assertEqual(len(self.calls), 1)

    async def test_simultaneous_callers_admit_only_one_provider_operation(self):
        delayed = DelayedBody()
        self.provider_reply = lambda request: httpx.Response(200, stream=delayed)
        callers = [asyncio.create_task(self.post()) for _ in range(8)]
        await asyncio.wait_for(delayed.started.wait(), 1)
        for _ in range(100):
            if sum(task.done() for task in callers) == 7:
                break
            await asyncio.sleep(.001)
        self.assertEqual(sum(task.done() for task in callers), 7)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(task.result().status_code == 409 for task in callers if task.done()))
        delayed.release.set()
        results = await asyncio.wait_for(asyncio.gather(*callers), 1)
        self.assertEqual(sum(result.status_code == 200 for result in results), 1)

    async def test_changed_record_owner_is_never_cleared_by_old_completion(self):
        delayed = DelayedBody()
        self.provider_reply = lambda request: httpx.Response(200, stream=delayed)
        caller = asyncio.create_task(self.post())
        await asyncio.wait_for(delayed.started.wait(), 1)
        replacement = {'version': 1, 'operation_id': '12345678-1234-1234-1234-123456789abc', 'started_at': time.time()}
        self.path.write_text(json.dumps(replacement))
        delayed.release.set()
        response = await asyncio.wait_for(caller, 1)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(self.path.read_text()), replacement)
        self.assertTrue(admission.status()['blocked'])

    async def test_local_serialization_failure_never_creates_admission(self):
        with self.assertRaisesRegex(app.ContractError, 'nothing was dispatched'):
            await app.speech({'role': {**ROLE, 'voice': object()}, 'text': 'hello'})
        self.assertFalse(self.data.exists())
        self.assertEqual(self.calls, [])

    async def test_exclusive_file_claim_closes_status_check_race(self):
        delayed = DelayedBody()
        self.provider_reply = lambda request: httpx.Response(200, stream=delayed)
        caller = asyncio.create_task(app.speech(PAYLOAD))
        await asyncio.wait_for(delayed.started.wait(), 1)
        fresh = self.fresh_admission()
        # Simulate two processes both having observed idle before either claim.
        with patch.object(fresh, 'status', side_effect=[{'state': 'idle', 'blocked': False}, admission.status()]):
            with self.assertRaises(fresh.SpeechAdmissionError) as error:
                fresh.start(lambda: app.speech(PAYLOAD))
        self.assertEqual(error.exception.status_code, 409)
        delayed.release.set()
        await asyncio.wait_for(caller, 1)
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
