"""The conversation model declining ([SILENT]) or waiting for the rest of a thought ([WAIT]), the answer to a tool's result, the
voice turn's clock, and interruption while the classifier is still busy. Virtual clock, scripted providers, synthetic audio."""
import asyncio
import importlib
import unittest

if __package__:
    from .behavior_harness import LONG_REPLY, SPEAK, SPEAK_PLAIN, script
    from .test_behavior_corpus import Scenario
else:
    from behavior_harness import LONG_REPLY, SPEAK, SPEAK_PLAIN, script
    from test_behavior_corpus import Scenario

WEB_SEARCH = {'type': 'tool.proposal', 'name': 'web_search', 'arguments': {'query': 'current time in Tokyo'}}
CLOCK = 'Monday, 9 November 2026, 8:41 PM (America/New_York)'


def contents(h, turn):
    return [t['content'] for t in h.store.context('a')['turns'] if t['id'] == turn]


class Declined(Scenario):
    def test_a_silent_reply_is_neither_shown_nor_spoken_and_leaves_the_models_history(self):
        async def scenario(h):
            await h.candidate('q', h.audio('test'), final=True)
            await h.settle()
            first = {'shown': [p['text'] for _, p in h.events('text.delta', 'q')], 'audible': len(h.audible('q')),
                     'held': [r['payload'] for r in h.journal('turn.held')], 'content': contents(h, 'q')[0],
                     'notice': [p for _, p in h.events('notice', 'q') if p.get('kind') == 'held']}
            h.model_deltas = [('USER: What is the capital of Kansas?\nTopeka, sir.', .2)]
            await h.until(h.now() + 2)
            await h.candidate('k', h.audio('kansas'), final=True)
            await h.settle()
            return first, h.answers[-1]['history'], [text for _, text in h.spoken]
        (first, history, spoken), _ = self.play(
            scenario, classifier={'test': script(SPEAK, .2), 'kansas': script(SPEAK, .2)},
            model_deltas=[('USER: This is a test. Do not respond.\n', .1), ('[SILENT] asked not to respond', .1)])
        self.assertEqual((first['shown'], first['audible']), ([], 0))
        self.assertEqual(first['held'], [{'decision': 'silent', 'reason': 'asked not to respond'}])
        self.assertEqual(first['notice'][0]['decision'], 'silent')
        self.assertEqual((first['content'].get('held'), first['content'].get('transcript')), ('silent', 'This is a test. Do not respond.'))
        self.assertEqual(history, [])                                  # the declined exchange is not in the next turn's history
        self.assertEqual(spoken, ['Topeka, sir.'])

    def test_a_silence_the_classifier_contradicts_is_asked_once_more_and_answered(self):
        """Live 2026-11-09: the model wrote [SILENT] for "What about Kansas?" (classifier .94 addressed)."""
        async def scenario(h):
            original = h.sidecar.answer
            async def answer(payload):
                first = len(h.answers) == 0
                h.model_deltas = [('USER: What about Kansas?\n[SILENT]', .1)] if first else [('USER: What about Kansas?\nTopeka, sir.', .1)]
                async for item in original(payload):
                    yield item
            h.sidecar.answer = answer
            await h.candidate('k', h.audio('kansas'), final=True)
            await h.settle()
            return h.answers, [text for _, text in h.spoken], h.journal('turn.held')
        (answers, spoken, held), _ = self.play(scenario, classifier={'kansas': script(SPEAK, .2)})
        self.assertEqual(len(answers), 2)
        self.assertIn('You chose [SILENT], but this was said to you', answers[1]['system'])
        self.assertEqual((spoken, held), (['Topeka, sir.'], []))

    def test_a_waited_thought_is_joined_to_what_follows_and_answered_once(self):
        async def scenario(h):
            first = h.audio('part one', seconds=.8)
            await h.candidate('t1', first, final=True)
            await h.settle()
            waited = {'audible': len(h.audible('t1')), 'held': [r['payload'] for r in h.journal('turn.held')]}
            h.model_deltas = [('USER: I need to go to the store.\nThe store opens at nine, sir.', .2)]
            await h.until(h.now() + 1.0)                               # the user goes on a second later
            second = h.audio('part two', seconds=.6)
            await h.candidate('t2', second, final=True)
            await h.settle()
            return waited, [text for _, text in h.spoken], h.answers[-1]['audio'], first, second, contents(h, 't1')[0]
        (waited, spoken, audio, first, second, t1), _ = self.play(
            scenario, classifier={'part one': script(SPEAK_PLAIN, .2), 'part two': script(SPEAK_PLAIN, .2)}, default=SPEAK_PLAIN,
            model_deltas=[('USER: I need to\n[WAIT]', .2)])
        self.assertEqual(waited, {'audible': 0, 'held': [{'decision': 'wait', 'reason': ''}]})
        self.assertEqual(spoken, ['The store opens at nine, sir.'])    # one answer, to the whole thought
        self.assertNotIn(audio, (first, second))                       # the model heard both parts joined
        self.assertGreater(len(audio), len(first) + len(second) - 100)
        self.assertEqual(t1.get('superseded'), 't2')

    def test_the_models_report_of_the_users_words_is_kept_for_the_next_turn(self):
        """Regression (2026-11-09): the reported USER line was handed to the service instead of its store and silently lost, so
        without Whisper the model never saw what the user had asked ("What about Kansas?" had nothing to follow)."""
        async def scenario(h):
            await h.candidate('j', h.audio('japan'), final=True)
            await h.settle()
            for phrase in [p['phrase_id'] for _, p in h.audible('j')]:            # the browser heard the reply to its end
                await h.playback('j', phrase, 'started'); await h.playback('j', phrase)
            h.model_deltas = [('USER: What about Kansas?\nTopeka, sir.', .2)]
            await h.until(h.now() + 2)
            await h.candidate('k', h.audio('kansas'), final=True)
            await h.settle()
            return contents(h, 'j')[0], h.answers[-1]['history']
        (japan, history), _ = self.play(scenario, classifier={'japan': script(SPEAK, .2), 'kansas': script(SPEAK, .2)},
                                        model_deltas=[('USER: What is the capital of Japan?\nTokyo, sir.', .2)])
        self.assertEqual((japan['transcript'], japan['transcript_source']), ('What is the capital of Japan?', 'model'))
        self.assertEqual([(t.get('transcript'), t.get('assistant')) for t in history], [('What is the capital of Japan?', 'Tokyo, sir.')])

    def test_a_request_is_answered_normally_when_the_model_does_not_decline(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return [text for _, text in h.spoken], h.journal('turn.held')
        (spoken, held), _ = self.play(scenario, classifier={'question': script(SPEAK, .2)},
                                      model_deltas=[('USER: Wait, what is the time?\n', .1), ('Wait, sir: it is late.', .1)])
        self.assertEqual((spoken, held), (['Wait, sir: it is late.'], []))


class ToolAnswers(Scenario):
    def test_a_failed_search_is_told_to_the_model_which_tells_the_user(self):
        async def scenario(h):
            async def direct(target, name, args):
                h.host.directs.append((target, name, args))
                raise ValueError('The search service did not answer')
            h.host.direct = direct
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return h.answers, [p['message'] for _, p in h.events('error', 't')]
        (answers, errors), _ = self.play(scenario, classifier={'ask': script(SPEAK_PLAIN, .2)}, proposals=[WEB_SEARCH],
                                          model_deltas=[('Let me check.', .1)], follow_deltas=[('The search is not answering right now.', .1)])
        self.assertEqual(len(answers), 2)
        self.assertIn('The web search failed: the search service did not answer', answers[1]['tool_turn']['result'])
        # A failed look-up is one of the reply's few rounds, not its end: the model may still try another read-only tool.
        self.assertTrue(answers[1]['allow_tools'])
        self.assertIn(importlib.import_module('plugins._convo.helpers.convo_tools').ONE_MORE, answers[1]['tool_turn']['result'])
        self.assertEqual(errors, [])                                   # not Convo's generic failure: the model says what happened

    def test_the_clock_is_in_the_voice_system_text_and_the_held_reply_still_matches(self):
        async def scenario(h):
            h.host.local_time = lambda: CLOCK
            await h.candidate('t', h.audio('time'), final=True)
            await h.settle()
            return h.answers, [m for m in h.log('answer.start')], h.journal()
        (answers, starts, _), _ = self.play(scenario, classifier={'time': script(SPEAK, .4)}, config={'speculative_reply': True})
        self.assertIn('Current local date and time: ' + CLOCK + '.', answers[0]['system'])
        self.assertEqual(len(starts), 1)                               # the held parallel reply was used, not asked again


class OverlapWhileBusy(Scenario):
    def run_over(self, voiced, *, barge=None, busy=True):
        async def scenario(h):
            h.handler._typed = lambda state: True
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(1.0)
            phrase = h.audible('q')[0][1]['phrase_id']
            if busy:
                # A classifier call that timed out and ignores its cancellation still owns the classifier.
                h.state['policy_task'] = asyncio.get_running_loop().create_future()
            fields = {'overlap': True, 'voiced_s': voiced, 'heard': {'phrase_id': phrase, 'played_s': .5}}
            if barge is not None:
                fields['barge_in_s'] = barge
            await h.candidate('s', h.audio('over'), sequence=1, final=False, **fields)
            await h.until(h.now() + 2.0)
            state = h.last_state('s')
            h.state['policy_task'] = None
            await h.settle()
            return state, h.events('candidate.admitted', 's')
        result, _ = self.play(scenario, model_deltas=LONG_REPLY, tts_delay=.4,
                              classifier={'question': script(SPEAK, .2), 'over': script(SPEAK, .2)})
        return result

    def test_the_safety_yield_never_waits_for_a_busy_classifier(self):
        state, admitted = self.run_over(1.2)
        self.assertEqual(state, 'admit')
        self.assertEqual(admitted[0][1].get('fade_ms'), 100)

    def test_short_speech_waits_for_a_busy_classifier_then_degrades_without_yielding(self):
        state, admitted = self.run_over(.5)
        self.assertEqual((state, admitted), ('degraded', []))

    def test_this_devices_own_interrupt_after_is_used(self):
        state, admitted = self.run_over(.6, barge=.5)
        self.assertEqual(state, 'admit')
        state, admitted = self.run_over(.6, barge=.3)                 # outside the setting's bounds: the saved 1.0 s applies
        self.assertEqual((state, admitted), ('degraded', []))


if __name__ == '__main__':
    unittest.main()
