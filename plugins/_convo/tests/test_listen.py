"""The main model's own "this was not for me": exactly [LISTEN] is no reply. It can only suppress output, never grant it."""
import importlib
import unittest

if __package__:
    from .behavior_harness import SILENT, SPEAK, SPEAK_PLAIN, ScriptedSidecar, script
    from .test_behavior_corpus import Scenario
else:
    from behavior_harness import SILENT, SPEAK, SPEAK_PLAIN, ScriptedSidecar, script
    from test_behavior_corpus import Scenario

listen = importlib.import_module('plugins._convo.helpers.convo_listen')
LISTEN = {'main_listen_decision': True}
SENTENCE = 'This is an ordinary reply that is long enough to be spoken as one phrase. '


def marks(turn):
    LOG = importlib.import_module('plugins._convo.helpers.convo_metrics').LOG
    return [m['name'] for r in LOG.records(200) if r['turn'] == turn for m in r['marks'] if m['name'].startswith('model.')]


class FilterUnit(unittest.TestCase):
    def run_filter(self, *chunks):
        filtered, passed = listen.ListenFilter(), []
        for chunk in chunks:
            passed.append(filtered.feed(chunk))
        exact, held = filtered.finish()
        return exact, ''.join(passed) + held

    def test_the_token_alone_is_a_decision_however_it_is_split(self):
        for chunks in (['[LISTEN]'], ['[LIS', 'TEN]'], ['[', 'L', 'I', 'S', 'T', 'E', 'N', ']'], ['  [Listen]\n'], ['\n', '[listen', ']  ']):
            with self.subTest(chunks):
                self.assertEqual(self.run_filter(*chunks), (True, ''))

    def test_anything_else_passes_through_unchanged_and_in_order(self):
        for chunks in (['Hello there'], ['[LIST of things]'], ['[', 'LIST', ' of things]'], ['[LISTEN] and more'], ['[LISTEN]', ' more'], ['x[LISTEN]'], ['']):
            with self.subTest(chunks):
                exact, text = self.run_filter(*chunks)
                self.assertEqual((exact, text), (False, ''.join(chunks)))

    def test_an_ordinary_reply_starts_flowing_on_its_first_chunk(self):
        filtered = listen.ListenFilter()
        self.assertEqual(filtered.feed('Sure, '), 'Sure, ')
        self.assertEqual(filtered.feed('[LISTEN]'), '[LISTEN]')                  # decided already: it is just text now

    def test_only_a_confident_explicit_request_is_strong(self):
        policy = importlib.import_module('plugins._convo.helpers.convo_policy')
        make = lambda explicit, confidence: policy.Decision('speak', 'none', confidence, explicit)
        self.assertTrue(listen.strong(make(True, .9)))
        self.assertFalse(listen.strong(make(True, .89)))
        self.assertFalse(listen.strong(make(False, .99)))


class Decision(Scenario):
    def setUp(self):
        importlib.import_module('plugins._convo.helpers.convo_metrics').LOG.reset()

    def reply(self, deltas, *, decision=SPEAK_PLAIN, config=LISTEN, **options):
        importlib.import_module('plugins._convo.helpers.convo_metrics').LOG.reset()
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return {'text': ''.join(p.get('text', '') for _, p in h.events('text.delta', 'q')), 'spoken': [t for _, t in h.spoken], 'errors': h.events('error', 'q'),
                    'journal': [(r['kind'], r['payload'].get('text')) for r in h.journal() if r['kind'].startswith('assistant')], 'marks': marks('q'),
                    'systems': [a['system'] for a in h.answers], 'accepted': h.accepted_turns(), 'delivered': h.journal('assistant.delivered')}
        facts, _ = self.play(scenario, model_deltas=deltas, config=config, classifier={'question': script(decision, .2)}, **options)
        return facts

    def test_the_model_is_told_it_may_stay_quiet_only_when_the_setting_is_on(self):
        on, off = self.reply([(SENTENCE, .1)]), self.reply([(SENTENCE, .1)], config={})
        self.assertIn('[LISTEN]', on['systems'][0])
        self.assertNotIn('LISTEN', off['systems'][0])

    def test_exactly_the_token_is_no_reply_nothing_is_shown_spoken_or_remembered(self):
        for deltas in ([('[LISTEN]', .1)], [('[LIS', .1), ('TEN]', .1)], [(' [Listen]\n', .1)]):
            with self.subTest(deltas):
                facts = self.reply(deltas)
                self.assertEqual((facts['text'], facts['spoken'], facts['errors'], facts['delivered']), ('', [], [], []))
                self.assertEqual(facts['journal'], [('assistant.generated', '')])        # settled empty: no assistant words in the history
                self.assertEqual(facts['marks'], ['model.listen'])

    def test_a_reply_that_merely_contains_or_starts_like_the_token_is_spoken(self):
        for deltas, expected in (([('Sure. [LISTEN] ' + SENTENCE, .1)], 'Sure. [LISTEN] ' + SENTENCE), ([('[LIST', .1), (' of three items follows here and it keeps going for a while.', .1)],
                                                                                                    '[LIST of three items follows here and it keeps going for a while.')):
            with self.subTest(expected[:12]):
                facts = self.reply(deltas)
                self.assertEqual(facts['text'], expected)
                self.assertEqual(facts['marks'], [])
                self.assertTrue(facts['spoken'])

    def test_the_token_means_nothing_when_the_setting_is_off(self):
        facts = self.reply([('[LISTEN]', .1)], config={})
        self.assertEqual(facts['text'], '[LISTEN]')                                        # today's behaviour, untouched
        self.assertEqual(facts['marks'], [])

    def test_a_classifier_that_ignored_the_audio_stays_silent_whatever_the_model_would_say(self):
        async def scenario(h):
            await h.candidate('tv', h.audio('television'), final=True)
            await h.settle()
            return len(h.answers), h.audible(), h.accepted_turns()
        facts, _ = self.play(scenario, model_deltas=[(SENTENCE, .1)], config=LISTEN, classifier={'television': script(SILENT, .2)})
        self.assertEqual(facts, (0, [], []))                                                # the model is never even asked

    def test_a_confident_explicit_request_is_never_silenced_it_is_asked_again_without_the_option(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return {'systems': ['LISTEN' in a['system'] for a in h.answers], 'spoken': [t for _, t in h.spoken], 'marks': marks('q'), 'errors': h.events('error', 'q')}
        # the scripted model says [LISTEN] to the first request and a real answer to the second
        replies = iter([[('[LISTEN]', .1)], [(SENTENCE, .1)]])
        original = ScriptedSidecar.answer
        async def answer(self, payload):
            self.h.model_deltas = next(replies)
            async for item in original(self, payload):
                yield item
        ScriptedSidecar.answer = answer
        try:
            facts, _ = self.play(scenario, config=LISTEN, classifier={'question': script(SPEAK, .2)})
        finally:
            ScriptedSidecar.answer = original
        self.assertEqual(facts['systems'], [True, False])
        self.assertEqual(facts['spoken'], [SENTENCE])                                       # the second answer, once
        self.assertEqual(facts['marks'], ['model.listen_overridden'])
        self.assertEqual(facts['errors'], [])

    def test_the_reply_guard_still_applies_after_the_filter(self):
        markup = '<tool_call><function=web_search><parameter=query>x</parameter></function></tool_call>'
        facts = self.reply([('Sure, I will look that up for you right now. ', .1), (markup, .1)])
        self.assertEqual(facts['text'].strip(), 'Sure, I will look that up for you right now.')
        self.assertEqual(len(facts['errors']), 1)

    def test_with_a_held_parallel_reply_the_token_still_only_suppresses(self):
        facts = self.reply([('[LISTEN]', .1)], config={**LISTEN, 'speculative_reply': True})
        self.assertEqual((facts['text'], facts['spoken'], facts['marks']), ('', [], ['model.listen']))


if __name__ == '__main__':
    unittest.main()
