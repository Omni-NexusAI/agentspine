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
# A model that streams a long first sentence in small pieces, then a second sentence.
STREAM = [('Sure, ', .1), ('here is the first part ', .1), ('of the answer you asked for, ', .1), ('and then the rest of this sentence follows on. ', .1),
          ('The second sentence closes the reply and is long enough to stand alone as a phrase.', .1)]


class Rule(unittest.TestCase):
    def split(self, *chunks):
        """Feed chunks the way the turn does; return the phrases released."""
        clause, buffer, released = module.FirstClause(), '', []
        for chunk in chunks:
            buffer += chunk
            taken = clause.take(buffer)
            while taken:
                phrase, buffer = taken
                released.append(phrase)
                taken = clause.take(buffer)
        return released, buffer

    def test_the_first_clause_is_released_at_its_first_punctuation(self):
        for text, first in (('Sure, here is more', 'Sure,'), ('Hello. Then more', 'Hello.'), ('Really? Yes', 'Really?'), ('Well; ok', 'Well;'), ('Note: this', 'Note:')):
            with self.subTest(text):
                self.assertEqual(self.split(text)[0][0], first)

    def test_three_characters_is_enough_two_is_not(self):
        self.assertEqual(self.split('Ok,')[0], ['Ok,'])
        self.assertEqual(self.split('A,')[0], [])
        self.assertEqual(self.split('A, then')[0], ['A, then'][:0])           # nothing yet: the comma is the second character

    def test_without_a_clause_it_is_cut_at_a_word_boundary_near_forty_characters(self):
        released, rest = self.split('the quick brown fox jumps over the lazy dog and keeps running')
        self.assertEqual(len(released), 1)
        self.assertTrue(30 <= len(released[0]) <= 45)
        self.assertEqual(released[0] + rest, 'the quick brown fox jumps over the lazy dog and keeps running')
        self.assertEqual(rest[:1], ' ')                                                    # cut between words, never inside one

    def test_the_cut_never_falls_inside_a_word(self):
        for text in ('abcdefghij ' * 8, 'a' * 30 + ' ' + 'b' * 30 + ' tail words', 'x' * 70 + ' end'):
            with self.subTest(text[:20]):
                released, rest = self.split(text)
                self.assertEqual(len(released), 1)
                self.assertEqual(rest[:1], ' ')
                self.assertEqual(released[0] + rest, text)

    def test_numbers_times_and_abbreviations_are_not_clause_ends(self):
        for text in ('It is 3.5 degrees today and more', 'At 10:30 we meet and so on', 'I paid 1,000 for it and then', 'Dr. Smith will see you'):
            with self.subTest(text):
                released, _ = self.split(text)
                self.assertTrue(all(not phrase.rstrip().endswith(('3.', '10:', '1,', 'Dr.')) for phrase in released), released)

    def test_a_number_split_across_chunks_waits_for_the_next_one(self):
        self.assertEqual(self.split('It is 3.', '5 degrees today and more')[0], [])
        self.assertEqual(self.split('I have 3.', ' Then more')[0], ['I have 3.'])

    def test_later_phrases_keep_todays_rule(self):
        released, rest = self.split('Sure, ', 'a short sentence. ', 'and another short one. ')
        self.assertEqual(released, ['Sure,'])                                         # the first clause only; nothing else is long enough
        self.assertEqual(rest, ' a short sentence. and another short one. ')
        long_sentence = 'x' * 60 + '.'
        self.assertEqual(self.split('Ok, ', long_sentence)[0], ['Ok,', ' ' + long_sentence])

    def test_a_very_long_run_is_released_in_pieces(self):
        released, rest = self.split('Ok,', 'y' * 900)
        self.assertEqual([len(p) for p in released], [3, 900])


class Queue(unittest.IsolatedAsyncioTestCase):
    async def test_phrases_are_spoken_one_at_a_time_in_order(self):
        spoken, running, peak = [], 0, 0
        async def speak(text):
            nonlocal running, peak
            running += 1; peak = max(peak, running)
            await asyncio.sleep(.01)
            spoken.append(text)
            running -= 1
        queue = module.SpeechQueue(speak)
        for word in 'abcd':
            await queue.put(word)
        await queue.drain()
        self.assertEqual((spoken, peak), (list('abcd'), 1))

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
        self.assertEqual(started, list('abcd'))

    async def test_the_first_failure_is_raised_to_the_turn_and_later_phrases_are_dropped(self):
        spoken = []
        async def speak(text):
            if text == 'b':
                raise ValueError('Speech provider rejected the request')
            spoken.append(text)
        queue = module.SpeechQueue(speak)
        await queue.put('a'); await queue.put('b'); await queue.put('c')
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
        off, on = self.run_stream({}), self.run_stream(ON)
        record('first_clause_first_audible', off=off['first_audible'], on=on['first_audible'], classifier_s=.2, model_chunk_s=.1, tts_s=.1,
               note='virtual clock; scripted latencies; first sentence is 140 characters long')
        self.assertLess(on['first_audible'], off['first_audible'])
        self.assertAlmostEqual(on['first_audible'], .2 + .1 + .1, delta=.02)         # classifier + first chunk ("Sure,") + one synthesis
        self.assertGreater(off['first_audible'], .2 + 4 * .1)                         # today: after the whole first sentence has arrived

    def test_the_words_are_the_same_in_the_same_order_whatever_the_split(self):
        off, on = self.run_stream({}), self.run_stream(ON)
        self.assertEqual(on['shown'], off['shown'])
        self.assertEqual(''.join(on['spoken']).replace(' ', ''), ''.join(off['spoken']).replace(' ', ''))
        self.assertEqual(on['generated'], off['generated'])
        self.assertGreater(len(on['spoken']), len(off['spoken']))                    # finer phrases

    def test_reading_the_model_does_not_stall_while_a_phrase_is_synthesised(self):
        off, on = self.run_stream({}, tts_delay=1.0), self.run_stream(ON, tts_delay=1.0)
        self.assertLess(on['text_done'], off['text_done'])                           # the text kept streaming during synthesis
        self.assertLessEqual(on['text_done'], .2 + 5 * .1 + .05)
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

    def test_a_tool_turn_is_not_split(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return [t for _, t in h.spoken], len(h.jobs())
        (spoken, jobs), _ = self.play(scenario, model_deltas=[('Okay, doing that now. ', .1)], proposal=DELEGATE_TASK, config=ON, classifier={'ask': script(DELEGATE, .2)})
        self.assertEqual((spoken, jobs), (['Queued that in the selected chat.'], 1))


if __name__ == '__main__':
    unittest.main()
