"""Convo's Whisper worker: a separate process, so a transcription never competes with the voice loop for Agent Zero's process.

Started by `convo_whisper.Manager` with Agent Zero's own Python (which has Whisper) and only while transcripts are on. It runs at a
low priority with a capped number of CPU threads, loads only a model file that is already on disk and complete (it never downloads),
decodes the WAV in memory and lets Whisper detect the language. One transcription runs at a time and one waits (the newest replaces
the waiting one, which is answered `dropped`); while the model loads, a request is answered `loading` at once.

Protocol: one JSON object per line. In: {"id", "op": "transcribe", "audio": base64 WAV} or {"id", "op": "status"}. Out: answers
carrying the request's `id` ({"text", "language"} | {"state": "loading"} | {"unavailable": reason} | {"dropped": true} |
{"error": kind}), and `{"event": "status", "state", "detail"}` whenever the state changes. Nothing is written to disk; no audio or
words are ever logged."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import threading
import time
import wave
from urllib.parse import urlparse

SETTLE_SECONDS = 1.0          # a model file's size must stay the same for this long (a download in progress keeps growing)
MIN_MODEL_BYTES = 1 << 20     # anything smaller is not a Whisper checkpoint
RATE = 16000                  # Whisper's input rate
ALIASES = {'turbo': 'large-v3-turbo.pt', 'large': 'large-v3.pt'}
# A segment Whisper itself rates as more likely not speech than this is left out of the text: Whisper writes words for noise ("you" for
# a hum or music) while scoring that segment as non-speech. Measured 2026-11-17 on the test host (base and small, two synthetic voices,
# six noises): noise kept as words scored .87-.90; speech .001-.016. Whisper's own default threshold, judged from its own score of
# the audio, never from which words came out.
NO_SPEECH = 0.6


def model_file(name, root, models=None):
    """(file path, expected sha256 or '') for model `name` under the download root, as openai-whisper names it (the file name of its
    download URL; the URL's directory is the file's sha256). `name` may also be a path to a checkpoint."""
    if name and os.path.sep in name or (name or '').endswith('.pt'):
        return name, ''
    url = (models or {}).get(name)
    if url:
        parts = urlparse(url).path.rstrip('/').split('/')
        sha = parts[-2] if len(parts) >= 2 and len(parts[-2]) == 64 else ''
        return os.path.join(root, parts[-1]), sha
    return os.path.join(root, ALIASES.get(name, str(name) + '.pt')), ''


def check_model_file(path, sha='', settle=SETTLE_SECONDS, sleep=time.sleep):
    """'' when `path` is a complete model file, else why not: missing, still being written (its size changed while watched), too
    small, or not the file Whisper's own list names (sha256). Never downloads anything."""
    try:
        first = os.stat(path).st_size
    except OSError:
        return 'model file not on disk'
    sleep(settle)
    try:
        second = os.stat(path).st_size
    except OSError:
        return 'model file not on disk'
    if first != second:
        return 'model file is still being written'
    if second < MIN_MODEL_BYTES:
        return 'model file is incomplete'
    if sha:
        digest = hashlib.sha256()
        with open(path, 'rb') as stream:
            for block in iter(lambda: stream.read(1 << 20), b''):
                digest.update(block)
        if digest.hexdigest() != sha:
            return 'model file is incomplete or not the expected one'
    return ''


def decode_wav(encoded):
    """Base64 WAV (optionally a data: URL) to mono float32 samples at 16 kHz, in memory."""
    import numpy as np
    if ',' in encoded and encoded.lstrip().startswith('data:'):
        encoded = encoded.split(',', 1)[1]
    with wave.open(io.BytesIO(base64.b64decode(encoded))) as reader:
        channels, width, rate = reader.getnchannels(), reader.getsampwidth(), reader.getframerate()
        frames = reader.readframes(reader.getnframes())
    if width != 2:
        raise ValueError('16-bit PCM WAV only')
    samples = np.frombuffer(frames, dtype='<i2').astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != RATE and samples.size:
        count = int(round(samples.size * RATE / rate))
        samples = np.interp(np.linspace(0, samples.size - 1, count), np.arange(samples.size), samples).astype(np.float32)
    return samples


class WhisperEngine:
    """openai-whisper, as Agent Zero's own Whisper uses it, loaded from a file path only (a path never triggers a download)."""
    def __init__(self, name, root, device='cpu', threads=1, module=None):
        self.name, self.root, self.device, self.threads, self.module, self.model = name, root, device, threads, module, None

    def _whisper(self):
        if self.module is None:
            import whisper
            self.module = whisper
        return self.module

    def load(self):
        """'' once loaded, else why Whisper is unavailable."""
        try:
            whisper = self._whisper()
        except Exception:
            return 'whisper is not installed in this Python'
        path, sha = model_file(self.name, self.root, getattr(whisper, '_MODELS', {}))
        problem = check_model_file(path, sha)
        if problem:
            return problem
        try:
            import torch
            torch.set_num_threads(max(1, int(self.threads)))
            device = self.device
            if device == 'auto':
                device = 'cuda' if torch.cuda.is_available() else 'cpu'
        except Exception:
            device = 'cpu' if self.device == 'auto' else self.device
        self.device = device
        try:
            self.model = whisper.load_model(path, device=device)
        except Exception as error:
            return 'model file could not be loaded (%s)' % type(error).__name__
        return ''

    def transcribe(self, encoded):
        result = self.model.transcribe(decode_wav(encoded), fp16='cuda' in str(self.device), language=None,
                                       condition_on_previous_text=False, verbose=None)
        segments = result.get('segments')
        if isinstance(segments, list) and segments:
            kept = [s for s in segments if float(s.get('no_speech_prob') or 0.0) < NO_SPEECH]
            text = ''.join(str(s.get('text') or '') for s in kept)
        else:
            kept, text = [], str(result.get('text') or '')
        return {'text': text.strip(), 'language': str(result.get('language') or '')[:12],
                'left_out': len(segments) - len(kept) if isinstance(segments, list) else 0}


class Worker:
    """The job rules, separate from the process so they can be tested in place: one running, one waiting (newest wins)."""
    def __init__(self, engine, send):
        self.engine, self.send = engine, send
        self.lock = threading.Condition()
        self.state, self.detail = 'loading', ''
        self.running, self.waiting = None, None
        self.active = 0                 # transcriptions running now (never more than one)
        self.peak = 0
        self.closed = False

    def _status(self, state, detail=''):
        with self.lock:
            self.state, self.detail = state, detail
            self.lock.notify_all()
        self.send({'event': 'status', 'state': state, 'detail': detail})

    def load(self):
        try:
            problem = self.engine.load()
        except Exception as error:
            problem = 'model could not be loaded (%s)' % type(error).__name__
        self._status('unavailable' if problem else 'ready', problem)

    def request(self, message):
        ident, op = message.get('id'), message.get('op')
        if op == 'status':
            with self.lock:
                self.send({'id': ident, 'state': self.state, 'detail': self.detail})
            return
        if op != 'transcribe' or not isinstance(message.get('audio'), str):
            self.send({'id': ident, 'error': 'bad_request'})
            return
        with self.lock:
            if self.state == 'loading':
                self.send({'id': ident, 'state': 'loading'})
                return
            if self.state != 'ready':
                self.send({'id': ident, 'unavailable': self.detail or 'whisper unavailable'})
                return
            dropped, self.waiting = self.waiting, message
            self.lock.notify_all()
        if dropped is not None:
            self.send({'id': dropped.get('id'), 'dropped': True})

    def run_jobs(self):
        while True:
            with self.lock:
                while self.waiting is None and not self.closed:
                    self.lock.wait()
                if self.closed:
                    return
                job, self.waiting = self.waiting, None
                self.running = job
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                answer = {'id': job.get('id'), **self.engine.transcribe(job['audio'])}
            except Exception as error:
                answer = {'id': job.get('id'), 'error': type(error).__name__}
            finally:
                with self.lock:
                    self.active -= 1
                    self.running = None
            self.send(answer)

    def close(self):
        with self.lock:
            self.closed = True
            waiting, self.waiting = self.waiting, None
            self.lock.notify_all()
        if waiting is not None:
            self.send({'id': waiting.get('id'), 'dropped': True})


def lower_priority(level=10):
    try:
        os.nice(level)
    except Exception:
        pass


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--threads', type=int, default=1)
    args = parser.parse_args(argv)
    lower_priority()
    out_lock = threading.Lock()
    stdout = sys.stdout

    def send(message):
        line = json.dumps(message, ensure_ascii=False)
        with out_lock:
            stdout.write(line + '\n')
            stdout.flush()

    worker = Worker(WhisperEngine(args.model, args.root, args.device, args.threads), send)
    send({'event': 'status', 'state': 'loading', 'detail': ''})
    threading.Thread(target=worker.load, name='ConvoWhisperLoad', daemon=True).start()
    threading.Thread(target=worker.run_jobs, name='ConvoWhisperJobs', daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            if message.get('op') == 'exit':
                break
            worker.request(message)
    worker.close()


if __name__ == '__main__':
    main()
