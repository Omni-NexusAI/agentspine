// Playback that cannot go silent unnoticed (browser side): bounds that a long reply fits in, a phrase that never starts released so
// it cannot block the next one, the output following the system default device and resumed when the browser paused it (or said
// to be paused until a click), every failure in words that say the session goes on, and a test sound through the same output.
// The audio graph is a recording stub: this proves the logic, not what reaches a speaker.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { AudioSession, PCM_AHEAD_SECONDS, PCM_MAX_SOURCES, STUCK_PHRASE_MS, TONE, sinkLabel } from '../webui/audio.js';
import { storeFixture } from './store-fixture.mjs';

const settle = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
const plain = value => JSON.parse(JSON.stringify(value));
const NEXT = /Convo keeps listening; the next phrase plays normally\.$/;

function pcmRig(options = {}) {
    const events = [], sources = [], errors = [];
    const audio = new AudioSession({ onError: error => errors.push(error.message), ...options });
    audio.context = { currentTime: 0, state: 'running', destination: {}, async close() {},
        decodeAudioData: () => assert.fail('Raw PCM must not use buffered audio decoder'),
        createBuffer: (_, length, rate) => ({ duration: length / rate, values: new Float32Array(length), getChannelData() { return this.values; } }),
        createBufferSource: () => { const source = { connect() { return this; }, disconnect() {}, start(when) { this.when = when; }, stop() { this.stopped = true; } }; sources.push(source); return source; } };
    const phrase = (id = 'p') => ({ session_id: 's', epoch: 0, turn_id: 't', phrase_id: id, format: 'pcm_s16le', channels: 1, sample_rate: 24000 });
    const begin = (id = 'p') => audio.pcmBegin(phrase(id), () => events.push(id + ':completed'), () => events.push(id + ':started'));
    const chunk = (id, sequence, bytes = Buffer.alloc(4800)) => audio.pcmChunk({ ...phrase(id), sequence, audio: bytes.toString('base64') });
    const end = (id, chunks) => audio.pcmEnd({ ...phrase(id), chunks });
    const finish = source => { audio.context.currentTime = Math.max(audio.context.currentTime, source.when + source.buffer.duration); source.onended?.(); };
    return { audio, events, sources, errors, begin, chunk, end, finish };
}

test('A long reply fits: 90 s may be scheduled ahead and 4096 sources at once; past either bound the queue is dropped and said so', () => {
    assert.deepEqual([PCM_AHEAD_SECONDS, PCM_MAX_SOURCES], [90, 4096]);
    const ahead = pcmRig();
    ahead.begin();
    for (let n = 0; n < 124; n++) assert.equal(ahead.chunk('p', n, Buffer.alloc(32768)), true, `chunk ${n}`);   // 84.7 s of 24 kHz audio
    assert.deepEqual(ahead.errors, []);
    for (let n = 124; n < 140 && !ahead.errors.length; n++) ahead.chunk('p', n, Buffer.alloc(32768));
    assert.match(ahead.errors[0], /fell more than 90 seconds behind/);
    assert.match(ahead.errors[0], NEXT);
    const many = pcmRig();
    many.begin();
    for (let n = 0; n < PCM_MAX_SOURCES; n++) many.chunk('p', n, Buffer.from([0, 0]));
    assert.deepEqual([many.errors.length, many.audio.pcmSources.size], [0, PCM_MAX_SOURCES]);
    many.chunk('p', PCM_MAX_SOURCES, Buffer.from([0, 0]));
    assert.match(many.errors[0], /more audio queued than it can hold/);
    assert.equal(many.audio.pcmSources.size, 0);
});

test('A phrase that never starts while the context runs is released after its wait, and never blocks the next phrase', async () => {
    assert.equal(STUCK_PHRASE_MS, 4000);
    const f = pcmRig({ stuckMs: 300 });                                  // the context "runs" but its clock never moves
    f.begin('p1'); f.chunk('p1', 0);
    assert.equal(f.audio.pcm.get('p1').budget, 20 + 300);                // due in 20 ms, then the wait
    await settle(30);
    assert.equal(f.audio.pcm.size, 1);                                   // still within its wait
    for (let n = 0; n < 200 && f.audio.pcm.size; n++) await settle(10);
    assert.equal(f.audio.pcm.size, 0);
    assert.equal(f.errors.length, 1);
    assert.match(f.errors[0], /never started playing \(the audio output did not advance\), so it was skipped/);
    assert.match(f.errors[0], NEXT);
    assert.deepEqual(f.events, []);                                      // treated as interrupted: nothing acknowledged
    assert.equal(f.sources[0].stopped, true);
    assert.equal(f.chunk('p1', 1), false);                               // the rest of a released phrase is ignored, not a new failure
    assert.equal(f.end('p1', 2), false);
    assert.equal(f.errors.length, 1);
    f.audio.context.currentTime = 1;                                     // the output moves again: the next phrase plays and completes
    assert.equal(f.begin('p2'), true);
    f.chunk('p2', 0); f.end('p2', 1);
    f.finish(f.sources.at(-1));
    assert.deepEqual(f.events, ['p2:started', 'p2:completed']);
    f.audio.interrupt();
});

test('A paused context is not a stuck phrase: the wait only counts while the context runs', async () => {
    const f = pcmRig({ stuckMs: 40 });
    f.audio.context.state = 'suspended';
    f.begin('p'); f.chunk('p', 0);
    await settle(150);
    assert.deepEqual([f.audio.pcm.size, f.errors.length], [1, 0]);
    f.audio.context.state = 'running'; f.audio.context.currentTime = .5;
    await settle(40);
    assert.deepEqual(f.events, ['p:started']);
    f.audio.interrupt();
});

test('A stale finished phrase (its audio should have ended long ago) never refuses the next phrase', () => {
    const f = pcmRig({ stuckMs: 40 });
    f.begin('old'); f.chunk('old', 0); f.end('old', 1);
    f.audio.context.currentTime = .03; f.sources[0].onended = null;      // it started, and its end was never reported
    f.audio.pcm.get('old').didStart = true;
    f.audio.context.currentTime = 5;
    assert.equal(f.begin('new'), true);
    assert.match(f.errors[0], /stopped advancing before its end/);
    assert.deepEqual([...f.audio.pcm.keys()], ['new']);
    assert.equal(f.audio.pcmBegin({ session_id: 's', epoch: 0, turn_id: 't', phrase_id: 'other', format: 'wav', channels: 1, sample_rate: 24000 }, () => {}), false);
    assert.match(f.errors.at(-1), /overlapped another or had an audio format it cannot play/);   // a real refusal still says what it was
    f.audio.interrupt();
});

test('One failed phrase is reported once: the rest of it (and of the phrase it stopped) is ignored, and the next phrase plays', () => {
    const f = pcmRig();
    f.begin('a'); f.chunk('a', 0);                                       // a phrase is playing ...
    assert.equal(f.begin('b'), false);                                   // ... and another begins over it: refused, playback stopped
    assert.equal(f.errors.length, 1);
    assert.equal(f.chunk('b', 0), false); assert.equal(f.end('b', 1), false);   // the refused phrase's own audio arrives anyway
    assert.equal(f.chunk('a', 1), false); assert.equal(f.end('a', 2), false);   // and the rest of the one it stopped
    assert.equal(f.errors.length, 1);                                    // once, not once per chunk
    assert.equal(f.begin('c'), true);
    f.chunk('c', 0); f.end('c', 1); f.finish(f.sources.at(-1));
    assert.deepEqual(f.events, ['c:started', 'c:completed']);
    f.audio.interrupt();
});

test('A buffered phrase whose output stalls is released after its length plus the wait, and the next one plays', async () => {
    const errors = [], done = [];
    const audio = new AudioSession({ onError: error => errors.push(error.message), stuckMs: 300 });
    const sources = [];
    audio.context = { currentTime: 0, state: 'running', destination: {}, async close() {},
        decodeAudioData: async () => ({ duration: .02 }),
        createBufferSource: () => { const source = { connect() { return this; }, disconnect() {}, start() {}, stop() { this.stopped = true; } }; sources.push(source); return source; } };
    audio.enqueue('AAAA', () => done.push('w1:completed'), () => done.push('w1:started'), 'w1');
    audio.enqueue('AAAA', () => done.push('w2:completed'), () => done.push('w2:started'), 'w2');
    await settle(10);
    assert.deepEqual(done, ['w1:started']);
    await settle(30);
    assert.deepEqual(errors, []);                                        // 20 ms of audio plus a 300 ms wait: not yet
    for (let n = 0; n < 300 && !done.includes('w2:started'); n++) await settle(5);
    assert.match(errors[0], /stopped advancing before its end/);
    assert.match(errors[0], NEXT);
    assert.equal(sources[0].stopped, true);
    assert.deepEqual(done, ['w1:started', 'w2:started']);                // the stalled one was not acknowledged; the next one plays
    sources[1].onended();
    assert.deepEqual(done, ['w1:started', 'w2:started', 'w2:completed']);
    audio.close();
});

test('A playback failure says what happened and that the conversation goes on, for every kind', async () => {
    const decode = new AudioSession({ onError: error => errors.push(error.message) }), errors = [];
    decode.context = { decodeAudioData: async () => { throw new Error('Unable to decode audio data'); }, state: 'running', async close() {} };
    decode.enqueue('AAAA', () => {}, () => {}, 'w');
    await settle(5);
    assert.match(errors[0], /^A reply phrase could not be decoded \(Unable to decode audio data\), so it was skipped\. /);
    const full = new AudioSession({ onError: error => errors.push(error.message) });
    full.queue = new Array(24).fill({});
    full.enqueue('AAAA', () => {});
    assert.match(errors[1], /^Speech playback fell behind, so the queued phrases were dropped\. /);
    const rig = pcmRig();
    rig.begin(); rig.chunk('p', 1);
    assert.match(rig.errors[0], /^Speech playback lost part of a reply phrase \(a missing or out-of-order chunk\)/);
    for (const message of [...errors, ...rig.errors]) assert.match(message, NEXT);
    decode.close(); full.close();
});

// ---- the output itself: device, pause, resume, test sound ----------------------------------------------------------------------

async function withBrowser(run, { setSinkId = true } = {}) {
    const keys = ['navigator', 'AudioContext', 'AudioWorkletNode', 'document', 'addEventListener', 'removeEventListener'];
    const saved = Object.fromEntries(keys.map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
    const listeners = {}, log = { sinks: [], suspends: 0, resumes: 0, connects: [], disconnects: [], contexts: [] };
    const on = scope => (type, fn) => { (listeners[scope + ':' + type] ||= []).push(fn); };
    const off = scope => (type, fn) => { listeners[scope + ':' + type] = (listeners[scope + ':' + type] || []).filter(item => item !== fn); };
    class Node { constructor(name) { this.name = name; } connect(target) { log.connects.push([this.name, target?.name]); return target; } disconnect(target) { log.disconnects.push([this.name, target?.name]); } }
    globalThis.AudioContext = class {
        constructor() { this.sampleRate = 48000; this.currentTime = 1; this.state = 'running'; this.sinkId = ''; this.destination = new Node('destination'); this.audioWorklet = { addModule: async () => {} };
                        this.resumeWorks = true; log.contexts.push(this); if (setSinkId) this.setSinkId = async id => { log.sinks.push(id); this.sinkId = id; }; }
        createGain() { const gain = new Node('gain'); gain.gain = { value: 1, setValueAtTime() {}, linearRampToValueAtTime() {} }; return gain; }
        createMediaStreamSource() { return new Node('source'); }
        createOscillator() { const osc = new Node('oscillator'); osc.frequency = { value: 0 }; osc.start = at => { osc.started = at; }; osc.stop = at => { osc.stopAt = at; }; log.oscillator = osc; return osc; }
        async suspend() { log.suspends++; this.state = 'suspended'; }
        resume() { log.resumes++; if (!this.resumeWorks) return new Promise(() => {}); this.state = 'running'; return Promise.resolve(); }
        async close() { this.state = 'closed'; }
    };
    globalThis.AudioWorkletNode = class extends Node { constructor() { super('capture'); this.port = { postMessage() {} }; } };
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: {
        getUserMedia: async () => ({ getTracks: () => [{ stop() {} }], getAudioTracks: () => [{ getSettings: () => ({ echoCancellation: true }) }] }),
        addEventListener: on('devices'), removeEventListener: off('devices') } } });
    globalThis.document = { hidden: false, addEventListener: on('document'), removeEventListener: off('document') };
    globalThis.addEventListener = on('window'); globalThis.removeEventListener = off('window');
    const fire = (key, event = {}) => { for (const fn of [...(listeners[key] || [])]) fn(event); };
    try { await run({ log, listeners, fire }); } finally {
        for (const key of keys) { if (saved[key]) Object.defineProperty(globalThis, key, saved[key]); else delete globalThis[key]; }
    }
}

test('A change of audio devices moves playback to the system default with setSinkId(\'\'); without it nothing is reopened, the state is reported', async () => {
    for (const sink of [true, false]) await withBrowser(async ({ log, fire }) => {
        const states = [];
        const audio = new AudioSession({ echoGuard: 'off', onStart() {}, onTurn() {}, onPlaybackState: info => states.push(info), resumeWaitMs: 10 });
        await audio.start();
        const resumes = log.resumes;
        fire('devices:devicechange');
        await settle(20);
        if (sink) assert.deepEqual(log.sinks, ['']);
        else {
            // No suspend/resume "reopen" (on Android it flips the phone between its call and media streams) and no reconnect.
            assert.deepEqual([log.suspends, log.sinks.length, log.resumes - resumes], [0, 0, 0]);
            assert.ok(!log.disconnects.some(([from, to]) => from === 'gain' && to === 'destination'));
        }
        assert.deepEqual(plain(states.at(-1)), { state: 'running', sink: 'default', latency: 0, paused: false });
        audio.close();
        fire('devices:devicechange'); await settle(20);
        assert.equal(log.sinks.length, sink ? 1 : 0);                    // closed: no longer listening
    }, { setSinkId: sink });
});

test('A paused output is resumed on return, focus or the next gesture; one that stays paused is reported once until a click resumes it', async () => {
    await withBrowser(async ({ log, fire }) => {
        const states = [];
        const audio = new AudioSession({ echoGuard: 'off', onStart() {}, onTurn() {}, onPlaybackState: info => states.push(info.paused), resumeWaitMs: 10 });
        await audio.start();
        const context = log.contexts[0];
        context.resumeWorks = false; context.state = 'suspended';
        context.onstatechange();                                         // the browser paused it (a background window, another app)
        await settle(30);
        assert.equal(states.at(-1), true);                               // resume was not allowed: reported as paused
        globalThis.document.hidden = true; fire('document:visibilitychange'); await settle(20);
        const tries = log.resumes;
        assert.equal(states.at(-1), true);
        context.resumeWorks = true;
        fire('document:pointerdown');                                    // the next click (an orb press is one) resumes it
        await settle(5);
        assert.equal(log.resumes, tries + 1);
        assert.deepEqual([context.state, states.at(-1)], ['running', false]);
        fire('document:keydown'); await settle(5);
        assert.equal(log.resumes, tries + 1);                            // running: a gesture does nothing
        context.state = 'interrupted'; globalThis.document.hidden = false;
        fire('window:focus'); await settle(5);                           // Safari's 'interrupted' is resumed on focus too
        assert.equal(context.state, 'running');
        audio.close();
        assert.equal(context.onstatechange, null);
    });
});

test('The test sound plays through the session\'s own output gain, or through a temporary context closed afterwards', async () => {
    assert.ok(TONE.peak <= .1 && TONE.seconds <= .5);                    // short and soft
    await withBrowser(async ({ log }) => {
        const levels = [];
        const audio = new AudioSession({ echoGuard: 'off', onStart() {}, onTurn() {}, onLevel: (kind, value) => levels.push(kind) });
        await audio.start();
        const result = await audio.testTone();
        assert.deepEqual(plain(result), { ok: true, path: 'conversation', state: 'running', sink: 'default', latency: 0 });
        assert.equal(log.oscillator.frequency.value, TONE.hz);
        const connected = log.connects.filter(([from]) => from === 'oscillator');
        assert.deepEqual(connected, [['oscillator', 'gain']]);           // into its envelope, which feeds the session's output gain
        assert.ok(log.connects.some(([from, to]) => from === 'gain' && to === 'gain'));
        log.contexts[0].state = 'suspended'; log.contexts[0].resumeWorks = false;
        audio.resumeWaitMs = 5;
        const paused = await audio.testTone();
        assert.equal(paused.ok, false);
        assert.match(paused.message, /audio output is suspended; click again to resume it/);
        audio.close();
        const temporary = await AudioSession.testTone();
        assert.deepEqual([temporary.ok, temporary.path], [true, 'temporary']);
        assert.equal(log.contexts.length, 2);
        await settle(TONE.seconds * 1000 + 300);
        assert.equal(log.contexts[1].state, 'closed');                   // the temporary context is closed again
    });
    assert.deepEqual([sinkLabel({ sinkId: '' }), sinkLabel({ sinkId: 'usb-headset' }), sinkLabel({ sinkId: { type: 'none' } }), sinkLabel({})], ['default', 'usb-headset', 'none', 'default']);
});

// ---- the store ---------------------------------------------------------------------------------------------------------------

test('The store says "Audio paused — click to resume" once, offers the resume, and forgets it once the output runs again', async () => {
    const toasts = [];
    const f = await storeFixture({ globals: { justToast: (message, kind) => toasts.push([message, kind]) } });
    await f.store.start();
    const audio = f.audios.at(-1);
    audio.ensureRunning = async () => { audio.resumed = (audio.resumed || 0) + 1; return true; };
    audio.onPlaybackState({ state: 'suspended', paused: true });
    audio.onPlaybackState({ state: 'suspended', paused: true });
    assert.equal(f.store.audioPaused, true);
    assert.deepEqual(plain(toasts), [['Audio paused — click to resume', 'info']]);   // said once, not on every report
    assert.equal(await f.store.resumeAudio(), true);
    assert.equal(audio.resumed, 1);
    audio.onPlaybackState({ state: 'running', paused: false });
    assert.equal(f.store.audioPaused, false);
    audio.onPlaybackState({ state: 'suspended', paused: true });
    await f.store.stop();
    assert.equal(f.store.audioPaused, false);                             // nothing to resume without a session
    assert.equal(await f.store.resumeAudio(), false);
    const html = await readFile(new URL('../webui/main.html', import.meta.url), 'utf8');
    assert.match(html, /<button type="button" class="button convo-resume" x-show="\$store\.convo\.audioPaused" @click="\$store\.convo\.resumeAudio\(\)">[\s\S]*?Audio paused — click to resume<\/button>/);
});

test('While audio is paused the orb\'s tap resumes it instead of stopping; a second tap stops; the panel\'s Stop always stops', async () => {
    const f = await storeFixture();
    await f.store.start();
    const audio = f.audios.at(-1);
    audio.ensureRunning = async () => { audio.resumed = (audio.resumed || 0) + 1; return false; };   // the output stays paused
    audio.onPlaybackState({ state: 'suspended', paused: true });
    await f.store.toggle();                                              // the orb's tap (and the hotkey) call toggle()
    assert.deepEqual([audio.resumed, f.store.active], [1, true]);
    await f.store.toggle();                                              // still paused: this time it stops, nothing traps the user
    assert.equal(f.store.active, false);
    await f.store.start();
    const next = f.audios.at(-1);
    next.ensureRunning = async () => true;
    next.onPlaybackState({ state: 'suspended', paused: true });
    next.onPlaybackState({ state: 'running', paused: false });
    await f.store.toggle();                                              // running again: an ordinary tap stops
    assert.equal(f.store.active, false);
    const html = await readFile(new URL('../webui/main.html', import.meta.url), 'utf8');
    assert.match(html, /class="button convo-primary" @click="\$store\.convo\.active \|\| \$store\.convo\.busy \? \$store\.convo\.stop\(\) : \$store\.convo\.toggle\(\)"/);
});

test('A playback failure shows as an error that clears itself once the next phrase plays; other errors stay', async () => {
    const f = await storeFixture();
    await f.store.start();
    const audio = f.audios.at(-1), send = event => f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, ...event } });
    audio.onError(new Error('A reply phrase never started playing (the audio output did not advance), so it was skipped. Convo keeps listening; the next phrase plays normally.'));
    assert.match(f.store.error, /never started playing/);
    send({ type: 'audio.begin', turn_id: 't', phrase_id: 'p2' });
    audio.started();
    assert.equal(f.store.error, '');
    f.store.error = 'Turn policy unavailable; this turn was not answered.';
    send({ type: 'audio.phrase', turn_id: 't', phrase_id: 'p3', audio: 'UklGRg==' });
    audio.started();
    assert.equal(f.store.error, 'Turn policy unavailable; this turn was not answered.');
    await f.store.stop();
});

test('The test sound goes through the session when there is one, and through the audio module\'s temporary output when not', async () => {
    const f = await storeFixture();
    const none = await f.store.playTestSound();
    assert.equal(none.ok, false);                                         // the stub AudioSession class has no temporary output
    await f.store.start();
    const audio = f.audios.at(-1);
    audio.testTone = async () => ({ ok: true, path: 'conversation', sink: 'default' });
    assert.deepEqual(plain(await f.store.playTestSound()), { ok: true, path: 'conversation', sink: 'default' });
    audio.testTone = async () => { throw new Error('graph gone'); };
    assert.match((await f.store.playTestSound()).message, /graph gone/);
    await f.store.stop();
});
