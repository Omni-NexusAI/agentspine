// Echo guard (BP9). The standalone Adaptive v3 test (tests/echo_guard.mjs in the speech-to-speech fork, a synthetic script that is
// not part of any CI) ported to node:test against the Convo class, plus the Convo-specific behaviour: original capture samples
// are released (never residuals), withheld time is reported, the noise floor serves as the boundary's voiced threshold, the
// learned path survives a recreated session. Synthetic signals only: none of this says how a real room or speaker behaves.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DOUBLE_TALK_MS, EchoGuard, HUMAN_FLOOR_MAX_RMS, HUMAN_FLOOR_MIN_RMS, LEGACY_VOICED_RMS } from '../webui/echo-guard.js';

const RATE = 48000;
const INPUT_SAMPLES = 1920;                                  // one 40 ms analysis frame at 48 kHz

function tone(frequency, phase = 0, amplitude = 0.2) {
    const values = new Float32Array(INPUT_SAMPLES);
    for (let i = 0; i < values.length; i++) values[i] = amplitude * Math.sin((2 * Math.PI * frequency * i) / RATE + phase);
    return values;
}

// A guard plus a log of everything it released and withheld.
function configure(mode) {
    const guard = new EchoGuard({ inputRate: RATE, mode, nativeAec: true });
    guard.log = { released: [], dropped: 0, metrics: [], steps: [] };
    guard.feed = (mic, reference) => {
        const results = guard.ingest(mic, reference);
        for (const step of results) {
            guard.log.released.push(...step.released); guard.log.dropped += step.dropped; guard.log.steps.push(step);
            if (step.metric) guard.log.metrics.push(step.metric);
        }
        return results;
    };
    return guard;
}

const hasSignal = frame => !!frame && frame.some(sample => Math.abs(sample) > 8 / 32768);

function deterministicNoise(length, seed = 0x12345678) {
    const values = new Float32Array(length);
    let state = seed;
    for (let i = 0; i < length; i++) {
        state = (1664525 * state + 1013904223) >>> 0;
        values[i] = (((state / 0xffffffff) * 2) - 1) * 0.15;
    }
    return values;
}

function roomEcho(reference, delaySamples, gain = 0.55) {
    const values = new Float32Array(reference.length);
    for (let i = 0; i < values.length; i++) {
        const direct = i - delaySamples;
        const reflectionA = direct - 240;
        const reflectionB = direct - 600;
        values[i] = (direct >= 0 ? reference[direct] * gain : 0)
            + (reflectionA >= 0 ? reference[reflectionA] * 0.23 : 0)
            + (reflectionB >= 0 ? reference[reflectionB] * 0.11 : 0);
    }
    return values;
}

function directEcho(reference, delaySamples, gain) {
    const values = new Float32Array(reference.length);
    for (let i = delaySamples; i < values.length; i++) values[i] = reference[i - delaySamples] * gain;
    return values;
}

const add = (a, b) => a.map((value, i) => value + b[i]);
const scale = (values, gain) => values.map(value => value * gain);
const clip = (values, limit = 0.12) => values.map(value => Math.max(-limit, Math.min(limit, value)));
const frameOf = (signal, index) => signal.subarray(index * INPUT_SAMPLES, (index + 1) * INPUT_SAMPLES);
const sameSamples = (a, b) => a.length === b.length && a.every((value, i) => value === b[i]);

test('Modes: adaptive and strict withhold correlated playback; off passes the microphone untouched', () => {
    const echo = tone(440);
    const adaptive = configure('adaptive'); adaptive.feed(echo, echo);
    assert.equal(adaptive.log.released.length, 0, 'adaptive suppresses correlated playback');
    assert.equal(adaptive.log.dropped, INPUT_SAMPLES, 'what was withheld is reported as time, not audio');
    const off = configure('off'); off.feed(echo, echo);
    assert.equal(off.log.released.length, 1); assert.ok(sameSamples(off.log.released[0], echo), 'off releases the original samples');
    const strict = configure('strict'); strict.feed(echo, echo);
    assert.equal(strict.log.released.length, 0, 'strict suspends upload during playback');
    const quiet = configure('strict'); quiet.feed(new Float32Array(INPUT_SAMPLES), null);
    assert.equal(quiet.log.released.length, 1, 'strict only suspends while playback is active');
    assert.equal(new EchoGuard({ inputRate: RATE, mode: 'nonsense' }).mode, 'adaptive');
});

test('Isolated headset: independent speech with an active reference is held for 450 ms, then released as the original samples', () => {
    const guard = configure('adaptive');
    const reference = deterministicNoise(INPUT_SAMPLES * 16, 0x2468ace0);
    const mics = [];
    for (let frame = 0; frame < 12; frame++) {
        // A headset / native-AEC path: the assistant's exact playback reference is active, but the microphone hears only the user.
        const human = scale(tone(733, frame * 0.13), 0.012);
        mics.push(human);
        guard.feed(human, frameOf(reference, frame));
        if (frame < 11) assert.equal(guard.log.released.length, 0, `isolated speech stays local before ${DOUBLE_TALK_MS} ms (frame ${frame})`);
    }
    assert.equal(guard._echoModelReady, false, 'an isolated headset does not need an echo model');
    assert.equal(guard._acousticState, 'uncoupled', 'sustained independent speech resolves uncoupled capture');
    assert.equal(guard.log.released.length, 12, 'the whole held onset is released after 450 ms');
    guard.log.released.forEach((frame, i) => assert.ok(sameSamples(frame, mics[i]), `released frame ${i} is the original microphone samples`));
    assert.equal(guard.log.dropped, 0, 'nothing of the confirmed onset is dropped');
});

test('Native AEC that removes the echo entirely is recognised without predictor warmup and uploads nothing', () => {
    const guard = configure('adaptive');
    const reference = deterministicNoise(INPUT_SAMPLES * 14, 0x13579bdf);
    for (let frame = 0; frame < 8; frame++) guard.feed(new Float32Array(INPUT_SAMPLES), frameOf(reference, frame));
    assert.equal(guard._acousticState, 'uncoupled');
    assert.equal(guard.log.released.length, 0, 'silence while the assistant speaks is not forwarded');
});

test('Bluetooth-scale delay (500 ms) with reverberation: the lag is tracked, the model becomes ready, the echo is suppressed', () => {
    const guard = configure('adaptive');
    const reference = deterministicNoise(INPUT_SAMPLES * 40);
    const echo = roomEcho(reference, Math.round(RATE * 0.5));
    for (let frame = 0; frame < 40; frame++) guard.feed(frameOf(echo, frame), frameOf(reference, frame));
    const metric = guard.log.metrics.at(-1);
    assert.ok(metric, 'diagnostics are emitted');
    assert.equal(metric.modelReady, true);
    assert.ok(metric.lagMs >= 430 && metric.lagMs <= 500, `lag ${metric.lagMs} ms`);
    assert.equal(guard.log.released.length, 0);
});

test('Clipped (distorted) echo after training is still suppressed, and nothing predicted is ever uploaded', () => {
    const guard = configure('adaptive');
    const training = deterministicNoise(INPUT_SAMPLES * 35);
    const trainingEcho = roomEcho(training, Math.round(RATE * 0.12));
    for (let frame = 0; frame < 35; frame++) guard.feed(frameOf(trainingEcho, frame), frameOf(training, frame));
    const reference = deterministicNoise(INPUT_SAMPLES * 8);
    const echo = clip(roomEcho(reference, Math.round(RATE * 0.12), 0.8));
    for (let frame = 0; frame < 8; frame++) guard.feed(frameOf(echo, frame), frameOf(reference, frame));
    assert.equal(guard.log.released.length, 0);
});

test('The learned echo path persists between assistant responses and across mode changes', () => {
    const guard = configure('adaptive');
    const first = 24, gap = 8, second = 12;
    const reference = new Float32Array(INPUT_SAMPLES * (first + gap + second));
    reference.set(deterministicNoise(INPUT_SAMPLES * first), 0);
    reference.set(deterministicNoise(INPUT_SAMPLES * second, 0x87654321), INPUT_SAMPLES * (first + gap));
    const echo = add(roomEcho(reference, Math.round(RATE * 0.12), 0.42), scale(deterministicNoise(reference.length, 0xdeadbeef), 0.02));
    for (let frame = 0; frame < first; frame++) guard.feed(frameOf(echo, frame), frameOf(reference, frame));
    assert.equal(guard._echoModelReady, true, 'noisy attenuated echo trains the classifier');
    assert.equal(guard.log.released.length, 0);
    assert.ok(guard._echoFilter.reduce((sum, v) => sum + v * v, 0) > 0, 'the predictor learns an echo path');
    for (let frame = first; frame < first + gap; frame++) guard.feed(frameOf(echo, frame), frameOf(reference, frame));
    assert.equal(guard.log.released.length, 0, 'playback gaps and the 350 ms tail remain suppressed');
    assert.equal(guard._echoModelReady, true, 'playback gaps do not discard the learned path');
    for (let frame = first + gap; frame < first + gap + second; frame++) guard.feed(frameOf(echo, frame), frameOf(reference, frame));
    assert.equal(guard.log.released.length, 0, 'a later assistant response remains echo-only');
    const energy = guard._echoFilter.reduce((sum, v) => sum + v * v, 0);
    guard.configure({ mode: 'strict', nativeAec: true }); guard.configure({ mode: 'adaptive', nativeAec: true });
    assert.equal(guard._echoModelReady, true, 'explicit mode changes retain the learned predictor');
    assert.equal(guard._echoFilter.reduce((sum, v) => sum + v * v, 0), energy, 'and do not rewrite its weights');
});

test('Double talk over a learned echo: one uncertain frame fails closed; confirmed speech releases the original microphone samples', () => {
    const guard = configure('adaptive');
    const reference = deterministicNoise(INPUT_SAMPLES * 40);
    const echo = roomEcho(reference, Math.round(RATE * 0.08), 0.35);
    for (let frame = 0; frame < 20; frame++) guard.feed(frameOf(echo, frame), frameOf(reference, frame));
    assert.equal(guard.log.released.length, 0, 'echo-only warmup uploads nothing');
    guard.feed(add(frameOf(echo, 20), scale(tone(997, 0.17), 0.02)), frameOf(reference, 20));
    assert.equal(guard.log.released.length, 0, 'one uncertain frame fails closed');
    const mics = [];
    for (let frame = 21; frame < 33; frame++) {
        const mic = add(frameOf(echo, frame), tone(997, 0.37 + (frame - 21) * 0.11));
        mics.push(mic);
        guard.feed(mic, frameOf(reference, frame));
        if (frame < 32) assert.equal(guard.log.released.length, 0, 'candidate audio remains local before 450 ms confirmation');
    }
    assert.equal(guard.log.released.length, 12, 'the complete buffered onset is released');
    guard.log.released.forEach((frame, i) => assert.ok(sameSamples(frame, mics[i]), `frame ${i} is original mic PCM, not the predictor residual`));
    for (let frame = 33; frame < 38; frame++) guard.feed(add(frameOf(echo, frame), tone(997, 0.37 + frame * 0.11)), frameOf(reference, frame));
    const metric = guard.log.metrics.at(-1);
    assert.equal(metric.nativeAec, true); assert.equal(metric.modelReady, true); assert.equal(metric.doubleTalk, true);
    assert.ok(Number.isFinite(metric.erleDb)); assert.ok(metric.candidateMs >= 450);
});

test('Natural barge-in over a quiet echo survives short speech gaps; the echo that follows is not added to the turn', () => {
    const guard = configure('adaptive');
    const reference = deterministicNoise(INPUT_SAMPLES * 48, 0x13572468);
    const echo = directEcho(reference, Math.round(RATE * 0.08), 0.08);
    for (let frame = 0; frame < 20; frame++) guard.feed(frameOf(echo, frame), frameOf(reference, frame));
    assert.equal(guard._echoModelReady, true, 'quiet hardware-style echo trains the predictor');
    assert.equal(guard.log.released.length, 0, 'quiet echo-only playback remains suppressed');
    let evidence = 0; const mics = [];
    for (let frame = 20; frame < 34; frame++) {
        const gap = frame === 23 || frame === 28;
        const human = gap ? new Float32Array(INPUT_SAMPLES) : scale(tone(997, 0.21 + frame * 0.07), 0.08);
        const mic = add(frameOf(echo, frame), human);
        mics.push(mic);
        guard.feed(mic, frameOf(reference, frame));
        if (!gap) evidence++;
        if (evidence < 12) assert.equal(guard.log.released.length, 0, 'natural barge-in stays local until 450 ms of human evidence');
    }
    assert.equal(guard.log.released.length, 14, 'brief speech gaps do not prevent confirmed barge-in');
    guard.log.released.forEach((frame, i) => assert.ok(sameSamples(frame, mics[i])));
    for (let frame = 34; frame < 40; frame++) guard.feed(frameOf(echo, frame), frameOf(reference, frame));
    assert.equal(guard.log.released.length, 14, 'echo after a genuine interruption is suppressed instead of extending the user turn');
});

test('Conservation: every capture sample is released, withheld as dropped time, or still held; never duplicated', () => {
    const guard = configure('adaptive');
    const reference = deterministicNoise(INPUT_SAMPLES * 60, 0x0badf00d);
    const echo = directEcho(reference, Math.round(RATE * 0.08), 0.1);
    const mic = new Float32Array(reference.length);
    for (let frame = 0; frame < 60; frame++) {
        const speaking = (frame >= 22 && frame < 40) || frame >= 52;
        const human = speaking ? scale(tone(500 + frame, frame * 0.1), 0.08) : new Float32Array(INPUT_SAMPLES);
        mic.set(add(frameOf(echo, frame), human), frame * INPUT_SAMPLES);
        guard.feed(frameOf(mic, frame), frameOf(reference, frame));
    }
    const released = guard.log.released.reduce((n, f) => n + f.length, 0);
    assert.equal(released + guard.log.dropped + guard._pendingSamples, mic.length);
    assert.ok(released > 0 && guard.log.dropped > 0, 'the scenario exercises both outcomes');
});

test('Arbitrary block sizes (128-sample worklet quanta, odd lengths, no reference) give the same decisions as whole frames', () => {
    const reference = deterministicNoise(INPUT_SAMPLES * 30, 0x5eed);
    const echo = directEcho(reference, Math.round(RATE * 0.06), 0.2);
    const whole = configure('adaptive'), pieces = configure('adaptive');
    for (let frame = 0; frame < 30; frame++) whole.feed(frameOf(echo, frame), frameOf(reference, frame));
    for (let at = 0; at < echo.length;) { const n = Math.min(at % 3 ? 128 : 77, echo.length - at); pieces.feed(echo.subarray(at, at + n), reference.subarray(at, at + n)); at += n; }
    assert.equal(pieces.log.steps.length, whole.log.steps.length);
    assert.deepEqual(pieces.log.steps.map(s => s.released.length), whole.log.steps.map(s => s.released.length));
    const noReference = configure('adaptive'), speech = tone(300);
    for (let frame = 0; frame < 5; frame++) noReference.feed(speech, null);
    assert.equal(noReference.log.released.length, 5, 'with no playback reference the microphone passes through');
});

test('Other capture rates analyse correctly and release the original block length', () => {
    for (const rate of [16000, 44100]) {
        const guard = new EchoGuard({ inputRate: rate, mode: 'adaptive' });
        const sine = Float32Array.from({ length: rate }, (_, i) => 0.1 * Math.sin(2 * Math.PI * 300 * i / rate));
        const out = guard.ingest(sine, null);
        assert.equal(out.length, 25, `${rate} Hz: 25 frames per second`);
        assert.equal(out[0].released[0].length, Math.floor(rate * 0.04));
        assert.ok(sameSamples(out[0].released[0], sine.subarray(0, out[0].released[0].length)));
    }
});

test('The measured noise floor is the voiced threshold: legacy until calibrated, then quiet speech counts and long speech does not raise it', () => {
    const guard = configure('adaptive');
    assert.equal(guard.voicedFloor, LEGACY_VOICED_RMS, 'before calibration the fixed threshold applies');
    for (let i = 0; i < 40; i++) guard.feed(scale(deterministicNoise(INPUT_SAMPLES, 7 + i), 0.0005 / 0.15 * 0.577), null);      // a quiet room
    const quiet = guard.voicedFloor;
    assert.ok(quiet >= HUMAN_FLOOR_MIN_RMS && quiet < 0.005, `quiet room floor ${quiet}`);
    // Three seconds of quiet speech (rms .012, under the old fixed .02) must stay above the floor throughout.
    for (let i = 0; i < 75; i++) {
        const speech = scale(deterministicNoise(INPUT_SAMPLES, 99 + i), 0.012 / 0.0866);
        const rms = Math.sqrt(speech.reduce((n, v) => n + v * v, 0) / speech.length);
        guard.feed(speech, null);
        assert.ok(rms > guard.voicedFloor, `frame ${i}: speech ${rms.toFixed(4)} vs floor ${guard.voicedFloor.toFixed(4)}`);
    }
    // A real noise step (a fan switching on at rms .02) is learned: the floor converges instead of staying low forever.
    for (let i = 0; i < 1500; i++) guard.feed(scale(deterministicNoise(INPUT_SAMPLES, 5000 + i), 0.02 / 0.0866), null);
    assert.ok(guard.voicedFloor > 0.02 && guard.voicedFloor <= HUMAN_FLOOR_MAX_RMS, `floor after the noise step ${guard.voicedFloor}`);
});

test('The learned echo path can be exported and restored (retarget keeps it), but only for the same rate', () => {
    const trained = configure('adaptive');
    const reference = deterministicNoise(INPUT_SAMPLES * 30);
    const echo = roomEcho(reference, Math.round(RATE * 0.12), 0.45);
    for (let frame = 0; frame < 30; frame++) trained.feed(frameOf(echo, frame), frameOf(reference, frame));
    const snapshot = trained.log.steps.map(s => s.state).filter(Boolean).at(-1) ?? trained.exportState();
    assert.ok(snapshot.filter.length === 256);
    const fresh = new EchoGuard({ inputRate: RATE, mode: 'adaptive' });
    assert.equal(fresh._echoModelReady, false);
    assert.equal(fresh.importState(snapshot), true);
    assert.equal(fresh._echoModelReady, true, 'a restored session does not have to relearn the room');
    assert.deepEqual(Array.from(fresh._echoFilter), Array.from(snapshot.filter));
    assert.equal(new EchoGuard({ inputRate: 44100 }).importState(snapshot), false, 'a different rate is refused');
    assert.equal(fresh.importState(null), false); assert.equal(fresh.importState({ inputRate: RATE, filter: [1, 2] }), false);
});

test('Reset clears history, readiness and predictor weights', () => {
    const guard = configure('adaptive');
    const reference = deterministicNoise(INPUT_SAMPLES * 40);
    const echo = roomEcho(reference, Math.round(RATE * 0.5));
    for (let frame = 0; frame < 40; frame++) guard.feed(frameOf(echo, frame), frameOf(reference, frame));
    assert.equal(guard._echoModelReady, true);
    guard.reset();
    assert.equal(guard._referenceHistory.length, 0, 'echo reset clears playback history');
    assert.equal(guard._echoModelReady, false, 'echo reset clears adaptive readiness');
    assert.equal(guard._echoFilter.some(value => value !== 0), false, 'echo reset clears predictor weights');
});

// ---- the worklet wrapper and the session wiring (stubs for the browser globals) -------------------------------------------

test('Guard worklet: registers, posts only original samples with the dropped count and the floor, and obeys configuration messages', async () => {
    const saved = ['sampleRate', 'AudioWorkletProcessor', 'registerProcessor'].map(key => [key, globalThis[key]]);
    let Processor = null, name = null;
    globalThis.sampleRate = RATE;
    globalThis.AudioWorkletProcessor = class { constructor() { this.messages = []; this.port = { onmessage: null, postMessage: value => this.messages.push(value) }; } };
    globalThis.registerProcessor = (registered, implementation) => { name = registered; Processor = implementation; };
    try {
        await import('../webui/audio-worklet-guard.js?worklet-test');
        assert.equal(name, 'convo-capture-guard');
        const processor = new Processor({ processorOptions: { mode: 'adaptive', nativeAec: true } });
        const echo = tone(440);
        for (let at = 0; at < INPUT_SAMPLES; at += 128) processor.process([[echo.subarray(at, at + 128)], [echo.subarray(at, at + 128)]]);
        const audio = processor.messages.filter(m => m?.kind === 'audio');
        assert.equal(audio.length, 1, 'one analysis frame');
        assert.equal(audio[0].frames.length, 0, 'the echo is withheld');
        assert.equal(audio[0].dropped, INPUT_SAMPLES);
        const quiet = new Processor({ processorOptions: { mode: 'off' } });
        const speech = tone(300);
        for (let at = 0; at < INPUT_SAMPLES; at += 128) quiet.process([[speech.subarray(at, at + 128)], []]);
        const passed = quiet.messages.find(m => m?.kind === 'audio');
        assert.ok(passed.frames[0] instanceof Float32Array && sameSamples(passed.frames[0], speech));
        assert.equal(passed.floor, LEGACY_VOICED_RMS);
        quiet.port.onmessage({ data: { kind: 'echo_guard', mode: 'strict', nativeAec: false } });
        assert.equal(quiet.guard.mode, 'strict');
        assert.equal(quiet.process([[]], []), true, 'empty input is tolerated');
        assert.equal(quiet.process([], []), true);
    } finally { for (const [key, value] of saved) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value; } }
});

test('Diagnostics say when the guard is withholding, and a held onset is released if playback ends before it is confirmed', () => {
    const guard = configure('adaptive');
    const echo = tone(440);
    for (let n = 0; n < 5; n++) guard.feed(echo, echo);
    const metric = guard.log.metrics.at(-1);
    assert.equal(metric.suppressing, true); assert.ok(metric.suppressedMs >= 160); assert.equal(metric.playbackActive, true);
    const strict = configure('strict');
    for (let n = 0; n < 5; n++) strict.feed(echo, echo);
    assert.equal(strict.log.metrics.at(-1).suppressing, true, 'strict reports that it is suppressing');
    // Independent speech starts in the last frame of playback; the 350 ms tail ends before 450 ms of it have been confirmed.
    const held = configure('adaptive'), reference = deterministicNoise(INPUT_SAMPLES, 0x1357), mics = [];
    for (let frame = 0; frame < 10; frame++) {
        const human = scale(tone(733, frame * 0.13), 0.02); mics.push(human);
        held.feed(human, frame === 0 ? reference : null);
        if (frame < 9) assert.equal(held.log.released.length, 0, `still held at frame ${frame}: playback or its tail is active`);
    }
    assert.equal(held.log.released.length, 10, 'once playback and its tail are over, the whole held onset is released');
    held.log.released.forEach((frame, i) => assert.ok(sameSamples(frame, mics[i]), `released frame ${i} is original and in order`));
    assert.equal(held.log.dropped, 0);
});
