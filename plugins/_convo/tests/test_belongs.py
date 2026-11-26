"""Speech the classifier takes for talk to someone else is compared with the conversation before it is left unanswered
(convo_voice_tools.belongs, Realtime._weigh): an addition is answered, real side talk is not, and nothing waits for the comparison."""
import unittest

if __package__:
    from .behavior_harness import Harness, make_wav, run_virtual, script, typed_decision
else:
    from behavior_harness import Harness, make_wav, run_virtual, script, typed_decision

ASK = typed_decision(explicit=.95, context_note='')
SIDE = typed_decision(attention='side_conversation', share=.95, explicit=.02, context_note='')
ACK = typed_decision(attention='backchannel', share=.95, explicit=.02, speech_seconds=.5, ack_respond=.05)
SLOW = [('First answer, said in full.', 30.0)]             # a reply that takes long to begin: "being prepared"


async def conversation(h, reply='It is raining there.'):
    """One settled exchange, so there is something to compare with."""
    h.model_deltas = [('USER: what is the weather in Lisbon\nLANGUAGE: English\n' + reply, .1)]
    await h.candidate('first', h.audio('first'), final=True)
    await h.settle()
    for item in [payload for _, payload in h.timeline if payload.get('type') == 'audio.phrase']:
        await h.playback(item['turn_id'], item['phrase_id'])


def spoken(h):
    return [entry[-1] for entry in h.log('speech.start')]


def marks(h, name):
    return [entry for entry in h.handler_marks if entry[1] == name]


class Base(unittest.TestCase):
    @staticmethod
    def joined(h, first, second):
        """The clip the two are joined into (a long pause is kept as 1.5 s), judged like the newest part: talk to someone else."""
        return h.merged('more joined', first, make_wav(0, 1.5), second)

    def play(self, body, **options):
        async def run():
            classifier = {'first': script(ASK, .2), 'ask': script(ASK, .2), 'next': script(ASK, .2), 'more': script(SIDE, .2), 'ack': script(ACK, .2),
                          'more joined': script(SIDE, .2)}
            h = await Harness.create(typed=True, classifier=classifier, **options)
            h.handler_marks = []
            original = h.handler._mark
            def mark(turn, name, **fields):
                h.handler_marks.append((turn, name, fields))
                return original(turn, name, **fields)
            h.handler._mark = mark
            try:
                return await body(h)
            finally:
                await h.close()
        return run_virtual(run())


class WhileAReplyIsPrepared(Base):
    def test_an_addition_takes_over_the_reply_nothing_was_heard_of(self):
        async def body(h):
            await conversation(h)
            h.belongs, h.model_deltas = 'YES', SLOW
            ask = h.audio('ask')
            await h.candidate('ask', ask, final=True)
            await h.advance(9)                                                 # past the joining window, still nothing heard
            h.model_deltas = [('Both answered together.', .1)]
            more = h.audio('more')
            joined = self.joined(h, ask, more)
            await h.candidate('more', more, final=True)
            await h.settle()
            self.assertEqual((h.weighed[0]['audio'], h.answers[-1]['audio']), (more, joined))    # compared alone, answered together
            self.assertNotIn('more joined', [entry[2] for entry in h.log('policy.start')])       # long after the request: judged alone
            return spoken(h), len(h.weighed), h.weighed[0], [m[2].get('reason') for m in marks(h, 'policy.gate') if m[0] == 'more'], h.accepted_turns()
        said, asked, request, reasons, accepted = self.play(body)
        self.assertEqual(said[1:], ['Both answered together.'])
        self.assertEqual(asked, 1)
        self.assertEqual(reasons, ['for_someone_else', 'belongs'])
        self.assertIn('more', accepted)
        self.assertIn('what is the weather in Lisbon', request['system'])
        self.assertIn('It is raining there.', request['system'])
        self.assertIs(request['allow_tools'], False)

    def test_real_side_talk_leaves_the_reply_alone(self):
        for word in ('NO', 'OTHER', 'yes, it might be', ''):
            async def body(h):
                await conversation(h)
                h.belongs, h.model_deltas = word, [('First answer, said in full.', 12.0)]
                ask = h.audio('ask')
                await h.candidate('ask', ask, final=True)
                await h.advance(9)
                more = h.audio('more')
                self.joined(h, ask, more)
                await h.candidate('more', more, final=True)
                await h.settle()
                return spoken(h), h.last_state('more'), h.accepted_turns(), len(h.weighed)
            said, state, accepted, asked = self.play(body)
            self.assertEqual(asked, 1, word)
            self.assertEqual(said[1:], ['First answer, said in full.'], word)
            self.assertEqual(state, 'ignore', word)
            self.assertNotIn('more', accepted, word)

    def test_a_request_whose_words_are_not_known_yet_is_compared_by_its_audio(self):
        async def body(h):
            await conversation(h)
            h.belongs, h.model_deltas = 'NO', [('Slow.', 30.0)]
            ask, more = h.audio('ask'), h.audio('more')
            self.joined(h, ask, more)
            await h.candidate('ask', ask, final=True)
            await h.advance(9)
            await h.candidate('more', more, final=True)
            await h.advance(2)
            return [turn.get('user_audio') == ask for turn in h.weighed[0]['history']], h.weighed[0]['audio'] == more
        self.assertEqual(self.play(body), ([True], True))


    def test_speech_right_after_the_request_is_compared_on_its_own_too(self):
        """Judged joined to the request before it, compared alone: together with the request it would always belong to it."""
        async def body(h):
            await conversation(h)
            h.belongs, h.model_deltas = 'NO', [('Slow.', 30.0)]
            ask, more = h.audio('ask', seconds=2.0), h.audio('more', seconds=3.0)
            h.merged('more joined', ask, make_wav(0, 1.0), more)
            await h.candidate('ask', ask, final=True)
            await h.advance(4.0)
            await h.candidate('more', more, final=True)
            await h.advance(2)
            return [entry[2] for entry in h.log('policy.start')][-1], h.weighed[0]['audio'] == more
        self.assertEqual(self.play(body), ('more joined', True))


class Otherwise(Base):
    def test_with_nothing_going_on_an_addition_is_answered(self):
        async def body(h):
            await conversation(h)
            h.belongs, h.model_deltas = 'YES', [('Noted, only the cheaper ones.', .1)]
            await h.candidate('more', h.audio('more'), final=True)
            await h.settle()
            return spoken(h), h.last_state('more')
        said, state = self.play(body)
        self.assertEqual((said[1:], state), (['Noted, only the cheaper ones.'], 'admit'))

    def test_an_answer_already_being_heard_is_finished_first(self):
        async def body(h):
            await conversation(h)
            h.belongs, h.model_deltas = 'YES', [('A first answer that is being heard. ', .1), ('And its second sentence.', 9.0)]
            await h.candidate('ask', h.audio('ask'), final=True)
            await h.advance(3)
            heard = bool(h.state['phrases'])
            h.model_deltas = [('And the addition.', .1)]
            epoch = h.state['session']['epoch']
            await h.candidate('more', h.audio('more'), final=True)
            await h.advance(2)
            same = h.state['session']['epoch'] == epoch
            await h.settle()
            return heard, same, spoken(h)[1:]
        heard, same, said = self.play(body)
        self.assertTrue(heard and same)
        self.assertEqual(said, ['A first answer that is being heard.', 'And its second sentence.', 'And the addition.'])

    def test_before_anything_was_said_there_is_nothing_to_compare_with(self):
        async def body(h):
            h.belongs = 'YES'
            await h.candidate('more', h.audio('more'), final=True)
            await h.settle()
            return len(h.weighed), h.last_state('more'), spoken(h)
        self.assertEqual(self.play(body), (0, 'ignore', []))

    def test_an_acknowledgement_is_never_put_to_it(self):
        async def body(h):
            await conversation(h)
            h.belongs = 'YES'
            await h.candidate('ack', h.audio('ack'), final=True)
            await h.settle()
            return len(h.weighed), h.last_state('ack')
        self.assertEqual(self.play(body), (0, 'ignore'))

    def test_it_says_nothing_about_actions(self):
        async def body(h):
            await conversation(h)
            h.belongs, h.follow_deltas = 'YES', [('Shall I go ahead?', .1)]
            h.proposals = [{'type': 'tool.proposal', 'name': 'delegate_task', 'arguments': {'task': 'Write the report.'}}]
            await h.candidate('more', h.audio('more'), final=True)
            await h.advance(8)
            return len(h.jobs()), h.state.get('pending') is not None
        self.assertEqual(self.play(body), (0, True))             # asked about first, as for any request the user did not plainly make

    def test_speech_on_another_machine_only_where_it_already_goes(self):
        async def body(h):
            await conversation(h)
            h.state['config']['roles']['conversation']['remote'] = True
            h.state['config']['speculative_reply'] = h.keep
            h.belongs = 'YES'
            await h.candidate('more', h.audio('more'), final=True)
            await h.settle()
            return len(h.weighed)
        for keep in (False, True):
            async def each(h, keep=keep):
                h.keep = keep
                return await body(h)
            self.assertEqual(self.play(each), 1 if keep else 0)


class NothingWaits(Base):
    def test_the_next_utterance_is_decided_while_it_is_read(self):
        async def body(h):
            await conversation(h)
            h.belongs, h.belongs_delay, h.model_deltas = 'NO', 11, [('The next answer.', .1)]
            await h.candidate('more', h.audio('more'), final=True)
            await h.advance(1)
            await h.candidate('next', h.audio('next'), final=True)
            await h.advance(3)
            early = spoken(h)[1:]
            await h.settle()
            return early, spoken(h)[1:]
        early, said = self.play(body)
        self.assertEqual(early, ['The next answer.'])
        self.assertEqual(said, ['The next answer.'])

    def test_what_the_user_says_next_is_joined_to_an_addition(self):
        async def body(h):
            await conversation(h)
            h.belongs, h.belongs_delay = 'YES', 1.0
            await h.candidate('more', h.audio('more'), final=True)
            await h.advance(.5)
            await h.candidate('next', h.audio('next'), sequence=1)              # already speaking again when the answer comes
            await h.advance(1.5)
            h.merged('both', h.audio('more'), h.audio('next'))
            await h.candidate('next', h.audio('next'), sequence=2, final=True)
            await h.settle()
            return [m[1] for m in h.handler_marks if m[0] in ('more', 'next') and m[1].startswith('cont.')], len(spoken(h)) - 1
        kept, said = self.play(body)
        self.assertEqual(kept[:1], ['cont.hold'])
        self.assertEqual(said, 1)

    def test_stopping_ends_it(self):
        async def body(h):
            await conversation(h)
            h.belongs, h.belongs_delay = 'YES', 5
            await h.candidate('more', h.audio('more'), final=True)
            await h.advance(1)
            task = h.state['weigh_task']
            await h.interrupt()
            await h.advance(.5)
            ended = task.done()
            await h.advance(8)
            return ended, spoken(h)[1:], h.accepted_turns()
        done, said, accepted = self.play(body)
        self.assertTrue(done)
        self.assertEqual(said, [])
        self.assertNotIn('more', accepted)


if __name__ == '__main__':
    unittest.main()
