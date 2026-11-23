// Voicing (webui/voicing.js): what counts as a human voice, on synthetic signals only. A voiced source (pulse train with pitch
// movement and vibrato through three formants, at a syllable rate) must be voiced; broadband noise, a fan, claps, typing and mains
// hum must not be (hum once the floor has learned it: the warm-up is measured and pinned here). Real voices, rooms and
// microphones are a separate listening test.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Decimator, FLOOR_START_DB, FRAME_SECONDS, GATE_DEFAULT_DB, GATE_OFF_DB, NoiseGate, VOICING_RATE, Voicing, clampGateDb, gateThreshold } from '../webui/voicing.js';
import { claps, concat, fan, hum, keyboard, noise, pink, silence, voiced } from './signals.mjs';

const R = VOICING_RATE;
const room = seconds => noise(seconds, R, { rms: .0008, seed: 99 });          // a quiet room before the sound starts
// Seconds counted voiced after `lead` seconds of quiet room (the floor has settled on the room by then).
function voicedSeconds(signal, { lead = 1, gateDb = GATE_DEFAULT_DB, from = 0 } = {}) {
    const v = new Voicing({ gateDb });
    const results = v.push(concat(room(lead), signal)).slice(Math.round(lead / FRAME_SECONDS) + Math.round(from / FRAME_SECONDS));
    return results.filter(result => result.voiced).length * FRAME_SECONDS;
}

test('A voice is voiced: pitch 100-250 Hz with movement, soft or loud, after a quiet room or from the first frame', () => {
    for (const f0 of [100, 140, 180, 250]) {
        const seconds = voicedSeconds(voiced(3, R, { f0, rms: .05, seed: f0 }));
        assert.ok(seconds >= 2.8, `f0 ${f0}: ${seconds.toFixed(2)} s of 3`);
    }
    assert.ok(voicedSeconds(voiced(3, R, { rms: .008 })) >= 2.8, 'quiet speech (-42 dBFS) is still a voice');
    assert.ok(voicedSeconds(voiced(3, R, { rms: .05 }), { lead: 0 }) >= 2.8, 'no warm-up needed for a voice');
});

test('Broadband noise, pink noise and a fan are never voiced, warm or not', () => {
    for (const lead of [0, 1]) {
        assert.equal(voicedSeconds(noise(4, R, { rms: .03 }), { lead }), 0, `white, lead ${lead}`);
        assert.equal(voicedSeconds(pink(4, R, { rms: .03 }), { lead }), 0, `pink, lead ${lead}`);
        assert.equal(voicedSeconds(fan(4, R, { rms: .02 }), { lead }), 0, `fan, lead ${lead}`);
        assert.equal(voicedSeconds(fan(4, R, { rms: .02, hz: 250 }), { lead }), 0, `wider fan, lead ${lead}`);
    }
});

test('Claps and typing are periodic for a moment but decay at once: never voiced', () => {
    assert.equal(voicedSeconds(claps(4, R)), 0);
    assert.equal(voicedSeconds(keyboard(4, R)), 0);
    assert.equal(voicedSeconds(claps(4, R, { peak: .9 })), 0, 'loud claps too');
});

test('Mains hum is learned into the floor: 60 Hz never counts, 120 Hz at most 0.5 s at the start', () => {
    assert.equal(voicedSeconds(hum(4, R, { rms: .01 }), { lead: 0 }), 0, '60 Hz: its period is below the pitch range');
    const warm = voicedSeconds(hum(4, R, { rms: .01, hz: 120 }), { lead: 0 });
    // A steady pitch and level for STEADY_FRAMES (0.5 s) is a machine: it stops counting and the floor jumps to it. Shorter windows
    // also caught level stretches of a monotone voice and dropped words (live 2026-11-09), so a hum that starts from silence may count
    // up to 0.5 s once; one that is already there never does (below).
    assert.ok(warm <= .52, `120 Hz hum from the first frame: ${warm.toFixed(2)} s counted (floor starts at ${FLOOR_START_DB} dB)`);
    assert.equal(voicedSeconds(hum(4, R, { rms: .01, hz: 120 }), { lead: 0, from: 2 }), 0, 'after two seconds it is floor, not voice');
    // Speech over the learned hum is still voice.
    const v = new Voicing();
    v.push(hum(3, R, { rms: .005, hz: 120 }));
    const over = v.push(hum(2, R, { rms: .005, hz: 120 }).map((value, i, all) => value + voiced(2, R, { rms: .05 })[i]));
    assert.ok(over.filter(result => result.voiced).length * FRAME_SECONDS >= 1.8);
});

test('Hangover: a short gap inside a word does not break voicing, a pause does', () => {
    const word = voiced(.5, R, { rms: .05, syllables: 0.01 });
    const gap = seconds => { const v = new Voicing(); return v.push(concat(word, silence(seconds, R), word)).map(result => result.voiced); };
    const short = gap(.08);
    const shortStart = Math.round(.5 / FRAME_SECONDS);
    assert.ok(short.slice(shortStart, shortStart + 4).every(Boolean), '80 ms gap: still voiced');
    const long = gap(.4);
    assert.ok(long.slice(shortStart + 8, shortStart + 18).every(value => !value), '400 ms pause: not voiced after the 120 ms hangover');
});

test('The noise gate threshold: voicing needs the level too; Off (-66) has no threshold', () => {
    assert.equal(gateThreshold(GATE_OFF_DB), 0);
    assert.ok(Math.abs(gateThreshold(-50) - .00316) < 1e-4);
    assert.equal(clampGateDb(-90), GATE_OFF_DB); assert.equal(clampGateDb(10), -3); assert.equal(clampGateDb('x'), GATE_DEFAULT_DB);
    const soft = voiced(2, R, { rms: .002 });                                  // -54 dBFS
    assert.equal(voicedSeconds(soft, { gateDb: -40 }), 0, 'below the gate: not voiced');
    assert.ok(voicedSeconds(soft, { gateDb: GATE_OFF_DB }) >= 1.8, 'gate off: voicing alone decides');
});

test('Noise gate: opens in 5 ms, holds 250 ms after the level drops, then closes; off passes the block itself', () => {
    const gate = new NoiseGate({ rate: 48000, gateDb: -40 });
    const loud = new Float32Array(480).fill(.05), quiet = new Float32Array(480).fill(.001);
    const opened = gate.process(loud);
    assert.ok(opened[480 - 1] > .049, 'open within the block');
    for (let n = 0; n < 24; n++) gate.process(quiet);                           // 240 ms: still holding
    assert.ok(gate.gain > .99);
    for (let n = 0; n < 20; n++) gate.process(quiet);                           // past the hold, then the 80 ms release
    assert.ok(gate.gain < .1);
    gate.idle(48000);
    assert.ok(gate.gain < 1e-6, 'withheld time closes it too');
    const off = new NoiseGate({ rate: 48000, gateDb: GATE_OFF_DB });
    assert.equal(off.process(quiet), quiet);
});

test('Decimator: 48 kHz and 44.1 kHz blocks of any length become a continuous 16 kHz stream', () => {
    for (const rate of [48000, 44100]) {
        const d = new Decimator(rate);
        let total = 0;
        for (let n = 0; n < 50; n++) total += d.push(new Float32Array(n % 2 ? 2048 : 128).fill(.1)).length;
        const input = 25 * 2048 + 25 * 128;
        assert.ok(Math.abs(total - input * VOICING_RATE / rate) <= 2, `${rate}: ${total}`);
    }
});

test('Cheap enough for a phone: well under 1% of real time on this machine', () => {
    const signal = voiced(10, R), v = new Voicing(), start = performance.now();
    v.push(signal);
    const share = (performance.now() - start) / 10000;
    assert.ok(share < .01, `${(share * 100).toFixed(2)}% of real time`);
});
