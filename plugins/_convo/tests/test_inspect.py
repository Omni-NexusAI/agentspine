"""Evidence first (change 1): what the conversation model saw and wrote, turn by turn (helpers/convo_inspect.py), the reply's
end-of-answer figures, the memory view, and Test connection's tool and image probe (helpers/convo_setup.py) with its record read
back by convo_tools. Memory only and offline: stand-in models and servers, temporary folders."""
import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from plugins._convo.helpers import convo_history, convo_inspect as inspect, convo_metrics, convo_policy, convo_tools
from plugins._convo.helpers.convo_contract import validate_settings
from plugins._convo.helpers.convo_host import Compactor
from plugins._convo.helpers.convo_store import Store

if __package__:
    from .setup_harness import SetupHarness
else:
    from setup_harness import SetupHarness

CLIP = 'UklGRg' + 'A' * 6400            # stands for a base64 WAV (never decoded)


async def scripted(items):
    for item in items:
        yield item


def request(**extra):
    return {'role': {'url': 'http://127.0.0.1:8080/v1', 'model': 'm', 'token_env': 'SECRET_NAME', 'capabilities': {'audio_input': True}},
            'system': 'rules' + convo_history.LATE_MARK + 'clock line', 'history': [{'transcript': 'Hi', 'assistant': 'Hello.'}],
            'audio': CLIP, 'transcript': None, 'allow_tools': True, 'tools': ['web_search'], 'tool_mode': 'text', 'max_tokens': 64, **extra}


class Ring(unittest.TestCase):
    def setUp(self):
        inspect.clear()
        self.addCleanup(inspect.clear)

    def test_only_the_last_ten_turns_are_kept(self):
        for index in range(13):
            inspect.begin('t%d' % index, request())
        kept = [item['turn'] for item in inspect.turns()]
        self.assertEqual(len(kept), inspect.RING)
        self.assertEqual(kept[0], 't12')                  # newest first
        self.assertNotIn('t2', kept)

    def test_every_request_of_a_turn_is_kept_with_its_kind(self):
        inspect.begin('t', request(), early=True)
        inspect.begin('t', request())
        inspect.begin('t', request(tool_turn={'id': 'c', 'name': 'web_search', 'arguments': {'query': 'q'}, 'result': 'r', 'image': 'data:x'}))
        passes = inspect.turns()[0]['passes']
        self.assertEqual([item['kind'] for item in passes], ['early', 'again', 'tool follow-up'])
        self.assertTrue(passes[2]['request']['tool_turn']['image'])          # said, never shown

    def test_the_view_never_carries_audio_and_marks_the_per_reply_part(self):
        inspect.begin('t', request(context_audio=[CLIP]))
        view = inspect.turns()[0]
        shown = json.dumps(view)
        self.assertNotIn(CLIP[:200], shown)
        self.assertEqual(view['audio_s'], convo_history.audio_seconds(CLIP))
        seen = view['passes'][0]['request']
        self.assertIn(inspect.LATE_SHOWN, seen['system'])
        self.assertEqual(seen['system_late'], 'clock line')
        self.assertEqual((seen['tools'], seen['tool_mode'], seen['context_clips']), (['web_search'], 'text', 1))
        self.assertIn('[audio', seen['messages'][-1]['text'])

    def test_raw_output_is_kept_before_any_filter_and_each_removal_is_recorded(self):
        async def run():
            entry = inspect.begin('t', request())
            raw = ['USER: what time is it\nLANGUAGE: English\n', 'Let me check. <tool_call>{"name": "web_search", ',
                   '"arguments": {"query": "time"}}</tool_call>']
            async for _ in inspect.tap(entry, scripted([{'type': 'text.delta', 'text': text} for text in raw]
                                                       + [{'type': 'answer.done', 'finish_reason': 'stop', 'tool_calls': 0, 'reasoning_chars': 7}])):
                pass
            inspect.filters('t', header=types.SimpleNamespace(line='what time is it', language='English'),
                            guard=types.SimpleNamespace(captured='<tool_call>...'), control=None)
            inspect.stage('t', 'header', 'Let me check. <tool_call>{"name": "web_search", "arguments": {"query": "time"}}</tool_call>')
            inspect.stage('t', 'markup', 'Let me check. ')
            inspect.stage('t', 'control', 'Let me check. ')
        asyncio.run(run())
        seen = inspect.turns()[0]['passes'][0]
        self.assertTrue(seen['raw'].startswith('USER: what time is it\nLANGUAGE: English\n'))
        self.assertEqual(seen['removed']['header'], 'USER: what time is it\nLANGUAGE: English\n')
        self.assertIn('<tool_call>', seen['removed']['markup'])
        self.assertEqual(seen['removed']['control'], '')
        self.assertEqual((seen['filters']['user_line'], seen['filters']['language']), ('what time is it', 'English'))
        self.assertEqual(seen['done'], {'finish_reason': 'stop', 'tool_calls': 0, 'reasoning_chars': 7})
        self.assertEqual(seen['after'], 'Let me check. ')

    def test_a_reply_never_read_through_the_filters_claims_no_removals(self):
        inspect.begin('t', request(), early=True)
        seen = inspect.turns()[0]['passes'][0]
        self.assertEqual((seen['filtered'], seen['removed'], seen['after']), (False, {}, None))

    def test_removed_reads_deletions_anywhere(self):
        self.assertEqual(inspect.removed('[laughs] Sure.', 'Sure.'), '[laughs] ')
        self.assertEqual(inspect.removed('Hi <x> there', 'Hi there').strip(), '<x>')
        self.assertEqual(inspect.removed('same', 'same'), '')


class SaveForReplay(unittest.TestCase):
    def setUp(self):
        inspect.clear()
        self.addCleanup(inspect.clear)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'data'

    def test_nothing_is_written_until_save_and_a_saved_turn_can_be_deleted(self):
        inspect.begin('t', request(tool_turn={'id': 'c', 'name': 'camera_context', 'arguments': {}, 'result': 'r', 'image': 'data:image/jpeg;base64,AAAA'}))
        inspect.spoken('t', 'Hello there.')
        inspect.whisper('t', 'hi')
        self.assertFalse(self.data.exists())
        self.assertEqual(inspect.saved(self.data), [])
        ident = inspect.save(self.data, 't')
        listed = inspect.saved(self.data)
        self.assertEqual([item['id'] for item in listed], [ident])
        document = json.loads((self.data / 'replay' / (ident + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(document['format'], inspect.FORMAT)
        saved = document['passes'][0]['request']
        self.assertEqual(saved['audio'], CLIP)                                # the audio leaves memory only here
        self.assertEqual((saved['system_stable'], saved['system_late']), ('rules', 'clock line'))
        self.assertNotIn('token_env', saved['role'])
        self.assertNotIn('image', saved['tool_turn'])
        self.assertTrue(saved['tool_turn']['image_omitted'])
        self.assertEqual((document['spoken'], document['whisper']), (['Hello there.'], 'hi'))
        self.assertTrue(inspect.delete(self.data, ident))
        self.assertEqual(inspect.saved(self.data), [])
        self.assertFalse(inspect.delete(self.data, ident))
        with self.assertRaises(ValueError):
            inspect.delete(self.data, '../config')

    def test_a_turn_no_longer_kept_cannot_be_saved(self):
        with self.assertRaises(ValueError):
            inspect.save(self.data, 'gone')
        self.assertFalse(self.data.exists())


class MemoryView(unittest.TestCase):
    def setUp(self):
        inspect.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'db')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(inspect.clear)

    def test_chapters_with_their_turns_time_and_size_standing_notes_and_compactions(self):
        session = self.store.start('o', 'a')
        for index in range(8):
            self.store.accept_turn(session['id'], 'o', session['epoch'], 't%d' % index, {'transcript': 'Words %d' % index})
            self.store.settle(session['id'], 'o', session['epoch'], 't%d' % index, 'Reply %d' % index)
        snapshot = self.store.snapshot(session['id'], 'o', 2, 4)
        self.assertTrue(self.store.splice(snapshot, 'Chapter one: the user said words.', 'Name: Sam.'))
        inspect.compaction('a', {'ok': True, 'changed': True})
        inspect.compaction('b', {'ok': False, 'reason': 'stopped'})
        view = inspect.memory_view(self.store, 'a', {'context_tokens': 24000})
        self.assertEqual(view['window'], 24000)
        self.assertEqual(len(view['chapters']), 1)
        chapter = view['chapters'][0]
        self.assertEqual((chapter['turns'], chapter['first'], chapter['last']), (4, 't0', 't3'))
        self.assertGreater(chapter['tokens'], 0)
        self.assertIsInstance(chapter['time'], float)
        self.assertEqual(view['standing'], 'Name: Sam.')
        self.assertEqual(view['verbatim_turns'], 4)
        self.assertEqual([item['changed'] for item in view['compactions']], [True])          # this conversation's only


class Live(unittest.IsolatedAsyncioTestCase):
    """The real turn engine in developer mode with a scripted conversation model."""
    async def asyncSetUp(self):
        inspect.clear()
        self.tmp = tempfile.TemporaryDirectory(); self.store = Store(Path(self.tmp.name) / 'db')
        host = types.SimpleNamespace(context=lambda target: None, freeze_speech=lambda role: role, local_time=lambda: '',
                                     persona=AsyncMock(return_value='Calm'), summarize=AsyncMock(return_value=''),
                                     direct=AsyncMock(return_value='Search result'), chat_name=lambda target: '')
        self.service = types.SimpleNamespace(store=self.store, host=host, persona_cache={}, dispatcher=types.SimpleNamespace(start=lambda: None),
                                             compactor=Compactor(self.store, host))
        self.config = validate_settings({'enabled': True, 'developer_mode': True, 'sidecar': {'managed': False},
                                         'roles': {'classifier': {'url': 'http://localhost/v1', 'model': 'c'}, 'conversation': {'model': 'm'}}})
        answers = self.answers = []

        class Sidecar:
            def __init__(self, config): self.spoken = []
            async def health(self): return {'service': 'convo', 'protocol': 1}
            async def policy(self, payload): return {'speech': 'speak', 'action': 'none', 'confidence': .99, 'explicit_request': True}
            async def answer(self, payload):
                for item in (answers.pop(0) if answers else [{'type': 'answer.done'}]):
                    yield item
            async def speech(self, role, text): self.spoken.append(text); return b'wave'

        stub = types.ModuleType('helpers.ws')
        stub.WsHandler = type('WsStub', (), {'__init__': lambda self, *a, **k: None, 'emit_to': AsyncMock()})
        with patch.dict(sys.modules, {'helpers.ws': stub}):
            self.module = importlib.import_module('plugins._convo.api.realtime')
        self.patch = patch.object(self.module, 'modules', return_value=(types.SimpleNamespace(
            __package__='plugins._convo.helpers', settings=lambda: self.config, service=lambda: self.service), convo_policy,
            types.SimpleNamespace(Sidecar=Sidecar)))
        self.patch.start()
        models = patch.object(importlib.import_module('plugins._convo.helpers.convo_models'), 'state', new=AsyncMock(return_value=''))
        models.start(); self.addCleanup(models.stop)
        self.handler = self.module.Realtime()
        self.session = (await self.handler.process('convo.start', {'target': 'a'}, 'browser'))['session']
        self.ids = {'session_id': self.session['id'], 'epoch': self.session['epoch']}

    async def asyncTearDown(self):
        await self.handler.on_disconnect('browser'); self.patch.stop(); self.store.close(); self.tmp.cleanup(); inspect.clear()

    async def turn(self, name, *passes):
        self.answers[:] = [list(items) for items in passes]
        await self.handler.process('convo.turn', {**self.ids, 'turn_id': name, 'transcript': 'Question'}, 'browser')
        await self.handler.connections['browser']['task']
        return next(item for item in inspect.turns() if item['turn'] == name)

    @staticmethod
    def deltas(*texts, **done):
        return [{'type': 'text.delta', 'text': text} for text in texts] + [{'type': 'answer.done', **done}]

    async def test_a_control_word_and_its_header_are_recorded_as_taken_out(self):
        seen = await self.turn('quiet', self.deltas('USER: um\nLANGUAGE: English\n', '[SILENT] background noise', finish_reason='stop'))
        first = seen['passes'][0]
        self.assertEqual(first['kind'], 'first')
        self.assertIn('[SILENT]', first['raw'])
        self.assertIn('USER: um', first['removed']['header'])
        self.assertIn('[SILENT] background noise', first['removed']['control'])
        self.assertEqual((first['filters']['control'], first['filters']['control_reason']), ('silent', 'background noise'))
        self.assertEqual(first['after'], '')
        self.assertEqual(seen['spoken'], [])

    async def test_a_written_tool_call_is_recorded_as_markup_then_its_follow_up_and_what_was_spoken(self):
        call = '<tool_call>{"name": "web_search", "arguments": {"query": "weather"}}</tool_call>'
        seen = await self.turn('tool', self.deltas('USER: weather?\nLANGUAGE: English\n', 'Let me look that up. ', call, finish_reason='stop'),
                               self.deltas('It is sunny in the results.', finish_reason='stop', reasoning_chars=4))
        first, follow = seen['passes']
        self.assertIn(call, first['raw'])
        self.assertIn('<tool_call>', first['removed']['markup'])
        self.assertEqual(follow['kind'], 'tool follow-up')
        self.assertEqual(follow['request']['tool_turn']['name'], 'web_search')
        self.assertEqual(seen['tools'][0]['name'], 'web_search')
        self.assertIn('It is sunny in the results.', ' '.join(seen['spoken']))
        done = [mark for record in convo_metrics.LOG.records(200) if record['turn'] == 'tool' for mark in record['marks'] if mark['name'] == 'reply.done']
        self.assertEqual((done[-1]['finish_reason'], done[-1]['passes'], done[-1]['reasoning_chars']), ('stop', 2, 4))

    async def test_whispers_text_joins_its_turn(self):
        await self.turn('w', self.deltas('USER: hi\nLANGUAGE: English\n', 'Hello.'))
        await self.handler.process('convo.transcript', {**self.ids, 'turn_id': 'w', 'text': 'hi there'}, 'browser')
        self.assertEqual(next(item for item in inspect.turns() if item['turn'] == 'w')['whisper'], 'hi there')

    async def test_nothing_is_kept_outside_developer_mode(self):
        self.config['developer_mode'] = False                  # the saved setting decides, not the session's copy
        self.answers[:] = [self.deltas('Hello.')]
        await self.handler.process('convo.turn', {**self.ids, 'turn_id': 'off', 'transcript': 'Question'}, 'browser')
        await self.handler.connections['browser']['task']
        self.assertEqual(inspect.turns(), [])

    async def test_capture_is_forgotten_when_developer_mode_goes_off_the_session_ends_or_convo_is_turned_off(self):
        """Review 2026-11-17 #9: capture followed the session's copied settings and was never cleared."""
        await self.turn('kept', self.deltas('USER: hi\nLANGUAGE: English\n', 'Hello.'))
        self.assertTrue(inspect.turns())
        self.config['developer_mode'] = False                  # turned off mid-session: the next capture point forgets it all
        self.answers[:] = [self.deltas('Hello again.')]
        await self.handler.process('convo.turn', {**self.ids, 'turn_id': 'after', 'transcript': 'Question'}, 'browser')
        await self.handler.connections['browser']['task']
        self.assertEqual(inspect.turns(), [])
        # Session end: the conversation's turns go; compaction runs (no conversation text) stay for the memory view.
        self.config['developer_mode'] = True
        await self.turn('again', self.deltas('USER: hi\nLANGUAGE: English\n', 'Hello.'))
        inspect.compaction('a', {'ok': True, 'kind': 'warm'})
        await self.handler.process('convo.stop', {'session_id': self.session['id']}, 'browser')
        self.assertEqual(inspect.turns(), [])
        self.assertEqual(len(inspect.compactions('a')), 1)
        # Plugin OFF forgets everything.
        from plugins._convo.helpers import convo_service
        inspect.begin('t', request())
        service = object.__new__(convo_service.Service)
        service.connection_lock, service.connections, service.disabled = __import__('threading').RLock(), {}, False
        service.dispatcher = types.SimpleNamespace(pause=lambda: None)
        service.store = self.store
        service.deactivate()
        self.assertEqual((inspect.turns(), inspect.compactions()), ([], []))

    async def test_a_measured_native_record_sends_tools_natively(self):
        state = self.handler.connections['browser']
        state['config']['roles']['conversation']['capabilities']['tool_calls'] = False        # nothing declared: the measurement decides
        state['measured'] = {'tool_mode': 'native'}
        self.assertEqual(self.handler._tool_offer(state, state['config'], False)[1], 'native')
        state['measured'] = {'tool_mode': 'ignored'}
        self.assertEqual(self.handler._tool_offer(state, state['config'], False)[1], 'text')


class Probe(SetupHarness, unittest.IsolatedAsyncioTestCase):
    """Test connection on the conversation card measures tools and images against a stand-in server."""
    def setUp(self):
        super().setUp()
        self.http.side_effect = None
        self.http.return_value = {'data': [{'id': 'local-model'}]}
        self.tools_module = importlib.import_module(self.setup.__package__ + '.convo_tools')

    def answers(self, tool, image):
        self.post.side_effect = [tool, image]

    @staticmethod
    def reply(content='', calls=None):
        return 200, {'choices': [{'message': {'content': content, **({'tool_calls': calls} if calls else {})}}]}

    async def probe(self):
        return (await self.setup.discover('conversation', check=True))['capabilities']

    async def test_native_calls_and_an_image_seen_are_kept_and_read_back(self):
        self.answers(self.reply(calls=[{'id': 'c', 'type': 'function', 'function': {'name': 'get_time', 'arguments': '{"city": "Tokyo"}'}}]),
                     self.reply('Red'))
        found = await self.probe()
        self.assertEqual((found['tool_mode'], found['image_input']), ('native', True))
        body = self.post.await_args_list[0].args[1]
        self.assertEqual((body['temperature'], body['tool_choice'], len(body['tools'])), (0, 'required', 1))
        self.assertEqual(self.post.await_args_list[1].args[1]['messages'][0]['content'][1]['type'], 'image_url')
        role = self.setup.read()['config']['roles']['conversation']
        record = self.tools_module.measured(self.path.parent / 'data', role)
        self.assertEqual((record['tool_mode'], record['image_input']), ('native', True))
        self.assertEqual(self.tools_module.tool_mode(role.get('capabilities'), record), 'native')
        self.assertTrue(self.tools_module.accepts_images(record))
        row = next(item for item in self.setup.read()['readiness'] if item['id'] == 'conversation_measured')
        self.assertIn('Calls tools natively', row['detail'])
        self.assertNotIn('tool_mode', self.store.read(self.path)['convo']['roles']['conversation'].get('capabilities', {}))   # never in settings

    async def test_a_call_written_as_text_and_an_image_refused(self):
        self.answers(self.reply('<tool_call>{"name": "get_time", "arguments": {"city": "Tokyo"}}</tool_call>'), (400, None))
        found = await self.probe()
        self.assertEqual((found['tool_mode'], found['image_input']), ('text', False))
        self.assertIn('writes tool calls as text', found['message'].lower())
        self.assertIn('Native tools are on anyway', found['message'])          # the harness's model declares tool calls
        self.assertIn('does not accept images', found['message'])

    async def test_tools_ignored_and_an_image_not_seen(self):
        self.answers(self.reply('It is about noon in Tokyo.'), self.reply('I cannot see any picture.'))
        found = await self.probe()
        self.assertEqual((found['tool_mode'], found['image_input']), ('ignored', False))
        role = self.setup.read()['config']['roles']['conversation']
        record = self.tools_module.measured(self.path.parent / 'data', role)
        self.assertEqual(self.tools_module.tool_mode({'tool_calls': False}, record), 'text')
        self.assertEqual(self.tools_module.tool_mode({'tool_calls': True}, record), 'native')      # the declared override

    async def test_a_server_refusing_a_forced_choice_is_asked_once_with_auto(self):
        self.post.side_effect = [(400, None), self.reply(calls=[{'function': {'name': 'get_time'}}]), self.reply('red.')]
        found = await self.probe()
        self.assertEqual(found['tool_mode'], 'native')
        self.assertEqual(self.post.await_args_list[1].args[1]['tool_choice'], 'auto')

    async def test_unreachable_measures_nothing_and_keeps_nothing(self):
        found = await self.probe()                     # the harness's POST never connects
        self.assertEqual(found['state'], 'unavailable')
        self.assertFalse((self.path.parent / 'data' / 'model_capabilities.json').exists())

    async def test_only_test_connection_probes_and_never_echoes_the_model(self):
        result = await self.setup.discover('conversation')
        self.assertNotIn('capabilities', result)
        self.post.assert_not_awaited()
        self.answers(self.reply('SECRET-MODEL-TEXT'), self.reply('SECRET-MODEL-TEXT'))
        self.assertNotIn('SECRET-MODEL-TEXT', json.dumps(await self.probe()))

    def test_the_probe_picture_is_a_valid_png(self):
        self.assertTrue(convo_tools.snapshot(self.setup.PROBE_IMAGE).startswith('data:image/png;base64,'))

    async def test_an_earlier_record_with_other_fields_is_extended_not_replaced(self):
        data = self.path.parent / 'data'
        data.mkdir(parents=True, exist_ok=True)
        (data / 'model_capabilities.json').write_text(json.dumps({'models': [
            {'url': 'http://127.0.0.1:8080/v1', 'model': 'local-model', 'image_input': True, 'note': 'kept'},
            {'url': 'http://other/v1', 'model': 'x', 'image_input': False}]}), encoding='utf-8')
        self.answers(self.reply(calls=[{'function': {'name': 'get_time'}}]), (503, None))
        found = await self.probe()
        self.assertNotIn('image_input', found)                                  # 503 says nothing about images
        models = json.loads((data / 'model_capabilities.json').read_text(encoding='utf-8'))['models']
        mine = next(item for item in models if item['model'] == 'local-model')
        self.assertEqual((mine['tool_mode'], mine['image_input'], mine['note']), ('native', True, 'kept'))
        self.assertEqual(len(models), 2)


if __name__ == '__main__':
    unittest.main()
