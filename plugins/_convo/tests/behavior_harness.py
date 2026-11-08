"""Behaviour harness: the real Realtime handler, policy gate and journal on a virtual clock.

Everything outside Convo's own logic is scripted: the classifier (per audio tag, with a latency, an optional failure
and an optional cancellation-ignoring mode), the conversation model (deltas with delays), TTS latency and admission,
playback receipts and the browser's candidate stream. Time is virtual, so a 1.5 s classifier deadline costs nothing
and every latency is reproducible.

This tests Convo's decisions, not acoustics and not real providers. The live corpus (real models, real
microphone) is a separate acceptance step; automated results here are never a performance claim."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import importlib
import io
import json
import os
import selectors
import sys
import tempfile
import types
import wave
from pathlib import Path
from unittest.mock import patch

if __package__:
    from .test_realtime import HostStub, WsStub
else:  # pragma: no cover - direct execution
    from test_realtime import HostStub, WsStub

from plugins._convo.helpers import convo_policy
from plugins._convo.helpers.convo_contract import SpeechBusy, merge, validate_settings
from plugins._convo.helpers.convo_host import Compactor
from plugins._convo.helpers.convo_store import Store


# --------------------------------------------------------------------------- virtual time
class _VirtualSelector(selectors.SelectSelector):
    """Advance virtual time instead of sleeping; real descriptors (the loop's self-pipe) still poll."""
    loop = None

    def select(self, timeout=None):
        events = super().select(0)
        if events or self.loop is None:
            return events
        if timeout is None:
            self.loop.idle += 1
            if self.loop.idle > 1000:
                raise RuntimeError('Virtual loop is deadlocked: nothing scheduled and nothing runnable')
            return super().select(0.001)
        self.loop.idle = 0
        if timeout > 0:
            self.loop.virtual += timeout
        return events


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        selector = _VirtualSelector()
        super().__init__(selector)
        selector.loop = self
        self.virtual = 0.0
        self.idle = 0

    def time(self):
        return self.virtual


def run_virtual(coroutine):
    loop = VirtualLoop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coroutine)
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


# --------------------------------------------------------------------------- audio helpers
def make_wav(level, seconds=0.2, rate=16000):
    """A valid mono PCM16 WAV whose constant sample value identifies the clip to the scripted classifier."""
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(rate)
        writer.writeframes(int(level).to_bytes(2, 'little', signed=True) * int(rate * seconds))
    return base64.b64encode(output.getvalue()).decode()


def make_frames(level, frames, rate=16000):
    """Like make_wav but with an exact frame count (to probe the server's length bound)."""
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(rate)
        writer.writeframes(int(level).to_bytes(2, 'little', signed=True) * int(frames))
    return base64.b64encode(output.getvalue()).decode()


def concat_wavs(*encoded):
    frames, params = [], None
    for item in encoded:
        with wave.open(io.BytesIO(base64.b64decode(item))) as reader:
            params = reader.getparams(); frames.append(reader.readframes(reader.getnframes()))
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setparams(params); writer.writeframes(b''.join(frames))
    return base64.b64encode(output.getvalue()).decode()


def frame_count(encoded):
    with wave.open(io.BytesIO(base64.b64decode(encoded))) as reader:
        return reader.getnframes()


# --------------------------------------------------------------------------- scripted decisions
SPEAK = {'speech': 'speak', 'action': 'none', 'confidence': .99, 'explicit_request': True, 'context_note': 'Addressed'}
SPEAK_PLAIN = {'speech': 'speak', 'action': 'none', 'confidence': .99, 'explicit_request': False, 'context_note': 'Addressed'}
SILENT = {'speech': 'silent', 'action': 'none', 'confidence': .99}
UNCERTAIN = {'speech': 'clarify', 'action': 'none', 'confidence': .4}
DELEGATE = {'speech': 'speak', 'action': 'delegate', 'confidence': .99, 'explicit_request': True, 'context_note': 'Long task'}
DIRECT = {'speech': 'speak', 'action': 'direct', 'confidence': .99, 'explicit_request': True, 'context_note': 'Quick lookup'}

# Two ready-made model replies. Each delta is a complete spoken phrase (>= 60 characters ending in a stop).
LONG_REPLY = [('The first sentence of this reply is deliberately long enough to be spoken. ', .3),
              ('The second sentence keeps the reply going for a little while longer. ', .3),
              ('The third sentence closes the reply and gives the user time to interrupt.', .3)]


def script(decision=None, delay=.2, fail=False, stubborn=0):
    """One scripted classifier behaviour. `stubborn` keeps a cancelled call running for that many seconds."""
    return {'decision': dict(decision or SILENT), 'delay': delay, 'fail': fail, 'stubborn': stubborn}


class RecordingHost(HostStub):
    def __init__(self):
        self.directs = []

    async def direct(self, target, name, args):
        self.directs.append((target, name, args))
        return 'Search result'


class ScriptedSidecar:
    """Classifier, model and TTS doubles with scripted latency; records what it was asked and when."""
    def __init__(self, harness):
        self.h = harness; self.calls = []; self.log = []; self.busy_until = 0.0; self.count = {}

    async def health(self):
        return {'service': 'convo', 'protocol': 1}

    @staticmethod
    def fingerprint(audio):
        return hashlib.sha1((audio or '').encode()).hexdigest()[:12]

    def _tag(self, audio, fallback):
        return self.h.tags.get(self.fingerprint(audio), fallback) if audio else fallback

    def _script(self, tag):
        item = self.h.classifier.get(tag)
        if item is None:
            return self.h.default_script
        if isinstance(item, list):
            n = self.count.get(tag, 0); self.count[tag] = n + 1
            item = item[min(n, len(item) - 1)]
        return item if 'decision' in item else script(item)

    async def policy(self, payload):
        loop = asyncio.get_running_loop()
        self.calls.append('policy')
        tag = self._tag(payload.get('audio'), '<text>')
        item = self._script(tag)
        self.log.append(('policy.start', loop.time(), tag, bool(payload['context'].get('candidate_final')), bool(payload['context'].get('assistant_active'))))
        due = loop.time() + item['delay']
        try:
            await asyncio.sleep(item['delay'])
        except asyncio.CancelledError:
            self.log.append(('policy.cancelled', loop.time(), tag))
            if not item['stubborn']:
                raise
            await asyncio.sleep(max(0, due - loop.time()))  # a provider that ignores cancellation
            self.log.append(('policy.late', loop.time(), tag))
            return dict(item['decision'])
        if item['fail']:
            self.log.append(('policy.failed', loop.time(), tag))
            raise ValueError('classifier unavailable')
        self.log.append(('policy.done', loop.time(), tag))
        return dict(item['decision'])

    async def _nap(self, seconds):
        stubborn = self.h.stubborn_answer
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            self.log.append(('answer.cancelled', asyncio.get_running_loop().time()))
            if not stubborn:
                raise
            await asyncio.sleep(stubborn)  # a provider that ignores cancellation and keeps going

    async def answer(self, payload):
        loop = asyncio.get_running_loop()
        tag = self._tag(payload.get('audio'), '<text>')
        self.calls.append('answer')
        self.h.answers.append(copy.deepcopy(payload))  # the host mutates its request between model calls
        self.log.append(('answer.start', loop.time(), tag, bool(payload.get('allow_tools'))))
        for text, delay in self.h.model_deltas:
            await self._nap(delay)
            yield {'type': 'text.delta', 'text': text}
        # A misbehaving model may propose tools it was not offered; the host must still refuse them.
        proposals = self.h.proposal if isinstance(self.h.proposal, list) else ([self.h.proposal] if self.h.proposal else [])
        for item in proposals:
            if self.h.proposal_always or payload.get('allow_tools'):
                yield dict(item)
        yield {'type': 'answer.done'}
        self.log.append(('answer.done', loop.time(), tag))

    async def speech(self, role, text):
        loop = asyncio.get_running_loop()
        now = loop.time()
        self.calls.append('speech')
        if self.h.tts_admission and now < self.busy_until:
            # Mirrors sidecar/speech_admission.py: an owned worker keeps draining after its caller detached.
            self.log.append(('speech.409', now, text))
            raise SpeechBusy('draining')       # the typed error the real transport raises for a tracked, still-running request
        if self.h.tts_admission:
            self.busy_until = now + self.h.tts_delay
        self.log.append(('speech.start', now, text))
        try:
            await asyncio.sleep(self.h.tts_delay)
        except asyncio.CancelledError:
            self.log.append(('speech.cancelled', loop.time(), text))
            raise
        self.h.spoken.append((loop.time(), text))
        return b'pretend-wave'


# --------------------------------------------------------------------------- the session under test
class Harness:
    """One connected voice session. Use inside run_virtual(): `h = await Harness.create(...)`."""
    @classmethod
    async def create(cls, **options):
        self = cls()
        await self._start(**options)
        return self

    async def _start(self, config=None, classifier=None, model_deltas=None, tts_delay=.1, tts_admission=False,
                     proposal=None, proposal_always=True, mode='active', system_one=None, stubborn_answer=0, backend=None,
                     truth=None, default=None):
        loop = asyncio.get_running_loop()
        self.t0 = loop.time()
        self.tmp = tempfile.TemporaryDirectory(); self.store = Store(Path(self.tmp.name) / 'db')
        self.tags, self.classifier, self.default_script = {}, dict(classifier or {}), script(default or SILENT)
        self.model_deltas = list(model_deltas if model_deltas is not None else [('A complete answer.', .2)])
        self.tts_delay, self.tts_admission, self.proposal, self.stubborn_answer = tts_delay, tts_admission, proposal, stubborn_answer
        self.proposal_always = proposal_always
        self.timeline, self.dispatches, self.answers, self.spoken, self.arrivals = [], [], [], [], []
        self.truth = dict(truth or {}); self.dummy = None
        self.host = RecordingHost()
        self.sidecar = backend if backend is not None else ScriptedSidecar(self)
        self.service = types.SimpleNamespace(store=self.store, host=self.host, persona_cache={},
            dispatcher=types.SimpleNamespace(start=lambda: self.dispatches.append('wake'), cancel=lambda job: self.dispatches.append(('cancel', job))),
            compactor=Compactor(self.store, self.host))
        base = {'enabled': True, 'roles': {'classifier': {'url': 'http://localhost/v1', 'model': 'classifier'}, 'conversation': {'model': 'conversation'}}}
        self.config = validate_settings(merge(base, {'system_one': system_one} if system_one else {}, config or {}))
        module_stub = types.ModuleType('helpers.ws'); module_stub.WsHandler = WsStub
        with patch.dict(sys.modules, {'helpers.ws': module_stub}):
            self.module = importlib.import_module('plugins._convo.api.realtime')
        self.patch = patch.object(self.module, 'modules', return_value=(
            types.SimpleNamespace(__package__='plugins._convo.helpers', settings=lambda: self.config, service=lambda: self.service),
            convo_policy, types.SimpleNamespace(Sidecar=lambda config: self.sidecar)))
        self.patch.start()
        self.handler = self.module.Realtime()
        original = self.handler.emit_to
        async def emit(sid, event, payload):
            self.timeline.append((asyncio.get_running_loop().time() - self.t0, payload))
            await original(sid, event, payload)
        self.handler.emit_to = emit
        try:
            response = await self.handler.process('convo.start', {'target': 'a', 'mode': mode}, 'browser')
            if not response.get('ok'):
                raise ValueError(response['message'])      # a refused start is data on the wire; tests read it as the refusal it is
        except BaseException:
            await self.close()  # a refused start must not leak the store or the patch
            raise
        self.session = response['session']
        self.state = self.handler.connections['browser']

    # ---- time
    def now(self):
        return asyncio.get_running_loop().time() - self.t0

    async def until(self, seconds):
        await asyncio.sleep(max(0, seconds - self.now()))

    async def advance(self, seconds):
        await asyncio.sleep(seconds)

    # ---- the browser side
    def audio(self, tag, level=None, seconds=.2):
        """A clip the scripted classifier recognises by `tag` (distinct levels give distinct fingerprints)."""
        level = level if level is not None else 200 + len(self.tags)
        wav = make_wav(level, seconds)
        self.tags[ScriptedSidecar.fingerprint(wav)] = tag
        return wav

    def merged(self, tag, *clips):
        wav = concat_wavs(*clips)
        self.tags[ScriptedSidecar.fingerprint(wav)] = tag
        return wav

    def ids(self):
        return {'session_id': self.session['id'], 'epoch': self.store.session(self.session['id'], 'browser')['epoch']}

    async def candidate(self, turn, wav=None, sequence=1, final=False, epoch=None, transcript=None):
        """One candidate message. Audio for direct-audio conversations; a transcript only for text-only ones."""
        ids = self.ids()
        if epoch is not None:
            ids['epoch'] = epoch
        message = {**ids, 'turn_id': turn, 'sequence': sequence, 'final': final}
        if wav is not None:
            message['audio'] = wav
        if transcript is not None:
            message['transcript'] = transcript
        self.arrivals.append({'time': self.now(), 'tag': self.tags.get(ScriptedSidecar.fingerprint(wav), '<untagged>') if wav else '<text>',
                              'turn': turn, 'sequence': sequence, 'final': final})
        return await self.handler.process('convo.candidate', message, 'browser')

    async def interrupt(self):
        return await self.handler.process('convo.interrupt', self.ids(), 'browser')

    async def retarget(self, target):
        return await self.handler.process('convo.retarget', {**self.ids(), 'target': target}, 'browser')

    async def playback(self, turn, phrase, status='completed'):
        return await self.handler.process('convo.playback', {**self.ids(), 'turn_id': turn, 'phrase_id': phrase, 'status': status}, 'browser')

    def start_reply(self):
        """A reply already in flight that never finishes (like the existing candidate tests)."""
        self.dummy = self.state['task'] = asyncio.create_task(asyncio.Event().wait())
        return self.dummy

    async def settle(self, limit=120):
        """Run until the candidate loop, any real reply and compaction have finished."""
        for _ in range(20):
            tasks = [t for t in (self.state.get(key) for key in ('candidate_task', 'task', 'compact_task'))
                     if t and not t.done() and t is not self.dummy]
            if not tasks:
                return
            _, pending = await asyncio.wait(tasks, timeout=limit)
            if pending:
                raise AssertionError('Scenario did not settle within %s virtual seconds' % limit)
        raise AssertionError('Scenario kept starting new work')

    # ---- observations
    def events(self, kind=None, turn=None):
        return [(t, p) for t, p in self.timeline if (kind is None or p.get('type') == kind) and (turn is None or p.get('turn_id') == turn)]

    def states(self, turn=None):
        return [(t, p['state'], p.get('final'), p.get('sequence')) for t, p in self.events('candidate.state', turn)]

    def last_state(self, turn):
        found = self.states(turn)
        return found[-1][1] if found else None

    def audible(self, turn=None):
        return [(t, p) for t, p in self.timeline if p.get('type') in {'audio.phrase', 'audio.begin'} and (turn is None or p.get('turn_id') == turn)]

    def first_audible(self, turn):
        found = self.audible(turn)
        return found[0][0] if found else None

    def journal(self, kind=None):
        return [r for r in self.store.events('a', 0, 500) if kind is None or r['kind'] == kind]

    def accepted_turns(self):
        return [r['turn'] for r in self.journal('user.accepted')]

    def jobs(self):
        return self.store.jobs('a')

    def epoch(self):
        return self.store.session(self.session['id'], 'browser')['epoch']

    def log(self, kind):
        return [item for item in getattr(self.sidecar, 'log', []) if item[0] == kind]

    # ---- invariants and metrics
    def assert_invariants(self):
        """Properties that must hold in every scenario regardless of what the scripted providers did."""
        epochs = [p['epoch'] for _, p in self.timeline if 'epoch' in p]
        assert epochs == sorted(epochs), 'event epochs went backwards: %r' % epochs
        admitted = {}
        for time, payload in self.timeline:
            if payload.get('type') == 'candidate.admitted':
                admitted[payload['previous_epoch']] = time
        for time, payload in self.timeline:
            if payload.get('type') in {'audio.phrase', 'audio.begin', 'audio.chunk', 'text.delta'} and payload['epoch'] in admitted:
                assert time <= admitted[payload['epoch']], 'output for revoked epoch %s at %.2f (revoked %.2f)' % (payload['epoch'], time, admitted[payload['epoch']])
        phrases = [p['phrase_id'] for _, p in self.timeline if p.get('type') == 'audio.phrase']
        assert len(phrases) == len(set(phrases)), 'a phrase was delivered twice'
        generated = [r['turn'] for r in self.journal('assistant.generated')]
        assert len(generated) == len(set(generated)), 'a turn was settled twice'

    def metrics(self):
        """Ground-truth driven rates. `truth` maps an audio tag to 'ignore' | 'interrupt' | 'answer'."""
        outcome = {'false_cuts': 0, 'false_responses': 0, 'missed_interruptions': 0, 'missed_answers': 0,
                   'unauthorized_actions': 0, 'interrupt_latency': [], 'first_audible': []}
        accepted = set(self.accepted_turns())
        admitted = {p['turn_id']: t for t, p in self.timeline if p.get('type') == 'candidate.admitted'}
        for item in self.arrivals:
            label = self.truth.get(item['tag'])
            if not label:
                continue
            took = item['turn'] in admitted or item['turn'] in accepted
            if label == 'ignore' and took:
                outcome['false_cuts' if item['turn'] in admitted and item['turn'] not in accepted else 'false_responses'] += 1
            if label == 'interrupt':
                if item['turn'] in admitted:
                    outcome['interrupt_latency'].append(round(admitted[item['turn']] - item['time'], 3))
                elif not took:
                    outcome['missed_interruptions'] += 1
            if label == 'answer' and item['final']:
                if item['turn'] not in accepted:
                    outcome['missed_answers'] += 1
                else:
                    first = self.first_audible(item['turn'])
                    if first is not None:
                        outcome['first_audible'].append(round(first - item['time'], 3))
        return outcome

    async def close(self):
        try:
            await self.handler.on_disconnect('browser')
        finally:
            self.patch.stop(); self.store.close(); self.tmp.cleanup()


# --------------------------------------------------------------------------- baseline report
RESULTS = {}


def record(name, **values):
    """Collect per-scenario numbers; written when CONVO_BEHAVIOR_REPORT names a file."""
    RESULTS[name] = values


def write_report():
    path = os.environ.get('CONVO_BEHAVIOR_REPORT')
    if path and RESULTS:
        Path(path).write_text(json.dumps(RESULTS, indent=2, sort_keys=True), encoding='utf-8')
