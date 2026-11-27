// Playback-reference echo guard ("Adaptive v3").
//
// Origin: adapted from Omni-NexusAI/speech-to-speech, web/hf-realtime-voice/worklets/mic-capture.js (the standalone
// Adaptive v3 echo guard; the fork is Apache-2.0 by the same owner, its vendored HF UI part carries no LICENSE of its own).
// Adapted for Convo: a plain class with no browser globals (so node tests drive it directly), the noise gate, the Int16
// packing and the 16 kHz upload format are removed, and it hands back the ORIGINAL capture-rate samples instead.
//
// What it does: it is given the microphone block and, in step, the exact playback PCM that went to the speakers
// (the post-duck output). For every 40 ms analysis frame (decimated to 16 kHz for analysis only) it finds the
// echo lag, correlates envelopes and runs a 256-tap NLMS predictor to decide whether the frame is the assistant's own
// echo or independent speech. Two evidence paths: a learned predictor for acoustic echo (coupled), and an
// independent-speech path for headsets / native AEC where there is no echo to learn (uncoupled).
//
// Contract:
//  - The predictor is classification-only. Its residual is NEVER returned or uploaded: released frames are the
//    untouched original microphone samples.
//  - During playback, frames are held locally. Only after DOUBLE_TALK_MS of confirmed independent speech is the held
//    onset released (all of it, original samples). Ambiguous frames and echo-only frames stay withheld.
//  - Whatever is withheld and not released is reported as `dropped` samples, so the caller can keep time (silence)
//    without ever receiving the echo. Once double talk is confirmed (until playback ends) withheld time is instead synthetic
//    floor-level comfort noise, flagged `comfort` (see KEEP_NEAR_VOICE_MS): never microphone audio, never voiced.
//  - `strict` withholds everything while playback is active (no barge-in during playback); `off` passes everything.
//
// Convo changes to the decision (2026-11, from live tests on a phone, a laptop and a desktop launcher):
//  - Voicing (voicing.js) decides what counts as a human voice: every human candidate frame must be voice-like (periodic, above the
//    adaptive floor and the noise gate), so claps, keys and fan bursts are never independent speech.
//  - Echo evidence counts only at waveform correlation >= ECHO_EVIDENCE_CORRELATION at a stable lag (within one search step over
//    the last LAG_STABLE_FRAMES frames); the delay estimate moves only on a stable lag. Two different voices correlate by chance now
//    and then, which used to latch `coupled` within a quarter second of the user talking over playback.
//  - `coupled` falls back to `uncoupled` after CLEAN_PLAYBACK_MS of clean playback (reference active, microphone below the human
//    floor, no echo evidence): the volume was turned down or a headset plugged in.
//  - Whenever there is no echo evidence (always in `uncoupled`), a voiced frame is independent-speech evidence; DOUBLE_TALK_MS of it
//    releases the held onset. Double-talk state is cleared when playback ends.
//  - Every emitted frame carries its voicing and raw level (`voiced`, `levels`), so SpeechBoundary counts the guard's verdict.

import { GATE_OFF_DB, Voicing } from './voicing.js';

export const ECHO_GUARD_MODES = ['adaptive', 'strict', 'off'];

const ANALYSIS_RATE = 16000;
const CHUNK_MS = 40;
const ECHO_HISTORY_MS = 750;
const ECHO_MAX_LAG_MS = 500;
export const ECHO_TAIL_MS = 350;      // audio.js OVERLAP_TAIL_MS counts the same tail as still heard (a test keeps them equal)
const ECHO_FILTER_TAPS = 256;
const ECHO_NLMS_STEP = 0.05;
const ECHO_NLMS_EPSILON = 1e-7;
const ECHO_MODEL_WARMUP_MS = 600;
// How long independent speech must be confirmed during playback before the held original audio is released.
// This is the same 450 ms as SpeechBoundary's voiced-audio admission (MIN_VOICED_SECONDS in audio.js): during playback
// the guard confirms first and the released onset then admits at once, so the two do not stack.
export const DOUBLE_TALK_MS = 450;
const DOUBLE_TALK_GAP_MS = 120;
// Convo: a quiet pause between words during playback keeps the onset this long (only DOUBLE_TALK_GAP_MS when the gap is echo or noise).
export const QUIET_GAP_MS = 500;
const COUPLED_EVIDENCE_MS = 160;
const CLEAN_PLAYBACK_MS = 240;
const NOISE_FLOOR_HISTORY_MS = 2000;
const ECHO_CORRELATION_EVIDENCE = 0.35;
const ECHO_RESIDUAL_RATIO_MAX = 0.55;
const HUMAN_RESIDUAL_CORRELATION_MAX = 0.35;
const HUMAN_RESIDUAL_RATIO_MIN = 0.60;
const REFERENCE_ACTIVE_RMS = 0.001;
export const HUMAN_FLOOR_MIN_RMS = 0.0015;
export const HUMAN_FLOOR_MAX_RMS = 0.03;
const INDEPENDENT_WAVEFORM_CORRELATION_MAX = 0.20;
const INDEPENDENT_ENVELOPE_CORRELATION_MAX = 0.35;
const INDEPENDENT_RESIDUAL_RATIO_MIN = 0.80;
// Convo additions (not in the standalone guard), for using the floor as the boundary's voiced threshold.
// Until this many quiet frames have been measured the floor is not trusted and the legacy fixed threshold applies.
const CALIBRATION_FRAMES = 12;
export const LEGACY_VOICED_RMS = 0.02;
// Frames above the floor (speech) are kept out of the noise estimate, so a sentence is not learned as "the noise" (which would
// raise the floor above the speaker). A run of this many consecutive such frames (6 s with no pause) is a new noise level
// (a fan switching on) and is adopted, so the floor cannot stay too low for good.
const ADOPT_VOICED_FRAMES = 150;
const SNAPSHOT_FRAMES = 25;
export const ECHO_EVIDENCE_CORRELATION = 0.6;
// A loud headset: the microphone hears a quiet, muffled copy of the playback (live 2026-11-25: Convo answered its own voice twice
// in a row). Such a leak correlates too weakly with the reference to count as echo evidence, so the level is what tells it from
// the user: the slow microphone level during playback, as a share of the slow playback level, is tracked downwards (the
// quietest it gets while the reply plays is the leak; a voice over it only raises it for a moment). Speech on the "no echo here"
// path must be LEAK_FACTOR times louder than that. Where nothing leaks the share is the room's noise and nothing changes.
const LEAK_SMOOTH = 0.1;              // per analysis chunk (40 ms): about 0.4 s, longer than any playback delay's gaps
const LEAK_FAST = 0.35;               // the microphone level a candidate is judged by: about 0.1 s, so one loud peak of a leak is not a voice
const LEAK_RISE = 1.02;               // the tracked share may rise 2 % per chunk (a volume turned up is followed in seconds)
const LEAK_ONSET_RISE = 1.6;          // ...and fast while a stretch of playback begins: the leak arrives after the playback's own delay
const LEAK_ONSET_CHUNKS = 18;         // 0.72 s: a headset's playback delay (wired or Bluetooth) plus the time the slow levels take to settle
const LEAK_IDLE_CHUNKS = 6;           // playback silent this long ends a stretch
const LEAK_FACTOR = 3;                // about 9.5 dB above the leak
const LAG_STEP = 16;                   // _echoMatch searches lags in steps of 16 analysis samples (1 ms)
const LAG_STABLE_FRAMES = 3;
// Convo, double talk (2026-11): once the user's speech over the reply is confirmed, a frame that is not echo is kept only within
// KEEP_NEAR_VOICE_MS of the user's voiced frames (before or after; a frame that may precede voice waits at most this long). Kept frames
// carry the reply too (the original microphone audio is released). Everything else becomes floor-level comfort noise (at least
// COMFORT_MIN_RMS, at most HUMAN_FLOOR_MIN_RMS) with COMFORT_FADE_MS crossfades instead of digital zeros, which left the model holes
// in the user's words. Comfort noise bypasses the noise gate and is never voiced. Starting points from synthetic signals only.
export const KEEP_NEAR_VOICE_MS = 250;
export const COMFORT_FADE_MS = 5;
export const COMFORT_MIN_RMS = 0.0002;

export class EchoGuard {
    constructor({ inputRate, mode = 'adaptive', nativeAec = false, gateDb = GATE_OFF_DB }) {   // the session passes its noise gate
        this.inputRate = inputRate;
        this._ratio = inputRate / ANALYSIS_RATE;
        this._chunk = Math.round(ANALYSIS_RATE * CHUNK_MS / 1000);          // analysis samples per frame
        this._needIn = Math.ceil(this._chunk * this._ratio);                 // capture samples read to build one frame
        this._consume = Math.floor(this._chunk * this._ratio);               // capture samples a frame accounts for
        const capacity = this._needIn * 2 + 512;
        this._mic = new Float32Array(capacity);
        this._ref = new Float32Array(capacity);
        this._fill = 0;
        this._decimated = new Float32Array(this._chunk);
        this._referenceDecimated = new Float32Array(this._chunk);

        this._echoMode = ECHO_GUARD_MODES.includes(mode) ? mode : 'adaptive';
        this._nativeAec = !!nativeAec;
        this._referenceHistory = new Float32Array(0);
        this._echoFilter = new Float32Array(ECHO_FILTER_TAPS);
        this._echoDelaySamples = 0;
        this._echoDelayConfidence = 0;
        this._echoModelSamples = 0;
        this._echoModelReady = false;
        this._frames = 0;
        this._voicing = new Voicing({ gateDb });
        this._meta = new WeakMap();                                          // original frame -> {voiced, level}
        this._keepSamples = Math.round(KEEP_NEAR_VOICE_MS / 1000 * inputRate);
        this._comfortSeed = 0x2545F491; this._comfortState = 0;
        this._resetEchoTransientState();
    }

    // ---- configuration -------------------------------------------------------------------------------------------

    configure({ mode, nativeAec, gateDb }) {
        const next = ECHO_GUARD_MODES.includes(mode) ? mode : 'adaptive';
        if (next !== this._echoMode) this._resetEchoCandidate();
        this._echoMode = next;
        this._nativeAec = !!nativeAec;
        if (gateDb !== undefined) this.setGate(gateDb);
    }
    // The noise gate threshold (dBFS; voicing.js GATE_OFF_DB = off) a voiced frame must reach. Live: no restart.
    setGate(db) { this._voicing.setGate(db); }
    get gateDb() { return this._voicing.gateDb; }

    get mode() { return this._echoMode; }

    // Clears history and predictor (a full reset, e.g. a different microphone).
    reset() { this._resetEchoTransientState(); this._resetEchoPredictor(); this._voicing.reset(); this._fill = 0; }

    // The learned echo path, so a recreated capture (a retarget) can keep it. Plain copies; valid for the same inputRate.
    exportState() {
        return {
            inputRate: this.inputRate, filter: this._echoFilter.slice(), delaySamples: this._echoDelaySamples,
            delayConfidence: this._echoDelayConfidence, modelSamples: this._echoModelSamples,
        };
    }
    importState(state) {
        if (!state || state.inputRate !== this.inputRate || !(state.filter?.length === ECHO_FILTER_TAPS)) return false;
        this._echoFilter = Float32Array.from(state.filter);
        this._echoDelaySamples = state.delaySamples | 0;
        this._echoDelayConfidence = Number(state.delayConfidence) || 0;
        this._echoModelSamples = Math.max(0, state.modelSamples | 0);
        this._echoModelReady = this._echoModelSamples >= Math.round(ECHO_MODEL_WARMUP_MS / 1000 * ANALYSIS_RATE) && this._echoDelayConfidence >= 0.2;
        return true;
    }

    // The RMS above which a block counts as voiced for SpeechBoundary: the measured noise floor (clamped to
    // HUMAN_FLOOR_MIN/MAX), or the legacy fixed threshold until the floor has been measured.
    get voicedFloor() { return this._noiseFrames >= CALIBRATION_FRAMES ? this._humanFloorRms() : LEGACY_VOICED_RMS; }

    // ---- state ----------------------------------------------------------------------------------------------------

    _resetEchoTransientState() {
        this._referenceHistory = new Float32Array(0);
        this._echoTailRemaining = 0;
        this._acousticState = 'unknown';
        this._leakMic = 0; this._leakRef = 0; this._leakFastMic = 0; this._leakShare = 0; this._leakChunks = 0; this._leakStretch = 0; this._leakIdle = 0;
        this._candidatePath = 'none';
        this._lastRejectReason = 'warming';
        this._echoEvidenceSamples = 0;
        this._cleanPlaybackSamples = 0;
        this._independentSamples = 0;
        this._micEnvelope = [];
        this._referenceEnvelope = [];
        this._noiseFloorFrames = [];
        this._noiseFloorRms = HUMAN_FLOOR_MIN_RMS / 3;
        this._noiseFrames = 0;
        this._voicedRun = 0;
        this._suppressedMs = 0;
        this._echoMetricCounter = 0;
        this._lags = [];
        this._clockIn = 0; this._frameStart = 0; this._lastVoiceEnd = -Infinity; this._overlapSpeech = false;
        this._lastKind = ''; this._lastSample = 0;
        this._resetEchoCandidate();
    }

    // The echo lag is stable when the last LAG_STABLE_FRAMES matches all found a lag within one search step of each other.
    _lagStable() {
        if (this._lags.length < LAG_STABLE_FRAMES || this._lags.some(lag => lag === null)) return false;
        return Math.max(...this._lags) - Math.min(...this._lags) <= LAG_STEP;
    }

    _resetEchoCandidate() {
        this._doubleTalkSamples = 0;
        this._doubleTalkGapSamples = 0;
        this._doubleTalkActive = false;
        this._pendingHumanChunks = [];
        this._pendingSamples = 0;
        this._dtQueue = []; this._dtQueueSamples = 0;
    }

    _resetEchoPredictor() {
        this._echoFilter = new Float32Array(ECHO_FILTER_TAPS);
        this._echoDelaySamples = 0;
        this._echoDelayConfidence = 0;
        this._echoModelSamples = 0;
        this._echoModelReady = false;
    }

    _humanFloorRms() {
        return Math.max(HUMAN_FLOOR_MIN_RMS, Math.min(HUMAN_FLOOR_MAX_RMS, this._noiseFloorRms * 3));
    }

    _recordNoiseFloor(rms, playbackActive) {
        if (playbackActive) return;
        const maxFrames = Math.max(1, Math.round(NOISE_FLOOR_HISTORY_MS / CHUNK_MS));
        // After calibration a frame above the floor is not noise (see ADOPT_VOICED_FRAMES).
        if (this._noiseFrames >= CALIBRATION_FRAMES && rms >= this._humanFloorRms()) {
            if (++this._voicedRun < ADOPT_VOICED_FRAMES) return;
        } else this._voicedRun = 0;
        this._noiseFrames++;
        this._noiseFloorFrames.push(rms);
        if (this._noiseFloorFrames.length > maxFrames) this._noiseFloorFrames.shift();
        const values = [...this._noiseFloorFrames].sort((a, b) => a - b);
        this._noiseFloorRms = values[Math.floor((values.length - 1) * 0.2)] ?? this._noiseFloorRms;
    }

    _appendEnvelope(micRms, referenceRms) {
        const maxFrames = Math.max(1, Math.ceil(ECHO_HISTORY_MS / CHUNK_MS));
        this._micEnvelope.push(micRms);
        this._referenceEnvelope.push(referenceRms);
        if (this._micEnvelope.length > maxFrames) this._micEnvelope.shift();
        if (this._referenceEnvelope.length > maxFrames) this._referenceEnvelope.shift();
    }

    _envelopeCorrelation() {
        const mic = this._micEnvelope;
        const reference = this._referenceEnvelope;
        const frames = Math.min(12, mic.length, reference.length);
        const maxLag = Math.min(Math.ceil(ECHO_MAX_LAG_MS / CHUNK_MS), reference.length - frames);
        if (frames < 6 || maxLag < 0) return 0;
        const micStart = mic.length - frames;
        let best = 0;
        for (let lag = 0; lag <= maxLag; lag++) {
            const refStart = reference.length - frames - lag;
            let micMean = 0;
            let refMean = 0;
            for (let i = 0; i < frames; i++) {
                micMean += mic[micStart + i];
                refMean += reference[refStart + i];
            }
            micMean /= frames;
            refMean /= frames;
            let dot = 0;
            let micEnergy = 0;
            let refEnergy = 0;
            for (let i = 0; i < frames; i++) {
                const a = mic[micStart + i] - micMean;
                const b = reference[refStart + i] - refMean;
                dot += a * b;
                micEnergy += a * a;
                refEnergy += b * b;
            }
            if (micEnergy > ECHO_NLMS_EPSILON && refEnergy > ECHO_NLMS_EPSILON) {
                best = Math.max(best, Math.abs(dot) / Math.sqrt(micEnergy * refEnergy));
            }
        }
        return best;
    }

    _appendReference(chunk) {
        const maxSamples = Math.round((ECHO_HISTORY_MS / 1000) * ANALYSIS_RATE);
        const next = new Float32Array(Math.min(maxSamples, this._referenceHistory.length + chunk.length));
        const keep = Math.max(0, next.length - chunk.length);
        if (keep > 0) next.set(this._referenceHistory.subarray(this._referenceHistory.length - keep), 0);
        next.set(chunk.subarray(Math.max(0, chunk.length - next.length)), keep);
        this._referenceHistory = next;
    }

    _echoMatch(mic, micEnergy) {
        const history = this._referenceHistory;
        const n = mic.length;
        const maxLag = Math.min(Math.round((ECHO_MAX_LAG_MS / 1000) * ANALYSIS_RATE), history.length - n);
        if (micEnergy <= 1e-9 || maxLag < 0) {
            return { correlation: 0, residual: 1, lagMs: 0, lagSamples: 0, start: -1 };
        }
        let bestCorrelation = 0;
        let bestStart = -1;
        let bestRefEnergy = 0;
        for (let lag = 0; lag <= maxLag; lag += 16) {
            const start = history.length - n - lag;
            let dot = 0;
            let refEnergy = 0;
            for (let i = 0; i < n; i++) {
                const ref = history[start + i];
                dot += mic[i] * ref;
                refEnergy += ref * ref;
            }
            if (refEnergy <= 1e-9) continue;
            const correlation = Math.abs(dot) / Math.sqrt(micEnergy * refEnergy);
            if (correlation > bestCorrelation) {
                bestCorrelation = correlation;
                bestStart = start;
                bestRefEnergy = refEnergy;
            }
        }
        if (bestStart < 0) {
            return { correlation: 0, residual: 1, lagMs: 0, lagSamples: 0, start: -1 };
        }
        let dot = 0;
        for (let i = 0; i < n; i++) dot += mic[i] * history[bestStart + i];
        const gain = dot / bestRefEnergy;
        let residualEnergy = 0;
        for (let i = 0; i < n; i++) {
            const residual = mic[i] - gain * history[bestStart + i];
            residualEnergy += residual * residual;
        }
        const lagSamples = history.length - n - bestStart;
        return {
            correlation: bestCorrelation,
            residual: Math.sqrt(residualEnergy / micEnergy),
            lagMs: (lagSamples / ANALYSIS_RATE) * 1000,
            lagSamples,
            start: bestStart,
        };
    }

    // Predict the microphone echo from the delayed playback reference. The residual is used only for classification and
    // diagnostics; released audio always comes from the untouched capture samples.
    _echoPrediction(mic, adapt) {
        const history = this._referenceHistory;
        const n = mic.length;
        const start = history.length - n - this._echoDelaySamples;
        if (start < 0) {
            let energy = 0;
            for (let i = 0; i < n; i++) energy += mic[i] * mic[i];
            const rms = Math.sqrt(energy / n);
            return { residualRatio: 1, residualRms: rms, residualCorrelation: 1, erleDb: 0, predictionRms: 0 };
        }

        const weights = this._echoFilter;
        let micEnergy = 0;
        let residualEnergy = 0;
        let predictionEnergy = 0;
        let residualReferenceDot = 0;
        let referenceEnergy = 0;
        for (let i = 0; i < n; i++) {
            const referenceIndex = start + i;
            const taps = Math.min(ECHO_FILTER_TAPS, referenceIndex + 1);
            let predicted = 0;
            let norm = ECHO_NLMS_EPSILON;
            for (let tap = 0; tap < taps; tap++) {
                const reference = history[referenceIndex - tap];
                predicted += weights[tap] * reference;
                norm += reference * reference;
            }
            const error = mic[i] - predicted;
            micEnergy += mic[i] * mic[i];
            residualEnergy += error * error;
            predictionEnergy += predicted * predicted;
            const alignedReference = history[referenceIndex];
            residualReferenceDot += error * alignedReference;
            referenceEnergy += alignedReference * alignedReference;
            if (adapt) {
                const scale = (ECHO_NLMS_STEP * error) / norm;
                for (let tap = 0; tap < taps; tap++) {
                    const nextWeight = weights[tap] + scale * history[referenceIndex - tap];
                    weights[tap] = Math.max(-2, Math.min(2, nextWeight));
                }
            }
        }
        const safeMicEnergy = Math.max(micEnergy, ECHO_NLMS_EPSILON);
        return {
            residualRatio: Math.sqrt(residualEnergy / safeMicEnergy),
            residualRms: Math.sqrt(residualEnergy / n),
            residualCorrelation:
                Math.abs(residualReferenceDot)
                / Math.sqrt(Math.max(residualEnergy * referenceEnergy, ECHO_NLMS_EPSILON)),
            erleDb: 10 * Math.log10(safeMicEnergy / Math.max(residualEnergy, ECHO_NLMS_EPSILON)),
            predictionRms: Math.sqrt(predictionEnergy / n),
        };
    }

    // ---- processing -----------------------------------------------------------------------------------------------

    // Feed capture-rate samples and the same-length playback reference (null/short = silence). Returns one result per
    // completed 40 ms frame:
    //   released: Float32Array[]  original capture samples now cleared to go on (oldest first; may include a held onset)
    //   dropped:  number          capture samples that were withheld for good (the caller treats them as silence)
    //   floor:    number          voiced threshold to use for these frames
    //   rms:      number          level of the current frame (pre-decision)
    //   metric:   object|null     diagnostics, about every 200 ms
    //   state:    object|null     a snapshot of the learned echo path, about every second (see exportState)
    ingest(mic, reference) {
        const results = [];
        let offset = 0;
        while (offset < mic.length) {
            const room = this._mic.length - this._fill;
            const take = Math.min(room, mic.length - offset);
            this._mic.set(mic.subarray(offset, offset + take), this._fill);
            if (reference) {
                const available = Math.max(0, Math.min(take, reference.length - offset));
                if (available) this._ref.set(reference.subarray(offset, offset + available), this._fill);
                if (available < take) this._ref.fill(0, this._fill + available, this._fill + take);
            } else this._ref.fill(0, this._fill, this._fill + take);
            this._fill += take; offset += take;
            this._drain(results);
        }
        return results;
    }

    _drain(results) {
        const r = this._ratio, n = this._chunk, dec = this._decimated, refDec = this._referenceDecimated;
        while (this._fill >= this._needIn) {
            // 1. Decimate to 16 kHz for analysis only (boxcar at 3:1, linear interpolation elsewhere).
            let sumSq = 0;
            if (Math.abs(r - 3) < 1e-6) {
                for (let i = 0; i < n; i++) {
                    const idx = i * 3;
                    const s = (this._mic[idx] + this._mic[idx + 1] + this._mic[idx + 2]) / 3;
                    dec[i] = s;
                    refDec[i] = (this._ref[idx] + this._ref[idx + 1] + this._ref[idx + 2]) / 3;
                    sumSq += s * s;
                }
            } else {
                for (let i = 0; i < n; i++) {
                    const srcPos = i * r;
                    const idx = Math.floor(srcPos);
                    const frac = srcPos - idx;
                    const a = this._mic[idx];
                    const b = this._mic[idx + 1] ?? a;
                    const s = a + (b - a) * frac;
                    dec[i] = s;
                    const refA = this._ref[idx] || 0;
                    const refB = this._ref[idx + 1] ?? refA;
                    refDec[i] = refA + (refB - refA) * frac;
                    sumSq += s * s;
                }
            }
            const original = this._mic.slice(0, this._consume);
            results.push(this._frame(original, dec, refDec, sumSq));
            this._mic.copyWithin(0, this._consume, this._fill);
            this._ref.copyWithin(0, this._consume, this._fill);
            this._fill -= this._consume;
        }
    }

    _frame(original, dec, refDec, sumSq) {
        const n = this._chunk;
        const rms = Math.sqrt(sumSq / n);
        let refSumSq = 0;
        for (let i = 0; i < n; i++) refSumSq += refDec[i] * refDec[i];
        const referenceRms = Math.sqrt(refSumSq / n);
        this._appendReference(refDec);
        if (referenceRms >= REFERENCE_ACTIVE_RMS) {
            this._echoTailRemaining = Math.round((ECHO_TAIL_MS / 1000) * ANALYSIS_RATE);
        } else {
            this._echoTailRemaining = Math.max(0, this._echoTailRemaining - n);
        }
        const playbackActive = referenceRms >= REFERENCE_ACTIVE_RMS || this._echoTailRemaining > 0;
        this._recordNoiseFloor(rms, playbackActive);
        this._appendEnvelope(rms, referenceRms);
        const humanFloorRms = this._humanFloorRms();
        const envelopeCorrelation = this._envelopeCorrelation();
        const match = this._echoMatch(dec, sumSq);
        // Voicing of this frame's two 20 ms halves: `voiced` (with hangover) is what SpeechBoundary counts; a raw-voiced half whose
        // onset is still being confirmed also makes the frame voice-like here, so a held onset includes its first 40 ms.
        const voicing = this._voicing.push(dec);
        const voiced = voicing.some(item => item.voiced);
        const voiceLike = voiced || voicing.some(item => item.raw);
        const periodicity = voicing.reduce((best, item) => Math.max(best, item.periodicity), 0);
        this._meta.set(original, { voiced, level: rms });
        this._lags.push(playbackActive && match.correlation >= 0.2 ? match.lagSamples : null);
        if (this._lags.length > LAG_STABLE_FRAMES) this._lags.shift();
        const lagStable = this._lagStable();
        if (playbackActive && match.correlation >= 0.2 && lagStable) {
            const delayShift = Math.abs(match.lagSamples - this._echoDelaySamples);
            if (
                this._echoDelayConfidence === 0
                || delayShift <= Math.round(0.08 * ANALYSIS_RATE)
                || match.correlation > this._echoDelayConfidence + 0.1
            ) {
                if (
                    this._echoDelayConfidence === 0
                    || delayShift > Math.round(0.08 * ANALYSIS_RATE)
                ) {
                    this._echoFilter = new Float32Array(ECHO_FILTER_TAPS);
                    this._echoModelSamples = 0;
                    this._echoModelReady = false;
                }
                this._echoDelaySamples = match.lagSamples;
            }
            this._echoDelayConfidence =
                this._echoDelayConfidence === 0
                    ? match.correlation
                    : this._echoDelayConfidence * 0.85 + match.correlation * 0.15;
        }

        // Echo evidence: the microphone is the playback at a stable delay (see ECHO_EVIDENCE_CORRELATION).
        const referenceEchoEvidence = playbackActive && lagStable && match.correlation >= ECHO_EVIDENCE_CORRELATION;
        // Classify before adapting. This prevents real near-end speech from being learned into the echo path merely
        // because the raw mic still correlates with loud playback.
        const prediction = this._echoPrediction(dec, false);
        const modeledHumanCandidate =
            playbackActive
            && voiceLike
            && !referenceEchoEvidence                                 // the raw microphone is still the playback: not a person
            && this._echoModelReady
            && rms >= humanFloorRms
            && prediction.residualRms >= humanFloorRms
            && prediction.residualRatio >= HUMAN_RESIDUAL_RATIO_MIN
            && prediction.residualCorrelation < HUMAN_RESIDUAL_CORRELATION_MAX;
        const echoEvidence =
            referenceEchoEvidence
            || prediction.residualRatio <= ECHO_RESIDUAL_RATIO_MAX;
        const echoDominant =
            playbackActive
            && !modeledHumanCandidate
            && (
                match.residual <= ECHO_RESIDUAL_RATIO_MAX                // the playback itself, almost unchanged (correlation >= .83)
                || prediction.residualRatio <= ECHO_RESIDUAL_RATIO_MAX
                || referenceEchoEvidence
            );
        // Independent speech: a voice-like frame with no echo evidence. While the room is known to couple the speaker into the
        // microphone, the waveform must also be clearly unlike the playback (the standalone guard's independence test).
        let aboveLeak = true, leakOnset = false;
        if (playbackActive && referenceRms >= REFERENCE_ACTIVE_RMS) {
            const first = this._leakChunks++ === 0;
            if (this._leakIdle >= LEAK_IDLE_CHUNKS) this._leakStretch = 0;
            this._leakIdle = 0;
            const onset = this._leakStretch++ < LEAK_ONSET_CHUNKS;
            this._leakRef = first ? referenceRms : this._leakRef + LEAK_SMOOTH * (referenceRms - this._leakRef);
            this._leakMic = first ? rms : this._leakMic + LEAK_SMOOTH * (rms - this._leakMic);
            this._leakFastMic = first ? rms : this._leakFastMic + LEAK_FAST * (rms - this._leakFastMic);
            const share = this._leakMic / Math.max(this._leakRef, 1e-6);
            this._leakShare = first ? share : Math.min(share, this._leakShare * (onset ? LEAK_ONSET_RISE : LEAK_RISE) + 1e-5);
            // While a stretch begins the leak's level is not known yet: speech is held and counted, but not confirmed (below).
            leakOnset = onset;
            aboveLeak = this._leakFastMic >= LEAK_FACTOR * this._leakShare * this._leakRef;
        } else if (this._leakIdle < LEAK_IDLE_CHUNKS) this._leakIdle++;
        const independentCandidate =
            playbackActive
            && voiceLike
            && rms >= humanFloorRms
            && (aboveLeak || leakOnset)                               // not merely the playback leaking into a headset microphone
            && !echoEvidence
            && !echoDominant;
        const adaptEchoPath =
            playbackActive
            && referenceRms >= REFERENCE_ACTIVE_RMS
            && rms >= REFERENCE_ACTIVE_RMS
            && echoEvidence
            && echoDominant;
        if (adaptEchoPath) {
            this._echoPrediction(dec, true);
            this._echoModelSamples = Math.min(
                Math.round((ECHO_MODEL_WARMUP_MS / 1000) * ANALYSIS_RATE),
                this._echoModelSamples + n,
            );
        }
        this._echoModelReady =
            this._echoModelSamples >= Math.round((ECHO_MODEL_WARMUP_MS / 1000) * ANALYSIS_RATE)
            && this._echoDelayConfidence >= 0.2;

        if (playbackActive && referenceRms >= REFERENCE_ACTIVE_RMS) {
            if (referenceEchoEvidence) {
                this._echoEvidenceSamples += n;
                this._cleanPlaybackSamples = 0;
            } else if (rms < humanFloorRms) {
                this._cleanPlaybackSamples += n;
                this._echoEvidenceSamples = 0;
            } else {
                this._echoEvidenceSamples = 0;
                this._cleanPlaybackSamples = 0;
            }
            if (this._acousticState !== 'coupled' && (
                (this._acousticState === 'unknown' && this._echoModelReady && referenceEchoEvidence)
                || this._echoEvidenceSamples >= Math.round((COUPLED_EVIDENCE_MS / 1000) * ANALYSIS_RATE)
            )) {
                this._acousticState = 'coupled';
            } else if (
                this._acousticState !== 'uncoupled'
                && this._cleanPlaybackSamples >= Math.round((CLEAN_PLAYBACK_MS / 1000) * ANALYSIS_RATE)
            ) {
                this._acousticState = 'uncoupled';            // also out of `coupled`: the speaker is no longer heard
            }
        }

        const humanCandidate = modeledHumanCandidate || independentCandidate;
        if (independentCandidate) this._independentSamples += n;
        else this._independentSamples = 0;
        this._candidatePath = modeledHumanCandidate ? 'modeled' : independentCandidate ? 'independent' : 'none';
        this._lastRejectReason = echoDominant
            ? 'echo_dominant'
            : humanCandidate
                ? 'candidate'
                : rms < humanFloorRms
                    ? 'below_human_floor'
                    : this._acousticState === 'unknown'
                        ? 'ambiguous_warmup'
                        : 'ambiguous';

        // Decide what to release. Everything is in terms of ORIGINAL capture samples (`original`), never residuals. `entries` is the
        // ordered output: {frame} original samples, or {comfort: samples} floor-level noise standing in for withheld time while the user
        // talks over the reply (see KEEP_NEAR_VOICE_MS).
        const pendingBefore = this._pendingSamples, queuedBefore = this._dtQueueSamples, frameStart = this._frameStart = this._clockIn;
        this._clockIn += original.length;
        let framesToSend = [original];
        let entries = null;
        let suppressEcho = this._echoMode === 'strict' ? playbackActive : false;
        const holdFrame = () => { this._pendingHumanChunks.push(original); this._pendingSamples += original.length; };
        const discardPending = () => { this._pendingHumanChunks = []; this._pendingSamples = 0; };
        if (this._echoMode === 'strict' && playbackActive) {
            framesToSend = [];
        } else if (this._echoMode === 'adaptive' && playbackActive && this._doubleTalkActive) {
            // Confirmed double talk. A voiced frame of the user is kept, and so is any frame that is not echo within
            // KEEP_NEAR_VOICE_MS of it (consonants, soft syllables, the gaps between words), before or after: a frame that may precede
            // voice waits in a short queue. Echo, and anything farther from the user's voice, becomes comfort noise, never zeros.
            entries = [];
            if (humanCandidate) {
                this._doubleTalkGapSamples = 0;
                entries.push(...this._releaseQueue(true), { frame: original });
                this._lastVoiceEnd = this._clockIn;
                suppressEcho = false;
            } else if (echoDominant && (this._doubleTalkGapSamples += n) > Math.round((DOUBLE_TALK_GAP_MS / 1000) * ANALYSIS_RATE)) {
                // The user stopped and the reply goes on: double talk ends (this frame is withheld time, comfort while overlapping).
                entries.push(...this._releaseQueue(false));
                discardPending();
                this._doubleTalkSamples = 0; this._doubleTalkGapSamples = 0; this._doubleTalkActive = false;
                suppressEcho = true;
            } else {
                if (!echoDominant) { this._doubleTalkSamples = 0; this._doubleTalkGapSamples = 0; }
                const near = !echoDominant && frameStart - this._lastVoiceEnd < this._keepSamples;
                if (near && !this._dtQueue.length) { entries.push({ frame: original }); suppressEcho = false; }
                else { this._dtQueue.push({ frame: original, comfort: echoDominant }); this._dtQueueSamples += original.length; suppressEcho = true; }
            }
            // Comfort entries at the head never wait; an ambiguous frame that waited longer than the window had no voice after it.
            while (this._dtQueue.length && (this._dtQueue[0].comfort || this._dtQueueSamples > this._keepSamples)) {
                const head = this._dtQueue.shift(); this._dtQueueSamples -= head.frame.length;
                entries.push({ comfort: head.frame.length });
            }
            framesToSend = entries.filter(entry => entry.frame).map(entry => entry.frame);
        } else if (this._echoMode === 'adaptive' && playbackActive) {
            if (echoDominant) {
                const candidateInProgress = this._pendingHumanChunks.length > 0 || this._doubleTalkActive;
                this._doubleTalkGapSamples += n;
                if (
                    candidateInProgress
                    && this._doubleTalkGapSamples <= Math.round((DOUBLE_TALK_GAP_MS / 1000) * ANALYSIS_RATE)
                ) {
                    if (this._doubleTalkActive) {
                        framesToSend = [];
                        suppressEcho = true;
                    } else {
                        holdFrame();
                        framesToSend = [];
                        suppressEcho = true;
                    }
                } else {
                    discardPending();
                    this._doubleTalkSamples = 0;
                    this._doubleTalkGapSamples = 0;
                    this._doubleTalkActive = false;
                    framesToSend = [];
                    suppressEcho = true;
                }
            } else if (humanCandidate && !this._doubleTalkActive) {
                holdFrame();
                // While a stretch of playback begins the leak's level is still being learned: the frame is kept (nothing of an
                // onset is lost) but does not count towards confirmation, so one loud moment afterwards cannot confirm a leak.
                if (!leakOnset || modeledHumanCandidate) this._doubleTalkSamples += n;
                this._doubleTalkGapSamples = 0;
                const confirmed =
                    this._doubleTalkSamples >= Math.round((DOUBLE_TALK_MS / 1000) * ANALYSIS_RATE);
                if (confirmed) {
                    this._doubleTalkActive = true; this._overlapSpeech = true; this._lastVoiceEnd = this._clockIn;
                    if (this._candidatePath === 'independent') this._acousticState = 'uncoupled';
                    framesToSend = this._pendingHumanChunks;
                    discardPending();
                    suppressEcho = false;
                } else {
                    framesToSend = [];
                    suppressEcho = true;
                }
            } else if (this._pendingHumanChunks.length) {
                // Natural speech has short low-energy consonants and inter-syllable gaps. Keep the onset local across a
                // small ambiguous gap rather than requiring 450 ms of perfectly uniform frames.
                // A quiet pause between words (below the human floor, no echo evidence) is not echo: it may last QUIET_GAP_MS.
                this._doubleTalkGapSamples += n;
                const gapLimit = rms < humanFloorRms && !referenceEchoEvidence ? QUIET_GAP_MS : DOUBLE_TALK_GAP_MS;
                if (this._doubleTalkGapSamples <= Math.round((gapLimit / 1000) * ANALYSIS_RATE)) {
                    holdFrame();
                } else {
                    discardPending();
                    this._doubleTalkSamples = 0;
                    this._doubleTalkGapSamples = 0;
                }
                framesToSend = [];
                suppressEcho = true;
            } else {
                discardPending();
                this._doubleTalkSamples = 0;
                this._doubleTalkGapSamples = 0;
                framesToSend = [];
                suppressEcho = true;
            }
        } else if (this._pendingHumanChunks.length) {
            framesToSend = [...this._pendingHumanChunks, original];
            discardPending();
        }
        if (!entries && this._dtQueue.length) {
            // Playback ended (or the mode changed) with frames still waiting for the user's voice: they leave in order first.
            entries = [...this._releaseQueue(false), ...framesToSend.map(frame => ({ frame }))];
            framesToSend = entries.filter(entry => entry.frame).map(entry => entry.frame);
        }
        if (!playbackActive) {                         // playback (and its tail) ended: no double talk carries into the next reply
            this._doubleTalkSamples = 0; this._doubleTalkGapSamples = 0; this._doubleTalkActive = false;
        }
        if (suppressEcho) this._suppressedMs += (n / ANALYSIS_RATE) * 1000;

        const releasedSamples = framesToSend.reduce((total, frame) => total + frame.length, 0);
        const comforted = entries ? entries.reduce((total, entry) => total + (entry.comfort || 0), 0) : 0;
        let dropped = Math.max(0, pendingBefore + queuedBefore + original.length - releasedSamples - comforted - this._pendingSamples - this._dtQueueSamples);
        // While the user's confirmed speech overlaps the reply, withheld time is comfort noise too: no digital holes in their turn.
        if (this._overlapSpeech && dropped > 0) { (entries ||= framesToSend.map(frame => ({ frame }))).push({ comfort: dropped }); dropped = 0; }
        if (!playbackActive) this._overlapSpeech = false;
        const out = this._emit(entries || framesToSend.map(frame => ({ frame })), dropped);

        this._frames++;
        this._echoMetricCounter += 1;
        let metric = null;
        if (this._echoMetricCounter >= 5) {
            this._echoMetricCounter = 0;
            metric = {
                kind: 'echo_metric',
                mode: this._echoMode,
                nativeAec: this._nativeAec,
                correlation: match.correlation,
                envelopeCorrelation,
                residual: prediction.residualRatio,
                residualCorrelation: prediction.residualCorrelation,
                residualEnergy: prediction.residualRms * prediction.residualRms,
                erleDb: prediction.erleDb,
                lagMs: (this._echoDelaySamples / ANALYSIS_RATE) * 1000,
                modelReady: this._echoModelReady,
                predictionConfidence: Math.max(0, Math.min(1, 1 - prediction.residualRatio)),
                acousticState: this._acousticState,
                candidatePath: this._candidatePath,
                rejectReason: this._lastRejectReason,
                noiseFloorRms: this._noiseFloorRms,
                humanFloorRms,
                independentMs: (this._independentSamples / ANALYSIS_RATE) * 1000,
                echoEvidenceMs: (this._echoEvidenceSamples / ANALYSIS_RATE) * 1000,
                candidateMs: (this._doubleTalkSamples / ANALYSIS_RATE) * 1000,
                echoEvidence: referenceEchoEvidence,
                lagStable,
                voiced,
                periodicity,
                voicingFloorDb: this._voicing.floorDb,
                gateDb: this._voicing.gateDb,
                suppressedMs: this._suppressedMs,
                suppressing: suppressEcho,
                doubleTalk: this._doubleTalkActive,
                playbackActive,
            };
        }
        const state = this._frames % SNAPSHOT_FRAMES === 0 && this._echoModelSamples > 0 ? this.exportState() : null;
        // `withheld`: something audible in this frame was kept back (echo, or speech not yet confirmed); the meter shows it apart.
        const withheld = suppressEcho && rms >= humanFloorRms;
        // `released`: the original frames let through (for accounting). `frames` is what to send, in order, with `voiced`, `levels` and
        // `comfort` per entry: the released frames (the first one after comfort noise crossfaded from it) and the comfort noise itself.
        return { released: framesToSend, frames: out.frames, voiced: out.voiced, levels: out.levels, comfort: out.comfort,
                 dropped, floor: this.voicedFloor, rms, level: rms, voicedNow: voiced, periodicity, withheld, acousticState: this._acousticState,
                 metric, state, playbackActive };
    }

    // ---- double talk: keep near voice, comfort noise elsewhere ---------------------------------------------------------------

    // The frames waiting for the user's voice leave in order: as original samples when voice followed (`voiceFollowed`) or when they
    // were within the window after the last voice, as comfort noise otherwise; echo entries are always comfort noise.
    _releaseQueue(voiceFollowed) {
        const entries = [];
        for (const item of this._dtQueue) {
            const start = this._frameStart - this._dtQueueSamples;   // where this queued frame began (the queue ends where this frame starts)
            const near = voiceFollowed || start - this._lastVoiceEnd < this._keepSamples;
            entries.push(!item.comfort && near ? { frame: item.frame } : { comfort: item.frame.length });
            this._dtQueueSamples -= item.frame.length;
        }
        this._dtQueue = []; this._dtQueueSamples = 0;
        return entries;
    }

    // Floor-level noise (white, gently low-passed) at the measured room floor, never louder than HUMAN_FLOOR_MIN_RMS and never digital
    // silence. It is not microphone audio (nothing of the reply), is sent past the gate and is never voiced.
    _comfortNoise(length) {
        const out = new Float32Array(length), level = Math.max(COMFORT_MIN_RMS, Math.min(HUMAN_FLOOR_MIN_RMS, this._noiseFloorRms));
        const scale = level / 0.33;                                 // a one-pole (a = .5) low-pass of uniform noise has an RMS of about .33
        let seed = this._comfortSeed, y = this._comfortState;
        for (let i = 0; i < length; i++) {
            seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5;
            y += .5 * (((seed >>> 0) / 4294967296) * 2 - 1 - y);
            out[i] = y * scale || scale * 1e-3;                     // never an exact zero
        }
        this._comfortSeed = seed; this._comfortState = y;
        return out;
    }

    // Build the outgoing frames with short crossfades: comfort noise starts from the last sample sent (no click), and the first original
    // frame after comfort noise fades in over COMFORT_FADE_MS from the noise. Dropped time (not overlapping) stays reported as `dropped`.
    _emit(entries, dropped) {
        const fade = Math.max(1, Math.round(COMFORT_FADE_MS / 1000 * this.inputRate));
        const out = { frames: [], voiced: [], levels: [], comfort: [] };
        if (dropped > 0) { this._lastKind = 'zero'; this._lastSample = 0; }
        for (const entry of entries) {
            if (entry.frame) {
                const info = this._meta.get(entry.frame) || { voiced: false, level: 0 };
                let frame = entry.frame;
                if (this._lastKind === 'comfort' && frame.length) {
                    const noise = this._comfortNoise(Math.min(fade, frame.length));
                    frame = Float32Array.from(frame);
                    for (let i = 0; i < noise.length; i++) { const r = i / noise.length; frame[i] = frame[i] * r + noise[i] * (1 - r); }
                }
                out.frames.push(frame); out.voiced.push(info.voiced); out.levels.push(info.level); out.comfort.push(false);
                this._lastKind = 'audio'; this._lastSample = frame[frame.length - 1] || 0;
            } else if (entry.comfort > 0) {
                const noise = this._comfortNoise(entry.comfort);
                if (this._lastKind === 'audio') {
                    const length = Math.min(fade, noise.length);
                    for (let i = 0; i < length; i++) { const r = i / length; noise[i] = this._lastSample * (1 - r) + noise[i] * r; }
                }
                out.frames.push(noise); out.voiced.push(false); out.levels.push(0); out.comfort.push(true);
                this._lastKind = 'comfort'; this._lastSample = noise[noise.length - 1];
            }
        }
        return out;
    }
}
