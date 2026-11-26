"""Everything the user says until an answer is heard is one request, and no reply ends in silence (2026-11-24, from a live session:
a thought said over half a minute with pauses lost its first 19 seconds, a slow answer cost the user what they had said before it,
a question card came seconds before the question, and three look-ups ended without a word).

Virtual clock, scripted providers, synthetic audio: Convo's decisions, not real voices or models."""
import asyncio
import importlib
import unittest
from unittest.mock import patch

if __package__:
    from .behavior_harness import Harness, SPEAK, SPEAK_PLAIN, frame_count, make_wav, concat_wavs, run_virtual, script, typed_decision
    from .test_behavior_corpus import DELEGATE_TASK, WEB_SEARCH
else:
    from behavior_harness import Harness, SPEAK, SPEAK_PLAIN, frame_count, make_wav, concat_wavs, run_virtual, script, typed_decision
    from test_behavior_corpus import DELEGATE_TASK, WEB_SEARCH

tools = importlib.import_module('plugins._convo.helpers.convo_tools')
metrics = importlib.import_module('plugins._convo.helpers.convo_metrics')
continuation = importlib.import_module('plugins._convo.helpers.convo_continuation')
history = importlib.import_module('plugins._convo.helpers.convo_history')
REQUEST = typed_decision(explicit=.95, context_note='')
PLAIN = typed_decision(attention='side_conversation', share=.6, explicit=.1, context_note='')      # unclear whom it is for: presumed for Convo
SIDE = typed_decision(attention='side_conversation', share=.95, explicit=.02, context_note='')
UNASKED = typed_decision(explicit=.1, context_note='')
SLOW = [('One answer to all of it.', 20.0)]
HEADER = 'USER: a request\nLANGUAGE: English\n'


def call(name, **arguments):
    return {'type': 'tool.proposal', 'name': name, 'arguments': arguments}


class Base(unittest.TestCase):
    def setUp(self):
        metrics.LOG.reset()
        self.addCleanup(metrics.LOG.reset)

    def play(self, scenario, **options):
        options.setdefault('typed', True)

        async def run():
            h = await Harness.create(**options)
            try:
                result = await scenario(h)
                h.assert_invariants()
                return result
            finally:
                await h.close()
        return run_virtual(run())

    @staticmethod
    def marks(turn, *names):
        return [m for r in metrics.LOG.records(500) if r['turn'] == turn for m in r['marks'] if m['name'] in names]

    @staticmethod
    def spoken(h):
        return [text for _, text in h.spoken]


class OneRequest(Base):
    def test_a_request_during_a_slow_answer_is_answered_with_what_was_said_before_it(self):
        async def scenario(h):
            first, second = h.audio('first', seconds=3.0), h.audio('second', seconds=2.0)
            await h.candidate('a', first, final=True)
            await h.until(14.0)                                              # long past the joining window; nothing was heard yet
            heard = len(h.audible('a'))
            await h.candidate('b', second, final=True)
            await h.settle()
            return heard, frame_count(h.answers[-1]['audio']), frame_count(concat_wavs(first, make_wav(0, 1.5), second)), self.spoken(h), \
                [p['superseded'] for _, p in h.events('turn.superseded')], [len(t['content'].get('superseded') or '') > 0 for t in h.store.context('a')['turns']]
        heard, sent, joined, said, superseded, _ = self.play(scenario, model_deltas=SLOW, classifier={'first': script(REQUEST, .2), 'second': script(REQUEST, .2)})
        self.assertEqual((heard, sent, superseded), (0, joined, ['a']))
        self.assertEqual(len(said), 1)                                       # one answer, to both

    def test_the_same_request_does_not_join_an_answer_already_being_heard(self):
        async def scenario(h):
            first, second = h.audio('first'), h.audio('second')
            await h.candidate('a', first, final=True)
            await h.until(6.0)
            await h.candidate('b', second, final=True)
            await h.settle()
            return frame_count(h.answers[-1]['audio']), frame_count(second), h.events('turn.superseded')
        sent, alone, superseded = self.play(scenario, model_deltas=[('An answer that is heard at once, in one piece.', .2)],
                                            classifier={'first': script(REQUEST, .2), 'second': script(REQUEST, .2)})
        self.assertEqual((sent, superseded), (alone, []))

    def test_after_an_action_what_is_said_later_is_a_request_of_its_own(self):
        """A task already queued is never joined to: the model would do it twice."""
        async def scenario(h):
            first, second = h.audio('first'), h.audio('second')
            await h.candidate('a', first, final=True)
            await h.until(12.0)                                              # queued at about 1 s; the spoken word about it is slow
            jobs = [job['status'] for job in h.jobs()]
            h.proposals = []
            await h.candidate('b', second, final=True)
            await h.settle()
            return jobs, frame_count(h.answers[-1]['audio']), frame_count(second), [job['status'] for job in h.jobs()]
        jobs, sent, alone, after = self.play(scenario, model_deltas=[('', .1)], proposals=[DELEGATE_TASK], follow_deltas=SLOW,
                                             classifier={'first': script(typed_decision(explicit=.99, context_note=''), .2), 'second': script(REQUEST, .2)})
        self.assertEqual(jobs, ['queued'])
        self.assertEqual((sent, after), (alone, ['queued']))

    def test_a_thought_longer_than_one_clip_is_heard_whole_in_order(self):
        async def scenario(h):
            clips = [h.audio('part %d' % index, seconds=9.0) for index in range(5)]
            at = 0.0
            for index, clip in enumerate(clips):
                at += 10.0
                await h.until(at - 9.0)
                await h.candidate('t%d' % index, h.audio('so far %d' % index, seconds=.75), sequence=1, final=False)
                await h.until(at)
                await h.candidate('t%d' % index, clip, sequence=2, final=True)
            await h.settle()
            answer = h.answers[-1]
            return [frame_count(clip) for clip in answer['earlier_audio']] + [frame_count(answer['audio'])], \
                [frame_count(clip) for clip in clips], self.spoken(h), h.accepted_turns(), answer
        sent, said, spoken, accepted, answer = self.play(scenario, model_deltas=SLOW, default=PLAIN)
        one = said[0] + 16000 + said[1]                                      # two parts and the second between them fit one clip
        self.assertEqual(sent, [one, one, said[4]])
        self.assertEqual((len(spoken), len(accepted)), (1, 5))
        self.assertEqual(history.view(answer)[-1]['text'].count('[audio'), 3)

    def test_the_gateway_tells_the_model_the_parts_are_one_request(self):
        app = importlib.import_module('plugins._convo.sidecar.app')
        role = {'capabilities': {'audio_input': True}}
        clips = [make_wav(300 + index, .5) for index in range(3)]
        content = app.answer_content({'audio': clips[2], 'earlier_audio': clips[:2]}, role)
        self.assertEqual([part['type'] for part in content], ['text', 'input_audio', 'input_audio', 'text', 'input_audio'])
        self.assertIn('one thing in several parts', content[0]['text'])
        self.assertEqual([part['input_audio']['data'] for part in content if part['type'] == 'input_audio'], clips)
        self.assertEqual(app.answer_content({'audio': clips[2]}, role)[0]['text'], app.AUDIO_LABEL)
        with self.assertRaises(Exception):
            app.answer_content({'audio': clips[2], 'earlier_audio': clips * 3}, role)
        with self.assertRaises(Exception):
            app.answer_content({'audio': clips[2], 'earlier_audio': ['not audio']}, role)

    def test_a_turn_without_words_is_shown_to_the_model_by_all_its_clips(self):
        found = history.messages([{'user_audio': ['one', 'two'], 'assistant': 'Yes.'}], audio_input=True,
                                 audio_part=lambda clip: [{'type': 'input_audio', 'data': clip}])
        self.assertEqual([part['data'] for part in found[0]['content']], ['one', 'two'])

    def test_a_remark_waiting_for_an_answer_is_told_to_the_request_that_ends_that_answer(self):
        async def scenario(h):
            first, remark, second = h.audio('first'), h.audio('remark', seconds=3.0), h.audio('second')
            await h.candidate('a', first, final=True)
            await h.until(3.0)                                               # its first words are out and being heard
            await h.candidate('r', remark, final=True)
            await h.until(4.0)
            await h.candidate('b', second, final=True)
            await h.settle()
            return h.answers[-1].get('earlier_audio') == [remark], frame_count(h.answers[-1]['audio']) == frame_count(second), self.spoken(h)
        told, alone, said = self.play(scenario, model_deltas=[('First sentence of a long answer, heard. ', .2), ('And a second one much later.', 20.0)],
                                      classifier={'first': script(REQUEST, .2), 'remark': script(PLAIN, .2), 'second': script(REQUEST, .2)})
        self.assertEqual((told, alone), (True, True))
        self.assertIn('First sentence of a long answer, heard.', said[0])

    def test_every_waiting_remark_is_told_in_order_to_the_request_that_ends_the_answer(self):
        """Live 2026-11-25: "I am thinking about building a small bookshelf", "out of pine, with four shelves", "so how many boards?",
        all while an answer was still being heard: the middle one was lost."""
        async def scenario(h):
            first, one, two, second = h.audio('first'), h.audio('one', seconds=3.0), h.audio('two', seconds=4.0), h.audio('second')
            await h.candidate('a', first, final=True)
            await h.until(3.0)
            await h.candidate('r', one, final=True)
            await h.until(6.0)
            await h.candidate('s', two, final=True)
            await h.until(9.0)
            await h.candidate('b', second, final=True)
            await h.settle()
            return h.answers[-1].get('earlier_audio') == [one, two], h.state.get('after_waiters')
        self.assertEqual(self.play(scenario, model_deltas=[('First sentence of a long answer, heard. ', .2), ('And a second one much later.', 40.0)],
                                   classifier={'first': script(REQUEST, .2), 'one': script(PLAIN, .2), 'two': script(PLAIN, .2),
                                               'second': script(REQUEST, .2)}), (True, []))

    def test_remarks_waiting_for_the_same_answer_are_answered_as_one(self):
        async def scenario(h):
            first, one, two = h.audio('first'), h.audio('one', seconds=3.0), h.audio('two', seconds=4.0)
            await h.candidate('a', first, final=True)
            await h.until(3.0)
            await h.candidate('r', one, final=True)
            await h.until(6.0)
            await h.candidate('s', two, final=True)
            await h.settle()
            return (len(h.answers), h.answers[-1].get('earlier_audio') == [one], h.answers[-1]['audio'] == two,
                    h.state.get('after_waiters'), h.state.get('after_parts'))
        self.assertEqual(self.play(scenario, model_deltas=[('First sentence of a long answer, heard. ', .2), ('And a second one later.', 8.0)],
                                   classifier={'first': script(REQUEST, .2), 'one': script(PLAIN, .2), 'two': script(PLAIN, .2)}),
                         (2, True, True, [], None))

    def test_stop_forgets_a_waiting_remark(self):
        async def scenario(h):
            await h.candidate('a', h.audio('first'), final=True)
            await h.until(3.0)
            await h.candidate('r', h.audio('remark', seconds=3.0), final=True)
            await h.until(4.0)
            await h.interrupt()
            await h.candidate('b', h.audio('second'), final=True)
            await h.settle()
            return h.answers[-1].get('earlier_audio')
        self.assertIsNone(self.play(scenario, model_deltas=[('First sentence of a long answer, heard. ', .2), ('And a second one.', 20.0)],
                                    classifier={'first': script(REQUEST, .2), 'remark': script(PLAIN, .2), 'second': script(REQUEST, .2)}))

    def test_speech_that_cut_an_unheard_answer_is_never_left_unanswered(self):
        """Its first check read as a request and ended the answer being prepared; the whole utterance reads as talk to someone else."""
        async def scenario(h):
            first, probe, second = h.audio('first', seconds=2.0), h.audio('probe', seconds=.8), h.audio('second', seconds=4.0)
            h.merged('both', first, make_wav(0, .7), second)
            await h.candidate('a', first, final=True)
            await h.until(1.5)
            await h.candidate('b', probe, sequence=1, final=False)
            await h.until(4.7)
            cut = len(h.events('candidate.admitted', 'b'))
            await h.candidate('b', second, sequence=2, final=True)
            await h.settle()
            return cut, h.last_state('b'), len(self.spoken(h)), frame_count(h.answers[-1]['audio']) > frame_count(second)
        cut, state, said, joined = self.play(scenario, model_deltas=SLOW, default=SIDE,
                                             classifier={'first': script(REQUEST, .2), 'probe': script(typed_decision(explicit=.99, context_note=''), .2)})
        self.assertEqual((cut, state, said, joined), (1, 'admit', 1, True))

    def test_an_acknowledgement_that_cut_an_unheard_answer_still_gets_one(self):
        """Its first check read as a request and ended the answer being prepared; the whole reads as a mere "mm-hm"."""
        ack = typed_decision(attention='backchannel', share=.95, explicit=.02, speech_seconds=.5, ack_respond=.05)

        async def scenario(h):
            first, probe, second = h.audio('first', seconds=2.0), h.audio('probe', seconds=.5), h.audio('second', seconds=.6)
            await h.candidate('a', first, final=True)
            await h.until(1.5)
            await h.candidate('b', probe, sequence=1, final=False)
            await h.until(2.2)
            await h.candidate('b', second, sequence=2, final=True)
            await h.settle()
            return len(h.events('candidate.admitted', 'b')), h.last_state('b'), len(self.spoken(h))
        found = self.play(scenario, model_deltas=SLOW, default=ack,
                          classifier={'first': script(REQUEST, .2), 'probe': script(typed_decision(explicit=.99, context_note=''), .2), 'second': script(ack, .2)})
        self.assertEqual(found, (1, 'admit', 1))

    def test_talk_to_someone_else_in_between_leaves_the_request_joinable(self):
        async def scenario(h):
            first, other, second = h.audio('first', seconds=2.0), h.audio('other', seconds=3.0), h.audio('second', seconds=3.0)
            h.belongs = 'NO'
            h.merged('second', first, make_wav(0, 1.5), second)
            await h.candidate('a', first, final=True)
            await h.until(5.0)
            await h.candidate('x', other, final=True)
            await h.until(10.0)
            await h.candidate('b', second, final=True)
            await h.settle()
            return h.last_state('x'), frame_count(h.answers[-1]['audio']) == frame_count(first) + int(1.5 * 16000) + frame_count(second)
        state, joined = self.play(scenario, model_deltas=SLOW, default=SIDE,
                                  classifier={'first': script(REQUEST, .2), 'other': script(SIDE, .2), 'second': script(REQUEST, .2)})
        self.assertEqual((state, joined), ('ignore', True))


class SeveralAtOnce(Base):
    def ask(self, scenario=None, **options):
        async def run(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return h.answers, h.host.directs, [p['message'] for _, p in h.events('error')], self.spoken(h), h.jobs()
        options.setdefault('classifier', {'ask': script(REQUEST, .2)})
        options.setdefault('model_deltas', [(HEADER, .1)])
        options.setdefault('follow_deltas', [('Here is all of it.', .1)])
        return self.play(scenario or run, **options)

    def test_look_ups_asked_for_together_run_side_by_side_and_are_answered_once(self):
        MEMORY = call('memory_read', query='notes')
        running = []

        async def scenario(h):
            original = h.handler._run_tool

            async def slow(*args, **kwargs):
                running.append(('start', args[4], h.now()))
                await asyncio.sleep(2.0)
                found = await original(*args, **kwargs)
                running.append(('end', args[4], h.now()))
                return found
            h.handler._run_tool = slow
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return h.answers, h.host.directs, [p['message'] for _, p in h.events('error')], self.spoken(h)
        answers, directs, errors, said = self.ask(scenario, proposals=[[WEB_SEARCH, MEMORY, call('web_search', query='snow')]])
        self.assertEqual(len(directs), 3)
        self.assertEqual([kind for kind, _, _ in running], ['start'] * 3 + ['end'] * 3)      # side by side, not one after another
        self.assertEqual((len(answers), errors), (2, []))                                      # one answer to the three, nothing to complain of
        self.assertEqual([turn['name'] for turn in answers[1]['tool_turns']], ['web_search', 'memory_read', 'web_search'])
        self.assertEqual(len({turn['id'] for turn in answers[1]['tool_turns']}), 3)
        self.assertEqual(said[-1], 'Here is all of it.')

    def test_the_same_call_twice_in_one_pass_runs_once(self):
        answers, directs, errors, _, _ = self.ask(proposals=[[WEB_SEARCH, WEB_SEARCH]])
        self.assertEqual((len(directs), len(answers), errors), (1, 2, []))

    def test_an_action_and_a_look_up_asked_for_together_both_happen(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            voice_tools = importlib.import_module('plugins._convo.helpers.convo_voice_tools')
            return len(voice_tools.timers(h.service).pending(voice_tools.TIMER_SCOPE)), h.host.directs, h.answers
        timers, directs, answers = self.ask(scenario, proposals=[[WEB_SEARCH, call('set_timer', minutes=5)]])
        self.assertEqual((timers, len(directs), len(answers)), (1, 1, 2))
        self.assertIn(tools.NO_MORE, answers[1]['tool_turns'][0]['result'])                    # after an action nothing more is called

    def test_an_action_is_still_never_run_after_a_result_was_read(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            voice_tools = importlib.import_module('plugins._convo.helpers.convo_voice_tools')
            return len(voice_tools.timers(h.service).pending(voice_tools.TIMER_SCOPE)), [kind for kind, _ in [(r['kind'], 0) for r in h.journal()] if kind == 'tool.refused']
        self.assertEqual(self.ask(scenario, proposals=[WEB_SEARCH, call('set_timer', minutes=5)]), (0, ['tool.refused']))

    def test_more_calls_than_one_pass_may_make_are_left_out_without_an_error(self):
        many = [call('web_search', query='question %d' % index) for index in range(tools.MAX_CALLS + 2)]
        _, directs, errors, _, _ = self.ask(proposals=[many])
        self.assertEqual((len(directs), errors), (tools.MAX_CALLS, []))

    def test_two_things_that_need_the_users_yes_are_asked_about_one_at_a_time(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return len(h.jobs()), len(h.journal('action.asked')), [turn['result'] for turn in h.answers[1]['tool_turns']]
        jobs, asked, results = self.ask(scenario, proposals=[[call('delegate_task', task='One.'), call('delegate_task', task='Two.')]],
                                        classifier={'ask': script(UNASKED, .2)})
        self.assertEqual((jobs, asked), (0, 1))
        self.assertEqual(results[1].split(':')[0], tools.one_question('task hand-over').split(':')[0])


class NeverSilent(Base):
    def ask(self, deltas, follow, proposals, **options):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return h.answers, self.spoken(h), [p['message'] for _, p in h.events('error')]
        return self.play(scenario, model_deltas=deltas, follow_deltas=follow, proposals=proposals,
                         classifier={'ask': script(REQUEST, .2)}, **options)

    def test_a_model_that_only_keeps_calling_tools_is_asked_once_more_with_the_tools_closed(self):
        """Live 2026-11-24: three look-ups, then a fourth call instead of an answer, and nothing was said."""
        async def scenario(h):
            original = h.sidecar.answer

            async def answer(payload):
                if payload.get('no_tools'):
                    h.closed = payload['no_tools']
                    yield {'type': 'text.delta', 'text': 'This is what I found.'}
                    yield {'type': 'answer.done'}
                    return
                h.calls_made = getattr(h, 'calls_made', 0) + 1
                yield call('web_search', query='question %d' % h.calls_made)
                yield {'type': 'answer.done', 'finish_reason': 'tool_calls', 'tool_calls': 1}
            h.sidecar.answer = answer
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return len(h.host.directs), h.closed, self.spoken(h), [p['message'] for _, p in h.events('error')], h.module.MAX_TOOL_ROUNDS
        directs, closed, said, errors, rounds = self.play(scenario, classifier={'ask': script(REQUEST, .2)})
        self.assertEqual((directs, closed, said, errors), (rounds, 'none', ['This is what I found.'], []))

    def test_a_server_that_refuses_the_closed_list_is_asked_without_any_tool(self):
        async def scenario(h):
            seen = []

            async def answer(payload):
                seen.append(payload.get('no_tools', ''))
                if payload.get('no_tools') == 'none':
                    yield {'type': 'error', 'message': 'The server refused the request (HTTP 400).'}
                    return
                if payload.get('no_tools') == 'text':
                    yield {'type': 'text.delta', 'text': 'Here is the answer.'}
                elif not payload.get('tool_turn'):
                    yield call('web_search', query='weather')
                yield {'type': 'answer.done'}
            h.sidecar.answer = answer
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return seen, self.spoken(h), [p['message'] for _, p in h.events('error')]
        seen, said, errors = self.play(scenario, classifier={'ask': script(REQUEST, .2)})
        self.assertEqual((seen, said, errors), (['', '', 'none', 'text'], ['Here is the answer.'], []))

    def test_when_even_that_is_empty_the_user_hears_that_there_is_no_answer(self):
        answers, said, errors = self.ask([('', .1)], [], [WEB_SEARCH])
        self.assertEqual([a.get('no_tools', '') for a in answers], ['', '', 'none', 'text'])
        self.assertEqual((said, errors), ([tools.NO_ANSWER], []))

    def test_an_acknowledgement_alone_is_not_an_answer(self):
        _, said, _ = self.ask([(HEADER + 'Let me look that up for you.', .1)], [], [WEB_SEARCH])
        self.assertEqual(said, ['Let me look that up for you.', tools.NO_ANSWER])

    def test_a_question_nobody_can_word_is_still_asked_aloud(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return self.spoken(h), h.state['pending']['heard'] is not None, len(h.events('action.pending'))
        said, heard, cards = self.play(scenario, model_deltas=[('', .1)], follow_deltas=[], proposals=[DELEGATE_TASK], classifier={'ask': script(UNASKED, .2)})
        self.assertEqual((said, heard, cards), (['Hand over a task: Write the weekly report. Shall I go ahead?'], True, 1))

    def test_an_empty_first_answer_is_asked_again_twice_and_then_said(self):
        answers, said, errors = self.ask([('', .1)], [], [])
        self.assertEqual([a.get('no_tools', '') for a in answers], ['', '', 'text'])
        self.assertEqual(len(errors), 1)
        self.assertEqual(said, ['Sorry, the model gave me nothing to say to that. Could you say it again?'])

    def test_a_failure_that_may_pass_is_asked_once_more_and_any_other_is_said(self):
        for reason, tries, said_wanted in (('transient', 2, ['All is well.']), ('', 1, ['Sorry, I could not get an answer from the model just now.'])):
            async def scenario(h, reason=reason):
                seen = []

                async def answer(payload):
                    seen.append(1)
                    if len(seen) == 1:
                        yield {'type': 'error', 'message': 'The model server is overloaded.', **({'reason': reason} if reason else {})}
                        return
                    yield {'type': 'text.delta', 'text': 'All is well.'}
                    yield {'type': 'answer.done'}
                h.sidecar.answer = answer
                await h.candidate('t', h.audio('ask'), final=True)
                await h.settle()
                return len(seen), self.spoken(h), len(h.events('error'))
            self.assertEqual(self.play(scenario, classifier={'ask': script(REQUEST, .2)}), (tries, said_wanted, 0 if reason else 1), reason)

    def test_a_request_that_hangs_at_someone_elses_server_is_made_once_more(self):
        async def scenario(h, remote):
            h.state['config']['roles']['conversation']['remote'] = remote
            seen = []

            async def answer(payload):
                seen.append(h.now())
                if len(seen) == 1:
                    await asyncio.sleep(170)
                yield {'type': 'text.delta', 'text': 'Here I am, at last.'}
                yield {'type': 'answer.done'}
            h.sidecar.answer = answer
            await h.candidate('t', h.audio('ask'), final=True)
            await h.advance(60)
            said = self.spoken(h)
            await h.settle()
            return len(seen), said
        for remote, expected in ((True, (2, ['Here I am, at last.'])), (False, (1, []))):
            async def each(h, remote=remote):
                return await scenario(h, remote)
            self.assertEqual(self.play(each, classifier={'ask': script(REQUEST, .2)}), expected, remote)


class WhatIsHeardMeanwhile(Base):
    def run_search(self, deltas, *proposals, follow=None):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return self.spoken(h), h.host.directs, [r['payload'] for r in h.journal() if r['kind'] == 'tool.requested'], h.answers
        return self.play(scenario, model_deltas=deltas, proposals=list(proposals), follow_deltas=follow or [('It is sunny.', .1)],
                         classifier={'ask': script(REQUEST, .2)})

    def test_a_bare_call_says_its_own_words_while_it_runs(self):
        said, directs, requested, answers = self.run_search([('', .1)], call('web_search', query='weather', say='Let me check the forecast'))
        self.assertEqual(said, ['Let me check the forecast.', 'It is sunny.'])
        self.assertEqual(directs, [('a', 'web_search', {'query': 'weather'})])                 # not part of the call
        self.assertNotIn('forecast', str(requested))
        self.assertEqual(answers[1]['tool_turn']['arguments'], {'query': 'weather'})
        self.assertEqual(answers[1]['tool_turn']['said'].strip(), 'Let me check the forecast.')

    def test_not_after_the_models_own_acknowledgement_nor_again_right_away(self):
        said, _, _, _ = self.run_search([(HEADER + 'One moment, checking.', .1)], call('web_search', query='weather', say='Let me check'))
        self.assertEqual(said, ['One moment, checking.', 'It is sunny.'])
        said, _, _, _ = self.run_search([('', .1)], call('web_search', query='a', say='Let me check'), call('web_search', query='b', say='Still looking'),
                                        follow=[('', .1)])
        self.assertEqual(said[0], 'Let me check.')
        self.assertNotIn('Still looking.', said)

    def test_only_plain_short_words_are_said(self):
        for value, spoken in (('  Checking   now ', 'Checking now'), ('<tool_call>', ''), ('x' * 300, ''), (5, ''), ('', ''), ('[SILENT]', '')):
            self.assertEqual(tools.spoken_note('web_search', {'query': 'q', 'say': value}), (spoken, {'query': 'q'}))
        self.assertEqual(tools.spoken_note('set_timer', {'minutes': 5, 'say': 'ok'}), ('', {'minutes': 5, 'say': 'ok'}))

    def test_the_words_are_optional_and_only_on_look_ups_that_take_a_while(self):
        for name in tools.CATALOG:
            function = tools.schema(name)['function']['parameters']
            self.assertEqual('say' in function['properties'], name in tools.SAYS, name)
            self.assertNotIn('say', function['required'])

    def test_the_user_is_told_what_is_waited_for_and_for_how_long(self):
        async def scenario(h):
            h.state['config']['roles']['conversation'].update(model='voice/model', remote=True)
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return [p['message'] for _, p in h.events('notice', 't') if p.get('kind') == 'loading' and p['message']]

        async def slow_state(*args, **kwargs):
            return ''
        with patch('plugins._convo.helpers.convo_models.state', new=slow_state):
            first = self.play(scenario, model_deltas=[('A late answer.', 17.0)], classifier={'ask': script(REQUEST, .2)})
        self.assertEqual(first[0], 'Waiting for voice/model to answer… 4 s')
        self.assertEqual(first[-1], 'Waiting for voice/model to answer… 14 s (the model provider is slow to respond)')
        self.assertEqual(len(first), 3)


class AskedAloud(Base):
    """Work that needs the user's yes is asked about aloud; its card comes with the voice, and only what is said after that answers it."""
    def play(self, scenario, **options):
        options.setdefault('classifier', {'ask': script(UNASKED, .2), 'answer': script(UNASKED, .2), 'talk': script(REQUEST, .2),
                                          'talk so far': script(PLAIN, .2)})
        options.setdefault('model_deltas', [('', .1)])
        options.setdefault('follow_deltas', [('Shall I hand that over?', 6.0)])
        options.setdefault('proposals', [DELEGATE_TASK])
        return super().play(scenario, **options)

    def test_the_card_comes_with_the_voice_not_before_it(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.advance(4)
            early = (h.state['pending'] is not None, len(h.events('action.pending')))
            await h.settle()
            at = [when for when, p in h.timeline if p.get('type') == 'action.pending'][0]
            heard = [when for when, p in h.timeline if p.get('type') == 'audio.phrase'][0]
            return early, abs(at - heard) < .5, len(h.events('action.pending'))
        self.assertEqual(self.play(scenario), ((True, 0), True, 1))

    def test_what_the_user_was_saying_before_it_is_not_its_answer(self):
        """Live 2026-11-24: speech begun before the question could be heard was read as an answer and dropped the task."""
        async def scenario(h):
            first, second = h.audio('ask', seconds=2.0), h.audio('talk', seconds=4.0)
            await h.candidate('t', first, final=True)
            await h.advance(1)
            await h.candidate('u', h.audio('talk so far', seconds=.8), sequence=1, final=False)
            await h.advance(3.5)
            asked = h.state['pending'] is not None
            h.proposals = []
            h.model_deltas = [('All right, both.', .1)]
            await h.candidate('u', second, sequence=2, final=True)
            await h.settle()
            return asked, len(h.readings), [r['kind'] for r in h.journal() if r['kind'].startswith('action.')], \
                frame_count(h.answers[-1]['audio']) > frame_count(second), len(h.events('action.pending')), self.spoken(h), h.answers[-1]['system']
        asked, read, journal, joined, cards, said, system = self.play(scenario, default=REQUEST)
        self.assertEqual((asked, read, journal, joined, cards), (True, 0, ['action.asked', 'action.replaced'], True, 0))
        self.assertEqual(said, ['All right, both.'])
        self.assertNotIn('was not answered', system)                         # nothing is said about a question nobody heard

    def test_what_began_before_the_question_and_ended_after_it_is_not_its_answer_and_leaves_it_open(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask', seconds=2.0), final=True)
            await h.advance(5.5)                                             # the question's voice starts at about six seconds
            await h.candidate('u', h.audio('talk so far', seconds=.8), sequence=1, final=False)
            await h.advance(2.0)
            heard = h.state['pending']['heard'] is not None
            h.proposals = []
            h.model_deltas = [('All right.', .1)]
            await h.candidate('u', h.audio('talk', seconds=3.0), sequence=2, final=True)
            await h.advance(3)
            return heard, len(h.readings), h.state['pending'] is not None, len(h.jobs())
        self.assertEqual(self.play(scenario), (True, 0, True, 0))

    def test_a_yes_with_a_change_has_the_call_made_again_with_it_and_not_asked_again(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            h.reading, h.proposals = 'CHANGE', [call('delegate_task', task='Write the weekly report, in French.')]
            h.follow_deltas = [('Handed over, in French.', .1)]
            await h.advance(2)
            await h.candidate('u', h.audio('answer'), final=True)
            await h.settle()
            return [job['prompt'] for job in h.jobs()], len(h.journal('action.asked')), self.spoken(h)[-1], \
                [r['kind'] for r in h.journal() if r['kind'].startswith('action.')]
        prompts, asked, said, journal = self.play(scenario, follow_deltas=[('Shall I hand that over?', .2)])
        self.assertEqual(len(prompts), 1)
        self.assertIn('in French', prompts[0])
        self.assertEqual((asked, said, journal), (1, 'Handed over, in French.', ['action.asked', 'action.replaced']))

    def test_the_change_is_for_that_action_only(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            h.reading, h.proposals = 'CHANGE', [call('cancel_task')]
            await h.advance(2)
            await h.candidate('u', h.audio('answer'), final=True)
            await h.settle()
            return len(h.journal('action.asked'))
        self.assertEqual(self.play(scenario, follow_deltas=[('Shall I hand that over?', .2)]), 2)

    def test_a_question_whose_voice_fails_is_still_shown(self):
        async def scenario(h):
            async def broken(role, text):
                raise RuntimeError('no voice')
            h.sidecar.speech = broken
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return len(h.events('action.pending')), h.state['pending'] is not None
        self.assertEqual(self.play(scenario, follow_deltas=[('Shall I hand that over?', .2)]), (1, True))


class HiddenHeader(unittest.TestCase):
    """A header written as a bracketed tag is taken off like any other (2026-11-24: "[LANGUAGE: English]" was spoken aloud)."""
    def read(self, text):
        module = importlib.import_module('plugins._convo.helpers.convo_user_line')
        seen = set()
        for size in (1, 2, 3, 5, 11, 400):
            found = module.HeaderFilter()
            out = ''.join(found.feed(text[index:index + size]) for index in range(0, len(text), size)) + found.finish()
            seen.add((found.line, found.language, out.strip()))
        self.assertEqual(len(seen), 1, seen)
        return seen.pop()

    def test_tags_are_the_header(self):
        self.assertEqual(self.read('[LANGUAGE: English] Sir, it is close.'), ('', 'English', 'Sir, it is close.'))
        self.assertEqual(self.read('[USER: how close is it?] [LANGUAGE: English] Very close.'), ('how close is it?', 'English', 'Very close.'))
        self.assertEqual(self.read('[USER: hi there]\n[LANGUAGE: English]\nHello.'), ('hi there', 'English', 'Hello.'))
        self.assertEqual(self.read('USER: hi there\n(LANGUAGE: Spanish) Hola.'), ('hi there', 'Spanish', 'Hola.'))

    def test_other_brackets_are_untouched(self):
        for text in ('[Laughs] Sure.', '[SILENT]', '[WAIT] unfinished', '[1] is the first.', '(By the way) yes.', '[Languages differ] indeed.'):
            self.assertEqual(self.read(text), ('', '', text))

    def test_a_minute_of_words_is_kept(self):
        words = ' '.join('word%d' % index for index in range(180))
        self.assertEqual(self.read('USER: ' + words + '\nLANGUAGE: English\nYes.'), (words, 'English', 'Yes.'))


if __name__ == '__main__':
    unittest.main()
