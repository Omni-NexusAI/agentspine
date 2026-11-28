"""The panel's Permissions and Reasoning menus (convo_voice_modes), answers remembered as "always", the thinking a reply's requests ask for
(convo_tools reasoning shapes, the think tool) and who writes the plan (convo_plan_writer). Scripted model, classifier and host on a virtual
clock; no real model. How much a real model's answers change with each level is measured separately."""
import importlib
import tempfile
import types
import unittest
from pathlib import Path

if __package__:
    from .behavior_harness import script, typed_decision
    from .test_behavior_corpus import DELEGATE_TASK
    from .test_tool_rounds import FIRST, FOLLOW, Loop, call
else:
    from behavior_harness import script, typed_decision
    from test_behavior_corpus import DELEGATE_TASK
    from test_tool_rounds import FIRST, FOLLOW, Loop, call

from plugins._convo.helpers import convo_policy
from plugins._convo.helpers.convo_store import Store

voice_modes = importlib.import_module('plugins._convo.helpers.convo_voice_modes')
plan_writer = importlib.import_module('plugins._convo.helpers.convo_plan_writer')
tools = importlib.import_module('plugins._convo.helpers.convo_tools')
voice_tools = importlib.import_module('plugins._convo.helpers.convo_voice_tools')
modes = importlib.import_module('plugins._convo.helpers.convo_modes')
planning = importlib.import_module('plugins._convo.helpers.convo_planning')
PLAINLY, UNASKED = typed_decision(explicit=.97), typed_decision(explicit=.1)


class Kept(unittest.TestCase):
    """What a conversation chose, in the journal."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'db')
        self.kept = voice_modes.of(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_a_conversation_follows_settings_until_it_chooses_and_each_keeps_its_own(self):
        self.assertEqual(self.kept.get('a', {}), {'permission': 'ask', 'reasoning': 'auto', 'chosen': False})
        self.assertEqual(self.kept.get('a', {'permission_default': 'allow', 'reasoning_default': 'off'}),
                         {'permission': 'allow', 'reasoning': 'off', 'chosen': False})
        self.kept.set('a', {}, reasoning='high')
        self.assertEqual(self.kept.get('a', {'permission_default': 'allow'}), {'permission': 'allow', 'reasoning': 'high', 'chosen': True})
        self.kept.set('a', {}, permission='allow')
        self.assertEqual(self.kept.get('a', {}), {'permission': 'allow', 'reasoning': 'high', 'chosen': True})
        self.assertEqual(self.kept.get('b', {})['chosen'], False)                     # another chat keeps its own
        self.assertIs(voice_modes.of(self.store), self.kept)

    def test_unknown_values_are_refused_and_change_nothing(self):
        for fields in ({'permission': 'plan'}, {'permission': 'always'}, {'reasoning': 'max'}, {'reasoning': ''}):
            with self.subTest(fields):
                with self.assertRaises(ValueError):
                    self.kept.set('a', {}, **fields)
        self.assertFalse(self.kept.get('a', {})['chosen'])

    def test_always_is_kept_only_for_the_kinds_of_work_that_may_be_remembered_and_can_be_forgotten(self):
        self.kept.allow('delegate_task')
        self.kept.allow('remember')
        self.assertEqual(self.kept.allowed(), ['delegate_task', 'remember'])
        self.assertTrue(self.kept.is_allowed('delegate_task'))
        for never in ('voice_mode', 'web_search', 'project_files'):
            with self.subTest(never):
                with self.assertRaises(ValueError):
                    self.kept.allow(never)
                self.assertFalse(self.kept.is_allowed(never))
        self.kept.forget('delegate_task')
        self.assertEqual(self.kept.allowed(), ['remember'])

    def test_it_survives_a_restart_of_the_journal(self):
        self.kept.set('a', {}, permission='allow', reasoning='low')
        self.kept.allow('cancel_task')
        path = Path(self.tmp.name) / 'db'
        self.store.close()
        self.store = Store(path)
        again = voice_modes.of(self.store)
        self.assertEqual((again.get('a', {})['permission'], again.get('a', {})['reasoning'], again.allowed()), ('allow', 'low', ['cancel_task']))


class Permission(unittest.TestCase):
    """What "Allow without asking" and a remembered "always" change in the rule, and what they never change."""
    VERIFIED = convo_policy.Decision.parse(PLAINLY, typed=True)
    NOT_HEARD = convo_policy.Decision.parse(UNASKED, typed=True)

    def run_it(self, decision, **fields):
        base = {'conversing': True, 'followup': False, 'untrusted': False, 'always_ask': False}
        return convo_policy.authorize('delegate_task', 'work', decision, **{**base, **fields})

    def test_ask_is_the_rule_as_it_was(self):
        self.assertEqual(self.run_it(self.VERIFIED), 'run')
        self.assertEqual(self.run_it(self.NOT_HEARD), 'confirm')
        self.assertEqual(self.run_it(self.VERIFIED, always_ask=True), 'confirm')

    def test_allow_or_always_runs_what_the_model_calls_in_a_conversation_the_user_started(self):
        for fields in ({'permission': 'allow'}, {'remembered': True}):
            with self.subTest(fields):
                self.assertEqual(self.run_it(self.NOT_HEARD, **fields), 'run')
                self.assertEqual(self.run_it(self.NOT_HEARD, always_ask=True, **fields), 'run')

    def test_text_from_outside_still_never_starts_anything_by_itself(self):
        for fields in ({'permission': 'allow'}, {'remembered': True}):
            with self.subTest(fields):
                self.assertEqual(self.run_it(self.NOT_HEARD, untrusted=True, **fields), 'confirm')    # someone else's text is in the request
                self.assertEqual(self.run_it(self.VERIFIED, untrusted=True, **fields), 'run')          # the user's own request still carries it
                self.assertEqual(self.run_it(self.VERIFIED, followup=True, **fields), 'refuse')       # just after reading it: never
                self.assertEqual(self.run_it(self.NOT_HEARD, conversing=False, **fields), 'refuse')    # nobody to ask, nobody who asked

    def test_planning_is_a_floor_that_allow_does_not_loosen(self):
        tmp = tempfile.TemporaryDirectory()
        store = Store(Path(tmp.name) / 'db')
        try:
            session = store.start('browser', 'a')
            planning.of(store).begin('a', 'x', session=session)
            helpers = {'convo_policy': convo_policy, 'convo_voice_tools': voice_tools, 'convo_planning': planning, 'convo_tools': tools, 'convo_modes': modes}
            rt = types.SimpleNamespace(_helper=helpers.get, _mark=lambda *args, **fields: None)
            state = {'service': types.SimpleNamespace(store=store), 'session': session, 'config': {}, 'mode': 'active'}
            how = convo_policy.authorize('delegate_task', 'work', self.NOT_HEARD, conversing=True, permission='allow')
            self.assertEqual(how, 'run')

            async def asked(*args, **fields):
                return False
            import asyncio
            from unittest.mock import patch
            with patch.object(voice_tools, 'asked', new=asked):
                final = asyncio.run(modes.decide(rt, state, session, 't', 'delegate_task', {'task': 'x'}, how, conversing=True,
                                                 decision=self.NOT_HEARD, audio='a', transcript=None))
            self.assertEqual(final, 'confirm')                                          # asked once, as planning always does
        finally:
            store.close()
            tmp.cleanup()

    def test_the_reader_has_an_always_and_nothing_else_is_read_as_one(self):
        self.assertEqual(voice_tools.parse_reading('ALWAYS'), 'always')
        self.assertEqual(voice_tools.parse_reading('Always.'), 'always')
        self.assertEqual(voice_tools.parse_reading('YES'), 'yes')
        self.assertEqual(voice_tools.parse_reading('always yes'), 'unread')
        self.assertIn('ALWAYS', voice_tools.READER)


class Remembered(Loop):
    """A spoken "always" to Convo's question: it runs once now and that kind is no longer asked about."""
    def play(self, scenario, **options):
        options.setdefault('typed', True)
        options.setdefault('classifier', {'ask': script(UNASKED, .2), 'next': script(PLAINLY, .2), 'again': script(UNASKED, .2)})
        return super().play(scenario, **options)

    def test_always_runs_it_and_remembers_the_kind(self):
        async def scenario(h):
            h.proposals = [DELEGATE_TASK]
            await self.ask(h)
            asked = (h.state.get('pending') or {}).get('name')
            for _, payload in h.audible():
                if payload.get('type') == 'audio.phrase' and payload['phrase_id'] in h.state['phrases']:
                    await h.playback(payload['turn_id'], payload['phrase_id'])
            await h.advance(3)
            h.reading, h.proposals = 'ALWAYS', []
            await h.candidate('u', h.audio('next'), final=True)
            await h.settle()
            return asked, voice_modes.of(h.store).allowed(), len(h.jobs()), h.state.get('pending')
        self.assertEqual(self.play(scenario), ('delegate_task', ['delegate_task'], 1, None))

    def test_a_remembered_kind_is_not_asked_about_and_the_one_after_its_own_confirmation_neither(self):
        async def scenario(h):
            voice_modes.of(h.store).allow('delegate_task')
            h.proposals = [DELEGATE_TASK]
            await self.ask(h)
            first = (len(h.jobs()), h.state.get('pending'))
            h.proposals = [call('delegate_task', task='Write the second report')]
            await h.candidate('v', h.audio('again'), final=True)                        # Convo's own "the task is queued" is in its history now
            await h.settle()
            return first, len(h.jobs()), h.state.get('pending')
        self.assertEqual(self.play(scenario), ((1, None), 2, None))

    def test_without_it_the_same_call_is_asked_about(self):
        async def scenario(h):
            h.proposals = [DELEGATE_TASK]
            await self.ask(h)
            return len(h.jobs()), (h.state.get('pending') or {}).get('name')
        self.assertEqual(self.play(scenario), (0, 'delegate_task'))


class Reasoning(unittest.TestCase):
    """The request fields for each level: only shapes the gateway knows, and nothing for a model that cannot be asked."""
    CLOUD_EFFORT = {'thinking': 'off', 'thinking_off': {'reasoning_effort': 'none'}, 'effort': 'reasoning_effort'}
    CLOUD_ON = {'thinking': 'off', 'thinking_on': {'reasoning': {'enabled': True}}, 'effort': None}
    LOCAL = {'thinking': 'off', 'thinking_on': {'chat_template_kwargs': {'enable_thinking': True}}, 'effort': None}

    def test_the_fields_for_each_model(self):
        self.assertEqual(tools.thinking_fields(self.CLOUD_EFFORT, 'high', False), {'reasoning_effort': 'high'})
        self.assertEqual(tools.thinking_fields({**self.CLOUD_EFFORT, 'effort': 'reasoning.effort'}, 'low', False), {'reasoning': {'effort': 'low'}})
        self.assertEqual(tools.thinking_fields(self.CLOUD_ON, 'medium', False), {'reasoning': {'enabled': True}})
        self.assertEqual(tools.thinking_fields({'thinking': 'on'}, 'medium', False), {})                     # it thinks anyway: its own amount
        self.assertEqual(tools.thinking_fields(self.LOCAL, 'low', True), {'chat_template_kwargs': {'enable_thinking': True}})
        for record, local in (({}, False), ({}, True), ({'thinking': 'none'}, False), ({'thinking': 'none', 'thinking_on': None}, True)):
            with self.subTest(record=record, local=local):
                self.assertIsNone(tools.thinking_fields(record, 'medium', local))
        self.assertIsNone(tools.thinking_fields(self.LOCAL, 'off', True))
        self.assertIsNone(tools.thinking_fields(self.LOCAL, 'auto', True))

    def test_every_field_set_it_can_make_is_one_the_gateway_accepts_and_nothing_else_is(self):
        for record, local in ((self.CLOUD_EFFORT, False), ({**self.CLOUD_EFFORT, 'effort': 'reasoning.effort'}, False), (self.CLOUD_ON, False),
                              ({'thinking': 'on'}, False), (self.LOCAL, True)):
            for level in tools.LEVELS:
                self.assertTrue(tools.thinking_on_valid(tools.thinking_fields(record, level, local)), (record, level))
        for odd in ({'reasoning_effort': 'max'}, {'reasoning': {'effort': 'none'}}, {'temperature': 2}, {'chat_template_kwargs': {'x': 1}},
                    {'reasoning_effort': 'high', 'extra': 1}, None, 'high'):
            self.assertFalse(tools.thinking_on_valid(odd), odd)

    def test_the_menu_says_what_the_model_takes(self):
        self.assertEqual(tools.reasoning_offer({}, False), {'measured': False, 'can': False, 'graded': False})
        self.assertEqual(tools.reasoning_offer(self.CLOUD_EFFORT, False), {'measured': True, 'can': True, 'graded': True})
        self.assertEqual(tools.reasoning_offer(self.CLOUD_ON, False), {'measured': True, 'can': True, 'graded': False})
        self.assertEqual(tools.reasoning_offer(self.LOCAL, True), {'measured': True, 'can': True, 'graded': False})
        self.assertEqual(tools.reasoning_offer({'thinking': 'none'}, False), {'measured': True, 'can': False, 'graded': False})


class Requests(Loop):
    """What a reply asks the model for, by the conversation's Reasoning choice: on the user's own server through its chat template, from a
    provider through the level field the connection check found."""
    LOCAL = {'thinking': 'off', 'thinking_on': {'chat_template_kwargs': {'enable_thinking': True}}, 'effort': None, 'tool_mode': 'native'}
    CLOUD = {'thinking': 'off', 'thinking_off': {'reasoning_effort': 'none'}, 'effort': 'reasoning_effort', 'tool_mode': 'native'}
    REMOTE = {'roles': {'conversation': {'url': 'https://api.example.com/v1', 'model': 'conversation', 'remote': True}}}
    ON = {'chat_template_kwargs': {'enable_thinking': True}}

    def run_with(self, reasoning, proposals=(), measured=None, cloud=False):
        async def scenario(h):
            h.state['measured'] = dict((self.CLOUD if cloud else self.LOCAL) if measured is None else measured)
            if reasoning:
                voice_modes.of(h.store).set('a', h.config, reasoning=reasoning)
            h.proposals = list(proposals)
            await self.ask(h)
            return [(a.get('thinking_on'), a.get('max_tokens'), 'think' in (a.get('tools') or [])) for a in h.answers]
        return self.play(scenario, **({'config': self.REMOTE} if cloud else {}))

    def test_off_and_auto_ask_for_no_thinking_and_a_level_asks_for_it_with_its_budget(self):
        answer = 1024
        self.assertEqual(self.run_with('off')[0], (None, answer, False))
        self.assertEqual(self.run_with('auto')[0], (None, answer, True))
        self.assertEqual(self.run_with('low')[0], (self.ON, answer + 1024, False))
        self.assertEqual(self.run_with('high')[0], (self.ON, answer + 4096, False))
        self.assertEqual(self.run_with('medium', cloud=True)[0], ({'reasoning_effort': 'medium'}, answer + 2048, False))
        self.assertEqual(self.run_with('off', cloud=True)[0], (None, answer, False))

    def test_under_auto_the_model_chooses_to_think_and_only_what_follows_thinks(self):
        for cloud, fields in ((False, self.ON), (True, {'reasoning_effort': 'high'})):
            with self.subTest(cloud=cloud):
                passes = self.run_with('auto', proposals=[call('think', level='high')], cloud=cloud)
                self.assertEqual(passes[0], (None, 1024, True))
                self.assertEqual(passes[1][:2], (fields, 1024 + 4096))

    def test_a_model_that_cannot_think_is_never_offered_the_think_tool_nor_sent_a_level(self):
        for measured in ({'thinking': 'none', 'thinking_on': None, 'tool_mode': 'native'}, {'tool_mode': 'native'}):
            with self.subTest(measured):
                self.assertEqual(self.run_with('auto', measured=measured)[0], (None, 1024, False))
                self.assertEqual(self.run_with('high', measured=measured)[0], (None, 1024, False))


class Writer(unittest.TestCase):
    """Who writes the plan: by where the main chat model runs and what the user allowed, never by a guess at how capable it is."""
    ROLE = {'roles': {'conversation': {'url': 'http://10.0.0.5:8080/v1', 'model': 'small-voice'}}}
    LOCAL_MAIN = {'provider': 'llama_cpp', 'name': 'big', 'api_base': 'http://10.0.0.9:8000/v1', 'base': 'http://10.0.0.9:8000/v1'}
    CLOUD_MAIN = {'provider': 'openrouter', 'name': 'vendor/big', 'api_base': '', 'base': 'https://openrouter.ai/api/v1'}

    def choose(self, main, **config):
        return plan_writer.choose({**self.ROLE, **config}, main)

    def test_auto(self):
        self.assertEqual(self.choose(self.LOCAL_MAIN)['writer'], 'main')
        self.assertEqual(self.choose(self.CLOUD_MAIN)['writer'], 'conversation')
        self.assertEqual(self.choose(self.CLOUD_MAIN)['reason'], 'main_not_consented')
        self.assertEqual(self.choose(self.CLOUD_MAIN, main_plan_consent=True)['writer'], 'main')
        self.assertEqual(self.choose({})['reason'], 'main_unknown')
        same = {'provider': 'llama_cpp', 'name': 'Small-Voice', 'base': 'http://10.0.0.5:8080/v1'}
        self.assertEqual((self.choose(same)['writer'], self.choose(same)['reason']), ('conversation', 'same_model'))

    def test_a_choice_of_writer_never_widens_who_receives_the_conversation(self):
        self.assertEqual(self.choose(self.CLOUD_MAIN, plan_writer='main')['writer'], 'conversation')
        self.assertEqual(self.choose(self.CLOUD_MAIN, plan_writer='main', main_plan_consent=True)['writer'], 'main')
        self.assertEqual(self.choose(self.LOCAL_MAIN, plan_writer='conversation')['writer'], 'conversation')
        rows = plan_writer.readiness_rows({**self.ROLE, 'plan_writer': 'main'}, self.CLOUD_MAIN)
        self.assertEqual(rows[0]['state'], 'warning')


if __name__ == '__main__':
    unittest.main()


class Probe(unittest.IsolatedAsyncioTestCase):
    """What the connection check learns about thinking, from a scripted server (no real model)."""
    async def check(self, answers, found, local):
        setup = importlib.import_module('plugins._convo.helpers.convo_setup')
        sent = []

        async def post(url, body, headers):
            sent.append({key: value for key, value in body.items() if key in ('reasoning_effort', 'reasoning', 'chat_template_kwargs')})
            return answers(body)
        from unittest.mock import patch
        with patch.object(setup, '_post', new=post):
            return await setup.probe_reasoning('http://x/v1', {'model': 'm'}, {}, found, local), sent

    @staticmethod
    def thought(yes):
        return 200, {'choices': [{'message': {'content': '391', **({'reasoning_content': 'seventeen times...'} if yes else {})}}]}

    async def test_a_local_template_that_thinks_and_one_that_does_not(self):
        learned, sent = await self.check(lambda body: self.thought(True), {}, True)
        self.assertEqual(learned, {'thinking': 'off', 'thinking_on': {'chat_template_kwargs': {'enable_thinking': True}}, 'effort': None})
        self.assertEqual(sent, [{'chat_template_kwargs': {'enable_thinking': True}}])
        learned, _ = await self.check(lambda body: self.thought(False), {}, True)
        self.assertEqual(learned, {'thinking': 'none', 'thinking_on': None, 'effort': None})
        learned, _ = await self.check(lambda body: (503, {}), {}, True)
        self.assertEqual(learned, {})                                                 # no answer: what is on record stays

    async def test_a_provider_with_a_level_field_and_one_that_only_switches_on(self):
        learned, sent = await self.check(lambda body: self.thought('reasoning_effort' in body), {'thinking': 'off'}, False)
        self.assertEqual(learned, {'effort': 'reasoning_effort', 'thinking_on': None})
        self.assertEqual(sent, [{'reasoning_effort': 'high'}])
        learned, _ = await self.check(lambda body: self.thought(body.get('reasoning') == {'enabled': True}), {'thinking': 'none'}, False)
        self.assertEqual(learned, {'effort': None, 'thinking_on': {'reasoning': {'enabled': True}}, 'thinking': 'off'})
        learned, _ = await self.check(lambda body: self.thought(False), {'thinking': 'none'}, False)
        self.assertEqual(learned, {'effort': None, 'thinking_on': None})
        self.assertFalse(tools.can_think({'thinking': 'none', **learned}, False))
        learned, _ = await self.check(lambda body: (400, {}) if 'reasoning_effort' in body else self.thought(False), {'thinking': 'none'}, False)
        self.assertEqual(learned, {'effort': None, 'thinking_on': None})              # a refused field is simply not one it takes
