"""Lazy singleton. Importing the plugin never loads a model or starts a service."""
from __future__ import annotations

import importlib
import asyncio
import threading
from contextlib import contextmanager
from pathlib import Path

from .convo_contract import validate_settings
from .convo_host import Compactor, Dispatcher, Host
from .convo_store import Store
from . import studio_admission

_instance = None
_lock = threading.Lock()


def runtime_root():
    plugin_root = Path(__file__).resolve().parents[1]
    parent = plugin_root.parent.parent
    return parent.parent if parent.name == "usr" else parent


def settings():
    from helpers import plugins
    raw = plugins.get_plugin_config("_convo") or {}
    config = validate_settings(raw.get("convo", {}))
    from .convo_setup import attach_keys
    config = attach_keys(config)
    if plugins.get_toggle_state("_convo") == "disabled":
        config["enabled"] = False
    # Reading settings is deliberately inert. Only native lifecycle hooks and
    # explicit runtime actions may spawn/stop the owned gateway.
    from .managed_runtime import resolve
    config = resolve(config)
    return config


class Service:
    def __init__(self, root=None, host=None):
        self.root = Path(root or runtime_root() / "usr" / "plugins")
        data = self.root / "_convo" / "data"
        data.mkdir(parents=True, exist_ok=True)
        self.store = Store(data / "convo.sqlite3")
        self.host = host or Host()
        self.dispatcher = Dispatcher(self.store, self.host)
        self.compactor = Compactor(self.store, self.host)
        self.persona_cache = {}
        self.connections = {}
        self.retiring_connections = {}
        self.retirement_failures = {}
        self._retirement_scheduled = set()
        self._retirement_acknowledged = set()
        self._retirement_tasks = {}
        self.connection_lock = threading.RLock()
        self.disabled = False
        self.studio_busy = False

    def _ensure_retirement(self):
        # Called under connection_lock; safely retain state across class refresh.
        for name, factory in (('retiring_connections', dict), ('retirement_failures', dict),
                              ('_retirement_scheduled', set), ('_retirement_acknowledged', set),
                              ('_retirement_tasks', dict)):
            if not hasattr(self, name):
                setattr(self, name, factory())

    def start_session(self, owner, target):
        # Same lock as Studio admission; no async operation holds this lock.
        from . import setup_store
        # Settings lock comes first, matching transaction admission and preventing
        # a mode-save/session-start lock inversion. Keep it through store.start.
        with setup_store.lock(), self.connection_lock:
            self._ensure_retirement()
            if self.retiring_connections:
                raise ValueError('Convo connections are still releasing; wait for owning-loop cleanup before starting')
            persisted = setup_store.read(self.root / '_convo' / 'config.json')
            if (persisted.get('convo') or {}).get('ai_link_development_mode'):
                raise ValueError('AI Link development mode keeps browser voice idle.')
            if self.studio_admission_status()['blocked']:
                raise ValueError(studio_admission.RECOVERY_MESSAGE)
            if self.disabled or getattr(self, 'studio_busy', False):
                raise ValueError('Studio is using the audio backend; finish that operation before starting Convo')
            return self.store.start(owner, target)

    def studio_admission_path(self):
        return self.root / '_convo' / 'data' / studio_admission.FILENAME

    def studio_admission_status(self):
        return studio_admission.status(self.studio_admission_path())

    def register_connection(self, handler, sid):
        with self.connection_lock:
            self._ensure_retirement()
            if self.disabled or self.retiring_connections:
                raise ValueError('Convo was disabled during session setup')
            self.connections[(id(handler), sid)] = (asyncio.get_running_loop(), handler, sid)

    def forget_connection(self, handler, sid, *, completed=True):
        """Acknowledge cleanup, never just socket removal or cancellation intent."""
        key = (id(handler), sid)
        with self.connection_lock:
            self._ensure_retirement()
            entry = self.connections.pop(key, None)
            if not completed:
                if entry is not None:
                    self.retiring_connections.setdefault(key, entry)
                if key in self.retiring_connections:
                    self.retirement_failures[key] = 'cleanup_not_completed'
                return
            if key in self._retirement_scheduled:
                # Wait for the scheduled disable callback's final send/cleanup,
                # not merely its inner on_disconnect acknowledgment.
                self._retirement_acknowledged.add(key)
            else:
                self._finish_retirement(key)

    def _finish_retirement(self, key):
        self.retiring_connections.pop(key, None)
        self.retirement_failures.pop(key, None)
        self._retirement_scheduled.discard(key)
        self._retirement_acknowledged.discard(key)
        self._retirement_tasks.pop(key, None)

    def _schedule_retirement(self, key, entry):
        loop, handler, sid = entry
        with self.connection_lock:
            if key not in self.retiring_connections or key in self._retirement_scheduled:
                return
            if loop.is_closed():
                self.retirement_failures[key] = 'owning_loop_closed'
                return
            self._retirement_scheduled.add(key)

        def launch():
            with self.connection_lock:
                if key not in self.retiring_connections:
                    self._retirement_scheduled.discard(key)
                    return
                if key in self._retirement_acknowledged:
                    self._finish_retirement(key)
                    return
                task = asyncio.create_task(self._retire_connection(key, handler, sid))
                self._retirement_tasks[key] = task

        try:
            loop.call_soon_threadsafe(launch)
        except RuntimeError:
            with self.connection_lock:
                self._retirement_scheduled.discard(key)
                self.retirement_failures[key] = 'owning_loop_unavailable'

    async def _retire_connection(self, key, handler, sid):
        reason = 'cleanup_not_acknowledged'
        try:
            await handler.disable(sid)
        except asyncio.CancelledError:
            reason = 'cleanup_cancelled'
        except Exception:
            reason = 'cleanup_failed'
        finally:
            with self.connection_lock:
                self._retirement_tasks.pop(key, None)
                self._retirement_scheduled.discard(key)
                if key in self._retirement_acknowledged:
                    self._finish_retirement(key)
                elif key in self.retiring_connections:
                    self.retirement_failures[key] = reason

    def deactivate(self):
        with self.connection_lock:
            self.disabled = True
            self._ensure_retirement()
            self.retiring_connections.update(self.connections)
            self.connections.clear()
            retiring = list(self.retiring_connections.items())
        self.dispatcher.pause()
        with self.store.lock:
            sessions = list(self.store.db.execute('SELECT id,owner FROM sessions WHERE active=1'))
            for row in sessions:
                self.store.stop(row['id'], row['owner'])
        for key, entry in retiring:
            # Retain ownership until the actual owning-loop cleanup acknowledges.
            # Never kill a host agent, external model, or delegated job.
            self._schedule_retirement(key, entry)


def service():
    global _instance
    from . import lifecycle
    if not lifecycle.enabled():
        raise ValueError('Convo is disabled')
    with _lock:
        if _instance is None:
            # Plugin module refresh must not create a second dispatcher or reset an
            # existing process's active sessions. The host runtime outlives plugins.
            from helpers import runtime
            if not hasattr(runtime, "_convo_service"):
                runtime._convo_service = Service()
            _instance = runtime._convo_service
        if type(_instance) is not Service:
            # Native OFF has released connections. Preserve journal/host jobs
            # while updating plugin-owned methods across a module refresh.
            with _instance.connection_lock:
                if _instance.connections or getattr(_instance, 'retiring_connections', {}):
                    raise ValueError('Disable Convo and finish connection cleanup before updating')
                _instance.__class__ = Service
                _instance.studio_busy = getattr(_instance, 'studio_busy', False)
                _instance._ensure_retirement()
        control = lifecycle.state()
        with control.lock:
            if control.blocked or control.disabling:
                raise ValueError('Convo is disabled or updating; explicitly re-enable it before starting')
            with _instance.connection_lock:
                _instance._ensure_retirement()
                if _instance.retiring_connections:
                    raise ValueError('Convo connections are still releasing; retry after owning-loop cleanup')
                _instance.disabled = False
            control.register('service', _instance.deactivate)
        return _instance


def load_from_api(filename):
    root = Path(filename).resolve().parent.parent
    prefix = "usr.plugins" if root.parent.parent.name == "usr" else "plugins"
    return importlib.import_module(prefix + "._convo.helpers.convo_service")


class StudioLease:
    def __init__(self, current=None):
        self.current, self.operation_id = current, None

    def dispatched(self):
        if self.current is not None:
            with self.current.connection_lock:
                if self.current.disabled:
                    raise ValueError('Convo was disabled before Studio dispatch')
                self.operation_id = studio_admission.dispatched(self.current.studio_admission_path())

    def completed(self):
        if self.current is not None and self.operation_id:
            with self.current.connection_lock:
                studio_admission.completed(self.current.studio_admission_path(), self.operation_id)
                self.operation_id = None


async def studio_operation(sidecar, payload, method):
    """Direct API mutations use exactly the same dispatch/acknowledgment lease."""
    import json
    # Match the HTTP client's JSON validation before recording network dispatch.
    json.dumps(payload, allow_nan=False)
    with studio_request_guard(method, '/studio') as lease:
        lease.dispatched()
        try:
            result = await sidecar.studio(payload)
        except Exception as exc:
            # httpx raises only after the buffered HTTP response is received.
            # Do not mistake a server timeout (5xx) for downstream completion.
            import httpx
            if isinstance(exc, httpx.HTTPStatusError) and 300 <= exc.response.status_code < 500:
                lease.completed()
            raise
        lease.completed()
        return result


@contextmanager
def studio_request_guard(method, path, *, allow_uncertain=False):
    """Lease, not a threading lock held across awaits. Native OFF remains free."""
    from . import lifecycle
    if not lifecycle.enabled():
        raise ValueError('Convo is disabled')
    if method.upper() in {'GET', 'HEAD', 'OPTIONS'}:
        yield StudioLease()
        return
    current = service()
    with current.connection_lock:
        if not allow_uncertain and current.studio_admission_status()['blocked']:
            raise ValueError(studio_admission.RECOVERY_MESSAGE)
        if current.disabled or getattr(current, 'studio_busy', False):
            raise ValueError('Another Studio operation is active')
        with current.store.lock:
            if current.store.db.execute('SELECT 1 FROM sessions WHERE active=1').fetchone():
                raise ValueError('Stop Convo before previewing or changing its audio backend')
        current.studio_busy = True
    try:
        yield StudioLease(current)
    finally:
        with current.connection_lock:
            # Releasing an HTTP request is not evidence that its upstream work
            # stopped. Only lease.completed() can clear the durable marker.
            current.studio_busy = False
