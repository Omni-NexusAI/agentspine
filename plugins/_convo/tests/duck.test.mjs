// Stable ducking (BP6, browser side) and the speech-boundary options that the echo guard and pre-roll use. Virtual time only.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { virtualTime } from './signals.mjs';
import { AudioSession, DUCK, DuckController, MIN_VOICED_SECONDS, PRE_ROLL_SECONDS, SpeechBoundary, VOICED_RMS, feedBoundary } from '../webui/audio.js';

function duckFixture(overrides = {}) {
    const time = virtualTime(), applied = [];
    const duck = new DuckController({ apply: (target, ramp) => applied.push({ at: time.now(), target, ramp }), clock: time.now, schedule: time.schedule, cancel: time.cancel, ...overrides });
    return { time, applied, duck };
}

test('Ducking attacks at once on a request, then ramps instead of stepping', () => {
    const { applied, duck } = duckFixture();
    duck.request(true);
    assert.deepEqual(applied, [{ at: 0, target: DUCK.level, ramp: DUCK.attackMs }]);
    duck.request(true); assert.equal(applied.length, 1, 'renewing a duck is not another attack');
});

test('A release is delayed: held at least minHoldMs and releaseDelayMs after the last request, then ramps', () => {
    const { time, applied, duck } = duckFixture();
    duck.request(true); time.advance(100); duck.request(false);
    time.advance(DUCK.releaseDelayMs - 100 - 1); assert.equal(applied.length, 1, 'still ducked just before the delay ends');
    time.advance(1);
    assert.deepEqual(applied.at(-1), { at: DUCK.releaseDelayMs, target: 1, ramp: DUCK.releaseMs });
    assert.ok(DUCK.releaseDelayMs >= DUCK.minHoldMs);
    // With the delay shorter than the minimum hold, the minimum hold governs.
    const short = duckFixture({ releaseDelayMs: 100, minHoldMs: 500 });
    short.duck.request(true); short.time.advance(150); short.duck.request(false);
    short.time.advance(349); assert.equal(short.applied.length, 1); short.time.advance(1);
    assert.equal(short.applied.at(-1).target, 1); assert.equal(short.applied.at(-1).at, 500);
});

test('Probes every 750 ms that each end in "no decision to interrupt" make one duck, not a pumping gain', () => {
    const { time, applied, duck } = duckFixture();
    for (let probe = 0; probe < 8; probe++) {               // a 6 s reply, a probe every .75 s: ducked on the probe, released 150 ms later
        duck.request(true); time.advance(150); duck.request(false); time.advance(600);
    }
    assert.equal(duck.toggles, 1, 'one engage so far, the release still pending');
    time.advance(DUCK.releaseDelayMs);
    assert.equal(duck.toggles, 2); assert.deepEqual(applied.map(entry => entry.target), [DUCK.level, 1]);
});

test('Without hysteresis the same probes toggle sixteen times (the behaviour being replaced)', () => {
    const time = virtualTime(); let toggles = 0, last = 1;
    const instant = held => { const next = held ? .15 : 1; if (next !== last) { toggles++; last = next; } };
    for (let probe = 0; probe < 8; probe++) { instant(true); time.advance(150); instant(false); time.advance(600); }
    assert.equal(toggles, 16);
});

test('A new request during a pending release cancels it; the duck never releases and re-engages', () => {
    const { time, applied, duck } = duckFixture();
    duck.request(true); duck.request(false); time.advance(500); duck.request(true);
    time.advance(DUCK.releaseDelayMs - 1); assert.equal(applied.length, 1);
    assert.equal(duck.toggles, 1);
});

test('Never stuck: a duck nobody renews ends by itself, and a late "release" from the store takes effect at once', () => {
    const { time, applied, duck } = duckFixture();
    duck.request(true); time.advance(DUCK.maxHoldMs - 1); assert.equal(duck.ducked, true);
    time.advance(1); assert.equal(duck.ducked, false); assert.equal(applied.at(-1).target, 1);
    assert.equal(time.pending(), 0, 'no timer is left behind');
    // The store's safety timer asks for release long after the last request: no extra delay applies.
    const late = duckFixture(); late.duck.request(true); late.time.advance(8000); late.duck.request(false);
    assert.equal(late.duck.ducked, false); assert.equal(late.applied.at(-1).ramp, DUCK.releaseMs);
});

test('An immediate release (a finished decision, a new reply) skips the delay and the minimum hold', () => {
    const { applied, duck } = duckFixture();
    duck.request(true); duck.request(false, true);
    assert.equal(duck.ducked, false); assert.equal(applied.at(-1).target, 1); assert.equal(applied.at(-1).ramp, DUCK.attackMs);
    duck.request(false, true); assert.equal(applied.length, 2, 'releasing when not ducked does nothing');
});

test('Disposing the controller leaves no timer', () => {
    const { time, duck } = duckFixture();
    duck.request(true); duck.request(false); duck.dispose();
    assert.equal(time.pending(), 0);
});

test('The audio output uses a ramp when the platform has one; closing cancels a pending release', () => {
    const calls = [], audio = new AudioSession({}); audio.context = { currentTime: 3, state: 'closed' };
    audio.outputGain = { disconnect() {}, gain: { value: 1, setTargetAtTime: (...args) => calls.push(args) } };
    audio.setPolicyHold(true);
    assert.deepEqual(calls, [[DUCK.level, 3, DUCK.attackMs / 3000]]);
    audio.setPolicyHold(false); assert.ok(audio.duck.releaseTimer);
    audio.close();
    assert.equal(audio.duck.releaseTimer, null);
});

// ---- speech boundary: floor, admission threshold, pre-roll ---------------------------------------------------------------

test('The admission threshold is a named constant (450 ms) and the voiced threshold a parameter of push', () => {
    assert.equal(MIN_VOICED_SECONDS, .45); assert.equal(VOICED_RMS, .02); assert.equal(PRE_ROLL_SECONDS, 0);
    const quiet = new Float32Array(1600).fill(.012);                       // rms .012: below the fixed threshold
    const fixed = new SpeechBoundary({ rate: 16000, onStart: () => assert.fail('never voiced'), onTurn: () => {} });
    for (let n = 0; n < 20; n++) fixed.push(quiet);
    assert.equal(fixed.chunks.length, 0);
    let starts = 0;
    const measured = new SpeechBoundary({ rate: 16000, onStart: () => starts++, onTurn: () => {} });
    for (let n = 0; n < 4; n++) measured.push(quiet, .004);
    assert.equal(starts, 0, '400 ms of voiced audio is below the admission threshold: a one-word interruption this short is not detected');
    measured.push(quiet, .004); assert.equal(starts, 1);
    let stricter = 0;
    const custom = new SpeechBoundary({ rate: 16000, minVoiced: .2, onStart: () => stricter++, onTurn: () => {} });
    custom.push(new Float32Array(1600).fill(.1)); custom.push(new Float32Array(1600).fill(.1));
    assert.equal(stricter, 1);
});

test('Pre-roll keeps the last blocks before the first voiced block, counts toward the turn, and keeps the probe cadence from the onset', () => {
    const rate = 16000, turns = [], probes = [];
    const boundary = new SpeechBoundary({ rate, preRoll: .2, onStart: () => {}, onProbe: v => probes.push(v), onTurn: v => turns.push(v) });
    const idle = [1, 2, 3, 4, 5].map(n => new Float32Array(1600).fill(n * 1e-4));   // 100 ms blocks, all below the threshold
    idle.forEach(block => boundary.push(block));
    assert.equal(boundary.chunks.length, 0);
    boundary.push(new Float32Array(1600).fill(.1));
    assert.deepEqual(boundary.chunks.slice(0, 2), idle.slice(-2), 'the two most recent idle blocks (200 ms) lead the turn');
    for (let n = 0; n < 12; n++) boundary.push(new Float32Array(1600).fill(.1));
    assert.ok(probes[0].length >= 9 && probes[0].length <= 10, `first probe about .75 s after the onset: ${probes[0].length} blocks`);
    for (let n = 0; n < 10; n++) boundary.push(new Float32Array(1600));
    assert.equal(turns.length, 1); assert.equal(boundary.ring.length, 0, 'the ring is cleared for the next utterance');
    // An unadmitted noise burst leaves no pre-roll behind for the next one.
    const noisy = new SpeechBoundary({ rate, preRoll: .2, onStart: () => {}, onTurn: () => {} });
    noisy.push(new Float32Array(1600).fill(.1)); for (let n = 0; n < 10; n++) noisy.push(new Float32Array(1600));
    assert.equal(noisy.chunks.length, 0); assert.ok(noisy.ring.length <= 3);
});

test('Pre-roll never makes a turn longer than the maximum', () => {
    const rate = 16000, turns = [];
    const boundary = new SpeechBoundary({ rate, maximum: 1, preRoll: .5, onStart: () => {}, onTurn: v => turns.push(v) });
    for (let n = 0; n < 8; n++) boundary.push(new Float32Array(1600).fill(1e-4));
    for (let n = 0; n < 12; n++) boundary.push(new Float32Array(1600).fill(.1));
    assert.equal(turns.length, 1); assert.equal(turns[0].reduce((n, chunk) => n + chunk.length, 0), 16000);
});

// ---- guard messages -> boundary ------------------------------------------------------------------------------------------

test('feedBoundary: a legacy block is pushed as before; guard frames keep their floor; dropped time is silence, never audio', () => {
    const pushed = [];
    const boundary = { push: (chunk, floor) => pushed.push([chunk.length, floor, chunk[0]]) };
    const block = new Float32Array(2048).fill(.1);
    assert.ok(Math.abs(feedBoundary(boundary, block) - .1) < 1e-6);
    assert.deepEqual(pushed, [[2048, undefined, Math.fround(.1)]], 'the legacy call passes no floor, so the fixed threshold applies');
    pushed.length = 0;
    const frame = new Float32Array(1920).fill(.05);
    assert.ok(Math.abs(feedBoundary(boundary, { kind: 'audio', frames: [frame], dropped: 640, floor: .004 }) - .05) < 1e-6);
    assert.deepEqual(pushed, [[640, .004, 0], [1920, .004, Math.fround(.05)]]);
    pushed.length = 0;
    assert.equal(feedBoundary(boundary, { kind: 'audio', frames: [], dropped: 1920, floor: .02 }), 0);
    assert.deepEqual(pushed, [[1920, .02, 0]]);
});

test('The default timers work the way a browser calls them: window timers throw "Illegal invocation" when called as a method of another object', () => {
    // Found live (not by any stub): the controller kept setTimeout/clearTimeout as its own properties and called them as this.schedule(...).
    const real = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
    const strict = fn => function (...args) { if (this !== undefined && this !== globalThis) throw new TypeError('Illegal invocation'); return fn(...args); };
    globalThis.setTimeout = strict(real.setTimeout); globalThis.clearTimeout = strict(real.clearTimeout);
    try {
        const applied = [];
        const duck = new DuckController({ apply: (target, ramp) => applied.push({ target, ramp }) });
        duck.request(true); duck.request(true); duck.request(false);
        duck.release(); duck.dispose();
        assert.deepEqual(applied.map(entry => entry.target), [DUCK.level, 1]);
    } finally { Object.assign(globalThis, real); }
});
