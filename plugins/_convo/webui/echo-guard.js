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
//    without ever receiving the echo.
//  - `strict` withholds everything while playback is active (no barge-in during playback); `off` passes everything.

export const ECHO_GUARD_MODES = ['adaptive', 'strict', 'off'];

const ANALYSIS_RATE = 16000;
const CHUNK_MS = 40;
const ECHO_HISTORY_MS = 750;
const ECHO_MAX_LAG_MS = 500;
const ECHO_TAIL_MS = 350;
const ECHO_FILTER_TAPS = 256;
const ECHO_NLMS_STEP = 0.05;
const ECHO_NLMS_EPSILON = 1e-7;
const ECHO_MODEL_WARMUP_MS = 600;
// How long independent speech must be confirmed during playback before the held original audio is released.
// This is the same 450 ms as SpeechBoundary's voiced-audio admission (MIN_VOICED_SECONDS in audio.js): during playback
// the guard confirms first and the released onset then admits at once, so the two do not stack.
export const DOUBLE_TALK_MS = 450;
const DOUBLE_TALK_GAP_MS = 120;
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

export class EchoGuard {
    constructor({ inputRate, mode = 'adaptive', nativeAec = false }) {
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
        this._resetEchoTransientState();
    }

    // ---- configuration -------------------------------------------------------------------------------------------

    configure({ mode, nativeAec }) {
        const next = ECHO_GUARD_MODES.includes(mode) ? mode : 'adaptive';
        if (next !== this._echoMode) this._resetEchoCandidate();
        this._echoMode = next;
        this._nativeAec = !!nativeAec;
    }

    get mode() { return this._echoMode; }

    // Clears history and predictor (a full reset, e.g. a different microphone).
    reset() { this._resetEchoTransientState(); this._resetEchoPredictor(); this._fill = 0; }

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
        this._resetEchoCandidate();
    }

    _resetEchoCandidate() {
        this._doubleTalkSamples = 0;
        this._doubleTalkGapSamples = 0;
        this._doubleTalkActive = false;
        this._pendingHumanChunks = [];
        this._pendingSamples = 0;
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
        if (playbackActive && match.correlation >= 0.2) {
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

        const referenceEchoEvidence =
            match.correlation >= ECHO_CORRELATION_EVIDENCE
            || match.residual <= ECHO_RESIDUAL_RATIO_MAX
            || envelopeCorrelation >= 0.55;
        // Classify before adapting. This prevents real near-end speech from being learned into the echo path merely
        // because the raw mic still correlates with loud playback.
        const prediction = this._echoPrediction(dec, false);
        const modeledHumanCandidate =
            playbackActive
            && this._echoModelReady
            && rms >= humanFloorRms
            && prediction.residualRms >= humanFloorRms
            && prediction.residualRatio >= HUMAN_RESIDUAL_RATIO_MIN
            && prediction.residualCorrelation < HUMAN_RESIDUAL_CORRELATION_MAX;
        const independentCandidate =
            playbackActive
            && rms >= humanFloorRms
            && match.correlation < INDEPENDENT_WAVEFORM_CORRELATION_MAX
            && envelopeCorrelation < INDEPENDENT_ENVELOPE_CORRELATION_MAX
            && match.residual >= INDEPENDENT_RESIDUAL_RATIO_MIN;
        const echoEvidence =
            referenceEchoEvidence
            || prediction.residualRatio <= ECHO_RESIDUAL_RATIO_MAX;
        const echoDominant =
            playbackActive
            && !modeledHumanCandidate
            && (
                match.residual <= ECHO_RESIDUAL_RATIO_MAX
                || prediction.residualRatio <= ECHO_RESIDUAL_RATIO_MAX
                || match.correlation >= 0.55
            );
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
                this._echoModelReady
                || this._echoEvidenceSamples >= Math.round((COUPLED_EVIDENCE_MS / 1000) * ANALYSIS_RATE)
            )) {
                this._acousticState = 'coupled';
            } else if (
                this._acousticState === 'unknown'
                && this._cleanPlaybackSamples >= Math.round((CLEAN_PLAYBACK_MS / 1000) * ANALYSIS_RATE)
            ) {
                this._acousticState = 'uncoupled';
            }
        }

        const humanCandidate = modeledHumanCandidate || (
            independentCandidate && this._acousticState !== 'coupled'
        );
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

        // Decide what to release. Everything is in terms of ORIGINAL capture samples (`original`), never residuals.
        const pendingBefore = this._pendingSamples;
        let framesToSend = [original];
        let suppressEcho = this._echoMode === 'strict' ? playbackActive : false;
        const holdFrame = () => { this._pendingHumanChunks.push(original); this._pendingSamples += original.length; };
        const discardPending = () => { this._pendingHumanChunks = []; this._pendingSamples = 0; };
        if (this._echoMode === 'strict' && playbackActive) {
            framesToSend = [];
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
                this._doubleTalkSamples += n;
                this._doubleTalkGapSamples = 0;
                const confirmed =
                    this._doubleTalkSamples >= Math.round((DOUBLE_TALK_MS / 1000) * ANALYSIS_RATE);
                if (confirmed) {
                    this._doubleTalkActive = true;
                    if (this._candidatePath === 'independent') this._acousticState = 'uncoupled';
                    framesToSend = this._pendingHumanChunks;
                    discardPending();
                    suppressEcho = false;
                } else {
                    framesToSend = [];
                    suppressEcho = true;
                }
            } else if (humanCandidate && this._doubleTalkActive) {
                this._doubleTalkGapSamples = 0;
                framesToSend = [original];
                suppressEcho = false;
            } else if (this._pendingHumanChunks.length && !this._doubleTalkActive) {
                // Natural speech has short low-energy consonants and inter-syllable gaps. Keep the onset local across a
                // small ambiguous gap rather than requiring 450 ms of perfectly uniform frames.
                this._doubleTalkGapSamples += n;
                if (this._doubleTalkGapSamples <= Math.round((DOUBLE_TALK_GAP_MS / 1000) * ANALYSIS_RATE)) {
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
        if (suppressEcho) this._suppressedMs += (n / ANALYSIS_RATE) * 1000;

        const releasedSamples = framesToSend.reduce((total, frame) => total + frame.length, 0);
        const dropped = Math.max(0, pendingBefore + original.length - releasedSamples - this._pendingSamples);

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
                suppressedMs: this._suppressedMs,
                suppressing: suppressEcho,
                doubleTalk: this._doubleTalkActive,
                playbackActive,
            };
        }
        const state = this._frames % SNAPSHOT_FRAMES === 0 && this._echoModelSamples > 0 ? this.exportState() : null;
        return { released: framesToSend, dropped, floor: this.voicedFloor, rms, metric, state, playbackActive };
    }
}
