"""BP5: continued and corrected thoughts. A fragment that follows an unfinished or still-unanswered one within the horizon is
joined to it (memory only) and answered once; a reply that has started to be heard is never taken over.
Virtual clock, scripted providers, synthetic audio: this says nothing about real voices or rooms."""
import asyncio
import importlib
import json
import unittest

if __package__:
    from .behavior_harness import (DELEGATE, SILENT, SPEAK, SPEAK_PLAIN, Harness, concat_wavs, frame_count, make_wav, record, run_virtual, script)
    from .test_behavior_corpus import DELEGATE_TASK, Scenario
else:
    from behavior_harness import (DELEGATE, SILENT, SPEAK, SPEAK_PLAIN, Harness, concat_wavs, frame_count, make_wav, record, run_virtual, script)
    from test_behavior_corpus import DELEGATE_TASK, Scenario

continuation = importlib.import_module('plugins._convo.helpers.convo_continuation')
RATE = 16000
PHRASE = 'The reply is one complete sentence that is long enough to be spoken at once. '

# Typed envelopes (a System One decision backend's answer): the JSON classifier path cannot carry an attention class.
UNFINISHED = {'speech': 'silent', 'action': 'none', 'confidence': .6, 'explicit_request': False, 'attention': 'unfinished',
              'attention_probabilities': {'addressed': .05, 'backchannel': .02, 'side_conversation': .03, 'unfinished': .85, 'unclear': .05},
              'explicit_probability': .1, 'name_probability': 0.0}
ADDRESSED = {'speech': 'speak', 'action': 'none', 'confidence': .99, 'explicit_request': True, 'attention': 'addressed',
             'attention_probabilities': {'addressed': .9, 'backchannel': .02, 'side_conversation': .02, 'unfinished': .03, 'unclear': .03},
             'explicit_probability': .9, 'name_probability': 0.0}

ADDRESSED_PLAIN = {**ADDRESSED, 'explicit_request': False, 'explicit_probability': .1}      # a continuation, not a command


def typed(h):
    h.handler._typed = lambda state: True


def silence(seconds):
    return make_wav(0, seconds)


class JoinUnit(unittest.IsolatedAsyncioTestCase):
    def wav(self, level, seconds):
        return make_wav(level, seconds)

    async def test_the_joined_audio_is_the_fragments_with_the_real_gap_as_silence(self):
        cont = continuation.Continuation(7, 20)
        first, second = self.wav(300, 1.0), self.wav(500, 1.0)
        cont.hold(first, 10.0)
        merge = cont.plan(second, 11.5)                                      # the second began 0.5 s after the first ended
        self.assertEqual(merge.audio, concat_wavs(first, silence(.5), second))
        self.assertAlmostEqual(merge.gap, .5, delta=1e-6)
        self.assertEqual((merge.supersede, merge.revisions), ('', 1))
        self.assertAlmostEqual(merge.seconds, 2.5, delta=1e-6)

    async def test_overlapping_arrival_joins_with_no_silence(self):
        cont = continuation.Continuation(7, 20)
        first, second = self.wav(300, 1.0), self.wav(500, 1.0)
        cont.hold(first, 10.0)
        self.assertEqual(cont.plan(second, 10.9).audio, concat_wavs(first, second))

    async def test_a_long_pause_is_joined_as_a_short_one(self):
        cont = continuation.Continuation(7, 20)
        first, second = self.wav(300, 1.0), self.wav(500, 1.0)
        cont.hold(first, 0.0)
        self.assertEqual(cont.plan(second, 7.0).audio, concat_wavs(first, silence(1.5), second))   # a 5 s pause, kept to 1.5 s

    async def test_a_fragment_starting_after_the_horizon_does_not_join_and_the_held_audio_is_dropped(self):
        cont = continuation.Continuation(7, 20)
        cont.hold(self.wav(300, 1.0), 0.0)
        self.assertIsNone(cont.plan(self.wav(500, 1.0), 9.5))                # starts at 8.5 s: 8.5 s after the first ended
        self.assertIsNone(cont.pending)

    async def test_the_combined_length_is_bounded_by_the_turn_limit_and_the_hard_cap(self):
        cont = continuation.Continuation(7, 20)
        cont.hold(self.wav(300, 12.0), 0.0)
        kept = cont.plan(self.wav(500, 9.0), 10.0)                            # 21 s > 20 s: not one clip, but nothing is lost
        self.assertEqual((kept.audio, kept.earlier), (self.wav(500, 9.0), [self.wav(300, 12.0)]))
        joined = cont.plan(self.wav(500, 7.0), 8.0)                           # 19 s
        self.assertEqual(joined.parts, ())
        self.assertLessEqual(joined.seconds, 20.0)
        wide = continuation.Continuation(7, 45)                              # max_audio_seconds may be 45, but joined audio stops at 30
        wide.hold(self.wav(300, 20.0), 0.0)
        self.assertEqual(len(wide.plan(self.wav(500, 11.0), 12.0).parts), 1)
        self.assertEqual(wide.plan(self.wav(500, 9.0), 10.0).parts, ())

    async def test_one_request_in_several_clips_is_bounded_in_length_and_number(self):
        cont = continuation.Continuation(7, 20)
        at, parts, pcm = 0.0, (), self.wav(300, 15.0)
        cont.remember(pcm, at, 'f1')
        for index in range(2, 6):
            at += 15.5
            merge = cont.plan(self.wav(300 + index, 15.0), at)
            self.assertEqual((len(merge.parts), merge.audio), (index - 1, self.wav(300 + index, 15.0)))
            cont.remember(merge.audio, at, 'f%d' % index, parts=merge.parts, revisions=merge.revisions)
        self.assertEqual(cont.pending.total, 75.0)
        self.assertIsNone(cont.plan(self.wav(900, 1.0), at + 1.5))            # 76 s: what follows is a request of its own
        self.assertIsNotNone(cont.pending)                                    # ...and the earlier one is not dropped by that
        short = continuation.Continuation(7, 1)
        short.remember(self.wav(300, .9), 0.0, 'p0')
        for index in range(1, continuation.MAX_PARTS + 1):
            merge = short.plan(self.wav(400 + index, .9), index * 1.0)
            short.remember(merge.audio, index * 1.0, 'p%d' % index, parts=merge.parts, revisions=merge.revisions)
        self.assertEqual(len(short.pending.parts), continuation.MAX_PARTS)
        self.assertIsNone(short.plan(self.wav(900, .9), 8.0))

    async def test_an_answer_still_being_prepared_stays_joinable_however_long_the_pause(self):
        cont = continuation.Continuation(7, 20)
        first, second = self.wav(300, 1.0), self.wav(500, 1.0)
        cont.remember(first, 0.0, 'f1')
        self.assertEqual(cont.plan(second, 31.0, open=True).audio, concat_wavs(first, silence(1.5), second))
        self.assertTrue(cont.holds('f1'))
        preparing = ['f1']
        cont.alive = lambda turn: turn in preparing
        cont._expire()
        self.assertTrue(cont.holds('f1'))                                     # its time is up, but its answer is still being prepared
        self.assertIsNotNone(cont._timer)
        preparing.clear()
        cont._expire()
        self.assertIsNone(cont.pending)

    async def test_an_unfinished_fragment_with_no_answer_expires_even_while_something_is_prepared(self):
        cont = continuation.Continuation(7, 20)
        cont.alive = lambda turn: True
        cont.hold(self.wav(300, 1.0), 0.0)
        cont._expire()
        self.assertIsNone(cont.pending)

    async def test_the_revision_limit(self):
        cont = continuation.Continuation(7, 20)
        limit = continuation.MAX_REVISIONS
        cont.hold(self.wav(300, .2), 0.0, revisions=limit - 1)
        self.assertEqual(cont.plan(self.wav(500, .2), 1.0).revisions, limit)
        cont.hold(self.wav(300, .2), 0.0, revisions=limit)
        self.assertIsNone(cont.pending)                                       # nothing is kept past the limit

    async def test_different_sample_rates_are_never_one_clip(self):
        cont = continuation.Continuation(7, 20)
        cont.hold(make_wav(300, 1.0, rate=16000), 0.0)
        merge = cont.plan(make_wav(500, 1.0, rate=24000), 1.0)
        self.assertEqual((merge.audio, merge.earlier), (make_wav(500, 1.0, rate=24000), [make_wav(300, 1.0, rate=16000)]))
        self.assertEqual(continuation.whole(merge.earlier, merge.audio), merge.audio)        # nothing to put together: the newest alone

    async def test_an_admitted_utterance_is_forgotten_the_moment_its_reply_is_heard(self):
        cont = continuation.Continuation(7, 20)
        cont.remember(self.wav(300, 1.0), 0.0, 'f1')
        self.assertTrue(cont.holds('f1'))
        cont.committed('other')
        self.assertTrue(cont.holds('f1'))
        cont.committed('f1')
        self.assertIsNone(cont.plan(self.wav(500, 1.0), 1.0))
        self.assertIsNone(cont.pending)

    async def test_held_audio_expires_from_memory_by_itself(self):
        cont = continuation.Continuation(7, 20)
        cont.hold(self.wav(300, 1.0), 0.0)
        await asyncio.sleep(0)
        self.assertIsNotNone(cont.pending)
        loop = asyncio.get_running_loop()
        # horizon + one full turn: a fragment that starts at the end of the horizon may still be finishing then
        timer = cont._timer
        self.assertAlmostEqual(timer.when() - loop.time(), 27.0, delta=.5)
        cont.clear()
        self.assertTrue(timer.cancelled())

    async def test_a_disabled_horizon_keeps_and_joins_nothing(self):
        cont = continuation.Continuation(0, 20)
        cont.hold(self.wav(300, 1.0), 0.0)
        cont.remember(self.wav(300, 1.0), 0.0, 'f1')
        self.assertIsNone(cont.pending)
        self.assertFalse(cont.enabled)


    async def test_the_browser_pre_roll_is_not_speech_for_the_gap_or_the_short_command_test(self):
        # Change 2 (2026-11-10): the browser keeps up to 0.4-0.5 s from before the speech began. A 2.2 s command arrives as 2.7 s of
        # audio, so measured with the pre-roll it would no longer be "short" and its gap would look 0.5 s shorter.
        cont = continuation.Continuation(7, 20)
        first = self.wav(300, 1.0)
        command = concat_wavs(silence(.5), self.wav(500, 2.2))                     # 0.5 s of pre-roll, then 2.2 s
        cont.hold(first, 10.0)
        merge = cont.plan(command, 10.0 + .7 + 2.2, pre_roll=.5)                    # the speech began 0.7 s after the first ended
        self.assertAlmostEqual(merge.fragment, 2.2, delta=1e-6)
        self.assertLessEqual(merge.fragment, 2.5)                                    # api/realtime.py SHORT_COMMAND_SECONDS
        self.assertAlmostEqual(merge.gap, .7, delta=1e-6)
        # The pre-roll is real sound from the gap, so only the rest of the gap is added as silence (0.7 s in all).
        self.assertEqual(merge.audio, concat_wavs(first, silence(.2), command))
        self.assertAlmostEqual(merge.seconds, 1.0 + .2 + 2.7, delta=1e-6)
        unaware = cont.plan(command, 10.0 + .7 + 2.2)
        self.assertGreater(unaware.fragment, 2.5)                                    # what the server would have measured without it
        # A pre-roll longer than the gap adds no silence; nonsense values are bounded.
        self.assertEqual(cont.plan(command, 10.0 + .2 + 2.2, pre_roll=.5).audio, concat_wavs(first, command))
        self.assertAlmostEqual(cont.plan(command, 12.0, pre_roll=-3).fragment, 2.7, delta=1e-6)
        self.assertAlmostEqual(cont.plan(command, 12.0, pre_roll=99).fragment, 0.0, delta=1e-6)


class Held(Scenario):
    """An `unfinished` completed utterance is answered, after a short wait for the rest of it (policy.UNFINISHED_WAIT): a fragment
    that starts within the wait, or while the user is still speaking then, is joined to it (memory only) and answered once."""
    def fragments(self, *, second_at, second_seconds=1.0, second_decision=ADDRESSED_PLAIN, first_seconds=1.0, config=None, tag_merged=None):
        async def scenario(h):
            typed(h)
            first, second = h.audio('part one', seconds=first_seconds), h.audio('part two', level=900, seconds=second_seconds)
            await h.candidate('f1', first, final=True)
            await h.until(1.0)
            held = h.state['cont'].pending is not None
            await h.until(second_at)
            await h.candidate('f2', second, final=True)
            await h.settle()
            return {'held': held, 'f1': h.last_state('f1'), 'f2': h.last_state('f2'), 'accepted': h.accepted_turns(),
                    'requests': [frame_count(a['audio']) for a in h.answers], 'f1_frames': frame_count(first), 'f2_frames': frame_count(second),
                    'heard': len(h.audible()), 'journal': h.store.events('a'), 'cont_after': h.state['cont'].pending,
                    'policy': [c[2] for c in h.log('policy.start')], 'first': first, 'second': second}
        facts, _ = self.play(scenario, model_deltas=[(PHRASE, .3)], config=config or {},
                             classifier={'part one': script(UNFINISHED, .2), 'part two': script(second_decision, .2), **(tag_merged or {})}, default=ADDRESSED)
        return facts

    def test_an_unfinished_utterance_waits_and_is_joined_to_its_continuation(self):
        facts = self.fragments(second_at=1.2)                                       # 1.2 s after it ended: inside the wait
        self.assertTrue(facts['held'])
        self.assertEqual(facts['f1'], 'admit')                                      # it will be answered...
        self.assertEqual(facts['accepted'], ['f1', 'f2'])                            # ...but f2 took it over before its answer started
        self.assertIn('turn.superseded', [event['kind'] for event in facts['journal']])
        self.assertEqual(facts['requests'], [facts['f1_frames'] + int(.2 * RATE) + facts['f2_frames']])    # the model heard one joined utterance
        self.assertEqual(facts['heard'], 1)
        self.assertIsNone(facts['cont_after'])                                       # the joined clip was answered; nothing is kept for it

    def test_an_unfinished_utterance_is_answered_when_nothing_follows(self):
        async def scenario(h):
            typed(h)
            await h.candidate('f1', h.audio('part one', seconds=1.0), final=True)
            await h.settle()
            return h.accepted_turns(), len(h.answers), h.first_audible('f1')
        (accepted, answers, audible), _ = self.play(scenario, model_deltas=[(PHRASE, .3)], classifier={'part one': script(UNFINISHED, .2)})
        self.assertEqual((accepted, answers), (['f1'], 1))                           # never dropped
        self.assertGreaterEqual(audible, 1.5)                                         # but only after the wait for the rest of it

    def test_the_joined_utterance_is_what_the_classifier_hears_too(self):
        facts = self.fragments(second_at=1.2)
        self.assertEqual(len(facts['policy']), 3)                                    # f1; the short fragment alone (it could be a command); then the joined clip
        self.assertNotEqual(facts['policy'][-1], 'part two')                         # the last call heard the joined clip, not the fragment

    def test_a_2_2_s_command_with_its_pre_roll_is_still_decided_on_its_own_first(self):
        """The browser sends `pre_roll_s` with the audio; the server subtracts it, so a 2.2 s fragment that arrives as 2.7 s of audio
        is still a short fragment (classified alone first), as it was before the pre-roll existed."""
        def run(fields):
            async def scenario(h):
                typed(h)
                first = h.audio('part one', seconds=1.0)
                second = h.audio('part two', level=900, seconds=2.7)
                await h.candidate('f1', first, final=True)
                await h.until(1.2)
                await h.candidate('f2', second, final=True, **fields)
                await h.settle()
                return [c[2] for c in h.log('policy.start')], h.state['candidate'].get('pre_roll_s')
            (policy, lead), _ = self.play(scenario, model_deltas=[(PHRASE, .3)],
                                          classifier={'part one': script(UNFINISHED, .2), 'part two': script(ADDRESSED_PLAIN, .2)}, default=ADDRESSED)
            return policy, lead
        policy, lead = run({'voiced_s': 1.9, 'pre_roll_s': .5})
        self.assertEqual(lead, .5)
        self.assertEqual(len(policy), 3)                                             # f1; the fragment alone (it could be a command); joined
        self.assertEqual(policy[1], 'part two')
        policy, lead = run({'voiced_s': 1.9})
        self.assertEqual(lead, 0.0)
        self.assertEqual(len(policy), 2, '2.7 s with no pre-roll reported is not short: joined at once')
        policy, lead = run({'pre_roll_s': True})
        self.assertEqual(lead, 0.0, 'a non-number is ignored')

    def test_audio_is_never_journaled(self):
        facts = self.fragments(second_at=1.2)
        text = json.dumps(facts['journal'])
        self.assertNotIn(facts['first'][:64], text)
        self.assertNotIn(facts['second'][:64], text)
        self.assertTrue(all('audio' not in event['payload'] for event in facts['journal']))

    def test_a_continuation_after_the_answer_started_is_a_separate_utterance(self):
        facts = self.fragments(second_at=4.0)
        self.assertEqual(facts['accepted'], ['f1', 'f2'])
        self.assertEqual(facts['requests'], [facts['f1_frames'], facts['f2_frames']])   # f1 was answered; f2 stands alone
        self.assertEqual(facts['policy'][-1], 'part two')

    def test_a_joined_utterance_that_is_not_for_the_assistant_withdraws_the_waiting_answer(self):
        # the joined clip is unknown to the script, so it gets the default decision: make that one a side conversation
        async def scenario(h):
            typed(h)
            await h.candidate('f1', h.audio('part one'), final=True)
            await h.until(1.0)
            await h.candidate('f2', h.audio('part two', level=900), final=True)
            await h.settle()
            return {'accepted': h.accepted_turns(), 'pending': h.state['cont'].pending, 'answers': len(h.answers),
                    'superseded': [p['superseded'] for _, p in h.events('turn.superseded')], 'f2': h.last_state('f2')}
        silent_env = {**UNFINISHED, 'attention': 'side_conversation',
                      'attention_probabilities': {'addressed': .02, 'backchannel': .02, 'side_conversation': .93, 'unfinished': .02, 'unclear': .01}}
        result, _ = self.play(scenario, default=silent_env, classifier={'part one': script(UNFINISHED, .2)})
        self.assertEqual((result['accepted'], result['pending'], result['answers']), (['f1'], None, 0))     # f1's answer never started
        self.assertEqual((result['superseded'], result['f2']), (['f1'], 'ignore'))

    def test_a_joined_utterance_that_is_still_unfinished_waits_again_up_to_the_limit(self):
        async def scenario(h):
            typed(h)
            seen = []
            for index in range(1, continuation.MAX_REVISIONS + 3):
                await h.candidate('f%d' % index, h.audio('more', level=300 + index, seconds=.5), final=True)
                await h.advance(1.0)                                                 # the next fragment starts inside the wait
                pending = h.state['cont'].pending
                seen.append(None if pending is None else pending.revisions)
            await h.settle()
            return seen, len(h.answers)
        (seen, answers), _ = self.play(scenario, default=UNFINISHED)
        limit = continuation.MAX_REVISIONS
        self.assertEqual(seen[:limit + 1], list(range(limit)) + [None])              # so many joins at most, then nothing is kept
        self.assertEqual(answers, 1)                                                  # one answer, after the user stopped

    def test_joining_is_off_when_the_horizon_is_zero(self):
        facts = self.fragments(second_at=4.0, config={'continuation_seconds': 0})
        self.assertFalse(facts['held'])
        self.assertEqual(facts['requests'], [facts['f1_frames'], facts['f2_frames']])   # each answered on its own

    def test_what_does_not_fit_one_clip_is_answered_together_as_two(self):
        async def scenario(h):
            typed(h)
            first, second = h.audio('part one', seconds=12.0), h.audio('part two', level=900, seconds=9.0)
            await h.candidate('f1', first, final=True)
            await h.until(1.95)
            await h.candidate('f2', h.audio('part two so far', level=901, seconds=.75), sequence=1, final=False)   # began 1.2 s after f1
            await h.until(10.2)
            await h.candidate('f2', second, sequence=2, final=True)
            await h.settle()
            return [(frame_count(a['audio']), [frame_count(clip) for clip in a.get('earlier_audio') or ()]) for a in h.answers], \
                frame_count(first), frame_count(second), h.accepted_turns(), len(h.audible())
        (requests, one, two, accepted, heard), _ = self.play(scenario, model_deltas=[(PHRASE, .3)], default=ADDRESSED_PLAIN,
                                       classifier={'part one': script(UNFINISHED, .2), 'part two so far': script(ADDRESSED_PLAIN, .2)})
        # 22 s together is past one clip's limit: the model hears both, in order, and answers once; f1's answer never talked over f2.
        self.assertEqual((requests, accepted, heard), ([(two, [one])], ['f1', 'f2'], 1))


class Unanswered(Scenario):
    """An admitted utterance whose reply has not started to be heard is taken over by the fragment that follows it."""

    def test_the_unheard_reply_is_superseded_journaled_and_left_out_of_the_history(self):
        async def scenario(h):
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=1.0)
            await h.candidate('f1', first, final=True)
            await h.until(.9)
            await h.candidate('f2', second, final=True)
            await h.settle()
            return {'journal': [(r['kind'], r['turn']) for r in h.store.events('a')], 'history': [a['history'] for a in h.answers],
                    'superseded': [p for _, p in h.events('turn.superseded')], 'heard': len(h.audible()), 'accepted': h.accepted_turns(),
                    'cancelled': len(h.log('answer.cancelled'))}
        facts, _ = self.play(scenario, model_deltas=[(PHRASE, 1.5)], default=SPEAK, classifier={'part one': script(SPEAK, .2), 'part two': script(SPEAK, .2)})
        self.assertIn(('turn.superseded', 'f1'), facts['journal'])
        self.assertEqual([p['turn_id'] for p in facts['superseded']], ['f2'])
        self.assertEqual(facts['superseded'][0]['superseded'], 'f1')
        self.assertEqual(facts['history'][-1], [])                                    # the joined request does not repeat f1 as a separate turn
        self.assertEqual((facts['heard'], facts['cancelled']), (1, 1))               # one reply; the first generation was cancelled

    def test_queued_work_of_the_superseded_turn_is_cancelled_and_a_running_job_is_not_touched(self):
        async def scenario(h):
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=1.0)
            await h.candidate('f1', first, final=True)
            await h.until(1.25)                       # f1's job is queued (after the quiet hold); its confirmation is still being synthesised
            queued = [(j['turn'], j['status']) for j in h.jobs()]
            await h.candidate('f2', second, final=True)
            await h.settle()
            return queued, [(j['turn'], j['status']) for j in h.jobs()], [r['kind'] for r in h.store.events('a')]
        # No words before the call: a spoken "Okay." would be heard before f2 and make f2 an ordinary barge-in instead.
        (queued, jobs, kinds), _ = self.play(scenario, tts_delay=.5, model_deltas=[('', .2)], proposal=DELEGATE_TASK, default=DELEGATE,
                                             classifier={'part one': script(DELEGATE, .2), 'part two': script(DELEGATE, .2)})
        self.assertEqual(queued, [('f1', 'queued')])
        self.assertEqual(jobs[0], ('f1', 'cancelled'))                                # never started, so it is withdrawn
        self.assertIn('job.cancelled', kinds)
        self.assertEqual(sum(1 for turn, status in jobs if status == 'queued'), 1)    # exactly one live request remains: the joined one

    def test_a_correction_inside_the_quiet_hold_means_the_first_job_is_never_queued(self):
        async def scenario(h):
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=1.0)
            await h.candidate('f1', first, final=True)
            await h.until(.55)                        # f1's proposal has arrived; the hold has not ended
            await h.candidate('f2', second, final=True)
            await h.settle()
            return [(j['turn'], j['status']) for j in h.jobs()], [r['kind'] for r in h.store.events('a')].count('job.queued')
        (jobs, queued_events), _ = self.play(scenario, model_deltas=[('Okay.', .2)], proposal=DELEGATE_TASK, default=DELEGATE,
                                             classifier={'part one': script(DELEGATE, .2), 'part two': script(DELEGATE, .2)})
        self.assertEqual(jobs, [('f2', 'queued')])
        self.assertEqual(queued_events, 1)

    def test_a_reply_that_has_started_to_be_heard_is_not_taken_over(self):
        async def scenario(h):
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=1.0)
            await h.candidate('f1', first, final=True)
            await h.until(2.0)                         # f1's reply is audible by now
            heard = h.first_audible('f1') is not None
            await h.candidate('f2', second, final=True)
            await h.settle()
            return {'heard_first': heard, 'requests': [frame_count(a['audio']) for a in h.answers], 'f2_frames': frame_count(second),
                    'superseded': h.events('turn.superseded'), 'kinds': [r['kind'] for r in h.store.events('a')]}
        facts, _ = self.play(scenario, model_deltas=[(PHRASE, .3)], default=SPEAK, classifier={'part one': script(SPEAK, .2), 'part two': script(SPEAK, .2)})
        self.assertTrue(facts['heard_first'])
        self.assertEqual(facts['requests'][-1], facts['f2_frames'])                  # f2 alone: ordinary barge-in
        self.assertEqual(facts['superseded'], [])
        self.assertNotIn('turn.superseded', facts['kinds'])

    def test_a_probe_that_already_stopped_the_unheard_reply_still_lets_the_final_join_it(self):
        async def scenario(h):
            first, probe, second = h.audio('part one', seconds=1.0), h.audio('correction probe', level=700, seconds=.8), h.audio('part two', level=900, seconds=1.0)
            await h.candidate('f1', first, final=True)
            await h.until(.5)
            await h.candidate('f2', probe, sequence=1, final=False)       # "no wait": explicit enough to stop the reply that was not heard yet
            await h.until(.8)
            stopped = len(h.events('candidate.admitted', 'f2'))
            await h.candidate('f2', second, sequence=2, final=True)
            await h.settle()
            return stopped, [frame_count(a['audio']) for a in h.answers], frame_count(first), frame_count(second), [p['superseded'] for _, p in h.events('turn.superseded')]
        (stopped, requests, one, two, superseded), _ = self.play(scenario, model_deltas=[(PHRASE, 1.5)], default=SPEAK,
                                                                 classifier={'part one': script(SPEAK, .2), 'correction probe': script(SPEAK, .15), 'part two': script(SPEAK_PLAIN, .2)})
        self.assertEqual(stopped, 1)
        self.assertEqual(requests[-1], one + two)                                         # the final joined the first fragment although a probe had cut its reply
        self.assertEqual(superseded, ['f1'])

    def test_a_turn_that_ended_without_a_reply_is_not_taken_over(self):
        """A suppressed or silent turn has no reply left to supersede: a fragment that follows starts something new."""
        async def scenario(h):
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=1.0)
            await h.candidate('f1', first, final=True)
            await h.settle()                           # the model said [LISTEN]: nothing was heard
            pending = h.state['cont'].pending
            await h.candidate('f2', second, final=True)
            await h.settle()
            return pending, [frame_count(a['audio']) for a in h.answers], frame_count(second), h.events('turn.superseded')
        (pending, requests, frames, superseded), _ = self.play(scenario, config={'main_listen_decision': True}, model_deltas=[('[LISTEN]', .1)],
                                                              classifier={'part one': script(SPEAK_PLAIN, .2), 'part two': script(SPEAK_PLAIN, .2)})
        self.assertIsNone(pending)
        # f2 was asked on its own (the model stayed quiet again); each decline was confirmed once (CONFIRM_SILENT)
        self.assertEqual((requests, superseded), ([frames] * 4, []))

    def test_the_experimental_pcm_stream_commits_the_reply_too(self):
        async def scenario(h):
            async def chunks(role, text):
                yield {'audio': b'\0\0', 'format': 'pcm_s16le', 'sample_rate': 24000, 'channels': 1}
            h.sidecar.speech_stream = chunks
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=1.0)
            await h.candidate('f1', first, final=True)
            await h.until(2.0)
            heard = [p['type'] for _, p in h.audible('f1')]
            await h.candidate('f2', second, final=True)
            await h.settle()
            return heard, [frame_count(a['audio']) for a in h.answers][-1] == frame_count(second), h.events('turn.superseded')
        (heard, alone, superseded), _ = self.play(scenario, model_deltas=[(PHRASE, .3)], default=SPEAK,
                                                 config={'roles': {'tts': {'capabilities': {'audio_output': True, 'native_pcm': True}}}},
                                                 classifier={'part one': script(SPEAK, .2), 'part two': script(SPEAK, .2)})
        self.assertEqual(heard, ['audio.begin'])
        self.assertEqual((alone, superseded), (True, []))

    def test_a_reply_that_becomes_audible_while_the_joined_clip_is_classified_is_decided_on_its_own(self):
        async def scenario(h):
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=1.0)
            h.merged('joined', first, second)
            await h.candidate('f1', first, final=True)
            await h.until(.3)                          # f1's phrase is being synthesised (audible at 0.5 s)
            await h.candidate('f2', second, final=True)
            await h.settle()
            return {'policy': [c[2] for c in h.log('policy.start')], 'requests': [frame_count(a['audio']) for a in h.answers],
                    'f2_frames': frame_count(second), 'superseded': h.events('turn.superseded'), 'accepted': h.accepted_turns()}
        facts, _ = self.play(scenario, model_deltas=[(PHRASE, .2)], tts_delay=.1,
                             classifier={'part one': script(SPEAK, .2), 'joined': script(SPEAK, .5), 'part two': script(SPEAK_PLAIN, .05)})
        self.assertEqual(facts['policy'], ['part one', 'part two', 'joined', 'part two'])        # alone, the joined clip, then the fragment alone again
        self.assertEqual(facts['requests'][-1], facts['f2_frames'])
        self.assertEqual((facts['superseded'], facts['accepted']), ([], ['f1', 'f2']))

    def test_a_fragment_that_the_classifier_would_ignore_alone_cannot_cancel_an_unheard_reply_unless_the_joined_clip_is_admitted(self):
        async def scenario(h):
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=1.0)
            h.merged('joined', first, second)
            await h.candidate('f1', first, final=True)
            await h.until(.9)
            await h.candidate('f2', second, final=True)
            await h.settle()
            return {'f2': h.last_state('f2'), 'accepted': h.accepted_turns(), 'heard': [p['turn_id'] for _, p in h.audible()], 'superseded': h.events('turn.superseded')}
        facts, _ = self.play(scenario, model_deltas=[(PHRASE, 1.5)], classifier={'part one': script(SPEAK, .2), 'joined': script(SILENT, .2), 'part two': script(SPEAK_PLAIN, .2)})
        self.assertEqual(facts['f2'], 'ignore')                                       # the joined clip decides, and it is ignorable
        self.assertEqual((facts['accepted'], facts['heard'], facts['superseded']), (['f1'], ['f1'], []))   # f1's reply carries on, heard once

    def test_stop_retarget_and_disconnect_forget_the_held_audio(self):
        async def scenario(h):
            outcomes = {}
            typed(h)
            for name, action in (('interrupt', lambda: h.interrupt()), ('retarget', lambda: h.retarget('b')), ('disconnect', lambda: h.handler.on_disconnect('browser'))):
                await h.candidate(name, h.audio('part one', level=300 + len(outcomes)), final=True)
                await h.until(h.now() + 1.0)
                outcomes[name] = [h.state['cont'].pending is not None]
                await action()
                outcomes[name].append(h.state['cont'].pending is None)
                if name == 'disconnect':
                    break
            return outcomes
        outcomes, _ = self.play(scenario, classifier={'part one': script(UNFINISHED, .2)})
        self.assertEqual(outcomes, {'interrupt': [True, True], 'retarget': [True, True], 'disconnect': [True, True]})

    def test_name_and_ambient_listening_never_keep_audio(self):
        async def scenario(h):
            return h.state['cont'].enabled
        result, _ = self.play(scenario, mode='name', config={'ambient_enabled': True, 'activation_names': ['Jarvis'],
                              'roles': {'classifier': {'capabilities': {'audio_input': True}}, 'conversation': {'capabilities': {'audio_input': True}}}})
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
