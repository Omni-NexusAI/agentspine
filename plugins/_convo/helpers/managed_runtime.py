"""Owned Linux subprocess gateway; never installs dependencies or starts models.

States: stopped, setup_required, starting, slow_start, ready, health_failed, startup_failed. A cold start imports a
large dependency tree over a slow mount, so "not answering yet" is not a failure: `starting` becomes `slow_start`
after SLOW_AFTER seconds and `health_failed` only after DEADLINE seconds without a healthy answer. A degraded but
alive gateway is re-probed in the background (single flight, at most every REPROBE seconds), so a late start recovers
without a restart. `public()` never performs I/O.
"""
from __future__ import annotations

import atexit
import collections
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler

TOKEN_ENV = 'CONVO_MANAGED_RUNTIME_TOKEN'
SLOW_AFTER = 20.0     # seconds of "starting" before it is shown as a slow start
DEADLINE = 180.0      # seconds without a healthy answer before the start is reported as not responding
REPROBE = 5.0         # minimum gap between background re-probes of a degraded gateway
LOG_NAME = 'runtime-gateway.log'
LOG_LINES, LOG_LINE_CHARS = 200, 400

LABELS = {
    'stopped': 'Stopped',
    'setup_required': 'Setup required',
    'starting': 'Starting',
    'slow_start': 'Still starting (a first start can take a few minutes)',
    'ready': 'Ready',
    'health_failed': 'Not responding (stop and start the owned runtime from Advanced)',
    'startup_failed': 'Exited during start',
}
REFUSALS = {
    'stopped': 'Convo runtime is stopped; enable Convo or start the owned runtime from Advanced',
    'setup_required': 'Convo runtime setup is required; check plugin setup',
    'starting': 'Convo runtime is still starting; retry in a moment',
    'slow_start': 'Convo runtime is still starting (a first start can take a few minutes); retry shortly',
    'health_failed': 'Convo runtime did not answer its health check; stop and start the owned runtime from Advanced',
    'startup_failed': 'Convo runtime exited during start; check plugin setup or retry',
}


class ManagedRuntime:
    # Class-level defaults keep an instance created by older code usable after a hot class swap.
    started_at = None
    last_probe = None
    probing = False
    probe_done = None

    def __init__(self, root, *, clock=None, sleep=None, fetch=None, spawn=None):
        """`clock`, `sleep`, `fetch` and `spawn` exist so tests can run the state machine without real time or I/O."""
        self.root = Path(root)
        self.lock = threading.RLock()
        self.process = None
        self.url = ''
        self.status = 'stopped'
        self.retry_after = 0
        self.token = ''
        self.studio_available = False
        for name, value in (('clock', clock), ('sleep', sleep), ('fetch', fetch), ('spawn', spawn)):
            if value is not None:
                self.__dict__[name] = value
        atexit.register(self.stop)

    # ---- injectable seams (look in the instance first so a swapped-in old instance still works)
    def _now(self):
        return (self.__dict__.get('clock') or time.monotonic)()

    def _sleep(self, seconds):
        (self.__dict__.get('sleep') or time.sleep)(seconds)

    def _spawn(self, target, *args):
        spawn = self.__dict__.get('spawn')
        if spawn:
            spawn(target, *args)
        else:
            threading.Thread(target=target, args=args, name='ConvoRuntime' + target.__name__, daemon=True).start()

    def _fetch(self, url, token):
        """Healthy answer body, or None. The only network read in this module."""
        fetch = self.__dict__.get('fetch')
        if fetch:
            return fetch(url, token)
        try:
            opener = build_opener(ProxyHandler({}))
            request = Request(url + '/health', headers={'Authorization': 'Bearer ' + token})
            with opener.open(request, timeout=1.0) as response:
                body = json.loads(response.read(4096))
            return body if isinstance(body, dict) and body.get('service') == 'convo' and body.get('protocol') == 1 else None
        except Exception:
            return None

    # ---- lifecycle
    def start(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return
            if self._now() < self.retry_after:
                return
            self.retry_after = self._now() + 30
            executable = self.root / 'usr/plugins/_convo/data/runtime-env/bin/python'
            if sys.platform != 'linux' or not executable.is_file():
                self.status = 'setup_required'
                return
            self.token = secrets.token_urlsafe(32)
            env = dict(os.environ)
            env['CONVO_SIDECAR_TOKEN'] = self.token
            env['CONVO_DATA_DIR'] = str(self.root / 'usr/plugins/_convo/data')
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            plugin = Path(__file__).resolve().parents[1]
            prefix = 'usr.plugins' if plugin.parent.parent.name == 'usr' else 'plugins'
            try:
                # Inherit the bound socket, eliminating port-selection races.
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1', 0))
                    listener.listen(128)
                    self.url = 'http://127.0.0.1:' + str(listener.getsockname()[1])
                    self.process = subprocess.Popen(
                        [str(executable), '-m', prefix + '._convo.sidecar.managed_runner',
                         str(listener.fileno()), str(os.getpid())],
                        cwd=self.root, env=env, pass_fds=(listener.fileno(),),
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, start_new_session=True)
                os.environ[TOKEN_ENV] = self.token
                self.status = 'starting'
                self.started_at = self._now()
                self.last_probe = None
                self.probing = False
                self._spawn(self._health, self.process, self.url, self.token)
                self._spawn(self._drain, self.process)
            except (OSError, ValueError):
                self.status = 'startup_failed'

    def _health(self, process, url, token):
        """Initial bounded wait. Failing the deadline is not final: public() keeps re-probing a live process."""
        started = self._now()
        delay = .25
        while True:
            with self.lock:
                if self.process is not process or self.status == 'ready':
                    return
                if process.poll() is not None:
                    self.status = 'startup_failed'
                    return
            body = self._fetch(url, token)
            if body is not None:
                self._mark_ready(process, body)
                return
            if self._now() - started >= DEADLINE:
                with self.lock:
                    if self.process is process and self.status == 'starting':
                        self.status = 'health_failed'
                return
            self._sleep(delay)
            delay = min(1.0, delay * 1.5)

    def _mark_ready(self, process, body):
        with self.lock:
            if self.process is process and self.status in {'starting', 'health_failed'}:
                self.status = 'ready'
                self.studio_available = body.get('studio_available') is True

    def _reprobe(self, process, url, token, done):
        try:
            body = self._fetch(url, token)
            if body is not None:
                self._mark_ready(process, body)
        finally:
            with self.lock:
                self.probing = False
                self.last_probe = self._now()
            done.set()

    def _maybe_probe(self, *, force=False):
        """With the lock held: schedule at most one background probe of a degraded but alive gateway."""
        process = self.process
        if self.status != 'health_failed' or self.probing or process is None or process.poll() is not None:
            return None
        if not self.url or not self.token:
            return None
        if not force and self.last_probe is not None and self._now() - self.last_probe < REPROBE:
            return None
        self.probing = True
        done = self.probe_done = threading.Event()
        self._spawn(self._reprobe, process, self.url, self.token, done)
        return done

    def _drain(self, process):
        """Keep only the tail of the child's stderr, in memory and in a private, bounded, atomically replaced file."""
        tail = collections.deque(maxlen=LOG_LINES)
        path = self.root / 'usr/plugins/_convo/data' / LOG_NAME
        written = 0.0
        try:
            for raw in process.stderr:
                tail.append(raw.decode('utf-8', 'replace').rstrip()[:LOG_LINE_CHARS])
                if self._now() - written >= 2:
                    self._write_log(path, tail)
                    written = self._now()
        except (OSError, ValueError, TypeError):
            pass
        finally:
            self._write_log(path, tail)

    @staticmethod
    def _write_log(path, tail):
        if not tail:
            return
        temporary = path.with_name(path.name + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write('\n'.join(tail) + '\n')
            os.replace(temporary, path)
        except OSError:
            pass

    def stop(self):
        with self.lock:
            process, self.process = self.process, None
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=.5)
            if self.token and os.environ.get(TOKEN_ENV) == self.token:
                os.environ.pop(TOKEN_ENV, None)
            self.token, self.url, self.status = '', '', 'stopped'
            self.studio_available = False
            self.retry_after = 0
            self.started_at = self.last_probe = None
            self.probing = False

    # ---- reading
    def state(self):
        """Derived without I/O: a long `starting` is a slow start."""
        if self.status == 'starting' and self.started_at is not None and self._now() - self.started_at >= SLOW_AFTER:
            return 'slow_start'
        return self.status

    def public(self):
        with self.lock:
            if self.process is not None and self.process.poll() is not None:
                self.status = 'startup_failed'
            self._maybe_probe()
            state = self.state()
            result = {'mode': 'managed', 'state': state, 'label': LABELS.get(state, state),
                      'studio_available': self.studio_available and self.status == 'ready'}
            if state in {'starting', 'slow_start', 'health_failed'} and self.started_at is not None:
                result['waited'] = int(max(0, self._now() - self.started_at))
            return result

    def confirm(self, wait=.5):
        """For an explicit action: probe a degraded gateway now and wait a bounded moment for the answer."""
        with self.lock:
            done = self._maybe_probe(force=True) or (self.probe_done if self.probing else None)
        if done is not None:
            done.wait(wait)
        return self.public()


def instance():
    from helpers import runtime
    from . import lifecycle
    from .convo_service import runtime_root
    with lifecycle.state().lock:
        if not hasattr(runtime, '_convo_backend'):
            runtime._convo_backend = ManagedRuntime(runtime_root())
        elif type(runtime._convo_backend) is not ManagedRuntime:
            manager = runtime._convo_backend
            with manager.lock:
                if manager.process is not None and manager.process.poll() is None:
                    raise ValueError('Disable Convo before refreshing its runtime implementation')
                manager.__class__ = ManagedRuntime
                manager.studio_available = False
        return runtime._convo_backend


def configure(config):
    from . import lifecycle
    control = lifecycle.state()
    with control.lock:
        manager = instance()
        if not config['sidecar'].get('managed', False):
            manager.stop()
            return config
        if control.blocked or control.disabling or not lifecycle.enabled():
            manager.stop()
            return config
        control.register('managed-runtime', manager.stop)
        manager.start()
        return resolve(config)


def resolve(config):
    """Resolve an already-started runtime without starting or stopping anything."""
    if config['sidecar'].get('managed'):
        manager = instance()
        with manager.lock:
            config['sidecar'] = dict(config['sidecar'], url=manager.url or 'http://127.0.0.1:0',
                                     token_env=TOKEN_ENV, remote=False)
    return config


def require_ready(config):
    if config['sidecar'].get('managed'):
        manager = instance()
        state = manager.public()['state']
        if state == 'health_failed':
            state = manager.confirm()['state']  # the user asked to start now: a bounded recheck, never a latch
        if state != 'ready':
            raise ValueError(REFUSALS.get(state, 'Convo runtime is ' + state.replace('_', ' ') + '; check plugin setup or retry after startup'))
