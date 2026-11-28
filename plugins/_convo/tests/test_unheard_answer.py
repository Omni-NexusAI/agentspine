"""Speech the classifier did not hear as a request never cancels an answer the user has not heard yet (fix B, 2026-11-17), and a
finished turn the classifier could not judge is answered by the conversation model (fix A).

Measured live on the test host 2026-11-17: "Name a planet that has rings." took the model 10 s to start answering; the user's
"Perfect, thank you" (read .001 for the assistant) arrived as that answer began, was admitted by the conversation's presumption,
and cancelled it. The rule is by meaning (what the classifier heard), never by words: a turn answered only because a conversation
presumes speech is for the assistant waits for the answer in progress and is answered after it; a request still takes over.
Virtual time; scripted classifier, model and speech (behavior_harness)."""
import unittest

if __package__:
    from .behavior_harness import Harness, frame_count, SPEAK, run_virtual, script
else:
    from behavior_harness import Harness, frame_count, SPEAK, run_virtual, script

# What the classifier says about an acknowledgement or a remark: not confidently "not for the assistant" (a conversation still
# presumes it is), and not a request either.
REMARK = {'speech': 'silent', 'action': 'none', 'confidence': .5}
# The model starts answering 10 s after the reply begins (as measured live), so speech can come after the 7 s window in which a
# following fragment is joined to the unanswered question (continuation_seconds) and still before the answer is heard.
SLOW = [('A complete answer to the question.', 10.0)]
LATER = 8.5
QUICK = [('A complete answer to the question.', .2)]


def play(scenario, **options):
    async def run():
        h = await Harness.create(**options)
        try:
            result = await scenario(h)
            h.assert_invariants()
            return result
        finally:
            await h.close()
    return run_virtual(run())


def answer_starts(h):
    return [(round(item[1] - h.t0, 2), item[2]) for item in h.log('answer.start')]


class RemarkWaits(unittest.TestCase):
    def test_a_remark_while_the_answer_is_being_prepared_waits_for_it_and_is_answered_after(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(LATER)
            await h.candidate('r', h.audio('remark'), final=True)          # said before the answer began, after the joining window
            await h.settle()
            state = [p for _, p in h.events('candidate.state', 'r')][-1]
            return {'state': (state['state'], state.get('after_reply')), 'admitted': [p['turn_id'] for _, p in h.events('candidate.admitted')],
                    'epoch': h.epoch(), 'heard_q': len(h.audible('q')), 'accepted': h.accepted_turns(), 'answers': answer_starts(h),
                    'q_heard_at': h.first_audible('q'), 'cancelled': len(h.log('answer.cancelled'))}
        facts = play(scenario, model_deltas=SLOW, classifier={'question': script(SPEAK, .2), 'remark': script(REMARK, .2)})
        self.assertEqual(facts['state'], ('admit', True))
        self.assertEqual((facts['admitted'], facts['epoch'], facts['cancelled']), (['q'], 1, 0))      # the answer was never revoked
        self.assertEqual((facts['heard_q'], facts['accepted']), (1, ['q', 'r']))
        self.assertEqual([tag for _, tag in facts['answers']], ['question', 'remark'])           # one model operation at a time, in order
        self.assertGreater(facts['answers'][1][0], facts['q_heard_at'])                           # after the answer was sent to be heard

    def test_a_request_while_the_answer_is_being_prepared_still_takes_it_over(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(LATER)
            await h.candidate('n', h.audio('new question'), final=True)
            await h.settle()
            return {'admitted': [p['turn_id'] for _, p in h.events('candidate.admitted')], 'heard': (len(h.audible('q')), len(h.audible('n'))),
                    'accepted': h.accepted_turns()}
        facts = play(scenario, model_deltas=SLOW, classifier={'question': script(SPEAK, .2), 'new question': script(SPEAK, .2)})
        self.assertEqual((facts['admitted'], facts['heard'], facts['accepted']), (['q', 'n'], (0, 1), ['q', 'n']))

    def test_a_request_after_a_waiting_remark_takes_over_both(self):
        async def scenario(h):
            clips = h.audio('question'), h.audio('remark'), h.audio('correction')
            await h.candidate('q', clips[0], final=True)
            await h.until(LATER)
            await h.candidate('r', clips[1], final=True)
            await h.until(LATER + 1.0)
            await h.candidate('n', clips[2], final=True)
            await h.settle()
            return {'clips': clips, 'last': (frame_count(h.answers[-1]['audio']), h.answers[-1].get('earlier_audio')), 'accepted': h.accepted_turns(), 'heard': [len(h.audible(t)) for t in ('q', 'r', 'n')],
                    'answers': [tag for _, tag in answer_starts(h)], 'cancelled': len(h.log('answer.cancelled'))}
        facts = play(scenario, model_deltas=SLOW, classifier={'question': script(SPEAK, .2), 'remark': script(REMARK, .2),
                                                              'correction': script(SPEAK, .2)})
        self.assertEqual((facts['accepted'], facts['heard']), (['q', 'n'], [0, 0, 1]))
        # The waiting remark never ran on its own; the one answer is to everything said (a joined clip is a new one to the script).
        self.assertEqual((facts['answers'], facts['cancelled']), (['question', '<text>'], 1))
        self.assertEqual(facts['last'], (frame_count(facts['clips'][0]) + frame_count(facts['clips'][2]) + int(1.5 * 16000), [facts['clips'][1]]))

    def test_a_remark_while_the_answer_is_still_playing_waits_for_the_page_to_finish_it(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(.8)                                                # the answer's one phrase is out, not yet heard to its end
            await h.candidate('r', h.audio('remark'), final=True)
            await h.until(1.5)
            phrase = h.audible('q')[0][1]['phrase_id']
            await h.playback('q', phrase, 'started')
            await h.playback('q', phrase)                                    # the page has played it
            done = h.now()
            await h.settle()
            return {'answers': answer_starts(h), 'done': done, 'admitted': [p['turn_id'] for _, p in h.events('candidate.admitted')]}
        facts = play(scenario, model_deltas=QUICK, classifier={'question': script(SPEAK, .2), 'remark': script(REMARK, .2)})
        self.assertEqual(facts['admitted'], ['q'])
        self.assertEqual([tag for _, tag in facts['answers']], ['question', 'remark'])
        self.assertAlmostEqual(facts['answers'][1][0], facts['done'], delta=.15)                  # as soon as it was heard

    def test_a_page_that_never_says_the_answer_was_heard_is_waited_for_no_longer_than_its_audio(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(.8)
            await h.candidate('r', h.audio('remark'), final=True)
            await h.settle()
            return {'answers': answer_starts(h), 'sent': h.first_audible('q')}
        facts = play(scenario, model_deltas=QUICK, classifier={'question': script(SPEAK, .2), 'remark': script(REMARK, .2)})
        self.assertEqual([tag for _, tag in facts['answers']], ['question', 'remark'])
        # The scripted speech has no measurable length, so the bound is when it was sent plus the page's start-up margin.
        self.assertAlmostEqual(facts['answers'][1][0], facts['sent'] + 2.0, delta=.2)

    def test_after_the_answer_has_been_heard_a_remark_is_an_ordinary_turn(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            phrase = h.audible('q')[0][1]['phrase_id']
            await h.playback('q', phrase)
            await h.candidate('r', h.audio('remark'), final=True)
            await h.settle()
            return [p['turn_id'] for _, p in h.events('candidate.admitted')], [p.get('after_reply') for _, p in h.events('candidate.state', 'r')]
        admitted, after = play(scenario, model_deltas=QUICK, classifier={'question': script(SPEAK, .2), 'remark': script(REMARK, .2)})
        self.assertEqual(admitted, ['q', 'r'])
        self.assertNotIn(True, after)


class Unjudged(unittest.TestCase):
    def test_the_user_is_told_once_until_the_classifier_answers_again(self):
        FAILS = [script(SPEAK, .2, fail=True), script(SPEAK, .2, fail=True)]
        async def scenario(h):
            for turn, tag in (('a', 'first'), ('b', 'second'), ('c', 'works'), ('d', 'third')):
                await h.candidate(turn, h.audio(tag), final=True)
                await h.settle()
                for _, payload in h.audible(turn):
                    await h.playback(turn, payload['phrase_id'])
            notices = [p for _, p in h.events('notice') if p.get('kind') == 'info']
            return [p['turn_id'] for p in notices], notices[0]['message'], h.accepted_turns()
        turns, message, accepted = play(scenario, model_deltas=QUICK, classifier={'first': list(FAILS), 'second': list(FAILS),
                                                                                 'works': script(SPEAK, .2), 'third': list(FAILS)})
        self.assertEqual(accepted, ['a', 'b', 'c', 'd'])                          # none lost
        self.assertEqual(turns, ['a', 'd'])                                       # once, and again after it had answered
        self.assertTrue(message.startswith("Convo's classifier failed (ValueError)"), message)
        self.assertTrue(message.endswith(', so the conversation model decided whether to answer.'), message)

    def test_a_turn_the_classifier_could_not_judge_takes_no_action_the_classifier_alone_may_grant(self):
        """No reading means no explicit request: the conversation model may still use the tools a conversation offers it (that is its
        own choice, as on every conversation turn), but nothing is authorised on the classifier's behalf."""
        async def scenario(h):
            await h.candidate('a', h.audio('first'), final=True)
            await h.settle()
            return h.answers[-1].get('allow_tools'), h.jobs()
        allow, jobs = play(scenario, model_deltas=QUICK, classifier={'first': [script(SPEAK, .2, fail=True), script(SPEAK, .2, fail=True)]},
                           mode='active')
        self.assertEqual(jobs, [])
        self.assertIsNotNone(allow)


if __name__ == '__main__':
    unittest.main()
