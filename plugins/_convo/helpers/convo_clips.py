"""Labelled test clips for measuring turn-taking models (the decision-model bake-off): developer mode only.

A clip is recorded only when the user presses "Record next utterance" in the diagnostics panel; that utterance is saved here and is
not sent to the conversation. Clips are mono PCM16 WAV (what Convo's capture sends), at most MAX_SECONDS, kept in the plugin's own
data folder on the Agent Zero host (`data/test_clips/`), listed with their label, length, time and a short device label, and deleted
on request. Nothing is uploaded anywhere."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import threading
import time
import wave
from pathlib import Path

from .convo_contract import validate_audio

LABELS = ('request', 'statement', 'follow_up', 'acknowledgement', 'unfinished', 'dont_respond', 'noise', 'side_talk', 'over_playback')
MAX_SECONDS = 30
MAX_CLIPS = 500
ID = re.compile(r'^[0-9]{14}-[0-9a-f]{8}$')
_lock = threading.Lock()


def folder(data):
    return Path(data) / 'test_clips'


def _index_path(data):
    return folder(data) / 'index.json'


def _read(data):
    try:
        items = json.loads(_index_path(data).read_text(encoding='utf-8'))
        return [item for item in items if isinstance(item, dict) and ID.match(str(item.get('id', '')))]
    except (OSError, ValueError):
        return []


def _write(data, items):
    path = _index_path(data)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(items, indent=1), encoding='utf-8')
    os.replace(temporary, path)


def save(data, label, audio, device=''):
    """Store one clip; returns its id. Raises ValueError for a bad label, audio or a full folder."""
    if label not in LABELS:
        raise ValueError('Choose one of the clip labels')
    try:
        validate_audio(audio, MAX_SECONDS)
    except Exception as error:
        raise ValueError('A clip must be a mono PCM16 WAV of at most %d seconds' % MAX_SECONDS) from error
    raw = base64.b64decode(audio)
    with wave.open(io.BytesIO(raw)) as reader:
        seconds = round(reader.getnframes() / reader.getframerate(), 2)
    device = ' '.join(str(device or '').split())[:40]
    with _lock:
        items = _read(data)
        if len(items) >= MAX_CLIPS:
            raise ValueError('The test clip folder is full (%d clips); delete some first' % MAX_CLIPS)
        clip = time.strftime('%Y%m%d%H%M%S') + '-' + secrets.token_hex(4)
        folder(data).mkdir(parents=True, exist_ok=True)
        (folder(data) / (clip + '.wav')).write_bytes(raw)
        items.append({'id': clip, 'label': label, 'seconds': seconds, 'at': round(time.time(), 3), 'device': device})
        _write(data, items)
    return clip


def list_clips(data):
    with _lock:
        return _read(data)


def delete(data, clip):
    if not isinstance(clip, str) or not ID.match(clip):
        raise ValueError('Unknown clip')
    with _lock:
        items = _read(data)
        kept = [item for item in items if item['id'] != clip]
        if len(kept) == len(items):
            return False
        try:
            (folder(data) / (clip + '.wav')).unlink()
        except FileNotFoundError:
            pass
        _write(data, kept)
        return True
