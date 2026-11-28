import importlib.util
import sys
import types
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'extensions/python/_functions/helpers/plugins/after_plugin_change/start/_00_convo_refresh.py'


class NamespaceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.host = types.ModuleType('helpers')
        self.runtime = types.ModuleType('helpers.runtime')
        self.host.runtime = self.runtime
        extension = types.ModuleType('helpers.extension')
        extension.Extension = object
        self.user = types.ModuleType('usr.plugins')
        self.plugin = types.ModuleType('usr.plugins._convo')
        self.child = types.ModuleType('usr.plugins._convo.helpers')
        self.plugin.helpers = self.child
        self.user._convo = self.plugin
        self.other = types.ModuleType('usr.plugins.other')
        self.modules = patch.dict(sys.modules, {'helpers': self.host, 'helpers.runtime': self.runtime,
            'helpers.extension': extension, 'usr.plugins': self.user, 'usr.plugins._convo': self.plugin,
            'usr.plugins._convo.helpers': self.child, 'usr.plugins.other': self.other,
            'usr.plugins._convo_extra': self.other})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location('_00_convo_refresh', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.extension = module.ConvoRefresh()

    def test_releases_before_purge_preserves_host_singletons_and_other_plugins(self):
        calls = []
        def disable():
            self.assertIn('usr.plugins._convo.helpers', sys.modules)
            calls.append('disable')
        control = types.SimpleNamespace(disable=disable)
        self.runtime._convo_lifecycle = control
        for name in ('_convo_service', '_convo_settings_lock', '_convo_gateway', '_convo_jobs'):
            setattr(self.runtime, name, object())
        before = vars(self.runtime).copy()
        self.extension.execute(data={'kwargs': {'plugin_names': ['_convo'], 'python_change': True}})
        self.assertEqual(calls, ['disable'])
        self.assertNotIn('usr.plugins._convo', sys.modules)
        self.assertNotIn('usr.plugins._convo.helpers', sys.modules)
        self.assertFalse(hasattr(self.user, '_convo'))
        self.assertIs(sys.modules['usr.plugins.other'], self.other)
        self.assertIs(sys.modules['usr.plugins._convo_extra'], self.other)
        self.assertEqual(vars(self.runtime), before)

    def test_unrelated_nonpython_and_off_toggle_refreshes_are_inert(self):
        self.runtime._convo_lifecycle = types.SimpleNamespace(disable=lambda: self.fail('Unexpected teardown'))
        for data in ({'args': [['_convo'], False]}, {'args': [['another'], True]}, {'kwargs': {'plugin_names': ['_convo']}}):
            self.extension.execute(data=data)
            self.assertIs(sys.modules['usr.plugins._convo'], self.plugin)

    def test_full_refresh_and_repeat_do_not_create_or_resume_resources(self):
        for _ in range(2):
            self.extension.execute(data={'args': [None, True]})
        self.assertFalse(hasattr(self.runtime, '_convo_lifecycle'))
        self.assertNotIn('usr.plugins._convo', sys.modules)

    def test_parent_attribute_replaced_by_another_owner_is_retained(self):
        sentinel = object()
        self.user._convo = sentinel
        self.extension.execute(data={'args': [[], True]})
        self.assertIs(self.user._convo, sentinel)

    def test_teardown_failure_does_not_purge_owned_modules(self):
        def fail():
            raise RuntimeError('Cannot safely release resources')
        self.runtime._convo_lifecycle = types.SimpleNamespace(disable=fail)
        with self.assertRaisesRegex(RuntimeError, 'safely release'):
            self.extension.execute(data={'args': [['_convo'], True]})
        self.assertIs(sys.modules['usr.plugins._convo'], self.plugin)

    def test_full_refresh_leaves_existing_control_blocked_without_rearming(self):
        control = types.SimpleNamespace(blocked=False, disable_count=0)
        def disable():
            control.blocked = True
            control.disable_count += 1
        control.disable = disable
        self.runtime._convo_lifecycle = control
        for _ in range(2):
            self.extension.execute(data={'kwargs': {'python_change': True}})
        self.assertTrue(control.blocked)
        self.assertEqual(control.disable_count, 2)
        self.assertIs(self.runtime._convo_lifecycle, control)


class NativeOnFreshnessTests(unittest.TestCase):
    setUp = NamespaceRefreshTests.setUp

    def load_resume(self):
        path = ROOT / 'extensions/python/_functions/helpers/plugins/toggle_plugin/end/_00_convo_resume.py'
        spec = importlib.util.spec_from_file_location('_00_convo_resume', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.__file__ = str(ROOT.parents[1] / 'usr/plugins/_convo/extensions/python/_functions/helpers/plugins/toggle_plugin/end/_00_convo_resume.py')
        return module.ConvoResume()

    def test_off_copy_on_purges_stale_user_helper_before_import(self):
        calls = []
        self.host.plugins = types.SimpleNamespace(get_toggle_state=lambda _: 'enabled')
        control = types.SimpleNamespace(lock=threading.RLock(), blocked=True, disabling=False)
        control.disable = lambda: calls.append('disable')
        self.runtime._convo_lifecycle = control
        service = types.SimpleNamespace(connection_lock=threading.RLock(), connections={}, dispatcher=object(), store=object())
        self.runtime._convo_service = service
        self.runtime._convo_settings_lock = object()
        old_state = vars(self.runtime).copy()
        # Native OFF/non-Python refresh leaves user modules cached on this host.
        self.extension.execute(data={'args': [['_convo'], False]})
        self.assertIs(sys.modules['usr.plugins._convo.helpers'], self.child)
        resume = self.load_resume()
        lifecycle = types.SimpleNamespace(state=lambda: control, enabled=lambda: True, Lifecycle=type(control))
        modules = {
            'usr.plugins._convo.helpers.lifecycle': lifecycle,
            'usr.plugins._convo.helpers.convo_service': types.SimpleNamespace(settings=lambda: {'enabled': True}),
            'usr.plugins._convo.helpers.managed_runtime': types.SimpleNamespace(configure=lambda cfg: calls.append(('configure', cfg))),
            'usr.plugins._convo.helpers.studio_proxy': types.SimpleNamespace(attach=lambda: calls.append('attach')),
        }
        def fresh_import(name):
            self.assertNotIn('usr.plugins._convo.helpers', sys.modules)
            self.assertEqual(calls[0], 'disable')
            return modules[name]
        with patch('importlib.import_module', side_effect=fresh_import):
            resume.execute(data={'kwargs': {'plugin_name': '_convo', 'enabled': True}})
        self.assertFalse(control.blocked)
        self.assertEqual(calls, ['disable', ('configure', {'enabled': True}), 'attach'])
        self.assertEqual(vars(self.runtime), old_state)
        self.assertIs(sys.modules['usr.plugins.other'], self.other)

    def test_real_stale_lifecycle_readers_survive_purge_and_fresh_on_upgrade(self):
        self.host.plugins = types.SimpleNamespace(get_toggle_state=lambda _: 'enabled')
        name = 'usr.plugins._convo.helpers.lifecycle'

        def load_lifecycle(module_name):
            spec = importlib.util.spec_from_file_location(module_name, ROOT / 'helpers/lifecycle.py')
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            # Exercise ownership synchronously, without a real watcher thread.
            module.Lifecycle.watch = lambda self: None
            return module

        old = load_lifecycle(name)
        self.child.lifecycle = old
        control = old.state()
        original_lock = control.lock
        owner = types.ModuleType('convo_lifecycle_owner_test')
        original = owner.call = lambda: 'native'
        sys.modules[owner.__name__] = owner

        @old.owned_patch('old-reader', [(owner.__name__, ['call', 'new_flag'])])
        def cached_adapter():
            owner.call = lambda: 'convo'
            owner.new_flag = True

        cached_adapter()
        self.assertEqual(owner.call(), 'convo')
        control.disable()
        self.assertIs(owner.call, original)
        self.assertFalse(hasattr(owner, 'new_flag'))
        alias = load_lifecycle('convo_lifecycle_alias_test')
        self.assertIs(alias.state(), control)
        self.assertIs(type(control), old.Lifecycle, 'Read-only lookup must not adopt even a released class')

        fresh_modules = []
        original_import = importlib.import_module

        def fresh_import(module_name):
            if module_name == name:
                self.assertNotIn(name, sys.modules)
                self.assertNotIn('usr.plugins._convo.helpers', sys.modules)
                self.assertTrue(control.blocked)
                module = load_lifecycle(name)
                self.assertIs(module.state(), control)
                self.assertIs(type(control), old.Lifecycle)
                fresh_modules.append(module)
                return module
            if module_name.endswith('.convo_service'):
                return types.SimpleNamespace(settings=lambda: {})
            if module_name.endswith('.managed_runtime'):
                return types.SimpleNamespace(configure=lambda cfg: None)
            if module_name.endswith('.studio_proxy'):
                return types.SimpleNamespace(attach=lambda: None)
            return original_import(module_name)

        with patch('importlib.import_module', side_effect=fresh_import):
            self.load_resume().execute(data={'args': ['_convo', True]})
        fresh = fresh_modules[0]
        self.assertIs(type(control), fresh.Lifecycle)
        self.assertIs(control.lock, original_lock)
        self.assertFalse(control.blocked)
        # Retained host imports and adapter aliases remain valid after real purge.
        for reader in (old, alias, fresh):
            self.assertIs(reader.state(), control)
            self.assertIs(type(control), fresh.Lifecycle)
        cached_adapter()
        self.assertEqual(owner.call(), 'convo')
        self.assertTrue(old.state().patches)
        old.state().disable()
        self.assertIs(owner.call, original)
        self.assertFalse(hasattr(owner, 'new_flag'), 'Fresh owner must restore its own MISSING sentinel')
        self.assertFalse(cached_adapter(), 'Cached adapters cannot resume ownership after OFF')
        self.assertIs(type(control), fresh.Lifecycle)

    def test_nonexplicit_on_and_busy_teardown_never_purge_or_import(self):
        self.host.plugins = types.SimpleNamespace(get_toggle_state=lambda _: 'enabled')
        resume = self.load_resume()
        with patch('importlib.import_module', side_effect=AssertionError('Unexpected import')):
            resume.execute(data={'args': ['_convo', False]})
            resume.execute(data={'args': ['other', True]})
            self.runtime._convo_lifecycle = types.SimpleNamespace(lock=threading.RLock(), disabling=True, disable=lambda: None)
            with self.assertRaisesRegex(RuntimeError, 'teardown'):
                resume.execute(data={'args': ['_convo', True]})
        self.assertIs(sys.modules['usr.plugins._convo.helpers'], self.child)

    def test_pending_connection_close_defers_purge(self):
        self.host.plugins = types.SimpleNamespace(get_toggle_state=lambda _: 'enabled')
        handler = types.SimpleNamespace(connections={'browser': object()})
        service = types.SimpleNamespace(connection_lock=threading.RLock(), connections={'old': (None, handler, 'browser')})
        self.runtime._convo_service = service
        self.runtime._convo_lifecycle = types.SimpleNamespace(lock=threading.RLock(), disabling=False,
            disable=lambda: service.connections.clear())
        with self.assertRaisesRegex(RuntimeError, 'connections'):
            self.load_resume().execute(data={'args': ['_convo', True]})
        self.assertIs(sys.modules['usr.plugins._convo.helpers'], self.child)

    def test_prior_off_retirement_defers_on_even_with_empty_active_registry(self):
        self.host.plugins = types.SimpleNamespace(get_toggle_state=lambda _: 'enabled')
        self.runtime._convo_service = types.SimpleNamespace(connection_lock=threading.RLock(),
            connections={}, retiring_connections={'pending': object()})
        self.runtime._convo_lifecycle = types.SimpleNamespace(lock=threading.RLock(), disabling=False, disable=lambda: None)
        with self.assertRaisesRegex(RuntimeError, 'connections'):
            self.load_resume().execute(data={'args': ['_convo', True]})
        self.assertIs(sys.modules['usr.plugins._convo.helpers'], self.child)

    def test_native_on_import_lock_order_matches_concurrent_lifecycle_upgrade(self):
        self.host.plugins = types.SimpleNamespace(get_toggle_state=lambda _: 'enabled')
        resume = self.load_resume()
        # load_resume uses an unregistered spec: the function globals own _imp.
        globals_ = resume.execute.__globals__
        import_lock, control_lock = threading.Lock(), threading.RLock()
        import_held, on_waiting, permit_upgrade = threading.Event(), threading.Event(), threading.Event()
        acquired_control, errors = [], []
        control = types.SimpleNamespace(lock=control_lock, disabling=False, disable=lambda: None)
        self.runtime._convo_lifecycle = control

        def acquire_import():
            if threading.current_thread().name == 'ConvoResumeRegression':
                on_waiting.set()
            import_lock.acquire()

        proxy = types.SimpleNamespace(acquire_lock=acquire_import, release_lock=import_lock.release)

        def upgrade():
            # Same ordering as the explicit fresh native ON class upgrade.
            with import_lock:
                import_held.set()
                if not permit_upgrade.wait(2):
                    errors.append('ON never reached its import-lock boundary')
                    return
                acquired = control_lock.acquire(timeout=.5)
                acquired_control.append(acquired)
                control.disabling = True  # stop ON safely before any import/start
                if acquired:
                    control_lock.release()

        def native_on():
            try:
                resume.execute(data={'args': ['_convo', True]})
            except RuntimeError as error:
                if 'teardown' not in str(error):
                    errors.append(str(error))
            except BaseException as error:
                errors.append(type(error).__name__)

        with patch.dict(globals_, {'_imp': proxy}):
            upgrader = threading.Thread(target=upgrade, name='ConvoUpgradeRegression')
            on = threading.Thread(target=native_on, name='ConvoResumeRegression')
            upgrader.start()
            self.assertTrue(import_held.wait(1))
            on.start()
            self.assertTrue(on_waiting.wait(1))
            permit_upgrade.set()
            upgrader.join(2)
            on.join(2)
        self.assertFalse(upgrader.is_alive())
        self.assertFalse(on.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(acquired_control, [True], 'ON held control while waiting for import lock')


if __name__ == '__main__':
    unittest.main()
