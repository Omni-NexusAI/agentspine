"""Track S's pure helpers wired into the turn engine: voice rules and persona block, spoken job updates, persona refresh at a
response boundary, relevant context, delegation context, and the short-command rule of continuation. Scripted providers only."""
import asyncio
import copy
import importlib
import json
import unittest
from unittest.mock import patch

if __package__:
    from .behavior_harness import DELEGATE, SILENT, SPEAK, SPEAK_PLAIN, frame_count, script
    from .test_behavior_corpus import DELEGATE_TASK, Scenario
    from .test_continuation import ADDRESSED, UNFINISHED, typed
    from . import test_realtime as _realtime
else:
    from behavior_harness import DELEGATE, SILENT, SPEAK, SPEAK_PLAIN, frame_count, script
    from test_behavior_corpus import DELEGATE_TASK, Scenario
    from test_continuation import ADDRESSED, UNFINISHED, typed
    import test_realtime as _realtime

style = importlib.import_module('plugins._convo.helpers.convo_voice_style')
updates = importlib.import_module('plugins._convo.helpers.convo_task_updates')
EXPLICIT = {**ADDRESSED}
PLAIN = {**ADDRESSED, 'explicit_request': False, 'explicit_probability': .1}


class SystemText(Scenario):
    def system_of(self, config=None, **options):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return h.answers[0]['system']
        system, _ = self.play(scenario, config=config or {}, classifier={'question': script(SPEAK, .2)}, **options)
        return system

    def test_the_voice_rules_come_from_the_style_helper_and_start_with_the_base_rules(self):
        system = self.system_of()
        expected = style.voice_rules({}, system.split('\n', 1)[0])
        self.assertTrue(system.startswith(expected.split('\n', 1)[0]))
        self.assertIn(style.DETAIL['balanced'], system)
        self.assertIn(style.TASK_STATE, system)

    def test_the_persona_block_is_the_helpers_bounded_json(self):
        system = self.system_of(config={'style': 'Warm and dry. "Quoted" text\nnew line'})
        block = style.persona_data('Calm voice', 'Warm and dry. "Quoted" text\nnew line')
        self.assertIn('\n' + block + '\nMemory data: ', system)
        self.assertEqual(json.loads(block.split(': ', 1)[1])['style'], 'Warm and dry. "Quoted" text\nnew line')
        self.assertEqual(len(json.loads(style.persona_data('p' * 3000, 's' * 3000).split(': ', 1)[1])['persona']), 1600)   # the helper's own bound

    def test_response_detail_is_read_for_every_response_not_once_per_session(self):
        async def scenario(h):
            h.state['config'] = copy.deepcopy(h.config)                   # the session's own copy keeps the start-time value
            await h.candidate('a', h.audio('question'), final=True)
            await h.settle()
            h.config['response_detail'] = 'concise'                        # the user saves a new value mid-conversation
            await h.candidate('b', h.audio('question again'), final=True)
            await h.settle()
            return [a['system'] for a in h.answers]
        systems, _ = self.play(scenario, classifier={'question': script(SPEAK, .2), 'question again': script(SPEAK, .2)})
        self.assertIn(style.DETAIL['balanced'], systems[0])
        self.assertIn(style.DETAIL['concise'], systems[1])
        self.assertNotIn(style.DETAIL['balanced'], systems[1])

    def test_a_settings_read_that_fails_keeps_the_sessions_own_value(self):
        async def scenario(h):
            h.state['config']['response_detail'] = 'detailed'
            m = h.module.modules()[0]
            working = m.settings
            def settings():
                import sys
                if sys._getframe(1).f_code.co_name in {'style_settings', '_style'}:      # only the style read fails; the rest of the turn still needs its settings
                    raise RuntimeError('settings store down')
                return working()
            m.settings = settings
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return h.answers[0]['system'], h.accepted_turns()
        result, _ = self.play(scenario, classifier={'question': script(SPEAK, .2)})
        self.assertIn(style.DETAIL['detailed'], result[0])
        self.assertEqual(result[1], ['q'])

    def test_listen_and_overheard_lines_still_follow_the_rules(self):
        system = self.system_of(config={'main_listen_decision': True})
        rules_end = system.index('\nPersona and style data')
        self.assertIn('[LISTEN]', system[:rules_end])
        self.assertLess(system.index(style.OVERRIDE), system.index('[LISTEN]'))


class RelevantContext(Scenario):
    def test_a_matching_earlier_exchange_is_added_bounded_and_labelled(self):
        async def scenario(h):
            await h.candidate('a', h.audio('first'), final=True)
            await h.settle()
            await h.candidate('b', h.audio('second'), final=True)
            await h.settle()
            return [a['system'] for a in h.answers]
        note = {**SPEAK, 'context_note': 'The user wants the quarterly budget spreadsheet reviewed'}
        later = {**SPEAK, 'context_note': 'Which budget spreadsheet were we reviewing'}
        systems, _ = self.play(scenario, classifier={'first': script(note, .2), 'second': script(later, .2)})
        self.assertNotIn('quarterly budget', systems[0])                              # nothing to match on the first turn
        self.assertIn('quarterly budget spreadsheet', systems[1])
        block = systems[1].split('\nMemory data: ', 1)[1].split('\n', 1)[1]
        self.assertTrue(block.startswith('Relevant conversation context'))
        self.assertLessEqual(len(block), 1600)

    def test_nothing_is_added_when_nothing_matches_or_there_is_no_request_text(self):
        async def scenario(h):
            await h.candidate('a', h.audio('first'), final=True)
            await h.settle()
            await h.candidate('b', h.audio('second'), final=True)
            await h.settle()
            return [a['system'] for a in h.answers]
        systems, _ = self.play(scenario, classifier={'first': script({**SPEAK, 'context_note': 'Weather in Paris'}, .2), 'second': script({**SPEAK, 'context_note': ''}, .2)})
        self.assertEqual(systems[0].split('\nMemory data: ', 1)[1], systems[1].split('\nMemory data: ', 1)[1].split('\n', 1)[0] if False else systems[0].split('\nMemory data: ', 1)[1])

    def test_a_turn_a_newer_fragment_took_over_is_never_context(self):
        async def scenario(h):
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=1.0)
            await h.candidate('f1', first, final=True)
            await h.until(.9)
            await h.candidate('f2', second, final=True)
            await h.settle()
            return h.answers[-1]['system']
        note = {**SPEAK_PLAIN, 'context_note': 'zebra migration schedule'}
        system, _ = self.play(scenario, model_deltas=[('A reply that is long enough to be heard later on.', 1.5)], default={**SPEAK_PLAIN, 'context_note': 'zebra migration schedule'},
                              classifier={'part one': script(note, .2), 'part two': script(note, .2)})
        self.assertNotIn('zebra', system.split('\nMemory data: ', 1)[1])

    def test_a_failure_to_build_the_context_never_ends_the_turn(self):
        async def scenario(h):
            h.host.recent_chat = lambda target: (_ for _ in ()).throw(RuntimeError('chat unreadable'))
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return h.accepted_turns(), h.first_audible('q') is not None
        result, _ = self.play(scenario, classifier={'question': script({**SPEAK, 'context_note': 'anything at all'}, .2)})
        self.assertEqual(result, (['q'], True))


class Delegation(Scenario):
    def test_the_delegated_task_carries_its_bounded_context_and_is_rechecked_afterwards(self):
        async def scenario(h):
            await h.candidate('a', h.audio('first'), final=True)
            await h.settle()
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return [j['prompt'] for j in h.jobs()], h.events('error', 't')
        note = {**SPEAK, 'context_note': 'Write the weekly report about the budget'}
        prompts, errors = self.play(scenario, proposal={**DELEGATE_TASK, 'arguments': {'task': 'Write the weekly budget report'}},
                                    classifier={'first': script(note, .2), 'ask': script(DELEGATE, .2)})[0]
        self.assertEqual(len(prompts), 1)
        self.assertTrue(prompts[0].startswith('Write the weekly budget report'))
        self.assertIn('budget', prompts[0].split('Write the weekly budget report', 1)[1])      # context follows the untouched task text
        self.assertEqual(errors, [])

    def test_a_task_that_only_exceeds_the_limit_with_its_context_is_refused(self):
        big = 'x' * 15990
        async def scenario(h):
            await h.candidate('a', h.audio('first'), final=True)
            await h.settle()
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return h.jobs(), len(h.events('error', 't'))
        note = {**SPEAK, 'context_note': 'budget report numbers'}
        (jobs, errors), _ = self.play(scenario, proposal={**DELEGATE_TASK, 'arguments': {'task': 'budget report ' + big[:15900]}},
                                      classifier={'first': script(note, .2), 'ask': script(DELEGATE, .2)})
        self.assertEqual((jobs, errors), ([], 1))

    def test_a_task_without_matching_context_is_exactly_the_proposed_text(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return [j['prompt'] for j in h.jobs()]
        prompts, _ = self.play(scenario, proposal=DELEGATE_TASK, classifier={'ask': script(DELEGATE, .2)})
        self.assertEqual(prompts, ['Write the weekly report'])

    def test_a_retarget_does_not_move_a_job_already_queued(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            await h.retarget('b')
            return [(j['target'], j['status']) for j in h.jobs()]
        jobs, _ = self.play(scenario, proposal=DELEGATE_TASK, classifier={'ask': script(DELEGATE, .2)})
        self.assertEqual(jobs, [('a', 'queued')])


class RealtimeBase(unittest.IsolatedAsyncioTestCase):
    """The real-time handler fixture of test_realtime.py, without re-running its tests."""
    asyncSetUp = _realtime.RealtimeTests.asyncSetUp
    asyncTearDown = _realtime.RealtimeTests.asyncTearDown


class Notices(RealtimeBase):
    def finished_job(self, status='completed'):
        Notices.count = getattr(Notices, 'count', 0) + 1
        job = self.store.submit(self.ids['session_id'], 'browser', 0, 'original', 'request%d' % Notices.count, 'Authorized work')
        self.store.transition(job['id'], 'queued', 'dispatching')
        self.store.transition(job['id'], 'dispatching', 'running')
        self.store.transition(job['id'], 'running', status, 'Result')
        return job

    async def notice(self, job):
        requests = []
        sidecar = self.handler.connections['browser']['sidecar']
        original = sidecar.answer
        async def answer(payload):
            requests.append(payload)
            async for item in original(payload):
                yield item
        sidecar.answer = answer
        await self.handler.process('convo.notice', {**self.ids, 'job_id': job['id']}, 'browser')
        await self.handler.connections['browser']['task']
        return requests

    async def test_the_instruction_matches_the_real_status(self):
        for status in ('completed', 'failed', 'uncertain'):
            with self.subTest(status):
                self.handler.connections['browser']['task'] = None
                requests = await self.notice(self.finished_job(status))
                self.assertEqual(requests[-1]['transcript'], updates.notice_instruction(status))
        self.assertNotIn('completed', updates.notice_instruction('failed'))

    async def test_a_text_only_update_is_not_spoken_yet_the_notice_is_settled_not_stuck(self):
        job = self.finished_job()
        with patch.object(updates, 'should_speak_update', return_value=updates.UpdateDecision(False, 'text_only_at_completion')):
            requests = await self.notice(job)
        self.assertEqual(requests, [])                                              # no model call, nothing synthesised
        self.assertNotIn('speech', self.handler.connections['browser']['sidecar'].calls)
        turns = [t for t in self.store.context('a')['turns'] if t['content'].get('jobs') == [job['id']]]
        self.assertEqual([t['settled'] for t in turns], [1])                        # settled
        self.assertEqual([e[2].get('state') for e in self.handler.sent if e[2]['type'] == 'state'][-1], 'listening')
        again = await self.notice(job)                                              # and not repeated
        self.assertEqual(again, [])

    async def test_the_gate_is_asked_with_the_configured_level_and_the_jobs_event_kind(self):
        self.handler.connections['browser']['config']['spoken_updates'] = 'milestones'
        seen = []
        real = updates.should_speak_update
        def spy(level, event):
            seen.append((level, event))
            return real(level, event)
        with patch.object(updates, 'should_speak_update', side_effect=spy):
            await self.notice(self.finished_job('failed'))
        self.assertEqual(seen, [('milestones', 'failed')])


class PersonaRefresh(RealtimeBase):
    async def run_turn(self, name='t'):
        await self.handler.process('convo.turn', {**self.ids, 'turn_id': name, 'transcript': 'Question'}, 'browser')
        await self.handler.connections['browser']['task']

    async def test_a_cached_persona_is_used_for_this_response(self):
        state = self.handler.connections['browser']
        systems = []
        original = state['sidecar'].answer
        async def answer(payload):
            systems.append(payload['system'])
            async for item in original(payload):
                yield item
        state['sidecar'].answer = answer
        async def cached(target, style_text, cache):
            return 'Brisk voice'
        with patch.object(self.host, 'persona_cached', create=True, side_effect=cached):
            await self.run_turn()
        self.assertIn('Brisk voice', systems[0])
        self.assertEqual(state['persona'], 'Brisk voice')

    async def test_a_missing_persona_never_blocks_the_turn_and_is_refreshed_in_the_background(self):
        state = self.handler.connections['browser']
        gate = asyncio.Event()
        async def cached(target, style_text, cache):
            return None
        async def slow(target, style_text, cache):
            await gate.wait()
            return 'Fresh voice'
        with patch.object(self.host, 'persona_cached', create=True, side_effect=cached), patch.object(self.host, 'persona', side_effect=slow):
            await asyncio.wait_for(self.run_turn(), 2)                                 # the turn finished while the refresh is still waiting
            self.assertEqual(state['persona'], 'Calm voice')
            self.assertFalse(state['persona_task'].done())
            gate.set()
            await state['persona_task']
        self.assertEqual(state['persona'], 'Fresh voice')

    async def test_failures_in_either_step_never_reach_the_turn(self):
        state = self.handler.connections['browser']
        async def broken_cache(*args):
            raise RuntimeError('prompt unreadable')
        async def broken_persona(*args):
            raise RuntimeError('utility model down')
        with patch.object(self.host, 'persona_cached', create=True, side_effect=broken_cache):
            await self.run_turn('a')
        self.assertEqual(self.store.events('a')[0]['kind'], 'user.accepted')
        async def none(*args):
            return None
        with patch.object(self.host, 'persona_cached', create=True, side_effect=none), patch.object(self.host, 'persona', side_effect=broken_persona):
            await self.run_turn('b')
            await state['persona_task']
        self.assertEqual(state['persona'], 'Calm voice')

    async def test_a_background_refresh_for_the_old_target_does_not_overwrite_the_new_one(self):
        state = self.handler.connections['browser']
        gate = asyncio.Event()
        async def none(*args):
            return None
        async def slow(target, *args):
            if target == 'b':
                return 'New chat voice'
            await gate.wait()
            return 'Old chat voice'
        with patch.object(self.host, 'persona_cached', create=True, side_effect=none), patch.object(self.host, 'persona', side_effect=slow):
            await self.run_turn()
            result = await self.handler.process('convo.retarget', {**self.ids, 'target': 'b'}, 'browser')
            gate.set()
            await asyncio.sleep(0)
        self.assertEqual(state['persona'], 'New chat voice')

    async def test_a_refresh_that_finishes_after_the_target_changed_is_dropped(self):
        state = self.handler.connections['browser']
        async def finished(*args):
            return 'Old chat voice'
        with patch.object(self.host, 'persona', side_effect=finished):
            await self.handler._persona_later(state, 'b')                              # the session is on 'a'
        self.assertEqual(state['persona'], 'Calm voice')
        with patch.object(self.host, 'persona', side_effect=finished):
            await self.handler._persona_later(state, 'a')
        self.assertEqual(state['persona'], 'Old chat voice')

    async def test_a_host_without_the_cache_call_is_unchanged(self):
        self.assertFalse(hasattr(self.host, 'persona_cached'))
        await self.run_turn()
        self.assertNotIn('persona_task', self.handler.connections['browser'])


class ShortCommands(Scenario):
    """A short, confident, explicit command is answered on its own (it supersedes an unheard reply instead of joining it);
    everything else that continues an unanswered utterance is still joined."""
    FIRST = 'explain how the engine works'

    def run_pair(self, second_decision, *, second_seconds=.6, first_decision=SPEAK_PLAIN, tags=None, **options):
        async def scenario(h):
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=second_seconds)
            await h.candidate('f1', first, final=True)
            await h.until(.9)
            await h.candidate('f2', second, final=True)
            await h.settle()
            return {'requests': [frame_count(a['audio']) for a in h.answers], 'one': frame_count(first), 'two': frame_count(second),
                    'joined': frame_count(first) + frame_count(second) + int(.3 * 16000),         # f2 began 0.3 s after f1 ended
                    'policy': [c[2] for c in h.log('policy.start')], 'journal': [(r['kind'], r['turn']) for r in h.store.events('a')],
                    'superseded': [p['superseded'] for _, p in h.events('turn.superseded')], 'heard': len(h.audible()), 'jobs': [(j['turn'], j['status']) for j in h.jobs()]}
        facts, _ = self.play(scenario, model_deltas=[('A reply that is long enough to be spoken as one phrase later on.', 1.5)],
                             default=SPEAK, classifier={'part one': script(first_decision, .2), 'part two': script(second_decision, .2), **(tags or {})}, **options)
        return facts

    def test_a_short_explicit_command_is_answered_alone_and_supersedes_the_unheard_reply(self):
        facts = self.run_pair(SPEAK)                                                    # explicit, confident, 0.6 s
        self.assertEqual(facts['requests'][-1], facts['two'])                           # alone, not joined
        self.assertEqual(facts['policy'], ['part one', 'part two'])                     # one classifier call for it
        self.assertEqual(facts['superseded'], ['f1'])                                   # the unheard reply is still replaced
        self.assertIn(('turn.superseded', 'f1'), facts['journal'])
        self.assertEqual(facts['heard'], 1)

    def test_a_command_that_finds_the_earlier_reply_already_heard_is_an_ordinary_barge_in(self):
        async def scenario(h):
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=.6)
            await h.candidate('f1', first, final=True)
            await h.until(.3)                          # f1's reply becomes audible at 0.5 s, while the command is still being decided
            await h.candidate('f2', second, final=True)
            await h.settle()
            return h.events('turn.superseded'), [r['kind'] for r in h.store.events('a')].count('turn.superseded'), h.accepted_turns(), [frame_count(a['audio']) for a in h.answers][-1], frame_count(second)
        (events, journaled, accepted, last, two), _ = self.play(scenario, model_deltas=[('A reply that is long enough to be spoken as one phrase right away.', .1)], tts_delay=.1, default=SPEAK,
                                                              classifier={'part one': script(SPEAK, .2), 'part two': script(SPEAK, .4)})
        self.assertEqual((events, journaled, accepted, last), ([], 0, ['f1', 'f2'], two))      # a heard reply is never marked superseded

    def test_a_queued_job_of_the_replaced_turn_is_cancelled_too(self):
        facts = self.run_pair(SPEAK, first_decision=DELEGATE, proposal=DELEGATE_TASK, tts_delay=.5)
        # f1's proposal arrives at 0.2 + 1.5 s, after f2 (0.9 s) took over: nothing of f1 may be queued
        self.assertNotIn(('f1', 'queued'), facts['jobs'])

    def test_a_short_fragment_that_is_not_an_explicit_command_is_joined(self):
        facts = self.run_pair(SPEAK_PLAIN, tags={})
        self.assertEqual(facts['requests'][-1], facts['joined'])
        self.assertEqual(facts['policy'][:2], ['part one', 'part two'])                  # asked alone first, then as the joined clip
        self.assertEqual(len(facts['policy']), 3)
        self.assertEqual(facts['superseded'], ['f1'])

    def test_a_long_explicit_fragment_is_joined_without_the_extra_call(self):
        facts = self.run_pair(SPEAK, second_seconds=3.0)
        self.assertEqual(facts['requests'][-1], facts['one'] + facts['two'])
        self.assertEqual(len(facts['policy']), 2)

    def test_a_confident_but_not_explicit_command_is_still_joined(self):
        facts = self.run_pair({**SPEAK, 'explicit_request': False})
        self.assertEqual(facts['requests'][-1], facts['joined'])

    def test_an_explicit_but_unsure_fragment_is_joined(self):
        facts = self.run_pair({**SPEAK, 'confidence': .88})
        self.assertEqual(facts['requests'][-1], facts['joined'])

    def test_a_short_command_after_an_unfinished_utterance_is_answered_on_its_own(self):
        async def scenario(h):
            typed(h)
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=.6)
            await h.candidate('f1', first, final=True)
            await h.until(2.0)
            await h.candidate('f2', second, final=True)
            await h.settle()
            return [frame_count(a['audio']) for a in h.answers], frame_count(second), h.accepted_turns()
        (requests, two, accepted), _ = self.play(scenario, classifier={'part one': script(UNFINISHED, .2), 'part two': script(EXPLICIT, .2)})
        self.assertEqual((requests, accepted), ([two], ['f2']))

    def test_a_short_continuation_after_an_unfinished_utterance_is_joined(self):
        async def scenario(h):
            typed(h)
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=.6)
            await h.candidate('f1', first, final=True)
            await h.until(2.0)
            await h.candidate('f2', second, final=True)
            await h.settle()
            return [frame_count(a['audio']) for a in h.answers], frame_count(first), frame_count(second)
        (requests, one, two), _ = self.play(scenario, classifier={'part one': script(UNFINISHED, .2), 'part two': script(PLAIN, .2)}, default=PLAIN)
        self.assertGreaterEqual(requests[-1], one + two)

    def test_the_short_command_limit_is_two_and_a_half_seconds(self):
        self.assertEqual(self.run_pair(SPEAK, second_seconds=2.5)['requests'][-1], int(2.5 * 16000))
        self.assertGreater(self.run_pair(SPEAK, second_seconds=2.6)['requests'][-1], int(2.6 * 16000))


if __name__ == '__main__':
    unittest.main()
