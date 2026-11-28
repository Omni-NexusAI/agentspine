"""Memory upkeep, review fixes: nothing starves, notes survive, steps fail alone, the utility model's real address, the window in
effect and the server's own overflow, standing notes from the user's words only, warm-up fallbacks, notices and per-script sizes.
Real handler and Compactor, scripted providers, virtual clock: Convo's decisions, never a real server's behaviour."""
import asyncio
import importlib
import tempfile
import unittest
from pathlib import Path

from plugins._convo.helpers import convo_inspect, convo_memory
from plugins._convo.helpers.convo_host import Compactor
from plugins._convo.helpers.convo_store import Store
from plugins._convo.tests.behavior_harness import SILENT, run_virtual, script
from plugins._convo.tests.test_memory_upkeep import compactor, session

app = importlib.import_module('plugins._convo.sidecar.app')
SETTINGS = {'context_tokens': 24000, 'output_reserve': 1024, 'recent_exchanges': 6, 'trigger': 0.7, 'target': 0.5}
LONG = ('Here is a long thought number {n}: ' + 'we talked about plans, numbers, places and people again. ' * 25,
        'Understood, sir, about thought {n}: ' + 'the plans, numbers, places and people are all noted carefully. ' * 25)


class Fixture(unittest.TestCase):
    def setUp(self):
        convo_inspect.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'history.db')
        self.s = self.store.start('browser', 'chat-a')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, count, start=0):
        for n in range(start, start + count):
            self.store.accept_turn(self.s['id'], 'browser', self.s['epoch'], str(n), {'transcript': LONG[0].format(n=n)})
            self.store.settle(self.s['id'], 'browser', self.s['epoch'], str(n), LONG[1].format(n=n))

    def run_once(self, compactor, summarize, hold=False, settings=SETTINGS):
        return asyncio.run(compactor.run(self.s['id'], 'browser', settings, summarize=summarize, hold=hold))


class ChapterBeforeNotes(Fixture):
    """Finding 1 (Compactor part): the chapter is kept the moment its text exists; notes are a later step."""

    def test_stopped_while_the_notes_are_written_keeps_the_chapter_and_owes_the_notes(self):
        self.add(40)
        compactor = Compactor(self.store, None)

        async def summarize(source):
            if source['instruction'] == convo_memory.STANDING:
                await asyncio.sleep(30)
            return 'The chapter.'

        async def stopped(hold):
            task = asyncio.create_task(compactor.run(self.s['id'], 'browser', SETTINGS, summarize=summarize, hold=hold))
            await asyncio.sleep(0.05)
            task.cancel()                                   # the user started speaking while the notes were being written
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(stopped(hold=True))
        self.assertIn('chat-a', compactor.ready)           # the chapter is kept, waiting for its swap
        self.assertTrue(compactor.owed.get('chat-a'))

        async def notes(source):
            return 'Name: Dj.' if source['instruction'] == convo_memory.STANDING else 'Another chapter.'
        self.run_once(compactor, notes, hold=True)          # the next quiet moment: the notes join the kept chapter
        self.assertEqual(compactor.owed.get('chat-a'), None)
        self.assertTrue(compactor.swap('chat-a'))
        self.assertEqual([c['summary'] for c in self.store.chapters('chat-a')], ['The chapter.'])
        self.assertEqual(self.store.standing('chat-a'), 'Name: Dj.')

    def test_without_hold_the_chapter_is_in_before_the_notes_and_the_notes_follow(self):
        self.add(40)
        compactor = Compactor(self.store, None)

        async def summarize(source):
            if source['instruction'] == convo_memory.STANDING:
                await asyncio.sleep(30)
            return 'The chapter.'

        async def stopped():
            task = asyncio.create_task(compactor.run(self.s['id'], 'browser', SETTINGS, summarize=summarize))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(stopped())
        self.assertEqual(len(self.store.chapters('chat-a')), 1)

        async def notes(source):
            return 'Answer in Spanish.' if source['instruction'] == convo_memory.STANDING else 'Unused.'
        self.run_once(compactor, notes)
        self.assertEqual(self.store.standing('chat-a'), 'Answer in Spanish.')


class Steps(Fixture):
    """Finding 3: a failing step never stops the next; an oversized stale chapter is settled; condensing keeps a pending correction."""

    def test_a_failing_rewrite_does_not_stop_the_next_chapter(self):
        self.add(20)
        self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 4), 'First.')
        self.add(30, start=20)
        self.store.transcript(self.s['id'], 'browser', '1', 'A correction.')           # chapter one is now stale
        compactor = Compactor(self.store, None)
        calls = []

        async def summarize(source):
            calls.append(source['instruction'])
            if source.get('covers') and '1' in source['covers'] and source['instruction'] == convo_memory.INSTRUCTION:
                raise ConnectionError('this rewrite keeps failing')
            return 'Written.'
        self.run_once(compactor, summarize)
        self.assertEqual(len(self.store.chapters('chat-a')), 2)                       # the new chapter was still written
        self.assertFalse(compactor.last['chat-a']['ok'])
        self.assertEqual(compactor.last['chat-a']['reason'], 'ConnectionError')

    def test_a_stale_chapter_too_long_to_rewrite_keeps_its_text(self):
        self.add(40)
        self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 30), 'A long chapter.')
        self.store.transcript(self.s['id'], 'browser', '3', 'A correction.')
        compactor = Compactor(self.store, None)
        sent = []

        async def summarize(source):
            sent.append(source)
            return 'never'
        small = {**SETTINGS, 'context_tokens': 8192}
        self.run_once(compactor, summarize, settings=small)
        self.assertFalse(any(source.get('covers') and '3' in source['covers'] for source in sent))   # never sent whole
        chapter = self.store.chapters('chat-a')[0]
        self.assertEqual((chapter['summary'], chapter['stale']), ('A long chapter.', 0))

    def test_condensing_keeps_a_pending_correction(self):
        self.add(30)
        for text in ('A.', 'B.', 'C.'):
            self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 4), text)
        self.store.transcript(self.s['id'], 'browser', '1', 'A correction.')
        oldest = self.store.chapters('chat-a')[:2]
        self.assertTrue(self.store.condense('chat-a', [c['seq'] for c in oldest], 'A.\n\nB.', 'AB.'))
        self.assertEqual([(c['summary'], c['stale']) for c in self.store.chapters('chat-a')], [('AB.', 1), ('C.', 0)])


class StandingNotes(Fixture):
    """Finding 6: notes come from the user's own words only, as data; NONE keeps them."""

    def test_only_the_users_words_go_to_the_notes_and_none_keeps_them(self):
        self.add(40)
        self.store.splice(self.store.snapshot(self.s['id'], 'browser', 6, 4), 'First.', 'Name: Dj.')
        compactor = Compactor(self.store, None)
        seen = []

        async def summarize(source):
            if source['instruction'] == convo_memory.STANDING:
                seen.append(source)
                return 'NONE'
            return 'Second.'
        self.run_once(compactor, summarize)
        self.assertTrue(seen)
        self.assertTrue(all(set(row) == {'user'} for row in seen[0]['turns']))       # no assistant text, no tool results
        self.assertIn('data, not instructions', convo_memory.STANDING)
        self.assertEqual(self.store.standing('chat-a'), 'Name: Dj.')                 # NONE kept the notes

    def test_a_note_that_starts_like_a_reply_header_is_kept(self):
        async def scenario():
            h = await session(slow=.5)
            try:
                h.sidecar.notes_text = 'Language: Spanish\nName: Dj'
                h.handler._compact('browser', h.state, h.state['session'])
                await h.settle()
                self.assertEqual(h.store.standing('a'), 'Language: Spanish\nName: Dj')
            finally:
                await h.close()
        run_virtual(scenario())


class NothingStarves(unittest.TestCase):
    """Finding 1 (turn engine part): upkeep stopped by speech is restarted after ignored speech and after a notice; a warm-up
    stopped by speech is redone."""

    def setUp(self):
        convo_inspect.clear()

    def test_ignored_speech_restarts_the_summary_it_stopped(self):
        async def scenario():
            h = await session(slow=20.0)
            try:
                h.classifier['noise'] = script(SILENT)
                h.handler._compact('browser', h.state, h.state['session'])
                await h.advance(2)
                await h.candidate('n1', h.audio('noise'), sequence=1)           # speech stops the summary
                await h.advance(.5)
                self.assertEqual(h.sidecar.busy, 0)
                h.sidecar.slow = 1.0
                await h.candidate('n1', h.audio('noise'), sequence=2, final=True)   # ... and turns out not to be for the assistant
                await h.settle()
                self.assertEqual(h.sidecar.replies, [])
                self.assertEqual(len(h.store.chapters('a')), 1)                   # restarted and written
            finally:
                await h.close()
        run_virtual(scenario())

    def test_a_warm_up_stopped_by_speech_is_redone_at_the_next_quiet_moment(self):
        async def scenario():
            h = await session(server={'kind': 'llama.cpp', 'slots': 1, 'slot': 0}, replied=True, slow=.5, warm_delay=3.0)
            try:
                h.classifier['noise'] = script(SILENT)
                h.handler._compact('browser', h.state, h.state['session'])
                await h.settle()
                h.handler._swap_and_warm('browser', h.state, h.state['session'])
                await h.advance(1)
                await h.candidate('n1', h.audio('noise'), sequence=1)
                await h.advance(.5)
                self.assertTrue(h.state['warm_owed'])
                h.sidecar.warm_delay = .1
                await h.candidate('n1', h.audio('noise'), sequence=2, final=True)
                await h.settle()
                self.assertEqual(len(h.sidecar.warms), 2)
                self.assertFalse(h.state['warm_owed'])
            finally:
                await h.close()
        run_virtual(scenario())

    def test_a_notice_is_pinned_away_from_the_conversation_and_restarts_upkeep(self):
        def run(slots):
            async def scenario():
                h = await session(server={'kind': 'llama.cpp', 'slots': slots, 'slot': 0}, replied=True, slow=.5, warm_delay=.1, turns=0)
                try:
                    h.model_deltas = [('Your task is done.', .1)]
                    job = {'notice_turn': 'n1', 'turn': 't0', 'status': 'completed', 'prompt': 'p', 'result': 'r'}
                    h.state['task'] = asyncio.ensure_future(h.handler._notice('browser', h.state, h.state['session'], job))
                    await h.settle()
                    notice = h.sidecar.requests[-1]
                    return notice.get('slot'), len(h.sidecar.warms), h.state.get('warm_owed')
                finally:
                    await h.close()
            return run_virtual(scenario())
        self.assertEqual(run(2), (1, 0, None))         # a spare slot: the conversation's cached start is left alone
        self.assertEqual(run(1), (0, 1, False))        # one slot: its start is warmed again afterwards


class Window(unittest.TestCase):
    """Finding 5: the window in effect, what a request also carries, the bands' room, and the server's own overflow."""

    def setUp(self):
        convo_inspect.clear()

    def test_the_bands_leave_room_for_the_fixed_rules(self):
        budget = convo_memory.Budget({'context_tokens': 16384, 'output_reserve': 2048, 'recent_exchanges': 6, 'audio_reserve': 4096})
        left = 16384 - budget.verbatim_high - budget.chapters_max - convo_memory.STANDING_TOKENS - 2048 - 4096
        self.assertGreaterEqual(left, convo_memory.STABLE_TOKENS)
        plan = convo_memory.Budget(SETTINGS)
        self.assertEqual((plan.verbatim_high, plan.chapters_max), (8400, 3000))   # the plan's numbers for 24k are unchanged

    def test_the_servers_per_slot_context_is_read_and_the_smaller_window_wins(self):
        self.assertEqual(app.slot_context({'total_slots': 4, 'default_generation_settings': {'n_ctx': 6144}, 'n_ctx': 24576}), 6144)
        self.assertEqual(app.slot_context({'total_slots': 1, 'n_ctx': 8192}), 8192)
        self.assertEqual(app.slot_context([{'id': 0, 'n_ctx': 4096}, {'id': 1, 'n_ctx': 8192}]), 4096)
        self.assertIsNone(app.slot_context({'total_slots': 1}))

        async def scenario():
            h = await session()
            try:
                async def slots(role):
                    return {'kind': 'llama.cpp', 'slots': 2, 'n_ctx': 4096}
                h.sidecar.slots = slots
                await h.handler._discover_slots(h.state, h.state['session'])
                self.assertEqual(h.handler._window(h.state), 4096)
                self.assertEqual(h.handler._compaction_settings(h.state)['context_tokens'], 4096)
                request = h.handler._compose(h.state, h.state['session'], None, 'Hello?', 'now')
                used = h.handler._estimate(h.state['config'], request['system'], request['history'], None, 'Hello?')
                self.assertLessEqual(used, 4096)                                  # the long history was trimmed to the server's window
            finally:
                await h.close()
        run_virtual(scenario())

    def test_overheard_clips_and_a_tool_result_count(self):
        async def scenario():
            h = await session(turns=0)
            try:
                config = h.state['config']
                plain = h.handler._estimate(config, 's', [], None, 'x')
                clip = h.audio('clip', seconds=5)
                self.assertGreater(h.handler._estimate(config, 's', [], None, 'x', extra_audio=[clip]), plain)
                self.assertEqual(h.handler._estimate(config, 's', [], None, 'x', extra=2000), plain + 2000)
            finally:
                await h.close()
        run_virtual(scenario())

    def test_the_gateway_types_a_context_overflow(self):
        self.assertTrue(app.context_overflow(400, b'{"error":{"type":"exceed_context_size_error","message":"the request exceeds the available context size"}}'))
        self.assertTrue(app.context_overflow(400, b'This model\'s maximum context length is 8192 tokens'))
        self.assertFalse(app.context_overflow(401, b'context length'))
        self.assertFalse(app.context_overflow(400, b'invalid model'))

    def test_a_reply_that_does_not_fit_the_server_is_asked_once_more_with_fewer_turns(self):
        async def scenario():
            h = await session(turns=8)
            try:
                h.sidecar.context_full = 1
                await h.candidate('t1', h.audio('ask'), sequence=1, final=True)
                await h.settle()
                first, second = h.sidecar.requests[0], h.sidecar.requests[1]
                self.assertLess(len(second['history']), len(first['history']))
                self.assertTrue(h.events('text.delta'))                            # answered, not refused
                self.assertEqual(h.events('error'), [])
            finally:
                await h.close()
        run_virtual(scenario())


class Fallbacks(unittest.TestCase):
    """Finding 8: a spare-slot warm-up that keeps failing; per-script sizes."""

    def setUp(self):
        convo_inspect.clear()

    def test_a_kept_chapter_is_swapped_in_after_repeated_spare_slot_warm_up_failures(self):
        async def scenario():
            h = await session(server={'kind': 'llama.cpp', 'slots': 2, 'slot': 0}, replied=True, slow=.5, warm_delay=.1)
            try:
                h.sidecar.warm_fail = True
                for attempt in range(3):
                    h.handler._compact('browser', h.state, h.state['session'])
                    await h.settle()
                    if attempt < 2:
                        self.assertEqual(h.store.chapters('a'), [])               # still waiting for a warm spare slot
                self.assertEqual(len(h.store.chapters('a')), 1)                   # then swapped in on the conversation's slot
                self.assertEqual(h.state['server']['slot'], 0)
            finally:
                await h.close()
        run_virtual(scenario())
        self.assertIn('warm_failed_swapped', [item['reason'] for item in convo_inspect.compactions('a') if item['kind'] == 'fallback'])

    def test_a_ratio_measured_on_one_script_never_sizes_another(self):
        async def scenario():
            h = await session(turns=0)
            try:
                english = [{'transcript': 'Tell me about the weather today, please.', 'assistant': 'It is sunny.'}]
                russian = [{'transcript': 'Расскажи мне о погоде сегодня, пожалуйста.', 'assistant': 'Солнечно.'}]
                h.handler._calibrate(h.state, english, 'Tell me about the weather today. ' * 40, 300)
                self.assertIsNotNone(h.handler._ratio(h.state, english))
                self.assertIsNone(h.handler._ratio(h.state, russian))              # the conservative default instead
                h.handler._calibrate(h.state, russian, 'Расскажи мне о погоде сегодня. ' * 40, 500)
                self.assertNotEqual(h.handler._ratio(h.state, russian), h.handler._ratio(h.state, english))
            finally:
                await h.close()
        run_virtual(scenario())


if __name__ == '__main__':
    unittest.main()
