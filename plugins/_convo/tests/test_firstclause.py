"""BP8: first audible sooner. First-clause rule plus an ordered, bounded speech queue. Virtual clock, scripted providers.

The numbers recorded here come from the behaviour harness' virtual clock with scripted latencies: they show what the rule changes
when classifier, model and synthesis take the times written in each test. They are not measurements of any real model or voice."""
import asyncio
import importlib
import unittest

if __package__:
    from .behavior_harness import DELEGATE, SPEAK, Harness, ScriptedSidecar, record, script
    from .test_behavior_corpus import DELEGATE_TASK, Scenario
else:
    from behavior_harness import DELEGATE, SPEAK, Harness, ScriptedSidecar, record, script
    from test_behavior_corpus import DELEGATE_TASK, Scenario

module = importlib.import_module('plugins._convo.helpers.convo_firstclause')
ON = {'first_clause_speech': True}
OFF = {'first_clause_speech': False}            # the whole reply once it has ended
# A model that streams a long first sentence in small pieces, then a second sentence.
STREAM = [('Sure, ', .1), ('here is the first part ', .1), ('of the answer you asked for, ', .1), ('and then the rest of this sentence follows on. ', .1),
          ('The second sentence closes the reply and is long enough to stand alone as a phrase.', .1)]


class ShortFirstSentence(unittest.TestCase):
    def split(self, *chunks):
        return Rule.split(self, *chunks, min_words=module.FIRST_WORDS)

    def test_a_short_first_sentence_waits_and_is_spoken_with_the_next(self):
        released, rest = self.split('Good evening, sir. ', 'Frankfort is the capital of Kentucky. ', 'More follows')
        self.assertEqual(released, ['Good evening, sir. Frankfort is the capital of Kentucky.'])
        self.assertEqual(rest, ' More follows')

    def test_a_long_enough_first_sentence_still_goes_alone_and_first(self):
        released, _ = self.split('The capital of Kentucky is Frankfort, sir. ', 'Anything else?')
        self.assertEqual(released[0], 'The capital of Kentucky is Frankfort, sir.')          # alone, first

    def test_a_reply_that_is_only_a_short_sentence_is_left_for_the_end_of_the_reply(self):
        self.assertEqual(self.split('Yes, sir.'), ([], 'Yes, sir.'))


class Rule(unittest.TestCase):
    def split(self, *chunks, min_words=1):
        """Feed chunks the way the turn does; return the phrases released. Sentence-boundary cases use a one-word minimum; the
        product's minimum (FIRST_WORDS) has its own test below."""
        clause, buffer, released = module.FirstClause(min_words=min_words), '', []
        for chunk in chunks:
            buffer += chunk
            taken = clause.take(buffer)
            while taken:
                phrase, buffer = taken
                released.append(phrase)
                taken = clause.take(buffer)
        return released, buffer

    def test_the_first_phrase_is_the_first_full_sentence(self):
        for text, first in (('Sure, here is more. Then', 'Sure, here is more.'), ('Hello. Then more', 'Hello.'), ('Really? Yes', 'Really?'),
                            ('Well; ok. And', 'Well; ok.'), ('Note: this works! More', 'Note: this works!'), ('He said "Yes." Then', 'He said "Yes."')):
            with self.subTest(text):
                self.assertEqual(self.split(text)[0][0], first)

    def test_a_comma_or_a_long_clause_is_never_a_cut(self):
        """Found live (chat 906): "Could you please clarify what you would", a pause, then the rest. A sentence is waited for."""
        released, rest = self.split('Could you please clarify ', 'what you would ', 'like me to search for')
        self.assertEqual((released, rest), ([], 'Could you please clarify what you would like me to search for'))
        released, _ = self.split('Could you please clarify ', 'what you would ', 'like me to search for? Then')
        self.assertEqual(released, ['Could you please clarify what you would like me to search for?'])

    def test_three_characters_is_enough_two_is_not(self):
        self.assertEqual(self.split('Ok.')[0], ['Ok.'])
        self.assertEqual(self.split('A.')[0], [])
        self.assertEqual(self.split('A. then')[0], [])                            # nothing yet: the stop is the second character

    def test_only_a_first_sentence_longer_than_a_request_is_cut_and_never_inside_a_word(self):
        text = 'the quick brown fox jumps over the lazy dog and keeps running ' * 8      # 504 characters, no sentence end
        released, rest = self.split(text)
        self.assertEqual(len(released), 1)
        self.assertTrue(module.MERGE_LIMIT // 2 <= len(released[0]) <= module.MERGE_LIMIT)
        self.assertEqual(released[0] + rest, text)
        self.assertEqual(rest[:1], ' ')
        for text in ('a' * 300 + ' ' + 'b' * 300 + ' tail words', 'x' * 500 + ' end'):
            with self.subTest(text[:20]):
                released, rest = self.split(text)
                self.assertEqual(len(released), 1)
                self.assertEqual(rest[:1], ' ')
                self.assertEqual(released[0] + rest, text)

    def test_numbers_times_and_abbreviations_are_not_sentence_ends(self):
        for text in ('It is 3.5 degrees today and more', 'At 10:30 we meet and so on', 'I paid 1,000 for it and then', 'Dr. Smith will see you'):
            with self.subTest(text):
                released, _ = self.split(text)
                self.assertTrue(all(not phrase.rstrip().endswith(('3.', '10:', '1,', 'Dr.')) for phrase in released), released)

    def test_a_number_split_across_chunks_waits_for_the_next_one(self):
        self.assertEqual(self.split('It is 3.', '5 degrees today and more')[0], [])
        self.assertEqual(self.split('I have 3.', ' Then more')[0], ['I have 3.'])

    def test_after_the_first_sentence_every_complete_sentence_is_released(self):
        released, rest = self.split('Sure, ', 'a short sentence. ', 'and another short one. ', 'And a half')
        self.assertEqual(released, ['Sure, a short sentence.', ' and another short one.'])
        self.assertEqual(rest, ' And a half')

    def test_a_very_long_run_is_released_in_pieces(self):
        released, rest = self.split('Ok.', 'y' * 900)
        self.assertEqual(([len(p) for p in released], len(rest)), ([3, module.MERGE_LIMIT], 900 - module.MERGE_LIMIT))


class Queue(unittest.IsolatedAsyncioTestCase):
    async def test_the_first_phrase_alone_then_what_waits_in_one_request(self):
        spoken, running, peak = [], 0, 0
        async def speak(text):
            nonlocal running, peak
            running += 1; peak = max(peak, running)
            await asyncio.sleep(.01)
            spoken.append(text)
            running -= 1
        queue = module.SpeechQueue(speak)
        await queue.put('First.'); await asyncio.sleep(0)            # the worker takes it at once
        for sentence in (' Second.', ' Third.', ' Fourth.'):
            await queue.put(sentence)                                # waiting while the first is synthesised
        await queue.drain()
        self.assertEqual((spoken, peak, queue.requests), (['First.', 'Second. Third. Fourth.'], 1, 2))

    async def test_what_waits_is_merged_up_to_the_limit_and_cut_after_a_sentence(self):
        spoken = []
        async def speak(text):
            await asyncio.sleep(.01)
            spoken.append(text)
        queue = module.SpeechQueue(speak)
        await queue.put('First.'); await asyncio.sleep(0)
        sentence = ' ' + 'word ' * 11 + 'end.'                         # 60 characters
        for _ in range(5):
            await queue.put(sentence)
        await queue.put(' ' + 'x' * 200 + ' tail.')
        await queue.drain()
        self.assertEqual(spoken[0], 'First.')
        self.assertTrue(all(len(text) <= module.MERGE_LIMIT and text.endswith('.') for text in spoken))
        self.assertEqual(len(spoken), 3)                               # 300 + 207 characters do not fit one request of 420
        self.assertEqual(' '.join(spoken).replace(' ', ''), ('First.' + sentence * 5 + ' ' + 'x' * 200 + ' tail.').replace(' ', ''))

    async def test_the_queue_is_bounded_and_pushes_back(self):
        gate, started = asyncio.Event(), []
        async def speak(text):
            started.append(text)
            await gate.wait()
        queue = module.SpeechQueue(speak, limit=2)
        await queue.put('a'); await asyncio.sleep(0)               # the worker takes it
        await queue.put('b'); await queue.put('c')                 # two waiting
        blocked = asyncio.create_task(queue.put('d'))
        await asyncio.sleep(.01)
        self.assertFalse(blocked.done())                           # no unbounded growth
        gate.set()
        await blocked
        await queue.drain()
        self.assertEqual(started, ['a', 'bc', 'd'])                # what was waiting went as one request

    async def test_the_first_failure_is_raised_to_the_turn_and_later_phrases_are_dropped(self):
        spoken = []
        async def speak(text):
            if 'b' in text:
                raise ValueError('Speech provider rejected the request')
            spoken.append(text)
        queue = module.SpeechQueue(speak)
        await queue.put('a'); await asyncio.sleep(0); await asyncio.sleep(0)
        await queue.put('b'); await queue.put('c')
        with self.assertRaises(ValueError):
            await queue.drain()
        self.assertEqual(spoken, ['a'])
        with self.assertRaises(ValueError):
            await queue.put('d')

    async def test_cancel_stops_the_worker(self):
        async def speak(text):
            await asyncio.Event().wait()
        queue = module.SpeechQueue(speak)
        await queue.put('a'); await asyncio.sleep(0)
        worker = queue.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        self.assertTrue(worker.cancelled())


class Behaviour(Scenario):
    def run_stream(self, config, *, tts_delay=.1, deltas=STREAM, **options):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            phrases = [p for _, p in h.audible('q')]
            texts = [p['text'] for _, p in h.events('text.delta', 'q')]
            return {'first_audible': h.first_audible('q'), 'text_done': h.events('text.delta', 'q')[-1][0], 'spoken': [t for _, t in h.spoken],
                    'phrase_ids': [p['phrase_id'] for p in phrases], 'shown': ''.join(texts), 'generated': [r['payload']['text'] for r in h.journal('assistant.generated')]}
        facts, _ = self.play(scenario, model_deltas=deltas, tts_delay=tts_delay, config=config, classifier={'question': script(SPEAK, .2)}, **options)
        return facts

    def test_first_audible_is_earlier_and_the_numbers_are_recorded(self):
        off, on = self.run_stream(OFF), self.run_stream(ON)
        record('first_clause_first_audible', off=off['first_audible'], on=on['first_audible'], classifier_s=.2, model_chunk_s=.1, tts_s=.1,
               note='virtual clock; scripted latencies; first sentence is 140 characters long')
        self.assertLess(on['first_audible'], off['first_audible'])
        self.assertAlmostEqual(on['first_audible'], .2 + 4 * .1 + .1, delta=.02)     # classifier + the whole first sentence + one synthesis
        self.assertAlmostEqual(off['first_audible'], .2 + 5 * .1 + .1, delta=.02)    # the whole reply first

    def test_the_words_are_the_same_in_the_same_order_whatever_the_split(self):
        off, on = self.run_stream(OFF), self.run_stream(ON)
        self.assertEqual(on['shown'], off['shown'])
        self.assertEqual(''.join(on['spoken']).replace(' ', ''), ''.join(off['spoken']).replace(' ', ''))
        self.assertEqual(on['generated'], off['generated'])
        self.assertGreater(len(on['spoken']), len(off['spoken']))                    # finer phrases

    def test_with_first_sentence_speech_off_a_reply_up_to_the_limit_is_one_generation(self):
        # The parity candidate "one generation per reply up to 420 characters" (change 10) is the existing setting
        # first_clause_speech: false; its default stays on until the measurement decides.
        self.assertLessEqual(len(''.join(text for text, _ in STREAM)), module.MERGE_LIMIT)
        off = self.run_stream(OFF)
        self.assertEqual(len(off['spoken']), 1)
        self.assertTrue(importlib.import_module('plugins._convo.helpers.convo_contract').DEFAULTS['first_clause_speech'])

    def test_reading_the_model_does_not_stall_while_a_phrase_is_synthesised(self):
        off, on = self.run_stream(OFF, tts_delay=1.0), self.run_stream(ON, tts_delay=1.0)
        for facts in (on, off):                                                       # the text keeps streaming during synthesis
            self.assertLessEqual(facts['text_done'], .2 + 5 * .1 + .05)
        self.assertEqual(len(on['phrase_ids']), len(set(on['phrase_ids'])))

    def test_every_phrase_is_acknowledged_and_enters_the_history_in_order(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            ids = [p['phrase_id'] for _, p in h.audible('q')]
            for phrase in ids:
                await h.playback('q', phrase, 'started')
                await h.playback('q', phrase)
            return ids, [r['payload']['text'] for r in h.journal('assistant.delivered')], [t for _, t in h.spoken]
        (ids, delivered, spoken), _ = self.play(scenario, model_deltas=STREAM, config=ON, classifier={'question': script(SPEAK, .2)})
        self.assertEqual(len(ids), len(spoken))
        self.assertEqual(delivered, spoken)                                           # one receipt per phrase, in speaking order

    def test_a_synthesis_failure_ends_the_turn_like_a_direct_call_would(self):
        async def broken(self, role, text):
            self.calls.append('speech')
            raise ValueError('Speech provider rejected the request')
        original = ScriptedSidecar.speech
        ScriptedSidecar.speech = broken
        try:
            async def scenario(h):
                await h.candidate('q', h.audio('question'), final=True)
                await h.settle()
                return len(h.events('error', 'q')), [r['payload']['text'] for r in h.journal('assistant.generated')], h.audible()
            facts, _ = self.play(scenario, model_deltas=STREAM, config=ON, classifier={'question': script(SPEAK, .2)})
        finally:
            ScriptedSidecar.speech = original
        self.assertEqual(facts, (1, [''], []))

    def test_no_speech_worker_outlives_its_turn(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return [t for t in asyncio.all_tasks() if 'SpeechQueue' in repr(t.get_coro()) and not t.done()]
        leaked, _ = self.play(scenario, model_deltas=STREAM, config=ON, classifier={'question': script(SPEAK, .2)})
        self.assertEqual(leaked, [])

    def test_a_barge_in_cancels_the_queue_and_nothing_is_heard_afterwards(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(.55)
            await h.candidate('s', h.audio('stop-probe'), sequence=1, final=False)
            await h.settle()
            revoked = h.events('candidate.admitted', 's')[0][0]
            await h.advance(10)
            return revoked, [t for t, _ in h.audible('q') if t > revoked], h.journal('assistant.generated')
        (revoked, late, generated), _ = self.play(scenario, model_deltas=STREAM, config={**ON, 'continuation_seconds': 0}, tts_delay=.5, stubborn_answer=3.0,
                                                 classifier={'question': script(SPEAK, .2), 'stop-probe': script(SPEAK, .15)})
        self.assertEqual((late, generated), ([], []))
        self.assertAlmostEqual(revoked - .55, .15, delta=.03)                         # the stop is not held by synthesis in flight

    def test_the_busy_provider_wait_still_applies_to_the_next_reply(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(1.0)
            await h.candidate('s', h.audio('stop-probe'), sequence=1, final=False)
            await h.until(1.5)
            await h.candidate('s', h.audio('stop-final'), sequence=2, final=True)
            await h.settle()
            return {'rejected': len(h.log('speech.409')), 'errors': h.events('error', 's'), 'heard': h.first_audible('s')}
        facts, _ = self.play(scenario, model_deltas=STREAM, tts_delay=3.0, tts_admission=True, config={**ON, 'continuation_seconds': 0},
                             classifier={'question': script(SPEAK, .2), 'stop-probe': script(SPEAK, .3), 'stop-final': script(SPEAK, .2)})
        self.assertGreaterEqual(facts['rejected'], 1)
        self.assertEqual(facts['errors'], [])
        self.assertIsNotNone(facts['heard'])

    def test_a_tool_turn_speaks_the_models_words_then_the_confirmation(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return [t for _, t in h.spoken], len(h.jobs())
        (spoken, jobs), _ = self.play(scenario, model_deltas=[('Okay, doing that now. ', .1)], proposal=DELEGATE_TASK, config=ON, classifier={'ask': script(DELEGATE, .2)})
        self.assertEqual((spoken, jobs), (['Okay, doing that now.', 'Queued that in the selected chat.'], 1))


if __name__ == '__main__':
    unittest.main()
