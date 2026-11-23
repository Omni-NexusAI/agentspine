"""One conversation, owned by one page, surviving a dropped connection for a short grace (api/realtime.py, helpers/convo_ownership.py).

The real handler on the real journal with stand-in host and providers: a page re-attaches within the grace and the conversation goes
on under a new epoch; after the grace it stops; an unknown conversation or Convo turned off refuses at once; a start without a page id
keeps today's immediate stop; another page sees who owns it and takes it over through the existing shutdown, and two take-overs at once
have one winner. No socket, browser or model is involved."""
import asyncio
import importlib
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from plugins._convo.helpers import convo_ownership, convo_policy
from plugins._convo.helpers.convo_contract import validate_settings
from plugins._convo.helpers.convo_host import Compactor
from plugins._convo.helpers.convo_store import Store

PHONE, LAPTOP = 'page-phone-0001', 'page-laptop-0001'


class WsStub:
    def __init__(self, *args, **kwargs): self.sent = []
    async def emit_to(self, sid, event, payload): self.sent.append((sid, event, payload))


class HostStub:
    def context(self, target):
        if target not in {'a', 'b'}: raise ValueError('Unknown target')
    async def persona(self, target, style, cache): return 'Calm voice'
    async def summarize(self, target, source): return 'Older context'


class SidecarStub:
    def __init__(self, config): self.calls = []
    async def health(self): return {'service': 'convo', 'protocol': 1}


class Ownership(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = Store(Path(self.tmp.name) / 'db')
        self.connections = []
        self.service = types.SimpleNamespace(store=self.store, host=HostStub(), persona_cache={},
                                             dispatcher=types.SimpleNamespace(start=lambda: None), compactor=Compactor(self.store, HostStub()),
                                             register_connection=lambda handler, sid: self.connections.append(('register', sid)),
                                             forget_connection=lambda handler, sid, completed=True: self.connections.append(('forget', sid, completed)))
        self.config = validate_settings({'enabled': True, 'sidecar': {'managed': False},
                                         'roles': {'classifier': {'url': 'http://localhost/v1', 'model': 'classifier'}, 'conversation': {'model': 'conversation'}}})
        module_stub = types.ModuleType('helpers.ws'); module_stub.WsHandler = WsStub
        with patch.dict(sys.modules, {'helpers.ws': module_stub}):
            self.module = importlib.import_module('plugins._convo.api.realtime')
        self.patch = patch.object(self.module, 'modules', return_value=(types.SimpleNamespace(__package__='plugins._convo.helpers', settings=lambda: self.config,
                                  service=lambda: self.service), convo_policy, types.SimpleNamespace(Sidecar=SidecarStub)))
        self.patch.start(); self.handler = self.module.Realtime()
        models = patch.object(importlib.import_module('plugins._convo.helpers.convo_models'), 'state', new=AsyncMock(return_value=''))
        models.start(); self.addCleanup(models.stop)

    async def asyncTearDown(self):
        for sid in list(self.handler.connections):
            await self.handler._end(sid)
        for page, entry in list(self.handler.detached.items()):
            await self.handler._expire(page, entry, now=True)
        self.patch.stop(); self.store.close(); self.tmp.cleanup()

    async def start(self, sid, page=PHONE, device='Chrome on Android', **more):
        return await self.handler.process('convo.start', {'target': 'a', 'page': page, 'device': device, **more}, sid)

    def active(self):
        return [dict(row) for row in self.store.db.execute('SELECT id, owner, epoch FROM sessions WHERE active=1')]

    async def test_the_owner_is_recorded_in_a_new_table_and_older_readers_see_the_same_sessions(self):
        started = await self.start('phone')
        owner = convo_ownership.owner(self.store)
        self.assertEqual((owner['session'], owner['page'], owner['device'], owner['target']), (started['session']['id'], PHONE, 'Chrome on Android', 'a'))
        self.assertIsInstance(owner['since'], float)
        columns = [row[1] for row in self.store.db.execute('PRAGMA table_info(sessions)')]
        self.assertEqual(columns, ['id', 'owner', 'target', 'epoch', 'active', 'generation'], 'the existing table is unchanged')
        old = sqlite3.connect(str(Path(self.tmp.name) / 'db'))                # an older Convo reads the sessions it always read
        self.assertEqual(old.execute('SELECT owner FROM sessions WHERE active=1').fetchone()[0], 'phone')
        old.close()
        self.assertEqual(convo_ownership.device_label('Chrome\x00 on\n Android' + 'x' * 90)[:17], 'Chrome on Android')
        self.assertEqual(convo_ownership.page_id('../../etc'), '')

    async def test_a_dropped_connection_reattaches_within_the_grace_under_a_new_epoch(self):
        started = await self.start('phone')
        session = started['session']
        await self.handler.on_disconnect('phone')                           # the phone slept
        self.assertNotIn('phone', self.handler.connections)
        self.assertEqual(len(self.active()), 1, 'kept for the grace')
        self.assertIn(('forget', 'phone', True), self.connections, 'the dropped connection is still released')
        result = await self.handler.process('convo.attach', {'session_id': session['id'], 'epoch': session['epoch'], 'page': PHONE}, 'phone-again')
        self.assertTrue(result['ok'])
        self.assertEqual(result['session']['id'], session['id'])
        self.assertGreater(result['session']['epoch'], session['epoch'], 'anything addressed to the old connection is stale')
        self.assertEqual(self.active()[0]['owner'], 'phone-again')
        self.assertIn('phone-again', self.handler.connections)
        self.assertEqual(self.handler.detached, {})
        stale = await self.handler.process('convo.interrupt', {'session_id': session['id'], 'epoch': session['epoch']}, 'phone-again')
        self.assertEqual(stale, {'ok': False, 'stale': True})

    async def test_the_grace_ends_the_conversation(self):
        await self.start('phone')
        with patch.object(convo_ownership, 'GRACE_SECONDS', .05):
            await self.handler.on_disconnect('phone')
            await asyncio.sleep(.2)
        self.assertEqual(self.active(), [])
        self.assertEqual(self.handler.detached, {})

    async def test_an_unknown_conversation_or_convo_turned_off_refuses_at_once(self):
        started = await self.start('phone')
        session = started['session']
        await self.handler.on_disconnect('phone')
        wrong = await self.handler.process('convo.attach', {'session_id': 'nope', 'page': PHONE}, 'x')
        self.assertEqual((wrong['ok'], wrong['unknown']), (False, True))
        other = await self.handler.process('convo.attach', {'session_id': session['id'], 'page': LAPTOP}, 'x')
        self.assertFalse(other['ok'], 'another page cannot claim it')
        self.config['enabled'] = False
        off = await self.handler.process('convo.attach', {'session_id': session['id'], 'page': PHONE}, 'phone-again')
        self.assertFalse(off['ok'])
        self.assertEqual(self.active(), [], 'plugin off stops at once')

    async def test_without_a_page_id_a_dropped_connection_stops_at_once_as_before(self):
        await self.handler.process('convo.start', {'target': 'a'}, 'old-browser')
        await self.handler.on_disconnect('old-browser')
        self.assertEqual(self.active(), [])
        self.assertEqual(self.handler.detached, {})

    async def test_reattach_before_the_server_noticed_the_drop(self):
        session = (await self.start('phone'))['session']
        result = await self.handler.process('convo.attach', {'session_id': session['id'], 'page': PHONE}, 'phone-again')
        self.assertTrue(result['ok'])
        self.assertNotIn('phone', self.handler.connections)
        await self.handler.on_disconnect('phone')                           # the old connection's late disconnect changes nothing
        self.assertEqual(self.active()[0]['owner'], 'phone-again')

    async def test_another_page_is_told_who_owns_it_and_takes_over_through_the_existing_shutdown(self):
        session = (await self.start('phone'))['session']
        refused = await self.start('laptop', page=LAPTOP, device='Agent Zero app on Windows')
        self.assertEqual((refused['ok'], refused['refused']), (False, True))
        self.assertIn('Voice is running on Chrome on Android', refused['message'])
        taken = await self.start('laptop', page=LAPTOP, device='Agent Zero app on Windows', takeover=session['id'])
        self.assertTrue(taken['ok'])
        self.assertNotIn('phone', self.handler.connections)
        self.assertIn(('phone', 'convo.taken_over', {'session_id': session['id'], 'device': 'Agent Zero app on Windows'}), self.handler.sent)
        self.assertEqual([row['owner'] for row in self.active()], ['laptop'])
        # The loser never gets it back by itself: its re-attach is refused.
        back = await self.handler.process('convo.attach', {'session_id': session['id'], 'page': PHONE}, 'phone-again')
        self.assertFalse(back['ok'])

    async def test_two_takeovers_at_once_have_one_winner(self):
        session = (await self.start('phone'))['session']
        first, second = await asyncio.gather(
            self.start('laptop', page=LAPTOP, device='Edge on Windows', takeover=session['id']),
            self.start('tablet', page='page-tablet-0001', device='Chrome on Android', takeover=session['id']))
        outcomes = sorted([bool(first.get('ok')), bool(second.get('ok'))])
        self.assertEqual(outcomes, [False, True])
        loser = first if not first.get('ok') else second
        self.assertIn('just taken over', loser['message'])
        self.assertEqual(len(self.active()), 1)

    async def test_a_detached_conversation_is_taken_over_and_the_same_page_may_start_anew(self):
        session = (await self.start('phone'))['session']
        await self.handler.on_disconnect('phone')
        taken = await self.start('laptop', page=LAPTOP, takeover=session['id'])
        self.assertTrue(taken['ok'])
        self.assertEqual(self.handler.detached, {})
        await self.handler._end('laptop')
        again = await self.start('laptop2', page=LAPTOP)
        self.assertTrue(again['ok'])
        await self.handler.on_disconnect('laptop2')
        fresh = await self.start('laptop3', page=LAPTOP)                     # a reload of the same page ends its detached one first
        self.assertTrue(fresh['ok'])
        self.assertEqual([row['owner'] for row in self.active()], ['laptop3'])

    async def slow_turn(self, sid, seconds):
        """A reply task that takes `seconds` to wind down when cancelled; its cleanup records whether it was still current."""
        state = self.handler.connections[sid]
        epoch = state['session']['epoch']
        seen = {}

        async def turn():
            try:
                await asyncio.Event().wait()
            finally:
                seen['current'] = self.handler._current(sid, state, epoch)
                await asyncio.shield(asyncio.sleep(seconds))
        state['task'] = asyncio.create_task(turn())
        await asyncio.sleep(0)
        return state, seen

    async def test_a_reattach_while_the_old_connection_is_still_releasing_waits_for_it(self):
        """Review 2026-11-17 #3: the detached entry is registered before the release is awaited, and the attach waits for it."""
        session = (await self.start('phone'))['session']
        await self.slow_turn('phone', .5)
        dropping = asyncio.create_task(self.handler.on_disconnect('phone'))
        await asyncio.sleep(.05)                                            # the release is in progress
        self.assertIn(PHONE, self.handler.detached)
        result = await self.handler.process('convo.attach', {'session_id': session['id'], 'page': PHONE}, 'phone-again')
        self.assertTrue(result['ok'], result)
        self.assertTrue(self.handler.detached.get(PHONE) is None)
        await dropping
        self.assertEqual(self.active()[0]['owner'], 'phone-again')

    async def test_a_dropped_connections_cancelled_turn_is_no_longer_current(self):
        """Review 2026-11-17 #4: the epoch moves on before the turn is cancelled, so its cleanup sends nothing and compacts nothing."""
        await self.start('phone')
        _, seen = await self.slow_turn('phone', 0)
        await self.handler.on_disconnect('phone')
        self.assertIs(seen['current'], False)
        # The same for a re-attach before the server noticed the drop (the old connection's turn is cancelled there).
        await self.handler._expire(PHONE, self.handler.detached[PHONE], now=True)
        session = (await self.start('phone2'))['session']
        _, seen = await self.slow_turn('phone2', 0)
        result = await self.handler.process('convo.attach', {'session_id': session['id'], 'page': PHONE}, 'phone3')
        self.assertTrue(result['ok'])
        self.assertIs(seen['current'], False)

    async def test_a_reattach_is_not_refused_by_its_own_old_connection_still_retiring(self):
        from plugins._convo.helpers import convo_service
        service = object.__new__(convo_service.Service)
        service.connection_lock, service.connections, service.disabled = threading.RLock(), {}, False
        handler = object()
        service.retiring_connections = {(id(handler), 'phone'): None}
        with self.assertRaises(ValueError):
            service.register_connection(handler, 'phone-again')             # a plain registration still waits for every retirement
        service.register_connection(handler, 'phone-again', replacing='phone')
        self.assertIn((id(handler), 'phone-again'), service.connections)
        service.retiring_connections[(id(handler), 'other')] = None
        with self.assertRaises(ValueError):
            service.register_connection(handler, 'phone-again', replacing='phone')   # another retiring connection still refuses
        service.disabled, service.retiring_connections = True, {}
        with self.assertRaises(ValueError):
            service.register_connection(handler, 'phone-again', replacing='phone')

    async def test_a_reattach_keeps_the_whisper_worker_and_a_transcript_in_flight(self):
        """Review 2026-11-17 #8: the watcher follows the conversation, so a re-attach neither reloads the worker nor drops the
        transcript of a turn that was being written down."""
        class Whisper:
            def __init__(self): self.state, self.stops, self.calls = 'off', 0, 0
            async def ensure(self): self.state = 'ready'
            def status(self): return {'state': self.state, 'detail': '', 'model': 'base'}
            def stop(self): self.state, self.stops = 'off', self.stops + 1
            async def transcribe(self, audio, wait=0.0, timeout=60):
                self.calls += 1
                await asyncio.sleep(.3)
                return {'text': 'What is the weather'}
        whisper = self.service.whisper = Whisper()
        self.config['transcription_enabled'] = True
        session = (await self.start('phone', mode='active'))['session']
        state = self.handler.connections['phone']
        await asyncio.sleep(.05)
        watch = state['whisper_watch']
        self.store.accept_turn(session['id'], 'phone', session['epoch'], 'tw', {'transcript': ''})
        self.handler._whisper_later('phone', state, dict(session), 'tw', 'UklGRg==')
        await asyncio.sleep(.05)                                            # the transcript is being written
        await self.handler.on_disconnect('phone')
        result = await self.handler.process('convo.attach', {'session_id': session['id'], 'page': PHONE}, 'phone-again')
        self.assertTrue(result['ok'])
        await asyncio.sleep(1.5)
        self.assertIs(state['whisper_watch'], watch)
        self.assertFalse(watch.done())
        self.assertEqual((whisper.state, whisper.stops), ('ready', 0), 'no reload')
        turn = [t['content'] for t in self.store.context('a')['turns'] if t['id'] == 'tw'][0]
        self.assertEqual(turn.get('whisper'), 'What is the weather')
        self.assertTrue(any(sid == 'phone-again' and payload.get('type') == 'transcript.saved' for sid, _, payload in self.handler.sent))
        await self.handler._end('phone-again')
        await asyncio.sleep(1.2)
        self.assertEqual(whisper.state, 'off', 'the worker ends with the conversation')

    async def test_stop_and_disable_end_at_once_without_a_grace(self):
        session = (await self.start('phone'))['session']
        await self.handler.process('convo.stop', {'session_id': session['id']}, 'phone')
        self.assertEqual((self.active(), self.handler.detached), ([], {}))
        await self.start('phone2')
        await self.handler.disable('phone2')
        self.assertEqual((self.active(), self.handler.detached), ([], {}))


if __name__ == '__main__':
    unittest.main()
