// One audio output per page (audio.js PageAudio): one AudioContext reused by every conversation, suspended only while no microphone is
// open, recreated only when the browser closed it, no stale capture nodes; the chosen output device followed, with the default while
// it is gone and a return when it comes back; the re-buffering jitter buffer; the raw copy for "Record raw and sent"; the phone audio
// mode and the microphone's processing (sound-setup.js). Recording stubs only: no speaker, microphone or phone.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { AudioSession, PageAudio, REBUFFER_MS, RAW_MAX_SECONDS, sharedPageAudio } from '../webui/audio.js';
import { captureProcessing, isPhone, phoneAudioMode } from '../webui/sound-setup.js';

const settle = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));

function browser({ setSinkId = true, outputs = [] } = {}) {
    const log = { contexts: [], modules: [], nodes: [], sinks: [], suspends: 0, resumes: 0, constraints: [], tracks: 0, connects: [] };
    class Node { constructor(name) { this.name = name; } connect(target) { log.connects.push([this.name, target?.name]); return target; } disconnect() { this.disconnected = true; } }
    class Context {
        constructor() {
            this.sampleRate = 48000; this.currentTime = 1; this.state = 'running'; this.sinkId = ''; this.destination = new Node('destination');
            this.audioWorklet = { addModule: async url => { log.modules.push(url); } }; this.listeners = {};
            log.contexts.push(this);
            if (setSinkId) this.setSinkId = async function (id) { if (this !== log.contexts.at(-1)) throw new TypeError('Illegal invocation'); log.sinks.push(id); this.sinkId = id; };
        }
        addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
        fire(type) { for (const fn of this.listeners[type] || []) fn(); this.onstatechange?.(); }
        createGain() { const gain = new Node('gain'); gain.gain = { value: 1 }; return gain; }
        createMediaStreamSource() { return new Node('source'); }
        async suspend() { log.suspends++; this.state = 'suspended'; this.fire('statechange'); }
        resume() { log.resumes++; const was = this.state; this.state = 'running'; if (was !== 'running') this.fire('statechange'); return Promise.resolve(); }
        async close() { this.state = 'closed'; }
    }
    const devices = { list: outputs, async enumerateDevices() { return this.list; }, addEventListener() {}, removeEventListener() {},
        getUserMedia: async constraints => { log.constraints.push(constraints.audio); log.tracks++;
            return { getTracks: () => [{ stop() {} }], getAudioTracks: () => [{ label: 'USB microphone', getSettings: () => ({ deviceId: 'usb-mic', echoCancellation: constraints.audio.echoCancellation }) }] }; } };
    const saved = { AudioContext: globalThis.AudioContext, AudioWorkletNode: globalThis.AudioWorkletNode, navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator') };
    globalThis.AudioContext = Context;
    globalThis.AudioWorkletNode = class extends Node { constructor(context, name) { super(name); this.port = { onmessage: null, postMessage() {} }; log.nodes.push(this); } };
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: devices } });
    const restore = () => {
        for (const key of ['AudioContext', 'AudioWorkletNode']) { if (saved[key] === undefined) delete globalThis[key]; else globalThis[key] = saved[key]; }
        if (saved.navigator) Object.defineProperty(globalThis, 'navigator', saved.navigator); else delete globalThis.navigator;
    };
    return { log, devices, restore, Context };
}
const session = (page, options = {}) => new AudioSession({ echoGuard: 'off', onStart() {}, onTurn() {}, page, resumeWaitMs: 5, ...options });

test('One context per page: reused by every conversation, suspended only with no microphone open, no stale capture nodes', async () => {
    const b = browser();
    try {
        const page = new PageAudio({ resumeWaitMs: 5 });
        const first = session(page);
        await first.start();
        assert.equal(b.log.contexts.length, 1);
        assert.equal(page.users, 1);
        const capture = b.log.nodes[0];
        first.close();
        assert.equal(capture.port.onmessage, null, 'the closed capture hears nothing more');
        assert.equal(b.log.contexts[0].state, 'suspended', 'no microphone open: suspended, never closed');
        const second = session(page);
        await second.start();                                                  // a second conversation (or a chat switch)
        assert.equal(b.log.contexts.length, 1, 'the same context again');
        assert.equal(b.log.contexts[0].state, 'running');
        assert.deepEqual(b.log.modules, ['/plugins/_convo/webui/audio-worklet.js'], 'the worklet module is added once per context');
        assert.equal(page.snapshot().reopened, 1, 'the resume after a suspend is counted as a reopened output (diagnostics)');
        second.close();
        // A context the browser closed is recreated, and counted.
        b.log.contexts[0].state = 'closed';
        const third = session(page);
        await third.start();
        assert.equal(b.log.contexts.length, 2);
        assert.deepEqual([page.snapshot().created, page.snapshot().recreated], [2, 1]);
        third.close();
        assert.equal(typeof sharedPageAudio().acquire, 'function');
        assert.equal(AudioSession.page(), sharedPageAudio(), 'the store reaches the one page output through the class');
    } finally { b.restore(); }
});

test('The microphone opens with the processing chosen for it: Media turns the browser\'s processing off, Call keeps echo cancelling', async () => {
    const b = browser();
    try {
        const page = new PageAudio({ resumeWaitMs: 5 });
        const media = session(page, { processing: captureProcessing('media', { noise_suppression: true, auto_gain: true }) });
        await media.start(); media.close();
        const call = session(page, { processing: captureProcessing('call', { noise_suppression: false, auto_gain: true }) });
        await call.start();
        assert.equal(call.inputDeviceId(), 'usb-mic', 'the microphone actually in use');
        call.close();
        assert.deepEqual(b.log.constraints.map(c => [c.echoCancellation, c.noiseSuppression, c.autoGainControl]), [[false, false, false], [true, false, true]]);
        assert.equal(b.log.tracks, 2, 'one microphone per conversation, nothing more');
    } finally { b.restore(); }
});

test('Phone audio mode: chosen at the start from the device names, an override per device wins; desktops have none', () => {
    assert.deepEqual(phoneAudioMode({ phone: false, devices: [{ label: 'Headset' }] }).mode, '');
    assert.equal(phoneAudioMode({ phone: true, devices: [{ label: 'Speakerphone' }, { label: 'Earpiece' }] }).mode, 'call');
    const headset = phoneAudioMode({ phone: true, devices: [{ label: 'Speakerphone' }, { label: 'Bluetooth headset' }] });
    assert.deepEqual([headset.mode, headset.source], ['media', 'auto']);
    assert.match(headset.reason, /Bluetooth headset/);
    assert.equal(phoneAudioMode({ phone: true, devices: [{ label: 'Bluetooth headset' }], override: 'call' }).mode, 'call');
    assert.equal(phoneAudioMode({ phone: true, devices: [], override: 'media' }).source, 'override');
    assert.match(phoneAudioMode({ phone: true, devices: [{ label: '' }] }).reason, /permission/);
    assert.equal(isPhone({ userAgent: 'Mozilla/5.0 (Linux; Android 14) Chrome/130' }), true);
    assert.equal(isPhone({ userAgent: 'Mozilla/5.0 (Windows NT 10.0) Chrome/130' }), false);
    assert.equal(isPhone({ get userAgent() { throw new Error('blocked'); } }), false);
    assert.deepEqual(captureProcessing('', {}), { echoCancellation: true, noiseSuppression: true, autoGainControl: true });
});

test('The chosen output device: played to while present, the default while it is gone (said once), back when it returns', async () => {
    const b = browser({ outputs: [{ kind: 'audiooutput', deviceId: 'default', label: 'Default' }, { kind: 'audiooutput', deviceId: 'hdmi', label: 'Monitor speakers' }] });
    try {
        const page = new PageAudio({ resumeWaitMs: 5 });
        const notes = [];
        const audio = session(page, { onOutput: note => notes.push(note.message) });
        await audio.start();
        assert.deepEqual(await page.outputs(), [{ id: 'hdmi', label: 'Monitor speakers' }]);
        const chosen = await page.choose('hdmi');
        assert.deepEqual([chosen.ok, chosen.using, b.log.sinks.at(-1)], [true, 'hdmi', 'hdmi']);
        b.devices.list = [{ kind: 'audiooutput', deviceId: 'default', label: 'Default' }];             // unplugged
        await audio.followDefaultOutput();
        assert.equal(b.log.sinks.at(-1), '', 'the system default while it is gone');
        assert.match(notes.at(-1), /gone, so Convo plays to the system default/);
        await audio.followDefaultOutput();
        assert.equal(notes.length, 1, 'said once');
        b.devices.list.push({ kind: 'audiooutput', deviceId: 'hdmi', label: 'Monitor speakers' });     // back
        await audio.followDefaultOutput();
        assert.equal(b.log.sinks.at(-1), 'hdmi');
        assert.match(notes.at(-1), /back/);
        assert.equal(page.snapshot().fallbacks, 1);
        await page.choose('');
        assert.equal(b.log.sinks.at(-1), '');
        audio.close();
        // A new context (the browser closed the old one) gets the remembered choice at once.
        await page.choose('hdmi'); b.log.contexts[0].state = 'closed';
        page.ensure();
        await settle(5);
        assert.equal(b.log.contexts[1].sinkId, 'hdmi');
    } finally { b.restore(); }
});

test('Where a browser cannot choose an output, the choice is refused in words and the device is never reopened', async () => {
    const b = browser({ setSinkId: false });
    try {
        const page = new PageAudio({ resumeWaitMs: 5 });
        const audio = session(page);
        await audio.start();
        assert.equal(page.supportsSink(), false);
        assert.match((await page.choose('hdmi')).message, /cannot choose/);
        const before = [b.log.suspends, b.log.resumes];
        await audio.followDefaultOutput();
        assert.deepEqual([b.log.suspends, b.log.resumes], before, 'no suspend/resume "reopen"');
        audio.close();
    } finally { b.restore(); }
});

test('The test sound without a conversation plays through the page\'s one context, which is suspended again afterwards', async () => {
    const b = browser();
    try {
        const page = new PageAudio({ resumeWaitMs: 5 });
        b.Context.prototype.createOscillator = function () { const osc = { frequency: { value: 0 }, connect: target => target, start() {}, stop() {}, disconnect() {} }; return osc; };
        b.Context.prototype.createAnalyser = undefined;
        const gain = b.Context.prototype.createGain;
        b.Context.prototype.createGain = function () { const g = gain.call(this); g.gain = { value: 1, setValueAtTime() {}, linearRampToValueAtTime() {} }; g.connect = target => target; return g; };
        const result = await page.testTone();
        assert.deepEqual([result.ok, result.path], [true, 'page']);
        assert.equal(b.log.contexts.length, 1);
        await settle(800);
        assert.equal(b.log.contexts[0].state, 'suspended');
        assert.equal(b.log.contexts.length, 1, 'no temporary second context');
    } finally { b.restore(); }
});

function pcmRig() {
    const events = [], sources = [];
    const audio = new AudioSession({ onError: () => {} });
    audio.context = { currentTime: 0, state: 'running', destination: {}, async close() {},
        createBuffer: (_, length, rate) => ({ duration: length / rate, values: new Float32Array(length), getChannelData() { return this.values; } }),
        createBufferSource: () => { const source = { connect() { return this; }, disconnect() {}, start(when) { this.when = when; } }; sources.push(source); return source; } };
    const phrase = id => ({ session_id: 's', epoch: 0, turn_id: 't', phrase_id: id, format: 'pcm_s16le', channels: 1, sample_rate: 24000 });
    const begin = id => audio.pcmBegin(phrase(id), () => events.push(id + ':completed'), () => events.push(id + ':started'));
    const chunk = (id, sequence) => audio.pcmChunk({ ...phrase(id), sequence, audio: Buffer.alloc(4800).toString('base64') });   // 100 ms each
    const end = (id, chunks) => audio.pcmEnd({ ...phrase(id), chunks });
    return { audio, events, sources, begin, chunk, end };
}

test('Jitter buffer: a chunk that arrives after the scheduled audio ran out re-buffers REBUFFER_MS, then everything plays back to back, nothing dropped', () => {
    assert.equal(REBUFFER_MS, 400);
    const f = pcmRig();
    f.begin('a'); f.chunk('a', 0); f.chunk('a', 1);
    assert.equal(f.sources.length, 2);
    f.audio.context.currentTime = 1.0;                         // a slow request: the 200 ms scheduled ran out long ago
    f.chunk('a', 2);
    assert.equal(f.sources.length, 2, 'held, not played the moment it landed');
    assert.equal(f.audio.underruns, 1);
    f.chunk('a', 3); f.chunk('a', 4);
    assert.equal(f.sources.length, 2, '300 ms held: still re-buffering');
    f.chunk('a', 5);
    assert.equal(f.sources.length, 6, '400 ms: all of it starts');
    const when = f.sources.slice(2).map(source => Math.round(source.when * 1000));
    assert.deepEqual(when, [1020, 1120, 1220, 1320], 'back to back after one pause');
    f.chunk('a', 6);
    assert.equal(f.sources.length, 7, 'then it streams as before');
    // A late chunk near the end: the phrase's end plays what is held at once, never drops it.
    f.audio.context.currentTime = 5;
    f.chunk('a', 7);
    assert.equal(f.sources.length, 7);
    assert.equal(f.end('a', 8), true);
    assert.equal(f.sources.length, 8, 'the end flushes the re-buffer');
    assert.equal(f.audio.underruns, 2);
    // Audio still scheduled ahead: no re-buffer.
    const steady = pcmRig();
    steady.begin('b'); steady.chunk('b', 0); steady.audio.context.currentTime = .05; steady.chunk('b', 1);
    assert.deepEqual([steady.sources.length, steady.audio.underruns], [2, 0]);
    f.audio.interrupt(); steady.audio.interrupt();
});

test('Record raw and sent: a silent copy of the capture\'s first input (no second microphone), kept from the press, closed when taken', async () => {
    const b = browser();
    try {
        const page = new PageAudio({ resumeWaitMs: 5 });
        const audio = session(page, { echoGuard: 'off' });
        await audio.start();
        assert.equal(audio.rawTap, null, 'nothing recorded before the press');
        assert.equal(await audio.startRawTap(), true);
        assert.equal(b.log.tracks, 1, 'the same microphone');
        const tap = b.log.nodes.at(-1);
        assert.ok(b.log.connects.some(([from, to]) => from === 'source' && to === 'convo-capture'));
        for (let n = 0; n < 3; n++) tap.port.onmessage({ data: new Float32Array(2048).fill(.1) });
        const taken = audio.takeRawTap();
        assert.equal(taken.rate, 48000);
        assert.equal(taken.chunks.length, 3);
        assert.match(taken.audio, /^UklGR/, 'a WAV');
        assert.equal(tap.port.onmessage, null);
        assert.equal(audio.takeRawTap(), null);
        // Bounded to the newest RAW_MAX_SECONDS.
        await audio.startRawTap();
        const again = b.log.nodes.at(-1);
        for (let n = 0; n < Math.ceil(RAW_MAX_SECONDS * 48000 / 2048) + 30; n++) again.port.onmessage({ data: new Float32Array(2048) });
        assert.ok(audio.rawTap.samples < (RAW_MAX_SECONDS + .1) * 48000);
        audio.close();
        assert.equal(audio.rawTap, null, 'closing the conversation closes the copy');
    } finally { b.restore(); }
});

test('Per-device keys: each phone mode keeps its own calibration; choices per microphone; migration once; blocked storage is harmless', async () => {
    const { LEGACY_KEY, MIGRATED_MARK, migrateLegacy, normalizeChoices, readChoices, soundKey, soundSource, writeChoices } = await import('../webui/sound-setup.js');
    const values = new Map(), storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key) };
    const blocked = { getItem() { throw new Error('SecurityError'); }, setItem() { throw new Error('SecurityError'); } };
    assert.equal(soundKey('mic', 'out'), 'convo.sound.v1:mic|out');
    assert.equal(soundKey('mic', 'out', 'media'), 'convo.sound.v1:mic|out#media');
    assert.equal(soundKey('mic', 'out', 'nonsense'), 'convo.sound.v1:mic|out');
    assert.deepEqual(normalizeChoices({ output: 5, noise_suppression: false, phone_mode: 'loud' }), { output: '', noise_suppression: false, auto_gain: true, phone_mode: 'auto' });
    assert.equal(writeChoices('mic', { output: 'hdmi' }, storage), true);
    assert.equal(readChoices('mic', storage).output, 'hdmi');
    assert.deepEqual(readChoices('mic', blocked), normalizeChoices({}));
    assert.equal(writeChoices('mic', {}, blocked), false);
    values.set(LEGACY_KEY, JSON.stringify({ noise_gate_db: -44 }));
    assert.equal(soundSource('convo.sound.v1:mic|out', storage), LEGACY_KEY, 'read the old values until they moved');
    assert.equal(migrateLegacy(soundKey('', 'out'), storage), false, 'never to another default-microphone key');
    assert.equal(migrateLegacy('convo.sound.v1:mic|out', storage), true);
    assert.equal(migrateLegacy('convo.sound.v1:mic2|out', storage), false, 'once');
    assert.equal(values.get(MIGRATED_MARK), 'convo.sound.v1:mic|out');
    assert.equal(soundSource('convo.sound.v1:mic2|out', storage), 'convo.sound.v1:mic2|out');
    assert.equal(migrateLegacy('convo.sound.v1:x|y', blocked), false);
    assert.equal(soundSource('k', blocked), 'k');
});

test('A chat switch keeps the page output running between the old capture and the new one', async () => {
    const b = browser();
    try {
        const page = new PageAudio({ resumeWaitMs: 5 });
        const old = session(page); await old.start();
        const release = page.keep();
        old.close();
        assert.equal(b.log.contexts[0].state, 'running', 'not suspended in the gap');
        const next = session(page); await next.start();
        release(); release();
        assert.equal(page.users, 1);
        assert.equal(page.snapshot().reopened, 0);
        next.close();
        assert.equal(b.log.contexts[0].state, 'suspended');
    } finally { b.restore(); }
});
