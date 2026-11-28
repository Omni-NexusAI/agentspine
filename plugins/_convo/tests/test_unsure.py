"""Speech nobody could place (the classifier took it for talk to someone else, the conversation model could not tell): Convo asks, now and
then, whether it was meant for it (Realtime._may_ask), and what the user answers is understood as that answer (_answers_unsure)."""
import asyncio
import unittest

if __package__:
    from .behavior_harness import script, typed_decision
    from .test_belongs import Base, conversation, marks, spoken
else:
    from behavior_harness import script, typed_decision
    from test_belongs import Base, conversation, marks, spoken

QUESTION = [('USER: it would be nice to have a poem\nLANGUAGE: English\nShall I have that written, sir?', .1)]
DELEGATE = {'type': 'tool.proposal', 'name': 'delegate_task', 'arguments': {'task': 'Write a poem.'}}
SOFT = typed_decision(explicit=.3, context_note='')


async def heard(h):
    for item in [payload for _, payload in h.timeline if payload.get('type') == 'audio.phrase']:
        await h.playback(item['turn_id'], item['phrase_id'])


async def unsure(h, turn='more', word='OTHER', reply=QUESTION):
    h.state['gate'].clock = asyncio.get_running_loop().time          # the cooldown in the test's own time
    h.belongs, h.model_deltas = word, reply
    h.doubted = h.audio('more')
    await h.candidate(turn, h.doubted, final=True)
    await h.settle()
    await heard(h)


async def answered(h, deltas=(('Yes, sir.', .1),), wait=2):
    h.model_deltas = list(deltas)
    await h.advance(wait)
    h.reply = h.audio('ack')
    await h.candidate('ack', h.reply, final=True)
    await h.settle()


class Asking(Base):
    def test_it_asks_one_short_question_and_nothing_can_be_done(self):
        async def body(h):
            await conversation(h)
            await unsure(h)
            return spoken(h)[1:], h.answers[-1], [m[2].get('reason') for m in marks(h, 'policy.gate') if m[0] == 'more']
        said, request, reasons = self.play(body)
        self.assertEqual(said, ['Shall I have that written, sir?'])
        self.assertIn('Ask one short question, in character', request['system'])
        self.assertIs(request['allow_tools'], False)
        self.assertEqual(reasons, ['for_someone_else', 'unsure'])

    def test_only_when_the_model_itself_cannot_tell(self):
        for word, asked in (('OTHER', True), ('NO', False), ('', False), ('perhaps so', False)):
            async def body(h, word=word):
                await conversation(h)
                await unsure(h, word=word)
                return 'more' in h.accepted_turns()
            self.assertEqual(self.play(body), asked, word)

    def test_not_every_time(self):
        async def body(h):
            await conversation(h)
            await unsure(h)
            await h.advance(20)
            await unsure(h, 'more again')                    # within the cooldown
            await h.advance(70)
            await unsure(h, 'more later')                    # after it, but nothing was plainly asked in between
            h.model_deltas = [('Plainly answered.', .1)]
            await h.candidate('ask', h.audio('ask'), final=True)
            await h.settle()
            await heard(h)
            await unsure(h, 'more at last')
            return h.accepted_turns()
        self.assertEqual(self.play(body), ['first', 'more', 'ask', 'more at last'])

    def test_not_long_after_it_was_said(self):
        async def body(h):
            await conversation(h)
            h.belongs_delay = 9
            await unsure(h)
            return h.accepted_turns()
        self.assertEqual(self.play(body), ['first'])

    def test_not_while_an_answer_is_being_prepared_or_another_question_is_open(self):
        for blocked in ('_unheard', 'pending'):
            async def body(h, blocked=blocked):
                await conversation(h)
                voice = h.handler._helper('convo_voice_tools')
                original = voice.pending
                if blocked == 'pending':
                    voice.pending = lambda state: {'id': 'p'}
                else:
                    h.handler._unheard = lambda state: 'reply'
                try:
                    await unsure(h)
                finally:
                    voice.pending = original
                return h.accepted_turns()
            self.assertEqual(self.play(body), ['first'], blocked)

    def test_the_model_may_still_stay_silent(self):
        async def body(h):
            await conversation(h)
            await unsure(h, reply=[('USER: pass me the salt\n', .1), ('[SILENT] to someone else', .1)])
            await answered(h)
            return spoken(h)[1:], h.last_state('ack')
        self.assertEqual(self.play(body), ([], 'ignore'))          # nothing was asked, so a "yeah" afterwards answers nothing


class TheAnswer(Base):
    def test_a_short_yes_is_its_answer(self):
        async def body(h):
            await conversation(h)
            await unsure(h)
            await answered(h)
            return spoken(h)[-1], [m[2].get('reason') for m in marks(h, 'policy.gate') if m[0] == 'ack']
        self.assertEqual(self.play(body), ('Yes, sir.', ['clarified']))

    def test_not_long_afterwards_or_after_another_answer_or_unheard(self):
        async def late(h):
            await conversation(h)
            await unsure(h)
            await answered(h, wait=60)
            return h.last_state('ack')

        async def other(h):
            await conversation(h)
            await unsure(h)
            h.model_deltas = [('Something else entirely.', .1)]
            await h.candidate('ask', h.audio('ask'), final=True)
            await h.settle()
            await heard(h)
            await answered(h)
            return h.last_state('ack')

        async def unheard(h):
            await conversation(h)
            h.belongs, h.model_deltas = 'OTHER', QUESTION
            await h.candidate('more', h.audio('more'), final=True)
            await h.settle()
            h.state['phrases'].clear()                             # it never reached the user's ears
            await answered(h)
            return h.last_state('ack')
        for body in (late, other, unheard):
            self.assertEqual(self.play(body), 'ignore', body.__name__)

    def test_stop_forgets_the_question(self):
        async def body(h):
            await conversation(h)
            await unsure(h)
            before = h.state.get('unsure')
            await h.interrupt()
            return before is not None, h.state.get('unsure')
        self.assertEqual(self.play(body), (True, None))

    def test_the_work_it_was_about_is_not_asked_about_a_second_time(self):
        async def body(h):
            await conversation(h)
            await unsure(h)
            h.asked_for, h.proposals = 'YES', [DELEGATE]
            h.follow_deltas = [('It is queued, sir.', .1)]
            await answered(h, deltas=())
            joined = h.handler._helper('convo_continuation').whole([h.doubted], h.reply)
            return len(h.jobs()), h.state.get('pending'), [one['audio'] == joined for one in h.asked]
        self.assertEqual(self.play(body), (1, None, [True]))

    def test_later_work_is_judged_on_its_own_words(self):
        async def body(h):
            await conversation(h)
            await unsure(h)
            await answered(h)
            await heard(h)
            h.model_deltas, h.proposals = [], [DELEGATE]
            h.asked_for, h.follow_deltas = 'OTHER', [('Shall I go ahead?', .1)]
            soft = h.audio('soft')
            await h.candidate('soft', soft, final=True)
            await h.advance(8)
            return [one['audio'] == soft for one in h.asked]
        self.assertEqual(self.play(body, also={'soft': script(SOFT, .2)}), [True])


if __name__ == '__main__':
    unittest.main()
