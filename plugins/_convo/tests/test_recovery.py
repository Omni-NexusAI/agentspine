"""What a live conversation showed on 2026-11-24, as rules: a speech request with no confirmed end never blocks for good; a task's
result is spoken in short requests, offered again when nothing of it was heard, and known to the model whether or not it was spoken;
a finished task leaves no open line in its chat; a thought the model waited on is not lost; every model server a conversation needs
is told apart as loading, not answering or slow; a model's own call markup never reaches a tool; the assistant's own voice heard back
is not answered; a chapter on a cloud provider is written beside the conversation."""
import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

if __package__:
    from .behavior_harness import SPEAK_PLAIN, Harness, WsStub, run_virtual, script, typed_decision
else:
    from behavior_harness import SPEAK_PLAIN, Harness, WsStub, run_virtual, script, typed_decision

admission = importlib.import_module('plugins._convo.sidecar.speech_admission')
stream = importlib.import_module('plugins._convo.sidecar.speech_stream')
models = importlib.import_module('plugins._convo.helpers.convo_models')
tools = importlib.import_module('plugins._convo.helpers.convo_tools')
history = importlib.import_module('plugins._convo.helpers.convo_history')
host = importlib.import_module('plugins._convo.helpers.convo_host')
transport = importlib.import_module('plugins._convo.helpers.convo_transport')
_ws = types.ModuleType('helpers.ws')
_ws.WsHandler = WsStub
with patch.dict(sys.modules, {'helpers.ws': _ws}):
    realtime = importlib.import_module('plugins._convo.api.realtime')


class SpeechLock(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, {'CONVO_DATA_DIR': str(Path(self.folder.name).resolve())})
        env.start()
        self.addCleanup(env.stop)
        self.path = Path(self.folder.name).resolve() / 'speech-admission.json'

    async def fail(self, error, outcome=None):
        async def work():
            operation.outcome = outcome
            raise error
        operation = admission.start(work)
        with self.assertRaises(admission.SpeechAdmissionError) as raised:
            await operation.wait()
        return raised.exception.detail

    async def until_free(self, seconds):
        began = time.monotonic()
        while admission.status()['blocked'] and time.monotonic() - began < seconds:
            await asyncio.sleep(.02)
        return not admission.status()['blocked']

    async def test_a_stream_the_server_ended_is_released_after_a_moment(self):
        with patch.dict(admission.GRACE, {'transport_failure': .2}):
            detail = await self.fail(httpx.ReadError('gone'))
            self.assertTrue(admission.status()['blocked'])
            self.assertGreaterEqual(detail['clears_in_ms'], 100)
            self.assertTrue(await self.until_free(2))
        self.assertFalse(self.path.exists())

    async def test_the_servers_own_word_releases_it_at_once(self):
        asked = []
        async def outcome():
            asked.append(1)
            return 'limited'
        await self.fail(TimeoutError(), outcome)                     # would be held 20 s without it
        self.assertTrue(await self.until_free(2))
        self.assertTrue(asked)

    async def test_a_request_still_running_at_the_server_keeps_it(self):
        async def outcome():
            return 'producing'
        with patch.object(admission, 'GRACE_UNKNOWN', 5), patch.object(admission, 'OUTCOME_POLL', .05):
            await self.fail(TimeoutError(), outcome)
            await asyncio.sleep(.3)
            self.assertTrue(admission.status()['blocked'])

    async def test_a_record_left_by_a_process_that_is_gone_is_released_when_it_is_old(self):
        self.path.write_text(json.dumps({'version': 1, 'operation_id': '80aa6f4d-4a7c-437e-ba4d-62317eec79fc', 'started_at': time.time()}))
        self.assertFalse(admission.expire())
        self.assertTrue(admission.status()['blocked'])
        old = time.time() - admission.RECORD_STALE - 1
        os.utime(self.path, (old, old))
        self.assertTrue(admission.expire())
        self.assertFalse(admission.status()['blocked'])
        self.path.write_bytes(b'{broken')
        os.utime(self.path, (old, old))
        operation = admission.start(AsyncMock(return_value=b'audio'))         # the next request releases an old unreadable one itself
        self.assertEqual(await operation.wait(), b'audio')

    async def test_a_running_request_is_never_expired(self):
        release = asyncio.Event()
        operation = admission.start(release.wait)
        os.utime(self.path, (1, 1))
        self.assertFalse(admission.expire())
        release.set()
        await operation.wait()

    def test_the_servers_audio_limit_is_a_definite_end(self):
        body = json.dumps({'detail': {'state': 'generation-limit', 'reason': 'max_generated_frames'}}).encode()
        with self.assertRaises(admission.DefinitiveRejection):
            admission.raise_for_provider(502, body)

    async def test_the_outcome_is_asked_only_of_a_server_that_named_the_request(self):
        prepared = httpx.Request('POST', 'http://127.0.0.1:8890/v1/audio/speech', headers={'Authorization': 'Bearer k'})
        self.assertIsNone(stream.outcome_check(prepared, {}))
        self.assertIsNone(stream.outcome_check(prepared, {'x-tts-request-id': '../../control'}))
        seen, answers = [], iter([httpx.Response(200, json={'state': 'limited'}), httpx.Response(404, json={'detail': 'Synthesis outcome is absent or expired'}),
                                  httpx.Response(404, json={'detail': 'Not Found'}), httpx.Response(200, content=b'x')])
        def handler(request):
            seen.append((str(request.url), request.headers.get('authorization')))
            return next(answers)
        check = stream.outcome_check(prepared, {'x-tts-request-id': 'a' * 32})
        real = stream.convo_net.client
        with patch.object(stream.convo_net, 'client', side_effect=lambda timeout, transport=None: real(timeout, httpx.MockTransport(handler))):
            self.assertEqual([await check() for _ in range(4)], ['limited', 'completed', '', ''])
        self.assertEqual(seen[0], ('http://127.0.0.1:8890/v1/audio/outcomes/' + 'a' * 32, 'Bearer k'))

    async def test_a_lock_that_settles_is_waited_for_like_a_busy_server(self):
        busy = httpx.Response(409, json={'detail': {'state': 'uncertain', 'clears_in_ms': 2000}}, request=httpx.Request('POST', 'http://g'))
        with self.assertRaises(transport.SpeechBusy):
            await transport.speech_status(busy)
        with self.assertRaises(httpx.HTTPStatusError):
            await transport.speech_status(httpx.Response(409, json={'detail': {'state': 'uncertain'}}, request=httpx.Request('POST', 'http://g')))


class Start(unittest.IsolatedAsyncioTestCase):
    async def test_a_start_waits_for_the_lock_and_says_so(self):
        handler = realtime.Realtime.__new__(realtime.Realtime)
        handler.emit_to = AsyncMock()
        blocked = {'speech_admission': {'state': 'uncertain', 'blocked': True, 'clears_in_ms': 900}}
        sidecar = types.SimpleNamespace(health=AsyncMock(side_effect=[blocked, {'speech_admission': {'state': 'idle', 'blocked': False}}]))
        with patch.object(realtime.asyncio, 'sleep', AsyncMock()):
            await handler._speech_settled('s', {'id': 'i', 'epoch': 0}, sidecar, blocked)
        self.assertEqual([call.args[2]['kind'] for call in handler.emit_to.call_args_list], ['loading', 'ready'])
        with patch.object(realtime, 'START_SETTLE_WAIT', -1), self.assertRaises(ValueError) as raised:
            await handler._speech_settled('s', {'id': 'i', 'epoch': 0}, sidecar, blocked)
        self.assertIn('Start again in a moment', str(raised.exception))
        self.assertNotIn('reconcile', str(raised.exception))


LONG = ' '.join('The finding number %d is explained here in one plain sentence.' % n for n in range(1, 24))


class TaskResult(unittest.TestCase):
    def play(self, scenario, **options):
        options.setdefault('classifier', {'ask': script(SPEAK_PLAIN, .2)})

        async def run():
            h = await Harness.create(**options)
            try:
                return await scenario(h)
            finally:
                await h.close()
        return run_virtual(run())

    async def finished(self, h, result='The answer is 42.'):
        store, session = h.state['service'].store, h.state['session']
        turn = 'asked'
        store.accept_turn(session['id'], 'browser', session['epoch'], turn, {'input': 'text', 'transcript': 'Look into it.'})
        store.settle(session['id'], 'browser', session['epoch'], turn, '')
        job = store.submit(session['id'], 'browser', session['epoch'], turn, 'k', 'Look into it')
        for before, after in (('queued', 'dispatching'), ('dispatching', 'running'), ('running', 'completed')):
            store.transition(job['id'], before, after, result if after == 'completed' else '')
        return store, session, job

    async def notice(self, h, store, session, job):
        claimed = store.claim_notice(session['id'], 'browser', session['epoch'], job['id'])
        h.state['task'] = asyncio.ensure_future(h.handler._notice('browser', h.state, session, claimed))
        await h.settle()
        return claimed

    def test_a_long_result_is_spoken_in_short_requests(self):
        async def scenario(h):
            store, session, job = await self.finished(h)
            await self.notice(h, store, session, job)
            return [entry[-1] for entry in h.log('speech.start')]
        spoken = self.play(scenario, model_deltas=[(LONG, .1)])
        self.assertGreater(len(spoken), 2)
        self.assertTrue(all(len(text) <= 420 for text in spoken), [len(text) for text in spoken])
        self.assertEqual(' '.join(spoken), LONG)

    def test_the_result_is_known_even_when_it_cannot_be_spoken_and_is_offered_once_more(self):
        async def scenario(h):
            store, session, job = await self.finished(h, 'The build was fixed on Monday.')
            with patch.object(h.handler, '_speak', AsyncMock(side_effect=httpx.ReadError('gone'))):
                first = await self.notice(h, store, session, job)
                again = h.events('notice_retry', first['notice_turn'])
                second = await self.notice(h, store, session, job)
            seen = history.messages(h.handler._history(store.context(session['target']), h.state), audio_input=False)
            return (len(again), second is not None, len(h.events('notice_retry', second['notice_turn'])),
                    len(h.events('error', second['notice_turn'])), repr(seen), h.handler._tasks_note(h.state, session))
        again, reclaimed, third, errors, seen, note = self.play(scenario, model_deltas=[('It is fixed.', .1)])
        self.assertEqual((again, reclaimed, third, errors), (1, True, 0, 1))       # offered twice at most, then said in the panel
        self.assertIn('fixed on Monday', seen)
        self.assertIn('has finished', note)

    def test_a_running_task_is_described_as_it_is_now(self):
        async def scenario(h):
            store, session = h.state['service'].store, h.state['session']
            before = h.handler._tasks_note(h.state, session)
            store.accept_turn(session['id'], 'browser', session['epoch'], 'asked', {'input': 'text', 'transcript': 'x'})
            job = store.submit(session['id'], 'browser', session['epoch'], 'asked', 'k', 'Dig deeper')
            store.transition(job['id'], 'queued', 'dispatching')
            store.transition(job['id'], 'dispatching', 'running')
            return before, h.handler._tasks_note(h.state, session)
        before, running = self.play(scenario)
        self.assertEqual(before, '')
        self.assertIn('is still running', running)

    def test_a_finished_task_leaves_no_open_line_in_its_chat(self):
        lines = []
        adapter = host.Host.__new__(host.Host)
        chat = types.SimpleNamespace(log=types.SimpleNamespace(log=lambda **fields: lines.append(fields)))
        job = {'id': 'j', 'session': 's', 'target': 'c', 'result': 'done'}
        with patch.object(adapter, 'context', return_value=chat):
            for state in ('started', 'completed', 'failed'):
                adapter.log(job, state)
        self.assertEqual([(line['type'], line['update_progress']) for line in lines], [('info', 'none'), ('error', 'none')])


class Speaking(unittest.IsolatedAsyncioTestCase):
    def handler(self, effects):
        handler = realtime.Realtime.__new__(realtime.Realtime)
        calls = iter(effects)

        async def once(sid, state, session, turn, text, role, began):
            effect = next(calls)
            began.update(effect.get('began', {}))
            if 'error' in effect:
                raise effect['error']
            return 'spoken'
        handler._speak_once = once
        handler._current = lambda *args: True
        handler._stalled = AsyncMock()
        handler._mark = lambda *args, **fields: None
        return handler

    async def say(self, *effects):
        return await self.handler(effects)._speak('s', {}, {'epoch': 0}, 't', 'Hello there.', {})

    async def test_a_phrase_nothing_was_heard_of_is_asked_once_more(self):
        self.assertEqual(await self.say({'error': httpx.ReadError('gone')}, {}), 'spoken')
        self.assertEqual(await self.say({'error': transport.ContractError('Convo speech stream did not complete successfully')}, {}), 'spoken')

    async def test_a_phrase_that_began_or_was_refused_or_failed_twice_is_not(self):
        refused = httpx.HTTPStatusError('no', request=httpx.Request('POST', 'http://g'), response=httpx.Response(422))
        for effects in (({'error': httpx.ReadError('x'), 'began': {'audio': True}},), ({'error': refused},),
                        ({'error': transport.ContractError('Invalid speech phrase')},), ({'error': transport.SpeechBusy('running')},),
                        ({'error': httpx.ReadError('x')}, {'error': httpx.ReadError('x')})):
            with self.assertRaises(Exception):
                await self.say(*effects)


class WaitingWords(unittest.TestCase):
    def test_the_words_of_joined_fragments_survive_a_turn_whose_own_were_not_captured(self):
        context = {'turns': [
            {'id': 'a', 'content': {'transcript': 'Okay, I want to', 'superseded': 'b'}},
            {'id': 'b', 'content': {'held': 'wait', 'superseded': 'c'}},
            {'id': 'c', 'content': {'held': 'wait'}},
            {'id': 'd', 'content': {'transcript': 'know the latest release.', 'assistant': 'It is two.'}},
            {'id': 'e', 'content': {'transcript': 'Nothing for you.', 'held': 'silent'}}]}
        kept = realtime.Realtime._history(context)
        self.assertEqual([turn.get('transcript') for turn in kept], ['Okay, I want to', 'know the latest release.'])


class OwnVoice(unittest.IsolatedAsyncioTestCase):
    async def test_a_phrase_heard_back_is_recognised_and_a_quote_is_not(self):
        now = asyncio.get_running_loop().time()
        said = ['let', 'me', 'pull', 'up', 'the', 'details', 'on', 'that', 'release']
        state = {'voiced': [(now - 3, said, 'earlier')]}
        check = realtime.Realtime._own_voice
        self.assertTrue(check(state, 'now', 'Let me pull up details on that release.'))
        self.assertFalse(check(state, 'earlier', 'Let me pull up the details on that release.'))       # its own turn
        self.assertFalse(check(state, 'now', 'You said let me pull up the details on that release, so do it now please'))
        self.assertFalse(check(state, 'now', 'Yes, do that.'))
        self.assertFalse(check({'voiced': [(now - 300, said, 'earlier')]}, 'now', 'Let me pull up the details on that release.'))


class CallMarkup(unittest.TestCase):
    def test_a_models_call_markup_is_not_part_of_an_argument(self):
        for given, clean in (('Unsloth quants release</quers><parameter=query>GGUF quant release', 'GGUF quant release'),
                             ('weather in Oslo</parameter>', 'weather in Oslo'), ('is 3 < 4 in <b>HTML</b>', 'is 3 < 4 in <b>HTML</b>'),
                             ('<parameter=query>news today</parameter></function>', 'news today')):
            self.assertEqual(tools.validate('web_search', {'query': given})['query'], clean)


class Servers(unittest.IsolatedAsyncioTestCase):
    def answering(self, routes):
        posted = []

        def handler(request):
            if request.method == 'POST':
                posted.append((request.url.path, json.loads(request.content)))
                return httpx.Response(200, json={})
            answer = routes.get(request.url.path)
            if isinstance(answer, Exception):
                raise answer
            return answer if answer is not None else httpx.Response(404, json={'detail': 'Not Found'})
        real = models.convo_net.client
        return posted, patch.object(models.convo_net, 'client', side_effect=lambda timeout, transport=None: real(timeout, httpx.MockTransport(handler)))

    async def state(self, routes, kind='', url='http://127.0.0.1:8890/v1', model='m'):
        _, patched = self.answering(routes)
        with patched:
            return await models.state(url, {}, model, kind)

    async def test_every_kind_of_server_tells_loaded_from_loading_and_not_loaded(self):
        listed = httpx.Response(200, json={'data': [{'id': 'm'}]})
        studio = lambda state, loaded: {'/v1/backend/models': httpx.Response(200, json={'available': ['m', 'n'], 'loaded_models': loaded, 'state': state})}
        self.assertEqual(await self.state(studio('loaded', ['m']), 'speech'), 'loaded')
        self.assertEqual(await self.state(studio('loading', []), 'speech'), 'loading')
        self.assertEqual(await self.state(studio('unloaded', []), 'speech'), 'unloaded')
        self.assertEqual(await self.state(studio('loaded', ['n']), 'speech'), 'unloaded')            # another model is resident
        self.assertEqual(await self.state({'/v1/models': listed}, 'speech'), '')                     # a plain speech server
        self.assertEqual(await self.state({'/v1/models': httpx.Response(503)}), 'loading')
        self.assertEqual(await self.state({'/v1/models': listed, '/api/ps': httpx.Response(200, json={'models': [{'name': 'm:latest'}]})}, 'ollama'), 'loaded')
        self.assertEqual(await self.state({'/v1/models': listed, '/api/ps': httpx.Response(200, json={'models': []})}, 'ollama'), 'unloaded')
        lm = lambda value: {'/v1/models': listed, '/api/v0/models': httpx.Response(200, json={'data': [{'id': 'm', 'state': value}]})}
        self.assertEqual(await self.state(lm('not-loaded'), 'lm_studio'), 'unloaded')
        self.assertEqual(await self.state(lm('loaded'), 'lm_studio'), 'loaded')
        self.assertEqual(await self.state({'/v1/models': httpx.Response(401)}), '')                  # answers: nothing to wait for

    async def test_nothing_listening_is_told_apart_and_a_cloud_provider_is_left_alone(self):
        refused = httpx.ConnectError('[Errno 111] Connection refused')
        self.assertEqual(await self.state({'/v1/models': refused}), 'unreachable')
        posted, patched = self.answering({'/v1/models': refused})
        with patched:
            self.assertEqual(await models.state('https://api.example.com/v1', {}, 'm'), '')
            kind, message = await models.explain('classifier', 'http://127.0.0.1:9931/v1', {}, 'm')
            self.assertEqual(kind, 'unavailable')
            self.assertIn('classifier', message)
            self.assertIn('127.0.0.1:9931', message)
        self.assertEqual(posted, [])

    async def test_each_kind_gets_its_own_load_signal(self):
        for kind, path, body in (('speech', '/v1/backend/models/switch', {'model_key': 'm'}), ('ollama', '/api/generate', {'model': 'm'}),
                                 ('lm_studio', '/api/v1/models/load', {'model': 'm'}), ('', '/models/load', {'model': 'm'})):
            posted, patched = self.answering({})
            with patched:
                await models.load('http://127.0.0.1:8890/v1', {}, 'm', kind)
            self.assertEqual(posted, [(path, body)])

    async def test_a_step_that_stalls_on_an_unloaded_model_asks_for_the_load(self):
        with patch.object(models, 'state', AsyncMock(return_value='unloaded')), patch.object(models, 'load', AsyncMock()) as load:
            kind, message = await models.explain('voice', 'http://127.0.0.1:8890/v1', {}, 'tts', 'speech')
            await asyncio.sleep(0)
        self.assertEqual(kind, 'loading')
        self.assertIn('Loading tts for the voice', message)
        load.assert_awaited_once()

    async def test_a_server_that_stays_down_refuses_the_start_by_name(self):
        now = [0.0]

        async def sleep(seconds):
            now[0] += seconds
        with patch.object(models, 'state', AsyncMock(return_value='unreachable')), self.assertRaises(ValueError) as raised:
            await models.ensure_loaded('voice', 'http://127.0.0.1:8890/v1', {}, 'tts', AsyncMock(), sleep=sleep, clock=lambda: now[0], kind='speech')
        self.assertIn('voice’s server at 127.0.0.1:8890 is not answering', str(raised.exception))


class AskedFor(unittest.TestCase):
    """A task the classifier did not hear a request for: the user's own words, read alone, decide before anyone is asked."""
    def play(self, word):
        delegate = {'type': 'tool.proposal', 'name': 'delegate_task', 'arguments': {'task': 'Compare the two editors.'}}

        async def run():
            h = await Harness.create(classifier={'ask': script(typed_decision(explicit=.1), .2)}, typed=True, proposals=[delegate],
                                     model_deltas=[('One moment.', .1)], follow_deltas=[('It is on its way.', .1)])
            h.asked_for = word
            try:
                await h.candidate('ask', h.audio('ask'), final=True)
                await h.advance(8)
                jobs = h.state['service'].store.jobs(h.state['session']['target'])
                question = h.state.get('pending') is not None
                await h.settle()
                return len(jobs), question, [(item['history'], 'Compare' in repr(item)) for item in h.asked]
            finally:
                await h.close()
        return run_virtual(run())

    def test_work_the_user_asked_for_runs_without_a_question(self):
        jobs, question, asked = self.play('YES')
        self.assertEqual((jobs, question), (1, False))
        self.assertEqual(asked, [([], False)])               # read alone: no history, and not what the model proposed

    def test_work_they_did_not_ask_for_is_asked_about_first(self):
        for word in ('NO', 'OTHER', 'yes it is fine'):
            jobs, question, _ = self.play(word)
            self.assertEqual((jobs, question), (0, True), word)


class Waited(unittest.TestCase):
    def test_a_complete_question_the_model_waited_on_is_answered_after_a_pause(self):
        async def run():
            h = await Harness.create(classifier={'ask': script(SPEAK_PLAIN, .2)},
                                     model_deltas=[('USER: is it still going\nLANGUAGE: English\n[WAIT]', .1)])
            try:
                await h.candidate('ask', h.audio('ask'), final=True)
                await h.advance(2)
                early = len(h.answers)
                h.model_deltas = [('LANGUAGE: English\nYes, it is still going.', .1)]
                await h.settle()
                return early, len(h.answers), [entry[-1] for entry in h.log('speech.start')], h.answers[-1]['system'].count('write [WAIT] again')
            finally:
                await h.close()
        early, passes, spoken, told = run_virtual(run())
        self.assertEqual((early, passes, spoken, told), (1, 2, ['Yes, it is still going.'], 1))

    def test_the_beginning_of_a_thought_stays_unanswered(self):
        async def run():
            h = await Harness.create(classifier={'ask': script(SPEAK_PLAIN, .2)},
                                     model_deltas=[('USER: I need to\nLANGUAGE: English\n[WAIT]', .1)])
            try:
                await h.candidate('ask', h.audio('ask'), final=True)
                await h.settle()
                return len(h.answers), h.log('speech.start'), len(h.events('error'))
            finally:
                await h.close()
        self.assertEqual(run_virtual(run()), (2, [], 0))     # asked once more, never again, and nothing said


class Interruption(unittest.TestCase):
    def test_words_that_stopped_a_reply_are_always_put_to_the_model(self):
        async def run():
            side = typed_decision(attention='side_conversation', share=.95, explicit=.02)
            h = await Harness.create(typed=True, model_deltas=[(LONG, .1)],
                                     classifier={'ask': script(typed_decision(explicit=.95), .2), 'over': script(side, .2)})
            try:
                await h.candidate('ask', h.audio('ask'), final=True)
                await h.advance(6)
                for number, voiced in enumerate((.6, 1.2, 1.8, 2.4), 1):
                    await h.candidate('over', h.audio('over'), sequence=number, overlap=True, voiced_s=voiced)
                    await h.advance(.5)
                cut = h.state['session']['epoch']
                h.model_deltas = [('Slow builds were fixed.', .1)]
                await h.candidate('over', h.audio('over'), sequence=5, final=True, overlap=True, voiced_s=2.6)
                await h.settle()
                return cut, [entry[-1] for entry in h.log('speech.start')][-1]
            finally:
                await h.close()
        cut, last = run_virtual(run())
        self.assertGreaterEqual(cut, 2)                      # the reply was stopped by the safety yield
        self.assertEqual(last, 'Slow builds were fixed.')


class Muted(unittest.TestCase):
    def test_a_muted_utterance_frees_the_next_one(self):
        async def run():
            h = await Harness.create(classifier={'cut': script(SPEAK_PLAIN, .2), 'next': script(SPEAK_PLAIN, .2)})
            try:
                session = h.state['session']
                ids = {'session_id': session['id'], 'epoch': session['epoch']}
                await h.candidate('cut', h.audio('cut'), sequence=1)
                busy = await h.handler._candidate('browser', h.state, session, 'next', {'sequence': 1, 'audio': h.audio('next')}, h.module.modules()[0])
                first = await h.handler.process('convo.candidate_cancel', {**ids, 'turn_id': 'cut'}, 'browser')
                again = await h.handler.process('convo.candidate_cancel', {**ids, 'turn_id': 'cut'}, 'browser')
                late = await h.handler._candidate('browser', h.state, session, 'cut', {'sequence': 2, 'audio': h.audio('cut'), 'final': True}, h.module.modules()[0])
                taken = await h.handler._candidate('browser', h.state, session, 'next', {'sequence': 1, 'audio': h.audio('next')}, h.module.modules()[0])
                await h.settle()
                return busy, first, again, late, taken
            finally:
                await h.close()
        busy, first, again, late, taken = run_virtual(run())
        self.assertEqual((busy.get('busy'), first['cancelled'], again['cancelled']), (True, True, False))
        self.assertTrue(late.get('duplicate'))               # what was said before the mute is never completed afterwards
        self.assertTrue(taken.get('queued'))


class CloudUpkeep(unittest.IsolatedAsyncioTestCase):
    async def test_speech_does_not_stop_a_chapter_on_a_cloud_provider(self):
        handler = realtime.Realtime.__new__(realtime.Realtime)
        for kind, stopped in (('remote', False), ('llama.cpp', True)):
            task = asyncio.ensure_future(asyncio.sleep(30))
            handler._stop_upkeep({'server': {'kind': kind, 'slots': 1, 'slot': 0}, 'compact_task': task})
            await asyncio.sleep(0)
            self.assertEqual(task.cancelled() or task.cancelling() > 0, stopped)
            task.cancel()


if __name__ == '__main__':
    unittest.main()
