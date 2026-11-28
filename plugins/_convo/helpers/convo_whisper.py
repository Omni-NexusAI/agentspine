"""Convo's Whisper, outside Agent Zero's process (`convo_whisper_worker.py`), so transcripts never hold up a reply.

`Manager` owns at most one worker process: started only while transcripts are on (by a voice session), stopped when the session
ends, restarted when the selected Whisper model changes. Every call that could wait (starting the process, writing a request) runs
off the event loop; a request while the model loads is answered `loading` at once, and a missing or half-written model file is
`unavailable` (the worker never downloads). The worker holds its own copy of the model (about 0.3 GB for "base", 1 GB for "small").

`host_state()` reads, without importing anything, whether Agent Zero's own Whisper is loading a model or would have to download one:
the browser's own Whisper request (the fallback while the worker cannot run) is not made then."""
from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

WORKER = Path(__file__).resolve().parent / 'convo_whisper_worker.py'
DEFAULT_ROOT = '/a0/tmp/models/whisper'
REQUEST_SECONDS = 60.0         # one transcription answer, at most
NO_WHISPER = 'whisper is not installed in this Python'


def threads():
    """CPU threads the worker may use: half the machine's, at most two, so the voice path keeps its cores."""
    return max(1, min(2, (os.cpu_count() or 2) // 2))


def selected_model():
    """The Whisper model Agent Zero's dictation uses (`_whisper_stt` model_size, else the host's stt_model_size), 'base' if unknown."""
    try:
        from helpers import plugins
        config = plugins.get_plugin_config('_whisper_stt') or {}
        name = str(config.get('model_size') or '').strip()
        if name:
            return name
    except Exception:
        pass
    try:
        from helpers import settings
        return str(settings.get_settings().get('stt_model_size') or 'base')
    except Exception:
        return 'base'


def download_root():
    try:
        from helpers import files
        return files.get_abs_path('/tmp/models/whisper')
    except Exception:
        return DEFAULT_ROOT


def host_state(model=None, root=None):
    """Agent Zero's own Whisper as seen from here, without importing it: 'loading' while it loads (or downloads) a model, 'missing'
    when the selected model's file is not on disk (a request would download it), else 'ready'."""
    for name in ('helpers.whisper', 'plugins._whisper_stt.helpers.runtime', 'usr.plugins._whisper_stt.helpers.runtime'):
        module = sys.modules.get(name)
        if module is not None and any(getattr(module, flag, False) is True for flag in ('is_updating_model', 'is_loading', '_loading', 'loading')):
            return 'loading'
    from .convo_whisper_worker import model_file
    path, _ = model_file(model or selected_model(), root or download_root())
    return 'ready' if os.path.isfile(path) else 'missing'


class Manager:
    def __init__(self, python=None, script=None, *, model=None, root=None, device='cpu', limit=None, popen=subprocess.Popen):
        self.python, self.script = python or sys.executable, str(script or WORKER)
        self._model, self._root, self.device, self.limit = model, root, device, limit or threads()
        self.popen = popen
        self.lock = threading.RLock()
        self.process = None
        self.model = None
        self.state, self.detail = 'off', ''
        self.pending = {}
        self.ids = itertools.count(1)
        self.ready = threading.Event()        # set whenever the state leaves 'loading'/'starting'

    # ---- lifecycle (blocking: call through asyncio.to_thread from a loop)
    def wanted_model(self):
        return self._model or selected_model()

    def start(self, model=None):
        """Start the worker for `model` (the selected one) unless it already runs it. Returns the state."""
        model = model or self.wanted_model()
        with self.lock:
            if self.process is not None and self.process.poll() is None and self.model == model:
                return self.state
            self._stop_locked()
            env = dict(os.environ)
            for name in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
                env[name] = str(self.limit)
            env['HF_HUB_OFFLINE'] = env['TRANSFORMERS_OFFLINE'] = '1'
            try:
                self.process = self.popen([self.python, '-I', '-u', self.script, '--model', model, '--root', self._root or download_root(),
                                           '--device', self.device, '--threads', str(self.limit)],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                                          text=True, encoding='utf-8', bufsize=1)
            except Exception as error:
                self.process = None
                self._set('unavailable', 'the Whisper worker could not start (%s)' % type(error).__name__)
                return self.state
            self.model = model
            self._set('loading', '')
            threading.Thread(target=self._read, args=(self.process,), name='ConvoWhisperRead', daemon=True).start()
            return self.state

    def stop(self):
        with self.lock:
            self._stop_locked()
            self._set('off', '')

    def _stop_locked(self):
        process, self.process = self.process, None
        self.model = None
        for future in self.pending.values():
            if not future.done():
                future.set_result({'dropped': True})
        self.pending.clear()
        if process is not None and process.poll() is None:
            try:
                process.stdin.write(json.dumps({'op': 'exit'}) + '\n')
                process.stdin.flush()
            except Exception:
                pass
            try:
                process.terminate()
            except Exception:
                pass
            threading.Thread(target=self._reap, args=(process,), name='ConvoWhisperReap', daemon=True).start()

    @staticmethod
    def _reap(process):
        try:
            process.wait(5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        try:
            process.stdin.close()
        except Exception:
            pass

    def _set(self, state, detail):
        self.state, self.detail = state, detail
        if state in ('loading', 'starting'):
            self.ready.clear()
        else:
            self.ready.set()

    def _read(self, process):
        try:
            self._lines(process)
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass

    def _lines(self, process):
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            with self.lock:
                if process is not self.process:
                    return
                if message.get('event') == 'status':
                    self._set(str(message.get('state', '')), str(message.get('detail', ''))[:200])
                    continue
                future = self.pending.pop(message.get('id'), None)
            if future is not None and not future.done():
                future.set_result(message)
        with self.lock:
            if process is self.process:
                self.process = None
                for future in self.pending.values():
                    if not future.done():
                        future.set_result({'unavailable': 'the Whisper worker stopped'})
                self.pending.clear()
                if self.state != 'unavailable':
                    self._set('unavailable', 'the Whisper worker stopped')

    def _submit(self, audio):
        with self.lock:
            process = self.process
            if process is None or process.poll() is not None:
                return None
            ident = next(self.ids)
            future = self.pending[ident] = concurrent.futures.Future()
            try:
                process.stdin.write(json.dumps({'id': ident, 'op': 'transcribe', 'audio': audio}) + '\n')
                process.stdin.flush()
            except Exception:
                self.pending.pop(ident, None)
                return None
            return future

    def status(self):
        with self.lock:
            return {'state': self.state, 'detail': self.detail, 'model': self.model or ''}

    # ---- the voice path's view (never blocks the loop)
    async def ensure(self):
        """Start (or switch to the selected model) off the loop. Returns the state."""
        return await asyncio.to_thread(self.start)

    async def transcribe(self, audio, *, wait=0.0, timeout=REQUEST_SECONDS):
        """The transcript of one base64 WAV: {'text', 'language'}, or {'state': 'loading'} (answered at once unless `wait` allows a
        bounded wait for the load), {'unavailable': why}, {'dropped': True} (a newer request took its place) or {'error': kind}."""
        if not isinstance(audio, str) or not audio:
            return {'error': 'no_audio'}
        if self.status()['state'] in ('off',) or self.model != self.wanted_model():
            await self.ensure()
        if wait and self.status()['state'] == 'loading':
            await asyncio.to_thread(self.ready.wait, wait)
        state = self.status()
        if state['state'] == 'loading':
            return {'state': 'loading'}
        if state['state'] != 'ready':
            return {'unavailable': state['detail'] or 'whisper unavailable'}
        future = await asyncio.to_thread(self._submit, audio)
        if future is None:
            return {'unavailable': 'the Whisper worker is not running'}
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except asyncio.TimeoutError:
            return {'error': 'timeout'}


def manager_for(service):
    """The service's one Whisper manager (created on first use), or None for a service that has none (tests, older hosts)."""
    if service is None or not hasattr(service, 'store'):
        return None
    found = getattr(service, 'whisper', None)
    if found is None and getattr(service, '__class__', None).__name__ == 'Service':
        found = service.whisper = Manager()
    return found if isinstance(found, Manager) or hasattr(found, 'transcribe') else None

