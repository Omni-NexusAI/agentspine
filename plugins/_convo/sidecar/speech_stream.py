"""Experimental, durably admitted PCM transport; no activation or model lifecycle.

Original Convo adapter code, not copied engine code. Wire/header contract is from
the attributed audiocpp/supervisor.py native-incremental PCM response. The local
speech-to-speech FasterQwen language patch also documents stream=True,
response_format='pcm', and language, but headerless output is deliberately not
accepted without an independently verified format contract. No model name is
evidence of streaming or duplex support.

The admission owner must drain this iterator inside its independent worker and
withhold late chunks after user cancellation. Closing this HTTP stream is NOT
proof that upstream synthesis stopped; retain uncertainty on cancellation/error.
Only complete iterator exhaustion confirms a validated transport EOF.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json

import httpx

from ..helpers import convo_net
from ..helpers.convo_contract import Capabilities, ContractError, credentials, endpoint
from ..helpers.convo_transport import pcm_chunks
from . import speech_admission


PROTOCOL = 'pcm16-ndjson-v1'
QUEUE_FRAMES = 8
REJECTION_BYTES = 64 * 1024


def speech_request(role, text):
    caps = Capabilities.parse(role.get('capabilities', {}))
    if not caps.native_pcm or not caps.audio_output:
        raise ContractError('Native PCM requires explicit experimental audio-output capability')
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ContractError('Invalid speech phrase')
    if not isinstance(role.get('model'), str) or not role['model'].strip():
        raise ContractError('Select an explicit speech model')
    body = {'model': role['model'], 'voice': role.get('voice', ''), 'input': text,
            'response_format': 'pcm', 'stream': True}
    if role.get('language'):
        if not isinstance(role['language'], str) or len(role['language']) > 40:
            raise ContractError('Invalid assistant language')
        body['language'] = role['language']
    if role.get('backend') != 'managed_audio' and role.get('reply_tuning'):
        value = role['reply_tuning']
        if not isinstance(value, dict) or not isinstance(value.get('overrides'), dict) or len(json.dumps(value)) > 8192:
            raise ContractError('Invalid synthesis tuning snapshot')
        body['tuning'] = copy.deepcopy(value)          # one reply's seed and the user's synthesis choice (your audio.cpp)
    if role.get('backend') == 'managed_audio':
        for key in ('clone_snapshot', 'expected_engine_epoch', 'expected_supervisor_instance_id', 'tuning'):
            if key not in role or role[key] is None or role[key] == '':
                raise ContractError('Missing immutable managed speech snapshot: ' + key)
            body[key] = copy.deepcopy(role[key])
        if not role['clone_snapshot'] or not role['tuning']:
            raise ContractError('Missing immutable managed speech clone/tuning snapshot')
    if role.get('instructions'):
        if not caps.instruction_control:
            raise ContractError('This variant does not support instruction control')
        body['instructions'] = role['instructions']
    return endpoint(role), credentials(role), body


def prepare_request(role, text):
    """Validate and freeze serialization before durable dispatch admission."""
    url, headers, body = speech_request(role, text)
    try:
        return httpx.Request('POST', url + '/audio/speech', json=body, headers=headers)
    except (ValueError, TypeError, httpx.InvalidURL):
        raise ContractError('Invalid streaming speech request; nothing was dispatched') from None


async def provider_pcm(role=None, text=None, *, prepared=None):
    """Yield bounded PCM metadata/audio chunks, keeping response ownership local."""
    prepared = prepared or prepare_request(role, text)
    try:
        async with convo_net.client(httpx.Timeout(35, connect=5)) as client:
            async with client.stream('POST', prepared.url, content=prepared.content, headers=prepared.headers) as response:
                if not 200 <= response.status_code < 300:
                    # A complete bounded error body decides: any 4xx, and the server's own "generation already finished"
                    # 502, are terminal (the speech lock is released and the refusal reported); anything else stays unknown.
                    body = b''
                    async for part in response.aiter_bytes():
                        body += part
                        if len(body) > REJECTION_BYTES:
                            raise speech_admission.UncertainOutcome('response_limit')
                    speech_admission.raise_for_provider(response.status_code, body)
                async for chunk in pcm_chunks(response):
                    yield chunk
    except httpx.TimeoutException:
        raise speech_admission.UncertainOutcome('timeout') from None


# A phrase this short ends before the speech server has two native PCM pieces to stream (its 502 "native-stream-not-incremental":
# a one-word reply, under about half a second): it is synthesised whole instead, the same request with buffered WAV output.
SHORT_WORDS = 2
SHORT_CHARS = 24
WHOLE_BYTES = 16 * 1024 * 1024


def is_short(text):
    return isinstance(text, str) and len(text.split()) <= SHORT_WORDS and len(text.strip()) <= SHORT_CHARS


def whole_request(prepared):
    """The same frozen request for the whole phrase: buffered WAV instead of the native PCM stream (nothing else changes)."""
    body = json.loads(prepared.content)
    body.update(response_format='wav', stream=False)
    headers = {key: value for key, value in prepared.headers.items() if key.lower() not in ('content-length', 'content-type')}
    return httpx.Request('POST', prepared.url, json=body, headers=headers)


def wav_pcm16(data):
    """(sample_rate, PCM16 little-endian mono bytes) from a complete WAV: 16-bit integer or 32-bit float mono only."""
    import struct
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise speech_admission.DefinitiveRejection(502, 'whole-phrase-format')
    offset, fmt, audio = 12, None, None
    while offset + 8 <= len(data):
        kind, size = data[offset:offset + 4], struct.unpack('<I', data[offset + 4:offset + 8])[0]
        chunk = data[offset + 8:offset + 8 + size]
        if kind == b'fmt ' and len(chunk) >= 16:
            fmt = struct.unpack('<HHIIHH', chunk[:16])
            if fmt[0] == 0xFFFE and len(chunk) >= 26:
                fmt = (struct.unpack('<H', chunk[24:26])[0],) + fmt[1:]
        elif kind == b'data':
            audio = chunk
            break
        offset += 8 + size + (size & 1)
    if fmt is None or audio is None or fmt[1] != 1 or fmt[2] not in {8000, 16000, 22050, 24000, 32000, 44100, 48000}:
        raise speech_admission.DefinitiveRejection(502, 'whole-phrase-format')
    if fmt[0] == 1 and fmt[5] == 16:
        return fmt[2], audio[:len(audio) - len(audio) % 2]
    if fmt[0] == 3 and fmt[5] == 32:
        count = len(audio) // 4
        samples = struct.unpack('<%df' % count, audio[:count * 4])
        return fmt[2], struct.pack('<%dh' % count, *(max(-32768, min(32767, int(round(value * 32767)))) for value in samples))
    raise speech_admission.DefinitiveRejection(502, 'whole-phrase-format')


async def provider_whole(prepared):
    """The whole phrase at once, as the same PCM chunks the stream would have given (bounded, every outcome classified alike)."""
    try:
        async with convo_net.client(httpx.Timeout(35, connect=5)) as client:
            async with client.stream('POST', prepared.url, content=prepared.content, headers=prepared.headers) as response:
                body = b''
                async for part in response.aiter_bytes():
                    body += part
                    if len(body) > (WHOLE_BYTES if 200 <= response.status_code < 300 else REJECTION_BYTES):
                        raise speech_admission.UncertainOutcome('response_limit')
                if not 200 <= response.status_code < 300:
                    speech_admission.raise_for_provider(response.status_code, body)
    except httpx.TimeoutException:
        raise speech_admission.UncertainOutcome('timeout') from None
    rate, audio = wav_pcm16(body)
    if not audio:
        raise speech_admission.DefinitiveRejection(502, 'whole-phrase-format')
    step = 8192
    for offset in range(0, len(audio), step):
        yield {'audio': audio[offset:offset + step], 'sample_rate': rate, 'format': 'pcm_s16le', 'channels': 1}


class AdmittedPCM:
    """One independent producer; only its bounded delivery queue is disposable."""
    def __init__(self, prepared):
        self.prepared = prepared
        self.counters, self.path = [0, 0, hashlib.sha256()], ''
        self.queue = asyncio.Queue(maxsize=QUEUE_FRAMES)
        self.operation = speech_admission.start(self._produce)

    def detach(self):
        self.operation.detach()
        self._discard()

    def _discard(self):
        while not self.queue.empty():
            self.queue.get_nowait()

    async def _publish(self, frame):
        if self.operation.detached:
            return
        if not self.queue.full():
            self.queue.put_nowait(frame)
            return
        put = asyncio.create_task(self.queue.put(frame))
        detached = asyncio.create_task(self.operation.detached_event.wait())
        try:
            await asyncio.wait((put, detached), return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Cancel only queue bookkeeping, never the provider producer.
            put.cancel()
            detached.cancel()
            await asyncio.gather(put, detached, return_exceptions=True)
            if self.operation.detached:
                self._discard()

    async def _produce(self):
        """The phrase streamed, or whole when it is very short. A stream the server refuses as "not incremental" before any audio
        was published (it finished generating first, so it is idle) is asked once more, whole, inside the same admission."""
        self.counters = [0, 0, hashlib.sha256()]
        try:
            text = json.loads(self.prepared.content).get('input')
        except (ValueError, AttributeError):
            text = None
        if is_short(text):
            self.path = 'whole'
            await self._relay(provider_whole(whole_request(self.prepared)))
        else:
            self.path = 'stream'
            try:
                await self._relay(provider_pcm(prepared=self.prepared))
            except speech_admission.DefinitiveRejection as error:
                if self.counters[0] or error.reason != 'native-stream-not-incremental':
                    raise
                self.path = 'whole-after-stream'
                await self._relay(provider_whole(whole_request(self.prepared)))
        count, size, digest = self.counters
        return {'chunks': count, 'bytes': size, 'sha256': digest.hexdigest()}

    async def _relay(self, source):
        count, size, digest = self.counters
        async for chunk in source:
            if not count:
                await self._publish({'type': 'speech.start', 'format': chunk['format'],
                                     'sample_rate': chunk['sample_rate'], 'channels': chunk['channels']})
            audio = chunk['audio']
            digest.update(audio)
            size += len(audio)
            await self._publish({'type': 'speech.chunk', 'seq': count,
                                 'audio': base64.b64encode(audio).decode('ascii')})
            count += 1
            self.counters[0], self.counters[1] = count, size

    def _encode(self, frame):
        return (json.dumps({**frame, 'operation_id': self.operation.operation_id},
                           separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')

    async def frames(self):
        try:
            while True:
                if self.operation.detached:
                    return
                task = self.operation.task
                # On a known failure, discard queued PCM and notify promptly.
                if task.done() and (task.cancelled() or task.exception() is not None):
                    self._discard()
                    break
                if not self.queue.empty():
                    yield self._encode(self.queue.get_nowait())
                    continue
                if task.done():
                    break
                take = asyncio.create_task(self.queue.get())
                try:
                    done, _ = await asyncio.wait((take, task), return_when=asyncio.FIRST_COMPLETED)
                    if take in done and not self.operation.detached:
                        yield self._encode(take.result())
                finally:
                    take.cancel()
                    await asyncio.gather(take, return_exceptions=True)
            # Success is framed only after _run has released durable admission.
            if self.operation.detached:
                return
            if self.operation.task.cancelled():
                detail = self.operation.details('uncertain', 'Speech worker stopped without a terminal outcome', blocked=True)
                yield self._encode({'type': 'speech.error', 'detail': detail})
                return
            try:
                summary = await asyncio.shield(self.operation.task)
            except speech_admission.SpeechAdmissionError as error:
                yield self._encode({'type': 'speech.error', 'detail': error.detail})
                return
            yield self._encode({'type': 'speech.done', **summary,
                                'elapsed_ms': self.operation.details('completed', '', blocked=False)['elapsed_ms']})
        finally:
            self.detach()
