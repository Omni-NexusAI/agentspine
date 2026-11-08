"""Owning-loop retirement races, using temporary journals and no host services."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from plugins._convo.helpers import convo_service


class ServiceRetirementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service = convo_service.Service(root=temporary.name, host=types.SimpleNamespace())
        self.addCleanup(self.service.store.close)
        self.service.dispatcher.pause = Mock()
        self.session = self.service.store.start('browser', 'target')
        host = types.ModuleType('helpers')
        self.runtime = types.ModuleType('helpers.runtime')
        host.runtime = self.runtime
        host.plugins = types.SimpleNamespace(get_toggle_state=lambda _: 'enabled')
        extension = types.ModuleType('helpers.extension')
        extension.Extension = object
        self.runtime._convo_service = self.service
        self.runtime._convo_lifecycle = types.SimpleNamespace(lock=threading.RLock(), blocked=True,
            disabling=False, disable=lambda: None)
        modules = patch.dict(sys.modules, {'helpers': host, 'helpers.runtime': self.runtime, 'helpers.extension': extension})
        modules.start()
        self.addCleanup(modules.stop)
        path = Path(__file__).resolve().parents[1] / 'extensions/python/_functions/helpers/plugins/toggle_plugin/end/_00_convo_resume.py'
        spec = importlib.util.spec_from_file_location('convo_retirement_resume_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.resume = module.ConvoResume()

    def handler(self, *, fail=False, acknowledge=True, delayed_send=False):
        service = self.service
        class Handler:
            def __init__(self):
                self.connections = {'browser': object()}
                self.entered, self.release, self.acknowledged, self.send_release = (asyncio.Event() for _ in range(4))

            async def disable(self, sid):
                self.entered.set()
                await self.release.wait()
                if fail:
                    raise RuntimeError('mock callback failure')
                self.connections.pop(sid, None)
                if acknowledge:
                    service.forget_connection(self, sid, completed=True)
                    self.acknowledged.set()
                if delayed_send:
                    await self.send_release.wait()

        result = Handler()
        self.service.register_connection(result, 'browser')
        return result

    def assert_on_blocked(self):
        with patch('importlib.import_module', side_effect=AssertionError('ON must not import/start new resources')):
            with self.assertRaisesRegex(RuntimeError, 'connections.*releasing'):
                self.resume.execute(data={'args': ['_convo', True]})

    async def test_off_then_immediate_on_before_owning_loop_runs_remains_blocked(self):
        handler = self.handler()
        key = (id(handler), 'browser')
        dispatcher, store = self.service.dispatcher, self.service.store
        self.service.deactivate()
        self.assertFalse(handler.entered.is_set())  # owning loop has not yielded
        self.assertEqual(self.service.connections, {})
        self.assertIn(key, self.service.retiring_connections)
        self.assert_on_blocked()
        self.assertFalse(handler.entered.is_set())
        await asyncio.wait_for(handler.entered.wait(), 1)
        task = self.service._retirement_tasks[key]
        handler.release.set()
        await asyncio.wait_for(task, 1)
        self.assertFalse(self.service.retiring_connections)
        self.assertIs(self.service.dispatcher, dispatcher)
        self.assertIs(self.service.store, store)
        self.assertEqual(store.db.execute('SELECT active FROM sessions WHERE id=?', (self.session['id'],)).fetchone()['active'], 0)

    async def test_acknowledgment_waits_for_scheduled_callback_final_send(self):
        handler = self.handler(delayed_send=True)
        key = (id(handler), 'browser')
        self.service.deactivate()
        await asyncio.wait_for(handler.entered.wait(), 1)
        task = self.service._retirement_tasks[key]
        handler.release.set()
        await asyncio.wait_for(handler.acknowledged.wait(), 1)
        self.assertIn(key, self.service.retiring_connections)
        self.assert_on_blocked()
        handler.send_release.set()
        await asyncio.wait_for(task, 1)
        self.assertFalse(self.service.retiring_connections)

    async def test_callback_failure_or_missing_ack_is_not_completion(self):
        for fail in (True, False):
            handler = self.handler(fail=fail, acknowledge=False)
            key = (id(handler), 'browser')
            self.service.deactivate()
            await asyncio.wait_for(handler.entered.wait(), 1)
            task = self.service._retirement_tasks[key]
            handler.release.set()
            await asyncio.wait_for(task, 1)
            self.assertIn(key, self.service.retiring_connections)
            self.assertEqual(self.service.retirement_failures[key], 'cleanup_failed' if fail else 'cleanup_not_acknowledged')
            self.assert_on_blocked()
            # Explicit test-only terminal acknowledgment permits the next case.
            self.service.forget_connection(handler, 'browser', completed=True)
            self.service.disabled = False

    async def test_cancelled_cleanup_retains_retirement_until_explicit_ack(self):
        handler = self.handler()
        key = (id(handler), 'browser')
        self.service.deactivate()
        await asyncio.wait_for(handler.entered.wait(), 1)
        task = self.service._retirement_tasks[key]
        task.cancel()
        await asyncio.wait_for(task, 1)
        self.assertIn(key, self.service.retiring_connections)
        self.assertEqual(self.service.retirement_failures[key], 'cleanup_cancelled')
        self.assert_on_blocked()

    async def test_incomplete_disconnect_ack_preserves_failed_cleanup(self):
        handler = self.handler()
        key = (id(handler), 'browser')
        self.service.forget_connection(handler, 'browser', completed=False)
        self.assertNotIn(key, self.service.connections)
        self.assertIn(key, self.service.retiring_connections)
        self.assertEqual(self.service.retirement_failures[key], 'cleanup_not_completed')
        with self.assertRaisesRegex(ValueError, 'still releasing'):
            self.service.start_session('second-browser', 'target')

    async def test_old_service_without_retirement_attributes_initializes_without_losing_connections(self):
        for name in ('retiring_connections', 'retirement_failures', '_retirement_scheduled', '_retirement_acknowledged', '_retirement_tasks'):
            delattr(self.service, name)
        handler = self.handler()
        key = (id(handler), 'browser')
        self.service.deactivate()
        self.assertIn(key, self.service.retiring_connections)
        await asyncio.wait_for(handler.entered.wait(), 1)
        task = self.service._retirement_tasks[key]
        handler.release.set()
        await asyncio.wait_for(task, 1)
        self.assertFalse(self.service.retiring_connections)

    async def test_closed_owning_loop_never_implies_cleanup(self):
        loop = asyncio.new_event_loop()
        loop.close()
        handler = types.SimpleNamespace(disable=Mock())
        key = (id(handler), 'browser')
        self.service.connections[key] = (loop, handler, 'browser')
        self.service.deactivate()
        self.assertEqual(self.service.retirement_failures[key], 'owning_loop_closed')
        self.assert_on_blocked()
        handler.disable.assert_not_called()


if __name__ == '__main__':
    unittest.main()
