// Mute and push to talk: the microphone track is off and the capture is fed nothing while the conversation and its playback go on; what
// was being said is dropped (let go of push-to-talk: sent); unmuting starts clean, with the echo guard's learned path and nothing from
// before. The real AudioSession over a recording graph, the real EchoGuard, and the real store over stubbed host modules.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { AudioSession, SpeechBoundary } from '../webui/audio.js';
import { EchoGuard } from '../webui/echo-guard.js';
import { storeFixture } from './store-fixture.mjs';
import { delayed, gainDb, voiced } from './signals.mjs';

const settle = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
const RATE = 48000;

// ---- the capture
async function withBrowser(run) {
    const saved = { navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator'), AudioContext: globalThis.AudioContext, AudioWorkletNode: globalThis.AudioWorkletNode };
    const log = { track: { enabled: true, getSettings: () => ({ echoCancellation: true }), addEventListener() {}, removeEventListener() {} }, wires: [], posted: [], node: null };
    class Node {
        constructor(name) { this.name = name; this.gain = { value: 1 }; }
        connect(target, output = 0, input = 0) { log.wires.push([this.name, target.name, input]); return target; }
        disconnect(target) { const keep = log.wires.filter(([from, to]) => from !== this.name || (target && to !== target.name)); log.wires.splice(0, log.wires.length, ...keep); }
    }
    globalThis.AudioContext = class {
        constructor() { this.sampleRate = RATE; this.currentTime = 0; this.state = 'running'; this.destination = new Node('destination'); this.audioWorklet = { addModule: async () => {} }; }
        createGain() { return new Node('gain'); } createMediaStreamSource() { return new Node('source'); } async resume() {} async close() { this.state = 'closed'; }
    };
    globalThis.AudioWorkletNode = class extends Node {
        constructor() { super('capture'); log.node = this; this.port = { postMessage: message => log.posted.push(message) }; }
    };
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }], getAudioTracks: () => [log.track] }) } } });
    try { await run(log); } finally {
        if (saved.navigator) Object.defineProperty(globalThis, 'navigator', saved.navigator); else delete globalThis.navigator;
        for (const key of ['AudioContext', 'AudioWorkletNode']) { if (saved[key] === undefined) delete globalThis[key]; else globalThis[key] = saved[key]; }
    }
}
const wired = log => log.wires.some(([from, to]) => from === 'source' && to === 'capture');
const frame = () => new Float32Array(1920).fill(.05);
// One message of the capture: 40 ms of released voiced microphone (guard) or a 2048-sample block (echo protection Off).
const say = (log, guarded, count = 14) => { for (let n = 0; n < count; n++) log.node.port.onmessage({ data: guarded ? { kind: 'audio', frames: [frame()], voiced: [true], levels: [.05], dropped: 0, floor: .004 } : voiced(1920 / RATE, RATE, { rms: .1, seed: n + 1 }) }); };
function session(log, options = {}) {
    const events = [];
    const audio = new AudioSession({ maximum: 20, onStart: () => events.push('start'), onTurn: () => events.push('turn'), onProbe: () => events.push('probe'), onLevel: (kind, value) => { if (kind === 'input') events.push(['level', value]); }, ...options });
    return { audio, events, kinds: () => events.filter(item => typeof item === 'string') };
}

test('Muting turns the track off and takes the microphone out of the capture; nothing said then is heard, metered or sent; the output stays', async () => {
    for (const echoGuard of ['adaptive', 'off']) await withBrowser(async log => {
        const { audio, events, kinds } = session(log, { echoGuard });
        await audio.start();
        assert.deepEqual([log.track.enabled, wired(log)], [true, true]);
        say(log, true && echoGuard !== 'off', 6);                                                // some speech, not yet admitted
        const interrupted = audio.playbackGeneration;
        assert.equal(audio.setMuted(true), true);
        assert.equal(audio.setMuted(true), false);                                               // once
        assert.deepEqual([log.track.enabled, wired(log), audio.boundary.chunks.length, audio.boundary.ring.length], [false, false, 0, 0], echoGuard);
        assert.deepEqual(events.filter(Array.isArray).at(-1), ['level', 0]);
        if (echoGuard !== 'off') assert.ok(log.wires.some(([from, to, input]) => from === 'gain' && to === 'capture' && input === 1), 'the playback reference is left in place');
        events.length = 0;
        say(log, echoGuard !== 'off', 40);                                                       // blocks that were on their way
        assert.deepEqual(events, []);
        assert.equal(audio.playbackGeneration, interrupted, 'playback was not interrupted');
        assert.equal(audio.setMuted(false), true);
        assert.deepEqual([log.track.enabled, wired(log), audio.boundary.chunks.length], [true, true, 0]);
        if (echoGuard !== 'off') { say(log, true, 14); assert.deepEqual(kinds().slice(0, 1), ['start']); }
        audio.close();
        assert.equal(audio.setMuted(true), false);
    });
});

test('Unmuting resets the echo guard’s held frames and calibration and gives its learned echo path back; the end of a duck is not left waiting', async () => {
    await withBrowser(async log => {
        const { audio } = session(log);
        await audio.start();
        const state = { inputRate: RATE, filter: new Float32Array(256), delaySamples: 40, delayConfidence: .8, modelSamples: 9000 };
        log.node.port.onmessage({ data: { kind: 'echo_state', state } });
        audio.duck.request(true); audio.quietRelease = 400;
        audio.setMuted(true);
        assert.equal(audio.quietRelease, null);
        log.posted.length = 0;
        audio.setMuted(false);
        assert.deepEqual(log.posted.map(message => message.kind), ['echo_reset', 'echo_restore']);
        assert.equal(log.posted[1].state, state);
        audio.close();
    });
    await withBrowser(async log => {                                                             // nothing learned yet: only the reset
        const { audio } = session(log);
        await audio.start(); audio.setMuted(true); log.posted.length = 0; audio.setMuted(false);
        assert.deepEqual(log.posted.map(message => message.kind), ['echo_reset']);
        audio.close();
    });
});

test('A session that starts muted opens nothing to the capture until it is unmuted, whatever was pressed while it started', async () => {
    await withBrowser(async log => {
        const { audio } = session(log, { muted: true });
        await audio.start();
        assert.deepEqual([log.track.enabled, wired(log)], [false, false]);
        audio.setMuted(false);
        assert.deepEqual([log.track.enabled, wired(log)], [true, true]);
        audio.close();
    });
    await withBrowser(async log => {
        const { audio } = session(log);
        const starting = audio.start();
        audio.setMuted(true);                                                                    // before there is a microphone
        await starting;
        assert.deepEqual([log.track.enabled, wired(log)], [false, false]);
        audio.close();
    });
});

test('Letting go of push to talk completes what was said as a turn at once; too little said is dropped', async () => {
    await withBrowser(async log => {
        const { audio, kinds } = session(log);
        await audio.start();
        say(log, true, 14);
        assert.deepEqual(kinds(), ['start']);
        audio.setMuted(true, { finish: true });
        assert.deepEqual(kinds(), ['start', 'turn']);                                            // no silence has to pass first
        assert.equal(audio.boundary.chunks.length, 0);
        assert.ok(audio.boundary.lastTurn.voiced > .45);
        audio.setMuted(false);
        say(log, true, 3);
        audio.setMuted(true, { finish: true });
        assert.deepEqual(kinds(), ['start', 'turn']);
        audio.setMuted(false); audio.close();
    });
    const turns = [], boundary = new SpeechBoundary({ rate: 16000, onStart() {}, onTurn: chunks => turns.push(chunks.length) });
    assert.equal(boundary.finish(), false);
    assert.equal(boundary.lastTurn, null);
});

test('The assistant’s own voice is not taken for the user’s after a mute: the guard is fed nothing, and what it learned is kept', () => {
    // A reply plays through a speaker; the microphone hears it 90 ms later, 12 dB down, and nobody speaks.
    const reply = voiced(14, RATE, { rms: .12, seed: 5 }), echo = gainDb(delayed(reply, .09, RATE), -12).subarray(0, reply.length);
    const guard = new EchoGuard({ inputRate: RATE, mode: 'adaptive', nativeAec: false, gateDb: -50 });
    const feed = (from, to, mic = echo) => {
        let released = 0;
        for (let at = Math.round(from * RATE); at + 128 <= Math.round(to * RATE); at += 128) for (const step of guard.ingest(mic.subarray(at, at + 128), reply.subarray(at, at + 128))) {
            step.frames.forEach((_, index) => { if (step.voiced[index]) released++; });
        }
        return released;
    };
    feed(0, 6);
    const learned = guard.exportState();
    guard.reset(); assert.equal(guard.importState(learned), true);                              // muted from 6 s to 9 s: nothing at all was fed
    assert.equal(feed(9, 13.9), 0, 'no voiced frame of the reply is released');
    assert.equal(guard.exportState().delaySamples, learned.delaySamples);
});

// ---- the store
async function rig(more = {}) {
    const f = await storeFixture(more);
    f.capture = () => f.audios.at(-1);
    f.send = event => f.handlers['convo.event']({ data: { session_id: 's', epoch: f.store.session?.epoch ?? 0, ...event } });
    return f;
}
// The stub capture of store-fixture.mjs, with a mute that records and behaves as the real one.
function mutable(audio) {
    audio.mutes = [];
    audio.setMuted = (muted, { finish = false } = {}) => { audio.mutes.push([muted, finish]); if (muted && finish && audio.said) { const value = audio.said; audio.said = null; audio.onTurn(value, {}); } audio.muted = muted; return true; };
    return audio;
}
const candidates = f => f.requests.filter(item => item.event === 'convo.candidate').map(item => item.data);

test('Mute needs a running conversation, shows on the chip and the orb, keeps the conversation, and ends with it', async () => {
    const f = await rig();
    assert.equal(f.store.setMuted(true), false);
    await f.store.start();
    const audio = mutable(f.capture());
    assert.equal(f.store.toggleMute(), true);
    assert.deepEqual([f.store.muted, f.store.micMuted, f.store.statusLabel, f.store.chipState, f.mic.attrs.get('data-convo')], [true, true, 'Muted', 'muted', 'muted']);
    assert.match(f.mic.attrs.get('aria-label'), /^Convo muted\. Microphone off;/);
    assert.equal(f.mic.attrs.get('aria-pressed'), 'true');
    assert.deepEqual(audio.mutes, [[true, false]]);
    f.send({ type: 'state', state: 'speaking' });
    assert.equal(f.store.statusLabel, 'Muted · speaking');                                      // the reply goes on and is said so
    f.send({ type: 'audio.begin', turn_id: 't', phrase_id: 'p' });
    assert.equal(audio.interrupted ?? 0, 0);
    f.send({ type: 'state', state: 'listening' });
    f.send({ type: 'notice', kind: 'activity', message: 'Searching…' });
    assert.deepEqual([f.store.statusLabel, f.store.chipState], ['Muted', 'muted']);
    audio.onLevel('input', 0);
    assert.equal(f.store.toggleMute(), true);
    assert.deepEqual([f.store.micMuted, f.store.chipState], [false, 'activity']);
    assert.deepEqual(audio.mutes, [[true, false], [false, false]]);
    f.store.setMuted(true);
    await f.store.stop();
    assert.deepEqual([f.store.muted, f.store.talking, f.store.chipState], [false, false, 'ready']);
    await f.store.start();
    assert.deepEqual([f.store.muted, f.capture().muted], [false, false]);
    await f.store.stop();
});

test('The mute survives a chat switch (the new capture starts muted) and a dropped connection', async () => {
    const f = await rig();
    await f.store.start();
    mutable(f.capture());
    f.store.setMuted(true);
    f.store.target = 'b';
    await f.store.retarget();
    assert.equal(f.audios.length, 2);
    assert.equal(f.capture().muted, true);
    assert.deepEqual([f.store.muted, f.store.chipState], [true, 'muted']);
    f.store.setMuted(false);
    assert.equal(f.store.micMuted, false);
    f.store.setMuted(true);
    f.store.reattachRetryMs = 1;
    await f.store.reattach(null);                                                                // (its socket is another one: nothing happens)
    assert.equal(f.store.muted, true);
    await f.store.stop();
});

test('What was being said is dropped: no more probes, no turn, the server is told, and the duck ends', async () => {
    const f = await rig();
    await f.store.start();
    const audio = mutable(f.capture()), holds = [];
    audio.setPolicyHold = (value, immediate) => holds.push([value, immediate]);
    audio.onStart(); audio.onProbe('probe-1', {});
    await settle();
    const [probe] = candidates(f);
    f.send({ type: 'candidate.state', turn_id: probe.turn_id, sequence: 1, state: 'considering' });
    assert.equal(f.store.hearing, true);
    f.store.setMuted(true);
    assert.deepEqual([f.store.hearing, f.store.policyHold, f.store.inputLevel, f.store.meter.level], [false, false, 0, 0]);
    assert.deepEqual(holds.at(-1), [false, true]);
    audio.onProbe('probe-2', {}); audio.onTurn('late-turn', {});                                 // nothing of it can still be sent
    await settle();
    assert.equal(candidates(f).length, 1);
    assert.deepEqual(f.requests.filter(item => item.event === 'convo.candidate_cancel').map(item => [item.data.turn_id, item.data.epoch, item.data.session_id]), [[probe.turn_id, 0, 's']]);
    f.send({ type: 'candidate.state', turn_id: probe.turn_id, sequence: 1, state: 'withhold' });
    assert.equal(f.store.policyHold, false);                                                     // a late decision about it ducks nothing
    assert.equal(f.store.error, '');
    await f.store.stop();
});

test('An interruption the server had already admitted still moves the conversation on, so nothing after it is stale', async () => {
    const f = await rig();
    await f.store.start();
    const audio = mutable(f.capture());
    audio.onStart(); audio.onProbe('probe-1', {});
    await settle();
    f.store.setMuted(true);
    f.handlers['convo.event']({ data: { type: 'candidate.admitted', session_id: 's', previous_epoch: 0, turn_id: candidates(f)[0].turn_id, new_session: { id: 's', epoch: 1 } } });
    assert.equal(f.store.session.epoch, 1);
    assert.equal(audio.interrupted, 1);
    await f.store.stop();
});

test('A server that keeps the dropped utterance open is not answered “busy”: the next utterance carries on under its id; one that dropped it gets a new id', async () => {
    for (const [cancelled, same] of [[false, true], [true, false]]) {
        const f = await rig({ answer: event => (event === 'convo.candidate_cancel' ? (cancelled ? { cancelled: true } : new Error('Unsupported voice event')) : null) });
        await f.store.start();
        const audio = mutable(f.capture());
        audio.onStart(); audio.onProbe('probe-1', {}); await settle();
        audio.onProbe('probe-2', {}); await settle();
        f.store.setMuted(true); await settle();
        f.store.setMuted(false);
        audio.onStart(); audio.onProbe('probe-3', {}); await settle();
        audio.onTurn('turn', {}); await settle();
        const sent = candidates(f);
        assert.equal(sent.at(-1).turn_id === sent[0].turn_id, same, 'cancelled ' + cancelled);
        assert.deepEqual(sent.map(item => item.sequence), same ? [1, 2, 3, 4] : [1, 2, 1, 2]);
        assert.equal(sent.at(-1).final, true);
        assert.equal(f.store.error, '');
        audio.onStart(); audio.onProbe('probe-5', {}); await settle();
        assert.notEqual(candidates(f).at(-1).turn_id, sent.at(-1).turn_id);                      // only the first one after it
        await f.store.stop();
    }
    // Its capture deadline is nearly over (here: short turns, so it always is), or no probe was ever sent: a new id.
    const f = await rig({ settings: { max_audio_seconds: 4 } });
    await f.store.start();
    const audio = mutable(f.capture());
    audio.onStart();
    f.store.setMuted(true);
    assert.equal(f.calls.includes('convo.candidate_cancel'), false);
    f.store.setMuted(false);
    audio.onStart(); audio.onProbe('a', {}); await settle();
    f.store.setMuted(true);
    f.store.setMuted(false); audio.onStart(); audio.onProbe('b', {}); await settle();
    const [first, second] = candidates(f);
    assert.notEqual(first.turn_id, second.turn_id);
    await f.store.stop();
});

test('A completed turn that still waits for its decision was said before the mute: it keeps its hold and its answer', async () => {
    const f = await rig();
    await f.store.start();
    const audio = mutable(f.capture());
    audio.onStart(); audio.onTurn('said-before', {});
    await settle();
    const [turn] = candidates(f);
    f.send({ type: 'candidate.state', turn_id: turn.turn_id, sequence: turn.sequence, final: true, state: 'considering' });
    assert.equal(f.store.policyHold, true);
    f.store.setMuted(true);
    assert.equal(f.store.policyHold, true);
    f.send({ type: 'candidate.state', turn_id: turn.turn_id, sequence: turn.sequence, final: true, state: 'withhold' });
    assert.equal(f.store.policyHold, false);
    await f.store.stop();
});

test('Push to talk opens the microphone while it is held and sends what was said on release; it does nothing unless muted', async () => {
    const f = await rig();
    await f.store.start();
    const audio = mutable(f.capture());
    assert.equal(f.store.pushToTalk(true), false);
    f.store.setMuted(true);
    assert.equal(f.store.pushToTalk(true), true);
    assert.equal(f.store.pushToTalk(true), false);
    assert.deepEqual([f.store.muted, f.store.micMuted, f.store.chipState], [true, false, 'listening']);
    audio.onStart(); audio.said = 'what was said';
    assert.equal(f.store.chipState, 'hearing');
    assert.equal(f.store.pushToTalk(false), true);
    await settle();
    assert.deepEqual([f.store.micMuted, f.store.chipState, f.store.hearing], [true, 'muted', false]);
    assert.deepEqual(candidates(f).map(item => [item.audio, item.final]), [['what was said', true]]);
    assert.equal(f.calls.includes('convo.candidate_cancel'), false);
    assert.equal(f.store.pushToTalk(false), false);
    assert.deepEqual(audio.mutes, [[true, false], [false, false], [true, true]]);
    // Unmuting while it is held leaves the microphone on when it is let go of.
    f.store.pushToTalk(true); f.store.setMuted(false);
    assert.equal(f.store.pushToTalk(false), false);
    assert.equal(f.store.micMuted, false);
    await f.store.stop();
    assert.equal(f.store.pushToTalk(true), false);
});

test('Muted, a finished task’s notice still arrives; a test clip and a raw recording in progress are given up', async () => {
    const jobs = [{ id: 'one', session: 's', target: 'a', status: 'completed', prompt: 'one' }];
    const f = await rig({ settings: { developer_mode: true }, api: (_url, payload) => (payload.action === 'history' ? { events: [], jobs } : undefined) });
    await f.store.start();
    const audio = mutable(f.capture());
    audio.startRawTap = async () => true; audio.takeRawTap = () => null;
    const note = f.store.recordRawAndSent();
    await settle();
    f.store.armClip('request'); audio.onStart();
    f.store.setMuted(true);
    assert.match(f.store.clipNote, /the microphone was muted/);
    assert.match(await note, /Nothing was recorded/);
    f.store.state = 'listening';
    await f.store.history(); await settle(5);
    assert.deepEqual(f.requests.filter(item => item.event === 'convo.notice').map(item => item.data.job_id), ['one']);
    await f.store.stop();
});

test('The panel: Mute beside Start and Stop, only during a conversation, and no Dictate button; dictation still says so and can be ended', async () => {
    const html = await readFile(new URL('../webui/main.html', import.meta.url), 'utf8');
    const controls = html.slice(html.indexOf('<div class="convo-controls convo-stage"'), html.indexOf('<p class="convo-hint"'));
    assert.match(controls, /<button type="button" class="button convo-iconbtn convo-mute" style="--i:0" @click="\$store\.convo\.toggleMute\(\)" :aria-pressed="\$store\.convo\.muted \? 'true' : 'false'"/);
    // The controls exist only for a conversation: tucked behind the orb and out of reach (inert) until one runs.
    assert.match(controls, /<div class="convo-icons" role="group" aria-label="Conversation controls" :inert="!\(\$store\.convo\.active \|\| \$store\.convo\.busy\)">/);
    assert.match(html, /\.convo-stage \.convo-iconbtn\{opacity:0;transform:translateX\([^}]*pointer-events:none/);
    assert.match(html, /\.convo-stage\[data-active=true\] \.convo-iconbtn\{opacity:1;transform:none;pointer-events:auto/);
    // Muted is red, with the struck-through microphone: seen at a glance, and never by colour alone.
    assert.match(html, /\.convo-panel \.convo-mute\[aria-pressed=true\]\{border-color:#e06c6c/);
    assert.match(controls, /x-text="\$store\.convo\.muted \? 'mic_off' : 'mic'"/);
    // The orb is Start and Stop; the icons beside it are the microphone, the assistant's voice, the camera and the screen, in that order.
    assert.ok(controls.indexOf('convo-mute') < controls.indexOf('convo-voice') && controls.indexOf('convo-voice') < controls.indexOf('convo-iconbtn convo-camera')
        && controls.indexOf('convo-iconbtn convo-camera') < controls.indexOf('convo-iconbtn convo-screen'));
    assert.match(controls, /class="button convo-iconbtn convo-voice" style="--i:1" @click="\$store\.convo\.toggleVoice\(\)" :aria-pressed="\$store\.convo\.voiceMuted \? 'true' : 'false'"/);
    for (const name of ['convo-mute', 'convo-voice', 'convo-camera', 'convo-screen'])               // every icon has a name without its label showing
        assert.match(controls, new RegExp(`class="button convo-iconbtn ${name}"[^>]*:aria-label=`), name);
    assert.match(controls, /x-text="\$store\.convo\.muted \? 'Microphone muted' : 'Mute microphone'"/);
    assert.ok(html.indexOf('convo-primary convo-panel-orb') < html.indexOf('convo-iconbtn convo-mute') && controls.indexOf('convo-mute') < controls.indexOf('convo-camera'));
    assert.doesNotMatch(html, /toggleDictation|>Dictate<|'Dictate'/);
    assert.match(await readFile(new URL('../webui/convo-store.js', import.meta.url), 'utf8'), /Dictating · click the composer orb to finish, Escape cancels/);
    assert.match(html, /\.convo-chip\[data-state=muted\] \.convo-dot\{background:transparent;border:/);      // a hollow dot: not colour alone
    const f = await rig();
    await f.store.start();
    await f.store.beginDictation();
    assert.deepEqual([f.store.statusLabel, f.store.chipState, f.store.active], ['Dictating', 'dictation', false]);
    await f.store.toggleDictation();                                                             // the hotkey's way; the orb and Escape are dictation-store.test.mjs
    assert.equal(f.store.dictating, false);
});

test('Voice with a muted microphone: the sound drawer does not measure the room', async () => {
    const { soundStoreDefinition } = await import('../webui/sound-setup.js');
    const convo = { active: true, micMuted: true };
    const store = soundStoreDefinition({ convo: () => convo, setup: () => null });
    assert.equal(store.canMeasure, false);
    convo.micMuted = false;
    assert.equal(store.canMeasure, true);
});
