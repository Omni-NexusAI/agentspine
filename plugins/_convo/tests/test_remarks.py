"""Recent remarks as context: ignored speech is kept in memory for a short while and rides along, once, with the next admitted turn.
Scripted providers, synthetic audio. This sends overheard audio to the conversation model only when the setting is on and only
after a later turn was admitted; it grants nothing."""
import asyncio
import importlib
import json
import unittest

if __package__:
    from .behavior_harness import SILENT, SPEAK, SPEAK_PLAIN, make_wav, run_virtual, script
    from .test_behavior_corpus import Scenario
else:
    from behavior_harness import SILENT, SPEAK, SPEAK_PLAIN, make_wav, run_virtual, script
    from test_behavior_corpus import Scenario

remarks = importlib.import_module('plugins._convo.helpers.convo_remarks')
ON = {'recent_remarks_seconds': 30}


class RingUnit(unittest.IsolatedAsyncioTestCase):
    async def test_clips_come_back_oldest_first_and_are_consumed_by_take(self):
        ring = remarks.Remarks(30)
        clips = [make_wav(100 + i, 1.0) for i in range(3)]
        for clip in clips:
            ring.add(clip)
        self.assertEqual(ring.peek(), clips)
        self.assertEqual(ring.take(), clips)
        self.assertEqual(ring.peek(), [])

    async def test_the_total_is_bounded_and_the_oldest_go_first(self):
        ring = remarks.Remarks(60)
        clips = [make_wav(100 + i, 8.0) for i in range(4)]            # 32 s offered, 20 s allowed
        for clip in clips:
            ring.add(clip)
        self.assertEqual(ring.peek(), clips[2:])
        ring.add(make_wav(7, 21.0))                                   # one clip longer than the whole ring is never kept
        self.assertEqual(ring.peek(), clips[2:])

    async def test_at_most_eight_clips(self):
        ring = remarks.Remarks(60)
        clips = [make_wav(100 + i, .5) for i in range(12)]
        for clip in clips:
            ring.add(clip)
        self.assertEqual(ring.peek(), clips[-8:])

    async def test_a_zero_ttl_keeps_nothing(self):
        ring = remarks.Remarks(0)
        ring.add(make_wav(100, 1.0))
        self.assertEqual((ring.enabled, ring.peek()), (False, []))


class Expiry(unittest.TestCase):
    def test_clips_expire_by_themselves(self):
        async def scenario():
            ring = remarks.Remarks(5)
            ring.add(make_wav(100, 1.0))
            await asyncio.sleep(2)
            ring.add(make_wav(101, 1.0))
            await asyncio.sleep(3.5)                                    # the first is 5.5 s old now, the second 3.5 s
            first = len(ring.peek())
            await asyncio.sleep(2)
            return first, ring.peek(), ring._timer
        first, rest, timer = run_virtual(scenario())
        self.assertEqual((first, rest, timer), (1, [], None))


class Context(Scenario):
    def conversation(self, steps, *, config=ON, mode='active', **options):
        async def scenario(h):
            for name, tag, wait in steps:
                await h.candidate(name, h.audio(tag, level=300 + len(h.tags) * 7, seconds=1.0), final=True)
                await h.settle()
                await h.advance(wait)
            return {'requests': [{'clips': len(a.get('context_audio', [])), 'system': a['system'], 'audio': a['audio']} for a in h.answers],
                    'accepted': h.accepted_turns(), 'journal': json.dumps(h.store.events('a')), 'pending': h.state['remarks'].peek()}
        facts, _ = self.play(scenario, config=config, mode=mode, **options)
        return facts

    CLASS = {'remark': script(SILENT, .2), 'question': script(SPEAK, .2), 'another': script(SILENT, .2)}

    def test_an_ignored_remark_rides_along_with_the_next_admitted_turn_once(self):
        facts = self.conversation([('r', 'remark', 1.0), ('q', 'question', 1.0), ('q2', 'question', 1.0)], classifier=self.CLASS)
        self.assertEqual([r['clips'] for r in facts['requests']], [1, 0])                   # the second question gets nothing: it was consumed
        self.assertIn('overheard', facts['requests'][0]['system'])
        self.assertNotIn('overheard', facts['requests'][1]['system'])
        self.assertEqual(facts['pending'], [])

    def test_remarks_are_attached_oldest_first_and_never_to_an_ignored_turn(self):
        facts = self.conversation([('r1', 'remark', 1.0), ('r2', 'another', 1.0), ('q', 'question', 1.0)], classifier=self.CLASS)
        self.assertEqual(len(facts['requests']), 1)                                         # the two ignored turns made no model request at all
        self.assertEqual(facts['requests'][0]['clips'], 2)

    def test_a_remark_older_than_the_setting_is_forgotten(self):
        facts = self.conversation([('r', 'remark', 40.0), ('q', 'question', 1.0)], classifier=self.CLASS)
        self.assertEqual([r['clips'] for r in facts['requests']], [0])

    def test_off_by_default_nothing_is_kept_or_attached(self):
        facts = self.conversation([('r', 'remark', 1.0), ('q', 'question', 1.0)], config={}, classifier=self.CLASS)
        self.assertEqual([r['clips'] for r in facts['requests']], [0])
        self.assertEqual(facts['pending'], [])

    def test_the_audio_is_never_journaled_and_the_ignored_turn_leaves_no_trace(self):
        facts = self.conversation([('r', 'remark', 1.0), ('q', 'question', 1.0)], classifier=self.CLASS)
        self.assertEqual(facts['accepted'], ['q'])
        self.assertNotIn('UklGR', facts['journal'])                                          # no base64 WAV header anywhere in the journal

    def test_stop_retarget_and_disconnect_forget_the_remarks(self):
        async def scenario(h):
            seen = {}
            for name, action in (('interrupt', lambda: h.interrupt()), ('retarget', lambda: h.retarget('b'))):
                await h.candidate(name, h.audio('remark', level=300 + len(seen)), final=True)
                await h.settle()
                before = len(h.state['remarks'].peek())
                await action()
                seen[name] = (before, len(h.state['remarks'].peek()))
            return seen
        seen, _ = self.play(scenario, config=ON, classifier=self.CLASS)
        self.assertEqual(seen, {'interrupt': (1, 0), 'retarget': (1, 0)})

    def test_an_unfinished_utterance_is_held_for_its_continuation_not_remembered_as_a_remark(self):
        async def scenario(h):
            h.handler._typed = lambda state: True
            await h.candidate('f1', h.audio('part'), final=True)
            await h.settle()
            return len(h.state['remarks'].peek()), h.state['cont'].pending is not None
        unfinished = {'speech': 'silent', 'action': 'none', 'confidence': .6, 'explicit_request': False, 'attention': 'unfinished',
                      'attention_probabilities': {'addressed': .05, 'backchannel': .02, 'side_conversation': .03, 'unfinished': .85, 'unclear': .05},
                      'explicit_probability': .1, 'name_probability': 0.0}
        result, _ = self.play(scenario, config=ON, classifier={'part': script(unfinished, .2)})
        self.assertEqual(result, (0, True))

    def test_ambient_and_name_listening_keep_nothing(self):
        async def scenario(h):
            return h.state['remarks'].enabled
        enabled, _ = self.play(scenario, mode='name', config={**ON, 'ambient_enabled': True, 'activation_names': ['Jarvis'],
                               'roles': {'classifier': {'capabilities': {'audio_input': True}}, 'conversation': {'capabilities': {'audio_input': True}}}})
        self.assertFalse(enabled)

    def test_a_text_only_conversation_never_attaches_audio(self):
        async def scenario(h):
            h.state['remarks'].add(make_wav(100, 1.0))
            await h.candidate('q', None, final=True, transcript='What do you think?')
            await h.settle()
            return [bool(a.get('context_audio')) for a in h.answers]
        result, _ = self.play(scenario, config={**ON, 'roles': {'conversation': {'capabilities': {'audio_input': False}}, 'classifier': {'capabilities': {'audio_input': False}}}},
                              classifier={'<text>': script(SPEAK, .2)})
        self.assertEqual(result, [False])

    def test_with_a_held_parallel_reply_the_clips_are_part_of_the_comparison(self):
        importlib.import_module('plugins._convo.helpers.convo_metrics').LOG.reset()
        facts = self.conversation([('r', 'remark', 1.0), ('q', 'question', 1.0)], config={**ON, 'speculative_reply': True}, classifier=self.CLASS)
        # the ignored remark's own held reply (no clips, discarded) and then the question's held reply, which carried the clip and was promoted
        self.assertEqual([r['clips'] for r in facts['requests']], [0, 1])
        LOG = importlib.import_module('plugins._convo.helpers.convo_metrics').LOG
        names = lambda turn: [m['name'] for r in LOG.records(200) if r['turn'] == turn for m in r['marks'] if m['name'].startswith('spec.')]
        self.assertEqual((names('r'), names('q')), (['spec.start', 'spec.discard'], ['spec.start', 'spec.promote']))
        self.assertEqual(facts['pending'], [])


class SidecarContent(unittest.TestCase):
    """The model request the sidecar builds. Needs fastapi (the simplemem environment has it)."""

    def setUp(self):
        try:
            self.app = importlib.import_module('plugins._convo.sidecar.app')
        except ModuleNotFoundError as error:
            self.skipTest(str(error))

    def test_clips_come_first_labelled_then_the_latest_message(self):
        role = {'capabilities': {'audio_input': True}}
        first, second, latest = make_wav(100, .2), make_wav(101, .2), make_wav(102, .2)
        content = self.app.answer_content({'audio': latest, 'context_audio': [first, second]}, role)
        self.assertEqual([part['type'] for part in content], ['text', 'input_audio', 'input_audio', 'text', 'input_audio'])
        self.assertEqual([part['input_audio']['data'] for part in content if part['type'] == 'input_audio'], [first, second, latest])
        self.assertIn('Context only', content[0]['text'])

    def test_without_clips_the_content_is_exactly_what_it_was(self):
        role = {'capabilities': {'audio_input': True}}
        latest = make_wav(102, .2)
        self.assertEqual(self.app.answer_content({'audio': latest}, role), self.app.input_content({'audio': latest}, role))

    def test_a_model_that_cannot_take_audio_never_receives_clips(self):
        contract = importlib.import_module('plugins._convo.helpers.convo_contract')
        with self.assertRaises(contract.ContractError):
            self.app.answer_content({'transcript': 'Hello', 'context_audio': [make_wav(100, .2)]}, {'capabilities': {}})

    def test_clips_are_validated_and_bounded(self):
        contract = importlib.import_module('plugins._convo.helpers.convo_contract')
        role = {'capabilities': {'audio_input': True}}
        with self.assertRaises(contract.ContractError):
            self.app.answer_content({'audio': make_wav(1, .2), 'context_audio': ['not audio']}, role)
        with self.assertRaises(contract.ContractError):
            self.app.answer_content({'audio': make_wav(1, .2), 'context_audio': [make_wav(1, .1)] * 9}, role)


if __name__ == '__main__':
    unittest.main()
