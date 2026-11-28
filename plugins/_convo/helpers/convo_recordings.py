"""Developer "Record raw and sent": one utterance saved twice, only when the user presses the button in the Sound drawer (developer mode).

`raw` is the microphone before echo protection and the noise gate (a copy of the capture's first input, at the device's rate), `sent`
what Convo sent for that utterance (16 kHz mono PCM16). Both end when the utterance ended, so they are aligned on their ends. The
analysis says, per recording, its level, the stretches where the microphone heard sound but what was sent is silence or floor-level
noise (withheld as echo, or gated), and the clipped start: how much of the first sound the microphone heard is missing from the start
of what was sent. Like the test clips this writes audio to disk, the one exception to "no audio stored": only on the user's press, in
the plugin's own data folder (`data/recordings/`), listed and deletable. Nothing is uploaded anywhere."""
from __future__ import annotations

import array
import base64
import binascii
import io
import json
import math
import os
import re
import secrets
import threading
import time
import wave
from pathlib import Path

MAX_SECONDS = 31            # the browser keeps at most 30 s of raw audio; one second of slack for block rounding
MAX_RECORDINGS = 100
FRAME_S = 0.05              # the analysis frame
SOUND_DB = -50.0            # the microphone heard something (dBFS)
SILENT_DB = -66.0           # what was sent is silence or floor-level comfort noise
ID = re.compile(r'^[0-9]{14}-[0-9a-f]{8}$')
_lock = threading.Lock()


def folder(data):
    return Path(data) / 'recordings'


def _decode(value, label, rates=(8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 96000)):
    """(rate, samples as floats) of a mono PCM16 WAV in base64; ValueError otherwise."""
    if not isinstance(value, str) or not value or len(value) > 16 * 1024 * 1024:
        raise ValueError('The %s audio is missing or too large' % label)
    try:
        raw = base64.b64decode(value, validate=True)
        with wave.open(io.BytesIO(raw)) as reader:
            if reader.getnchannels() != 1 or reader.getsampwidth() != 2 or reader.getframerate() not in rates:
                raise ValueError('The %s audio must be mono 16-bit PCM' % label)
            rate, frames = reader.getframerate(), reader.readframes(reader.getnframes())
    except (binascii.Error, wave.Error, EOFError) as error:
        raise ValueError('The %s audio is not a WAV file' % label) from error
    samples = array.array('h')
    samples.frombytes(frames[:len(frames) // 2 * 2])
    if len(samples) / rate > MAX_SECONDS:
        raise ValueError('The %s audio is longer than %d seconds' % (label, MAX_SECONDS - 1))
    return rate, raw, [value / 32768.0 for value in samples]


def _levels(samples, rate):
    size = max(1, int(rate * FRAME_S))
    out = []
    for start in range(0, len(samples) - size + 1, size):
        frame = samples[start:start + size]
        rms = math.sqrt(sum(value * value for value in frame) / size)
        out.append(round(20 * math.log10(rms), 1) if rms > 1e-10 else -120.0)
    return out


def analyse(raw_rate, raw, sent_rate, sent, pre_roll_s=0.0):
    """Levels and the gaps between the two recordings, aligned on their ends (both stop when the utterance ended)."""
    raw_levels, sent_levels = _levels(raw, raw_rate), _levels(sent, sent_rate)
    shift = len(raw_levels) - len(sent_levels)          # sent frame i is raw frame i + shift
    silenced, run = [], None
    for index, level in enumerate(sent_levels):
        heard = 0 <= index + shift < len(raw_levels) and raw_levels[index + shift] >= SOUND_DB
        if heard and level <= SILENT_DB:
            run = run or [index, index]
            run[1] = index
        elif run:
            silenced.append(run)
            run = None
    if run:
        silenced.append(run)
    clipped = None
    first_heard = next((i for i, level in enumerate(raw_levels) if level >= SOUND_DB), None)
    first_sent = next((i for i, level in enumerate(sent_levels) if level > SILENT_DB), None)
    if first_heard is not None and first_sent is not None:
        clipped = max(0.0, (first_sent + shift - first_heard) * FRAME_S * 1000)
    peak = lambda levels: max(levels) if levels else -120.0
    mean = lambda levels: round(sum(levels) / len(levels), 1) if levels else -120.0
    return {'raw_s': round(len(raw) / raw_rate, 3), 'sent_s': round(len(sent) / sent_rate, 3), 'raw_rate': raw_rate, 'sent_rate': sent_rate,
            'raw_peak_db': peak(raw_levels), 'raw_mean_db': mean(raw_levels), 'sent_peak_db': peak(sent_levels), 'sent_mean_db': mean(sent_levels),
            'silenced': [{'from_s': round(a * FRAME_S, 2), 'to_s': round((b + 1) * FRAME_S, 2)} for a, b in silenced][:200],
            'clipped_start_ms': None if clipped is None else round(clipped), 'pre_roll_s': round(max(0.0, float(pre_roll_s or 0)), 3),
            'frame_s': FRAME_S, 'raw_levels': raw_levels[-int(MAX_SECONDS / FRAME_S):], 'sent_levels': sent_levels[-int(MAX_SECONDS / FRAME_S):]}


def _index(data):
    return folder(data) / 'index.json'


def _read(data):
    try:
        items = json.loads(_index(data).read_text(encoding='utf-8'))
        return [item for item in items if isinstance(item, dict) and ID.match(str(item.get('id', '')))]
    except (OSError, ValueError):
        return []


def _write(data, items):
    path = _index(data)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(items, indent=1), encoding='utf-8')
    os.replace(temporary, path)


def save(data, raw, sent, pre_roll_s=0.0, device='', mode=''):
    """Store one raw/sent pair with its analysis; returns (id, analysis). ValueError for bad audio or a full folder."""
    raw_rate, raw_bytes, raw_samples = _decode(raw, 'raw')
    sent_rate, sent_bytes, sent_samples = _decode(sent, 'sent', rates=(16000,))
    try:
        pre_roll = float(pre_roll_s or 0)
    except (TypeError, ValueError):
        pre_roll = 0.0
    result = analyse(raw_rate, raw_samples, sent_rate, sent_samples, pre_roll)
    device = ' '.join(str(device or '').split())[:60]
    mode = mode if mode in ('media', 'call') else ''
    with _lock:
        items = _read(data)
        if len(items) >= MAX_RECORDINGS:
            raise ValueError('The recordings folder is full (%d); delete some first' % MAX_RECORDINGS)
        item_id = time.strftime('%Y%m%d%H%M%S') + '-' + secrets.token_hex(4)
        folder(data).mkdir(parents=True, exist_ok=True)
        (folder(data) / (item_id + '-raw.wav')).write_bytes(raw_bytes)
        (folder(data) / (item_id + '-sent.wav')).write_bytes(sent_bytes)
        (folder(data) / (item_id + '.json')).write_text(json.dumps(result), encoding='utf-8')
        summary = {key: result[key] for key in ('raw_s', 'sent_s', 'raw_peak_db', 'sent_peak_db', 'clipped_start_ms')}
        items.append({'id': item_id, 'at': round(time.time(), 3), 'device': device, 'mode': mode, 'silenced': len(result['silenced']), **summary})
        _write(data, items)
    return item_id, result


def list_recordings(data):
    with _lock:
        return _read(data)


def delete(data, item_id):
    if not isinstance(item_id, str) or not ID.match(item_id):
        raise ValueError('Unknown recording')
    with _lock:
        items = _read(data)
        kept = [item for item in items if item['id'] != item_id]
        if len(kept) == len(items):
            return False
        for suffix in ('-raw.wav', '-sent.wav', '.json'):
            try:
                (folder(data) / (item_id + suffix)).unlink()
            except FileNotFoundError:
                pass
        _write(data, kept)
        return True
