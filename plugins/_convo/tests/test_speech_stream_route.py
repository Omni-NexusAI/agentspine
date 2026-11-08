"""Guarded PCM wire contract and real ASGI detach behavior; mock HTTP only."""
import asyncio
import base64
import hashlib
import json
import time
import unittest
from unittest.mock import patch

import httpx

from plugins._convo.sidecar import app, speech_admission as admission, speech_stream
from plugins._convo.helpers import convo_transport
from plugins._convo.tests import test_speech_admission as admission_tests
from plugins._convo.tests.test_speech_stream import HEADERS, ROLE


class PulsedBody(httpx.AsyncByteStream):
    def __init__(self, *, error=None):
        self.release = asyncio.Event()
        self.error, self.closed, self.terminal = error, False, False

    async def __aiter__(self):
        yield b'\x01\x02\x03\x04'
        await self.release.wait()
        if self.error:
            raise self.error
        yield b'\x05\x06'
        self.terminal = True

    async def aclose(self):
        self.closed = True


class ManyChunks(httpx.AsyncByteStream):
    def __init__(self):
        self.closed, self.terminal = False, False

    async def __aiter__(self):
        for _ in range(40):
            yield b'\x01\x02' * 4096
        self.terminal = True

    async def aclose(self):
        self.closed = True


class GuardedStreamTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only the existing mock-provider/temp-storage harness, not its tests.
    setUp = admission_tests.SpeechAdmissionTests.setUp
    provider = admission_tests.SpeechAdmissionTests.provider
    fresh_admission = admission_tests.SpeechAdmissionTests.fresh_admission

    async def post(self, role=ROLE, *, route='/speech-stream', authorized=True):
        async with self.original_client(transport=httpx.ASGITransport(app=app.app), base_url='http://sidecar') as client:
            headers = {'Authorization': 'Bearer test-only-never-real-speech-token'} if authorized else {}
            return await client.post(route, headers=headers, json={'role': role, 'text': 'private test phrase'})

    def asgi_stream(self, spec_version='2.4'):
        incoming, outgoing, audio_received = asyncio.Queue(), [], asyncio.Event()
        body = json.dumps({'role': ROLE, 'text': 'hello'}).encode()
        incoming.put_nowait({'type': 'http.request', 'body': body, 'more_body': False})
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': spec_version},
                 'http_version': '1.1', 'method': 'POST', 'scheme': 'http', 'path': '/speech-stream',
                 'raw_path': b'/speech-stream', 'query_string': b'', 'root_path': '',
                 'headers': [(b'host', b'sidecar'), (b'content-type', b'application/json'),
                             (b'authorization', b'Bearer test-only-never-real-speech-token'),
                             (b'content-length', str(len(body)).encode())],
                 'server': ('sidecar', 80), 'client': ('127.0.0.1', 12345)}

        async def send(message):
            outgoing.append(message)
            for line in message.get('body', b'').splitlines():
                frame = json.loads(line)
                if frame['type'] == 'speech.chunk':
                    audio_received.set()
                if frame['type'] == 'speech.done':
                    self.assertFalse(self.path.exists(), 'Success frame preceded durable admission release')

        caller = asyncio.create_task(app.app(scope, incoming.get, send))
        return caller, incoming, outgoing, audio_received

    def frames(self, outgoing):
        raw = b''.join(item.get('body', b'') for item in outgoing)
        return [json.loads(line) for line in raw.splitlines()]

    def assert_completion(self, frames, expected):
        self.assertEqual(frames[0]['type'], 'speech.start')
        self.assertEqual(frames[0]['sample_rate'], 24000)
        self.assertEqual(frames[0]['format'], 'pcm_s16le')
        self.assertEqual(frames[0]['channels'], 1)
        self.assertEqual(frames[-1]['type'], 'speech.done')
        chunks = frames[1:-1]
        self.assertTrue(all(frame['type'] == 'speech.chunk' for frame in chunks))
        self.assertEqual([frame['seq'] for frame in chunks], list(range(len(chunks))))
        audio = b''.join(base64.b64decode(frame['audio'], validate=True) for frame in chunks)
        self.assertEqual(audio, expected)
        self.assertEqual(frames[-1]['bytes'], len(audio))
        self.assertEqual(frames[-1]['chunks'], len(chunks))
        self.assertEqual(frames[-1]['sha256'], hashlib.sha256(audio).hexdigest())
        self.assertGreaterEqual(frames[-1]['elapsed_ms'], 0)
        self.assertEqual(len({frame['operation_id'] for frame in frames}), 1)

    async def test_audio_arrives_before_eof_and_done_follows_guard_release_with_hash(self):
        upstream = PulsedBody()
        self.provider_reply = lambda request: httpx.Response(200, headers=HEADERS, stream=upstream)
        caller, incoming, outgoing, audio = self.asgi_stream()
        await asyncio.wait_for(audio.wait(), 1)
        self.assertFalse(upstream.terminal)
        self.assertTrue(self.path.exists())
        self.assertNotIn('speech.done', [frame['type'] for frame in self.frames(outgoing)])
        upstream.release.set()
        await asyncio.wait_for(caller, 1)
        self.assert_completion(self.frames(outgoing), b'\x01\x02\x03\x04\x05\x06')
        start = next(item for item in outgoing if item['type'] == 'http.response.start')
        headers = dict(start['headers'])
        self.assertEqual(headers[b'x-convo-speech-protocol'], b'pcm16-ndjson-v1')
        self.assertEqual(headers[b'content-type'], b'application/x-ndjson')
        self.assertNotIn(b'content-length', headers)
        self.assertTrue(upstream.closed)

    async def test_disconnect_detaches_drains_and_blocks_both_stream_and_buffered_calls(self):
        upstream = PulsedBody()
        self.provider_reply = lambda request: httpx.Response(200, headers=HEADERS, stream=upstream)
        caller, incoming, outgoing, audio = self.asgi_stream()
        await asyncio.wait_for(audio.wait(), 1)
        operation = admission._active[str(self.path)]
        started = time.monotonic()
        incoming.put_nowait({'type': 'http.disconnect'})
        await asyncio.wait_for(caller, 1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(upstream.closed)
        self.assertTrue(operation.detached)
        self.assertEqual((await self.post()).status_code, 409)
        self.assertEqual((await self.post(route='/speech')).status_code, 409)
        self.assertEqual(len(self.calls), 1)
        count = len(outgoing)
        upstream.release.set()
        await asyncio.wait_for(asyncio.shield(operation.task), 1)
        self.assertTrue(upstream.terminal)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(outgoing), count)
        self.assertNotIn('speech.done', [frame['type'] for frame in self.frames(outgoing)])

    async def test_asgi_task_cancel_does_not_cancel_upstream(self):
        upstream = PulsedBody()
        self.provider_reply = lambda request: httpx.Response(200, headers=HEADERS, stream=upstream)
        caller, incoming, outgoing, audio = self.asgi_stream()
        await asyncio.wait_for(audio.wait(), 1)
        operation = admission._active[str(self.path)]
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 1)
        self.assertTrue(operation.detached)
        self.assertFalse(operation.task.done())
        self.assertFalse(upstream.closed)
        upstream.release.set()
        await asyncio.wait_for(asyncio.shield(operation.task), 1)
        self.assertFalse(admission.status()['blocked'])

    async def test_failure_after_partial_audio_has_error_never_done_and_persists(self):
        upstream = PulsedBody(error=httpx.RemoteProtocolError('private provider/key'))
        self.provider_reply = lambda request: httpx.Response(200, headers=HEADERS, stream=upstream)
        caller, incoming, outgoing, audio = self.asgi_stream()
        await asyncio.wait_for(audio.wait(), 1)
        upstream.release.set()
        await asyncio.wait_for(caller, 1)
        frames = self.frames(outgoing)
        self.assertEqual(frames[-1]['type'], 'speech.error')
        self.assertEqual(frames[-1]['detail']['state'], 'uncertain')
        self.assertNotIn('speech.done', [frame['type'] for frame in frames])
        self.assertNotIn('private', json.dumps(frames))
        self.assertTrue(self.fresh_admission().status()['blocked'])
        self.assertEqual((await self.post()).status_code, 409)
        self.assertEqual(len(self.calls), 1)

    async def test_invalid_provider_headers_do_not_emit_start_or_audio(self):
        self.provider_reply = lambda request: httpx.Response(200, headers={'content-type': 'audio/wav'}, content=b'\0\0')
        response = await self.post()
        frames = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual([frame['type'] for frame in frames], ['speech.error'])
        self.assertTrue(self.path.exists())

    async def test_complete_4xx_rejection_is_explicit_failure_but_releases_admission(self):
        self.provider_reply = lambda request: httpx.Response(409, content=b'private provider message')
        response = await self.post()
        frame = json.loads(response.text)
        self.assertEqual(frame['type'], 'speech.error')
        self.assertEqual(frame['detail']['state'], 'rejected')
        self.assertFalse(frame['detail']['blocked'])
        self.assertNotIn('private', response.text)
        self.assertFalse(self.path.exists())

    async def test_truncated_4xx_rejection_preserves_uncertainty(self):
        self.provider_reply = lambda request: httpx.Response(409, stream=admission_tests.TruncatedBody())
        response = await self.post()
        self.assertEqual(json.loads(response.text)['detail']['state'], 'uncertain')
        self.assertTrue(self.path.exists())

    async def test_missing_experimental_capability_or_auth_never_dispatches(self):
        response = await self.post({**ROLE, 'capabilities': {'audio_output': True}})
        self.assertEqual(response.status_code, 400)
        response = await self.post(authorized=False)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.data.exists())
        self.assertEqual(self.calls, [])

    async def test_detach_unblocks_full_bounded_queue_and_drains_every_provider_chunk(self):
        upstream = ManyChunks()
        self.provider_reply = lambda request: httpx.Response(200, headers=HEADERS, stream=upstream)
        stream = speech_stream.AdmittedPCM(speech_stream.prepare_request(ROLE, 'hello'))
        async with asyncio.timeout(1):
            while not stream.queue.full():
                await asyncio.sleep(0)
        self.assertEqual(stream.queue.qsize(), speech_stream.QUEUE_FRAMES)
        self.assertFalse(upstream.terminal)
        self.assertTrue(self.path.exists())
        stream.detach()
        summary = await asyncio.wait_for(asyncio.shield(stream.operation.task), 1)
        self.assertTrue(upstream.terminal)
        self.assertTrue(upstream.closed)
        self.assertEqual(summary['chunks'], 40)
        self.assertEqual(summary['bytes'], 40 * 8192)
        self.assertEqual(summary['sha256'], hashlib.sha256(b'\x01\x02' * (40 * 4096)).hexdigest())
        self.assertEqual(stream.queue.qsize(), 0)
        self.assertFalse(self.path.exists())

    async def test_drain_deadline_emits_error_and_keeps_durable_record(self):
        upstream = PulsedBody()
        self.provider_reply = lambda request: httpx.Response(200, headers=HEADERS, stream=upstream)
        with patch.object(admission, 'DEADLINE_SECONDS', .03):
            response = await self.post()
        frames = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(frames[-1]['type'], 'speech.error')
        self.assertIn('timeout', frames[-1]['detail']['reason'])
        self.assertTrue(self.path.exists())
        self.assertTrue(upstream.closed)
        self.assertFalse(upstream.terminal)

    async def test_legacy_asgi_disconnect_also_detaches_without_provider_cancel(self):
        upstream = PulsedBody()
        self.provider_reply = lambda request: httpx.Response(200, headers=HEADERS, stream=upstream)
        caller, incoming, outgoing, audio = self.asgi_stream('2.3')
        await asyncio.wait_for(audio.wait(), 1)
        operation = admission._active[str(self.path)]
        incoming.put_nowait({'type': 'http.disconnect'})
        await asyncio.wait_for(caller, 1)
        self.assertFalse(upstream.closed)
        self.assertTrue(operation.detached)
        upstream.release.set()
        await asyncio.wait_for(asyncio.shield(operation.task), 1)
        self.assertFalse(self.path.exists())

    async def test_actual_route_frames_pass_host_parser_and_missing_done_is_rejected(self):
        self.provider_reply = lambda request: httpx.Response(200, headers=HEADERS, content=b'\x01\x02' * 6000)
        response = await self.post()
        chunks = [chunk async for chunk in convo_transport.framed_pcm(response)]
        self.assertEqual(b''.join(chunk['audio'] for chunk in chunks), b'\x01\x02' * 6000)
        frames = response.content.splitlines(keepends=True)
        truncated = httpx.Response(200, headers=response.headers, content=b''.join(frames[:-1]))
        with self.assertRaisesRegex(convo_transport.ContractError, 'without verified completion'):
            _ = [chunk async for chunk in convo_transport.framed_pcm(truncated)]

    async def test_detached_stream_never_replays_queued_audio_or_completion(self):
        upstream = ManyChunks()
        self.provider_reply = lambda request: httpx.Response(200, headers=HEADERS, stream=upstream)
        stream = speech_stream.AdmittedPCM(speech_stream.prepare_request(ROLE, 'hello'))
        async with asyncio.timeout(1):
            while not stream.queue.full():
                await asyncio.sleep(0)
        stream.detach()
        self.assertEqual([frame async for frame in stream.frames()], [])
        await asyncio.wait_for(asyncio.shield(stream.operation.task), 1)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
