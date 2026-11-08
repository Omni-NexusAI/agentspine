// Deterministic synthetic audio and a replay driver for the real SpeechBoundary. No models, no hardware: this
// exercises Convo's own admission, probe and endpoint logic against known signals, the way the worklet feeds it
// (full 2048-sample blocks). It cannot say how real speech, rooms or microphones behave.
import { SpeechBoundary, feedBoundary, wavBase64 } from '../webui/audio.js';
import { EchoGuard } from '../webui/echo-guard.js';

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
// boundary is fed exactly as AudioSession feeds it (feedBoundary).
export function replay(signal, rate, { maximum = 20, size = BLOCK, guard = null, reference = null, preRoll, minVoiced, quantum = 128 } = {}) {
    const result = { starts: [], probes: [], turns: [], rate, size, guard: null, withheldSamples: 0, releasedSamples: 0 };
    let now = 0, position = 0;
    const boundary = new SpeechBoundary({
        rate, maximum, clock: () => now * 1000, preRoll, minVoiced,
        onStart: () => result.starts.push(now),
        onProbe: chunks => result.probes.push({ at: now, seconds: samplesOf(chunks) / rate, frames: framesFor(chunks, rate) }),
        onTurn: chunks => result.turns.push({ at: now, seconds: samplesOf(chunks) / rate, frames: framesFor(chunks, rate), chunks,
            startsAt: now - samplesOf(chunks) / rate, last: boundary.lastTurn }),
    });
    if (guard) {
        const echo = result.guard = new EchoGuard({ inputRate: rate, mode: guard, nativeAec: true });
        for (let start = 0; start + quantum <= signal.length; start += quantum) {
            position = start + quantum; now = position / rate;
            for (const step of echo.ingest(signal.subarray(start, start + quantum), reference ? reference.subarray(start, start + quantum) : null)) {
                result.withheldSamples += step.dropped; result.releasedSamples += step.released.reduce((n, f) => n + f.length, 0);
                feedBoundary(boundary, { kind: 'audio', frames: step.released, dropped: step.dropped, floor: step.floor });
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
