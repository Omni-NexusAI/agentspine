import { Decimator, GATE_DEFAULT_DB, NoiseGate, Voicing, clampGateDb } from './voicing.js';

// ---- the 16 kHz upload ------------------------------------------------------------------------------------------------------
// What the server, Whisper and the models receive: 16 kHz mono PCM16. The capture runs at the device's rate (44.1/48 kHz), so it is
// resampled with a windowed-sinc low-pass (Blackman window, RESAMPLE_ZEROS zero crossings a side, cut-off RESAMPLE_CUTOFF_HZ):
// hiss and sibilants above 8 kHz no longer fold back into the speech band, which plain averaging of neighbouring samples let them
// do. Streaming: each block is resampled once as it arrives (TurnEncoder), so a probe does not re-resample the whole utterance.
// The edges hold the first and last sample (a constant stays constant). Below 16 kHz (an 8 kHz phone link) it interpolates.
// The voicing analysis (voicing.js Decimator, the echo guard's own decimation) is unchanged: it never reaches the upload.
export const UPLOAD_RATE = 16000;
export const RESAMPLE_CUTOFF_HZ = 7000;
export const RESAMPLE_ZEROS = 24;
const RESAMPLE_PHASES = 256;
const KERNELS = new Map();

function resampleKernel(fc) {                     // fc: the cut-off in cycles per input sample
    const key = fc.toFixed(9);
    if (KERNELS.has(key)) return KERNELS.get(key);
    const half = Math.ceil(RESAMPLE_ZEROS / (2 * fc)), taps = 2 * half, table = new Float32Array(RESAMPLE_PHASES * taps);
    for (let phase = 0; phase < RESAMPLE_PHASES; phase++) {
        const frac = phase / RESAMPLE_PHASES, row = phase * taps;
        let sum = 0;
        for (let j = 0; j < taps; j++) {
            const d = j - half + 1 - frac, x = 2 * fc * d;
            const sinc = Math.abs(x) < 1e-12 ? 2 * fc : 2 * fc * Math.sin(Math.PI * x) / (Math.PI * x);
            const window = Math.abs(d) >= half ? 0 : .42 + .5 * Math.cos(Math.PI * d / half) + .08 * Math.cos(2 * Math.PI * d / half);
            table[row + j] = sinc * window; sum += sinc * window;
        }
        for (let j = 0; j < taps; j++) table[row + j] /= sum;   // unity gain at DC for every phase
    }
    const kernel = { half, taps, table };
    KERNELS.set(key, kernel);
    return kernel;
}

export class Resampler {
    constructor(inputRate, outputRate = UPLOAD_RATE) {
        this.inputRate = inputRate; this.outputRate = outputRate; this.ratio = inputRate / outputRate;
        this.mode = Math.abs(this.ratio - 1) < 1e-9 ? 'copy' : this.ratio < 1 ? 'linear' : 'sinc';
        this.kernel = this.mode === 'sinc' ? resampleKernel(Math.min(RESAMPLE_CUTOFF_HZ, outputRate * .4375) / inputRate) : null;
        this.reach = this.kernel ? this.kernel.half : 1;        // input samples needed after an output's centre
        this.buffer = new Float32Array(4096); this.base = 0; this.length = 0; this.total = 0; this.produced = 0; this.first = 0; this.last = 0;
    }
    // Output samples owed for the input so far (the same count the averaging upload had).
    get owed() { return Math.floor(this.total * this.outputRate / this.inputRate); }
    sample(index) {
        if (index < 0) return this.first;
        if (index >= this.total) return this.last;
        return this.buffer[index - this.base];
    }
    value(n) {
        if (this.mode === 'copy') return this.sample(n);
        const position = n * this.inputRate / this.outputRate;
        let centre = Math.floor(position);
        if (this.mode === 'linear') { const a = this.sample(centre); return a + (this.sample(centre + 1) - a) * (position - centre); }
        const { half, taps, table } = this.kernel;
        let phase = Math.round((position - centre) * RESAMPLE_PHASES);
        if (phase === RESAMPLE_PHASES) { phase = 0; centre++; }
        const row = phase * taps, start = centre - half + 1;
        let sum = 0;
        if (start >= this.base && start + taps <= this.base + this.length) {
            const offset = start - this.base, buffer = this.buffer;
            for (let j = 0; j < taps; j++) sum += buffer[offset + j] * table[row + j];
        } else for (let j = 0; j < taps; j++) sum += this.sample(start + j) * table[row + j];
        return sum;
    }
    // Feed capture samples; returns the 16 kHz samples that are now final.
    push(samples) {
        if (!samples?.length) return new Float32Array(0);
        if (!this.total) this.first = samples[0];
        if (this.length + samples.length > this.buffer.length) {
            const grown = new Float32Array(Math.max(this.buffer.length * 2, this.length + samples.length));
            grown.set(this.buffer.subarray(0, this.length)); this.buffer = grown;
        }
        this.buffer.set(samples, this.length); this.length += samples.length; this.total += samples.length; this.last = samples[samples.length - 1];
        const out = [];
        for (;;) {
            const n = this.produced, centre = this.mode === 'copy' ? n : Math.floor(n * this.inputRate / this.outputRate) + 1;
            if (n >= this.owed || centre + this.reach >= this.total) break;
            out.push(this.value(n)); this.produced++;
        }
        // Keep only what later outputs can still reach.
        const keepFrom = Math.max(0, (this.mode === 'copy' ? this.produced : Math.floor(this.produced * this.inputRate / this.outputRate)) - this.reach - 1);
        if (keepFrom - this.base > 8192) {
            const drop = keepFrom - this.base;
            this.buffer.copyWithin(0, drop, this.length); this.length -= drop; this.base = keepFrom;
        }
        return Float32Array.from(out);
    }
    // The rest of what is owed, as if the input ended here (edge held), without consuming it: a probe's snapshot.
    tail() {
        const out = new Float32Array(Math.max(0, this.owed - this.produced));
        for (let i = 0; i < out.length; i++) out[i] = this.value(this.produced + i);
        return out;
    }
    // The input ended: the rest, consumed.
    finish() { const out = this.tail(); this.produced += out.length; return out; }
}

function toPcm16(view, at, value) {
    const v = Math.max(-1, Math.min(1, value));
    view.setInt16(at, Math.round(v * (v < 0 ? 32768 : 32767)), true);
}
function wavHeader(view, rate, frames) {
    const tag = (start, text) => [...text].forEach((c, i) => view.setUint8(start + i, c.charCodeAt(0)));
    tag(0, 'RIFF'); view.setUint32(4, 36 + frames * 2, true); tag(8, 'WAVE'); tag(12, 'fmt ');
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true);
    view.setUint16(32, 2, true); view.setUint16(34, 16, true); tag(36, 'data'); view.setUint32(40, frames * 2, true);
}
function base64Of(bytes) {
    let binary = '';
    for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
    return btoa(binary);
}
// PCM16 bytes already encoded (`pcm`, `pcmBytes` of it) plus float samples -> base64 WAV.
function wavFrom(rate, pcm, pcmBytes, samples) {
    const frames = pcmBytes / 2 + samples.length, bytes = new Uint8Array(44 + frames * 2), view = new DataView(bytes.buffer);
    wavHeader(view, rate, frames);
    if (pcmBytes) bytes.set(pcm.subarray(0, pcmBytes), 44);
    for (let i = 0; i < samples.length; i++) toPcm16(view, 44 + pcmBytes + i * 2, samples[i]);
    return base64Of(bytes);
}

export function wavBase64(chunks, sourceRate, targetRate = UPLOAD_RATE) {
    const resampler = new Resampler(sourceRate, targetRate), parts = chunks.map(chunk => resampler.push(chunk));
    parts.push(resampler.finish());
    const samples = new Float32Array(parts.reduce((n, part) => n + part.length, 0));
    let offset = 0;
    for (const part of parts) { samples.set(part, offset); offset += part.length; }
    return wavFrom(targetRate, new Uint8Array(0), 0, samples);
}

// One utterance's upload, kept as it grows: every block is resampled and packed to PCM16 once. A probe is the cached part plus a
// provisional end (as if the input stopped there); the completed turn finishes it. A new utterance (its first block differs) starts over.
export class TurnEncoder {
    constructor(rate, targetRate = UPLOAD_RATE) { this.rate = rate; this.targetRate = targetRate; this.clear(); }
    clear() { this.first = null; this.used = 0; this.resampler = new Resampler(this.rate, this.targetRate); this.pcm = new Uint8Array(0); this.bytes = 0; }
    append(samples) {
        if (!samples.length) return;
        if (this.bytes + samples.length * 2 > this.pcm.length) {
            const grown = new Uint8Array(Math.max(this.pcm.length * 2, this.bytes + samples.length * 2, 65536));
            grown.set(this.pcm.subarray(0, this.bytes)); this.pcm = grown;
        }
        const view = new DataView(this.pcm.buffer);
        for (let i = 0; i < samples.length; i++) toPcm16(view, this.bytes + i * 2, samples[i]);
        this.bytes += samples.length * 2;
    }
    encode(chunks, final = false) {
        if (!chunks.length || chunks[0] !== this.first || chunks.length < this.used) { this.clear(); this.first = chunks[0] ?? null; }
        for (; this.used < chunks.length; this.used++) this.append(this.resampler.push(chunks[this.used]));
        const wav = wavFrom(this.targetRate, this.pcm, this.bytes, final ? this.resampler.finish() : this.resampler.tail());
        if (final) this.clear();
        return wav;
    }
}

// A block is voiced above this RMS unless the capture supplies its own floor (the echo guard's measured noise floor).
export const VOICED_RMS = .02;
// How much voiced audio a candidate needs before it is admitted (onStart). With the echo guard the guard confirms the same
// 450 ms of independent speech first and the released onset then admits at once, so the two do not stack. A one-word
// interruption shorter than this ("stop", ~0.3 s) is not detected; lowering it admits more noise, so it is a constant here and is
// only to be changed together with replay measurements (tests/echo-measure.mjs), never silently.
export const MIN_VOICED_SECONDS = .45;
// Audio kept from before the first voiced block, so the start of an utterance reaches the model: voicing confirms a voice only after
// a few clearly periodic frames, so an unvoiced first sound ("T", "S", "F") and the first 60 ms were cut ("Tell me a joke." was heard
// as "me a joke.", live 2026-11-09). The standalone fork keeps 500 ms (speech_pad_ms); 400 ms is the smallest that keeps every
// first word the corpus replay can keep (537 recorded/synthesised clips through the real voicing, off and adaptive paths, 2026-11-17:
// sound onset to first voiced block median 62 ms, 90th percentile 137 ms; 0.3 s kept 1058 of 1070 onsets, 0.35 s 1063, 0.4-0.5 s
// 1064; the other 6 start with a short word voicing never confirms ("Stop.", "Yeah,"), which no pre-roll of 0.5 s or less reaches).
// To be re-measured live. Never counted as speech (admission, voiced_s and the safety yield are unchanged) and reported to the
// server as `pre_roll_s`, which subtracts it from "short command" and the joining gap. The pre-roll and the block that opens the turn
// are sent ungated (the gate's attack cannot mute a soft onset), with a PRE_ROLL_FADE_MS fade-in. With echo protection Off there is
// no pre-roll while the assistant plays or within its echo tail.
export const PRE_ROLL_SECONDS = .4;
export const PRE_ROLL_FADE_MS = 8;
// Probe cadence: seconds of the utterance between two probes. Talking over the assistant (overlap) probes at once and then every
// OVERLAP_PROBE_SECONDS, so a real interruption is decided sooner; any other utterance keeps the ordinary cadence.
export const PROBE_SECONDS = .75;
export const OVERLAP_PROBE_SECONDS = .5;
// Silence that ends a turn: the setting `end_of_turn_seconds` (0.5-2.0, default 0.9), live per device (sound panel).
export const END_OF_TURN_SECONDS = .9;
export const END_OF_TURN_RANGE = Object.freeze([.5, 2]);
export const clampEndOfTurn = value => { const n = Number(value); return Number.isFinite(n) ? Math.max(END_OF_TURN_RANGE[0], Math.min(END_OF_TURN_RANGE[1], n)) : END_OF_TURN_SECONDS; };

export class SpeechBoundary {
    // `clock` only timestamps what happened (measurement); it never decides anything.
    // `allowPreRoll()`: false while audio before an utterance may hold the assistant's own voice (echo protection Off, during playback
    // and its tail); the ring is then emptied, so no pre-roll reaches back into that time.
    constructor({ rate, maximum = 20, onStart, onTurn, onProbe = () => {}, clock = () => globalThis.performance?.now?.() ?? Date.now(),
        minVoiced = MIN_VOICED_SECONDS, preRoll = PRE_ROLL_SECONDS, probeEvery = PROBE_SECONDS, endOfTurn = END_OF_TURN_SECONDS, allowPreRoll = () => true }) {
        Object.assign(this, { rate, maximum, onStart, onTurn, onProbe, clock, minVoiced, preRoll, cadence: probeEvery, allowPreRoll });
        this.endSilence = clampEndOfTurn(endOfTurn);
        // The longest turn, counted in whole samples so the cut is exact (see push). Not a number: never cut by length.
        const limit = Math.floor(maximum * rate);
        this.limit = Number.isFinite(limit) ? Math.max(1, limit) : Infinity;
        this.lastTurn = null;
        this.reset();
    }
    reset() { this.chunks = []; this.samples = 0; this.duration = 0; this.silence = 0; this.speech = 0; this.admitted = false; this.lastProbe = 0; this.onsetAt = 0; this.voicedAt = 0; this.ring = []; this.ringDuration = 0; this.preRolled = 0; this.probeEvery = this.cadence; }
    // Live: the silence that ends a turn (applies to the utterance in progress too).
    setEndOfTurn(seconds) { this.endSilence = clampEndOfTurn(seconds); }
    // `verdict`: the capture's voicing decision for this block (voicing.js through the echo guard, or computed beside the unguarded
    // capture). Only voiced blocks count toward minVoiced, the speech total and voiced_s. Without a verdict (a legacy block), the
    // block's RMS against `floor` decides, as before. `raw`: the same block before the noise gate (the pre-roll and the opening block
    // are sent ungated); the gated block itself when not given.
    push(chunk, floor = VOICED_RMS, verdict, raw = chunk) {
        // The worklet delivers whole blocks, so the maximum-length cut usually falls inside one. Cutting on the block
        // edge would upload a turn longer than the maximum (up to a block more at 44.1/48 kHz) and the server rejects
        // it, so the turn ends on the exact sample and the rest of the block starts the next utterance.
        const voiced = typeof verdict === 'boolean' ? verdict
            : Math.sqrt(chunk.reduce((n, v) => n + v * v, 0) / chunk.length) > floor;   // decided on the whole block, as delivered
        if (!raw || raw.length !== chunk.length) raw = chunk;
        while (chunk.length) {
            const room = this.limit - this.samples;
            if (chunk.length <= room) return this.take(chunk, voiced, raw);
            this.take(chunk.subarray(0, room), voiced, raw.subarray(0, room));
            chunk = chunk.subarray(room); raw = raw.subarray(room);
        }
    }
    take(chunk, voiced, raw = chunk) {
        const duration = chunk.length / this.rate;
        if (!this.chunks.length && !voiced) { this.remember(raw); return; }
        const opening = !this.chunks.length;
        if (opening) { this.onsetAt = this.clock(); this.prepend(chunk.length); chunk = raw; }
        this.chunks.push(chunk);
        if (opening) this.fadeIn();
        if (voiced) this.voicedAt = this.clock();
        this.samples += chunk.length;
        this.duration += duration;
        this.silence = voiced ? 0 : this.silence + duration;
        this.speech = voiced ? this.speech + duration : this.speech;
        if (!this.admitted && this.speech >= this.minVoiced) {
            this.admitted = true;
            this.onStart();
        }
        if (this.admitted && this.duration - this.lastProbe >= this.probeEvery && this.samples < this.limit && this.silence < this.endSilence) {
            this.lastProbe = this.duration;
            this.onProbe(this.chunks.slice()); // One bounded completed WAV snapshot, not a user turn.
        }
        if (this.samples >= this.limit || this.silence >= this.endSilence) {
            const chunks = this.chunks;
            const admitted = this.admitted;
            this.lastTurn = { onsetAt: this.onsetAt, voicedAt: this.voicedAt, endedAt: this.clock(), duration: this.duration, cut: this.samples >= this.limit, voiced: this.speech, preRolled: this.preRolled };
            this.reset();
            if (admitted) this.onTurn(chunks);
        }
    }
    // The utterance ends here (the microphone was let go of): what it holds is completed as a turn when it was admitted, else dropped.
    finish() {
        const chunks = this.chunks, admitted = this.admitted;
        if (chunks.length) this.lastTurn = { onsetAt: this.onsetAt, voicedAt: this.voicedAt, endedAt: this.clock(), duration: this.duration, cut: false, voiced: this.speech, preRolled: this.preRolled };
        this.reset();
        if (admitted) this.onTurn(chunks);
        return admitted;
    }
    // Probe at once, then every `seconds` for the rest of this utterance (overlap). Called from onStart, the probe is taken in the
    // same block; the next utterance starts at the ordinary cadence again (reset).
    quicken(seconds = OVERLAP_PROBE_SECONDS) { this.probeEvery = seconds; this.lastProbe = -Infinity; }
    // Seconds from the first voiced sample of the open utterance to the end of the newest block (pre-roll is not speech).
    spokenFor() { return this.chunks.length ? Math.max(0, this.duration - this.preRolled) : 0; }
    // The pre-roll: the most recent not-yet-voiced blocks (whole blocks, at least `preRoll` seconds when available), ungated.
    remember(chunk) {
        if (!(this.preRoll > 0)) return;
        let allowed = true;
        try { allowed = this.allowPreRoll() !== false; } catch {}
        if (!allowed) { this.ring = []; this.ringDuration = 0; return; }
        this.ring.push(chunk); this.ringDuration += chunk.length / this.rate;
        while (this.ring.length > 1 && this.ringDuration - this.ring[0].length / this.rate >= this.preRoll) this.ringDuration -= this.ring.shift().length / this.rate;
    }
    prepend(next) {
        if (!this.ring.length) return;
        // The pre-roll counts toward the turn length, so it must leave room for the block that opens the turn.
        while (this.ring.length && this.ring.reduce((n, c) => n + c.length, 0) + next > this.limit - this.samples) this.ring.shift();
        for (const old of this.ring) { this.chunks.push(old); this.samples += old.length; this.duration += old.length / this.rate; this.preRolled += old.length / this.rate; }
        this.lastProbe = this.duration;                              // probes keep their cadence from the onset, not from the pre-roll
        this.ring = []; this.ringDuration = 0;
    }
    // The turn's first PRE_ROLL_FADE_MS rise from silence (copies: a ring block may be shared with a caller).
    fadeIn() {
        const length = Math.max(1, Math.round(PRE_ROLL_FADE_MS / 1000 * this.rate));
        let done = 0;
        for (let index = 0; index < this.chunks.length && done < length; index++) {
            const faded = Float32Array.from(this.chunks[index]);
            for (let i = 0; i < faded.length && done < length; i++, done++) faded[i] *= done / length;
            this.chunks[index] = faded;
        }
    }
}

// Applies one capture-worklet message to a SpeechBoundary and returns the raw input level to show (the microphone before the
// guard and the gate). A plain Float32Array is the legacy capture block. With the echo guard a message carries only original
// microphone samples the guard has released, each with its voicing verdict (`voiced`) and raw level (`levels`), plus `dropped`:
// samples it withheld for good (echo / ambiguous). Those are time passing as silence and are never the echo itself. `gate`
// (a voicing.js NoiseGate, optional) is applied to what is counted and sent, after the guard's analysis; the ungated frame goes beside
// it for the pre-roll. A frame whose `comfort[index]` is true is floor-level noise the guard put in place of withheld time while the
// user talks over the assistant (so their speech has no digital holes): it bypasses the gate and is never voiced.
export function feedBoundary(boundary, data, gate = null) {
    const rmsOf = block => Math.sqrt(block.reduce((sum, value) => sum + value * value, 0) / Math.max(1, block.length));
    if (data?.kind !== 'audio') { boundary.push(gate ? gate.process(data) : data, undefined, undefined, data); return rmsOf(data); }
    let level = Number.isFinite(data.level) ? data.level : 0;
    if (data.dropped > 0) { gate?.idle(data.dropped); boundary.push(new Float32Array(data.dropped), data.floor, false); }
    data.frames.forEach((frame, index) => {
        if (data.comfort?.[index] === true) { gate?.idle(frame.length); boundary.push(frame, data.floor, false, frame); return; }
        const raw = Number.isFinite(data.levels?.[index]) ? data.levels[index] : rmsOf(frame);
        if (!Number.isFinite(data.level)) level = Math.max(level, raw);
        const verdict = typeof data.voiced?.[index] === 'boolean' ? data.voiced[index] : undefined;
        boundary.push(gate ? gate.process(frame, raw) : frame, data.floor, verdict, frame);
    });
    return level;
}

// The capture without the echo guard (echo protection Off): the same voicing, computed beside the untouched legacy worklet on
// the page's thread (decimated to 16 kHz for analysis only), and the same gate. Returns {level, voiced} for the meter.
export class UnguardedCapture {
    constructor({ rate, gateDb = GATE_DEFAULT_DB }) {
        this.decimator = new Decimator(rate); this.voicing = new Voicing({ gateDb }); this.gate = new NoiseGate({ rate, gateDb });
        this.voiced = false;
    }
    setGate(db) { this.voicing.setGate(db); this.gate.setGate(db); }
    feed(boundary, block) {
        const level = Math.sqrt(block.reduce((sum, value) => sum + value * value, 0) / Math.max(1, block.length));
        const results = this.voicing.push(this.decimator.push(block));
        if (results.length) this.voiced = results.some(item => item.voiced);
        boundary.push(this.gate.process(block, level), VOICED_RMS, this.voiced, block);
        return { level, voiced: this.voiced };
    }
}

// Stable ducking. Uncertainty about the user's speech lowers the output; the controller turns the per-probe on/off requests
// into one duck: attack quickly, hold for at least minHoldMs, release only releaseDelayMs after the last request to duck
// (longer than the 750 ms probe cadence, so a continuing utterance does not pump), and ramp both ways. Never stuck: any
// duck ends maxHoldMs after the last request, and the store's own safety timer still asks for a release.
export const DUCK = Object.freeze({ level: .15, attackMs: 40, releaseMs: 250, minHoldMs: 350, releaseDelayMs: 850, maxHoldMs: 12000 });

export class DuckController {
    constructor({ apply, clock = () => globalThis.performance?.now?.() ?? Date.now(), schedule = setTimeout, cancel = clearTimeout, ...overrides }) {
        // Wrapped: a window timer called as a method of this object (this.schedule(...)) throws "Illegal invocation" in a browser.
        Object.assign(this, DUCK, overrides, { apply, clock: () => clock(), schedule: (fn, ms) => schedule(fn, ms), cancel: id => cancel(id) });
        this.ducked = false; this.engagedAt = 0; this.lastHeldAt = 0; this.toggles = 0; this.engagements = 0; this.releaseTimer = null; this.maxTimer = null;
    }
    request(held, immediate = false) {
        const now = this.clock();
        if (held) {
            this.lastHeldAt = now;
            this.cancel(this.releaseTimer); this.releaseTimer = null;
            this.cancel(this.maxTimer); this.maxTimer = this.schedule(() => { this.maxTimer = null; this.release(); }, this.maxHoldMs);
            this.maxTimer?.unref?.();
            if (!this.ducked) { this.ducked = true; this.engagedAt = now; this.toggles++; this.engagements++; this.apply(this.level, this.attackMs); }
            return;
        }
        if (!this.ducked) return;
        const wait = immediate ? 0 : Math.max(this.engagedAt + this.minHoldMs, this.lastHeldAt + this.releaseDelayMs) - now;
        if (wait <= 0) return this.release(immediate ? this.attackMs : this.releaseMs);
        this.cancel(this.releaseTimer);
        this.releaseTimer = this.schedule(() => { this.releaseTimer = null; this.release(); }, wait);
        this.releaseTimer?.unref?.();
    }
    release(rampMs = this.releaseMs) {
        this.cancel(this.releaseTimer); this.releaseTimer = null; this.cancel(this.maxTimer); this.maxTimer = null;
        if (!this.ducked) return;
        this.ducked = false; this.toggles++; this.apply(1, rampMs);
    }
    dispose() { this.cancel(this.releaseTimer); this.cancel(this.maxTimer); this.releaseTimer = this.maxTimer = null; }
}

// Talking over the assistant: assistant audio that stopped this recently still counts as heard (the echo guard's own echo tail,
// echo-guard.js ECHO_TAIL_MS; a test keeps them equal).
export const OVERLAP_TAIL_MS = 350;
// `continue` (a backchannel or a side remark while the assistant speaks): the duck lifts this long after the user's speech ends.
export const CONTINUE_RELEASE_MS = 400;
// PCM bounds: how far ahead of playback audio may be scheduled and how many sources at once. Memory stays bounded by the 16 MiB
// a phrase may carry.
export const PCM_AHEAD_SECONDS = 90;
export const PCM_MAX_SOURCES = 4096;
// A phrase that should have started (or a buffered one that should have ended) this long ago while the context was running is
// released as interrupted: playback that never advances must not block the phrases after it.
export const STUCK_PHRASE_MS = 4000;
export const MAX_PRIME_MS = 2000;          // the longest start-up buffer a phrase may ask for (audio.begin prime_ms)
export const MAX_FADE_MS = 1000;           // a faded stop (`candidate.admitted` fade_ms) is bounded
// How long a resume may take before the output counts as paused: a browser may hold resume() until the next user gesture.
export const RESUME_WAIT_MS = 600;
// The microphone: a mute is reported only after it lasted this long; a microphone at or below LOW_MIC_RATE carries less than the
// speech band the models were measured on.
export const MIC_MUTE_REPORT_MS = 1500;
export const LOW_MIC_RATE = 16000;
// A Bluetooth headset in its call profile (HFP/HSP) shows these words in its name on Windows, macOS, Android and Linux.
const HANDS_FREE = /hands[- ]?free|\bHFP\b|\bHSP\b|AG Audio|headset \(.*bluetooth|bluetooth.*headset/i;
// A warning (words) for a microphone that limits what Convo can hear, or '' for none. Read from the live track only.
export function microphoneWarning(track) {
    let settings = {};
    try { settings = track?.getSettings?.() || {}; } catch {}
    const label = String(track?.label || '');
    if (HANDS_FREE.test(label)) return 'This microphone is a Bluetooth hands-free (call) link: it carries only narrow-band speech, so Convo and the models hear less of you. A wired or USB microphone, or the headset in its media (stereo) mode, sounds clearer.';
    const rate = Number(settings.sampleRate);
    if (Number.isFinite(rate) && rate > 0 && rate <= LOW_MIC_RATE) return `This microphone runs at ${rate / 1000} kHz, which cuts off the upper speech band, so consonants such as "s" and "f" are harder to hear. A microphone at 44.1 or 48 kHz sounds clearer.`;
    return '';
}
// "Record raw and sent" keeps at most this much raw microphone audio.
export const RAW_MAX_SECONDS = 30;
// A 16-bit mono WAV (base64) of float samples at their own rate, unchanged (the raw recording keeps the device's rate).
export function wavAtRate(chunks, rate) {
    const total = chunks.reduce((n, chunk) => n + chunk.length, 0), samples = new Float32Array(total);
    let offset = 0;
    for (const chunk of chunks) { samples.set(chunk, offset); offset += chunk.length; }
    return wavFrom(rate, new Uint8Array(0), 0, samples);
}
// The test sound: short and soft.
export const TONE = Object.freeze({ hz: 660, peak: .08, seconds: .45 });
const KEEPS_WORKING = 'Convo keeps listening; the next phrase plays normally.';
const clockNow = () => globalThis.performance?.now?.() ?? Date.now();
const round3 = value => Math.round(value * 1000) / 1000;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

// What is rendered now is heard this much later (seconds), when the browser says.
export function outputLatency(context) {
    const value = Number(context?.outputLatency) || Number(context?.baseLatency) || 0;
    return Number.isFinite(value) && value > 0 ? value : 0;
}
// The output device a context plays to: its sinkId, or 'default' for the system default (or a browser that cannot choose).
export function sinkLabel(context) {
    const sink = context?.sinkId;
    if (typeof sink === 'string') return sink || 'default';
    return sink?.type === 'none' ? 'none' : 'default';
}
// One soft tone into `destination`, with an optional meter. Returns its length in seconds.
function playTone(context, destination, onLevel = null) {
    const start = context.currentTime, end = start + TONE.seconds;
    const oscillator = context.createOscillator(), envelope = context.createGain();
    oscillator.type = 'sine'; oscillator.frequency.value = TONE.hz;
    envelope.gain.setValueAtTime(0, start); envelope.gain.linearRampToValueAtTime(TONE.peak, start + .03);
    envelope.gain.setValueAtTime(TONE.peak, end - .12); envelope.gain.linearRampToValueAtTime(0, end);
    let meter = null, timer = null;
    if (onLevel && context.createAnalyser) {
        meter = context.createAnalyser(); meter.fftSize = 256;
        const values = new Float32Array(256);
        timer = setInterval(() => { meter.getFloatTimeDomainData(values); onLevel(Math.min(1, Math.sqrt(values.reduce((n, v) => n + v * v, 0) / values.length) * 6)); }, 50);
        oscillator.connect(envelope).connect(meter).connect(destination);
    } else oscillator.connect(envelope).connect(destination);
    oscillator.onended = () => { clearInterval(timer); if (timer) onLevel(0); oscillator.disconnect(); envelope.disconnect(); meter?.disconnect(); };
    oscillator.start(start); oscillator.stop(end + .02);
    return TONE.seconds;
}

// ---- one audio output per page ----------------------------------------------------------------------------------------------
// Android Chrome picks the call or the media stream each time a page opens its audio output, so every reopen can move the sound to
// another speaker at another volume. The page therefore keeps ONE AudioContext, reused by every conversation, chat switch and test
// sound. It is suspended only while no microphone is open (release), resumed when one opens (acquire), recreated only if the browser
// closed it, and never suspended and resumed to follow a device (a browser that cannot choose an output keeps playing where it plays,
// and the state is reported instead). A chosen output device (`setSinkId`, desktop Chrome, Edge and Electron) is followed: when it
// disappears the default is used and said, and the choice comes back when the device does. Counts are for diagnostics only.
export class PageAudio {
    constructor({ create = null, devices = () => globalThis.navigator?.mediaDevices, resumeWaitMs = RESUME_WAIT_MS } = {}) {
        this.create = create || (() => { const Context = globalThis.AudioContext || globalThis.webkitAudioContext; return Context ? new Context() : null; });
        this.devices = devices; this.resumeWaitMs = resumeWaitMs;
        this.context = null; this.users = 0; this.modules = new Set(); this.chosen = ''; this.using = ''; this.missing = false;
        this.stats = { created: 0, recreated: 0, reopened: 0, suspended: 0, last_reopen: 0, sink_changes: 0, fallbacks: 0 };
        this.listeners = new Set(); this.wasPaused = false;
    }
    // The context, created on first use and again only if the browser closed it.
    ensure() {
        if (this.context && this.context.state !== 'closed') return this.context;
        const previous = this.context;
        const context = this.create();
        if (!context) return null;
        this.context = context; this.modules = new Set(); this.stats.created++;
        if (previous) { this.stats.recreated++; this.noteReopen(); }
        this.wasPaused = context.state !== 'running';
        try {
            context.addEventListener?.('statechange', () => {
                if (this.context !== context) return;
                const paused = context.state === 'suspended' || context.state === 'interrupted';
                if (this.wasPaused && context.state === 'running') this.noteReopen();      // a resumed output is a reopened one (Android)
                this.wasPaused = paused;
            });
        } catch { /* observational only */ }
        if (this.chosen && this.supportsSink()) void this.applySink(this.chosen);
        return context;
    }
    noteReopen() { this.stats.reopened++; this.stats.last_reopen = Date.now(); }
    // Keep the output running across a gap with no microphone (a chat switch recreates the capture): returns the release.
    keep() { this.users++; let done = false; return () => { if (!done) { done = true; this.release(); } }; }
    // A microphone opens: the context runs (a resume the browser holds until a gesture is not waited for long).
    async acquire() {
        const context = this.ensure();
        if (!context) throw new Error('This browser has no Web Audio output.');
        this.users++;
        if (context.state !== 'running') { try { await Promise.race([Promise.resolve(context.resume()).catch(() => {}), wait(this.resumeWaitMs)]); } catch {} }
        return context;
    }
    // A microphone closed: with none left open the context is suspended (Android then leaves call mode); it is never closed.
    release() {
        this.users = Math.max(0, this.users - 1);
        const context = this.context;
        if (this.users || !context || context.state !== 'running') return;
        this.stats.suspended++;
        try { void Promise.resolve(context.suspend?.()).catch(() => {}); } catch {}
    }
    // A worklet module is added once per context (a second addModule of the same file is not needed and some engines refuse it).
    async addModule(context, url) {
        if (this.context === context && this.modules.has(url)) return;
        await context.audioWorklet.addModule(url);
        if (this.context === context) this.modules.add(url);
    }
    supportsSink() {
        const context = this.context, Context = globalThis.AudioContext || globalThis.webkitAudioContext;
        return typeof context?.setSinkId === 'function' || typeof Context?.prototype?.setSinkId === 'function';
    }
    // The output devices this browser can name (labels need microphone permission, which a running conversation has).
    async outputs() {
        try {
            const media = this.devices();
            if (!media?.enumerateDevices) return [];
            const list = await media.enumerateDevices();
            return list.filter(device => device.kind === 'audiooutput' && device.deviceId && device.deviceId !== 'default' && device.deviceId !== 'communications')
                .map(device => ({ id: String(device.deviceId), label: String(device.label || '').slice(0, 80) }));
        } catch { return []; }
    }
    async applySink(id) {
        const context = this.context;
        if (!context || typeof context.setSinkId !== 'function') return false;
        try { await context.setSinkId(id); this.using = id; this.stats.sink_changes++; return true; } catch { return false; }
    }
    // Choose where the page plays ('' = the system default). Returns {ok, using, missing, message}.
    async choose(id) {
        this.chosen = typeof id === 'string' ? id.slice(0, 200) : '';
        if (!this.supportsSink()) return { ok: false, using: '', missing: false, message: 'This browser plays to the system default output and cannot choose another.' };
        return this.follow();
    }
    // The devices changed (or a choice was made): play to the chosen device while it is there, else to the default, and say which.
    async follow() {
        const context = this.context;
        if (!context || context.state === 'closed') return { ok: false, using: '', missing: false, message: '' };
        if (!this.supportsSink()) return { ok: true, using: '', missing: false, message: '' };
        let message = '';
        if (!this.chosen) { if (this.using !== '' || this.missing) await this.applySink(''); this.missing = false; }
        else {
            const present = (await this.outputs()).some(device => device.id === this.chosen);
            if (!present) {
                if (!this.missing) { this.stats.fallbacks++; message = 'The chosen output device is gone, so Convo plays to the system default until it comes back.'; }
                this.missing = true;
                if (this.using !== '') await this.applySink(''); else if (context.sinkId) await this.applySink('');
            } else {
                if (this.missing) message = 'The chosen output device is back; Convo plays to it again.';
                this.missing = false;
                if (this.using !== this.chosen || (typeof context.sinkId === 'string' && context.sinkId !== this.chosen)) await this.applySink(this.chosen);
            }
        }
        return { ok: true, using: this.using, missing: this.missing, message };
    }
    snapshot() {
        const context = this.context;
        return { state: String(context?.state || ''), sink: context ? sinkLabel(context) : '', chosen: this.chosen, missing: this.missing, users: this.users,
                 can_choose: this.supportsSink(), ...this.stats };
    }
    // The test sound through the page's one output (no session): the context is resumed for it and suspended again after when no
    // microphone is open.
    async testTone(onLevel = null) {
        const context = this.ensure();
        if (!context) return { ok: false, state: '', sink: '', message: 'This browser has no Web Audio output.' };
        try { await Promise.race([Promise.resolve(context.resume()).catch(() => {}), wait(this.resumeWaitMs)]); } catch {}
        const info = { state: String(context.state || ''), sink: sinkLabel(context), latency: round3(outputLatency(context)) };
        if (context.state !== 'running') return { ok: false, ...info, message: `The audio output is ${info.state || 'not running'}; click again to try.` };
        const seconds = playTone(context, context.destination, onLevel);
        setTimeout(() => { if (!this.users && this.context === context && context.state === 'running') { try { void Promise.resolve(context.suspend?.()).catch(() => {}); } catch {} } }, seconds * 1000 + 250);
        return { ok: true, path: 'page', ...info };
    }
}
let pageAudio = null;
// The page's one output (created on first use).
export function sharedPageAudio() { if (!pageAudio) pageAudio = new PageAudio(); return pageAudio; }

// ---- the re-buffering jitter buffer -------------------------------------------------------------------------------------------
// A streamed phrase whose next chunk arrives after the scheduled audio already ran out (a slow request, a network hiccup) would
// otherwise play each late chunk the moment it lands: a stutter of short gaps mid-sentence. Instead the player re-buffers: it holds
// the arriving audio until REBUFFER_MS of it is there (or the phrase ends), then plays it back to back, so one late stretch costs one
// pause, not many. Nothing is dropped. UNDERRUN_MARGIN_S: scheduled audio this close to running out counts as run out.
export const REBUFFER_MS = 400;
export const UNDERRUN_MARGIN_S = .01;

export class AudioSession {
    // echoGuard: 'adaptive' | 'strict' | 'off' (anything else = adaptive). echoState: the learned echo path of a previous session
    // (see echoSnapshot()), so a recreated session (retarget) keeps it. duck: overrides of DUCK, for tests. onPlaybackState
    // receives {state, sink, paused} whenever the output's state may have changed (paused: it could not be resumed). stuckMs and
    // resumeWaitMs are STUCK_PHRASE_MS and RESUME_WAIT_MS, overridable for tests. sound: {noise_gate_db, end_of_turn_seconds} for this
    // device (setSound changes them live). onMeter receives {level, withheld, voiced, state} per capture message: the raw microphone
    // level before the guard and the gate, whether audible input was withheld (echo or not yet confirmed), the voicing verdict and
    // the guard's acoustic state. Visuals only: a throwing meter never touches capture.
    // onMicrophone receives {kind, message, restart} about the microphone itself: 'warning' (a 16 kHz-or-less or Bluetooth hands-free
    // microphone, said once at start), 'ended' / 'muted' (unplugged, taken by another app or muted by the system: Convo would otherwise
    // keep "listening" to nothing; restart: true offers a restart) and 'unmuted'.
    constructor({ onStart, onTurn, onProbe = () => {}, onError, maximum, deviceId, onLevel = () => {}, echoGuard = 'adaptive', echoState = null, onEchoMetric = () => {}, onPlaybackState = () => {},
                  duck = {}, stuckMs = STUCK_PHRASE_MS, resumeWaitMs = RESUME_WAIT_MS, sound = {}, onMeter = () => {}, onMicrophone = () => {},
                  page = null, processing = null, onOutput = () => {}, muted = false, voiceMuted = false }) {
        this.voiceMuted = !!voiceMuted;                    // the assistant's voice is silent (its replies still appear as text)
        // page: the page's one output (PageAudio); without it this session owns a context of its own (tests, older callers).
        // processing: the microphone's echoCancellation / noiseSuppression / autoGainControl (captureProcessing); default all on.
        // onOutput receives {message} when the chosen output device went away or came back.
        // muted: start with the microphone off (setMuted); a conversation that was muted before a chat switch stays muted.
        this.muted = !!muted;
        Object.assign(this, { onStart, onTurn, onProbe, onError, maximum, deviceId, onLevel, echoState, onEchoMetric, onPlaybackState, stuckMs, resumeWaitMs, onMeter, onMicrophone, page, onOutput });
        this.processing = { echoCancellation: true, noiseSuppression: true, autoGainControl: true, ...(processing || {}) };
        this.underruns = 0; this.rawTap = null;
        this.gateDb = clampGateDb(sound?.noise_gate_db ?? GATE_DEFAULT_DB);
        this.endOfTurn = clampEndOfTurn(sound?.end_of_turn_seconds ?? END_OF_TURN_SECONDS);
        this.echoGuard = ['adaptive', 'strict', 'off'].includes(echoGuard) ? echoGuard : 'adaptive';
        this.duck = new DuckController({ apply: (target, rampMs) => this.rampOutput(target, rampMs), ...duck });
        this.closed = false;
        this.queue = [];
        this.playing = null; this.playingEntry = null; this.watchdog = null;
        this.playbackGeneration = 0;
        this.pcm = new Map(); this.pcmSources = new Set(); this.pcmNext = 0; this.released = new Set();
        // What has been audible, newest last: {phrase_id, start, end} on the context clock (end null while it plays).
        this.timeline = [];
        this.playbackGain = null; this.fades = new Set(); this.fadeUntil = 0; this.outputTarget = 1; this.rampTimer = null;
        this.quietRelease = null; this.unwatch = null; this.switching = false;
    }
    emitLevel(kind, value) { try { this.onLevel(kind, value); } catch {} }
    emitMeter(reading) { try { this.onMeter(reading); } catch {} }
    // This device's noise gate and end of turn, live: the running capture uses them from its next block, no restart.
    setSound({ noise_gate_db, end_of_turn_seconds } = {}) {
        if (noise_gate_db !== undefined) {
            this.gateDb = clampGateDb(noise_gate_db);
            this.gate?.setGate(this.gateDb); this.unguarded?.setGate(this.gateDb);
            if (this.echoGuard !== 'off') this.capture?.port?.postMessage?.({ kind: 'sound', gateDb: this.gateDb });
        }
        if (end_of_turn_seconds !== undefined) { this.endOfTurn = clampEndOfTurn(end_of_turn_seconds); this.boundary?.setEndOfTurn(this.endOfTurn); }
        return { noise_gate_db: this.gateDb, end_of_turn_seconds: this.endOfTurn };
    }
    // A playback failure is said in words, and says that the conversation goes on.
    report(message) { try { this.onError?.(new Error(`${message} ${KEEPS_WORKING}`)); } catch {} }
    async start() {
        try {
            this.stream = await navigator.mediaDevices.getUserMedia({ audio: {
                deviceId: this.deviceId ? { exact: this.deviceId } : undefined,
                channelCount: 1, echoCancellation: !!this.processing.echoCancellation, noiseSuppression: !!this.processing.noiseSuppression,
                autoGainControl: !!this.processing.autoGainControl,
            }});
            if (this.closed) return this.close();
            this.enableTracks();
            if (this.page) { this.context = await this.page.acquire(); this.acquired = true; }
            else this.context = new AudioContext();
            if (this.closed) return this.close();
            // The assistant's voice can be silenced (setVoiceMuted) after everything that listens to the output: the duck, the echo
            // guard's reference and the level meter still see the reply, so what counts as heard stays as it is.
            this.outputGain = this.context.createGain(); this.voiceGain = this.context.createGain();
            this.voiceGain.gain.value = this.voiceMuted ? 0 : 1;
            this.outputGain.connect(this.voiceGain); this.voiceGain.connect(this.context.destination);
            if (!this.page) await this.context.resume();
            this.watchOutput();
            // echo_guard 'off' keeps the previous capture (its own, untouched worklet file); otherwise the guard worklet is used.
            const guarded = this.echoGuard !== 'off';
            const module = guarded ? '/plugins/_convo/webui/audio-worklet-guard.js' : '/plugins/_convo/webui/audio-worklet.js';
            if (this.page) await this.page.addModule(this.context, module); else await this.context.audioWorklet.addModule(module);
            if (this.closed) return this.close();
            // Probes and turns carry how much confirmed voiced speech they hold (voiced_s) and how much of their start is pre-roll
            // (pre_roll_s), read when they are taken. The 16 kHz upload is resampled once per block (TurnEncoder).
            this.encoder = new TurnEncoder(this.context.sampleRate);
            this.boundary = new SpeechBoundary({ rate: this.context.sampleRate, maximum: this.maximum, onStart: this.onStart, endOfTurn: this.endOfTurn,
                allowPreRoll: () => this.echoGuard !== 'off' || !this.playbackNow(),
                onTurn: chunks => this.onTurn(this.encoder.encode(chunks, true), { voiced_s: round3(this.boundary.lastTurn?.voiced || 0), pre_roll_s: round3(this.boundary.lastTurn?.preRolled || 0) }),
                onProbe: chunks => this.onProbe(this.encoder.encode(chunks, false), { voiced_s: round3(this.boundary.speech), pre_roll_s: round3(this.boundary.preRolled) }) });
            this.watchMicrophone();
            this.source = this.context.createMediaStreamSource(this.stream);
            this.sink = this.context.createGain(); this.sink.gain.value = 0;
            if (guarded) {
                // Input 0: the microphone. Input 1: the exact post-duck playback output, the reference the guard compares against.
                this.capture = new AudioWorkletNode(this.context, 'convo-capture-guard', { numberOfInputs: 2, numberOfOutputs: 1,
                    processorOptions: { mode: this.echoGuard, nativeAec: this.nativeAec(), state: this.echoState } });
                if (!this.muted) this.source.connect(this.capture);
                this.outputGain.connect(this.capture, 0, 1);
                this.capture.connect(this.sink).connect(this.context.destination);
                this.gate = new NoiseGate({ rate: this.context.sampleRate, gateDb: this.gateDb });   // after the guard's analysis
                this.capture.port.postMessage({ kind: 'sound', gateDb: this.gateDb });              // the guard's voicing threshold
                this.capture.port.onmessage = event => {
                    if (this.closed) return;
                    const data = event.data;
                    if (data?.kind === 'echo_metric') { this.echoMetric = data; try { this.onEchoMetric(data); } catch {} return; }
                    if (data?.kind === 'echo_state') { this.echoState = data.state; return; }
                    if (this.muted) return;                       // a block on its way when the microphone was muted
                    const level = feedBoundary(this.boundary, data, this.gate);
                    this.emitLevel('input', Math.min(1, level * 8)); // Visuals cannot gate capture.
                    this.emitMeter({ level, withheld: !!data?.withheld, voiced: !!data?.voicedNow, state: String(data?.acousticState || ''), playback: !!data?.playback || this.playbackNow() });
                    this.checkQuiet();
                };
            } else {
                this.capture = new AudioWorkletNode(this.context, 'convo-capture');
                if (!this.muted) this.source.connect(this.capture);
                this.capture.connect(this.sink).connect(this.context.destination);
                this.unguarded = new UnguardedCapture({ rate: this.context.sampleRate, gateDb: this.gateDb });
                this.capture.port.onmessage = event => {
                    if (this.closed || this.muted) return;
                    const { level, voiced } = this.unguarded.feed(this.boundary, event.data);
                    this.emitLevel('input', Math.min(1, level * 8)); // Visuals cannot gate capture.
                    this.emitMeter({ level, withheld: false, voiced, state: 'off', playback: this.playbackNow() });
                    this.checkQuiet();
                };
            }
        } catch (error) { this.close(); throw error; }
    }
    // ---- mute: the microphone track is disabled and the capture is fed nothing, while playback goes on
    enableTracks() { for (const track of this.stream?.getAudioTracks?.() || []) track.enabled = !this.muted; }
    // Muting drops the utterance in progress with its pre-roll (`finish`: an admitted one is completed as a turn first, for a
    // push-to-talk that was let go of). The capture is disconnected rather than fed silence: silence during playback would read as a
    // room with no echo, and the echo guard would then take the assistant's own voice for the user's once the microphone is back.
    // Unmuting starts the capture as a recreated session does: the guard's held frames, voicing and calibration are reset, its learned
    // echo path is kept, and the boundary and its pre-roll are empty, so nothing from before can be sent.
    setMuted(muted, { finish = false } = {}) {
        muted = !!muted;
        if (this.muted === muted || this.closed) return false;
        this.muted = muted;
        this.enableTracks();
        if (muted) {
            if (finish) this.boundary?.finish();
            this.boundary?.reset(); this.encoder?.clear();
            this.stopRawTap();
            try { if (this.capture) this.source?.disconnect?.(this.capture); } catch { /* it was not connected */ }
            this.emitLevel('input', 0);
            this.checkQuiet();                       // a duck waiting for the user's speech to end has nothing left to wait for
            return true;
        }
        if (!this.capture || !this.source) return true;         // still starting: start() connects it
        this.boundary?.reset(); this.encoder?.clear();
        if (this.echoGuard !== 'off') {
            this.capture.port?.postMessage?.({ kind: 'echo_reset' });
            if (this.echoState) this.capture.port?.postMessage?.({ kind: 'echo_restore', state: this.echoState });
            this.gate = new NoiseGate({ rate: this.context.sampleRate, gateDb: this.gateDb });
        } else this.unguarded = new UnguardedCapture({ rate: this.context.sampleRate, gateDb: this.gateDb });
        this.source.connect(this.capture);
        return true;
    }
    // Whether the browser says its own echo cancellation is on for this microphone (read from the live track's settings).
    nativeAec() { return !!this.stream?.getAudioTracks?.()[0]?.getSettings?.().echoCancellation; }
    // The microphone actually in use (the live track's deviceId), '' when the browser does not say. Per-device settings use it.
    inputDeviceId() {
        try { return String(this.stream?.getAudioTracks?.()[0]?.getSettings?.().deviceId || ''); } catch { return ''; }
    }
    inputLabel() { try { return String(this.stream?.getAudioTracks?.()[0]?.label || '').slice(0, 80); } catch { return ''; } }
    // Is the assistant audible now, or was it within its echo tail (OVERLAP_TAIL_MS)? Anything playing, scheduled or queued counts.
    playbackNow() {
        if (this.playing || this.pcmSources?.size || this.pcm?.size || this.queue?.length) return true;
        try { return !!this.overlap(0).overlap; } catch { return false; }
    }
    emitMicrophone(issue) { try { this.onMicrophone(issue); } catch {} }
    // The microphone itself: warn once about one that cannot carry the speech band, and report the track ending or being muted.
    watchMicrophone() {
        const track = this.stream?.getAudioTracks?.()[0];
        if (!track) return;
        const warning = microphoneWarning(track);
        if (warning) this.emitMicrophone({ kind: 'warning', message: warning, restart: false });
        const ended = () => { if (!this.closed) this.emitMicrophone({ kind: 'ended', restart: true,
            message: 'The microphone stopped (unplugged, or taken by another app). Convo is hearing nothing; restart the conversation to listen again.' }); };
        // A mute is said only when it lasts MIC_MUTE_REPORT_MS (a track may be muted for a moment while the device starts).
        let muteTimer = null, reported = false;
        const muted = () => {
            clearTimeout(muteTimer);
            muteTimer = setTimeout(() => { muteTimer = null; if (this.closed || !track.muted) return; reported = true; this.emitMicrophone({ kind: 'muted', restart: true,
                message: 'The microphone was muted by the system or another app. Convo hears silence until it is unmuted; restart if it does not come back.' }); }, MIC_MUTE_REPORT_MS);
        };
        const unmuted = () => { clearTimeout(muteTimer); muteTimer = null; if (reported && !this.closed) { reported = false; this.emitMicrophone({ kind: 'unmuted', restart: false, message: '' }); } };
        track.addEventListener?.('ended', ended); track.addEventListener?.('mute', muted); track.addEventListener?.('unmute', unmuted);
        this.unwatchMicrophone = () => { clearTimeout(muteTimer); track.removeEventListener?.('ended', ended); track.removeEventListener?.('mute', muted); track.removeEventListener?.('unmute', unmuted); };
        if (track.readyState === 'ended') ended(); else if (track.muted) muted();
    }
    // Developer "Record raw and sent", only on the user's press: a copy of the capture's first input (the same microphone source node,
    // so no second microphone), before echo protection and the noise gate, kept from the press until it is taken (at most
    // RAW_MAX_SECONDS, the newest kept). It is a separate silent branch through the untouched legacy capture worklet; the
    // conversation's own capture is not changed.
    async startRawTap() {
        if (this.rawTap || !this.context || !this.source || this.closed || this.muted) return false;
        const module = '/plugins/_convo/webui/audio-worklet.js';
        if (this.page) await this.page.addModule(this.context, module); else await this.context.audioWorklet.addModule(module);
        if (this.closed || this.rawTap || !this.source || this.muted) return false;
        const node = new AudioWorkletNode(this.context, 'convo-capture'), sink = this.context.createGain();
        sink.gain.value = 0;
        const tap = { node, sink, chunks: [], samples: 0, rate: this.context.sampleRate };
        node.port.onmessage = event => {
            if (this.rawTap !== tap || !(event.data instanceof Float32Array)) return;
            tap.chunks.push(event.data); tap.samples += event.data.length;
            while (tap.chunks.length > 1 && tap.samples - tap.chunks[0].length >= RAW_MAX_SECONDS * tap.rate) tap.samples -= tap.chunks.shift().length;
        };
        this.source.connect(node); node.connect(sink).connect(this.context.destination);
        this.rawTap = tap;
        return true;
    }
    // The raw audio so far ({rate, chunks, audio: a 16-bit WAV at the device's rate}, or null), and the tap is closed.
    takeRawTap() {
        const tap = this.rawTap; this.stopRawTap();
        return tap ? { rate: tap.rate, chunks: tap.chunks, audio: tap.samples ? wavAtRate(tap.chunks, tap.rate) : '' } : null;
    }
    stopRawTap() {
        const tap = this.rawTap; this.rawTap = null;
        if (!tap) return;
        tap.node.port.onmessage = null;
        try { this.source?.disconnect?.(tap.node); } catch {}
        try { tap.node.disconnect(); tap.sink.disconnect(); } catch {}
    }
    // The learned echo path to hand to the next AudioSession (retarget recreates the session; the room has not changed).
    echoSnapshot() { return this.echoState; }
    // Mode change between adaptive and strict while running ('off' needs a restart: it is a different capture path).
    setEchoGuard(mode) {
        if (!['adaptive', 'strict'].includes(mode) || this.echoGuard === 'off') return false;
        this.echoGuard = mode; this.capture?.port.postMessage({ kind: 'echo_guard', mode, nativeAec: this.nativeAec() }); return true;
    }

    // ---- the output can never go silent unnoticed
    // Follow the system default output device, resume a context the browser paused (a background window, another app taking the
    // device, Safari's 'interrupted'), and say so when it cannot be resumed until the user clicks.
    watchOutput() {
        const doc = globalThis.document, devices = globalThis.navigator?.mediaDevices;
        const wake = () => { if (!doc?.hidden) void this.ensureRunning(); };
        const gesture = () => { if (this.context && this.context.state !== 'running') void this.ensureRunning(); };
        const changed = () => void this.followDefaultOutput();
        doc?.addEventListener?.('visibilitychange', wake);
        globalThis.addEventListener?.('focus', wake);
        doc?.addEventListener?.('pointerdown', gesture, true);
        doc?.addEventListener?.('keydown', gesture, true);
        devices?.addEventListener?.('devicechange', changed);
        const context = this.context;
        const onstate = () => {
            if (this.switching || this.closed) return;
            if (context.state === 'suspended' || context.state === 'interrupted') void this.ensureRunning(); else this.reportState();
        };
        context.onstatechange = onstate;
        this.unwatch = () => {
            doc?.removeEventListener?.('visibilitychange', wake);
            globalThis.removeEventListener?.('focus', wake);
            doc?.removeEventListener?.('pointerdown', gesture, true);
            doc?.removeEventListener?.('keydown', gesture, true);
            devices?.removeEventListener?.('devicechange', changed);
            if (context.onstatechange === onstate) context.onstatechange = null;   // the page's context outlives this session
        };
    }
    outputInfo() {
        const context = this.context;
        return context ? { state: String(context.state || ''), sink: sinkLabel(context), latency: round3(outputLatency(context)) } : { state: '', sink: '', latency: 0 };
    }
    reportState() {
        if (!this.context || this.closed) return;
        const info = this.outputInfo();
        try { this.onPlaybackState({ ...info, paused: info.state === 'suspended' || info.state === 'interrupted' }); } catch {}
    }
    // Resume a paused context. A browser may hold the promise until a user gesture, so this waits only briefly and then reports
    // whether it runs; the next click or key press (watchOutput) tries again.
    async ensureRunning() {
        const context = this.context;
        if (!context || this.closed || context.state === 'closed') return false;
        if (context.state !== 'running') {
            try { await Promise.race([Promise.resolve(context.resume()).catch(() => {}), wait(this.resumeWaitMs)]); } catch {}
            if (this.closed) return false;
        }
        this.reportState();
        return context.state === 'running';
    }
    // The devices changed (a headset plugged in, a VR headset became the default, the chosen device went away): with the page's output
    // the chosen device is followed (the default while it is gone, said once); otherwise setSinkId('') plays to the new default. A
    // browser that cannot choose is left playing where it plays: it is never suspended and resumed to "reopen" the device, which on
    // Android flips the phone between its call and media streams. The state is reported instead.
    async followDefaultOutput() {
        const context = this.context;
        if (!context || this.closed || context.state === 'closed') return false;
        try {
            if (this.page) {
                const result = await this.page.follow();
                if (result?.message && !this.closed) { try { this.onOutput({ message: result.message, missing: !!result.missing }); } catch {} }
            } else if (typeof context.setSinkId === 'function') await context.setSinkId('');
        } catch { /* the device may have gone with the change; the state report below says where things stand */ }
        if (this.closed) return false;
        this.reportState();
        if (context.state !== 'running') return this.ensureRunning();
        return true;
    }
    setVoiceMuted(value) { this.voiceMuted = !!value; if (this.voiceGain) this.voiceGain.gain.value = this.voiceMuted ? 0 : 1; return this.voiceMuted; }
    // A short soft tone through the conversation's own output (outputGain: the duck and the echo guard's reference see it too).
    // The click that asks for it is a user gesture, so a paused context is resumed first.
    async testTone() {
        const context = this.context;
        if (!context || this.closed) return { ok: false, state: '', sink: '', message: 'No voice session is running.' };
        await this.ensureRunning();
        const info = this.outputInfo();
        if (context.state !== 'running') return { ok: false, ...info, message: `The audio output is ${info.state || 'not running'}; click again to resume it.` };
        playTone(context, this.outputGain || context.destination, this.levelTimer ? null : value => this.emitLevel('output', value));
        return { ok: true, path: 'conversation', ...info };
    }
    // Without a voice session: the same tone through a temporary context, closed afterwards.
    static async testTone() {
        const Context = globalThis.AudioContext || globalThis.webkitAudioContext;
        if (!Context) return { ok: false, state: '', sink: '', message: 'This browser has no Web Audio output.' };
        const context = new Context();
        try { await Promise.race([Promise.resolve(context.resume()).catch(() => {}), wait(RESUME_WAIT_MS)]); } catch {}
        const info = { state: String(context.state || ''), sink: sinkLabel(context), latency: round3(outputLatency(context)) };
        if (context.state !== 'running') { void context.close?.(); return { ok: false, ...info, message: `The audio output is ${info.state || 'not running'}; click again to try.` }; }
        const seconds = playTone(context, context.destination);
        setTimeout(() => { void context.close?.(); }, seconds * 1000 + 250);
        return { ok: true, path: 'temporary', ...info };
    }

    // ---- talking over the assistant
    // Was assistant audio heard at any moment from `ago` seconds before now until now (or did it stop at most OVERLAP_TAIL_MS before
    // that moment), and which phrase was being heard then, and how much of it had played? Times are what the user hears: the
    // context renders `outputLatency` ahead of that.
    overlap(ago = 0) {
        const context = this.context, none = { overlap: false, heard: null };
        if (!context || this.closed || context.state !== 'running' || !Number.isFinite(context.currentTime)) return none;
        const heardNow = context.currentTime - outputLatency(context), began = heardNow - Math.max(0, Number(ago) || 0), tail = OVERLAP_TAIL_MS / 1000;
        const spans = this.timeline.filter(entry => (entry.end ?? Infinity) > entry.start && entry.start <= heardNow);
        if (!spans.some(entry => (entry.end ?? Infinity) + tail >= began)) return none;
        const at = spans.filter(entry => entry.start <= began && (entry.end ?? Infinity) + tail >= began).pop();
        return { overlap: true, heard: at ? { phrase_id: at.phrase_id, played_s: round3(Math.max(0, Math.min(began, at.end ?? Infinity) - at.start)) } : null };
    }
    // At a candidate's onset. The echo guard holds speech back during playback until it is confirmed and then releases the whole
    // onset at once, so the user really began the open utterance's length before now (the boundary's spokenFor).
    overlapAtOnset() { return this.overlap(this.boundary?.spokenFor?.() ?? 0); }
    // Probe at once, then every OVERLAP_PROBE_SECONDS while the user keeps talking.
    quicken() { this.boundary?.quicken?.(OVERLAP_PROBE_SECONDS); }
    remember(phrase_id, start) {
        const entry = { phrase_id: String(phrase_id || ''), start, end: null };
        this.timeline.push(entry);
        if (this.timeline.length > 16) this.timeline.shift();
        return entry;
    }
    heardUntil(entry, end = this.context?.currentTime) { if (entry && entry.end === null) entry.end = Number.isFinite(end) ? end : entry.start; }

    interrupt(fadeMs = 0) {
        this.playbackGeneration++;
        const context = this.context, sources = [...this.pcmSources];
        const playing = this.playing; this.playing = null;
        clearTimeout(this.watchdog); this.watchdog = null;
        if (playing) sources.push(playing);
        const fade = Math.min(MAX_FADE_MS, Math.max(0, Number(fadeMs) || 0));
        const branch = this.playbackGain;
        const fading = fade > 0 && sources.length > 0 && context?.state === 'running' && typeof branch?.gain?.linearRampToValueAtTime === 'function';
        const until = fading ? context.currentTime + fade / 1000 : context?.currentTime;
        for (const phrase of this.pcm.values()) { clearTimeout(phrase.timer); this.heardUntil(phrase.entry, until); }
        this.heardUntil(this.playingEntry, until); this.playingEntry = null;
        this.pcm.clear(); this.pcmNext = 0; this.pcmSources.clear();
        this.queue = [];
        if (fading) {
            // Fade the old audio to silence on its own branch, then stop it there; the next phrase gets a fresh branch at full level.
            const gain = branch.gain, start = context.currentTime;
            gain.cancelScheduledValues?.(start); gain.setValueAtTime?.(gain.value, start); gain.linearRampToValueAtTime(0, until);
            for (const source of sources) { source.onended = null; try { source.stop(until); } catch {} }
            const record = { sources, branch, meter: this.outputMeter, timer: null };
            record.timer = setTimeout(() => this.finishFade(record), fade + 30);
            this.fades.add(record);
            this.playbackGain = null; this.outputMeter = null;
            this.fadeUntil = clockNow() + fade;
        } else for (const source of sources) { source.onended = null; try { source.stop(); } catch {} source.disconnect(); }
        this.stopMeter();
    }
    finishFade(record) {
        clearTimeout(record.timer); this.fades.delete(record);
        for (const source of record.sources) { try { source.disconnect(); } catch {} }
        record.meter?.disconnect?.(); record.branch?.disconnect?.();
    }
    stopMeter() {
        if (this.levelTimer) clearInterval(this.levelTimer);
        this.levelTimer = null; this.outputMeter?.disconnect(); this.outputMeter = null;
        this.emitLevel('output', 0);
    }
    // Where playback goes: its own gain per reply, behind the duck (outputGain), so a faded stop ramps only the old audio down.
    branch() {
        if (!this.playbackGain && this.context?.createGain) {
            this.playbackGain = this.context.createGain();
            this.playbackGain.connect(this.outputGain || this.context.destination);
        }
        return this.playbackGain || this.outputGain || this.context.destination;
    }
    setPolicyHold(held, immediate = false) {
        // Uncertainty ducks the existing output without cancelling its generation,
        // clearing playback, or pretending an interrupted phrase was delivered. The duck ramps and has hysteresis (DuckController);
        // `immediate` is for a decision that has already ended (a new reply must never be born ducked).
        this.quietRelease = null;              // a newer decision replaces a pending `continue` release
        this.duck.request(held, immediate);
    }
    // `continue`: the user's speech over the reply is a backchannel or a side remark. Playback goes on, and the duck lifts `ms`
    // after the user's speech ends instead of the usual delay; a new request to duck cancels that. False when nothing is ducked.
    releaseAfterSpeech(ms = CONTINUE_RELEASE_MS) {
        if (!this.duck.ducked) return false;
        this.duck.cancel(this.duck.releaseTimer); this.duck.releaseTimer = null;
        this.quietRelease = ms; this.checkQuiet();
        return true;
    }
    checkQuiet() {
        if (this.quietRelease === null) return;
        const boundary = this.boundary;
        const quietMs = boundary?.chunks?.length ? boundary.silence * 1000 : Infinity;   // no utterance open: the speech has ended
        if (quietMs < this.quietRelease) return;
        this.quietRelease = null;
        this.duck.release();
    }
    rampOutput(target, rampMs) {
        const gain = this.outputGain?.gain;
        if (!gain) return;
        // A faded stop is still ramping the old audio down: raising the output now would swell it, so the rise waits for the fade.
        clearTimeout(this.rampTimer); this.rampTimer = null;
        const pending = this.fadeUntil - clockNow();
        if (pending > 0 && target > this.outputTarget) { this.rampTimer = setTimeout(() => { this.rampTimer = null; this.rampOutput(target, rampMs); }, pending); return; }
        this.outputTarget = target;
        // setTargetAtTime continues from the value the previous ramp has reached, so quick reversals do not jump.
        if (typeof gain.setTargetAtTime === 'function' && this.context) gain.setTargetAtTime(target, this.context.currentTime, rampMs / 3000);
        else gain.value = target;
    }
    // A failure stops playback as interrupt() does and is said once: the rest of the failed phrase (and of any phrase it stopped) is
    // ignored when it arrives, so one bad phrase cannot stop the next one again or repeat the error.
    pcmFail(message, event = null) {
        for (const id of [event?.phrase_id, ...this.pcm.keys()]) this.forgetPhrase(id);
        this.interrupt(); this.report(message); return false;
    }
    forgetPhrase(id) {
        if (typeof id !== 'string' || !id) return;
        this.released.add(id);
        while (this.released.size > 32) this.released.delete(this.released.values().next().value);
    }
    // Phrases finished on paper that can no longer complete: their audio should have ended a while ago with the context running, or
    // their start never came. Released as interrupted, so a stale finished phrase never refuses the next one.
    pcmSweep() {
        const context = this.context;
        if (!context || context.state !== 'running') return;
        for (const phrase of [...this.pcm.values()]) {
            if (!phrase.ended) continue;
            if (phrase.didStart && context.currentTime > phrase.until + this.stuckMs / 1000) this.pcmRelease(phrase, 'A reply phrase stopped advancing before its end, so it was dropped.');
            else if (!phrase.didStart && phrase.sequence > 0 && phrase.budget <= 0) this.pcmRelease(phrase, 'A reply phrase never started playing (the audio output did not advance), so it was skipped.');
        }
    }
    // One phrase released as interrupted: its sources stop, nothing is acknowledged, and its later events are ignored.
    pcmRelease(phrase, message) {
        clearTimeout(phrase.timer);
        for (const source of phrase.sources) { source.onended = null; try { source.stop(); } catch {} source.disconnect(); this.pcmSources.delete(source); }
        phrase.sources.clear();
        this.heardUntil(phrase.entry);
        this.pcm.delete(phrase.phrase_id);
        this.forgetPhrase(phrase.phrase_id);
        if (!this.pcm.size) { this.pcmNext = 0; this.stopMeter(); void this.drain(); }
        this.report(message);
    }
    // `event.prime_ms` (0-2000, from the server's start-up buffer rule): a phrase that begins while nothing else is playing waits until
    // that much of its audio has arrived (or its end) before it starts, so a streamed reply does not start thin and then stall.
    // A phrase queued behind audio that is still playing starts as before; absent or 0 = no wait.
    pcmBegin(event, completed, started = () => {}) {
        if (this.closed) return false;
        this.pcmSweep();
        if (!this.context || this.playing || this.decoding || this.queue.length || this.pcm.size >= 24 ||
            !['session_id','turn_id','phrase_id'].every(key => typeof event[key] === 'string' && event[key].length > 0 && event[key].length <= 256) ||
            !Number.isSafeInteger(event.epoch) || event.epoch < 0 || this.pcm.has(event.phrase_id) ||
            event.format !== 'pcm_s16le' || event.channels !== 1 ||
            ![8000,16000,22050,24000,32000,44100,48000].includes(event.sample_rate) ||
            [...this.pcm.values()].some(phrase => !phrase.ended)) return this.pcmFail('Speech playback refused a reply phrase that overlapped another or had an audio format it cannot play.', event);
        this.released.delete(event.phrase_id);
        const prime = Number(event.prime_ms);
        const idle = !this.pcmSources.size && !this.playing && !(this.pcmNext > this.context.currentTime);
        this.pcm.set(event.phrase_id, { ...event, completed, started, sequence: 0, scheduled: 0, bytes: 0, pending: 0, sources: new Set(), entry: null, until: 0,
            budget: Infinity, checked: 0, ended: false, didStart: false, generation: this.playbackGeneration, timer: null,
            primeMs: idle && Number.isFinite(prime) ? Math.max(0, Math.min(MAX_PRIME_MS, prime)) : 0, held: [], heldSeconds: 0 });
        return true;
    }
    pcmPhrase(event) {
        const phrase = this.pcm.get(event.phrase_id);
        return phrase && ['session_id','epoch','turn_id'].every(key => phrase[key] === event[key]) ? phrase : null;
    }
    pcmChunk(event) {
        if (this.closed) return false;
        if (!this.pcm.has(event.phrase_id) && this.released.has(event.phrase_id)) return false;   // a released phrase: its rest is ignored
        const phrase = this.pcmPhrase(event);
        if (!phrase || phrase.ended || !Number.isSafeInteger(event.sequence) || event.sequence !== phrase.sequence ||
            typeof event.audio !== 'string' || !event.audio.length || event.audio.length > 43692 ||
            event.audio.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(event.audio)) return this.pcmFail('Speech playback lost part of a reply phrase (a missing or out-of-order chunk), so playback was stopped.', event);
        try {
            const bytes = Uint8Array.from(atob(event.audio), c => c.charCodeAt(0));
            if (!bytes.length || bytes.length % 2 || bytes.length > 32768) return this.pcmFail('Speech playback received a malformed audio chunk, so playback was stopped.', event);
            if (phrase.bytes + bytes.length > 16 * 1024 * 1024 || this.pcmSources.size + phrase.held.length >= PCM_MAX_SOURCES)
                return this.pcmFail('Speech playback had more audio queued than it can hold, so the queued audio was dropped.', event);
            const buffer = this.context.createBuffer(1, bytes.length / 2, phrase.sample_rate);
            const values = buffer.getChannelData(0), view = new DataView(bytes.buffer);
            for (let i = 0; i < values.length; i++) values[i] = view.getInt16(i * 2, true) / 32768;
            phrase.sequence++; phrase.bytes += bytes.length;
            if (phrase.primeMs > 0) {                                      // start-up buffer: hold until enough has arrived
                phrase.held.push(buffer); phrase.heldSeconds += bytes.length / 2 / phrase.sample_rate;
                return phrase.heldSeconds * 1000 >= phrase.primeMs ? this.pcmFlush(phrase, event) : true;
            }
            // Jitter buffer: the phrase already plays and everything scheduled has (nearly) run out, so this chunk came late. Hold it and
            // what follows until REBUFFER_MS is there (or the phrase ends), then play it all back to back: one pause, never a stutter.
            if (phrase.scheduled > 0 && this.pcmNext < this.context.currentTime + UNDERRUN_MARGIN_S) {
                this.underruns++; phrase.rebuffers = (phrase.rebuffers || 0) + 1;
                phrase.primeMs = REBUFFER_MS; phrase.held = [buffer]; phrase.heldSeconds = bytes.length / 2 / phrase.sample_rate;
                return phrase.heldSeconds * 1000 >= phrase.primeMs ? this.pcmFlush(phrase, event) : true;
            }
            return this.pcmSchedule(phrase, buffer, event);
        } catch (error) { return this.pcmFail(`Speech playback could not use a reply phrase's audio (${error?.message || 'invalid PCM audio'}), so playback was stopped.`, event); }
    }
    // The start-up buffer is full (or the phrase ended): everything held starts now, in order, and the rest streams as usual.
    pcmFlush(phrase, event) {
        const held = phrase.held; phrase.held = []; phrase.primeMs = 0;
        for (const buffer of held) if (!this.pcmSchedule(phrase, buffer, event)) return false;
        return true;
    }
    pcmSchedule(phrase, buffer, event) {
        try {
            const when = Math.max(this.context.currentTime + .02, this.pcmNext);
            const duration = buffer.getChannelData(0).length / phrase.sample_rate;
            if (when + duration - this.context.currentTime > PCM_AHEAD_SECONDS) return this.pcmFail(`Speech playback fell more than ${PCM_AHEAD_SECONDS} seconds behind, so the queued audio was dropped.`, event);
            const source = this.context.createBufferSource(); source.buffer = buffer;
            if (!this.outputMeter && this.context.createAnalyser) {
                this.outputMeter = this.context.createAnalyser(); this.outputMeter.fftSize = 256;
                this.outputMeter.connect(this.branch());
                const levels = new Float32Array(256);
                this.levelTimer = setInterval(() => {
                    this.outputMeter?.getFloatTimeDomainData(levels);
                    this.emitLevel('output', Math.min(1, Math.sqrt(levels.reduce((n,v) => n + v*v, 0) / levels.length) * 6));
                }, 50);
            }
            source.connect(this.outputMeter || this.branch());
            // Started once the context plays the first chunk. Waiting counts only time the context was running: a phrase still not
            // started STUCK_PHRASE_MS after its first chunk was due is released, so it can never block the phrases after it.
            const markStarted = () => {
                if (this.closed || phrase.generation !== this.playbackGeneration || this.pcm.get(phrase.phrase_id) !== phrase || phrase.didStart) return;
                const running = this.context.state === 'running', at = clockNow();
                if (running) phrase.budget -= at - phrase.checked;
                phrase.checked = at;
                if (!running || this.context.currentTime < phrase.firstAt) {
                    if (running && phrase.budget <= 0) { this.pcmRelease(phrase, 'A reply phrase never started playing (the audio output did not advance), so it was skipped.'); return; }
                    phrase.timer = setTimeout(markStarted, 20); return;
                }
                phrase.didStart = true; phrase.started();
            };
            if (phrase.scheduled === 0) {
                phrase.firstAt = when; phrase.entry = this.remember(phrase.phrase_id, when);
                const due = Math.max(0, (when - this.context.currentTime) * 1000);
                phrase.budget = due + this.stuckMs; phrase.checked = clockNow();
                phrase.timer = setTimeout(markStarted, due);
            }
            phrase.scheduled++; phrase.pending++; phrase.until = when + duration;
            this.pcmSources.add(source); phrase.sources.add(source); this.pcmNext = when + duration;
            source.onended = () => {
                source.disconnect(); this.pcmSources.delete(source); phrase.sources.delete(source);
                if (phrase.generation !== this.playbackGeneration || this.closed) return;
                markStarted(); phrase.pending--; this.pcmComplete(phrase);
            };
            source.start(when);
            return true;
        } catch (error) { return this.pcmFail(`Speech playback could not use a reply phrase's audio (${error?.message || 'invalid PCM audio'}), so playback was stopped.`, event); }
    }
    pcmEnd(event) {
        if (this.closed) return false;
        if (!this.pcm.has(event.phrase_id) && this.released.has(event.phrase_id)) return false;
        const phrase = this.pcmPhrase(event);
        if (!phrase || phrase.ended || !Number.isSafeInteger(event.chunks) || event.chunks !== phrase.sequence || !phrase.bytes)
            return this.pcmFail('A reply phrase ended before all of its audio arrived, so playback was stopped.', event);
        if (phrase.held.length && !this.pcmFlush(phrase, event)) return false;    // a phrase shorter than its start-up buffer plays now
        phrase.primeMs = 0;
        phrase.ended = true; this.pcmComplete(phrase); return true;
    }
    pcmAbort(event) {
        if (this.pcmPhrase(event)) this.interrupt();
    }
    pcmComplete(phrase) {
        if (!phrase.ended || phrase.pending || !phrase.didStart || this.closed || phrase.generation !== this.playbackGeneration || this.pcm.get(phrase.phrase_id) !== phrase) return;
        clearTimeout(phrase.timer); this.pcm.delete(phrase.phrase_id); this.heardUntil(phrase.entry); phrase.completed();
        if (!this.pcm.size) { this.stopMeter(); void this.drain(); }
    }
    enqueue(base64, completed, started = () => {}, phraseId = '') {
        if (this.closed) return;
        if (this.queue.length >= 24) { this.interrupt(); this.report('Speech playback fell behind, so the queued phrases were dropped.'); return; }
        this.queue.push({ base64, completed, started, generation: this.playbackGeneration, phrase_id: phraseId });
        void this.drain();
    }
    async drain() {
        if (this.playing || this.decoding || this.pcm.size || this.closed || !this.queue.length) return;
        this.decoding = true;
        const item = this.queue.shift();
        try {
            const bytes = Uint8Array.from(atob(item.base64), c => c.charCodeAt(0));
            const buffer = await this.context.decodeAudioData(bytes.buffer);
            if (this.closed || item.generation !== this.playbackGeneration) return;
            const source = this.context.createBufferSource(); source.buffer = buffer;
            // Analyser observes the existing output stream, never another mic.
            if (this.context.createAnalyser) {
                this.outputMeter = this.context.createAnalyser(); this.outputMeter.fftSize = 256;
                source.connect(this.outputMeter).connect(this.branch());
                const values = new Float32Array(256);
                this.levelTimer = setInterval(() => {
                    this.outputMeter?.getFloatTimeDomainData(values);
                    const rms = Math.sqrt(values.reduce((sum, value) => sum + value * value, 0) / values.length);
                    this.emitLevel('output', Math.min(1, rms * 6));
                }, 50);
            } else source.connect(this.branch());
            this.playing = source;
            source.onended = () => {
                clearTimeout(this.watchdog); this.watchdog = null;
                source.disconnect();
                this.stopMeter();
                if (this.playing === source) { this.playing = null; this.heardUntil(this.playingEntry); this.playingEntry = null; }
                if (!this.closed && item.generation === this.playbackGeneration) item.completed();
                void this.drain();
            };
            source.start();
            this.playingEntry = this.remember(item.phrase_id, this.context.currentTime);
            this.watchPlaying(source, buffer.duration);
            item.started();
        } catch (error) { this.interrupt(); this.report(`A reply phrase could not be decoded (${error?.message || 'unknown error'}), so it was skipped.`); }
        finally { this.decoding = false; if (!this.playing && !this.closed) void this.drain(); }
    }
    // A buffered phrase still "playing" STUCK_PHRASE_MS past its length, counting only time the context was running, is released.
    watchPlaying(source, seconds) {
        clearTimeout(this.watchdog);
        let budget = (Number(seconds) || 0) * 1000 + this.stuckMs, last = clockNow();
        const check = () => {
            this.watchdog = null;
            if (this.closed || this.playing !== source) return;
            const at = clockNow();
            if (this.context.state === 'running') budget -= at - last;
            last = at;
            if (budget > 0) { this.watchdog = setTimeout(check, Math.min(1000, budget)); return; }
            source.onended = null; try { source.stop(); } catch {} source.disconnect();
            this.playing = null; this.heardUntil(this.playingEntry); this.playingEntry = null; this.stopMeter();
            this.report('A reply phrase stopped advancing before its end (the audio output did not advance), so it was skipped.');
            void this.drain();
        };
        this.watchdog = setTimeout(check, Math.min(1000, budget));
    }
    close() {
        this.closed = true; this.duck.dispose(); this.interrupt();
        clearTimeout(this.rampTimer); this.rampTimer = null;
        for (const record of [...this.fades]) this.finishFade(record);
        this.unwatch?.(); this.unwatch = null;
        this.unwatchMicrophone?.(); this.unwatchMicrophone = null;
        this.emitLevel('input', 0);
        this.stream?.getTracks().forEach(track => track.stop());
        this.stopRawTap();
        if (this.capture?.port) this.capture.port.onmessage = null;
        this.source?.disconnect(); this.capture?.disconnect(); this.sink?.disconnect();
        this.playbackGain?.disconnect(); this.outputGain?.disconnect();
        // The page's one context is kept (only suspended once no microphone is open); a context of its own is closed.
        if (this.page) { if (this.acquired) { this.acquired = false; this.page.release(); } }
        else if (this.context && this.context.state !== 'closed') void this.context.close();
    }
}
// The page's one output, reachable through the class too (the store reaches audio.js only through AudioSession).
AudioSession.page = sharedPageAudio;
