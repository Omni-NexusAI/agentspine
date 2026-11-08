"""Exercise the real plugin state machine with isolated host/provider doubles."""
import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from plugins._convo.helpers import convo_policy
from plugins._convo.helpers.convo_contract import merge, validate_settings
from plugins._convo.helpers.convo_host import Compactor
from plugins._convo.helpers.convo_store import Store


class WsStub:
    def __init__(self, *args, **kwargs): self.sent = []
    async def emit_to(self, sid, event, payload): self.sent.append((sid, event, payload))


class HostStub:
    def context(self, target):
        if target not in {'a', 'b'}: raise ValueError('Unknown target')
    async def persona(self, target, style, cache): return 'Calm voice'
    async def summarize(self, target, source): return 'Older context'
    def freeze_speech(self, role): return role
    async def direct(self, target, name, args): return 'Search result'


class SidecarStub:
    decision = {'speech': 'speak', 'action': 'none', 'confidence': .99, 'explicit_request': True, 'context_note': 'User asked a question'}
    proposal = None
    def __init__(self, config): self.calls = []; self.block = None; self.entered = asyncio.Event()
    async def health(self): return {'service':'convo', 'protocol':1}
    async def policy(self, payload):
        self.calls.append('policy'); self.entered.set()
        if self.block: await self.block.wait()
        return dict(self.decision)
    async def answer(self, payload):
        self.calls.append('answer')
        yield {'type':'text.delta', 'text':'A complete answer.'}
        if self.proposal: yield self.proposal
        yield {'type':'answer.done'}
    async def speech(self, role, text): self.calls.append('speech'); return b'pretend-wave'


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = Store(Path(self.tmp.name) / 'db')
        self.host = HostStub(); self.dispatches = []
        self.service = types.SimpleNamespace(store=self.store, host=self.host, persona_cache={},
            dispatcher=types.SimpleNamespace(start=lambda: self.dispatches.append('wake')), compactor=Compactor(self.store, self.host))
        self.config = validate_settings({'enabled': True, 'roles': {'classifier': {'url':'http://localhost/v1','model':'classifier'}, 'conversation': {'model':'conversation'}}})
        module_stub = types.ModuleType('helpers.ws'); module_stub.WsHandler = WsStub
        with patch.dict(sys.modules, {'helpers.ws': module_stub}):
            # Do not leave host stubs behind for any neighboring tests.
            self.module = importlib.import_module('plugins._convo.api.realtime')
        self.patch = patch.object(self.module, 'modules', return_value=(types.SimpleNamespace(__package__='plugins._convo.helpers', settings=lambda:self.config, service=lambda:self.service), convo_policy, types.SimpleNamespace(Sidecar=SidecarStub)))
        self.patch.start(); self.handler = self.module.Realtime()
        response = await self.handler.process('convo.start', {'target':'a'}, 'browser')
        self.session = response['session']; self.ids = {'session_id':self.session['id'], 'epoch':self.session['epoch']}

    async def asyncTearDown(self):
        await self.handler.on_disconnect('browser'); self.patch.stop(); self.store.close(); self.tmp.cleanup()

    async def turn(self, name='t'):
        await self.handler.process('convo.turn', {**self.ids,'turn_id':name,'transcript':'Question'}, 'browser')
        task = self.handler.connections['browser']['task']
        await task
        return self.handler.connections['browser']

    async def test_silent_ambient_is_not_retained_and_never_generates(self):
        state = self.handler.connections['browser']; state['sidecar'].decision = {'speech':'silent','action':'none','confidence':.99}
        await self.turn()
        self.assertEqual(self.store.events('a'), [])
        self.assertEqual(state['sidecar'].calls, ['policy'])

    async def test_generated_text_is_provisional_until_playback_completion(self):
        state = await self.turn()
        self.assertNotIn('assistant', self.store.context('a')['turns'][0]['content'])
        phrase = next(iter(state['phrases']))
        await self.handler.process('convo.playback', {**self.ids, 'turn_id':'t', 'phrase_id':phrase, 'status':'started'}, 'browser')
        self.assertNotIn('assistant', self.store.context('a')['turns'][0]['content'])
        await self.handler.process('convo.playback', {**self.ids, 'turn_id':'t', 'phrase_id':phrase}, 'browser')
        self.assertEqual(self.store.context('a')['turns'][0]['content']['assistant'], 'A complete answer.')

    async def test_incremental_phrase_waits_for_verified_end_before_completion_ack(self):
        state = self.handler.connections['browser']
        self.store.accept_turn(self.session['id'], 'browser', 0, 'pcm', {'transcript': 'Question'})
        released = asyncio.Event()
        async def chunks(role, text):
            yield {'audio': b'\0\0', 'format': 'pcm_s16le', 'sample_rate': 24000, 'channels': 1}
            await released.wait()
        state['sidecar'].speech_stream = chunks
        task = asyncio.create_task(self.handler._speak('browser', state, self.session, 'pcm', 'Answer', {'capabilities': {'native_pcm': True}}))
        await asyncio.sleep(0)
        phrase = next(iter(state['phrases']))
        result = await self.handler.process('convo.playback', {**self.ids, 'turn_id':'pcm', 'phrase_id':phrase}, 'browser')
        self.assertTrue(result['incomplete'])
        self.assertNotIn('assistant', self.store.context('a')['turns'][0]['content'])
        released.set(); await task
        self.assertEqual([event[2]['type'] for event in self.handler.sent], ['audio.begin', 'audio.chunk', 'audio.end'])
        await self.handler.process('convo.playback', {**self.ids, 'turn_id':'pcm', 'phrase_id':phrase}, 'browser')
        self.assertEqual(self.store.context('a')['turns'][0]['content']['assistant'], 'Answer')

    async def test_truncated_incremental_phrase_aborts_and_cannot_be_delivered(self):
        state = self.handler.connections['browser']
        async def chunks(role, text):
            yield {'audio': b'\0\0', 'format':'pcm_s16le', 'sample_rate':24000, 'channels':1}
            raise ValueError('Missing terminal receipt')
        state['sidecar'].speech_stream = chunks
        with self.assertRaises(ValueError):
            await self.handler._speak('browser', state, self.session, 'pcm', 'Answer', {'capabilities': {'native_pcm':True}})
        self.assertEqual(state['phrases'], {})
        self.assertEqual(self.handler.sent[-1][2]['type'], 'audio.abort')
        self.assertFalse(any(event[2]['type'] == 'audio.end' for event in self.handler.sent))

    async def test_selected_system_one_never_falls_back_when_unregistered(self):
        await self.handler.on_disconnect('browser')
        self.config['system_one'] = {'enabled':True, 'backend':'unavailable'}
        refusal = await self.handler.process('convo.start', {'target':'a'}, 'browser')
        self.assertEqual((refusal['ok'], refusal['refused']), (False, True))
        self.assertRegex(refusal['message'], 'not registered')
        self.assertEqual(self.handler.connections, {})

    async def test_ambient_remote_main_audio_requires_separate_consent(self):
        await self.handler.on_disconnect('browser')
        self.config['ambient_enabled'] = True
        self.config['roles']['conversation']['remote'] = True
        refusal = await self.handler.process('convo.start', {'target':'a', 'mode':'ambient'}, 'browser')
        self.assertFalse(refusal['ok'])
        self.assertRegex(refusal['message'], 'separate explicit opt-in')

    async def test_delayed_transcript_updates_original_target_after_retarget(self):
        await self.turn()
        await self.handler.process('convo.retarget', {**self.ids, 'target':'b'}, 'browser')
        await self.handler.process('convo.transcript', {**self.ids, 'turn_id':'t', 'text':'Corrected original question'}, 'browser')
        self.assertEqual(self.store.context('a')['turns'][0]['content']['transcript'], 'Corrected original question')
        self.assertEqual(self.store.context('b')['turns'], [])

    async def test_malformed_audio_never_reaches_classifier(self):
        state = self.handler.connections['browser']
        with self.assertRaises(ValueError):
            await self.handler.process('convo.turn', {**self.ids,'turn_id':'bad','audio':'not-wav'}, 'browser')
        self.assertEqual(state['sidecar'].calls, [])

    async def test_interrupt_cancels_policy_without_stale_response(self):
        state = self.handler.connections['browser']; sidecar = state['sidecar']; sidecar.block = asyncio.Event()
        await self.handler.process('convo.turn', {**self.ids,'turn_id':'t','transcript':'Question'}, 'browser')
        task = state['task']; await asyncio.wait_for(sidecar.entered.wait(), 2)
        result = await self.handler.process('convo.interrupt', self.ids, 'browser')
        self.assertTrue(task.cancelled())
        self.assertEqual(result['session']['epoch'], 1)
        self.assertNotIn('answer', sidecar.calls)
        self.assertFalse(any(e[2]['type'] == 'audio.phrase' for e in self.handler.sent))

    async def test_nonexplicit_tool_proposal_never_queues_work(self):
        state = self.handler.connections['browser']
        state['sidecar'].decision = {'speech':'speak','action':'delegate','confidence':.99,'explicit_request':False}
        state['sidecar'].proposal = {'type':'tool.proposal','name':'delegate_task','arguments':{'task':'Unapproved'}}
        await self.turn()
        self.assertEqual(self.store.jobs('a'), [])

    async def test_explicit_job_is_idempotent_and_target_bound(self):
        state = self.handler.connections['browser']
        state['sidecar'].decision = {'speech':'speak','action':'delegate','confidence':.99,'explicit_request':True}
        state['sidecar'].proposal = {'type':'tool.proposal','name':'delegate_task','arguments':{'task':'Authorized'}}
        await self.turn()
        jobs = self.store.jobs('a'); self.assertEqual(len(jobs),1)
        await self.handler.process('convo.turn',{**self.ids,'turn_id':'t','transcript':'Question'},'browser')
        await self.handler.process('convo.retarget',{**self.ids,'target':'b'},'browser')
        self.assertEqual(len(self.store.jobs('a')),1); self.assertEqual(self.store.jobs('b'),[])

    async def test_completion_notice_is_target_bound_and_not_replayed(self):
        job = self.store.submit(self.ids['session_id'],'browser',0,'original','request','Authorized work')
        self.store.transition(job['id'],'queued','dispatching')
        self.store.transition(job['id'],'dispatching','running')
        self.store.transition(job['id'],'running','completed','Result')
        await self.handler.process('convo.notice',{**self.ids,'job_id':job['id']},'browser')
        await self.handler.connections['browser']['task']
        count = len(self.store.context('a')['turns'])
        await self.handler.process('convo.notice',{**self.ids,'job_id':job['id']},'browser')
        self.assertEqual(len(self.store.context('a')['turns']), count)
        result = await self.handler.process('convo.retarget',{**self.ids,'target':'b'},'browser')
        with self.assertRaises(ValueError):
            await self.handler.process('convo.notice',{**self.ids,'epoch':result['session']['epoch'],'job_id':job['id']},'browser')

    async def test_invalid_retarget_does_not_leave_session_preparing(self):
        with self.assertRaises(ValueError):
            await self.handler.process('convo.retarget',{**self.ids,'target':'invalid'},'browser')
        self.assertFalse(self.handler.connections['browser'].get('preparing',False))
        await self.turn()

    async def test_disconnect_during_persona_setup_releases_ownership(self):
        await self.handler.on_disconnect('browser')
        entered = asyncio.Event()
        async def blocked(*args): entered.set(); await asyncio.Event().wait()
        with patch.object(self.host,'persona',side_effect=blocked):
            pending = asyncio.create_task(self.handler.process('convo.start',{'target':'a'},'browser'))
            await asyncio.wait_for(entered.wait(), 2); await self.handler.on_disconnect('browser')
            with self.assertRaises(asyncio.CancelledError): await pending
        self.assertEqual(self.handler.connections,{})
        self.store.start('replacement','a'); s=self.store.db.execute("SELECT id FROM sessions WHERE active=1").fetchone()
        self.store.stop(s['id'],'replacement')

    async def test_plugin_disable_detaches_pending_voice_without_losing_jobs(self):
        state = self.handler.connections['browser']
        state['sidecar'].block = asyncio.Event()
        job = self.store.submit(self.ids['session_id'],'browser',0,'t','pending','Authorized task')
        await self.handler.process('convo.turn', {**self.ids,'turn_id':'t','transcript':'Question'}, 'browser')
        task = state['task']
        await asyncio.wait_for(state['sidecar'].entered.wait(), 2)
        await asyncio.wait_for(self.handler.disable('browser'), 2)
        self.assertTrue(task.cancelled())
        self.assertEqual(self.handler.connections, {})
        self.assertEqual(self.store.job(job['id'])['status'],'queued')
        self.assertEqual(self.handler.sent[-1][1], 'convo.disabled')
        with self.assertRaises(ValueError):
            await self.handler.process('convo.turn', {**self.ids,'turn_id':'stale','transcript':'Question'}, 'browser')


if __name__ == '__main__': unittest.main()
