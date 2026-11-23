// Deterministic synthetic audio and a replay driver for the real SpeechBoundary. No models, no hardware: this
// exercises Convo's own admission, probe and endpoint logic against known signals, the way the worklet feeds it
// (full 2048-sample blocks). It cannot say how real speech, rooms or microphones behave.
import { SpeechBoundary, feedBoundary, wavBase64 } from '../webui/audio.js';
import { EchoGuard } from '../webui/echo-guard.js';
import { GATE_DEFAULT_DB, NoiseGate } from '../webui/voicing.js';

export const BLOCK = 2048;
export const SERVER_RATE = 16000;

export function rng(seed = 1) {
    let a = seed >>> 0;
    return () => {
        a = (a + 0x6D2B79F5) >>> 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

export const rmsOf = signal => Math.sqrt(signal.reduce((sum, value) => sum + value * value, 0) / Math.max(1, signal.length));
export const silence = (seconds, rate) => new Float32Array(Math.round(seconds * rate));

function scaled(signal, rms) {
    const current = rmsOf(signal);
    if (current > 0) for (let i = 0; i < signal.length; i++) signal[i] *= rms / current;
    return signal;
}

// Broadband noise at an exact RMS (a fan, a television hiss, a room).
export function noise(seconds, rate, { rms = .05, seed = 1 } = {}) {
    const next = rng(seed), out = new Float32Array(Math.round(seconds * rate));
    for (let i = 0; i < out.length; i++) out[i] = (next() + next() + next() + next() - 2);
    return scaled(out, rms);
}

// Syllable-like bursts: noise shaped by a raised-cosine envelope at ~4 Hz, scaled to an exact overall RMS.
export function speechLike(seconds, rate, { rms = .1, seed = 3, syllables = 4.2 } = {}) {
    const next = rng(seed), out = new Float32Array(Math.round(seconds * rate));
    for (let i = 0; i < out.length; i++) {
        const envelope = .2 + .8 * (.5 - .5 * Math.cos(2 * Math.PI * syllables * i / rate));
        out[i] = (next() + next() - 1) * envelope;
    }
    return scaled(out, rms);
}

export const concat = (...parts) => {
    const out = new Float32Array(parts.reduce((n, part) => n + part.length, 0));
    let offset = 0;
    for (const part of parts) { out.set(part, offset); offset += part.length; }
    return out;
};
export const delayed = (signal, seconds, rate) => concat(silence(seconds, rate), signal);
export const gainDb = (signal, db) => signal.map(value => value * 10 ** (db / 20));
export function fadeIn(signal, seconds, rate) {
    const length = Math.min(signal.length, Math.round(seconds * rate)), out = Float32Array.from(signal);
    for (let i = 0; i < length; i++) out[i] *= i / length;
    return out;
}
// a + b over the length of a (b is repeated silence beyond its end).
export const mix = (a, b) => a.map((value, index) => value + (b[index] ?? 0));

// How the browser's capture worklet delivers audio: only full blocks.
export function blocks(signal, size = BLOCK) {
    const out = [];
    for (let start = 0; start + size <= signal.length; start += size) out.push(signal.slice(start, start + size));
    return out;
}

const samplesOf = chunks => chunks.reduce((n, chunk) => n + chunk.length, 0);
export const framesFor = (chunks, rate) => Math.floor(samplesOf(chunks) * SERVER_RATE / rate);

// Real WAV bytes -> frame count, to prove the formula above is what the browser actually uploads.
export function wavFrames(chunks, rate) {
    const bytes = Buffer.from(wavBase64(chunks, rate), 'base64');
    return bytes.readUInt32LE(40) / 2;
}

// Feed a signal through a real SpeechBoundary. Times are audio time (seconds at the end of the block just pushed).
// Without `guard` the signal arrives in full blocks, as the legacy worklet delivers it. With `guard` ('adaptive' | 'strict')
// the microphone goes through the real EchoGuard in 128-sample quanta (what the guard worklet receives) together with
// `reference` (the playback PCM, time-aligned with the capture clock, i.e. the post-duck output at the same instant), and the
// boundary is fed exactly as AudioSession feeds it (feedBoundary, with each released frame's voicing verdict and the session's noise
// gate, `gateDb`, default -50 dBFS, applied after the guard's analysis).
// `allowPreRoll(now)` (seconds) stands in for AudioSession's rule (echo protection Off: no pre-roll during playback and its tail).
export function replay(signal, rate, { maximum = 20, size = BLOCK, guard = null, reference = null, preRoll, minVoiced, quantum = 128, gateDb = GATE_DEFAULT_DB, endOfTurn, allowPreRoll } = {}) {
    const result = { starts: [], probes: [], turns: [], rate, size, guard: null, withheldSamples: 0, releasedSamples: 0, comfortSamples: 0 };
    let now = 0, position = 0;
    const boundary = new SpeechBoundary({
        rate, maximum, clock: () => now * 1000, preRoll, minVoiced, endOfTurn, ...(allowPreRoll ? { allowPreRoll: () => allowPreRoll(now) } : {}),
        onStart: () => result.starts.push(now),
        onProbe: chunks => result.probes.push({ at: now, seconds: samplesOf(chunks) / rate, frames: framesFor(chunks, rate), preRolled: boundary.preRolled }),
        onTurn: chunks => result.turns.push({ at: now, seconds: samplesOf(chunks) / rate, frames: framesFor(chunks, rate), chunks,
            startsAt: now - samplesOf(chunks) / rate, last: boundary.lastTurn }),
    });
    if (guard) {
        const echo = result.guard = new EchoGuard({ inputRate: rate, mode: guard, nativeAec: true, gateDb });
        const gate = new NoiseGate({ rate, gateDb });
        result.voicedReleased = 0;
        for (let start = 0; start + quantum <= signal.length; start += quantum) {
            position = start + quantum; now = position / rate;
            for (const step of echo.ingest(signal.subarray(start, start + quantum), reference ? reference.subarray(start, start + quantum) : null)) {
                result.withheldSamples += step.dropped; result.releasedSamples += step.released.reduce((n, f) => n + f.length, 0);
                result.comfortSamples += step.frames.reduce((n, f, i) => n + (step.comfort[i] ? f.length : 0), 0);
                feedBoundary(boundary, { kind: 'audio', frames: step.frames, voiced: step.voiced, levels: step.levels, comfort: step.comfort, dropped: step.dropped, floor: step.floor, level: step.level }, gate);
            }
        }
    } else for (const block of blocks(signal, size)) { position += block.length; now = position / rate; boundary.push(block); }
    result.boundary = boundary;
    return result;
}

// A virtual clock with timers, so ducking is tested without waiting.
export function virtualTime() {
    let now = 0; const timers = new Set();
    return {
        now: () => now, schedule: (fn, ms) => { const timer = { at: now + ms, fn }; timers.add(timer); return timer; },
        cancel: timer => timers.delete(timer), pending: () => timers.size,
        advance(ms) {
            const end = now + ms;
            for (;;) {
                const next = [...timers].filter(timer => timer.at <= end).sort((a, b) => a.at - b.at)[0];
                if (!next) break;
                timers.delete(next); now = next.at; next.fn();
            }
            now = end;
        },
    };
}

// ---- voicing test signals (synthetic; they say nothing about real voices or rooms) -----------------------------------------
// A two-pole resonator (formant) applied in place.
function resonate(signal, rate, hz, bandwidth) {
    const r = Math.exp(-Math.PI * bandwidth / rate), theta = 2 * Math.PI * hz / rate;
    const a1 = 2 * r * Math.cos(theta), a2 = -r * r, gain = 1 - r;
    let y1 = 0, y2 = 0;
    for (let i = 0; i < signal.length; i++) { const y = gain * signal[i] + a1 * y1 + a2 * y2; y2 = y1; y1 = y; signal[i] = y; }
    return signal;
}
// A voiced source: a glottal-like pulse train (F0 from `f0` with `vibrato` Hz of slow pitch movement and a 5 Hz vibrato), through
// three formant resonators, shaped by a syllable envelope (`syllables` per second, 15% of each syllable near silence), at an RMS.
export function voiced(seconds, rate, { rms = .08, f0 = 140, drift = 20, syllables = 4.5, seed = 7, formants = [[650, 90], [1150, 110], [2600, 160]], breath = .02 } = {}) {
    const next = rng(seed), out = new Float32Array(Math.round(seconds * rate));
    let phase = 0;
    for (let i = 0; i < out.length; i++) {
        const t = i / rate, pitch = f0 + drift * Math.sin(2 * Math.PI * .7 * t + seed) + 3 * Math.sin(2 * Math.PI * 5 * t);
        phase += pitch / rate;
        if (phase >= 1) { phase -= 1; out[i] = 1; }
        out[i] += breath * (next() - .5);
    }
    for (const [hz, bw] of formants) resonate(out, rate, hz, bw);
    for (let i = 0; i < out.length; i++) {
        const cycle = (i / rate * syllables) % 1;
        out[i] *= cycle < .85 ? Math.sin(Math.PI * cycle / .85) ** .6 : 0;
    }
    return scaled(out, rms);
}
// Pink noise (Paul Kellet's filter) at an RMS.
export function pink(seconds, rate, { rms = .03, seed = 11 } = {}) {
    const next = rng(seed), out = new Float32Array(Math.round(seconds * rate));
    let b0 = 0, b1 = 0, b2 = 0, b3 = 0, b4 = 0, b5 = 0, b6 = 0;
    for (let i = 0; i < out.length; i++) {
        const w = next() * 2 - 1;
        b0 = .99886 * b0 + w * .0555179; b1 = .99332 * b1 + w * .0750759; b2 = .969 * b2 + w * .153852;
        b3 = .8665 * b3 + w * .3104856; b4 = .55 * b4 + w * .5329522; b5 = -.7616 * b5 - w * .016898;
        out[i] = b0 + b1 + b2 + b3 + b4 + b5 + b6 + w * .5362; b6 = w * .115926;
    }
    return scaled(out, rms);
}
// A fan: broadband noise through a low resonance (a band around `hz`) plus a weak blade-pass tone, at an RMS.
export function fan(seconds, rate, { rms = .02, hz = 180, seed = 13 } = {}) {
    const base = noise(seconds, rate, { rms: 1, seed });
    resonate(base, rate, hz, 120);
    const out = base.map((value, i) => value + .02 * Math.sin(2 * Math.PI * 97 * i / rate));
    return scaled(out, rms);
}
// Mains hum: 60 Hz with its 120 and 180 Hz harmonics, at an RMS.
export function hum(seconds, rate, { rms = .01, hz = 60 } = {}) {
    const out = Float32Array.from({ length: Math.round(seconds * rate) }, (_, i) => {
        const t = i / rate; return Math.sin(2 * Math.PI * hz * t) + .6 * Math.sin(2 * Math.PI * 2 * hz * t) + .3 * Math.sin(2 * Math.PI * 3 * hz * t);
    });
    return scaled(out, rms);
}
// Transients: claps (a noise burst ringing in a resonance, decaying in ~`decay` s) at the given times, each with a peak amplitude.
export function transients(seconds, rate, times, { peak = .5, decay = .012, hz = 1500, bandwidth = 400, seed = 17 } = {}) {
    const next = rng(seed), out = new Float32Array(Math.round(seconds * rate));
    for (const at of times) {
        const start = Math.round(at * rate), burst = new Float32Array(Math.round(.08 * rate));
        for (let i = 0; i < burst.length; i++) burst[i] = (next() * 2 - 1) * Math.exp(-i / rate / decay);
        resonate(burst, rate, hz, bandwidth);
        let max = 0; for (const value of burst) max = Math.max(max, Math.abs(value));
        for (let i = 0; i < burst.length && start + i < out.length; i++) out[start + i] += burst[i] * peak / (max || 1);
    }
    return out;
}
export const claps = (seconds, rate, options = {}) => transients(seconds, rate, Array.from({ length: Math.floor(seconds * 2) }, (_, i) => .3 + i * .5), { peak: .6, decay: .015, hz: 1200, bandwidth: 600, ...options });
// Typing: key clicks about 8 a second, irregular, each a short bright click and a softer release click.
export function keyboard(seconds, rate, { peak = .25, seed = 19 } = {}) {
    const next = rng(seed), times = [];
    for (let t = .2; t < seconds - .1; t += .08 + next() * .12) { times.push(t); times.push(t + .05 + next() * .03); }
    return transients(seconds, rate, times, { peak, decay: .004, hz: 3200, bandwidth: 900, seed });
}
