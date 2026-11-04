"""Content-free timing marks: bounded, typed, observational, and consistent with what the browser is actually sent."""
import asyncio
import json
import unittest

if __package__:
    from .behavior_harness import LONG_REPLY, Harness, run_virtual, script, SPEAK, SILENT, DELEGATE
else:
    from behavior_harness import LONG_REPLY, Harness, run_virtual, script, SPEAK, SILENT, DELEGATE

from plugins._convo.helpers import convo_metrics
from plugins._convo.helpers.convo_metrics import LOG, TimingLog, clean, percentiles, report


class TimingLogTests(unittest.TestCase):
    def test_marks_are_typed_bounded_and_content_free(self):
        log = TimingLog()
        log.mark('t', 'candidate.received', sequence=1, final=True, audio='UklGRg==', transcript='secret words', token='sk-live',
                 disposition='admit' * 30, confidence=float('nan'), epoch=True, chars=12)
        (record,) = log.records()
        (mark,) = record['marks']
        self.assertEqual(set(mark) - {'name', 't', 'seq'}, {'sequence', 'final', 'disposition', 'chars'})   # unknown keys, nan and bool-as-int are dropped
        self.assertEqual(len(mark['disposition']), 48)
        self.assertNotIn('secret', json.dumps(log.records()))
        self.assertNotIn('sk-live', json.dumps(log.records()))
        self.assertEqual(clean({'confidence': .987654}), {'confidence': .988})
        self.assertEqual(clean({'sequence': '1', 'final': 1, 'epoch': 2.5}), {})

    def test_turns_and_marks_per_turn_are_bounded(self):
        log = TimingLog()
        for number in range(convo_metrics.TURNS + 25):
            log.mark('turn-%d' % number, 'a')
        self.assertEqual(len(log.records(1000)), convo_metrics.TURNS)
        self.assertEqual(log.records(1000)[0]['turn'], 'turn-25')                 # the oldest turns go first
        for number in range(convo_metrics.MARKS + 10):
            log.mark('busy', 'tick', sequence=number)
        self.assertEqual(len(log.records(1)[0]['marks']), convo_metrics.MARKS)
        log.reset()
        self.assertEqual(log.records(), [])

    def test_percentiles_use_nearest_rank(self):
        self.assertIsNone(percentiles([]))
        self.assertEqual(percentiles([4, 1, 3, 2]), {'n': 4, 'p50': 2, 'p90': 4, 'p95': 4, 'max': 4})
        self.assertEqual(percentiles(list(range(1, 101)))['p95'], 95)

    def test_report_is_developer_only(self):
        with self.assertRaisesRegex(ValueError, 'developer mode'):
            report({'developer_mode': False})
        self.assertTrue(report({'developer_mode': True})['ok'])
        with self.assertRaisesRegex(ValueError, 'developer mode'):
            report({'developer_mode': False}, 0)

    def test_every_stored_mark_gets_the_next_sequence_number_and_a_cursor_reads_only_newer_marks(self):
        log = TimingLog()
        log.mark('a', 'one'); log.mark('b', 'two'); log.mark('a', 'three')
        self.assertEqual([m['seq'] for r in log.records() for m in r['marks']], [1, 3, 2])        # grouped by turn, numbered by arrival
        first = log.since(0)
        self.assertEqual([(r['turn'], [m['name'] for m in r['marks']]) for r in first['turns']], [('a', ['one', 'three']), ('b', ['two'])])
        self.assertEqual((first['cursor'], first['more'], first['reset']), (3, False, False))
        log.mark('b', 'four')
        later = log.since(first['cursor'])
        self.assertEqual(([m['name'] for r in later['turns'] for m in r['marks']], later['cursor']), (['four'], 4))
        self.assertEqual((log.since(4)['turns'], log.since(4)['cursor']), ([], 4))                   # nothing new: the cursor stays
        log.reset()
        log.mark('c', 'five')
        self.assertEqual([m['seq'] for r in log.since(4)['turns'] for m in r['marks']], [5])         # numbers are never reused

    def test_a_cursor_read_is_bounded_and_a_cursor_ahead_of_the_log_reads_it_again(self):
        log = TimingLog()
        for number in range(7):
            log.mark('turn-%d' % (number % 3), 'tick')
        page = log.since(0, limit=4)
        self.assertEqual((sum(len(r['marks']) for r in page['turns']), page['cursor'], page['more']), (4, 4, True))
        rest = log.since(page['cursor'], limit=4)
        self.assertEqual((sum(len(r['marks']) for r in rest['turns']), rest['cursor'], rest['more']), (3, 7, False))
        restarted = log.since(99)                                                                     # e.g. the host restarted
        self.assertEqual((restarted['reset'], restarted['cursor'], sum(len(r['marks']) for r in restarted['turns'])), (True, 7, 7))
        empty = TimingLog().since(5)
        self.assertEqual((empty['reset'], empty['cursor'], empty['turns']), (True, 0, []))
        for marks in range(convo_metrics.MARKS + 5):
            log.mark('capped', 'tick')
        self.assertEqual(log.sequence, 7 + convo_metrics.MARKS)                                     # a mark dropped by the cap takes no number

    def test_gate_retry_and_context_fields_are_typed_scalars_and_nothing_else(self):
        log = TimingLog()
        log.mark('t', 'policy.gate', sequence=1, final=True, gate='calibrated', speech='speak', disposition='admit', mode='active', floor=.3,
                 p_respond=.7, p_addressed=.6, p_backchannel=.1, p_side=.15, p_unfinished=.05, p_unclear=.1, p_explicit=.85, p_name=.05,
                 transcript='secret words', distribution={'addressed': .6})
        log.mark('t', 'policy.retry', sequence=1, final=True, reason='TimeoutError')
        log.mark('t', 'ctx.estimate', ctx_used=9123, ctx_window=16384)
        gate, retry, context = log.records()[0]['marks']
        self.assertEqual({key: gate[key] for key in ('mode', 'floor', 'p_backchannel', 'p_side', 'p_unfinished', 'p_unclear', 'p_explicit', 'p_name')},
                         {'mode': 'active', 'floor': .3, 'p_backchannel': .1, 'p_side': .15, 'p_unfinished': .05, 'p_unclear': .1, 'p_explicit': .85, 'p_name': .05})
        self.assertNotIn('transcript', gate)
        self.assertNotIn('distribution', gate)                                                   # a structure is never a field
        self.assertEqual(retry['reason'], 'TimeoutError')
        self.assertEqual((context['ctx_used'], context['ctx_window']), (9123, 16384))
        self.assertEqual(clean({'ctx_used': 1.5, 'ctx_window': True, 'floor': 'low', 'mode': 3, 'reason': None, 'p_name': float('inf')}), {})
        self.assertEqual(len(clean({'reason': 'x' * 100})['reason']), 48)

    def test_probe_marks_are_capped_so_a_completed_turn_keeps_its_own_marks(self):
        log = TimingLog()
        for sequence in range(1, 40):                         # speech that went on while the assistant spoke: a probe every 750 ms
            for name in ('candidate.received', 'policy.start', 'policy.done', 'policy.gate', 'state.sent'):
                log.mark('t', name, sequence=sequence, final=False)
        reply = [('candidate.received', True), ('policy.start', True), ('policy.done', True), ('policy.gate', True), ('admitted', None), ('state.sent', True),
                 ('reply.start', None), ('ctx.estimate', None), ('answer.first_delta', None), ('speech.request', None), ('audio.first', None),
                 ('reply.done', None), ('playback.started', None), ('playback.completed', None)]
        for name, final in reply:
            log.mark('t', name, sequence=40, final=final)
        marks = log.records()[0]['marks']
        self.assertEqual(sum(1 for m in marks if m.get('final') is False), convo_metrics.PROBE_MARKS)
        self.assertEqual([m['name'] for m in marks if m.get('final') is not False], [name for name, _ in reply])
        self.assertLessEqual(len(marks), convo_metrics.MARKS)

    def test_only_the_newest_playback_completion_is_kept_and_a_cursor_reader_sees_it(self):
        log = TimingLog()
        log.mark('t', 'reply.done')
        cursor = log.since(0)['cursor']
        for _ in range(70):                                   # a long reply: one completion per spoken phrase
            log.mark('t', 'playback.completed')
        marks = log.records()[0]['marks']
        self.assertEqual([m['name'] for m in marks], ['reply.done', 'playback.completed'])
        self.assertEqual(marks[-1]['seq'], log.sequence)      # the newest copy carries the newest number
        later = log.since(cursor)
        self.assertEqual([m['name'] for r in later['turns'] for m in r['marks']], ['playback.completed'])

    def test_turn_ending_marks_survive_a_full_turn_within_a_hard_ceiling(self):
        log = TimingLog()
        for number in range(convo_metrics.MARKS + 20):
            log.mark('t', 'tick', sequence=number)            # ordinary marks fill the turn
        log.mark('t', 'state.sent', sequence=1, final=True, disposition='admit')
        log.mark('t', 'reply.done', chars=12)
        names = [m['name'] for m in log.records()[0]['marks']]
        self.assertEqual((names.count('tick'), names[-2:]), (convo_metrics.MARKS, ['state.sent', 'reply.done']))
        for _ in range(convo_metrics.END_MARKS + 10):
            log.mark('t', 'reply.error', error='ValueError')
        self.assertEqual(len(log.records()[0]['marks']), convo_metrics.MARKS + convo_metrics.END_MARKS)   # memory stays bounded

    def test_report_carries_the_server_clock_so_the_page_can_place_marks_on_its_own(self):
        LOG.reset()
        self.addCleanup(LOG.reset)
        stamp = LOG.mark('clock-test', 'policy.start')
        for result in (report({'developer_mode': True}), report({'developer_mode': True}, 0)):
            self.assertIsInstance(result['now'], float)
            self.assertGreaterEqual(result['now'], round(stamp, 4) - 1e-4)                         # the same monotonic clock as the marks

    def test_report_with_a_cursor_validates_it_and_returns_only_newer_marks(self):
        LOG.reset()
        self.addCleanup(LOG.reset)
        before = report({'developer_mode': True})['cursor']
        LOG.mark('cursor-test', 'policy.start', sequence=1)
        result = report({'developer_mode': True}, before)
        self.assertEqual([(r['turn'], [m['name'] for m in r['marks']]) for r in result['turns']], [('cursor-test', ['policy.start'])])
        self.assertEqual(result['cursor'], before + 1)
        self.assertIn('summary', result)
        self.assertEqual(report({'developer_mode': True}, result['cursor'])['turns'], [])
        for cursor in (-1, 1.5, '3', True, [1]):
            with self.subTest(cursor=cursor), self.assertRaisesRegex(ValueError, 'cursor'):
                report({'developer_mode': True}, cursor)


class VoicePathMarks(unittest.TestCase):
    """Marks written by the real handler, compared with the events the browser actually received."""
    def setUp(self):
        LOG.reset()
        self.addCleanup(LOG.reset)

    def play(self, scenario, **options):
        async def run():
            h = await Harness.create(**options)
            try:
                return await scenario(h)
            finally:
                await h.close()
        return run_virtual(run())

    @staticmethod
    def names(turn):
        return [m['name'] for r in LOG.records(500) if r['turn'] == turn for m in r['marks']]

    def test_a_reply_leaves_an_ordered_trail_that_matches_the_wire(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            phrase = h.audible('q')[0][1]['phrase_id']
            await h.playback('q', phrase, 'started')
            await h.playback('q', phrase)
            return {'first_audible': h.first_audible('q'), 'state_sent': h.states('q')[-1][0]}
        facts = self.play(scenario, classifier={'question': script(SPEAK, .2)}, model_deltas=[('A complete answer.', .3)], tts_delay=.25)
        self.assertEqual(self.names('q'), ['candidate.received', 'policy.start', 'policy.done', 'policy.gate', 'admitted', 'state.sent', 'reply.start',
                                           'ctx.estimate', 'answer.first_delta', 'speech.request', 'audio.first', 'reply.done', 'playback.started',
                                           'playback.completed'])
        marks = {m['name']: m for r in LOG.records(500) if r['turn'] == 'q' for m in r['marks']}
        received = marks['candidate.received']['t']
        self.assertAlmostEqual(marks['policy.done']['t'] - marks['policy.start']['t'], .2, places=3)             # the scripted classifier latency
        self.assertAlmostEqual(marks['answer.first_delta']['t'] - received, .5, places=3)                        # .2 classifier + .3 model
        self.assertEqual(marks['state.sent']['disposition'], 'admit')
        self.assertEqual((marks['candidate.received']['sequence'], marks['candidate.received']['final']), (1, True))
        self.assertEqual(marks['ctx.estimate']['ctx_window'], 16384)                                             # the configured window
        self.assertTrue(4096 + 2048 < marks['ctx.estimate']['ctx_used'] < 16384)                                  # audio + response reserves and the request
        self.assertEqual((marks['policy.gate']['gate'], marks['policy.gate']['mode']), ('fixed', 'active'))
        self.assertNotIn('p_addressed', marks['policy.gate'])                                                     # a JSON classifier has no probabilities
        # the server's own mark and the event timeline are independent measurements of the same thing
        summary = LOG.summary()
        self.assertAlmostEqual(summary['final_to_first_audio_sent_s']['p50'], facts['first_audible'], places=3)
        self.assertEqual(summary['classifier_s']['n'], 1)
        self.assertEqual(summary['counts']['disposition.admit'], 1)
        self.assertNotIn('audio', json.dumps(LOG.records(500)).replace('audio.first', ''))                      # no payloads, only mark names

    def test_an_interruption_is_timed_from_its_probe_to_the_revocation(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(1.1)
            await h.candidate('s', h.audio('stop-probe'), sequence=1, final=False)
            await h.settle()
        self.play(scenario, model_deltas=LONG_REPLY, tts_delay=.2,
                  classifier={'question': script(SPEAK, .2), 'stop-probe': script(SPEAK, .3)})
        summary = LOG.summary()
        self.assertAlmostEqual(summary['probe_to_admit_s']['max'], .3, places=3)
        self.assertEqual(self.names('s')[:3], ['candidate.received', 'policy.start', 'policy.done'])
        admitted = [m for r in LOG.records(500) if r['turn'] == 's' for m in r['marks'] if m['name'] == 'admitted'][0]
        self.assertEqual(admitted['epoch'], admitted['previous_epoch'] + 1)

    def test_classifier_trouble_is_counted_once_per_call(self):
        async def scenario(h):
            await h.candidate('slow', h.audio('slow'), final=True)
            await h.settle()
            await h.candidate('broken', h.audio('broken'), final=True)
            await h.settle()
        self.play(scenario, classifier={'slow': script(SPEAK, 2.0), 'broken': script(SPEAK, .1, fail=True)})
        counts = LOG.summary()['counts']
        # A completed turn is asked twice (BP3d), so each of these finals records two failed calls and one retry between them.
        self.assertEqual((counts.get('policy.timeout'), counts.get('policy.error'), counts.get('policy.retry'), counts.get('disposition.admit')), (2, 2, 2, None))
        self.assertEqual(self.names('slow').count('policy.error'), 0)               # a timeout is not also an error
        self.assertEqual(self.names('slow').count('policy.start'), 2)
        self.assertEqual(self.names('slow').count('policy.retry'), 1)
        errors = [m for r in LOG.records(500) if r['turn'] == 'broken' for m in r['marks'] if m['name'] == 'policy.error']
        self.assertEqual([e['error'] for e in errors], ['ValueError', 'ValueError'])  # a type name, never the message
        reasons = {turn: [m.get('reason') for r in LOG.records(500) if r['turn'] == turn for m in r['marks'] if m['name'] == 'policy.retry'] for turn in ('slow', 'broken')}
        self.assertEqual(reasons, {'slow': ['TimeoutError'], 'broken': ['ValueError']})                    # why it was asked again is kept

    def test_a_calibrated_decision_records_every_probability_its_mode_and_the_floor_it_was_judged_against(self):
        typed = {'speech': 'silent', 'action': 'none', 'confidence': .9, 'explicit_request': False, 'attention': 'addressed',
                 'attention_probabilities': {'addressed': .55, 'backchannel': .1, 'side_conversation': .2, 'unfinished': .05, 'unclear': .1},
                 'explicit_probability': .4, 'name_probability': .15}
        async def scenario(h):
            h.handler._typed = lambda state: True              # the scripted classifier stands in for the typed System One consumer
            await h.candidate('q', h.audio('probe'), sequence=1, final=False)
            await h.settle()
            await h.candidate('q', h.audio('question'), sequence=2, final=True)
            await h.settle()
        self.play(scenario, classifier={'probe': script(typed, .2), 'question': script(typed, .2)})
        probe, final = [m for r in LOG.records(500) if r['turn'] == 'q' for m in r['marks'] if m['name'] == 'policy.gate']
        self.assertEqual((final['gate'], final['mode'], final['floor'], final['disposition']), ('calibrated', 'active', .3, 'admit'))
        self.assertEqual({key: final[key] for key in ('p_addressed', 'p_backchannel', 'p_side', 'p_unfinished', 'p_unclear', 'p_explicit', 'p_name')},
                         {'p_addressed': .55, 'p_backchannel': .1, 'p_side': .2, 'p_unfinished': .05, 'p_unclear': .1, 'p_explicit': .4, 'p_name': .15})
        self.assertAlmostEqual(final['p_respond'], .65, places=3)
        self.assertEqual((probe['final'], probe['mode'], probe['p_explicit'], probe['disposition']), (False, 'active', .4, 'withhold'))

    def test_an_over_full_context_is_recorded_before_the_reply_is_refused(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            self.assertIsNone(h.first_audible('q'))
        self.play(scenario, classifier={'question': script(SPEAK, .2)}, config={'audio_token_reserve': 16000})
        names = self.names('q')
        context = [m for r in LOG.records(500) if r['turn'] == 'q' for m in r['marks'] if m['name'] == 'ctx.estimate'][0]
        self.assertGreater(context['ctx_used'], context['ctx_window'])
        self.assertLess(names.index('ctx.estimate'), names.index('reply.error'))

    def test_a_probe_is_never_retried_so_its_trouble_is_one_mark(self):
        async def probe(h):
            await h.candidate('p', h.audio('p'), sequence=1, final=False)
            await h.settle()
        self.play(probe, classifier={'p': script(SPEAK, 2.0)})                       # times out
        self.play(probe, classifier={'p': script(SPEAK, .1, fail=True)})             # fails
        counts = LOG.summary()['counts']
        self.assertEqual((counts.get('policy.timeout'), counts.get('policy.error'), counts.get('policy.retry')), (1, 1, None))

    def test_waiting_for_a_busy_speech_provider_is_counted_once_per_turn(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(1.0)
            await h.candidate('s', h.audio('stop-probe'), sequence=1, final=False)
            await h.until(1.5)
            await h.candidate('s', h.audio('stop-final'), sequence=2, final=True)
            await h.settle()
        self.play(scenario, model_deltas=LONG_REPLY, tts_delay=3.0, tts_admission=True, config={'continuation_seconds': 0},   # this is about the speech wait, not joining
                  classifier={'question': script(SPEAK, .2), 'stop-probe': script(SPEAK, .3), 'stop-final': script(SPEAK, .2)})
        self.assertEqual(LOG.summary()['counts'].get('speech.busy'), 1)             # several polls, one turn that had to wait
        self.assertEqual(self.names('s').count('speech.busy'), 1)

    def test_marking_can_never_break_the_voice_path(self):
        original = convo_metrics.LOG.mark
        def explode(*args, **kwargs):
            raise RuntimeError('metrics store unavailable')
        convo_metrics.LOG.mark = explode
        self.addCleanup(setattr, convo_metrics.LOG, 'mark', original)
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            self.assertEqual(h.accepted_turns(), ['q'])
            self.assertIsNotNone(h.first_audible('q'))
        self.play(scenario, classifier={'question': script(SPEAK, .2)})


if __name__ == '__main__':
    unittest.main()
