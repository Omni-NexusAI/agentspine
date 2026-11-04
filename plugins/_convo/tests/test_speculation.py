"""BP10: a held parallel reply. The main model starts on a completed utterance while the classifier decides; nothing is shown,
spoken, recorded or run until the turn is admitted. Virtual clock, scripted providers; none of this is a live measurement."""
import asyncio
import importlib
import unittest

if __package__:
    from .behavior_harness import DELEGATE, LONG_REPLY, SILENT, SPEAK, SPEAK_PLAIN, UNCERTAIN, Harness, record, run_virtual, script
    from .test_behavior_corpus import DELEGATE_TASK, WEB_SEARCH, Scenario
else:
    from behavior_harness import DELEGATE, LONG_REPLY, SILENT, SPEAK, SPEAK_PLAIN, UNCERTAIN, Harness, record, run_virtual, script
    from test_behavior_corpus import DELEGATE_TASK, WEB_SEARCH, Scenario

SPEC = {'speculative_reply': True}
REPLY = [('The first sentence of this reply is deliberately long enough to be spoken. ', .3),
         ('The second sentence follows right after it for a short while. ', .3)]


def marks(turn):
    """The content-free timing marks of one turn, in order (the log is process-wide: filter by turn)."""
    LOG = importlib.import_module('plugins._convo.helpers.convo_metrics').LOG
    return [m['name'] for r in LOG.records(200) if r['turn'] == turn for m in r['marks'] if m['name'].startswith('spec.')]


class HeldReply(Scenario):
    def setUp(self):
        importlib.import_module('plugins._convo.helpers.convo_metrics').LOG.reset()      # the log is process-wide: start each test clean

    def run_once(self, speculative, *, classifier_delay=.2, deltas=REPLY, until=None, **options):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return {'answer_start': [round(a[1] - h.t0, 3) for a in h.log('answer.start')],
                    'first_audible': h.first_audible('q'), 'first_text': (h.events('text.delta', 'q') or [(None,)])[0][0],
                    'spoken': [text for _, text in h.spoken], 'accepted': h.accepted_turns(),
                    'generated': [r['payload']['text'] for r in h.journal('assistant.generated')], 'answers': len(h.answers),
                    'admitted': (h.events('candidate.admitted', 'q') or [(None,)])[0][0]}
        facts, _ = self.play(scenario, model_deltas=deltas, config=SPEC if speculative else {}, classifier={'question': script(SPEAK, classifier_delay)}, **options)
        return facts

    def test_the_model_starts_with_the_classifier_instead_of_after_it(self):
        off, on = self.run_once(False), self.run_once(True)
        self.assertAlmostEqual(off['answer_start'][0], .2, delta=.02)        # today: after the classifier answered
        self.assertAlmostEqual(on['answer_start'][0], 0, delta=.02)          # held parallel reply: at the same moment
        self.assertEqual(on['answers'], 1)                                    # promoted: no second request
        record('speculation_first_audible', off=off['first_audible'], on=on['first_audible'], classifier_s=.2, model_first_text_s=.3, tts_s=.1)
        self.assertLess(on['first_audible'], off['first_audible'])
        self.assertAlmostEqual(off['first_audible'] - on['first_audible'], .2, delta=.05)   # exactly the classifier's time is saved

    def test_a_promoted_reply_is_the_reply_the_sequential_path_would_have_given(self):
        off, on = self.run_once(False), self.run_once(True)
        for key in ('spoken', 'accepted', 'generated'):
            self.assertEqual(on[key], off[key], key)

    def test_nothing_reaches_the_browser_the_tts_or_the_journal_before_admission(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(.9)                                   # the model has finished; the classifier still has .6 s to go
            held = {'events': [p['type'] for _, p in h.timeline if p['type'] not in {'candidate.state'}], 'speech': len(h.log('speech.start')),
                    'journal': h.store.events('a'), 'answers': len(h.answers)}
            await h.settle()
            return held, h.events('candidate.admitted', 'q')[0][0], h.events('text.delta', 'q')[0][0]
        (held, admitted_at, text_at), _ = self.play(scenario, model_deltas=[('A complete answer, held for the classifier.', .1)], config=SPEC,
                                                    classifier={'question': script(SPEAK, 1.4)})
        self.assertEqual(held, {'events': [], 'speech': 0, 'journal': [], 'answers': 1})
        self.assertGreaterEqual(text_at, admitted_at)            # released in order, only after the admission

    def test_an_ignored_utterance_discards_the_held_reply_and_leaves_no_trace(self):
        async def scenario(h):
            await h.candidate('tv', h.audio('television'), final=True)
            await h.settle()
            return {'answers': len(h.answers), 'journal': h.store.events('a'), 'audible': h.audible(), 'text': h.events('text.delta'),
                    'speech': len(h.log('speech.start')), 'marks': marks('tv'), 'cancelled': len(h.log('answer.cancelled'))}
        facts, _ = self.play(scenario, config=SPEC, model_deltas=[('Something nobody asked for.', 5)], classifier={'television': script(SILENT, .2)})
        self.assertEqual((facts['journal'], facts['audible'], facts['text'], facts['speech']), ([], [], [], 0))
        self.assertEqual(facts['answers'], 1)                       # it did start (the cost) ...
        self.assertEqual(facts['cancelled'], 1)                     # ... and was cancelled, not left running
        self.assertEqual(facts['marks'], ['spec.start', 'spec.discard'])

    def test_a_classifier_that_fails_twice_never_promotes(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.advance(8)
            return {'states': [s for _, s, *_ in h.states('q')], 'journal': h.store.events('a'), 'audible': h.audible(), 'cancelled': len(h.log('answer.cancelled')),
                    'marks': marks('q')}
        facts, _ = self.play(scenario, config=SPEC, model_deltas=[('A reply that must not be used.', .1)],
                             classifier={'question': [script(SPEAK, .2, fail=True), script(SPEAK, .2, fail=True)]})
        self.assertEqual(facts['states'], ['considering', 'degraded'])
        self.assertEqual((facts['journal'], facts['audible']), ([], []))
        self.assertEqual(facts['marks'], ['spec.start', 'spec.discard'])

    def test_a_classifier_timeout_that_recovers_still_promotes_the_one_held_reply(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return {'answers': len(h.answers), 'accepted': h.accepted_turns(), 'marks': marks('q')}
        facts, _ = self.play(scenario, config=SPEC, classifier={'question': [script(SPEAK, 2.0), script(SPEAK, .2)]})
        self.assertEqual(facts, {'answers': 1, 'accepted': ['q'], 'marks': ['spec.start', 'spec.promote']})

    def test_tool_proposals_are_only_recorded_until_admission_then_run_through_the_unchanged_rules(self):
        async def scenario(h):
            woken = list(h.dispatches)                          # the session start wakes the dispatcher once
            await h.candidate('t', h.audio('ask'), final=True)
            await h.until(1.0)                                  # the model has proposed a job; the classifier is still deciding
            before = {'jobs': h.jobs(), 'wake': list(h.dispatches) == woken, 'directs': list(h.host.directs)}
            await h.settle()
            return before, [(j['turn'], j['prompt'], j['status']) for j in h.jobs()], len(h.answers), marks('t')
        (before, jobs, answers, seen), _ = self.play(scenario, config=SPEC, proposal=DELEGATE_TASK, model_deltas=[('Okay.', .2)],
                                                     classifier={'ask': script(DELEGATE, 1.4)})
        self.assertEqual(before, {'jobs': [], 'wake': True, 'directs': []})
        self.assertEqual(jobs, [('t', 'Write the weekly report', 'queued')])
        self.assertEqual((answers, seen), (1, ['spec.start', 'spec.promote']))

    def test_a_proposal_the_classifier_did_not_authorise_discards_the_held_reply_and_changes_nothing(self):
        """The held request offered tools; this turn may not use them. It is answered like any ordinary turn: the proposal is refused."""
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return {'jobs': h.jobs(), 'directs': h.host.directs, 'answers': [a['allow_tools'] for a in h.answers], 'errors': len(h.events('error')), 'marks': marks('t')}
        facts, _ = self.play(scenario, config=SPEC, proposal=DELEGATE_TASK, classifier={'ask': script(SPEAK, .2)})
        self.assertEqual((facts['jobs'], facts['directs']), ([], []))
        self.assertEqual(facts['answers'], [True, False])            # the held one offered tools; the real one did not
        self.assertEqual(facts['errors'], 1)
        self.assertEqual(facts['marks'], ['spec.start', 'spec.discard'])

    def test_a_proposal_that_arrives_after_promotion_on_an_unauthorised_turn_is_dropped_not_an_error(self):
        """Found live: the held reply was promoted while still generating and proposed a tool a moment later; the turn failed with
        a generic error although the user had been answered. The proposal is dropped (nothing runs); the text is the reply."""
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return {'jobs': h.jobs(), 'directs': h.host.directs, 'answers': [a['allow_tools'] for a in h.answers], 'errors': len(h.events('error')),
                    'heard': h.first_audible('t') is not None, 'marks': marks('t')}
        facts, _ = self.play(scenario, config=SPEC, proposal=DELEGATE_TASK, model_deltas=[('Okay.', 1.5)], classifier={'ask': script(SPEAK, .2)})
        self.assertEqual((facts['jobs'], facts['directs'], facts['errors']), ([], [], 0))
        self.assertEqual(facts['answers'], [True])                   # one model operation: the held reply was kept
        self.assertTrue(facts['heard'])
        self.assertEqual(facts['marks'], ['spec.start', 'spec.promote', 'spec.tool_dropped'])

    def test_a_promoted_reply_that_was_only_a_dropped_proposal_is_asked_again_without_tools(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return {'jobs': h.jobs(), 'answers': [a['allow_tools'] for a in h.answers], 'marks': marks('t')}
        facts, _ = self.play(scenario, config=SPEC, proposal=DELEGATE_TASK, model_deltas=[('', 1.5)], classifier={'ask': script(SPEAK, .2)},
                             proposal_always=False)
        self.assertEqual(facts['jobs'], [])
        self.assertEqual(facts['answers'], [True, False])
        self.assertIn('spec.tool_dropped', facts['marks'])

    def test_a_clarifying_turn_is_answered_by_a_fresh_request_with_its_own_instruction(self):
        clarify = {'speech': 'clarify', 'action': 'none', 'confidence': .75}
        async def scenario(h):
            await h.candidate('c', h.audio('unsure'), final=True)
            await h.settle()
            return {'asks': ['Ask one short in-character question' in a['system'] for a in h.answers], 'heard': h.first_audible('c') is not None, 'marks': marks('c')}
        facts, _ = self.play(scenario, config=SPEC, classifier={'unsure': script(clarify, .2)})
        self.assertEqual(facts, {'asks': [False, True], 'heard': True, 'marks': ['spec.start', 'spec.discard']})

    def test_a_reply_in_flight_means_no_second_model_operation(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(1.0)                                  # the first reply is being generated
            started = len(h.answers)
            await h.candidate('n', h.audio('next'), final=True)
            await h.until(1.1)
            return started, len(h.answers), [m for m in marks('n')]
        (before, during, seen), _ = self.play(scenario, config=SPEC, model_deltas=LONG_REPLY, tts_delay=.2,
                                              classifier={'question': script(SPEAK, .2), 'next': script(SPEAK, .2)})
        self.assertEqual((before, during, seen), (1, 1, []))        # barge-in candidates are classified first, as always

    def test_stop_cancels_the_held_reply_within_two_seconds_even_if_the_provider_ignores_it(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(.5)
            started = h.now()
            await h.interrupt()
            took = h.now() - started
            await h.advance(10)
            return {'took': took, 'audible': h.audible(), 'text': h.events('text.delta'), 'journal': h.store.events('a')}
        facts, _ = self.play(scenario, config=SPEC, stubborn_answer=4.0, model_deltas=[('Late.', .1), ('Later.', .1)], classifier={'question': script(SPEAK, 3.0)})
        self.assertLessEqual(facts['took'], 2.0)
        self.assertEqual((facts['audible'], facts['text'], facts['journal']), ([], [], []))

    def test_retarget_drops_the_held_reply(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(.5)
            await h.retarget('b')
            await h.advance(10)
            return {'audible': h.audible(), 'text': h.events('text.delta'), 'turns': h.store.context('b')['turns'], 'marks': marks('q')}
        facts, _ = self.play(scenario, config=SPEC, classifier={'question': script(SPEAK, 3.0)}, model_deltas=[('Late.', .1)])
        self.assertEqual((facts['audible'], facts['text'], facts['turns']), ([], [], []))
        self.assertEqual(facts['marks'], ['spec.start', 'spec.discard'])

    def test_ambient_and_name_listening_never_speculate(self):
        for mode in ('ambient', 'name'):
            with self.subTest(mode):
                async def scenario(h):
                    await h.candidate('n', h.audio('talking'), final=True)
                    await h.settle()
                    return len(h.answers), marks('n')
                (answers, seen), _ = self.play(scenario, mode=mode, classifier={'talking': script(SILENT, .2)},
                                               config={**SPEC, 'ambient_enabled': True, 'activation_names': ['Jarvis'],
                                                       'roles': {'classifier': {'capabilities': {'audio_input': True}}, 'conversation': {'capabilities': {'audio_input': True}}}})
                self.assertEqual((answers, seen), (0, []))

    def test_a_held_reply_is_bounded_and_an_overflowing_one_is_not_promoted(self):
        speculation = importlib.import_module('plugins._convo.helpers.convo_speculation')
        async def scenario(h):
            big = [('x' * 4000, .01)] * 4
            h.model_deltas = big
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return len(h.answers), marks('q'), len(h.events('error', 'q'))
        (answers, seen, errors), _ = self.play(scenario, config=SPEC, classifier={'question': script(SPEAK, 1.0)})
        self.assertEqual(answers, 2)                                 # the overflowing held reply was dropped; the turn asked again
        self.assertEqual(seen, ['spec.start', 'spec.discard'])
        self.assertLessEqual(speculation.MAX_CHARS, 12000)

    def test_a_failure_to_start_the_held_reply_never_affects_the_turn(self):
        async def scenario(h):
            def broken(*args, **kwargs):
                raise RuntimeError('boom')
            h.handler._compose = broken
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return h.accepted_turns(), len(h.answers), marks('q'), h.first_audible('q') is not None
        facts, _ = self.play(scenario, config=SPEC, classifier={'question': script(SPEAK, .2)})
        self.assertEqual(facts, (['q'], 1, [], True))

    def test_the_setting_off_is_the_sequential_path(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return [a[1] - h.t0 for a in h.log('answer.start')], marks('q')
        (starts, seen), _ = self.play(scenario, classifier={'question': script(SPEAK, .2)})
        self.assertAlmostEqual(starts[0], .2, delta=.02)
        self.assertEqual(seen, [])

    def test_the_setting_is_validated(self):
        contract = importlib.import_module('plugins._convo.helpers.convo_contract')
        for key in ('speculative_reply', 'main_listen_decision', 'first_clause_speech'):
            with self.assertRaises(contract.ContractError):
                contract.validate_settings({key: 'yes'})
        for key, bad in (('continuation_seconds', 16), ('continuation_seconds', -1), ('recent_remarks_seconds', 61), ('recent_remarks_seconds', True)):
            with self.assertRaises(contract.ContractError):
                contract.validate_settings({key: bad})
        defaults = contract.validate_settings({})
        self.assertEqual((defaults['speculative_reply'], defaults['main_listen_decision'], defaults['first_clause_speech'],
                          defaults['continuation_seconds'], defaults['recent_remarks_seconds']), (False, False, False, 7, 0))


class SpeculationUnit(unittest.IsolatedAsyncioTestCase):
    async def test_replay_yields_what_was_held_then_what_arrives(self):
        module = importlib.import_module('plugins._convo.helpers.convo_speculation')
        gate = asyncio.Event()
        async def answer(request):
            yield {'type': 'text.delta', 'text': 'one '}
            await gate.wait()
            yield {'type': 'text.delta', 'text': 'two'}
            yield {'type': 'answer.done'}
        spec = module.Speculation(answer, {'system': 's', 'history': [], 'audio': 'a'})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual([i['text'] for i in spec.items], ['one '])
        seen = []
        async def consume():
            async for item in spec.stream():
                seen.append(item['type'])
        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.01)
        self.assertEqual(seen, ['text.delta'])
        gate.set()
        await consumer
        self.assertEqual(seen, ['text.delta', 'text.delta', 'answer.done'])

    async def test_mismatch_names_the_difference(self):
        module = importlib.import_module('plugins._convo.helpers.convo_speculation')
        async def answer(request):
            yield {'type': 'answer.done'}
        base = {'system': 's', 'history': ['h'], 'audio': 'a', 'transcript': None, 'allow_tools': True}
        spec = module.Speculation(answer, dict(base))
        await asyncio.sleep(0.01)
        actual = dict(base)
        self.assertEqual(spec.mismatch(actual, allow_tools=True), '')
        self.assertEqual(spec.mismatch({**actual, 'system': 'other'}, allow_tools=True), 'system')
        self.assertEqual(spec.mismatch({**actual, 'history': ['x']}, allow_tools=True), 'history')
        self.assertEqual(spec.mismatch({**actual, 'audio': 'b'}, allow_tools=True), 'input')
        self.assertEqual(spec.mismatch({**actual, 'context_audio': ['x']}, allow_tools=True), 'input')
        spec.items.append({'type': 'tool.proposal', 'name': 'delegate_task', 'arguments': {}})
        self.assertEqual(spec.mismatch(actual, allow_tools=False), 'tools')
        self.assertEqual(spec.mismatch(actual, allow_tools=True), '')

    async def test_a_failed_generation_is_never_usable(self):
        module = importlib.import_module('plugins._convo.helpers.convo_speculation')
        async def answer(request):
            yield {'type': 'text.delta', 'text': 'partial'}
            raise RuntimeError('provider down')
        spec = module.Speculation(answer, {'system': 's', 'history': [], 'audio': 'a'})
        await asyncio.sleep(0.01)
        self.assertEqual(spec.status, 'failed')
        self.assertEqual(spec.mismatch({'system': 's', 'history': ['x'], 'audio': 'a'}, allow_tools=False), 'error')

    async def test_decide_is_true_only_once(self):
        module = importlib.import_module('plugins._convo.helpers.convo_speculation')
        async def answer(request):
            yield {'type': 'answer.done'}
        spec = module.Speculation(answer, {'system': 's', 'history': [], 'audio': 'a'})
        self.assertEqual([spec.decide(), spec.decide()], [True, False])


if __name__ == '__main__':
    unittest.main()
