"""Only configured sidecars receive audio; browsers cannot select endpoints."""
from __future__ import annotations

import json
import base64
import hashlib

import httpx

from .convo_contract import Capabilities, ContractError, SpeechBusy, credentials, endpoint


PCM_CHUNK_BYTES = 8192
PCM_MAX_BYTES = 16 * 1024 * 1024


def pcm_metadata(headers):
    """Accept only explicitly described little-endian PCM16 mono, never WAV bytes."""
    if headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'audio/pcm':
        raise ContractError('Streaming speech must return audio/pcm')
    if (headers.get('x-tts-codec') != 'pcm_s16le'
            or headers.get('x-tts-bits-per-sample') != '16'
            or headers.get('x-tts-channels') != '1'):
        raise ContractError('Streaming speech must explicitly declare PCM16 little-endian mono')
    rate = headers.get('x-tts-sample-rate', '')
    if not isinstance(rate, str) or not rate.isascii() or not rate.isdigit() or int(rate) not in {8000, 16000, 22050, 24000, 32000, 44100, 48000}:
        raise ContractError('Streaming speech sample rate is missing or unsupported')
    return {'sample_rate': int(rate), 'format': 'pcm_s16le', 'channels': 1}


async def pcm_chunks(response):
    """Align split samples without accumulating an utterance; reject incomplete EOF."""
    metadata = pcm_metadata(response.headers)
    pending, size = b'', 0
    async for part in response.aiter_bytes():
        size += len(part)
        if size > PCM_MAX_BYTES:
            raise ContractError('Streaming speech response too large')
        # Avoid copying an arbitrarily large provider chunk into pending storage.
        for offset in range(0, len(part), PCM_CHUNK_BYTES):
            data = pending + part[offset:offset + PCM_CHUNK_BYTES]
            end = len(data) - len(data) % 2
            pending = data[end:]
            if end:
                yield {'audio': data[:end], **metadata}
    if pending:
        raise ContractError('Streaming speech ended with an incomplete PCM16 sample')
    if size == 0:
        raise ContractError('Streaming speech returned no audio')


async def framed_pcm(response):
    """Verify the sidecar's terminal receipt; HTTP EOF alone is not success."""
    if (response.headers.get('content-type', '').split(';', 1)[0] != 'application/x-ndjson'
            or response.headers.get('x-convo-speech-protocol') != 'pcm16-ndjson-v1'):
        raise ContractError('Unsupported Convo speech stream protocol')
    metadata = None
    operation = None
    count = size = 0
    digest = hashlib.sha256()
    complete = False
    # Bound framing before decoding; a hostile service must not allocate a huge
    # aiter_lines buffer. Provider bytes remain separately bounded below.
    pending = b''
    async for part in response.aiter_bytes():
        for offset in range(0, len(part), 16384):
            pending += part[offset:offset + 16384]
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                if not line.strip():
                    continue
                if complete or len(line) > 16384:
                    raise ContractError('Invalid Convo speech stream framing')
                try:
                    frame = json.loads(line)
                except (ValueError, UnicodeError) as error:
                    raise ContractError('Invalid Convo speech stream frame') from error
                if not isinstance(frame, dict):
                    raise ContractError('Invalid Convo speech stream frame')
                kind = frame.get('type')
                if kind == 'speech.start':
                    if metadata is not None or not isinstance(frame.get('operation_id'), str) or not 0 < len(frame['operation_id']) <= 80:
                        raise ContractError('Invalid Convo speech stream start')
                    operation = frame['operation_id']
                    rate = frame.get('sample_rate')
                    if type(rate) is not int or rate not in {8000, 16000, 22050, 24000, 32000, 44100, 48000} or frame.get('format') != 'pcm_s16le' or type(frame.get('channels')) is not int or frame['channels'] != 1:
                        raise ContractError('Invalid Convo speech stream metadata')
                    metadata = {'sample_rate': rate, 'format': 'pcm_s16le', 'channels': 1}
                elif frame.get('operation_id') != operation or metadata is None:
                    raise ContractError('Convo speech stream operation mismatch')
                elif kind == 'speech.chunk':
                    if type(frame.get('seq')) is not int or frame['seq'] != count or not isinstance(frame.get('audio'), str) or len(frame['audio']) > 11000:
                        raise ContractError('Invalid Convo speech stream sequence')
                    try:
                        audio = base64.b64decode(frame['audio'], validate=True)
                    except ValueError as error:
                        raise ContractError('Invalid Convo speech stream audio') from error
                    if not audio or len(audio) % 2 or len(audio) > PCM_CHUNK_BYTES:
                        raise ContractError('Invalid Convo PCM chunk')
                    size += len(audio)
                    if size > PCM_MAX_BYTES:
                        raise ContractError('Convo speech stream exceeded size limit')
                    count += 1
                    digest.update(audio)
                    yield {'audio': audio, **metadata}
                elif kind == 'speech.done':
                    if not count or type(frame.get('chunks')) is not int or type(frame.get('bytes')) is not int or frame['chunks'] != count or frame['bytes'] != size or frame.get('sha256') != digest.hexdigest():
                        raise ContractError('Invalid Convo speech completion receipt')
                    complete = True
                else:
                    raise ContractError('Convo speech stream did not complete successfully')
            if len(pending) > 16384:
                raise ContractError('Convo speech stream frame too large')
    if pending.strip() or not complete:
        raise ContractError('Convo speech stream ended without verified completion')


async def speech_status(response):
    """raise_for_status, except that a provider still finishing a request it tracks is SpeechBusy (wait), not a failure.

    Only the sidecar's own `running`/`draining` admission states mean that. `uncertain` needs the operator's recovery
    and a rejection is final, so both stay ordinary errors."""
    if response.status_code == 409:
        await response.aread()
        try:
            detail = response.json().get('detail')
        except (ValueError, AttributeError):
            detail = None
        if isinstance(detail, dict) and detail.get('state') in {'running', 'draining'}:
            raise SpeechBusy(detail['state'])
    response.raise_for_status()


class Sidecar:
    def __init__(self, config):
        self.url = endpoint(config)
        self.headers = credentials(config)
        if not self.headers:
            raise ContractError("Set CONVO_SIDECAR_TOKEN in both host and sidecar before starting Convo")

    async def policy(self, payload):
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = await client.post(self.url + "/policy", json=payload, headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def health(self):
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = await client.get(self.url + '/health', headers=self.headers)
            response.raise_for_status()
            data = response.json()
            if data.get('service') != 'convo' or data.get('protocol') != 1:
                raise ContractError('Incompatible Convo sidecar')
            return data

    async def studio(self, payload):
        async with httpx.AsyncClient(timeout=190, follow_redirects=False, trust_env=False) as client:
            route = '/preview' if payload.get('operation') == 'preview' else '/studio'
            response = await client.post(self.url + route, json=payload.get('payload') if route == '/preview' else payload, headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def freeze_speech(self, role):
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.post(self.url + '/freeze-speech', json=role, headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def answer(self, payload):
        async with httpx.AsyncClient(timeout=httpx.Timeout(65, connect=5), follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", self.url + "/answer", json=payload, headers=self.headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        yield json.loads(line[5:])

    async def speech(self, role, text):
        async with httpx.AsyncClient(timeout=35, follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", self.url + "/speech", json={"role": role, "text": text}, headers=self.headers) as response:
                await speech_status(response)
                parts, size = [], 0
                async for part in response.aiter_bytes():
                    size += len(part)
                    if size > 16 * 1024 * 1024:
                        raise ContractError("Speech response too large")
                    parts.append(part)
                return b"".join(parts)

    async def speech_stream(self, role, text):
        """Experimental explicit PCM path; caller owns epoch checks and admission."""
        caps = Capabilities.parse(role.get('capabilities', {}))
        if not caps.native_pcm or not caps.audio_output:
            raise ContractError('Native PCM requires explicit experimental audio-output capability')
        async with httpx.AsyncClient(timeout=35, follow_redirects=False, trust_env=False) as client:
            async with client.stream('POST', self.url + '/speech-stream', json={'role': role, 'text': text}, headers=self.headers) as response:
                await speech_status(response)
                async for chunk in framed_pcm(response):
                    yield chunk
