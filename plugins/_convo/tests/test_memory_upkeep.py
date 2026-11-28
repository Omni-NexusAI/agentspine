"""Memory upkeep that never slows a reply (plan change 5, "No slower reply after a swap" and "Who writes chapters, and privacy").

The summary request reuses the conversation's cached start; a written chapter is kept until a swap costs least (one slot: when a
reply's text is complete, then the new start is warmed while the reply is spoken; spare slots: warmed in a spare slot first);
speech stops both; cloud providers get no warm-up and no slot pin; Agent Zero's utility model writes summaries only on the user's
own machines or with consent. Real handler, scripted providers, virtual clock: this proves Convo's decisions, not a server's cache
behaviour or real latency."""
import asyncio
import copy
import importlib
import json
import unittest
from unittest.mock import patch

from plugins._convo.helpers import convo_history, convo_inspect, convo_memory, convo_summarizer
from plugins._convo.helpers.convo_contract import ContractError, validate_settings
from plugins._convo.tests.behavior_harness import SPEAK, Harness, ScriptedSidecar, run_virtual

app = importlib.import_module('plugins._convo.sidecar.app')

# Long exchanges, so a few of them pass the word-for-word band of a small window.
LONG = ('Here is a long thought number {n}: ' + 'we talked about plans, numbers, places and people again. ' * 12,
        'Understood, sir, about thought {n}: ' + 'the plans, numbers, places and people are all noted carefully. ' * 12)
# The soak's exchanges: the user's words come back as the model's own USER line, in the user's language.
SOAK = {
    'en': ('Tell me about the weather in city number {n}, and remember that my code is {n} and my sister is Anna.',
           'It is mild and sunny in city {n}, sir; I have noted your code {n}. ' * 3),
    'ru': ('Расскажи мне о погоде в городе номер {n} и запомни, что мой код {n}, а мою сестру зовут Анна.',
           'В городе {n} тепло и солнечно, сэр; я запомнил ваш код {n}. ' * 3),
    'hi': ('शहर नंबर {n} में मौसम के बारे में बताओ और याद रखो कि मेरा कोड {n} है और मेरी बहन का नाम अन्ना है।',
           'शहर {n} में मौसम सुहाना है, सर; मैंने आपका कोड {n} याद रख लिया है। ' * 3),
}
SMALL = {'compaction': {'context_tokens': 8192, 'output_reserve': 1024, 'recent_exchanges': 6, 'trigger': 0.7, 'target': 0.5},
         'audio_token_reserve': 1024}


def wire(payload):
    """The messages and tools the gateway sends the model server for one /answer payload (sidecar/app.py answer), and how many
    messages come before the latest one."""
    role = payload['role']
    stable, late = app.split_system(payload['system'])
    content = app.with_late(app.answer_content(payload, role), late)
    tools = app.offered_tools(payload, role)
    messages = [{'role': 'system', 'content': stable}] + app.history_messages(payload.get('history', []), role, native_tools=bool(tools))
    before = len(messages)
    convo_history.add_user(messages, content)
    return messages, tools, before


def add_turns(h, count):
    epoch = h.ids()['epoch']
    for n in range(count):
        user, reply = LONG
        h.store.accept_turn(h.session['id'], 'browser', epoch, 'old%d' % n, {'transcript': user.format(n=n)})
        h.store.delivered(h.session['id'], 'browser', epoch, 'old%d' % n, 'p', reply.format(n=n))
        h.store.settle(h.session['id'], 'browser', epoch, 'old%d' % n, reply.format(n=n))


def upkeep(payload):
    return str(payload.get('transcript') or '').startswith(convo_memory.UPKEEP)


class Upkeep(ScriptedSidecar):
    """The scripted conversation model, with summaries that take `slow` seconds (every `fail_every`-th fails) and warm-ups that
    answer after `warm_delay`. Records whether a reply ever started while a summary or warm-up was still running, and every
    reply request (to check it fits the window)."""
    def __init__(self, harness, slow=0.0, fail_every=0, warm_delay=0.05):
        super().__init__(harness)
        self.slow, self.fail_every, self.warm_delay = slow, fail_every, warm_delay
        self.summaries, self.busy, self.overlaps, self.warms, self.replies, self.requests = 0, 0, [], [], [], []
        self.notes_text = None          # what a standing-notes request answers (None: like a chapter)
        self.warm_fail = False          # warm-ups answer with an error
        self.context_full = 0           # this many reply requests are refused as not fitting the server's context

    async def answer(self, payload):
        loop = asyncio.get_running_loop()
        if upkeep(payload) or payload.get('warm'):
            self.h.answers.append(copy.deepcopy(payload))
            self.busy += 1
            try:
                if payload.get('warm'):
                    self.warms.append((loop.time(), len(self.h.store.chapters('a'))))
                    await asyncio.sleep(self.warm_delay)
                    yield {'type': 'error', 'message': 'down'} if self.warm_fail else {'type': 'text.delta', 'text': '.'}
                    return
                self.summaries += 1
                n = self.summaries
                await asyncio.sleep(self.slow)
                if self.fail_every and n % self.fail_every == 0:
                    yield {'type': 'error', 'message': 'summariser unreachable'}
                    return
                notes = 'standing notes' in str(payload.get('transcript'))
                yield {'type': 'text.delta', 'text': self.notes_text if notes and self.notes_text is not None
                       else 'Chapter %d of the conversation.' % n}
                yield {'type': 'answer.done'}
            finally:
                self.busy -= 1
            return
        self.replies.append(loop.time())
        self.requests.append(copy.deepcopy(payload))
        if self.busy:
            self.overlaps.append(loop.time())
        if self.context_full:
            self.context_full -= 1
            yield {'type': 'error', 'message': 'does not fit', 'reason': 'context_full'}
            return
        async for item in super().answer(payload):
            yield item


async def session(slow=1.0, warm_delay=.05, fail_every=0, server=None, replied=False, turns=10, **options):
    config = options.pop('config', {})
    h = await Harness.create(config={**SMALL, 'developer_mode': True, **config}, default=SPEAK, **options)
    h.sidecar = Upkeep(h, slow=slow, fail_every=fail_every, warm_delay=warm_delay)
    h.state['sidecar'] = h.sidecar
    await h.settle()                                  # the session's own start-up compaction (nothing to do yet) has finished
    if server is not None:
        h.state['server'] = dict(server)
    if replied:
        h.state['replied'] = True                     # the model server already holds this conversation's start
    add_turns(h, turns)
    return h


def compactor(h):
    return h.state['service'].compactor


class SummaryRequest(unittest.TestCase):
    def setUp(self):
        convo_inspect.clear()

    def test_the_summary_shares_the_conversations_byte_identical_start_and_ends_with_the_instruction(self):
        async def scenario():
            h = await session(server={'kind': 'llama.cpp', 'slots': 1, 'slot': 0})
            try:
                conversation = h.handler._compose(h.state, h.state['session'], None, 'And what next?', 'now')
                await compactor(h).run(h.session['id'], 'browser', h.state['config']['compaction'],
                                       summarize=lambda source: h.handler._summarize(h.state, source), hold=True)
                self.assertTrue(compactor(h).swap('a'))
                summaries = [p for p in h.answers if upkeep(p)]
                self.assertEqual(len(summaries), 2)                       # the chapter, then the standing notes
                sent, tools, before = wire(conversation)
                for payload in summaries:
                    got, got_tools, got_before = wire(payload)
                    self.assertEqual(got_before, before)
                    self.assertEqual(json.dumps(got[:before], ensure_ascii=False), json.dumps(sent[:before], ensure_ascii=False))
                    self.assertEqual(got_tools, tools)
                    self.assertEqual(len(got), before + 1)                # one message more: the instruction, last
                    self.assertTrue(got[-1]['content'].startswith(convo_memory.UPKEEP))
                    self.assertEqual(payload['slot'], 0)                  # the conversation's slot
                    self.assertLessEqual(payload['max_tokens'], 2 * convo_memory.CHAPTER_TOKENS)
                    self.assertNotIn('Data (JSON)', payload['transcript'])   # turns in the history are named, never sent again
                covered = h.store.chapters('a')[0]['covered']
                for payload in summaries:                                 # the span is the head of the history, by count
                    self.assertIn('the first %d exchanges of the conversation above' % len(covered), payload['transcript'])
                self.assertNotIn('Here is a long thought', summaries[0]['transcript'])
                self.assertIn("only what the user said", summaries[1]['transcript'])
                self.assertEqual(len(h.store.chapters('a')), 1)
                self.assertGreater(len(sent), 4)
            finally:
                await h.close()
        run_virtual(scenario())

    def test_turns_no_longer_in_the_history_are_sent_as_data(self):
        text = convo_memory.cached_instruction({'instruction': convo_memory.INSTRUCTION, 'turns': [{'user': 'Привет', 'assistant': 'Да'}],
                                                'covers': ['x'], 'in_history': False})
        self.assertIn('Data (JSON)', text)
        self.assertIn('Привет', text)                                     # UTF-8, not escapes
        self.assertNotIn('covers', text)


class StoppedBySpeech(unittest.TestCase):
    def test_a_summary_stops_when_the_user_speaks_and_is_redone_in_the_next_quiet_moment(self):
        async def scenario():
            h = await session(slow=20.0)
            try:
                h.handler._compact('browser', h.state, h.state['session'])
                await h.advance(2)
                self.assertEqual(h.sidecar.busy, 1)
                task = h.state['compact_task']
                await h.candidate('u1', h.audio('speech'), sequence=1)  # the user starts speaking
                await h.advance(.5)
                self.assertTrue(task.cancelled())
                self.assertEqual(h.sidecar.busy, 0)                       # the summary request itself was dropped
                self.assertEqual(h.store.chapters('a'), [])               # nothing partial
                await h.settle()
                h.state['candidate'] = None                               # quiet again
                h.sidecar.slow = 1.0
                h.handler._compact('browser', h.state, h.state['session'])
                await h.settle()
                self.assertEqual(len(h.store.chapters('a')), 1)           # redone
            finally:
                await h.close()
        run_virtual(scenario())

    def test_nothing_starts_while_the_user_is_speaking(self):
        async def scenario():
            h = await session()
            try:
                await h.settle()
                before = h.state.get('compact_task')
                await h.candidate('u1', h.audio('speech'), sequence=1)
                h.handler._compact('browser', h.state, h.state['session'])
                self.assertIs(h.state.get('compact_task'), before)
            finally:
                await h.close()
        run_virtual(scenario())


class OneSlot(unittest.TestCase):
    def setUp(self):
        convo_inspect.clear()

    def test_a_kept_chapter_is_swapped_in_when_the_reply_text_is_complete_and_warmed_while_it_is_spoken(self):
        async def scenario():
            h = await session(server={'kind': 'llama.cpp', 'slots': 1, 'slot': 0}, replied=True, tts_delay=2.0,
                              model_deltas=[('The first sentence of this reply is long enough to be spoken now. ', .2),
                                            ('The second sentence closes the reply.', .2)])
            try:
                h.handler._compact('browser', h.state, h.state['session'])
                await h.settle()
                self.assertEqual(h.store.chapters('a'), [])               # written and kept: not swapped in yet
                self.assertIn('a', compactor(h).ready)
                await h.candidate('t1', h.audio('ask'), sequence=1, final=True)
                await h.settle()
                self.assertEqual(len(h.store.chapters('a')), 1)
                self.assertEqual(len(h.sidecar.warms), 1)
                warmed_at, chapters_at_warm = h.sidecar.warms[0]
                self.assertEqual(chapters_at_warm, 1)                     # swapped first, then the new start warmed
                self.assertLess(warmed_at - h.t0, h.spoken[-1][0] - h.t0) # while the reply was still being spoken
                warm = next(p for p in h.answers if p.get('warm'))
                self.assertEqual((warm['slot'], warm['max_tokens']), (0, 64))
                self.assertEqual(h.sidecar.overlaps, [])
            finally:
                await h.close()
        run_virtual(scenario())
        kinds = [item['kind'] for item in convo_inspect.compactions('a')]
        for kind in ('written', 'swap', 'warm'):
            self.assertIn(kind, kinds)

    def test_the_warm_up_stops_when_the_user_speaks(self):
        async def scenario():
            h = await session(server={'kind': 'llama.cpp', 'slots': 1, 'slot': 0}, replied=True, slow=.5, warm_delay=5.0)
            try:
                h.handler._compact('browser', h.state, h.state['session'])
                await h.settle()
                h.handler._swap_and_warm('browser', h.state, h.state['session'])
                warm = h.state['warm_task']
                await h.advance(1)
                await h.candidate('u1', h.audio('speech'), sequence=1)
                await asyncio.sleep(0)
                await h.settle()
                self.assertTrue(warm.cancelled())
                self.assertEqual(len(h.store.chapters('a')), 1)           # the swap itself stands
            finally:
                await h.close()
        run_virtual(scenario())
        warms = [(item['ok'], item['reason']) for item in convo_inspect.compactions('a') if item['kind'] == 'warm']
        self.assertEqual(warms, [(False, 'stopped')])


class SpareSlots(unittest.TestCase):
    def test_the_new_start_is_warmed_in_a_spare_slot_and_swapped_in_only_once_warm(self):
        # These count the model's requests: the separate reading of what a reply left open (left_open) is not part of them.
        self.enterContext(__import__('unittest.mock').mock.patch('plugins._convo.helpers.convo_voice_tools.left_open', new=__import__('unittest.mock').mock.AsyncMock(return_value='')))
        async def scenario():
            h = await session(server={'kind': 'llama.cpp', 'slots': 2, 'slot': 0}, replied=True, warm_delay=.5)
            try:
                h.handler._compact('browser', h.state, h.state['session'])
                await h.settle()
                self.assertEqual(len(h.sidecar.warms), 1)
                self.assertEqual(h.sidecar.warms[0][1], 0)                # warmed before the swap
                self.assertEqual(len(h.store.chapters('a')), 1)           # then swapped in
                warm = next(p for p in h.answers if p.get('warm'))
                self.assertEqual(warm['slot'], 1)                         # in the spare slot
                self.assertEqual(h.state['server']['slot'], 1)            # where the conversation now continues
                after = h.handler._upkeep_request(h.state, h.store.context('a'), 'x')
                self.assertEqual(app.split_system(warm['system'])[0], app.split_system(after['system'])[0])   # the start it now uses
                self.assertEqual(warm['history'], after['history'])
                await h.candidate('t1', h.audio('ask'), sequence=1, final=True)
                await h.settle()
                self.assertEqual(h.sidecar.requests[-1].get('slot'), 1)   # the next reply runs on the warm slot
            finally:
                await h.close()
        run_virtual(scenario())

    def test_speech_during_the_spare_slot_warm_up_keeps_the_chapter_until_the_next_quiet_moment(self):
        async def scenario():
            h = await session(server={'kind': 'llama.cpp', 'slots': 2, 'slot': 0}, replied=True, slow=.5, warm_delay=5.0)
            try:
                h.handler._compact('browser', h.state, h.state['session'])
                await h.advance(2)
                self.assertEqual(len(h.sidecar.warms), 1)
                await h.candidate('u1', h.audio('speech'), sequence=1)
                await h.settle()
                self.assertEqual(h.store.chapters('a'), [])
                self.assertIn('a', compactor(h).ready)
                h.state['candidate'] = None
                h.sidecar.warm_delay = .1
                h.handler._compact('browser', h.state, h.state['session'])
                await h.settle()
                self.assertEqual(len(h.store.chapters('a')), 1)
                self.assertEqual(h.state['server']['slot'], 1)
            finally:
                await h.close()
        run_virtual(scenario())


class Remote(unittest.TestCase):
    def setUp(self):
        convo_inspect.clear()

    def test_a_cloud_provider_gets_no_warm_up_and_no_slot_pin(self):
        async def scenario():
            h = await session(config={'roles': {'conversation': {'url': 'https://openrouter.ai/api/v1', 'remote': True, 'model': 'some/model'}}})
            asked = []
            try:
                async def slots(role):
                    asked.append(role)
                    return {'kind': 'llama.cpp', 'slots': 4}
                h.sidecar.slots = slots
                await h.handler._discover_slots(h.state, h.state['session'])
                self.assertEqual(asked, [])                               # never asked about slots
                self.assertEqual(h.state['server']['kind'], 'remote')
                h.state['replied'] = True
                h.handler._compact('browser', h.state, h.state['session'])
                await h.settle()
                self.assertEqual(len(h.store.chapters('a')), 1)           # swapped in at once
                await h.candidate('t1', h.audio('ask'), sequence=1, final=True)
                await h.settle()
                self.assertEqual(h.sidecar.warms, [])
                self.assertTrue(h.sidecar.requests)
                self.assertFalse(any('slot' in p or p.get('warm') for p in h.answers + h.sidecar.requests))
            finally:
                await h.close()
        run_virtual(scenario())

    def test_the_gateway_pins_and_shortens_only_for_your_own_server(self):
        self.assertEqual(app.pin_slot({'max_tokens': 64}, {'slot': 1, 'warm': True}, 'http://host.docker.internal:8080/v1'),
                         {'max_tokens': 1, 'id_slot': 1})
        self.assertEqual(app.pin_slot({'max_tokens': 64}, {'slot': 1, 'warm': True}, 'https://openrouter.ai/api/v1'), {'max_tokens': 64})
        self.assertEqual(app.pin_slot({}, {'slot': 'one'}, 'http://localhost:8080/v1'), {})
        self.assertEqual(app.pin_slot({}, {'slot': 999}, 'http://localhost:8080/v1'), {})
        self.assertEqual(app.pin_slot({}, {'slot': True}, 'http://localhost:8080/v1'), {})

    def test_slot_discovery(self):
        role = {'url': 'http://localhost:8080/v1', 'model': 'm'}

        class Response:
            def __init__(self, status, data): self.status_code, self.data = status, data
            def json(self): return self.data

        def client_for(answers, asked):
            class Client:
                async def __aenter__(self): return self
                async def __aexit__(self, *exc): return False
                async def get(self, url, headers=None):
                    asked.append(url)
                    return answers.get(url.rsplit('/', 1)[1], Response(404, {}))
            return lambda timeout: Client()

        for answers, expected in (({'props': Response(200, {'total_slots': 4})}, {'kind': 'llama.cpp', 'slots': 4}),
                                  ({'slots': Response(200, [{'id': 0}, {'id': 1}])}, {'kind': 'llama.cpp', 'slots': 2}),
                                  ({'props': Response(200, {'model': 'x'})}, {'kind': 'unknown', 'slots': 1}),
                                  ({}, {'kind': 'unknown', 'slots': 1})):
            asked = []
            with self.subTest(expected=expected), patch.object(app.convo_net, 'client', client_for(answers, asked)):
                self.assertEqual(asyncio.run(app.slots({'role': role})), expected)
                self.assertTrue(asked and all(url in ('http://localhost:8080/props', 'http://localhost:8080/slots') for url in asked))
        with patch.object(app.convo_net, 'client', side_effect=AssertionError('a cloud provider is never asked')):
            self.assertEqual(asyncio.run(app.slots({'role': {'url': 'https://openrouter.ai/api/v1', 'model': 'm', 'remote': True}})),
                             {'kind': 'remote', 'slots': 0})

    def test_an_older_gateway_or_unknown_server_means_one_slot_unpinned(self):
        async def scenario():
            h = await session()
            try:
                await h.handler._discover_slots(h.state, h.state['session'])
                self.assertEqual(h.state['server'], {'kind': 'unknown', 'slots': 1, 'slot': 0})
                self.assertEqual(h.handler._pinned(h.state, {'x': 1}), {'x': 1})
            finally:
                await h.close()
        run_virtual(scenario())
        self.assertIn('slots_unknown', [item['reason'] for item in convo_inspect.compactions('a') if item['kind'] == 'fallback'])

    def test_a_discovered_llama_cpp_server_pins_every_conversation_request(self):
        async def scenario():
            h = await session()
            try:
                async def slots(role):
                    return {'kind': 'llama.cpp', 'slots': 1}
                h.sidecar.slots = slots
                await h.handler._discover_slots(h.state, h.state['session'])
                await h.candidate('t1', h.audio('ask'), sequence=1, final=True)
                await h.settle()
                self.assertEqual(h.sidecar.requests[-1]['slot'], 0)
            finally:
                await h.close()
        run_virtual(scenario())


class UtilityWriter(unittest.TestCase):
    CLOUD = {'provider': 'openrouter', 'name': 'free/model', 'api_base': ''}
    LOCAL = {'provider': 'other', 'name': 'qwen', 'api_base': 'http://192.168.1.5:8080/v1'}

    def setUp(self):
        convo_inspect.clear()

    def test_refused_without_consent_in_the_cloud_and_allowed_on_your_own_machines(self):
        config = validate_settings({'summary_writer': 'utility'})
        refused = convo_summarizer.choose(config, self.CLOUD)
        self.assertEqual((refused['allowed'], refused['reason']), (False, 'utility_not_consented'))
        self.assertTrue(convo_summarizer.choose(config, self.LOCAL)['allowed'])
        self.assertFalse(convo_summarizer.choose(config, {'provider': 'ollama', 'name': 'llama3'})['allowed'])    # no base resolved: not local
        self.assertTrue(convo_summarizer.choose(config, {'provider': 'ollama', 'name': 'llama3', 'base': 'http://host.docker.internal:11434'})['allowed'])
        self.assertFalse(convo_summarizer.choose(config, {'provider': 'other', 'name': 'x', 'api_base': 'https://api.example.com/v1'})['allowed'])
        self.assertFalse(convo_summarizer.choose(config, {})['allowed'])                                         # unknown: refused
        consented = validate_settings({'summary_writer': 'utility', 'utility_summary_consent': True})
        self.assertTrue(convo_summarizer.choose(consented, self.CLOUD)['allowed'])
        default = convo_summarizer.choose(validate_settings({}), self.CLOUD)
        self.assertEqual((default['writer'], default['allowed']), ('conversation', True))
        self.assertIn('conversation model', default['detail'])
        row = convo_summarizer.readiness_rows(config, self.CLOUD)[0]
        self.assertEqual((row['id'], row['state']), ('summary_writer', 'warning'))
        self.assertIn('free/model', row['detail'])                       # the card says which model

    def test_the_utility_models_real_address_is_resolved_as_agent_zero_does(self):
        from plugins._convo.helpers.convo_host import resolve_base
        local, cloud = 'http://192.168.1.5:8080/v1', 'https://api.example.com/v1'
        # The model's own kwargs win over its api_base (models.ModelConfig.build_kwargs).
        self.assertEqual(resolve_base({'api_base': local, 'kwargs': {'api_base': cloud}}), cloud)
        self.assertEqual(resolve_base({'api_base': local, 'kwargs': {'base_url': cloud}}), cloud)
        self.assertEqual(resolve_base({'api_base': local}), local)
        # Then the provider's kwargs (conf/model_providers.yaml), then its documented local default.
        self.assertEqual(resolve_base({}, {'kwargs': {'api_base': 'https://openrouter.ai/api/v1'}}), 'https://openrouter.ai/api/v1')
        self.assertEqual(resolve_base({}, {'models_list': {'default_base': 'http://host.docker.internal:11434'}}), 'http://host.docker.internal:11434')
        self.assertEqual(resolve_base({}, {}), '')
        config = validate_settings({'summary_writer': 'utility'})
        sneaky = {'provider': 'other', 'name': 'x', 'api_base': local, 'base': resolve_base({'api_base': local, 'kwargs': {'api_base': cloud}})}
        self.assertFalse(convo_summarizer.choose(config, sneaky)['allowed'])   # a local api_base overridden by cloud kwargs is cloud

    def test_settings_are_validated(self):
        self.assertEqual(validate_settings({})['summary_writer'], 'conversation')
        self.assertIs(validate_settings({})['utility_summary_consent'], False)
        for bad in ({'summary_writer': 'cloud'}, {'utility_summary_consent': 'yes'}):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                validate_settings(bad)

    def test_the_session_never_sends_the_conversation_to_an_unconsented_cloud_utility_model(self):
        def run(utility, config):
            calls = []

            async def scenario():
                h = await session(config={'summary_writer': 'utility', **config})
                try:
                    async def summarize(target, source):
                        calls.append(source)
                        return 'Written by the utility model.'
                    h.host.summarize = summarize
                    h.host.utility_model = lambda target='': utility
                    await h.settle()
                    h.handler._compact('browser', h.state, h.state['session'])
                    await h.settle()
                    return [c['summary'] for c in h.store.chapters('a')], [p for p in h.answers if upkeep(p)]
                finally:
                    await h.close()
            chapters, asked = run_virtual(scenario())
            return calls, chapters, asked
        calls, chapters, asked = run(self.CLOUD, {})
        self.assertEqual((calls, chapters, asked), ([], [], []))         # nothing sent, and no other model instead
        self.assertIn('utility_not_consented', [item['reason'] for item in convo_inspect.compactions('a')])
        calls, chapters, asked = run(self.LOCAL, {})
        self.assertTrue(calls)
        self.assertNotIn('covers', calls[0])
        self.assertEqual((chapters, asked), (['Written by the utility model.'], []))
        calls, chapters, _ = run(self.CLOUD, {'utility_summary_consent': True})
        self.assertTrue(calls)


class Soak(unittest.TestCase):
    """300 exchanges in each language, the user speaking at a natural pace, summaries that take 20 s and every third one failing."""

    def run_language(self, lang, exchanges=300):
        async def scenario():
            h = await Harness.create(config={**SMALL}, default=SPEAK, tts_delay=.3)
            h.sidecar = Upkeep(h, slow=20.0, fail_every=3)
            h.state['sidecar'] = h.sidecar
            sidecars.append(h.sidecar)
            try:
                waits = []
                for n in range(exchanges):
                    user, reply = SOAK[lang]
                    h.model_deltas = [('USER: %s\nLANGUAGE: %s\n' % (user.format(n=n), lang), .05), (reply.format(n=n), .3)]
                    before = len(h.sidecar.replies)
                    said = h.now()
                    await h.candidate('t%d' % n, h.audio('turn%d' % n), sequence=1, final=True)
                    await h.settle()
                    self.assertGreater(len(h.sidecar.replies), before, n)
                    waits.append(h.sidecar.replies[before] - h.t0 - said)
                    # Quiet between turns: usually a few seconds (a 20 s summary is stopped by the next turn), now and then long
                    # enough for a chapter and its standing notes.
                    await h.advance(45 if n % 7 == 6 else 4)
                    await h.settle()
                window = h.state['config']['compaction']['context_tokens']
                for request in h.sidecar.requests:                        # every reply fit the window: nothing was refused
                    self.assertLessEqual(h.handler._estimate(h.state['config'], request['system'], request['history'], request['audio'],
                                                             request.get('transcript'), h.state.get('token_ratio')), window)
                covered = h.store.memory('a')['covered']
                verbatim = [t['id'] for t in h.store.context('a')['turns']]
                return waits, covered, verbatim, len(h.store.chapters('a')), [t for t, p in h.timeline if p.get('type') == 'error']
            finally:
                await h.close()
        return run_virtual(scenario())

    def test_300_exchanges_in_english_russian_and_hindi(self):
        for lang in ('en', 'ru', 'hi'):
            with self.subTest(lang=lang):
                sidecars.clear()
                waits, covered, verbatim, chapters, errors = self.run_language(lang)
                self.assertEqual(len(waits), 300)                         # every turn answered: no refusal
                self.assertEqual(errors, [])
                self.assertLess(max(waits), 1.0)                          # no reply waited on a summary or a warm-up
                self.assertEqual(sidecars[0].overlaps, [])                # one model operation at a time
                self.assertEqual(sorted(covered + verbatim, key=lambda t: int(t[1:])), ['t%d' % n for n in range(300)])
                self.assertEqual(len(covered), len(set(covered)))         # every turn exactly once
                self.assertGreater(chapters, 0)                           # compaction kept up in the long pauses
                self.assertGreater(sidecars[0].summaries, chapters)       # some summaries were stopped or failed
                self.assertGreater(len(sidecars[0].warms), 0)             # swaps were warmed while replies were spoken


sidecars = []


if __name__ == '__main__':
    unittest.main()
