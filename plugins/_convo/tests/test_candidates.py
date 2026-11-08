"""Semantic interruption tests: synthetic completed WAVs, no hardware/providers."""
import asyncio
import base64
import io
import time
import wave
from unittest.mock import patch

if __package__:
    from .test_realtime import RealtimeTests
else:
    from test_realtime import RealtimeTests


def wav():
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(16000)
        writer.writeframes(b'\0\0' * 16000)
    return base64.b64encode(output.getvalue()).decode()


class CandidateTests(RealtimeTests):
    async def candidate(self, name='candidate', sequence=1, final=True, epoch=None):
        return await self.handler.process('convo.candidate', {
            **self.ids, 'epoch': self.ids['epoch'] if epoch is None else epoch,
            'turn_id': name, 'sequence': sequence, 'final': final, 'audio': wav()}, 'browser')

    async def active_reply(self):
        state = self.handler.connections['browser']
        state['task'] = asyncio.create_task(asyncio.Event().wait())
        return state, state['task']

    async def test_backchannel_keeps_current_reply_and_epoch(self):
        state, reply = await self.active_reply()
        state['sidecar'].decision = {'speech': 'silent', 'action': 'none', 'confidence': .99}
        await self.candidate(); await state['candidate_task']
        self.assertFalse(reply.done())
        self.assertEqual(state['session']['epoch'], 0)
        self.assertEqual(self.store.events('a'), [])
        self.assertFalse(any(e[2]['type'] == 'candidate.admitted' for e in self.handler.sent))

    async def test_semantic_admission_cancels_reply_and_accepts_final_once(self):
        state, reply = await self.active_reply()
        await self.candidate(); await state['candidate_task']; await state['task']
        self.assertTrue(reply.cancelled())
        self.assertEqual(state['session']['epoch'], 1)
        admitted = [e[2] for e in self.handler.sent if e[2]['type'] == 'candidate.admitted']
        self.assertEqual(len(admitted), 1)
        self.assertEqual(admitted[0]['previous_epoch'], 0)
        self.assertEqual(len([e for e in self.store.events('a') if e['kind'] == 'user.accepted']), 1)
        result = await self.candidate(epoch=1)
        self.assertTrue(result['duplicate'])
        self.assertEqual(state['sidecar'].calls.count('policy'), 1)

    async def test_probe_is_not_independent_turn_and_full_audio_reaches_answer(self):
        state, reply = await self.active_reply()
        await self.candidate(final=False); await state['candidate_task']
        self.assertTrue(reply.cancelled())
        self.assertEqual(self.store.events('a'), [])
        captured = []
        original = state['sidecar'].answer
        async def answer(payload):
            captured.append(payload)
            async for event in original(payload): yield event
        state['sidecar'].answer = answer
        await self.candidate(sequence=2, epoch=1); await state['candidate_task']; await state['task']
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['audio'], wav())
        self.assertIsNone(captured[0]['transcript'])
        self.assertEqual(state['session']['epoch'], 1)

    async def test_latest_window_coalesces_and_lagged_result_cannot_interrupt(self):
        state, reply = await self.active_reply()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def policy(payload):
            calls.append(payload)
            if len(calls) == 1:
                entered.set(); await release.wait()
                return {'speech':'speak','action':'none','confidence':.99,'explicit_request':True}
            return {'speech':'silent','action':'none','confidence':.99}
        state['sidecar'].policy = policy
        await self.candidate(sequence=1, final=False); await entered.wait()
        await self.candidate(sequence=2, final=False)
        await self.candidate(sequence=3, final=True)
        release.set(); await state['candidate_task']
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[-1]['context']['candidate_final'])
        self.assertFalse(reply.done())
        self.assertEqual(state['session']['epoch'], 0)

    async def test_uncertain_policy_ducks_without_cancelling(self):
        state, reply = await self.active_reply()
        state['sidecar'].decision = {'speech':'clarify','action':'none','confidence':.4}
        await self.candidate(); await state['candidate_task']
        self.assertFalse(reply.done())
        self.assertTrue(any(e[2].get('state') == 'withhold' for e in self.handler.sent))
        self.assertEqual(self.store.events('a'), [])

    async def test_stale_interrupt_and_retarget_are_read_only(self):
        state, reply = await self.active_reply()
        for event in ['convo.interrupt','convo.retarget']:
            result = await self.handler.process(event, {**self.ids, 'epoch':99, 'target':'b'}, 'browser')
            self.assertTrue(result['stale'])
            self.assertFalse(reply.done())
            self.assertEqual(state['session']['target'], 'a')
            self.assertEqual(state['session']['epoch'], 0)

    async def test_interrupt_rejects_old_classifier_result(self):
        state, reply = await self.active_reply()
        entered, release = asyncio.Event(), asyncio.Event()
        async def stubborn(payload):
            entered.set()
            try: await release.wait()
            except asyncio.CancelledError: await release.wait()
            return {'speech':'speak','action':'none','confidence':.99,'explicit_request':True}
        state['sidecar'].policy = stubborn
        await self.candidate(final=False); await entered.wait()
        task = state['policy_task']
        start = time.monotonic()
        await self.handler.process('convo.interrupt', self.ids, 'browser')
        self.assertLess(time.monotonic() - start, 2)
        release.set(); await task; await asyncio.sleep(0)
        self.assertFalse(any(e[2]['type'] == 'candidate.admitted' for e in self.handler.sent))
        self.assertEqual(self.store.events('a'), [])

    async def test_policy_failure_is_explicit_degraded_and_no_bypass(self):
        state, reply = await self.active_reply()
        async def failed(payload): raise ValueError('unavailable')
        state['sidecar'].policy = failed
        await self.candidate(); await state['candidate_task']
        self.assertFalse(reply.done())
        self.assertTrue(any(e[2].get('state') == 'degraded' for e in self.handler.sent))
        self.assertEqual(self.store.events('a'), [])

    async def test_invalid_candidate_audio_never_classified(self):
        state = self.handler.connections['browser']
        with self.assertRaises(ValueError):
            await self.handler.process('convo.candidate', {**self.ids,'turn_id':'bad','sequence':1,'final':True,'audio':'url://not-wave'}, 'browser')
        self.assertEqual(state['sidecar'].calls, [])

    async def test_final_inflight_before_admission_event_survives_epoch_transition(self):
        state, reply = await self.active_reply()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.handler.emit_to
        async def delayed(sid, event, payload):
            if payload.get('type') == 'candidate.admitted':
                entered.set(); await release.wait()
            await original(sid, event, payload)
        self.handler.emit_to = delayed
        await self.candidate(final=False)
        await asyncio.wait_for(entered.wait(), 2)
        # Browser still knows epoch zero while the admitted event is delayed.
        result = await self.candidate(sequence=2, final=True, epoch=0)
        self.assertTrue(result['queued'])
        unrelated = await self.candidate(name='other', epoch=0)
        self.assertTrue(unrelated['stale'])
        release.set(); await state['candidate_task']; await state['task']
        self.assertTrue(reply.cancelled())
        self.assertEqual(len([e for e in self.store.events('a') if e['kind']=='user.accepted']),1)
        self.assertEqual(state['session']['epoch'],1)

    async def test_completed_text_candidate_semantically_interrupts(self):
        state, reply = await self.active_reply()
        await self.handler.process('convo.candidate', {**self.ids,'turn_id':'text','sequence':1,'final':True,'transcript':'Question'}, 'browser')
        await state['candidate_task']; await state['task']
        self.assertTrue(reply.cancelled())
        self.assertEqual(self.store.context('a')['turns'][0]['content']['transcript'],'Question')

    async def test_final_clarification_respects_cooldown_and_never_tools(self):
        state, reply = await self.active_reply()
        state['sidecar'].decision={'speech':'clarify','action':'delegate','confidence':.8,'explicit_request':False}
        await self.candidate(); await state['candidate_task']; await state['task']
        self.assertTrue(reply.cancelled())
        self.assertEqual(self.store.jobs('a'),[])
        self.assertEqual(len(self.store.context('a')['turns']),1)
        await self.candidate(name='repeat',epoch=1); await state['candidate_task']
        self.assertEqual(len(self.store.context('a')['turns']),1)
        self.assertEqual(state['session']['epoch'],1)
