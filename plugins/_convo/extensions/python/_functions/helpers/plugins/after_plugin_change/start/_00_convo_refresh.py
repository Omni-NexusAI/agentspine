"""Release live Convo ownership before the host drops its API/module caches."""
import _imp
import importlib
import sys

from helpers.extension import Extension


class ConvoRefresh(Extension):
    def execute(self, data=None, **kwargs):
        args = (data or {}).get('args', ())
        values = (data or {}).get('kwargs', {})
        names = values.get('plugin_names', args[0] if args else None)
        python_change = values.get('python_change', args[1] if len(args) > 1 else False)
        if not python_change or (names and '_convo' not in names):
            return

        # Do not create a lifecycle/service as a side effect of refreshing an
        # unloaded plugin. Its host-owned state and jobs survive namespace purge.
        from helpers import runtime
        control = getattr(runtime, '_convo_lifecycle', None)
        if control is not None:
            control.disable()

        # The host routes underscore names to `plugins`, even when installed
        # under usr/plugins. Purge this exact user namespace before cache clear.
        # Teardown runs outside the import lock: callbacks may themselves import.
        namespace = 'usr.plugins._convo'
        _imp.acquire_lock()
        try:
            names = [name for name in sys.modules
                     if name == namespace or name.startswith(namespace + '.')]
            for name in sorted(names, key=lambda value: value.count('.'), reverse=True):
                module = sys.modules.pop(name, None)
                parent_name, _, attribute = name.rpartition('.')
                parent = sys.modules.get(parent_name)
                if parent is not None and getattr(parent, attribute, None) is module:
                    delattr(parent, attribute)
            importlib.invalidate_caches()
        finally:
            _imp.release_lock()
        # Stay blocked. Only a completed explicit native ON may resume Convo.
