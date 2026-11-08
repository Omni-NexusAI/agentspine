"""Behaviour corpus on a virtual clock: Convo's real admission, interruption, action and cancellation logic.

Three kinds of test live here:
  * contract tests pass today and protect behaviour that already exists;
  * baseline tests pin a known defect or gap exactly as it behaves now (the backend work package that closes it is
    named in the docstring), so a silent change in either direction is noticed;
  * target tests are @expectedFailure and state the behaviour that package will add. When a package lands, remove its
    baseline test and the target's decorator in the same change; an "unexpected success" is the reminder.
Scripted providers only: nothing here measures acoustics, real models or listening quality."""
import asyncio
import importlib
import unittest

if __package__:
    from .behavior_harness import (DELEGATE, DIRECT, LONG_REPLY, SILENT, SPEAK, SPEAK_PLAIN, UNCERTAIN, Harness, ScriptedSidecar,
                                   frame_count, make_frames, make_wav, record, run_virtual, script, write_report)
else:
    from behavior_harness import (DELEGATE, DIRECT, LONG_REPLY, SILENT, SPEAK, SPEAK_PLAIN, UNCERTAIN, Harness, ScriptedSidecar,
                                  frame_count, make_frames, make_wav, record, run_virtual, script, write_report)

from plugins._convo.helpers.convo_contract import (ContractError, SpeechBusy, input_modality_conflict, public_settings,
                                                   validate_audio, validate_settings)

DELEGATE_TASK = {'type': 'tool.proposal', 'name': 'delegate_task', 'arguments': {'task': 'Write the weekly report'}}
WEB_SEARCH = {'type': 'tool.proposal', 'name': 'web_search', 'arguments': {'query': 'weather'}}


def tearDownModule():
    write_report()


class Scenario(unittest.TestCase):
    """Runs one async scenario on a fresh virtual loop and always closes the session."""
    def play(self, scenario, **options):
        """Returns (what the scenario returned, ground-truth metrics)."""
        async def run():
            h = await Harness.create(**options)
            try:
                result = await scenario(h)
                h.assert_invariants()
                return result, h.metrics()
            finally:
                await h.close()
        return run_virtual(run())


class InterruptionContract(Scenario):
    def test_backchannel_during_a_reply_is_ignored_and_the_reply_finishes(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(1.1)                                    # the reply is under way
            await h.candidate('m', h.audio('mhm'), final=False)
            await h.settle()
            self.assertEqual(h.last_state('m'), 'ignore')
            self.assertEqual(h.events('candidate.admitted', 'm'), [])
            self.assertEqual(h.epoch(), 1)                        # only the question was admitted
            self.assertEqual(len(h.audible('q')), 3)              # every phrase of the reply was delivered
            self.assertEqual(len(h.journal('assistant.generated')), 1)
        _, metrics = self.play(scenario, model_deltas=LONG_REPLY, tts_delay=.2,
                               classifier={'question': script(SPEAK, .2), 'mhm': script(SILENT, .25)},
                               truth={'question': 'answer', 'mhm': 'ignore'})
        self.assertEqual((metrics['false_cuts'], metrics['false_responses']), (0, 0))
        record('backchannel_during_reply', **metrics)

    def test_explicit_interruption_revokes_the_reply_within_classifier_latency(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(1.1)
            await h.candidate('s', h.audio('stop-probe'), sequence=1, final=False)
            await h.settle()
            admitted = h.events('candidate.admitted', 's')
            self.assertEqual(len(admitted), 1)
            revoked_at = admitted[0][0]
            self.assertAlmostEqual(revoked_at - 1.1, .3, delta=.02)       # probe + classifier latency, nothing more
            self.assertEqual([t for t, _ in h.audible('q') if t > revoked_at], [])
            self.assertEqual(h.journal('assistant.generated'), [])        # a revoked reply is never settled as spoken
            await h.candidate('s', h.audio('stop-final'), sequence=2, final=True)
            await h.settle()
            self.assertEqual(h.accepted_turns().count('s'), 1)
            self.assertIsNotNone(h.first_audible('s'))
        _, metrics = self.play(scenario, model_deltas=LONG_REPLY, tts_delay=.2,
                               classifier={'question': script(SPEAK, .2), 'stop-probe': script(SPEAK, .3), 'stop-final': script(SPEAK, .2)},
                               truth={'question': 'answer', 'stop-probe': 'interrupt', 'stop-final': 'answer'})
        self.assertEqual(metrics['missed_interruptions'], 0)
        self.assertLessEqual(max(metrics['interrupt_latency']), .5)
        record('explicit_interruption', **metrics)

    def test_partial_speech_without_explicit_evidence_only_withholds(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(1.1)
            await h.candidate('p', h.audio('maybe'), final=False)
            await h.settle()
            self.assertEqual(h.last_state('p'), 'withhold')
            self.assertEqual(h.events('candidate.admitted', 'p'), [])
            self.assertEqual(len(h.audible('q')), 3)
        self.play(scenario, model_deltas=LONG_REPLY, tts_delay=.2,
                  classifier={'question': script(SPEAK, .2), 'maybe': script(SPEAK_PLAIN, .2)})

    def test_slow_probe_classifier_degrades_without_interrupting(self):
        async def scenario(h):
            reply = h.start_reply()
            await h.candidate('p', h.audio('slow'), final=False)
            await h.settle()
            states = h.states('p')
            self.assertEqual([s[1] for s in states], ['considering', 'degraded'])
            self.assertAlmostEqual(states[1][0], 1.5, delta=.02)           # the classifier deadline, in virtual time
            self.assertFalse(reply.done())
            self.assertEqual((h.epoch(), h.events('candidate.admitted')), (0, []))
        self.play(scenario, classifier={'slow': script(SPEAK, 2.0)})

    def test_cancellation_ignoring_classifier_can_neither_interrupt_late_nor_overlap(self):
        async def scenario(h):
            reply = h.start_reply()
            await h.candidate('p', h.audio('slow'), sequence=1, final=False)       # would say "stop" at 2.0 s
            await h.until(1.7)                                                     # past the 1.5 s deadline
            await h.candidate('p', h.audio('next'), sequence=2, final=False)
            await h.until(2.5)                                                     # the detached call has now returned
            self.assertEqual([s[1] for s in h.states('p')], ['considering', 'degraded', 'considering', 'degraded'])
            self.assertEqual(len(h.log('policy.start')), 1)                        # the second probe never reached the provider
            self.assertEqual(len(h.log('policy.late')), 1)
            self.assertFalse(reply.done())
            self.assertEqual((h.epoch(), h.events('candidate.admitted')), (0, []))  # a late "stop" cannot interrupt
            await h.candidate('p', h.audio('later'), sequence=3, final=False)       # the classifier is free again
            await h.settle()
            self.assertEqual(h.last_state('p'), 'ignore')
            self.assertEqual(len(h.log('policy.start')), 2)
        self.play(scenario, classifier={'slow': script(SPEAK, 2.0, stubborn=1), 'next': script(SPEAK, .2), 'later': script(SILENT, .2)})

    def test_provider_that_ignores_cancellation_never_reaches_the_user_after_revocation(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(1.25)                                    # the model is between phrases
            await h.candidate('s', h.audio('stop-probe'), sequence=1, final=False)
            await h.until(2.0)
            revoked_at = h.events('candidate.admitted', 's')[0][0]
            self.assertAlmostEqual(revoked_at - 1.25, .15, delta=.02)       # the acknowledgement is not held by the provider
            await h.settle()
            admit_state_at = [t for t, s, _, _ in h.states('s') if s == 'admit'][0]
            # The bounded join (1.5 s) delays only the server's own bookkeeping, never the stop itself.
            self.assertAlmostEqual(admit_state_at - revoked_at, 1.5, delta=.05)
            await h.candidate('s', h.audio('stop-final'), sequence=2, final=True)
            await h.settle()
            self.assertEqual([t for t, _ in h.audible('q') if t > revoked_at], [])
            self.assertIsNotNone(h.first_audible('s'))
            await h.advance(10)                                    # the zombie finishes; assert_invariants checks the rest
            return {'admit_state_delay': round(admit_state_at - revoked_at, 3)}
        facts, metrics = self.play(scenario, model_deltas=LONG_REPLY, tts_delay=.2, stubborn_answer=3.0,
                                   classifier={'question': script(SPEAK, .2), 'stop-probe': script(SPEAK, .15), 'stop-final': script(SPEAK, .2)})
        record('provider_ignores_cancellation', **facts)


class ConversationContract(Scenario):
    def test_plain_question_is_answered_once_and_only_delivered_phrases_enter_context(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            self.assertEqual(h.accepted_turns(), ['q'])
            self.assertEqual(len(h.journal('assistant.generated')), 1)
            self.assertEqual(h.journal('assistant.delivered'), [])         # generated is not heard
            phrase = h.audible('q')[0][1]['phrase_id']
            await h.playback('q', phrase, 'started')
            self.assertEqual(h.journal('assistant.delivered'), [])         # started is not heard either
            await h.playback('q', phrase)
            self.assertEqual(len(h.journal('assistant.delivered')), 1)
            return {'first_audible': h.first_audible('q')}
        facts, metrics = self.play(scenario, truth={'question': 'answer'}, classifier={'question': script(SPEAK, .2)})
        record('plain_question', **metrics)

    def test_side_conversation_is_never_answered_or_retained(self):
        async def scenario(h):
            await h.candidate('tv', h.audio('television'), final=True)
            await h.settle()                                          # a completed utterance owns the single slot
            await h.candidate('tv2', h.audio('muttering'), final=True)
            await h.settle()
            self.assertEqual(h.states('tv')[-1][1], 'ignore')
            self.assertEqual(h.states('tv2')[-1][1], 'withhold')      # low-confidence silence is not an answer either
            self.assertEqual((h.accepted_turns(), h.answers, h.audible()), ([], [], []))
            self.assertEqual(h.store.events('a'), [])
        _, metrics = self.play(scenario, classifier={'television': script(SILENT, .2), 'muttering': script({**SILENT, 'confidence': .5}, .2)},
                               truth={'television': 'ignore', 'muttering': 'ignore'})
        self.assertEqual(metrics['false_responses'], 0)
        record('side_conversation', **metrics)

    def test_ambiguous_final_gets_one_clarifying_question_without_tools_then_cooldown(self):
        clarify = {'speech': 'clarify', 'action': 'none', 'confidence': .75}
        async def scenario(h):
            await h.candidate('c1', h.audio('unsure'), final=True)
            await h.settle()
            self.assertEqual(h.accepted_turns(), ['c1'])
            self.assertFalse(h.answers[0]['allow_tools'])
            self.assertIn('Ask one short in-character question', h.answers[0]['system'])
            self.assertIsNotNone(h.first_audible('c1'))
            await h.candidate('c2', h.audio('unsure again'), final=True)
            await h.settle()
            await h.candidate('c3', h.audio('hopeless'), final=True)
            await h.settle()
            self.assertEqual(h.accepted_turns(), ['c1'])               # the cooldown suppresses a second question
            self.assertEqual([h.last_state('c2'), h.last_state('c3')], ['withhold', 'withhold'])
        self.play(scenario, classifier={'unsure': script(clarify, .2), 'unsure again': script(clarify, .2), 'hopeless': script(UNCERTAIN, .2)})

    def test_a_json_classifier_cannot_switch_on_the_calibrated_gate_by_claiming_a_distribution(self):
        """It says 'silent, 99% sure' and attaches a distribution that would be answered if believed. Only a typed consumer is believed."""
        lying = {**SILENT, 'attention': 'addressed', 'attention_probabilities': {'addressed': .9, 'backchannel': .025, 'side_conversation': .025, 'unfinished': .025, 'unclear': .025},
                 'explicit_probability': .99}
        async def scenario(h):
            await h.candidate('x', h.audio('claims'), final=True)
            await h.settle()
            self.assertEqual(h.last_state('x'), 'ignore')                      # the fixed rule read 'silent at 0.99'
            self.assertEqual(h.accepted_turns(), [])
            marks = [m for r in importlib.import_module('plugins._convo.helpers.convo_metrics').LOG.records(200) if r['turn'] == 'x' for m in r['marks'] if m['name'] == 'policy.gate']
            self.assertEqual([m['gate'] for m in marks], ['fixed'])           # and the record says which rule decided
        self.play(scenario, classifier={'claims': script(lying, .2)})

    def test_name_mode_answers_only_when_addressed_by_name(self):
        async def scenario(h):
            await h.candidate('n1', h.audio('talking nearby'), final=True)
            await h.settle()
            await h.candidate('n2', h.audio('hey jarvis'), final=True)
            await h.settle()
            self.assertEqual([h.last_state('n1'), h.last_state('n2')], ['ignore', 'admit'])
            self.assertEqual(h.accepted_turns(), ['n2'])
        self.play(scenario, mode='name', classifier={'talking nearby': script({**SPEAK, 'addressed_by_name': False}, .2),
                                                      'hey jarvis': script({**SPEAK, 'addressed_by_name': True}, .2)},
                  config={'ambient_enabled': True, 'activation_names': ['Jarvis'],
                          'roles': {'classifier': {'capabilities': {'audio_input': True}}, 'conversation': {'capabilities': {'audio_input': True}}}})

    def test_retarget_during_a_reply_drops_its_late_output(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(1.1)
            moved_at = h.now()
            await h.retarget('b')
            await h.advance(5)
            self.assertEqual(h.state['session']['target'], 'b')
            self.assertEqual([t for t, _ in h.audible('q') if t > moved_at], [])
            self.assertEqual(h.store.context('b')['turns'], [])
            await h.candidate('n', h.audio('next'), final=True)
            await h.settle()
            self.assertEqual([t['id'] for t in h.store.context('b')['turns']], ['n'])
        self.play(scenario, model_deltas=LONG_REPLY, tts_delay=.2, classifier={'question': script(SPEAK, .2), 'next': script(SPEAK, .2)})

    def test_disconnect_cancels_the_classifier_and_nothing_more_is_sent(self):
        async def scenario(h):
            h.start_reply()
            await h.candidate('p', h.audio('slow'), final=False)
            await h.until(.5)
            await h.handler.on_disconnect('browser')
            sent = len(h.timeline)
            await h.advance(5)
            self.assertEqual(len(h.log('policy.cancelled')), 1)
            self.assertEqual(len(h.timeline), sent)
        self.play(scenario, classifier={'slow': script(SPEAK, 2.0)})


class ActionContract(Scenario):
    def test_unauthorised_action_never_queues_work_or_calls_a_tool(self):
        # name: (classifier decision, model proposal, does the model get called, is the proposal reported as an error)
        cases = {
            'not an explicit request': (script({**DELEGATE, 'explicit_request': False}), DELEGATE_TASK, True, True),
            'explicit but unsure': (script({**DELEGATE, 'confidence': .8}), DELEGATE_TASK, False, False),
            'speech only, model proposes a tool anyway': (script(SPEAK), DELEGATE_TASK, True, True),
            'a read-only tool the classifier did not allow': (script(SPEAK), WEB_SEARCH, True, True),
            'two proposals at once': (script(DELEGATE), [DELEGATE_TASK, DELEGATE_TASK], True, True),
            'a read-only tool proposed under a delegate decision': (script(DELEGATE), WEB_SEARCH, True, True),
        }
        for name, (decision, proposal, model_called, reported) in cases.items():
            with self.subTest(name):
                async def scenario(h):
                    await h.candidate('t', h.audio('ask'), final=True)
                    await h.settle()
                    self.assertEqual((h.jobs(), h.host.directs), ([], []))
                    self.assertEqual(bool(h.answers), model_called)           # the case reached the branch it names
                    self.assertEqual(bool(h.events('error')), reported)
                self.play(scenario, classifier={'ask': decision}, proposal=proposal)
                record('unauthorised:' + name, jobs=0)

    def test_authorised_delegation_queues_exactly_one_job_and_a_replay_is_a_noop(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            jobs = h.jobs()
            self.assertEqual([(j['turn'], j['prompt'], j['status']) for j in jobs], [('t', 'Write the weekly report', 'queued')])
            self.assertIn('wake', h.dispatches)
            self.assertEqual(len(h.events('job.queued')), 1)
            self.assertTrue(any('Queued that in the selected chat.' in text for _, text in h.spoken))
            again = await h.candidate('t', h.audio('ask again'), sequence=2, final=True)
            self.assertTrue(again.get('duplicate'))
            await h.settle()
            self.assertEqual(len(h.jobs()), 1)
        self.play(scenario, classifier={'ask': script(DELEGATE, .2)}, proposal=DELEGATE_TASK)

    def test_a_queued_job_is_reported_as_queued_and_each_later_stage_has_its_own_record(self):
        """BP7: the confirmation says only what is true when it is spoken. Running and completed are separate, journaled facts."""
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            said = ' '.join(p.get('text', '') for _, p in h.events('text.delta', 't')) + ' ' + ' '.join(text for _, text in h.spoken)
            queued = [(j['status'], j['result']) for j in h.jobs()]
            job = h.jobs()[0]['id']
            for expected, status in (('queued', 'dispatching'), ('dispatching', 'running'), ('running', 'completed')):
                self.assertTrue(h.store.transition(job, expected, status, 'All done' if status == 'completed' else ''))
            return said.lower(), queued, [r['kind'] for r in h.journal() if r['kind'].startswith('job.')]
        (said, queued, kinds), _ = self.play(scenario, classifier={'ask': script(DELEGATE, .2)}, proposal=DELEGATE_TASK)
        self.assertIn('queued', said)
        for word in ('done', 'finished', 'complete', 'ready', 'running', 'started'):
            self.assertNotIn(word, said)
        self.assertEqual(queued, [('queued', '')])
        self.assertEqual(kinds, ['job.queued', 'job.dispatching', 'job.running', 'job.completed'])

    def test_delegated_task_arguments_are_validated_before_any_job_exists(self):
        for name, task in {'blank': '   ', 'missing': None, 'oversized': 'x' * 16001}.items():
            with self.subTest(name):
                arguments = {} if task is None else {'task': task}
                async def scenario(h):
                    await h.candidate('t', h.audio('ask'), final=True)
                    await h.settle()
                    self.assertEqual(h.jobs(), [])
                    self.assertTrue(h.events('error'))                      # the user is told, nothing was queued
                self.play(scenario, classifier={'ask': script(DELEGATE, .2)},
                          proposal={'type': 'tool.proposal', 'name': 'delegate_task', 'arguments': arguments})

    def test_explicit_read_only_request_uses_the_direct_tool_once_and_answers(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            self.assertEqual(h.host.directs, [('a', 'web_search', {'query': 'weather'})])
            self.assertEqual([a['allow_tools'] for a in h.answers], [True, False])
            self.assertIn('Read-only tool result', h.answers[1]['system'])
            self.assertEqual(h.jobs(), [])
            self.assertIsNotNone(h.first_audible('t'))
        self.play(scenario, classifier={'ask': script(DIRECT, .2)}, proposal=WEB_SEARCH)


class ReplyMarkup(Scenario):
    """A model that writes a tool call as plain text, or says nothing: neither is spoken, shown or run, and the user is told."""
    LEAD = 'Sure, I will look that up for you right now. '
    MARKUP = '<tool_call><function=web_search><parameter=query>library hours</parameter></function></tool_call>'

    def reply(self, deltas):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return {'spoken': ' '.join(text for _, text in h.spoken), 'shown': ''.join(p.get('text', '') for _, p in h.events('text.delta', 'q')),
                    'errors': [p['message'] for _, p in h.events('error', 'q')], 'jobs': len(h.jobs()), 'settled': len(h.journal('assistant.generated'))}
        return self.play(scenario, classifier={'question': script(SPEAK, .2)}, model_deltas=deltas, tts_delay=.1)[0]

    def test_markup_after_a_sentence_keeps_the_sentence_and_drops_the_markup(self):
        result = self.reply([(self.LEAD, .1), (self.MARKUP, .1)])
        self.assertEqual(result['spoken'].strip(), self.LEAD.strip())
        self.assertEqual(result['shown'].strip(), self.LEAD.strip())
        self.assertEqual(len(result['errors']), 1)
        self.assertIn('tool call', result['errors'][0])
        self.assertEqual((result['jobs'], result['settled']), (0, 1))                 # nothing was run; the turn still ends cleanly

    def test_markup_split_across_deltas_is_caught_too(self):
        result = self.reply([(self.LEAD + '<tool', .1), ('_call><function=x>', .1), (' trailing words', .1)])
        for where in ('spoken', 'shown'):
            self.assertNotIn('<', result[where])
            self.assertNotIn('trailing', result[where])
        self.assertEqual(len(result['errors']), 1)

    def test_a_reply_that_is_only_markup_says_nothing_and_tells_the_user(self):
        result = self.reply([(self.MARKUP, .1)])
        self.assertEqual((result['spoken'], result['shown']), ('', ''))
        self.assertEqual(len(result['errors']), 1)
        self.assertIn('tool call', result['errors'][0])

    def test_an_empty_reply_is_reported_not_met_with_silence(self):
        result = self.reply([('', .1)])
        self.assertEqual((result['spoken'], result['shown']), ('', ''))
        self.assertEqual(len(result['errors']), 1)
        self.assertIn('no reply', result['errors'][0])

    def test_a_fragment_held_back_at_the_end_of_the_reply_is_still_spoken(self):
        result = self.reply([(self.LEAD, .1), ('the answer is below 5 <', .1)])
        self.assertTrue(result['spoken'].rstrip().endswith('5 <'))
        self.assertTrue(result['shown'].rstrip().endswith('5 <'))
        self.assertEqual(result['errors'], [])

    def test_an_ordinary_reply_is_untouched(self):
        result = self.reply([(self.LEAD, .1), ('If a < b then it is fine, and 5 > 3.', .1)])
        self.assertIn('a < b', result['spoken'])
        self.assertEqual(result['errors'], [])


class AudioBound(unittest.TestCase):
    def test_server_bound_for_one_audio_turn(self):
        """The browser's maximum-length cut must stay within this bound; the client side is checked by the replay tests."""
        validate_audio(make_frames(300, 320000), 20)
        with self.assertRaises(ContractError):
            validate_audio(make_frames(300, 320170), 20)         # what the old block-edge cut produced at 48 kHz (BP3a cuts on the sample now)
        with self.assertRaises(ContractError):
            validate_audio(make_wav(300, 0.2, rate=44100), 20)  # only 16/24/48 kHz are accepted


class FinalClassification(Scenario):
    """BP3d: a completed turn is never lost to one slow or failed classifier call. It is asked again once, by the same
    classifier and only after the first call has let go of it; a probe is never retried."""

    def final_with(self, *scripts, until=10, **options):
        async def scenario(h):
            await h.candidate('f', h.audio('request'), final=True)
            await h.advance(until)
            calls = h.log('policy.start')
            return {'states': [(round(t, 2), s) for t, s, *_ in h.states('f')], 'accepted': h.accepted_turns(), 'heard': len(h.audible()),
                    'calls': len(calls), 'starts': [round(c[1] - h.t0, 2) for c in calls], 'late': [round(c[1] - h.t0, 2) for c in h.log('policy.late')],
                    'errors': len(h.events('error', 'f'))}
        facts, _ = self.play(scenario, classifier={'request': list(scripts)}, **options)
        return facts

    def test_a_final_whose_classification_times_out_is_asked_again_once_and_answered(self):
        facts = self.final_with(script(SPEAK, 2.0), script(SPEAK, .2))
        self.assertEqual([s for _, s in facts['states']], ['considering', 'admit'])
        self.assertAlmostEqual(facts['states'][1][0], 1.7, delta=.02)           # the deadline, then the second answer
        self.assertEqual((facts['accepted'], facts['calls']), (['f'], 2))
        self.assertEqual(facts['heard'], 1)

    def test_a_final_whose_classification_fails_is_asked_again_once_and_answered(self):
        facts = self.final_with(script(SPEAK, .2, fail=True), script(SPEAK, .2))
        self.assertEqual([s for _, s in facts['states']], ['considering', 'admit'])
        self.assertAlmostEqual(facts['states'][1][0], .4, delta=.02)             # no deadline to wait for: the failure is immediate
        self.assertEqual((facts['accepted'], facts['calls'], facts['heard']), (['f'], 2, 1))

    def test_a_final_that_fails_twice_is_handed_back_after_exactly_two_calls(self):
        facts = self.final_with(script(SPEAK, 2.0), script(SPEAK, 2.0), script(SPEAK, .2))
        self.assertEqual([s for _, s in facts['states']], ['considering', 'degraded'])
        self.assertAlmostEqual(facts['states'][1][0], 3.0, delta=.02)            # two deadlines, never a third call
        self.assertEqual((facts['accepted'], facts['calls'], facts['heard']), ([], 2, 0))

    def test_the_retry_waits_for_a_cancellation_ignoring_call_and_never_overlaps_it(self):
        facts = self.final_with(script(SPEAK, 2.0, stubborn=1), script(SPEAK, .2))
        self.assertEqual([s for _, s in facts['states']], ['considering', 'admit'])
        self.assertEqual(facts['calls'], 2)
        self.assertEqual(len(facts['late']), 1)
        self.assertGreaterEqual(facts['starts'][1], facts['late'][0])           # the second call began only after the first returned
        self.assertAlmostEqual(facts['states'][1][0], 2.2, delta=.02)

    def test_a_call_that_never_lets_go_hands_the_turn_back_without_a_second_call(self):
        facts = self.final_with(script(SPEAK, 6.0, stubborn=1), script(SPEAK, .2))
        self.assertEqual([s for _, s in facts['states']], ['considering', 'degraded'])
        self.assertAlmostEqual(facts['states'][1][0], 3.5, delta=.02)            # 1.5 s deadline + 2 s wait for release
        self.assertEqual((facts['calls'], facts['accepted']), (1, []))

    def test_a_probe_is_never_retried(self):
        async def scenario(h):
            await h.candidate('p', h.audio('slow'), sequence=1, final=False)
            await h.advance(6)
            return {'states': [s for _, s, *_ in h.states('p')], 'calls': len(h.log('policy.start'))}
        facts, _ = self.play(scenario, classifier={'slow': [script(SPEAK, 2.0), script(SPEAK, .2)]})
        self.assertEqual((facts['states'], facts['calls']), (['considering', 'degraded'], 1))

    def test_a_final_arriving_while_an_earlier_probe_call_is_still_running_waits_for_it(self):
        async def scenario(h):
            await h.candidate('p', h.audio('slow'), sequence=1, final=False)       # times out at 1.5 s but ignores the cancellation until 2.0 s
            await h.until(1.7)
            await h.candidate('p', h.audio('whole'), sequence=2, final=True)
            await h.advance(6)
            return {'states': [(round(t, 2), s) for t, s, *_ in h.states('p')], 'accepted': h.accepted_turns(), 'heard': len(h.audible()),
                    'starts': [round(c[1] - h.t0, 2) for c in h.log('policy.start')]}
        facts, _ = self.play(scenario, classifier={'slow': script(SPEAK, 2.0, stubborn=1), 'whole': script(SPEAK, .2)})
        self.assertEqual(facts['accepted'], ['p'])                                  # it was not dropped as 'degraded'
        self.assertEqual(facts['heard'], 1)
        self.assertEqual(len(facts['starts']), 2)
        self.assertGreaterEqual(facts['starts'][1], 2.0)                             # only after the detached call returned

    def test_a_disconnect_while_waiting_to_retry_starts_no_second_call(self):
        async def scenario(h):
            await h.candidate('f', h.audio('request'), final=True)
            await h.until(1.7)                                  # timed out at 1.5 s; the first call is still releasing the classifier
            await h.handler.on_disconnect('browser')
            sent = len(h.timeline)
            await h.advance(6)
            self.assertEqual(len(h.timeline), sent)             # nothing more is sent to a closed connection
            return {'calls': len(h.log('policy.start')), 'accepted': h.accepted_turns()}
        facts, _ = self.play(scenario, classifier={'request': [script(SPEAK, 2.0, stubborn=1), script(SPEAK, .2)]})
        self.assertEqual((facts['calls'], facts['accepted']), (1, []))


class BusyBackend(Scenario):
    """A native decision backend cannot be interrupted by cancelling its caller: after a timeout it stays busy until the
    call returns, and says so with a typed PolicyBusy. A completed turn waits for it, briefly; a probe never does."""

    def with_busy(self, busy_calls, *, final=True, until=6, classifier=None):
        from plugins._convo.helpers.convo_metrics import LOG
        from plugins._convo.helpers.convo_system_one import PolicyBusy
        busy_marks = lambda: sum(1 for r in LOG.records(500) for x in r['marks'] if x['name'] == 'policy.busy')   # the log is process-wide: count what this run adds
        async def scenario(h):
            original = h.handler._policy
            counter = {'calls': 0}
            async def policy(state, payload):
                counter['calls'] += 1
                if counter['calls'] <= busy_calls:
                    raise PolicyBusy('The decision backend is still finishing an earlier call')
                return await original(state, payload)
            h.handler._policy = policy
            before = busy_marks()
            await h.candidate('f', h.audio('request'), sequence=1, final=final)
            await h.advance(until)
            return {'states': [(round(t, 2), s) for t, s, *_ in h.states('f')], 'accepted': h.accepted_turns(), 'calls': counter['calls'],
                    'busy_marks': busy_marks() - before}
        facts, _ = self.play(scenario, classifier=classifier or {'request': script(SPEAK, .2)})
        return facts

    def test_a_completed_turn_waits_for_a_busy_backend_and_is_answered(self):
        facts = self.with_busy(3)
        self.assertEqual([s for _, s in facts['states']], ['considering', 'admit'])
        self.assertAlmostEqual(facts['states'][1][0], .3 + .2, delta=.05)         # three polls of 0.1 s, then the 0.2 s call
        self.assertEqual((facts['accepted'], facts['calls'], facts['busy_marks']), (['f'], 4, 1))

    def test_a_backend_that_stays_busy_hands_the_turn_back_after_the_bound_without_a_second_round(self):
        facts = self.with_busy(1000, until=10)
        self.assertEqual([s for _, s in facts['states']], ['considering', 'degraded'])
        self.assertAlmostEqual(facts['states'][1][0], 2.0, delta=.15)             # the busy wait is 2 s and is not repeated by a retry
        self.assertEqual(facts['accepted'], [])
        self.assertLessEqual(facts['calls'], 25)

    def test_a_probe_never_waits_for_a_busy_backend(self):
        facts = self.with_busy(1000, final=False, until=5)
        self.assertEqual([s for _, s in facts['states']], ['considering', 'degraded'])
        self.assertAlmostEqual(facts['states'][1][0], 0.0, delta=.02)
        self.assertEqual(facts['calls'], 1)

    def test_a_revoked_turn_stops_waiting(self):
        from plugins._convo.helpers.convo_system_one import PolicyBusy
        async def scenario(h):
            counter = {'calls': 0}
            async def policy(state, payload):
                counter['calls'] += 1
                raise PolicyBusy('busy')
            h.handler._policy = policy
            await h.candidate('f', h.audio('request'), final=True)
            await h.until(.5)
            await h.interrupt()                                                      # the user pressed Stop meanwhile
            before = counter['calls']
            await h.advance(5)
            return before, counter['calls']
        (before, after), _ = self.play(scenario)
        self.assertEqual(before, after)                                              # no further call once the turn was revoked


class SpeechAdmissionWait(Scenario):
    """BP3e: a barge-in detaches a synthesis that the speech provider keeps draining, so the next reply's first speech
    request is told the provider is busy. The reply waits for it (bounded) instead of failing."""

    def barge_in_during_synthesis(self, tts_delay=3.0, then=None):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(1.0)                                    # the first phrase is being synthesised (it takes tts_delay)
            await h.candidate('s', h.audio('stop-probe'), sequence=1, final=False)
            await h.until(1.5)
            await h.candidate('s', h.audio('stop-final'), sequence=2, final=True)
            if then:
                await then(h)
            await h.settle()
            return {'rejected': [round(c[1] - h.t0, 2) for c in h.log('speech.409')], 'errors': h.events('error', 's'),
                    'heard': h.first_audible('s'), 'started': [round(c[1] - h.t0, 2) for c in h.log('speech.start')]}
        facts, _ = self.play(scenario, model_deltas=LONG_REPLY, tts_delay=tts_delay, tts_admission=True, config={'continuation_seconds': 0},   # these test the speech wait, not joining
                             classifier={'question': script(SPEAK, .2), 'stop-probe': script(SPEAK, .3), 'stop-final': script(SPEAK, .2)})
        return facts

    def test_the_next_reply_waits_for_the_provider_and_is_heard(self):
        facts = self.barge_in_during_synthesis()
        self.assertGreaterEqual(len(facts['rejected']), 1)        # it did meet the busy provider ...
        self.assertEqual(facts['errors'], [])                     # ... and treated that as a wait, not a failure
        self.assertIsNotNone(facts['heard'])
        self.assertGreater(facts['heard'], 3.5)                   # not before the draining synthesis ended (0.5 s + 3 s)
        self.assertLess(facts['heard'], 3.5 + 3.0 + .5)           # and then as soon as the provider was free + its own synthesis time
        first_request = facts['started'][1]
        self.assertAlmostEqual(first_request, 3.5, delta=.3)      # polled until free, then spoke at once

    def test_the_wait_polls_gently_and_is_bounded(self):
        facts = self.barge_in_during_synthesis(tts_delay=30.0)    # the provider stays busy for the whole wait
        self.assertEqual(len(facts['errors']), 1)                 # a provider that never frees up is a failure after the bound
        self.assertIsNone(facts['heard'])
        gaps = [b - a for a, b in zip(facts['rejected'], facts['rejected'][1:])]
        self.assertTrue(all(.2 <= gap <= .3 for gap in gaps), gaps)
        self.assertAlmostEqual(facts['rejected'][-1] - facts['rejected'][0], 8.0, delta=.3)
        self.assertLess(len(facts['rejected']), 40)

    def test_a_reply_revoked_while_it_waits_stops_polling_and_reports_nothing(self):
        counted = {}
        async def interrupt_again(h):
            await h.until(2.6)                                    # the second reply is waiting for the provider
            counted['before'] = len(h.log('speech.409'))
            await h.interrupt()
        facts = self.barge_in_during_synthesis(tts_delay=30.0, then=interrupt_again)
        self.assertGreaterEqual(counted['before'], 1)
        self.assertEqual(len(facts['rejected']), counted['before'])   # no request after the revocation
        self.assertEqual(facts['errors'], [])

    def test_only_a_tracked_busy_provider_is_waited_for(self):
        """Any other speech failure still fails the reply straight away: nothing is retried blindly."""
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return {'errors': len(h.events('error', 'q')), 'speech': len(h.log('speech.start'))}
        async def broken(self, role, text):
            self.calls.append('speech')
            self.log.append(('speech.start', asyncio.get_running_loop().time(), text))
            raise ValueError('Speech provider rejected the request')
        original = ScriptedSidecar.speech
        ScriptedSidecar.speech = broken
        try:
            facts, _ = self.play(scenario, classifier={'question': script(SPEAK, .2)})
        finally:
            ScriptedSidecar.speech = original
        self.assertEqual(facts, {'errors': 1, 'speech': 1})

    def test_the_experimental_pcm_stream_waits_too_and_never_replays_audio(self):
        async def scenario(h):
            h.store.accept_turn(h.session['id'], 'browser', 0, 'pcm', {'transcript': 'Question'})
            attempts = []
            async def chunks(role, text):
                attempts.append(asyncio.get_running_loop().time() - h.t0)
                if len(attempts) <= 2:
                    raise SpeechBusy('draining')                  # refused before any audio exists
                yield {'audio': b'\0\0', 'format': 'pcm_s16le', 'sample_rate': 24000, 'channels': 1}
                yield {'audio': b'\0\0', 'format': 'pcm_s16le', 'sample_rate': 24000, 'channels': 1}
            h.sidecar.speech_stream = chunks
            await h.handler._speak('browser', h.state, h.session, 'pcm', 'Answer', {'capabilities': {'native_pcm': True}})
            return {'kinds': [p['type'] for _, p in h.timeline if p.get('type', '').startswith('audio.')], 'attempts': [round(a, 2) for a in attempts]}
        facts, _ = self.play(scenario)
        self.assertEqual(facts['kinds'], ['audio.begin', 'audio.chunk', 'audio.chunk', 'audio.end'])
        self.assertEqual(len(facts['attempts']), 3)
        self.assertAlmostEqual(facts['attempts'][2] - facts['attempts'][0], .5, delta=.05)


class KnownGaps(Scenario):
    """Each gap has a facts function, a baseline test (current behaviour) and a target test (desired behaviour)."""

    # ---- BP5: a continuation joins the unanswered fragment (more cases: test_continuation.py)
    def continuation(self):
        async def scenario(h):
            first, second = h.audio('part one', seconds=1.0), h.audio('part two', level=900, seconds=1.0)
            await h.candidate('f1', first, final=True)
            await h.until(.9)                                      # the user carries on before the reply was heard
            await h.candidate('f2', second, final=True)
            await h.settle()
            return {'accepted': h.accepted_turns(), 'heard': len(h.audible()), 'final_request_frames': frame_count(h.answers[-1]['audio']),
                    'fragment_frames': frame_count(first), 'cancelled_first': h.events('candidate.admitted', 'f2') != []}
        facts, _ = self.play(scenario, model_deltas=[('Sure, I can set that up for you. ', 1.5)], default=SPEAK,
                             classifier={'part one': script(SPEAK, .2), 'part two': script(SPEAK_PLAIN, .2)})   # a continuation, not a short explicit command
        return facts

    def test_continuation_before_the_reply_is_heard_becomes_one_request(self):
        """BP5: the first fragment's unheard reply is superseded; one request carries both fragments' audio and exactly one reply is heard."""
        facts = self.continuation()
        self.assertEqual(facts['final_request_frames'], 2 * facts['fragment_frames'])
        self.assertEqual(facts['heard'], 1)
        self.assertEqual(facts['accepted'], ['f1', 'f2'])                               # the superseded turn stays in the journal (as superseded)

    # ---- BP7: the classifier's route is not binding on the model's tool choice
    def direct_classification_with_background_proposal(self):
        async def scenario(h):
            await h.candidate('t', h.audio('lookup'), final=True)
            await h.settle()
            return {'jobs': len(h.jobs())}
        facts, _ = self.play(scenario, classifier={'lookup': script(DIRECT, .2)}, proposal=DELEGATE_TASK)
        return facts

    def test_baseline_direct_classification_can_still_launch_background_work(self):
        """BP7 baseline: 'direct' authorises acting at all, so a proposed background job is accepted."""
        self.assertEqual(self.direct_classification_with_background_proposal()['jobs'], 1)

    @unittest.expectedFailure
    def test_target_direct_classification_cannot_launch_background_work(self):
        """BP7 target (routing contract): background work needs the classifier's own 'delegate' route."""
        self.assertEqual(self.direct_classification_with_background_proposal()['jobs'], 0)


class ModalityContract(Scenario):
    """BP3f: the classifier's audio flag never turns an audio conversation into transcript input. The conversation model's
    own capability decides what the browser sends; an incompatible classifier is refused, with the reason, up front."""

    @staticmethod
    def config(conversation_audio, classifier_audio):
        return {'enabled': True, 'roles': {
            'classifier': {'url': 'http://localhost/v1', 'model': 'classifier', 'capabilities': {'audio_input': classifier_audio}},
            'conversation': {'model': 'conversation', 'capabilities': {'audio_input': conversation_audio}}}}

    @staticmethod
    async def start_only(config):
        h = await Harness.create(config=config)
        await h.close()

    def test_an_audio_conversation_with_a_text_only_classifier_is_refused_not_converted(self):
        config = self.config(True, False)
        self.assertFalse(public_settings(validate_settings(config))['text_input_required'])     # the browser would still send audio
        with self.assertRaisesRegex(ValueError, r'classifier must too.*will not silently convert'):
            run_virtual(self.start_only(config))

    def test_the_other_combinations_start_and_the_browser_sends_what_the_conversation_model_takes(self):
        for conversation_audio, classifier_audio in ((True, True), (False, True), (False, False)):
            with self.subTest(conversation_audio=conversation_audio, classifier_audio=classifier_audio):
                config = self.config(conversation_audio, classifier_audio)
                self.assertEqual(public_settings(validate_settings(config))['text_input_required'], not conversation_audio)
                run_virtual(self.start_only(config))                                             # no refusal

    def test_the_conflict_is_reported_before_any_start_and_names_both_sides(self):
        conflict = input_modality_conflict(validate_settings(self.config(True, False)))
        self.assertIn('conversation model takes audio', conflict)
        self.assertIn('turn-policy classifier must too', conflict)
        self.assertIn('transcript-only conversation', conflict)                                 # the way out is stated
        for combination in ((True, True), (False, True), (False, False)):
            self.assertEqual(input_modality_conflict(validate_settings(self.config(*combination))), '')

    def test_a_selected_decision_backend_is_judged_by_its_declared_modalities(self):
        config = validate_settings(self.config(True, False))                                     # the classifier's own flag is irrelevant here
        ready = lambda *combinations: {'ready': True, 'capabilities': {'modality_combinations': [list(c) for c in combinations]}}
        self.assertEqual(input_modality_conflict(config, ready(('audio', 'text'))), '')
        self.assertIn('text context plus direct audio', input_modality_conflict(config, ready(('text',))))
        text_only = validate_settings(self.config(False, True))
        self.assertEqual(input_modality_conflict(text_only, ready(('text',))), '')
        self.assertIn('does not support this text-only conversation', input_modality_conflict(text_only, ready(('audio', 'text'))))
        self.assertEqual(input_modality_conflict(config, {'ready': False, 'capabilities': {}}), '')   # not ready: its own check says why


class StartRefusals(Scenario):
    """The host masks anything a handler raises as "Internal server error", so a refused start is returned as data: the user
    sees the reason, and no session or connection is left behind."""

    def refusal(self, change):
        async def scenario(h):
            await h.handler.on_disconnect('browser')
            change(h.config)
            result = await h.handler.process('convo.start', {'target': 'a'}, 'browser')
            return result, dict(h.handler.connections)
        (result, connections), _ = self.play(scenario)
        self.assertEqual(connections, {})                      # nothing owns the connection after a refusal
        return result

    def test_a_modality_conflict_is_returned_with_its_reason(self):
        result = self.refusal(lambda config: config['roles']['classifier']['capabilities'].update(audio_input=False))
        self.assertEqual((result['ok'], result['refused']), (False, True))
        self.assertIn('turn-policy classifier must too', result['message'])

    def test_an_unconfigured_classifier_is_returned_with_its_reason(self):
        result = self.refusal(lambda config: config['roles']['classifier'].update(url='', model=''))
        self.assertEqual((result['ok'], result['refused']), (False, True))
        self.assertEqual(result['message'], 'Configure a classifier before starting Convo')

    def test_development_mode_is_returned_with_its_reason(self):
        result = self.refusal(lambda config: config.update(ai_link_development_mode=True))
        self.assertTrue(result['refused'])
        self.assertIn('AI Link development mode keeps browser voice idle', result['message'])

    def test_only_a_start_is_answered_this_way_and_the_message_is_bounded(self):
        async def scenario(h):
            with self.assertRaises(ValueError):               # other events keep raising: they are protocol faults, not choices
                await h.handler.process('convo.turn', {'session_id': 'unknown', 'epoch': 0, 'turn_id': 't', 'audio': 'x'}, 'browser')
            await h.handler.on_disconnect('browser')
            h.config['roles']['classifier']['capabilities']['audio_input'] = False
            return await h.handler.process('convo.start', {'target': 'a'}, 'browser')
        result, _ = self.play(scenario)
        self.assertLessEqual(len(result['message']), 400)

    def test_a_cancelled_start_is_not_turned_into_a_refusal(self):
        async def scenario(h):
            await h.handler.on_disconnect('browser')
            entered = asyncio.Event()
            async def blocked(*args):
                entered.set()
                await asyncio.Event().wait()
            h.host.persona = blocked
            pending = asyncio.create_task(h.handler.process('convo.start', {'target': 'a'}, 'browser'))
            await asyncio.wait_for(entered.wait(), 2)
            await h.handler.on_disconnect('browser')
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.play(scenario)


if __name__ == '__main__':
    unittest.main()
