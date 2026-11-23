"""No turn limit, compaction you never notice (helpers/convo_memory.py, convo_store chapters, convo_host.Compactor)."""
import asyncio
import importlib
import json
import tempfile
import unittest
from pathlib import Path

memory = importlib.import_module('plugins._convo.helpers.convo_memory')
Store = importlib.import_module('plugins._convo.helpers.convo_store').Store
Compactor = importlib.import_module('plugins._convo.helpers.convo_host').Compactor

SETTINGS = {'context_tokens': 24000, 'output_reserve': 1024, 'recent_exchanges': 6, 'trigger': 0.7, 'target': 0.5}
LINES = {
    'en': ('Tell me about the weather in city number {n}, and remember my code is {n}.', 'It is mild and sunny in city {n}, sir; noted.'),
    'ru': ('Расскажи мне о погоде в городе номер {n} и запомни мой код {n}.', 'В городе {n} тепло и солнечно, сэр; запомнил.'),
    'long': ('Here is a long thought number {n}: ' + 'we talked about plans, numbers, places and people again. ' * 25,
             'Understood, sir, about thought {n}: ' + 'the plans, numbers, places and people are all noted carefully. ' * 25),
    'hi': ('शहर नंबर {n} में मौसम के बारे में बताओ और मेरा कोड {n} याद रखो।', 'शहर {n} में मौसम सुहाना है, सर; याद रखा।'),
}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'history.db')
        self.s = self.store.start('browser', 'chat-a')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, n, lang='en', settle=True):
        user, reply = LINES[lang]
        sid, epoch = self.s['id'], self.s['epoch']
        self.store.accept_turn(sid, 'browser', epoch, str(n), {'transcript': user.format(n=n)})
        self.store.delivered(sid, 'browser', epoch, str(n), 'p', reply.format(n=n))
        if settle:
            self.store.settle(sid, 'browser', epoch, str(n), reply.format(n=n))


class Planning(unittest.TestCase):
    def test_utf8_sizes_not_escapes(self):
        self.assertLess(memory.tokens('Привет, как дела?'), memory.tokens(json.dumps('Привет, как дела?')))

    def test_chunk_only_past_the_band_and_only_settled_old_turns(self):
        budget = memory.Budget(SETTINGS)
        small = [{'settled': 1, 'content': {'transcript': 'hi', 'assistant': 'hello'}} for _ in range(10)]
        self.assertEqual(memory.chunk(small, budget), 0)
        big = [{'settled': 1, 'content': {'transcript': 'x' * 900, 'assistant': 'y' * 900}} for _ in range(30)]
        count = memory.chunk(big, budget)
        self.assertGreaterEqual(count, memory.CHUNK_MIN)
        self.assertLessEqual(count, 30 - budget.recent)
        big[2]['settled'] = 0
        self.assertEqual(memory.chunk(big, budget), 0)          # fewer than CHUNK_MIN settled at the front: wait

    def test_fit_leaves_out_the_oldest_and_never_raises(self):
        messages = [{'transcript': 'x' * 3000} for _ in range(10)]
        used = memory.tokens(messages)
        dropped = memory.fit(messages, used, used // 2)
        self.assertGreater(dropped, 0)
        self.assertGreaterEqual(len(messages), 1)
        self.assertLessEqual(memory.tokens(messages), used // 2 + memory.tokens({'transcript': 'x' * 3000}))



class Calibration(unittest.TestCase):
    def test_server_counts_are_read_from_the_stream(self):
        usage = importlib.import_module('plugins._convo.sidecar.app').token_usage
        self.assertEqual(usage({'usage': {'prompt_tokens': 900, 'prompt_tokens_details': {'cached_tokens': 700}}}),
                         {'prompt_tokens': 900, 'cached_tokens': 700})
        self.assertEqual(usage({'timings': {'prompt_n': 120, 'cache_n': 4000}}), {'prompt_tokens': 4120, 'cached_tokens': 4000})
        self.assertEqual(usage({'choices': []}), {})
        self.assertEqual(usage({'usage': {'prompt_tokens': True}}), {})

    def test_the_ratio_follows_the_language_and_still_errs_large(self):
        russian = 'Расскажи мне о погоде в городе и запомни мой код. ' * 40
        size = len(russian.encode('utf-8'))
        ratio = memory.calibrated(None, russian, size // 2)          # the server counted about two bytes a token
        self.assertLess(ratio, 2.0)
        self.assertGreaterEqual(memory.tokens(russian, ratio), size // 2)
        self.assertEqual(memory.calibrated(ratio, 'short', 10), ratio)   # too small to measure: unchanged
        self.assertLessEqual(memory.calibrated(None, 'x' * 100000, 70), 5.0)

class Chapters(Fixture):
    def test_splice_appends_chapters_and_context_reads_only_later_turns(self):
        for n in range(20):
            self.add(n)
        snap = self.store.snapshot(self.s['id'], 'browser', 6, 8)
        self.assertTrue(self.store.splice(snap, 'Chapter one.'))
        snap = self.store.snapshot(self.s['id'], 'browser', 6, 4)
        self.assertTrue(self.store.splice(snap, 'Chapter two.'))
        chapters = self.store.chapters('chat-a')
        self.assertEqual([c['summary'] for c in chapters], ['Chapter one.', 'Chapter two.'])
        mem = self.store.memory('chat-a')
        self.assertEqual(mem['summary'], 'Chapter one.\n\nChapter two.')
        self.assertEqual(len(mem['covered']), 12)
        self.assertEqual([t['id'] for t in self.store.context('chat-a')['turns']], [str(n) for n in range(12, 20)])

    def test_a_late_transcript_marks_only_its_chapter_and_keeps_the_summary(self):
        for n in range(20):
            self.add(n)
        self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 5), 'First.')
        self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 5), 'Second.')
        self.store.transcript(self.s['id'], 'browser', '7', 'A corrected sentence.')
        chapters = self.store.chapters('chat-a')
        self.assertEqual([c['stale'] for c in chapters], [0, 1])
        self.assertEqual(self.store.memory('chat-a')['summary'], 'First.\n\nSecond.')     # nothing deleted
        self.assertEqual(len(self.store.context('chat-a')['turns']), 10)                  # the context does not grow back
        self.assertTrue(self.store.replace_chapter('chat-a', chapters[1]['seq'], 'Second.', 'Second, corrected.'))
        self.assertEqual(self.store.memory('chat-a')['summary'], 'First.\n\nSecond, corrected.')
        self.assertFalse(self.store.replace_chapter('chat-a', chapters[1]['seq'], 'Second.', 'stale write'))

    def test_condense_replaces_the_oldest_chapters_with_one(self):
        for n in range(30):
            self.add(n)
        for text in ('A.', 'B.', 'C.'):
            self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 4), text)
        oldest = self.store.chapters('chat-a')[:2]
        self.assertTrue(self.store.condense('chat-a', [c['seq'] for c in oldest], 'A.\n\nB.', 'AB.'))
        self.assertEqual([c['summary'] for c in self.store.chapters('chat-a')], ['AB.', 'C.'])
        self.assertEqual(len(self.store.memory('chat-a')['covered']), 12)

    def test_rollback_an_older_convo_writing_the_row_wins_and_a_missing_row_is_rebuilt(self):
        for n in range(20):
            self.add(n)
        self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 5), 'Mine.')
        # An older Convo (no chapters) replaces the summary row, as its splice does.
        covered = self.store.memory('chat-a')['covered'] + ['5', '6']
        with self.store.transaction():
            self.store.db.execute('INSERT OR REPLACE INTO memories VALUES(?,?,?,?)', ('chat-a', 'Old build.', json.dumps(covered), 9))
        self.assertEqual(self.store.chapters('chat-a'), [])                  # chapters no longer describe the row
        self.assertEqual(self.store.context('chat-a')['memory']['summary'], 'Old build.')
        self.assertTrue(self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 3), 'Next.'))
        self.assertEqual([c['summary'] for c in self.store.chapters('chat-a')], ['Old build.', 'Next.'])
        # The row deleted (an older build's correction): rebuilt from the chapters.
        with self.store.transaction():
            self.store.db.execute('DELETE FROM memories WHERE target=?', ('chat-a',))
        self.assertEqual(self.store.memory('chat-a')['summary'], '')
        self.assertEqual(len(self.store.chapters('chat-a')), 2)
        self.assertEqual(self.store.memory('chat-a')['summary'], 'Old build.\n\nNext.')




class Standing(Fixture):
    def test_standing_notes_are_written_with_the_chapter_and_survive_summarising(self):
        for n in range(40):
            self.add(n, 'long')
        compactor = Compactor(self.store, None)

        async def summarize(source):
            if source['instruction'] == memory.STANDING:
                return 'Name: Dj. Answer in Spanish from now on.'
            return 'Chapter.'
        self.assertTrue(asyncio.run(compactor.run(self.s['id'], 'browser', SETTINGS, summarize=summarize)))
        self.assertEqual(self.store.context('chat-a')['memory']['standing'], 'Name: Dj. Answer in Spanish from now on.')

    def test_a_failing_notes_request_keeps_the_old_notes_and_still_writes_the_chapter(self):
        for n in range(40):
            self.add(n, 'long')
        self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 4), 'First.', 'Name: Dj.')
        compactor = Compactor(self.store, None)

        async def summarize(source):
            if source['instruction'] == memory.STANDING:
                self.assertEqual(source['previous_notes'], 'Name: Dj.')
                raise ConnectionError('down')
            return 'Second.'
        self.assertTrue(asyncio.run(compactor.run(self.s['id'], 'browser', SETTINGS, summarize=summarize)))
        self.assertEqual(self.store.standing('chat-a'), 'Name: Dj.')
        self.assertEqual(len(self.store.chapters('chat-a')), 2)

    def test_none_clears_and_long_notes_are_bounded(self):
        for n in range(20):
            self.add(n)
        self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 4), 'A.', 'Name: Dj.')
        self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 4), 'B.', 'NONE')
        self.assertEqual(self.store.standing('chat-a'), '')
        self.assertLessEqual(memory.tokens(memory.bounded('word ' * 2000, memory.STANDING_TOKENS)), memory.STANDING_TOKENS)


class Recall(Fixture):
    """recall_conversation: exact words from the summarised part, this conversation only."""

    def setUp(self):
        super().setUp()
        for n in range(20):
            self.add(n, 'ru' if n == 3 else 'en')
        self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 8), 'Earlier talk about cities.')

    def test_finds_the_exact_words_of_a_summarised_turn(self):
        found = self.store.recall('chat-a', 'what was code 5?')
        self.assertIn('city number 5', found)
        self.assertIn('User:', found)
        self.assertNotIn('city number 15', found)            # not summarised: already in view word for word

    def test_other_languages_and_other_conversations(self):
        self.assertIn('городе номер 3', self.store.recall('chat-a', 'погоде'))
        self.assertEqual(self.store.recall('chat-b', 'weather'), '')
        self.assertEqual(self.store.recall('chat-a', ''), '')

    def test_a_corrected_turn_is_read_again(self):
        self.store.recall('chat-a', 'weather')
        self.store.transcript(self.s['id'], 'browser', '2', 'Remember the zebra password.')
        self.assertIn('zebra', self.store.recall('chat-a', 'zebra'))

    def test_offered_only_after_summarising(self):
        tools = importlib.import_module('plugins._convo.helpers.convo_tools')
        self.assertIn('recall_conversation', tools.READ_ONLY)
        self.assertEqual(tools.KNOWN['recall_conversation'], 'query')

class Record(Fixture):
    """The model's own words are the record; Whisper's text is kept beside them (change 5)."""

    def turn(self, n='w'):
        self.store.accept_turn(self.s['id'], 'browser', self.s['epoch'], n, {'input': 'audio'})
        return n

    def content(self, n):
        return next(t['content'] for t in self.store.context('chat-a')['turns'] if t['id'] == n)

    def test_the_model_line_wins_over_whisper_and_whisper_is_kept(self):
        n = self.turn()
        self.store.transcript(self.s['id'], 'browser', n, 'what is the capital of japan', source='whisper')
        self.store.transcript(self.s['id'], 'browser', n, 'What is the capital of Japan?', source='model')
        c = self.content(n)
        self.assertEqual((c['transcript'], c['transcript_source'], c['whisper']), ('What is the capital of Japan?', 'model', 'what is the capital of japan'))
        self.assertNotIn('heard_differently', c)
        # Whisper arriving after the model's line does not replace it either.
        self.store.transcript(self.s['id'], 'browser', n, 'what is the capital of japan?', source='whisper')
        self.assertEqual(self.content(n)['transcript'], 'What is the capital of Japan?')

    def test_whisper_fills_a_turn_the_model_reported_nothing_for(self):
        n = self.turn()
        self.store.transcript(self.s['id'], 'browser', n, 'tell me a joke', source='whisper')
        self.assertEqual((self.content(n)['transcript'], self.content(n)['transcript_source']), ('tell me a joke', 'whisper'))

    def test_strong_disagreement_uses_whisper_and_flags_the_turn(self):
        for line, whisper in (('Hi.', 'So what I was thinking is that we could meet on Tuesday instead'),
                              ('Привет, как дела?', 'Hello, how are you doing?')):
            with self.subTest(line=line):
                n = self.turn(line)
                self.store.transcript(self.s['id'], 'browser', n, line, source='model')
                self.store.transcript(self.s['id'], 'browser', n, whisper, source='whisper')
                c = self.content(n)
                self.assertEqual((c['transcript'], c['transcript_source'], c['model_line']), (whisper, 'whisper', line))
                self.assertTrue(c['heard_differently'])

    def test_disagreement_rule(self):
        disagree = importlib.import_module('plugins._convo.helpers.convo_store').disagree
        self.assertFalse(disagree('¿Qué hora es?', 'que hora es'))
        self.assertTrue(disagree('Yes.', 'yes and also please remind me tomorrow'))
        self.assertTrue(disagree('東京は何時ですか', 'what time is it in tokyo'))
        self.assertFalse(disagree('Tokyo, iPhone и всё', 'Tokyo, iPhone и всё'))

class Soak(Fixture):
    def run_conversation(self, lang, summarize, exchanges=300):
        compactor = Compactor(self.store, None)
        budget = memory.Budget(SETTINGS)
        peak = 0
        for n in range(exchanges):
            self.add(n, lang)
            asyncio.run(compactor.run(self.s['id'], 'browser', SETTINGS, summarize=summarize))
            context = self.store.context('chat-a')
            peak = max(peak, memory.tokens(memory.verbatim(context['turns'])))
        return budget, peak

    def assert_every_turn_once(self, exchanges=300):
        covered = self.store.memory('chat-a')['covered']
        verbatim = [t['id'] for t in self.store.context('chat-a')['turns']]
        self.assertEqual(sorted(covered + verbatim, key=int), [str(n) for n in range(exchanges)])

    def test_300_exchanges_in_three_languages_stay_within_the_window(self):
        async def summarize(source):
            return 'Summary of %d turns.' % len(source.get('turns') or source.get('summaries') or [])
        for lang in ('en', 'ru', 'hi', 'long'):
            with self.subTest(lang=lang):
                self.tearDown(); self.setUp()
                budget, peak = self.run_conversation(lang, summarize)
                self.assertLessEqual(peak, budget.verbatim_high + memory.tokens(memory.verbatim(self.store.context('chat-a')['turns'][-1:])) * 2)
                self.assert_every_turn_once()
                self.assertLessEqual(memory.tokens(self.store.memory('chat-a')['summary']), budget.chapters_max + budget.condensed * 4)

    def test_a_failing_summariser_loses_nothing(self):
        async def summarize(source):
            raise ConnectionError('summariser unreachable')
        budget, peak = self.run_conversation('long', summarize, 60)
        self.assertEqual(self.store.memory('chat-a')['summary'], '')
        self.assert_every_turn_once(60)
        self.assertGreater(peak, budget.verbatim_high)          # history grows; the request itself is trimmed by fit()

    def test_speech_during_a_summary_leaves_no_partial_chapter_and_the_next_quiet_moment_writes_it(self):
        for n in range(40):
            self.add(n, 'long')
        compactor = Compactor(self.store, None)

        async def slow(source):
            await asyncio.sleep(10)
            return 'never'

        async def stopped():
            task = asyncio.create_task(compactor.run(self.s['id'], 'browser', SETTINGS, summarize=slow))
            await asyncio.sleep(0.01)
            task.cancel()                                       # the user started speaking
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(stopped())
        self.assertEqual(self.store.chapters('chat-a'), [])

        async def quick(source):
            return 'Written later.'
        self.assertTrue(asyncio.run(compactor.run(self.s['id'], 'browser', SETTINGS, summarize=quick)))
        self.assertEqual(self.store.chapters('chat-a')[0]['summary'], 'Written later.')

    def test_one_worker_per_conversation(self):
        for n in range(40):
            self.add(n, 'long')
        compactor = Compactor(self.store, None)
        calls = []

        async def summarize(source):
            calls.append(1)
            await asyncio.sleep(0.05)
            return 'One.'

        async def both():
            return await asyncio.gather(*(compactor.run(self.s['id'], 'browser', SETTINGS, summarize=summarize) for _ in range(3)))
        results = asyncio.run(both())
        self.assertEqual(sum(1 for r in results if r), 1)


if __name__ == '__main__':
    unittest.main()
