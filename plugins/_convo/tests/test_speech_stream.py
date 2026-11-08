import asyncio
import json
import base64
import hashlib
import unittest
from unittest.mock import patch

import httpx

from plugins._convo.helpers import convo_transport as transport
from plugins._convo.helpers.convo_contract import ContractError
from plugins._convo.sidecar import speech_stream


HEADERS = {'content-type': 'audio/pcm', 'x-tts-codec': 'pcm_s16le',
           'x-tts-sample-rate': '24000', 'x-tts-bits-per-sample': '16', 'x-tts-channels': '1'}
ROLE = {'url': 'http://localhost:8881/v1', 'model': '1.7B-Base', 'voice': 'clone:test',
        'language': 'English', 'capabilities': {'audio_output': True, 'native_pcm': True}}

FRAMED_HEADERS = {'content-type': 'application/x-ndjson', 'x-convo-speech-protocol': 'pcm16-ndjson-v1'}


def frames(audio=b'\0\0', **changes):
    items = [dict(type='speech.start', operation_id='owned', sample_rate=24000, channels=1, format='pcm_s16le'),
             dict(type='speech.chunk', operation_id='owned', seq=0, audio=base64.b64encode(audio).decode()),
             dict(type='speech.done', operation_id='owned', chunks=1, bytes=len(audio), sha256=hashlib.sha256(audio).hexdigest())]
    for index, fields in changes.items():
        items[int(index)].update(fields)
    return [json.dumps(item).encode() + b'\n' for item in items]


def immediate(coroutine):
    # MockTransport and these iterators perform no I/O: avoid Windows loop sockets.
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError('Unexpected I/O suspension')


async def collect(iterator):
    return [chunk async for chunk in iterator]


class Bytes(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts, self.closed = parts, False

    async def __aiter__(self):
        for part in self.parts:
            if isinstance(part, BaseException):
                raise part
            yield part

    async def aclose(self):
        self.closed = True


class SpeechStreamTests(unittest.TestCase):
    def client(self, parts, headers=HEADERS):
        stream = Bytes(parts)
        seen = []
        def handle(request):
            seen.append(request)
            return httpx.Response(200, headers=headers, stream=stream)
        original = httpx.AsyncClient
        mock = httpx.MockTransport(handle)
        patcher = patch.object(httpx, 'AsyncClient', side_effect=lambda **kwargs: original(**{**kwargs, 'transport': mock}))
        return patcher, stream, seen

    def test_split_samples_metadata_bounded_chunks_and_request_fields(self):
        mock, stream, seen = self.client([b'\x01', b'\x02\x03', b'\x04' * 20001])
        with mock:
            chunks = immediate(collect(speech_stream.provider_pcm(ROLE, 'hello')))
        self.assertTrue(stream.closed)
        self.assertEqual(b''.join(chunk['audio'] for chunk in chunks), b'\x01\x02\x03' + b'\x04' * 20001)
        self.assertTrue(all(0 < len(chunk['audio']) <= transport.PCM_CHUNK_BYTES and len(chunk['audio']) % 2 == 0 for chunk in chunks))
        self.assertTrue(all(chunk['sample_rate'] == 24000 and chunk['channels'] == 1 and chunk['format'] == 'pcm_s16le' for chunk in chunks))
        body = json.loads(seen[0].content)
        self.assertEqual((body['stream'], body['response_format'], body['language'], body['model']), (True, 'pcm', 'English', '1.7B-Base'))

    def test_empty_odd_and_oversized_streams_reject_and_close(self):
        for parts, limit in (([], 20), ([b'\x00'], 20), ([b'\x00' * 22], 20)):
            with self.subTest(parts=parts):
                mock, stream, _ = self.client(parts)
                with mock, patch.object(transport, 'PCM_MAX_BYTES', limit), self.assertRaises(ContractError):
                    immediate(collect(speech_stream.provider_pcm(ROLE, 'hello')))
                self.assertTrue(stream.closed)

    def test_malformed_headers_fail_before_audio_release(self):
        for changes in ({'content-type': 'audio/wav'}, {'x-tts-sample-rate': '24000.0'}, {'x-tts-sample-rate': ''},
                        {'x-tts-sample-rate': '999999'}, {'x-tts-channels': '2'}, {'x-tts-codec': 'pcm_f32le'},
                        {'x-tts-bits-per-sample': '24'}):
            mock, stream, _ = self.client([b'\0\0'], {**HEADERS, **changes})
            with mock, self.assertRaises(ContractError):
                immediate(collect(speech_stream.provider_pcm(ROLE, 'hello')))
            self.assertTrue(stream.closed)

    def test_cancellation_propagates_and_closes_transport(self):
        mock, stream, _ = self.client([b'\0\0', asyncio.CancelledError()])
        with mock, self.assertRaises(asyncio.CancelledError):
            immediate(collect(speech_stream.provider_pcm(ROLE, 'hello')))
        self.assertTrue(stream.closed)

    def test_host_stream_uses_distinct_route_and_keeps_buffered_api(self):
        mock, stream, seen = self.client(frames(), FRAMED_HEADERS)
        sidecar = object.__new__(transport.Sidecar)
        sidecar.url, sidecar.headers = 'http://localhost:8090', {'Authorization': 'Bearer test-only'}
        with mock:
            chunks = immediate(collect(sidecar.speech_stream(ROLE, 'hello')))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(seen[0].url.path, '/speech-stream')
        self.assertEqual(seen[0].headers['authorization'], 'Bearer test-only')
        self.assertTrue(callable(sidecar.speech))
        self.assertTrue(stream.closed)

    def test_terminal_receipt_and_sequence_are_required(self):
        corruptions = [frames()[:-1], frames(**{'1': {'seq': 1}}), frames(**{'2': {'sha256': 'wrong'}}),
                       frames(**{'2': {'chunks': 2}}), frames(**{'1': {'operation_id': 'other'}}),
                       frames(**{'2': {'type': 'speech.error'}}), frames() + frames()[:1],
                       [b'x' * 17000], frames(**{'1': {'audio': '!!'}})]
        for parts in corruptions:
            with self.subTest(parts=parts):
                mock, stream, _ = self.client(parts, FRAMED_HEADERS)
                with mock, self.assertRaises(ContractError):
                    immediate(collect(transport.framed_pcm(httpx.Response(200, headers=FRAMED_HEADERS, stream=stream))))

    def test_split_framing_and_raw_pcm_route_cannot_be_confused(self):
        payload = b''.join(frames())
        stream = Bytes([payload[:7], payload[7:31], payload[31:]])
        chunks = immediate(collect(transport.framed_pcm(httpx.Response(200, headers=FRAMED_HEADERS, stream=stream))))
        self.assertEqual(chunks[0]['audio'], b'\0\0')
        with self.assertRaises(ContractError):
            immediate(collect(transport.framed_pcm(httpx.Response(200, headers=HEADERS, stream=Bytes([b'\0\0'])))))

    def test_capability_and_snapshot_checks_do_not_dispatch(self):
        with patch.object(httpx, 'AsyncClient', side_effect=AssertionError('Unexpected provider dispatch')):
            for role in ({**ROLE, 'capabilities': {'audio_output': True}}, {**ROLE, 'backend': 'managed_audio'}, {**ROLE, 'instructions': 'unapproved'}):
                with self.assertRaises(ContractError):
                    immediate(collect(speech_stream.provider_pcm(role, 'hello')))


if __name__ == '__main__':
    unittest.main()
