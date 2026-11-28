"""Reversible, process-local ownership. Never restart hosts or alter model services."""
from __future__ import annotations

import functools
import importlib
import logging
import threading

log = logging.getLogger('_convo')
MISSING = object()


def enabled():
    try:
        from helpers import plugins
        return plugins.get_toggle_state('_convo') != 'disabled'
    except Exception:
        return False  # A broken loader must not leave plugin behavior enabled.


class Lifecycle:
    def __init__(self):
        self.lock = threading.RLock()
        self.patches = []
        self.keys = set()
        self.callbacks = {}
        self.blocked = False
        self.disabling = False
        self.thread = None
        self.stopping = threading.Event()

    def watch(self):
        with self.lock:
            if self.thread is None or not self.thread.is_alive() or self.stopping.is_set():
                self.stopping = threading.Event()
                self.thread = threading.Thread(target=self._watch, args=(self.stopping,), name='ConvoLifecycle', daemon=True)
                self.thread.start()

    def _watch(self, stopping):
        while not stopping.wait(.25):
            if not enabled():
                self.disable(expected=stopping)
                return

    def register(self, key, callback):
        with self.lock:
            if self.disabling:
                raise RuntimeError('Convo is releasing its previous resources; retry after disable completes')
            self.callbacks[key] = callback
        self.watch()

    def forget(self, key):
        with self.lock:
            self.callbacks.pop(key, None)

    def install(self, key, operation, targets):
        with self.lock:
            if self.disabling or self.blocked or not enabled():
                return False
            if key in self.keys:
                return True
            before = [(obj, attr, getattr(obj, attr, MISSING)) for obj, attr in targets]
            try:
                result = operation()
            except BaseException:
                for obj, attr, value in reversed(before):
                    self._restore(obj, attr, value)
                raise
            changes = [(obj, attr, value, getattr(obj, attr, MISSING)) for obj, attr, value in before
                       if getattr(obj, attr, MISSING) is not value]
            if result is False:
                for obj, attr, value, _ in reversed(changes):
                    self._restore(obj, attr, value)
                return False
            self.patches.extend(changes)
            self.keys.add(key)
        self.watch()
        return result

    @staticmethod
    def _restore(obj, attr, value):
        if value is MISSING:
            if hasattr(obj, attr):
                delattr(obj, attr)
        else:
            setattr(obj, attr, value)

    def disable(self, expected=None):
        # Mark first; callbacks must invalidate epochs before cancelling workers.
        with self.lock:
            if self.disabling or (expected is not None and expected is not self.stopping):
                return
            self.disabling = True
            self.blocked = True
            self.stopping.set()
            callbacks, self.callbacks = list(self.callbacks.values()), {}
            patches, self.patches = self.patches, []
            self.keys.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                log.exception('Convo resource teardown failed; remaining resources still released')
        for obj, attr, previous, installed in reversed(patches):
            # Do not overwrite another plugin's later replacement.
            try:
                if getattr(obj, attr, MISSING) is installed:
                    self._restore(obj, attr, previous)
            except Exception:
                log.exception('Convo hook restoration failed; other hooks still released')
        with self.lock:
            self.disabling = False


def state():
    from helpers import runtime
    # Survives the host's plugin namespace purge. Initialization is serialized
    # with the import lock rather than a plugin-module lock that could be reloaded.
    import _imp
    _imp.acquire_lock()
    try:
        if not hasattr(runtime, '_convo_lifecycle'):
            runtime._convo_lifecycle = Lifecycle()
        # Cached readers and independently loaded adapters have different local
        # class identities. A lookup must never downgrade or mutate the shared
        # owner; only the fresh native ON boundary may upgrade released ownership.
        return runtime._convo_lifecycle
    finally:
        _imp.release_lock()


def owned_patch(key, specifications):
    """Track exact attributes, including flags/originals, for optional host adapters."""
    def decorate(operation):
        @functools.wraps(operation)
        def run():
            targets = []
            for module_name, attributes in specifications:
                try:
                    module = importlib.import_module(module_name)
                except ImportError:
                    continue
                for path in attributes:
                    obj = module
                    parts = path.split('.')
                    for part in parts[:-1]:
                        obj = getattr(obj, part, None)
                    if obj is not None:
                        targets.append((obj, parts[-1]))
            return state().install(key, operation, targets)
        return run
    return decorate
