import tempfile
import asyncio
import types
import unittest
from pathlib import Path
from concurrent.futures import Future
from contextlib import closing
from unittest.mock import patch

from plugins._convo.helpers import lifecycle
from plugins._convo.helpers.convo_host import Dispatcher
from plugins._convo.helpers.convo_store import Store
from plugins._convo.helpers.convo_service import Service


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.control = lifecycle.Lifecycle()
        self.enabled = patch.object(lifecycle, 'enabled', return_value=True)
        self.enabled.start()
        self.addCleanup(self.enabled.stop)
        self.addCleanup(self.control.disable)

    def test_repeated_disable_enable_restores_exact_functions_and_flags(self):
        original = lambda: 'native'
        owner = types.SimpleNamespace(call=original)
        for _ in range(3):
            # Only the completed native ON transaction grants this transition.
            self.control.blocked = False
            replacement = lambda: 'convo'
            def install():
                owner.call = replacement; owner.flag = True
            self.control.install('adapter', install, [(owner,'call'), (owner,'flag')])
            self.assertEqual(owner.call(), 'convo')
            self.control.disable(); self.control.disable()
            self.assertIs(owner.call, original)
            self.assertFalse(hasattr(owner,'flag'))

    def test_adapter_install_cannot_resume_blocked_ownership(self):
        self.control.disable()
        owner = types.SimpleNamespace(call='native')
        self.assertFalse(self.control.install('late', lambda: setattr(owner, 'call', 'late'), [(owner, 'call')]))
        self.assertEqual(owner.call, 'native')
        self.assertTrue(self.control.blocked)

    def test_failed_install_rolls_back_and_other_owners_are_preserved(self):
        owner = types.SimpleNamespace(call='native')
        def broken():
            owner.call = 'partial'
            raise ValueError('injected failure')
        with self.assertRaises(ValueError):
            self.control.install('bad', broken, [(owner,'call')])
        self.assertEqual(owner.call, 'native')
        self.control.install('ok', lambda: setattr(owner, 'call', 'convo'), [(owner,'call')])
        owner.call = 'another plugin'
        self.control.disable()
        self.assertEqual(owner.call, 'another plugin')

    def test_one_broken_cleanup_does_not_prevent_other_resources_releasing(self):
        released = []
        self.control.register('bad', lambda: (_ for _ in ()).throw(ValueError('injected cleanup failure')))
        self.control.register('good', lambda: released.append(True))
        with self.assertLogs('_convo', level='ERROR'):
            self.control.disable()
        self.assertEqual(released, [True])

    def test_disable_pauses_new_jobs_without_cancelling_running_jobs_or_losing_history(self):
        with tempfile.TemporaryDirectory() as tmp, closing(Store(Path(tmp) / 'db')) as store:
            session = store.start('browser','chat')
            host = types.SimpleNamespace(available=lambda _:True, begin=lambda _:Future(), log=lambda *a:None)
            dispatcher = Dispatcher(store, host)
            first = store.submit(session['id'],'browser',0,'t','one','First task')
            dispatcher.tick()
            second = store.submit(session['id'],'browser',0,'t','two','Second task')
            dispatcher.pause(); dispatcher.tick()
            self.assertEqual(store.job(second['id'])['status'],'queued')
            self.assertFalse(dispatcher.running[first['id']].cancelled())
            dispatcher.running[first['id']].set_result('Finished during disable')
            dispatcher.tick()
            self.assertEqual(store.job(first['id'])['status'],'completed')
            dispatcher.paused.clear(); dispatcher.tick()
            self.assertEqual(store.job(second['id'])['status'],'running')


class ServiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_disable_invalidates_journal_before_async_connection_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = Service(root=tmp, host=types.SimpleNamespace())
            try:
                session = service.store.start('browser','chat')
                service.store.accept_turn(session['id'],'browser',0,'turn',{'transcript':'Keep this history'})
                released = asyncio.Event()
                class Handler:
                    async def disable(self, sid):
                        self_sid = sid
                        if self_sid == 'browser': released.set()
                handler = Handler()
                service.register_connection(handler,'browser')
                await asyncio.to_thread(service.deactivate)
                self.assertTrue(service.dispatcher.paused.is_set())
                with self.assertRaises(ValueError): service.store.session(session['id'],'browser')
                await asyncio.wait_for(released.wait(),1)
                self.assertTrue(service.store.events('chat'))
                self.assertEqual(service.connections,{})
            finally: service.store.close()
