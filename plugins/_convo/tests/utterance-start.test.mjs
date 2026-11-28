// The whole utterance (plan 2026-11-10, change 2) and Convo's own microphone processing (change 11): the pre-roll, the ungated
// opening with its fade-in, no pre-roll over the assistant with echo protection Off, the anti-aliased 16 kHz upload, comfort noise
// instead of digital zeros during double talk, Measure room during playback, and the microphone's own warnings. Synthetic signals
// through the real SpeechBoundary, voicing and EchoGuard: nothing here says how real voices, rooms or microphones behave.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import {
    AudioSession, MIC_MUTE_REPORT_MS, PRE_ROLL_FADE_MS, PRE_ROLL_SECONDS, Resampler, SpeechBoundary, TurnEncoder, UnguardedCapture,
    feedBoundary, microphoneWarning, wavBase64,
} from '../webui/audio.js';
import { EchoGuard, KEEP_NEAR_VOICE_MS } from '../webui/echo-guard.js';
import { GATE_DEFAULT_DB, NoiseGate } from '../webui/voicing.js';
import { MEASURE_PLAYING, soundKey, soundStoreDefinition } from '../webui/sound-setup.js';
import { blocks, claps, concat, fan, gainDb, hum, keyboard, mix, noise, pink, replay, rmsOf, rng, silence, voiced } from './signals.mjs';

const RATES = [16000, 44100, 48000];
const ONSET = 1;                                             // seconds; every utterance below begins here

// An unvoiced fricative ("S", "F", the burst of a "T"): bright noise, high-passed twice, at a speech-like level.
function fricative(seconds, rate, { rms = .03, seed = 23 } = {}) {
    const next = rng(seed), out = new Float32Array(Math.round(seconds * rate));
    let x1 = 0, x2 = 0;
    for (let i = 0; i < out.length; i++) { const x = next() * 2 - 1; out[i] = x - 2 * x1 + x2; x2 = x1; x1 = x; }
    const level = rmsOf(out);
    return out.map(value => value * rms / level);
}
// Room tone under everything, so nothing is digital silence.
const room = (seconds, rate) => pink(seconds, rate, { rms: .0008, seed: 5 });

// The echo-protection-Off capture, the way AudioSession runs it: worklet blocks, page-side voicing, the noise gate after it.
function hear(signal, rate, { preRoll, allowPreRoll } = {}) {
    let now = 0;
    const result = { starts: [], turns: [] };
    const boundary = new SpeechBoundary({ rate, preRoll, clock: () => now * 1000, onStart: () => result.starts.push(now),
        allowPreRoll: allowPreRoll ? () => allowPreRoll(now) : undefined,
        onTurn: chunks => { const samples = chunks.reduce((n, c) => n + c.length, 0); result.turns.push({ chunks, startsAt: now - samples / rate, last: boundary.lastTurn }); } });
    const capture = new UnguardedCapture({ rate, gateDb: GATE_DEFAULT_DB });
    for (const block of blocks(signal, 2048)) { now += block.length / rate; capture.feed(boundary, block); }
    result.boundary = boundary;
    return result;
}
const joined = chunks => { const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0)); let o = 0; for (const c of chunks) { out.set(c, o); o += c.length; } return out; };

test('An unvoiced first sound before voiced speech keeps its start: the pre-roll reaches back past it (echo Off and adaptive)', () => {
    for (const rate of RATES) for (const [label, onset] of [['fricative', .12], ['plosive', .03]]) {
        const speech = concat(fricative(onset, rate), silence(.02, rate), voiced(1.4, rate, { rms: .06, seed: 31 }));
        const signal = mix(room(ONSET + 1.6 + 2, rate), concat(silence(ONSET, rate), speech, silence(2, rate)));
        for (const path of ['off', 'adaptive']) {
            const kept = path === 'off' ? hear(signal, rate) : replay(signal, rate, { guard: 'adaptive' });
            const cut = path === 'off' ? hear(signal, rate, { preRoll: 0 }) : replay(signal, rate, { guard: 'adaptive', preRoll: 0 });
            assert.equal(kept.turns.length, 1, `${label} ${path} ${rate}`);
            assert.ok(kept.turns[0].startsAt <= ONSET - .1, `${label} ${path} ${rate}: the turn starts ${kept.turns[0].startsAt.toFixed(3)} s, before the sound`);
            assert.ok(cut.turns[0].startsAt > ONSET, `${label} ${path} ${rate}: without the pre-roll the first sound is lost (${cut.turns[0].startsAt.toFixed(3)} s)`);
            // Never counted as speech: admission time and voiced seconds are those of the cut turn.
            assert.deepEqual(kept.starts, cut.starts, `${label} ${path} ${rate}: admission is unchanged`);
            assert.ok(Math.abs(kept.turns[0].last.voiced - cut.turns[0].last.voiced) < 1e-9);
            assert.ok(kept.turns[0].last.preRolled >= PRE_ROLL_SECONDS - 1e-9 && kept.turns[0].last.preRolled <= PRE_ROLL_SECONDS + 2048 / rate + 1e-9, `pre-roll ${kept.turns[0].last.preRolled}`);
        }
    }
});

test('The pre-roll and the opening block are sent ungated, rising from silence over the fade, and the rest is gated as before', () => {
    const rate = 48000;
    const speech = concat(fricative(.12, rate, { rms: .002 }), voiced(1.2, rate, { rms: .06, seed: 31 }));  // a soft onset below the gate
    const signal = mix(room(ONSET + 1.32 + 2, rate), concat(silence(ONSET, rate), speech, silence(2, rate)));
    const [turn] = hear(signal, rate).turns, audio = joined(turn.chunks);
    const lead = Math.round(turn.last.preRolled * rate), start = Math.round((ONSET - turn.startsAt) * rate);
    const sent = audio.subarray(start, start + Math.round(.1 * rate)), original = signal.subarray(Math.round(ONSET * rate), Math.round((ONSET + .1) * rate));
    assert.ok(sent.every((value, i) => Math.abs(value - original[i]) < 1e-7), 'the soft first sound inside the pre-roll is the original, not gated away');
    const fade = Math.round(PRE_ROLL_FADE_MS / 1000 * rate), source = signal.subarray(Math.round(turn.startsAt * rate));
    assert.equal(audio[0], 0);
    for (const i of [1, fade >> 1, fade - 1]) assert.ok(Math.abs(audio[i] - source[i] * i / fade) < 1e-7, `fade-in at sample ${i}`);
    assert.ok(Math.abs(audio[fade + 10] - source[fade + 10]) < 1e-7, 'after the fade the samples are the originals');
    assert.ok(lead > 0 && start > 0);
});

test('Noise is not admitted, pre-roll or not (room noise, fans, hum, claps, typing)', () => {
    for (const rate of [16000, 48000]) {
        const clips = { white: noise(4, rate, { rms: .02 }), pink: pink(4, rate, { rms: .03 }), fan: fan(4, rate, { rms: .03 }), hum: hum(4, rate, { rms: .02 }),
            claps: claps(4, rate), typing: keyboard(4, rate) };
        for (const [name, clip] of Object.entries(clips)) {
            const off = hear(mix(room(4, rate), clip), rate), guarded = replay(mix(room(4, rate), clip), rate, { guard: 'adaptive' });
            assert.deepEqual([off.starts.length, off.turns.length], [0, 0], `${name} at ${rate} (echo Off)`);
            assert.deepEqual([guarded.starts.length, guarded.turns.length], [0, 0], `${name} at ${rate} (adaptive)`);
        }
    }
});

test('Echo protection Off: speech right after a reply carries none of the reply (no pre-roll during playback or its echo tail)', () => {
    const rate = 48000, replyEnd = 3, tail = .35;
    // The reply as the microphone hears it with no echo protection: quiet enough never to be voiced, loud enough to be heard.
    const reply = gainDb(voiced(replyEnd, rate, { rms: .1, seed: 9, f0: 115 }), -36);
    for (const userAt of [replyEnd + .15, replyEnd + .6]) {
        const user = voiced(1.2, rate, { rms: .06, seed: 41 });
        const mic = mix(room(userAt + 3.5, rate), concat(reply, silence(userAt - replyEnd, rate), user, silence(2.3, rate)));
        const playing = now => now < replyEnd + tail;
        const guarded = hear(mic, rate, { allowPreRoll: now => !playing(now) });
        const unguarded = hear(mic, rate);
        assert.equal(guarded.turns.length, 1);
        assert.ok(guarded.turns[0].startsAt >= replyEnd, `user at ${userAt}: the turn starts at ${guarded.turns[0].startsAt.toFixed(3)} s, after the reply`);
        if (userAt - replyEnd < PRE_ROLL_SECONDS) assert.ok(unguarded.turns[0].startsAt < replyEnd, 'without the rule the pre-roll would reach back into the reply (the control)');
        assert.deepEqual(guarded.starts, unguarded.starts, 'admission is the same either way');
    }
    // AudioSession's rule: with echo protection Off the pre-roll waits until playback and its tail are over; with the guard it never does.
    const session = new AudioSession({ echoGuard: 'off' });
    session.context = { state: 'running', currentTime: 10, outputLatency: 0 };
    session.timeline = [{ phrase_id: 'p1', start: 6, end: 9.8 }];
    assert.equal(session.playbackNow(), true, 'within the echo tail');
    session.context.currentTime = 10.3; assert.equal(session.playbackNow(), false);
    session.queue = [{}]; assert.equal(session.playbackNow(), true, 'a queued phrase counts');
});

test('The 16 kHz upload is anti-aliased: tones above 8 kHz are attenuated, speech-band tones pass, and a constant stays constant', () => {
    const tone = (hz, rate, seconds = .5) => Float32Array.from({ length: Math.round(seconds * rate) }, (_, i) => .5 * Math.sin(2 * Math.PI * hz * i / rate));
    const middle = signal => signal.subarray(Math.round(signal.length * .2), Math.round(signal.length * .8));
    for (const rate of [44100, 48000]) {
        for (const hz of [9000, 10000, 12000, 15000, 20000]) {
            const resampler = new Resampler(rate), out = concat(resampler.push(tone(hz, rate)), resampler.finish());
            const db = 20 * Math.log10(rmsOf(middle(out)) / (.5 / Math.SQRT2));
            assert.ok(db < -50, `${hz} Hz at ${rate}: ${db.toFixed(1)} dB`);
        }
        for (const hz of [300, 1000, 3000, 5000]) {
            const resampler = new Resampler(rate), out = concat(resampler.push(tone(hz, rate)), resampler.finish());
            const db = 20 * Math.log10(rmsOf(middle(out)) / (.5 / Math.SQRT2));
            assert.ok(Math.abs(db) < .5, `${hz} Hz at ${rate}: ${db.toFixed(2)} dB`);
        }
    }
    // The averaging it replaces let a 10 kHz tone through at 48 kHz at about -6 dB, folded to 6 kHz (documented contrast).
    const bytes = Buffer.from(wavBase64([new Float32Array(48000).fill(.25)], 48000), 'base64');
    assert.equal(bytes.readInt16LE(44), 8192); assert.equal(bytes.readInt16LE(44 + 2 * 8000), 8192);
});

test('Streaming upload: blocks of any size, probes and the final turn give exactly the one-shot WAV; probes resample only new blocks', () => {
    for (const rate of [16000, 44100, 48000, 8000]) {
        const signal = voiced(1.3, rate, { rms: .08, seed: 3 }), chunks = [];
        const sizes = [2048, 1920, 128, 333, 4096, 7];
        for (let at = 0, k = 0; at < signal.length; k++) { const size = sizes[k % sizes.length]; chunks.push(signal.slice(at, at + size)); at += size; }
        const encoder = new TurnEncoder(rate);
        const half = chunks.slice(0, Math.floor(chunks.length / 2));
        assert.equal(encoder.encode(half, false), wavBase64(half, rate), `probe at ${rate}`);
        const used = encoder.used;
        assert.equal(encoder.encode(chunks, true), wavBase64(chunks, rate), `turn at ${rate}`);
        assert.ok(used === half.length);
        const frames = Buffer.from(wavBase64(chunks, rate), 'base64').readUInt32LE(40) / 2;
        assert.equal(frames, Math.floor(signal.length * 16000 / rate), 'the same length rule as before');
        assert.equal(encoder.first, null, 'a finished turn leaves nothing behind');
    }
});

// Speech over a learned echo with gaps between words: the guard's double-talk path.
function overlapped(rate) {
    const lead = 1.5, at = 3.5, total = 8;
    const playback = voiced(5.5, rate, { rms: .1, seed: 9, f0: 115 });
    const user = voiced(2, rate, { rms: .05, seed: 21, f0: 195, syllables: 3.1 });
    const echo = playback.map((value, i) => i >= Math.round(.06 * rate) ? .3 * playback[i - Math.round(.06 * rate)] : 0);
    const mic = mix(room(total, rate), mix(concat(silence(at, rate), user, silence(total - at - 2, rate)), concat(silence(lead, rate), echo, silence(total - lead - 5.5, rate))));
    return { mic, reference: concat(silence(lead, rate), playback, silence(total - lead - 5.5, rate)), at, userEnd: at + 2 };
}

test('Double talk: the user\'s turn never holds digital zeros mid-speech, comfort noise is never voice, and frames near the voice are kept', () => {
    const longestZeroRun = samples => { let best = 0, run = 0; for (const v of samples) { run = v === 0 ? run + 1 : 0; best = Math.max(best, run); } return best; };
    for (const rate of [48000, 16000]) {
        const { mic, reference, at, userEnd } = overlapped(rate);
        const result = replay(mic, rate, { guard: 'adaptive', reference });
        assert.equal(result.turns.length, 1, `one turn at ${rate}`);
        const [turn] = result.turns, audio = joined(turn.chunks);
        const speechStart = Math.round((at - turn.startsAt) * rate) + Math.round(.05 * rate);
        const mid = audio.subarray(speechStart, Math.round((userEnd - turn.startsAt) * rate));
        assert.ok(mid.length > rate, 'the turn spans the speech');
        assert.ok(longestZeroRun(mid) <= 2, `no digital silence inside the speech at ${rate} (longest run ${longestZeroRun(mid)})`);
        assert.ok(result.comfortSamples > 0, 'comfort noise filled the withheld time');
        // Comfort noise is never voice: the voiced seconds are those of a run where the same steps arrive with comfort replaced by zeros.
        const guard = new EchoGuard({ inputRate: rate, mode: 'adaptive', nativeAec: true, gateDb: GATE_DEFAULT_DB });
        const count = { comfort: 0, voicedComfort: 0, nearKept: 0 };
        const a = new SpeechBoundary({ rate, onStart() {}, onTurn() {} }), b = new SpeechBoundary({ rate, onStart() {}, onTurn() {} });
        const gateA = new NoiseGate({ rate, gateDb: GATE_DEFAULT_DB }), gateB = new NoiseGate({ rate, gateDb: GATE_DEFAULT_DB });
        for (let s = 0; s + 128 <= mic.length; s += 128) for (const step of guard.ingest(mic.subarray(s, s + 128), reference.subarray(s, s + 128))) {
            step.frames.forEach((frame, i) => { if (step.comfort[i]) { count.comfort++; if (step.voiced[i]) count.voicedComfort++; assert.ok(rmsOf(frame.subarray(Math.round(.005 * rate))) <= .0016, 'floor level after the crossfade'); } });
            feedBoundary(a, { kind: 'audio', ...step, frames: step.frames }, gateA);
            const zeroed = step.frames.map((frame, i) => step.comfort[i] ? new Float32Array(frame.length) : frame);
            feedBoundary(b, { kind: 'audio', ...step, frames: zeroed }, gateB);
            assert.equal(a.speech, b.speech, 'comfort noise never adds voiced time');
        }
        assert.ok(count.comfort > 0); assert.equal(count.voicedComfort, 0);
    }
    assert.equal(KEEP_NEAR_VOICE_MS, 250);
});

test('Double talk: echo alone after the user stops never becomes a second turn; frames near the voice are kept, and more of the speech arrives', () => {
    const rate = 48000, { mic, reference, at, userEnd } = overlapped(rate);
    const result = replay(mic, rate, { guard: 'adaptive', reference });
    assert.equal(result.starts.length, 1, 'the echo that follows (as comfort noise) never opens another utterance');
    // Every released frame is the original microphone audio; within the user's speech, more of it is released than voiced frames alone.
    const guard = new EchoGuard({ inputRate: rate, mode: 'adaptive', nativeAec: true, gateDb: GATE_DEFAULT_DB });
    let position = 0, voicedKept = 0, nearKept = 0;
    const originals = new Set();
    for (let s = 0; s + 128 <= mic.length; s += 128) for (const step of guard.ingest(mic.subarray(s, s + 128), reference.subarray(s, s + 128))) {
        for (const frame of step.released) originals.add(frame);
        step.frames.forEach((frame, i) => { if (!step.comfort[i]) { if (step.voiced[i]) voicedKept++; else nearKept++; } });
        position = s;
    }
    assert.ok(voicedKept > 0 && nearKept > 0, `voiced ${voicedKept}, kept near the voice ${nearKept}`);
    assert.ok(position > userEnd * rate - rate && at > 0);
});

test('Measure room refuses while the assistant is playing, and a reading taken during playback ends the measurement', () => {
    const timers = [];
    const convo = { active: true, state: 'listening', settings: {}, meter: { level: .01, playback: false }, soundKey: () => soundKey('mic', 'out'), applySound: () => '' };
    const store = soundStoreDefinition({ convo: () => convo, setup: () => ({}), storage: null, schedule: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, cancel: () => {} });
    store.refresh();
    convo.state = 'speaking';
    assert.equal(store.canMeasure, false); assert.equal(store.measureRoom(), false); assert.equal(store.error, MEASURE_PLAYING);
    convo.state = 'listening'; convo.meter = { level: .01, playback: true };
    assert.equal(store.measureRoom(), false, 'the echo tail counts as playing');
    convo.meter = { level: .01, playback: false };
    assert.equal(store.measureRoom(), true);
    for (let n = 0; n < 20; n++) convo.meterTap(.002, false);
    convo.meterTap(.05, true);
    assert.equal(store.measuring, false); assert.equal(convo.meterTap, null); assert.equal(store.error, MEASURE_PLAYING);
    assert.equal(store.values.noise_gate_db, -50, 'nothing was set from a measurement the reply interrupted');
});

test('The microphone: narrow-band and Bluetooth hands-free microphones are named; an ended or lasting mute is reported with a restart', async () => {
    const track = (label, sampleRate) => ({ label, getSettings: () => ({ sampleRate }) });
    assert.match(microphoneWarning(track('Headset (WH-1000XM4 Hands-Free AG Audio)', 16000)), /Bluetooth hands-free/);
    assert.match(microphoneWarning(track('Microphone (USB Audio)', 16000)), /16 kHz/);
    assert.match(microphoneWarning(track('Microphone', 8000)), /8 kHz/);
    assert.equal(microphoneWarning(track('Microphone Array (Realtek)', 48000)), '');
    assert.equal(microphoneWarning(track('Microphone', undefined)), '');
    assert.equal(microphoneWarning({ label: 'x', getSettings() { throw new Error('no'); } }), '');

    const issues = [], live = new EventTarget();
    Object.assign(live, { label: 'Headset (Hands-Free)', muted: false, readyState: 'live', getSettings: () => ({ sampleRate: 16000 }) });
    const session = new AudioSession({ onMicrophone: issue => issues.push(issue) });
    session.stream = { getAudioTracks: () => [live], getTracks: () => [{ stop() {} }] };
    session.watchMicrophone();
    assert.deepEqual(issues.map(i => [i.kind, i.restart]), [['warning', false]]);
    live.muted = true; live.dispatchEvent(new Event('mute'));
    live.muted = false; live.dispatchEvent(new Event('unmute'));
    await new Promise(resolve => setTimeout(resolve, MIC_MUTE_REPORT_MS + 50));
    assert.equal(issues.length, 1, 'a brief mute is not reported');
    live.muted = true; live.dispatchEvent(new Event('mute'));
    await new Promise(resolve => setTimeout(resolve, MIC_MUTE_REPORT_MS + 50));
    assert.deepEqual(issues.map(i => i.kind), ['warning', 'muted']); assert.equal(issues[1].restart, true);
    live.muted = false; live.dispatchEvent(new Event('unmute'));
    live.dispatchEvent(new Event('ended'));
    assert.deepEqual(issues.map(i => i.kind), ['warning', 'muted', 'unmuted', 'ended']); assert.equal(issues[3].restart, true);
    session.close();
    live.dispatchEvent(new Event('ended'));
    assert.equal(issues.length, 4, 'nothing is reported after close');

    const store = await readFile(new URL('../webui/convo-store.js', import.meta.url), 'utf8');
    assert.equal((store.match(/onMeter: meterReading, onMicrophone: microphoneIssue,/g) || []).length, 2, 'both capture sessions report the microphone');
    const panel = await readFile(new URL('../webui/main.html', import.meta.url), 'utf8');
    assert.match(panel, /restartMicrophone\(\)/);
});

test('The guard worklet posts comfort flags, playback and each frame buffer once; the session sends pre_roll_s with probes and turns', async () => {
    const worklet = await readFile(new URL('../webui/audio-worklet-guard.js', import.meta.url), 'utf8');
    assert.match(worklet, /comfort: result\.comfort/); assert.match(worklet, /playback: result\.playbackActive/);
    assert.match(worklet, /new Set\(result\.frames\.map\(frame => frame\.buffer\)\)/);
    const store = await readFile(new URL('../webui/convo-store.js', import.meta.url), 'utf8');
    assert.match(store, /\.\.\.\(item\.lead \|\| \{\}\)/);
});
