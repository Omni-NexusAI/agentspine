"""Permit a new session only after the native ON transaction has completed."""
import importlib
import _imp
import sys
from contextlib import ExitStack
from pathlib import Path
from helpers.extension import Extension


class ConvoResume(Extension):
    def execute(self, data=None, **kwargs):
        args = (data or {}).get('args', ())
        values = (data or {}).get('kwargs', {})
        if values.get('plugin_name', args[0] if args else None) != '_convo':
            return
        if values.get('enabled', args[1] if len(args) > 1 else None) is not True:
            return
        from helpers import plugins, runtime
        if plugins.get_toggle_state('_convo') != 'enabled':
            return
        previous = getattr(runtime, '_convo_lifecycle', None)
        current_service = getattr(runtime, '_convo_service', None)
        previous_connections = []
        if current_service is not None:
            with current_service.connection_lock:
                previous_connections = list(current_service.connections.values())
                # Seed retirement before calling even an older teardown method,
                # which may clear the active registry before its async callback.
                if not hasattr(current_service, 'retiring_connections'):
                    current_service.retiring_connections = {}
                current_service.retiring_connections.update(current_service.connections)
        if previous is not None:
            previous.disable()
        # Fresh lifecycle upgrades below use the same import -> control order.
        # Teardown callbacks above run outside every import/control lock.
        _imp.acquire_lock()
        try:
            with ExitStack() as locks:
                if previous is not None:
                    locks.enter_context(previous.lock)
                    if previous.disabling:
                        raise RuntimeError('Convo teardown is still running; retry native ON after it completes')
                if current_service is not None:
                    locks.enter_context(current_service.connection_lock)
                    if (current_service.connections or current_service.retiring_connections
                            or any(sid in getattr(handler, 'connections', {}) for _, handler, sid in previous_connections)):
                        raise RuntimeError('Convo connections are still releasing; retry native ON after owning-loop cleanup acknowledges completion')
                # An OFF plugin's refresh extension is not invoked by the host.
                # Explicit ON is therefore the final freshness boundary. Never
                # purge shared providers or discard host-owned service/job state.
                namespace = 'usr.plugins._convo'
                names = [name for name in sys.modules if name == namespace or name.startswith(namespace + '.')]
                for name in sorted(names, key=lambda value: value.count('.'), reverse=True):
                    module = sys.modules.pop(name, None)
                    parent_name, _, attribute = name.rpartition('.')
                    parent = sys.modules.get(parent_name)
                    if parent is not None and getattr(parent, attribute, None) is module:
                        delattr(parent, attribute)
                importlib.invalidate_caches()
        finally:
            _imp.release_lock()
        root = next(p for p in Path(__file__).resolve().parents if p.name == '_convo')
        prefix = 'usr.plugins' if root.parent.parent.name == 'usr' else 'plugins'
        lifecycle = importlib.import_module(prefix + '._convo.helpers.lifecycle')
        control = lifecycle.state()
        _imp.acquire_lock()
        try:
            with control.lock:
                if not lifecycle.enabled() or control.disabling:
                    return
                if type(control) is not lifecycle.Lifecycle:
                    # Only this fresh ON boundary owns code adoption. Cached
                    # modules remain readers of this same host-owned object.
                    # Empty ownership is essential: old patch records contain
                    # their old module's MISSING sentinel.
                    if not control.blocked or control.patches or control.callbacks or control.keys:
                        raise RuntimeError('Convo ownership must be released before refreshing its lifecycle')
                    control.__class__ = lifecycle.Lifecycle
                control.blocked = False
        finally:
            _imp.release_lock()
        service = importlib.import_module(prefix + '._convo.helpers.convo_service')
        importlib.import_module(prefix + '._convo.helpers.managed_runtime').configure(service.settings())
        importlib.import_module(prefix + '._convo.helpers.studio_proxy').attach()
