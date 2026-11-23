// Voicing: is this 20 ms of microphone audio a human voice? Pure (no DOM, no browser globals), cheap enough for a phone's
// audio thread, and shared by the echo-guard worklet (echo-guard.js), the unguarded capture path (audio.js) and the tests.
//
// Per 20 ms frame at the guard's analysis rate (16 kHz):
//  - RMS level;
//  - periodicity: the normalized autocorrelation peak over pitch lags 70-400 Hz (computed on a 2:1 decimated, DC-blocked copy, so
//    one frame costs about 18k multiply-adds). The search starts only after the autocorrelation has first fallen below
//    MAIN_LOBE_EXIT, so slowly varying sound (pink/brown noise, rumble) cannot claim its own main lobe as a "period";
//  - an adaptive noise floor in dB: it falls quickly to a quieter frame and rises slowly (time constant FLOOR_RISE_SECONDS) toward
//    a louder one, so a steady sound (hum, a fan, PC noise) is learned into the floor within about one to two seconds while speech,
//    whose level keeps dipping between syllables, stays above it.
// A frame is raw-voiced when its level reaches the noise gate threshold AND the floor + FLOOR_MARGIN_DB AND its periodicity reaches
// PERIODICITY_MIN. Voicing begins after ONSET_FRAMES raw-voiced frames in a row whose newest level is within ONSET_DECAY_DB of the
// run's loudest (a clap or a key click is periodic for a moment but decays at once), and then lasts HANGOVER_MS past the last
// raw-voiced frame, so unvoiced consonants and small gaps inside a word do not break it. An onset run must also average
// ONSET_PERIODICITY (narrow-band noise such as a fan's hum band reaches .5 now and then, a voice sits near .9).
//
// Warm-up, honestly: the floor starts at FLOOR_START_DB and rises with FLOOR_RISE_SECONDS. A periodic sound that holds its pitch and
// level (hum, a motor) is caught by the steady rule within STEADY_FRAMES and counts at most that long (plus the onset); one whose
// pitch wanders would count until the floor has climbed to within FLOOR_MARGIN_DB of it (about one to two seconds). Broadband noise
// is never periodic enough to be voiced, warm or not. A sustained, perfectly level sung note is the price: it stops counting too.

export const VOICING_RATE = 16000;
export const FRAME_SAMPLES = 320;                   // 20 ms at 16 kHz
export const FRAME_SECONDS = FRAME_SAMPLES / VOICING_RATE;
export const MIN_PITCH_HZ = 70;
export const MAX_PITCH_HZ = 400;
export const PERIODICITY_MIN = 0.5;
export const FLOOR_MARGIN_DB = 9;
export const HANGOVER_MS = 120;
export const ONSET_FRAMES = 3;
export const ONSET_DECAY_DB = 9;
export const ONSET_PERIODICITY = 0.7;               // an onset run must be clearly periodic on average; voicing then continues at PERIODICITY_MIN
export const FLOOR_START_DB = -80;
export const FLOOR_RISE_SECONDS = 1.0;
export const FLOOR_FALL_SECONDS = 0.04;
// A periodic sound whose pitch (within one decimated lag) and level (within STEADY_DB) hold for STEADY_FRAMES is a machine (mains
// hum, a motor, a beep), not a voice: it stops counting at once and the floor jumps to it. A voice's pitch and level keep moving.
// 0.5 s, not 0.16 s: with 8 frames a level stretch of a monotone voice lifted the floor mid-word ("Tell me a joke." counted 0.28 s and
// was never sent, live 2026-11-09); hum, fans and keys are still rejected (corpus check: 0 of 12 noise clips counted as speech).
export const STEADY_FRAMES = 25;
export const STEADY_DB = 1.5;
const MAIN_LOBE_EXIT = 0.3;
const SILENT_DB = -100;

// The noise gate's dB axis (the fork's design): the leftmost position is off.
export const GATE_OFF_DB = -66;
export const GATE_MAX_DB = -3;
export const GATE_DEFAULT_DB = -50;
export const GATE_ATTACK_MS = 5;
export const GATE_HOLD_MS = 250;
export const GATE_RELEASE_MS = 80;

export const dbOf = rms => (rms > 1e-10 ? Math.max(SILENT_DB, 20 * Math.log10(rms)) : SILENT_DB);
export const clampGateDb = db => {
    const value = Number(db);
    return Number.isFinite(value) ? Math.max(GATE_OFF_DB, Math.min(GATE_MAX_DB, value)) : GATE_DEFAULT_DB;
};
// Linear RMS threshold for a gate setting; 0 when the gate is off.
export const gateThreshold = db => { const value = clampGateDb(db); return value > GATE_OFF_DB ? 10 ** (value / 20) : 0; };

const DECIMATED = FRAME_SAMPLES / 2;                                  // 160 samples at 8 kHz
const MIN_LAG = Math.round(VOICING_RATE / 2 / MAX_PITCH_HZ);           // 20
const MAX_LAG = Math.round(VOICING_RATE / 2 / MIN_PITCH_HZ);           // 114
const DC_POLE = Math.exp(-2 * Math.PI * 60 / VOICING_RATE);            // first-order high-pass at 60 Hz

export class Voicing {
    constructor({ gateDb = GATE_DEFAULT_DB } = {}) {
        this._history = new Float32Array(DECIMATED + MAX_LAG);
        this._buffer = new Float32Array(FRAME_SAMPLES);
        this._fill = 0;
        this.setGate(gateDb);
        this.reset();
    }
    setGate(db) { this.gateDb = clampGateDb(db); this._gate = gateThreshold(this.gateDb); }
    reset() {
        this._history.fill(0); this._fill = 0;
        this._hpIn = 0; this._hpOut = 0;
        this.floorDb = FLOOR_START_DB;
        this._run = 0; this._runPeakDb = SILENT_DB; this._runPeriodicity = 0; this._hang = 0; this.voiced = false;
        this._lag = 0; this._prevLag = -9; this._prevDb = SILENT_DB; this._steady = 0;
        this.frames = 0;
        this.last = null;
    }
    get floor() { return 10 ** (this.floorDb / 20); }

    // Any number of 16 kHz samples; one result per completed 20 ms frame (oldest first).
    push(samples) {
        const results = [];
        let offset = 0;
        while (offset < samples.length) {
            const take = Math.min(FRAME_SAMPLES - this._fill, samples.length - offset);
            this._buffer.set(samples.subarray(offset, offset + take), this._fill);
            this._fill += take; offset += take;
            if (this._fill === FRAME_SAMPLES) { results.push(this.analyze(this._buffer)); this._fill = 0; }
        }
        return results;
    }

    // Exactly one 20 ms frame (FRAME_SAMPLES at 16 kHz). Returns {rms, db, periodicity, floorDb, raw, voiced}.
    analyze(frame) {
        let energy = 0;
        for (let i = 0; i < FRAME_SAMPLES; i++) energy += frame[i] * frame[i];
        const rms = Math.sqrt(energy / FRAME_SAMPLES), db = dbOf(rms);
        const periodicity = this._periodicity(frame);
        const steadyNow = periodicity >= PERIODICITY_MIN && Math.abs(this._lag - this._prevLag) <= 1 && Math.abs(db - this._prevDb) <= STEADY_DB;
        this._steady = steadyNow ? this._steady + 1 : 0;
        this._prevLag = this._lag; this._prevDb = db;
        const steady = this._steady >= STEADY_FRAMES;
        if (steady) { this.floorDb = Math.max(this.floorDb, db - 3); this.voiced = false; this._hang = 0; }
        const floorDb = this.floorDb;
        const raw = !steady && rms > 0 && rms >= this._gate && db >= floorDb + FLOOR_MARGIN_DB && periodicity >= PERIODICITY_MIN;
        // The floor moves after the decision, so a frame never raises the floor it is judged against.
        const tau = db < this.floorDb ? FLOOR_FALL_SECONDS : FLOOR_RISE_SECONDS;
        this.floorDb += (db - this.floorDb) * (1 - Math.exp(-FRAME_SECONDS / tau));
        if (this.floorDb < SILENT_DB) this.floorDb = SILENT_DB;
        const hangFrames = Math.round(HANGOVER_MS / 1000 / FRAME_SECONDS);
        if (raw) {
            this._run++; this._runPeakDb = this._run === 1 ? db : Math.max(this._runPeakDb, db);
            // Mean periodicity of the newest ONSET_FRAMES raw frames (an exponential stand-in, enough for a three-frame run).
            this._runPeriodicity = this._run === 1 ? periodicity : this._runPeriodicity + (periodicity - this._runPeriodicity) / Math.min(this._run, ONSET_FRAMES);
            if (this.voiced || (this._run >= ONSET_FRAMES && db >= this._runPeakDb - ONSET_DECAY_DB && this._runPeriodicity >= ONSET_PERIODICITY)) {
                this.voiced = true; this._hang = hangFrames;
            }
        } else {
            this._run = 0; this._runPeakDb = SILENT_DB; this._runPeriodicity = 0;
            if (this.voiced && this._hang > 0) this._hang--;           // still inside the hangover
            else this.voiced = false;
        }
        this.frames++;
        return (this.last = { rms, db, periodicity, floorDb, raw, voiced: this.voiced });
    }

    _periodicity(frame) {
        // DC-blocked, 2:1 decimated copy appended to the history (the previous frames supply the lagged samples).
        const history = this._history, base = MAX_LAG;
        history.copyWithin(0, DECIMATED);
        let xIn = this._hpIn, yOut = this._hpOut;
        for (let i = 0; i < DECIMATED; i++) {
            let sum = 0;
            for (let j = 0; j < 2; j++) {
                const x = frame[i * 2 + j];
                yOut = x - xIn + DC_POLE * yOut; xIn = x;
                sum += yOut;
            }
            history[base + i] = sum / 2;
        }
        this._hpIn = xIn; this._hpOut = yOut;
        let e0 = 0;
        for (let n = 0; n < DECIMATED; n++) e0 += history[base + n] * history[base + n];
        if (e0 <= 1e-12) { this._lag = 0; return 0; }
        // eLag: energy of the window shifted back by `lag`, kept up to date as the lag grows.
        let eLag = e0, best = 0, exited = false;
        this._lag = 0;
        for (let lag = 1; lag <= MAX_LAG; lag++) {
            const enter = history[base - lag], leave = history[base + DECIMATED - lag];
            eLag += enter * enter - leave * leave;
            let dot = 0;
            for (let n = 0; n < DECIMATED; n++) dot += history[base + n] * history[base + n - lag];
            const r = eLag > 1e-12 ? dot / Math.sqrt(e0 * eLag) : 0;
            if (!exited) { if (r < MAIN_LOBE_EXIT) exited = true; else continue; }
            if (lag >= MIN_LAG && r > best) { best = r; this._lag = lag; }
        }
        return best;
    }
}

// Linear decimation from a capture rate to 16 kHz for analysis only (boxcar at whole ratios, linear interpolation otherwise),
// carrying the fractional position between calls. Used where no echo guard decimates for us (echo protection Off).
export class Decimator {
    constructor(inputRate) {
        this.ratio = inputRate / VOICING_RATE;
        this._whole = Math.abs(this.ratio - Math.round(this.ratio)) < 1e-6 ? Math.round(this.ratio) : 0;
        this._carry = new Float32Array(0); this._pos = 0;
    }
    push(samples) {
        const input = this._carry.length ? concat(this._carry, samples) : samples, r = this.ratio;
        if (!(r > 1)) { this._carry = new Float32Array(0); return Float32Array.from(input); }
        const out = new Float32Array(Math.max(0, Math.ceil((input.length - this._pos) / r) + 1));
        let pos = this._pos, count = 0;
        while (pos + r <= input.length) {
            const idx = Math.floor(pos);
            if (this._whole) { let sum = 0; for (let j = 0; j < this._whole; j++) sum += input[idx + j]; out[count++] = sum / this._whole; }
            else { const frac = pos - idx, a = input[idx], b = input[idx + 1] ?? a; out[count++] = a + (b - a) * frac; }
            pos += r;
        }
        const consumed = Math.floor(pos);
        this._carry = input.slice(consumed); this._pos = pos - consumed;
        return out.subarray(0, count);
    }
}
const concat = (a, b) => { const out = new Float32Array(a.length + b.length); out.set(a, 0); out.set(b, a.length); return out; };

// The noise gate: what is counted and sent is multiplied by a gain that opens (5 ms attack) when a block's raw level reaches the
// threshold, holds 250 ms after the level falls below it, then closes (80 ms release). Off (GATE_OFF_DB) passes everything.
// It never sees or changes what the echo guard analyses: it is applied to released original samples only.
export class NoiseGate {
    constructor({ rate, gateDb = GATE_DEFAULT_DB }) {
        this.rate = rate;
        this._attack = Math.exp(-1 / (GATE_ATTACK_MS / 1000 * rate));
        this._release = Math.exp(-1 / (GATE_RELEASE_MS / 1000 * rate));
        this._holdSamples = Math.round(GATE_HOLD_MS / 1000 * rate);
        this._hold = 0;
        this.gain = 1;
        this.setGate(gateDb);
    }
    setGate(db) { this.gateDb = clampGateDb(db); this._threshold = gateThreshold(this.gateDb); if (!this._threshold) this.gain = 1; }
    get open() { return !this._threshold || this.gain > 0.5; }
    // Time passing with nothing to send (withheld audio): the hold runs down and the gain closes as it would on silence.
    idle(samples) {
        if (!this._threshold) return;
        const held = Math.min(this._hold, samples); this._hold -= held;
        const closing = samples - held;
        if (closing > 0) this.gain *= this._release ** closing;
    }
    // A block of original samples and its raw RMS (computed here when not given). Returns a gated copy, or the block itself when off.
    process(block, rms) {
        if (!this._threshold) return block;
        const level = Number.isFinite(rms) ? rms : Math.sqrt(block.reduce((n, v) => n + v * v, 0) / Math.max(1, block.length));
        let target = 1;
        if (level >= this._threshold) this._hold = this._holdSamples;
        else if (this._hold > 0) this._hold = Math.max(0, this._hold - block.length);
        else target = 0;
        const out = new Float32Array(block.length);
        let gain = this.gain;
        for (let i = 0; i < block.length; i++) {
            const coefficient = target > gain ? this._attack : this._release;
            gain = target + (gain - target) * coefficient;
            out[i] = block[i] * gain;
        }
        this.gain = gain;
        return out;
    }
}
