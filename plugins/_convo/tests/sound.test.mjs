// Sound per device (sound-setup.js, the sound drawer), the boundary's voicing verdict, the noise gate and end of turn in the capture
// (audio.js), and the start-up buffer for streamed replies (audio.begin prime_ms). Stubs only: no microphone or speaker.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { AudioSession, END_OF_TURN_SECONDS, MAX_PRIME_MS, SpeechBoundary, UnguardedCapture, feedBoundary } from '../webui/audio.js';
import { NoiseGate } from '../webui/voicing.js';
import { GATE_HINT, SOUND_DEFAULTS, axisPosition, effectiveSound, measuredThreshold, normalizeSound, readDevice, soundKey, soundStoreDefinition, writeDevice } from '../webui/sound-setup.js';
import { blocks, voiced } from './signals.mjs';

const memory = () => { const values = new Map(); return { values, getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key) }; };
const blocked = { getItem() { throw new Error('SecurityError'); }, setItem() { throw new Error('SecurityError'); }, removeItem() { throw new Error('SecurityError'); } };

test('Per-device values over the saved defaults, clamped to the ranges; blocked storage falls back to the defaults', () => {
    assert.deepEqual(SOUND_DEFAULTS, { noise_gate_db: -50, barge_in_seconds: 1.0, end_of_turn_seconds: 0.9, echo_guard: 'adaptive' });
    assert.equal(soundKey('mic-1', ''), 'convo.sound.v1:mic-1|default');
    const storage = memory(), key = soundKey('mic-1', 'speaker');
    assert.equal(writeDevice(key, { noise_gate_db: -40, other: 1 }, storage), true);
    assert.deepEqual(readDevice(key, storage), { noise_gate_db: -40 });
    const settings = { noise_gate_db: -55, barge_in_seconds: 1.4, end_of_turn_seconds: 1.2, echo_guard: 'strict' };
    assert.deepEqual(effectiveSound(settings, key, storage), { noise_gate_db: -40, barge_in_seconds: 1.4, end_of_turn_seconds: 1.2, echo_guard: 'strict' });
    assert.deepEqual(effectiveSound(settings, soundKey('other', 'speaker'), storage).noise_gate_db, -55, 'another device keeps the defaults');
    assert.deepEqual(normalizeSound({ noise_gate_db: -99, barge_in_seconds: 9, end_of_turn_seconds: .1, echo_guard: 'loud' }),
        { noise_gate_db: -66, barge_in_seconds: 2, end_of_turn_seconds: .5, echo_guard: 'adaptive' });
    assert.deepEqual(readDevice(key, blocked), {}); assert.equal(writeDevice(key, {}, blocked), false);
    assert.deepEqual(effectiveSound(settings, key, blocked), normalizeSound(settings));
    storage.setItem(key, '{not json'); assert.deepEqual(readDevice(key, storage), {});
});

test('Meter axis and Measure room: the floor + 8 dB, clamped; too little heard says so', () => {
    assert.equal(axisPosition(-66), 0); assert.equal(axisPosition(-3), 1); assert.equal(axisPosition(-200), 0);
    const quiet = Array.from({ length: 75 }, (_, i) => .001 * (1 + (i % 5) / 10));      // about -60 dBFS
    assert.equal(measuredThreshold(quiet), -52);
    assert.equal(measuredThreshold([.001]), null);
    assert.equal(measuredThreshold(Array(40).fill(.5)), -3, 'clamped to the axis');
});

function soundRig({ storage = memory(), active = true, patchResult = { ok: true, conflicts: [] } } = {}) {
    const applied = [], patches = [], timers = [];
    const convo = { active, settings: { noise_gate_db: -50, barge_in_seconds: 1, end_of_turn_seconds: .9, echo_guard: 'adaptive' }, meter: { level: .01, withheld: true, voiced: false, state: 'uncoupled' },
        soundKey: () => soundKey('mic', 'out'), applySound: (values, key) => { applied.push([key, { ...values }]); return key === 'echo_guard' && values.echo_guard === 'off' ? 'next start' : ''; }, refresh: async () => {} };
    const setup = { config: {}, async patch(changes) { patches.push(changes); return patchResult; }, error: '' };
    const store = soundStoreDefinition({ convo: () => convo, setup: () => setup, storage, schedule: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, cancel: () => {} });
    store.refresh();
    return { store, convo, setup, applied, patches, timers, storage };
}

test('The drawer: a change is saved for this device and reaches the running capture at once; Use as default patches the settings', async () => {
    const f = soundRig();
    f.store.set('noise_gate_db', -38);
    assert.deepEqual(readDevice(soundKey('mic', 'out'), f.storage), { noise_gate_db: -38 });
    assert.deepEqual(f.applied.at(-1), ['noise_gate_db', { noise_gate_db: -38, barge_in_seconds: 1, end_of_turn_seconds: .9, echo_guard: 'adaptive' }]);
    assert.equal(f.store.overridden('noise_gate_db'), true); assert.equal(f.store.overridden('barge_in_seconds'), false);
    f.store.set('echo_guard', 'off');
    assert.equal(f.store.message, 'next start', 'echo protection Off waits for the next start, and says so');
    f.store.set('barge_in_seconds', .25);
    assert.equal(f.store.values.barge_in_seconds, .4, 'clamped to 0.4-2.0');
    assert.equal(await f.store.useAsDefault(), true);
    assert.deepEqual(f.patches[0], { noise_gate_db: -38, barge_in_seconds: .4, end_of_turn_seconds: .9, echo_guard: 'off' });
    f.store.reset();
    assert.deepEqual(readDevice(soundKey('mic', 'out'), f.storage), {});
    assert.equal(f.store.values.noise_gate_db, -50);
    const meter = f.store.meter();
    assert.equal(meter.withheld, true); assert.equal(meter.state, 'uncoupled'); assert.equal(meter.text, '-40 dB');
    const conflict = soundRig({ patchResult: { ok: true, conflicts: [{ path: 'noise_gate_db' }] } });
    assert.equal(await conflict.store.useAsDefault(), false);
    assert.match(conflict.store.message, /Changed elsewhere/);
});

test('Blocked storage: changes still apply for the page and the drawer says they are not kept', () => {
    const f = soundRig({ storage: blocked });
    f.store.set('end_of_turn_seconds', 1.3);
    assert.equal(f.store.values.end_of_turn_seconds, 1.3);
    assert.match(f.store.message, /not keeping settings/);
    assert.equal(f.applied.length, 1);
});

test('Measure room listens three seconds through the live session only, then sets the gate for this device', () => {
    const idle = soundRig({ active: false });
    assert.equal(idle.store.measureRoom(), false);
    assert.match(idle.store.error, /Start a conversation first/);
    const f = soundRig();
    assert.equal(f.store.measureRoom(), true);
    assert.equal(f.timers[0].ms, 3000);
    for (let n = 0; n < 75; n++) f.convo.meterTap(.002);
    f.timers[0].fn();
    assert.equal(f.store.values.noise_gate_db, -46);
    assert.equal(f.convo.meterTap, null);
    assert.equal(f.store.measuring, false);
});

test('SpeechBoundary counts only voiced blocks (the verdict wins over RMS) and ends a turn after end_of_turn_seconds, live', () => {
    assert.equal(END_OF_TURN_SECONDS, .9);
    const turns = [], rate = 16000, block = new Float32Array(640);
    const b = new SpeechBoundary({ rate, onStart() {}, onTurn: chunks => turns.push(chunks), endOfTurn: .5 });
    for (let n = 0; n < 15; n++) b.push(block, .02, true);                    // 0.6 s voiced (silent samples, verdict says voice)
    assert.equal(b.admitted, true); assert.ok(Math.abs(b.speech - .6) < 1e-9);
    for (let n = 0; n < 13; n++) b.push(new Float32Array(640).fill(.5), .02, false);   // loud but not voiced: counted as silence
    assert.equal(turns.length, 1, 'ended after 0.5 s');
    b.setEndOfTurn(5); assert.equal(b.endSilence, 2, 'clamped to 2 s');
    b.setEndOfTurn(.1); assert.equal(b.endSilence, .5);
});

test('feedBoundary: the guard verdict per frame, the gate after the analysis, and the raw level for the meter', () => {
    const pushed = [], boundary = { push: (chunk, floor, verdict) => pushed.push([chunk, verdict]) };
    const gate = new NoiseGate({ rate: 48000, gateDb: -20 });
    const frame = new Float32Array(1920).fill(.01);
    const level = feedBoundary(boundary, { kind: 'audio', frames: [frame], voiced: [true], levels: [.01], dropped: 1920, floor: .004, level: .3 }, gate);
    assert.equal(level, .3, 'the raw level of the newest frame, not the released one');
    assert.equal(pushed[0][1], false, 'withheld time is unvoiced silence');
    assert.equal(pushed[1][1], true);
    assert.ok(pushed[1][0][1919] < .01, 'below the gate: attenuated');
    assert.equal(frame[0], Math.fround(.01), "the original frame is never changed");
});

test('Echo protection Off: the same voicing beside the legacy capture; a voice admits, a DC level does not', () => {
    const turns = [], starts = [];
    const b = new SpeechBoundary({ rate: 48000, onStart: () => starts.push(1), onTurn: () => turns.push(1) });
    const capture = new UnguardedCapture({ rate: 48000, gateDb: -50 });
    for (const block of blocks(voiced(1, 48000, { rms: .05 }))) capture.feed(b, block);
    assert.equal(starts.length, 1);
    const flat = new SpeechBoundary({ rate: 48000, onStart: () => starts.push(2), onTurn() {} }), level = new UnguardedCapture({ rate: 48000 });
    for (let n = 0; n < 30; n++) assert.ok(level.feed(flat, new Float32Array(2048).fill(.1)).level > .09);
    assert.equal(flat.admitted, false);
});

test('AudioSession.setSound changes the gate and end of turn of the running capture without a restart', () => {
    const posted = [];
    const audio = new AudioSession({ sound: { noise_gate_db: -45, end_of_turn_seconds: 1.1 } });
    assert.deepEqual([audio.gateDb, audio.endOfTurn], [-45, 1.1]);
    audio.capture = { port: { postMessage: message => posted.push(message) } };
    audio.boundary = new SpeechBoundary({ rate: 16000, onStart() {}, onTurn() {} });
    audio.gate = new NoiseGate({ rate: 16000, gateDb: -45 });
    assert.deepEqual(audio.setSound({ noise_gate_db: -30, end_of_turn_seconds: 1.6 }), { noise_gate_db: -30, end_of_turn_seconds: 1.6 });
    assert.deepEqual(posted, [{ kind: 'sound', gateDb: -30 }]);
    assert.equal(audio.boundary.endSilence, 1.6); assert.equal(audio.gate.gateDb, -30);
});

// ---- start-up buffer (audio.begin prime_ms)
function pcmRig() {
    const events = [], sources = [];
    const audio = new AudioSession({ onError: () => {} });
    audio.context = { currentTime: 0, state: 'running', destination: {}, async close() {},
        createBuffer: (_, length, rate) => ({ duration: length / rate, values: new Float32Array(length), getChannelData() { return this.values; } }),
        createBufferSource: () => { const source = { connect() { return this; }, disconnect() {}, start(when) { this.when = when; } }; sources.push(source); return source; } };
    const phrase = (id, prime) => ({ session_id: 's', epoch: 0, turn_id: 't', phrase_id: id, format: 'pcm_s16le', channels: 1, sample_rate: 24000, ...(prime === undefined ? {} : { prime_ms: prime }) });
    const begin = (id, prime) => audio.pcmBegin(phrase(id, prime), () => events.push(id + ':completed'), () => events.push(id + ':started'));
    const chunk = (id, sequence) => audio.pcmChunk({ ...phrase(id), sequence, audio: Buffer.alloc(4800).toString('base64') });   // 100 ms each
    const end = (id, chunks) => audio.pcmEnd({ ...phrase(id), chunks });
    return { audio, events, sources, begin, chunk, end };
}

test('A phrase that starts in silence waits for prime_ms of audio, then plays it all in order; absent or 0 = at once', () => {
    assert.equal(MAX_PRIME_MS, 2000);
    const f = pcmRig();
    f.begin('a', 300);
    f.chunk('a', 0); f.chunk('a', 1);
    assert.equal(f.sources.length, 0, '200 ms arrived: still filling the start-up buffer');
    f.chunk('a', 2);
    assert.equal(f.sources.length, 3, '300 ms: everything held starts');
    assert.deepEqual(f.sources.map(source => Math.round(source.when * 1000)), [20, 120, 220], 'back to back, in order');
    f.chunk('a', 3);
    assert.equal(f.sources.length, 4, 'the rest streams as before');
    const now = pcmRig(); now.begin('b'); now.chunk('b', 0); assert.equal(now.sources.length, 1);
    const zero = pcmRig(); zero.begin('c', 0); zero.chunk('c', 0); assert.equal(zero.sources.length, 1);
    f.audio.interrupt();
});

test('A short phrase plays at its end; a phrase queued behind audio still playing does not wait', () => {
    const f = pcmRig();
    f.begin('a', 1500); f.chunk('a', 0);
    assert.equal(f.sources.length, 0);
    assert.equal(f.end('a', 1), true);
    assert.equal(f.sources.length, 1, 'shorter than its buffer: it plays when it ends');
    f.begin('b', 1500); f.chunk('b', 0);
    assert.equal(f.sources.length, 2, 'audio is still scheduled ahead: no second wait');
    const capped = pcmRig(); capped.begin('x', 99999);
    assert.equal(capped.audio.pcm.get('x').primeMs, 2000);
    f.audio.interrupt();
});

test('The drawer markup: one dB axis for bar and handle, labelled controls, words beside the graphics, no live region on the meter', async () => {
    const html = await readFile(new URL('../webui/sound-panel.html', import.meta.url), 'utf8'), main = await readFile(new URL('../webui/main.html', import.meta.url), 'utf8');
    const markup = html.slice(0, html.indexOf('<style>')), css = html.slice(html.indexOf('<style>'));
    assert.match(markup, /<input type="range" min="-66" max="-3" step="1"/);
    assert.match(markup, /<div class="convo-sound-bar" aria-hidden="true">/);
    assert.ok(markup.includes(GATE_HINT));
    assert.match(markup, /withheld as echo/);
    for (const id of ['sound-gate', 'sound-barge', 'sound-end', 'sound-echo']) assert.match(markup, new RegExp(`:for="\\$id\\('${id}'\\)"`), id);
    assert.match(markup, /min="0\.4" max="2" step="0\.1"/); assert.match(markup, /min="0\.5" max="2" step="0\.1"/);
    assert.doesNotMatch(markup, /aria-live|role="log"|localStorage/);
    assert.match(css, /prefers-reduced-motion:reduce/);
    assert.match(main, /aria-controls="convo-sound-drawer"/);
    assert.match(main, /<x-component path="\/plugins\/_convo\/webui\/sound-panel\.html"><\/x-component><x-component path="\/plugins\/_convo\/webui\/synthesis-controls\.html"><\/x-component>/);
    assert.match(main, /Convo was updated — Reload to use the new version/);
});
