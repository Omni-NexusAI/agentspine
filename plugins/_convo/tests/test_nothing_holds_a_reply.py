"""Nothing can hold up a reply: Whisper in its own worker process, the voice loop freed of persona builds, settings reads, session
queries and disk waits, and gaps that stay explainable after a restart.

Offline only. The Whisper engine, the worker process and the CPU load are stand-ins (a fake engine, a fake worker script, a busy
loop); the real openai-whisper model and Agent Zero's process are not exercised here, so nothing in this file is a live latency
claim."""
import asyncio
import base64
import concurrent.futures
import importlib
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

if __package__:
    from .behavior_harness import LONG_REPLY, SPEAK, Harness, make_wav, run_virtual, script
    from .test_wiring import RealtimeBase
else:
    from behavior_harness import LONG_REPLY, SPEAK, Harness, make_wav, run_virtual, script
    from test_wiring import RealtimeBase

from plugins._convo.helpers import convo_metrics, convo_service, convo_whisper
from plugins._convo.helpers import convo_whisper_worker as worker
from plugins._convo.helpers.convo_host import Host
from plugins._convo.helpers.convo_store import Store


def wav(seconds=.5, rate=16000, channels=1):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as writer:
        writer.setnchannels(channels); writer.setsampwidth(2); writer.setframerate(rate)
        writer.writeframes(b'\x10\x00' * int(seconds * rate) * channels)
    return base64.b64encode(buffer.getvalue()).decode()


# --------------------------------------------------------------------------- the worker's own rules (in place)
class SlowEngine:
    """Stands in for Whisper: each transcription takes `seconds` and records how many ran at once."""
    def __init__(self, seconds=.15):
        self.seconds, self.active, self.peak, self.done = seconds, 0, 0, []
        self.lock = threading.Lock()

    def load(self):
        return ''

    def transcribe(self, encoded):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.seconds)
        with self.lock:
            self.active -= 1
        self.done.append(encoded)
        return {'text': 'words for ' + encoded, 'language': 'en'}


class WorkerRules(unittest.TestCase):
    def start(self, engine):
        sent, arrived = [], threading.Condition()
        def send(message):
            with arrived:
                sent.append(message); arrived.notify_all()
        rules = worker.Worker(engine, send)
        def wait_for(count, timeout=5):
            deadline = time.monotonic() + timeout
            with arrived:
                while len([m for m in sent if 'id' in m]) < count and time.monotonic() < deadline:
                    arrived.wait(.05)
            return [m for m in sent if 'id' in m]
        return rules, sent, wait_for

    def test_two_transcriptions_never_run_at_once_and_the_newest_waiting_one_wins(self):
        engine = SlowEngine()
        rules, sent, wait_for = self.start(engine)
        rules.load()
        thread = threading.Thread(target=rules.run_jobs, daemon=True); thread.start()
        rules.request({'id': 1, 'op': 'transcribe', 'audio': 'a'})
        time.sleep(.05)                                          # 'a' is running now
        rules.request({'id': 2, 'op': 'transcribe', 'audio': 'b'})
        rules.request({'id': 3, 'op': 'transcribe', 'audio': 'c'})          # replaces 'b', which is answered `dropped`
        answers = {m['id']: m for m in wait_for(3)}
        rules.close(); thread.join(2)
        self.assertEqual(engine.peak, 1)
        self.assertEqual(answers[2], {'id': 2, 'dropped': True})
        self.assertEqual((answers[1]['text'], answers[3]['text']), ('words for a', 'words for c'))
        self.assertEqual(engine.done, ['a', 'c'])

    def test_a_request_while_the_model_loads_is_answered_loading_at_once(self):
        rules, sent, wait_for = self.start(SlowEngine())
        started = time.monotonic()
        rules.request({'id': 1, 'op': 'transcribe', 'audio': 'a'})            # still 'loading': nothing was loaded yet
        self.assertEqual(wait_for(1, timeout=.5), [{'id': 1, 'state': 'loading'}])
        self.assertLess(time.monotonic() - started, .2)

    def test_an_unavailable_model_is_said_and_nothing_is_queued(self):
        engine = SlowEngine(); engine.load = lambda: 'model file not on disk'
        rules, sent, wait_for = self.start(engine)
        rules.load()
        rules.request({'id': 1, 'op': 'transcribe', 'audio': 'a'})
        self.assertEqual(wait_for(1), [{'id': 1, 'unavailable': 'model file not on disk'}])
        self.assertIsNone(rules.waiting)
        self.assertIn({'event': 'status', 'state': 'unavailable', 'detail': 'model file not on disk'}, sent)


class ModelFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def write(self, name, size):
        path = os.path.join(self.root, name)
        with open(path, 'wb') as stream:
            stream.write(b'\0' * size)
        return path

    def test_whispers_own_file_name_and_checksum_come_from_its_model_list(self):
        sha = 'a' * 64
        models = {'small': 'https://example.invalid/models/%s/small.pt' % sha}
        self.assertEqual(worker.model_file('small', '/m', models), (os.path.join('/m', 'small.pt'), sha))
        self.assertEqual(worker.model_file('turbo', '/m', {}), (os.path.join('/m', 'large-v3-turbo.pt'), ''))

    def test_a_missing_or_half_written_or_wrong_file_is_unavailable(self):
        self.assertEqual(worker.check_model_file(os.path.join(self.root, 'none.pt'), settle=0), 'model file not on disk')
        growing = self.write('growing.pt', 2 << 20)
        def download_in_progress(_seconds):
            with open(growing, 'ab') as stream:
                stream.write(b'\0' * 4096)                         # the size changes while it is watched
        self.assertEqual(worker.check_model_file(growing, sleep=download_in_progress), 'model file is still being written')
        self.assertEqual(worker.check_model_file(self.write('tiny.pt', 1000), settle=0), 'model file is incomplete')
        whole = self.write('whole.pt', 2 << 20)
        self.assertEqual(worker.check_model_file(whole, sha='0' * 64, settle=0), 'model file is incomplete or not the expected one')
        import hashlib
        self.assertEqual(worker.check_model_file(whole, sha=hashlib.sha256(b'\0' * (2 << 20)).hexdigest(), settle=0), '')

    def test_a_half_written_model_means_unavailable_and_nothing_is_ever_downloaded(self):
        calls = []
        fake = types.SimpleNamespace(_MODELS={'small': 'https://example.invalid/%s/small.pt' % ('b' * 64)},
                                     load_model=lambda *args, **kwargs: calls.append(('load_model', args, kwargs)),
                                     _download=lambda *args, **kwargs: calls.append(('download', args)))
        self.write('small.pt', 1 << 19)                                  # half written: below any checkpoint's size
        with patch.object(worker, 'SETTLE_SECONDS', 0):
            engine = worker.WhisperEngine('small', self.root, module=fake)
            self.assertEqual(engine.load(), 'model file is incomplete')
            self.assertEqual(worker.WhisperEngine('base', self.root, module=fake).load(), 'model file not on disk')
        self.assertEqual(calls, [])                                      # no load by name (which downloads), no download

    def test_a_complete_file_is_loaded_by_its_path_only(self):
        calls = []
        path = self.write('base.pt', 2 << 20)
        fake = types.SimpleNamespace(_MODELS={}, load_model=lambda name, **kwargs: calls.append((name, kwargs)) or object())
        with patch.object(worker, 'SETTLE_SECONDS', 0):
            self.assertEqual(worker.WhisperEngine('base', self.root, device='cpu', module=fake).load(), '')
        self.assertEqual(calls[0][0], path)                              # a path: openai-whisper never downloads for one

    def test_words_whisper_itself_rates_as_non_speech_are_left_out(self):
        # Judged from Whisper's own score of the audio (no_speech_prob), never from which words came out.
        class Model:
            def __init__(self, segments):
                self.segments = segments

            def transcribe(self, audio, **kwargs):
                return {'text': ''.join(s['text'] for s in self.segments), 'language': 'en', 'segments': self.segments}

        engine = worker.WhisperEngine('base', self.root, device='cpu', module=types.SimpleNamespace(_MODELS={}))
        engine.device = 'cpu'
        engine.model = Model([{'text': ' you', 'no_speech_prob': .9}])
        self.assertEqual(engine.transcribe(wav(.5)), {'text': '', 'language': 'en', 'left_out': 1})
        engine.model = Model([{'text': ' What time is it?', 'no_speech_prob': .01}, {'text': ' Thank you.', 'no_speech_prob': .88}])
        self.assertEqual(engine.transcribe(wav(.5))['text'], 'What time is it?')
        engine.model = Model([{'text': ' Thank you.', 'no_speech_prob': .02}])          # the same words, really said: kept
        self.assertEqual(engine.transcribe(wav(.5))['text'], 'Thank you.')

    def test_audio_is_decoded_in_memory_to_16_khz_mono(self):
        samples = worker.decode_wav(wav(.5, rate=8000, channels=2))
        self.assertEqual(len(samples), 8000)
        self.assertEqual(str(samples.dtype), 'float32')


# --------------------------------------------------------------------------- the manager, with a stand-in worker process
FAKE_WORKER = textwrap.dedent('''
    import json, os, sys, threading, time
    LOAD = float(os.environ.get("FAKE_WHISPER_LOAD", "1.5"))
    state = {"value": "loading"}
    lock = threading.Lock()
    def send(message):
        with lock:
            sys.stdout.write(json.dumps(message) + "\\n"); sys.stdout.flush()
    def load():
        end = time.monotonic() + LOAD
        while time.monotonic() < end:
            pass                                   # a busy CPU core while "loading"
        state["value"] = "ready"
        send({"event": "status", "state": "ready", "detail": ""})
    send({"event": "status", "state": "loading", "detail": ""})
    threading.Thread(target=load, daemon=True).start()
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("op") == "exit":
            break
        if state["value"] == "loading":
            send({"id": message["id"], "state": "loading"})
        else:
            send({"id": message["id"], "text": "heard %d" % len(message["audio"]), "language": "en"})
''')


class ManagerProcess(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.script = Path(self.tmp.name) / 'fake_worker.py'
        self.script.write_text(FAKE_WORKER, encoding='utf-8')
        self.manager = convo_whisper.Manager(script=self.script, model='base', root=self.tmp.name)

    async def asyncTearDown(self):
        self.manager.stop()
        await asyncio.sleep(.2)
        self.tmp.cleanup()

    async def test_a_loading_worker_with_a_busy_core_never_holds_the_loop_and_says_loading_at_once(self):
        gaps, stop = [], asyncio.Event()
        async def heartbeat():
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(.01)
                now = time.monotonic(); gaps.append(now - last); last = now
        beat = asyncio.create_task(heartbeat())
        await self.manager.ensure()
        answers = []
        for _ in range(5):
            started = time.monotonic()
            answers.append(await self.manager.transcribe(wav()))
            self.assertLess(time.monotonic() - started, .5)
            await asyncio.sleep(.05)
        stop.set(); await beat
        self.assertTrue(all(answer == {'state': 'loading'} for answer in answers), answers)
        self.assertLess(max(gaps), .25)                       # the voice loop kept its rhythm while the worker loaded
        self.assertEqual(self.manager.status()['state'], 'loading')

    async def test_after_loading_a_turn_is_written_down_and_stop_ends_the_process(self):
        await self.manager.ensure()
        self.assertTrue(await asyncio.to_thread(self.manager.ready.wait, 10))
        answer = await self.manager.transcribe(wav())
        self.assertTrue(answer['text'].startswith('heard '))
        process = self.manager.process
        self.manager.stop()
        await asyncio.to_thread(process.wait, 5)
        self.assertIsNotNone(process.poll())
        self.assertEqual(self.manager.status()['state'], 'off')

    async def test_a_text_only_turn_may_wait_for_the_load_bounded(self):
        await self.manager.ensure()
        answer = await self.manager.transcribe(wav(), wait=10, timeout=10)
        self.assertTrue(answer.get('text', '').startswith('heard '), answer)

    async def test_a_worker_that_cannot_start_is_unavailable_not_an_error(self):
        broken = convo_whisper.Manager(python=os.path.join(self.tmp.name, 'no-such-python'), model='base', root=self.tmp.name)
        answer = await broken.transcribe(wav())
        self.assertIn('unavailable', answer)


class HostWhisperState(unittest.TestCase):
    def test_loading_or_a_model_that_would_be_downloaded_stops_the_browser_from_asking(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(convo_whisper.host_state('small', root), 'missing')
            Path(root, 'small.pt').write_bytes(b'x')
            self.assertEqual(convo_whisper.host_state('small', root), 'ready')
            loading = types.ModuleType('helpers.whisper'); loading.is_updating_model = True
            with patch.dict(sys.modules, {'helpers.whisper': loading}):
                self.assertEqual(convo_whisper.host_state('small', root), 'loading')


# --------------------------------------------------------------------------- the store: session copy, writer thread, Whisper text
class StoreForTheVoiceLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'db'
        self.store = Store(self.path); self.addCleanup(self.store.close)
        self.session = self.store.start('browser', 'a')

    def test_the_check_before_each_audio_chunk_reads_no_database(self):
        statements = []
        self.store.session(self.session['id'], 'browser')
        self.store.db.set_trace_callback(statements.append)
        for _ in range(50):
            self.store.session(self.session['id'], 'browser')
        self.store.db.set_trace_callback(None)
        self.assertEqual(statements, [])
        self.assertEqual(self.store.db.execute('PRAGMA synchronous').fetchone()[0], 1)      # NORMAL

    def test_turning_the_plugin_off_is_seen_by_the_very_next_check(self):
        self.store.session(self.session['id'], 'browser')
        stopper = threading.Thread(target=self.store.stop, args=(self.session['id'], 'browser'))   # deactivate runs elsewhere
        stopper.start(); stopper.join()
        with self.assertRaises(ValueError):
            self.store.session(self.session['id'], 'browser')

    def test_a_rolled_back_change_leaves_no_stale_copy(self):
        before = self.store.session(self.session['id'], 'browser')['epoch']
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.db.execute('UPDATE sessions SET epoch=epoch+5 WHERE id=?', (self.session['id'],))
                self.store._sessions_changed()
                self.assertEqual(self.store.session(self.session['id'], 'browser')['epoch'], before + 5)
                raise RuntimeError('rolled back')
        self.assertEqual(self.store.session(self.session['id'], 'browser')['epoch'], before)
        self.assertEqual(self.store.interrupt(self.session['id'], 'browser')['epoch'], before + 1)
        self.assertEqual(self.store.session(self.session['id'], 'browser')['epoch'], before + 1)

    def test_timing_marks_and_ignore_reasons_are_kept_across_a_restart_bounded_and_content_free(self):
        log = convo_metrics.TimingLog()
        log.sink = self.store.keep_mark
        log.mark('t1', 'policy.gate', disposition='ignore', reason='backchannel', transcript='never kept', audio='never kept')
        log.mark('t1', 'state.sent', disposition='ignore')
        self.store.close()
        again = Store(self.path); self.addCleanup(again.close)                # a restart
        kept = again.marks()
        self.assertEqual([(m['turn'], m['name']) for m in kept], [('t1', 'policy.gate'), ('t1', 'state.sent')])
        self.assertEqual(kept[0]['fields']['reason'], 'backchannel')
        self.assertNotIn('never kept', repr(kept))
        self.assertGreater(kept[0]['at'], 0)                                 # wall time, so a gap can be placed after a restart
        again.MARKS_KEPT = 5
        for index in range(30):
            again.keep_mark('t%d' % index, {'name': 'reply.start', 't': 1.0, 'seq': index})
        kept = again.marks(limit=100)
        self.assertEqual([m['turn'] for m in kept], ['t25', 't26', 't27', 't28', 't29'])     # the newest MARKS_KEPT

    def test_closing_the_mark_writer_never_blocks_on_a_full_queue(self):
        """Review 2026-11-17 #10: close() used a blocking put(None); with the queue full (a writer that is stuck or never started)
        it waited for ever."""
        from plugins._convo.helpers.convo_store import MarkWriter
        writer = MarkWriter(None, 10)                                        # no thread: stands in for a writer that is not draining
        writer.thread = threading.Thread(target=lambda: None); writer.thread.start(); writer.thread.join()
        for index in range(writer.QUEUE):
            writer.queue.put_nowait(('t', 'm', 0.0, '{}'))
        started = time.monotonic()
        closer = threading.Thread(target=writer.close, daemon=True); closer.start(); closer.join(5)
        self.assertFalse(closer.is_alive())
        self.assertLess(time.monotonic() - started, 4)
        # A real writer whose queue was full when it was closed still stops once it has written what was queued.
        live = MarkWriter(str(Path(self.tmp.name) / 'marks.db'), 100000)
        for index in range(50):
            live.put('t', {'name': 'reply.start', 'seq': index})
        live.close()
        self.assertFalse(live.thread.is_alive())

    def test_a_failing_sink_never_reaches_the_voice_path(self):
        log = convo_metrics.TimingLog()
        log.sink = lambda turn, mark: (_ for _ in ()).throw(OSError('disk full'))
        log.mark('t', 'reply.start')
        self.assertEqual(log.records()[0]['marks'][0]['name'], 'reply.start')

    def test_a_turn_the_model_declined_keeps_no_whisper_text(self):
        sid = self.session['id']
        self.store.accept_turn(sid, 'browser', 0, 'noise', {'input': 'audio', 'transcript': 'words nobody said', 'whisper': 'words nobody said'})
        self.store.held(sid, 'browser', 'noise', 'silent', 'background noise')
        content = self.store.context('a')['turns'][0]['content']
        self.assertNotIn('whisper', content); self.assertNotIn('transcript', content)
        self.assertFalse(self.store.transcript(sid, 'browser', 'noise', 'more words nobody said'))      # a late one is refused
        self.assertNotIn('transcript', self.store.context('a')['turns'][0]['content'])

    def test_a_turn_with_no_reply_drops_whisper_text_but_a_text_turn_keeps_its_words(self):
        sid = self.session['id']
        self.store.accept_turn(sid, 'browser', 0, 'heard', {'input': 'audio', 'transcript': 'hiss', 'whisper': 'hiss', 'model_line': 'Hello'})
        self.store.accept_turn(sid, 'browser', 0, 'typed', {'input': 'text', 'transcript': 'Question'})
        self.assertTrue(self.store.forget_whisper(sid, 'browser', 'heard'))
        self.assertFalse(self.store.forget_whisper(sid, 'browser', 'typed'))
        turns = {t['id']: t['content'] for t in self.store.context('a')['turns']}
        self.assertEqual((turns['heard'].get('transcript'), turns['heard'].get('whisper')), ('Hello', None))
        self.assertEqual(turns['typed']['transcript'], 'Question')


# --------------------------------------------------------------------------- settings: cached per session, revision checked
class SettingsCacheTests(unittest.TestCase):
    def test_settings_are_read_again_only_when_the_file_or_toggle_changed(self):
        stamp, reads, now = [1], [], [0.0]
        def read():
            reads.append(1)
            return {'roles': {'tts': {'voice': 'v%d' % len(reads)}}}
        cache = convo_service.SettingsCache(read, lambda: stamp[0], max_age=30, clock=lambda: now[0])
        first = cache(); cache(); cache()
        self.assertEqual(len(reads), 1)
        first['roles']['tts']['voice'] = 'changed by a caller'
        self.assertEqual(cache()['roles']['tts']['voice'], 'v1')                    # callers get copies
        stamp[0] = 2
        self.assertEqual(cache()['roles']['tts']['voice'], 'v2')
        now[0] = 31
        cache()
        self.assertEqual(len(reads), 3)                                             # bounded age: keys and gateway state too

    def test_a_file_caught_mid_write_raises_so_the_session_copy_is_kept_and_the_next_read_retries(self):
        stamp, outcomes = [1], [ValueError('Expecting value: line 1 column 1'), {'ok': True}]
        def read():
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        cache = convo_service.SettingsCache(read, lambda: stamp[0])
        with self.assertRaises(ValueError):
            cache()
        self.assertEqual(cache(), {'ok': True})


class SettingsMidWrite(unittest.TestCase):
    def test_a_reply_keeps_the_sessions_copy_when_the_settings_cannot_be_read(self):
        async def run():
            h = await Harness.create(classifier={'q': script(SPEAK, .2)})
            try:
                h.config = None                          # every read now fails, as a settings file caught mid-write
                await h.candidate('t', h.audio('q'), final=True)
                await h.settle()
                return [text for _, text in h.spoken], h.events('error')
            finally:
                await h.close()
        spoken, errors = run_virtual(run())
        self.assertEqual((spoken, errors), (['A complete answer.'], []))


# --------------------------------------------------------------------------- the voice path with Whisper loading on a busy CPU
class FakeWhisper:
    """Convo's Whisper as the voice path sees it: a load of `load` seconds, then `answer`. Records every call."""
    def __init__(self, load=20.0, answer=None):
        self.load, self.answer, self.calls, self.active, self.peak = load, answer or {'state': 'loading'}, [], 0, 0
        self.state, self.stops = 'off', 0

    async def ensure(self):
        self.state = 'loading'

    def status(self):
        return {'state': self.state, 'detail': '', 'model': 'base'}

    def stop(self):
        self.state, self.stops = 'off', self.stops + 1

    async def transcribe(self, audio, wait=0.0, timeout=60):
        self.calls.append((asyncio.get_running_loop().time(), audio))
        self.active += 1; self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.load)
            return dict(self.answer)
        finally:
            self.active -= 1


class VoicePathUnchanged(unittest.TestCase):
    def conversation(self, whisper=None, model_deltas=None, burn=False):
        async def run():
            h = await Harness.create(classifier={'q1': script(SPEAK, .2), 'q2': script(SPEAK, .3)}, model_deltas=model_deltas or LONG_REPLY,
                                     config={'transcription_enabled': whisper is not None})
            if whisper is not None:
                h.service.whisper = whisper
            busy = subprocess.Popen([sys.executable, '-c', 'while True: pass']) if burn else None
            try:
                for turn in ('q1', 'q2'):
                    await h.candidate(turn, h.audio(turn), final=True)
                    await h.settle()
                    for _, event in h.audible(turn):
                        await h.playback(turn, event['phrase_id'])
                    await h.advance(.5)
                await h.advance(30)                       # long enough for any Whisper work to end
                kinds = {'audio.phrase', 'audio.begin', 'audio.chunk', 'candidate.state', 'candidate.admitted', 'state', 'text.delta'}
                timeline = [(round(t, 4), p['type'], p.get('turn_id')) for t, p in h.timeline if p.get('type') in kinds]
                policy = [round(item[1], 4) for item in h.log('policy.done')]
                turns = {t['id']: t['content'] for t in h.store.context('a')['turns']}
                return timeline, policy, turns, h.events('transcript.saved'), h.events('transcript.wanted')
            finally:
                if busy is not None:
                    busy.kill(); busy.wait(5)
                await h.close()
        return run_virtual(run())

    def test_whisper_loading_for_20_s_on_a_busy_core_changes_no_chunk_gap_classifier_time_or_first_audio(self):
        quiet = self.conversation()
        loading = FakeWhisper(load=20.0)
        busy = self.conversation(whisper=loading, burn=True)
        self.assertEqual(busy[0], quiet[0])                # every phrase, state and decision at the same moment
        self.assertEqual(busy[1], quiet[1])                # classifier timings (p95 included) unchanged
        self.assertEqual(len(loading.calls), 2)            # each replied-to turn was asked for, once, while it was loading

    def test_the_transcript_is_asked_for_after_the_reply_was_heard_and_kept_beside_the_models_line(self):
        whisper = FakeWhisper(load=1.0, answer={'text': 'What is the weather', 'language': 'en'})
        timeline, _, turns, saved, wanted = self.conversation(whisper=whisper)
        heard = max(t for t, kind, turn in timeline if turn == 'q1' and kind == 'audio.phrase')
        self.assertGreater(whisper.calls[0][0], heard)
        self.assertEqual(turns['q1']['whisper'], 'What is the weather')
        self.assertEqual([p['turn_id'] for _, p in saved], ['q1', 'q2'])
        self.assertEqual(wanted, [])

    def test_a_silent_turn_gets_no_whisper_request_or_text(self):
        whisper = FakeWhisper(load=.1, answer={'text': 'words nobody said'})
        _, _, turns, saved, wanted = self.conversation(whisper=whisper, model_deltas=[('USER: \n', .1), ('[SILENT] background noise', .1)])
        self.assertEqual(whisper.calls, [])
        self.assertEqual((saved, wanted), ([], []))
        self.assertTrue(all('whisper' not in content for content in turns.values()))


class WorkerLifetime(unittest.TestCase):
    def test_the_worker_runs_only_while_the_session_keeps_transcripts(self):
        async def run():
            h = await Harness.create(classifier={'q1': script(SPEAK, .2)}, config={'transcription_enabled': True})
            whisper = h.service.whisper = FakeWhisper(load=.1, answer={'text': 'Hello'})
            closed = False
            try:
                await h.candidate('q1', h.audio('q1'), final=True)
                await h.settle()
                for _, event in h.audible('q1'):
                    await h.playback('q1', event['phrase_id'])
                await h.advance(2)
                started = whisper.state                      # started beside the session at its first transcript
                h.config = {**h.config, 'transcription_enabled': False}
                await h.advance(7)
                turned_off = (whisper.state, whisper.stops)
                h.config = {**h.config, 'transcription_enabled': True}
                await whisper.ensure()
                await h.close(); closed = True
                await asyncio.sleep(1.5)
                return started, turned_off, (whisper.state, whisper.stops)
            finally:
                if not closed:
                    await h.close()
        started, turned_off, ended = run_virtual(run())
        self.assertEqual(started, 'loading')
        self.assertEqual(turned_off, ('off', 1))
        self.assertEqual(ended, ('off', 2))


class TextOnlyWorkerLifetime(unittest.TestCase):
    def test_a_text_only_session_owns_the_worker_its_turns_load_and_keeps_it_while_it_lives(self):
        """Review 2026-11-17 #7: a text-only turn with transcripts off at start loaded the worker with no watcher (it outlived the
        session), and transcripts off made the watcher stop it every 5 s (each text-only turn reloaded)."""
        async def run():
            h = await Harness.create(config={'transcription_enabled': False, 'roles': {'conversation': {'capabilities': {'audio_input': False}}}})
            whisper = h.service.whisper = FakeWhisper(load=0, answer={'text': 'Question'})
            closed = False
            try:
                before = h.state.get('whisper_watch')
                answer = await h.handler.process('convo.whisper', {**h.ids(), 'turn_id': 'tx', 'audio': wav()}, 'browser')
                watching = h.state.get('whisper_watch') is not None
                await h.advance(12)                          # transcripts stay off: two watcher checks
                kept = (whisper.state, whisper.stops)
                await h.close(); closed = True
                await asyncio.sleep(1.5)
                return before, answer, watching, kept, (whisper.state, whisper.stops)
            finally:
                if not closed:
                    await h.close()
        before, answer, watching, kept, ended = run_virtual(run())
        self.assertIsNone(before)
        self.assertEqual(answer, {'ok': True, 'text': 'Question'})
        self.assertTrue(watching)
        self.assertEqual(kept, ('loading', 0))
        self.assertEqual(ended, ('off', 1))


class TextOnlyWhisper(RealtimeBase):
    async def test_a_text_only_turn_is_written_down_by_the_worker_or_left_to_the_browser(self):
        whisper = FakeWhisper(load=0, answer={'text': 'Question'})
        request = {**self.ids, 'turn_id': 'tx', 'audio': wav()}
        self.assertEqual(await self.handler.process('convo.whisper', request, 'browser'), {'ok': True, 'by': 'browser'})
        self.service.whisper = whisper
        self.assertEqual(await self.handler.process('convo.whisper', request, 'browser'), {'ok': True, 'text': 'Question'})
        whisper.answer = {'unavailable': convo_whisper.NO_WHISPER}
        self.assertEqual(await self.handler.process('convo.whisper', request, 'browser'), {'ok': True, 'by': 'browser'})
        whisper.answer = {'unavailable': 'model file not on disk'}
        self.assertEqual(await self.handler.process('convo.whisper', request, 'browser'),
                         {'ok': True, 'text': '', 'unavailable': 'model file not on disk'})


class PluginOffMidReply(RealtimeBase):
    async def test_turning_the_plugin_off_mid_reply_stops_audio_within_one_chunk(self):
        state = self.handler.connections['browser']
        self.store.accept_turn(self.session['id'], 'browser', 0, 'pcm', {'transcript': 'Question'})
        next_chunk = asyncio.Event()
        async def chunks(role, text):
            for _ in range(20):
                yield {'audio': b'\0\0' * 240, 'format': 'pcm_s16le', 'sample_rate': 24000, 'channels': 1}
                await next_chunk.wait(); next_chunk.clear()
        state['sidecar'].speech_stream = chunks
        task = asyncio.create_task(self.handler._speak('browser', state, self.session, 'pcm', 'Answer', {'capabilities': {'native_pcm': True}}))
        await asyncio.sleep(0)
        await asyncio.to_thread(self.store.stop, self.session['id'], 'browser')     # native OFF (Service.deactivate) from its own thread
        next_chunk.set()
        await asyncio.wait_for(task, 2)
        self.assertEqual([event[2]['type'] for event in self.handler.sent], ['audio.begin', 'audio.chunk'])


# --------------------------------------------------------------------------- the persona is built off the voice loop
class PersonaOffTheLoop(unittest.IsolatedAsyncioTestCase):
    async def test_the_chats_own_prompt_is_built_by_a_throwaway_agent_on_the_persona_thread(self):
        loop_thread, seen = threading.current_thread(), []

        class Agent:
            def __init__(self, number, config, context):
                self.number, self.config, self.context = number, config, context
            async def get_system_prompt(self, loop_data):
                seen.append(threading.current_thread())
                return ['Identity: J.A.R.V.I.S.']

        class DeferredTask:
            def __init__(self, thread_name=''):
                self.future = concurrent.futures.Future()
            def start_task(self, build):
                threading.Thread(target=lambda: self.future.set_result(asyncio.run(build())), name='ConvoPersona').start()
                return self
            async def result(self, timeout):
                return await asyncio.wait_for(asyncio.wrap_future(self.future), timeout)

        chat_agent = types.SimpleNamespace(number=0, config=types.SimpleNamespace(profile='agent0'), context='ctx',
                                           get_system_prompt=None, call_utility_model=None)
        async def utility(system, message, background=False):
            return 'Calm butler.'
        chat_agent.call_utility_model = utility
        agent_module = types.ModuleType('agent')
        agent_module.Agent, agent_module.LoopData = Agent, (lambda: object())
        agent_module.AgentContext = types.SimpleNamespace(get=lambda target: types.SimpleNamespace(agent0=chat_agent))
        helpers = types.ModuleType('helpers'); defer = types.ModuleType('helpers.defer'); defer.DeferredTask = DeferredTask
        helpers.defer = defer
        with patch.dict(sys.modules, {'agent': agent_module, 'helpers': helpers, 'helpers.defer': defer}):
            persona = await Host().persona('a', '', {})
        self.assertEqual(persona, 'Calm butler.')
        self.assertEqual(len(seen), 1)
        self.assertIsNot(seen[0], loop_thread)
        self.assertEqual(seen[0].name, 'ConvoPersona')


if __name__ == '__main__':
    unittest.main()
