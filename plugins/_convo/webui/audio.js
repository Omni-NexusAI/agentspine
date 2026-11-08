export function wavBase64(chunks, sourceRate, targetRate = 16000) {
    const count = chunks.reduce((n, c) => n + c.length, 0);
    const joined = new Float32Array(count);
    let offset = 0;
    for (const chunk of chunks) { joined.set(chunk, offset); offset += chunk.length; }
    const frames = Math.floor(count * targetRate / sourceRate);
    const bytes = new Uint8Array(44 + frames * 2);
    const view = new DataView(bytes.buffer);
    const tag = (start, text) => [...text].forEach((c, i) => view.setUint8(start + i, c.charCodeAt(0)));
    tag(0, 'RIFF'); view.setUint32(4, bytes.length - 8, true); tag(8, 'WAVE'); tag(12, 'fmt ');
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, targetRate, true); view.setUint32(28, targetRate * 2, true);
    view.setUint16(32, 2, true); view.setUint16(34, 16, true); tag(36, 'data'); view.setUint32(40, frames * 2, true);
    for (let i = 0; i < frames; i++) {
        const begin = Math.floor(i * sourceRate / targetRate);
        const end = Math.min(count, Math.max(begin + 1, Math.floor((i + 1) * sourceRate / targetRate)));
        let value = 0;
        for (let j = begin; j < end; j++) value += joined[j];
        value = Math.max(-1, Math.min(1, value / (end - begin)));
        view.setInt16(44 + i * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true);
    }
    let binary = '';
    for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
    return btoa(binary);
}

// A block is voiced above this RMS unless the capture supplies its own floor (the echo guard's measured noise floor).
export const VOICED_RMS = .02;
// How much voiced audio a candidate needs before it is admitted (onStart). With the echo guard the guard confirms the same
// 450 ms of independent speech first and the released onset then admits at once, so the two do not stack. A one-word
// interruption shorter than this ("stop", ~0.3 s) is not detected; lowering it admits more noise, so it is a constant here and is
// only to be changed together with replay measurements (tests/echo-measure.mjs), never silently.
export const MIN_VOICED_SECONDS = .45;
// Audio kept from before the first voiced block so a soft onset reaches the model. 0 = off (the previous behaviour).
export const PRE_ROLL_SECONDS = 0;

export class SpeechBoundary {
    // `clock` only timestamps what happened (measurement); it never decides anything.
    constructor({ rate, maximum = 20, onStart, onTurn, onProbe = () => {}, clock = () => globalThis.performance?.now?.() ?? Date.now(),
        minVoiced = MIN_VOICED_SECONDS, preRoll = PRE_ROLL_SECONDS }) {
        Object.assign(this, { rate, maximum, onStart, onTurn, onProbe, clock, minVoiced, preRoll });
        // The longest turn, counted in whole samples so the cut is exact (see push). Not a number: never cut by length.
        const limit = Math.floor(maximum * rate);
        this.limit = Number.isFinite(limit) ? Math.max(1, limit) : Infinity;
        this.lastTurn = null;
        this.reset();
    }
    reset() { this.chunks = []; this.samples = 0; this.duration = 0; this.silence = 0; this.speech = 0; this.admitted = false; this.lastProbe = 0; this.onsetAt = 0; this.voicedAt = 0; this.ring = []; this.ringDuration = 0; }
    push(chunk, floor = VOICED_RMS) {
        // The worklet delivers whole blocks, so the maximum-length cut usually falls inside one. Cutting on the block
        // edge would upload a turn longer than the maximum (up to a block more at 44.1/48 kHz) and the server rejects
        // it, so the turn ends on the exact sample and the rest of the block starts the next utterance.
        const rms = Math.sqrt(chunk.reduce((n, v) => n + v * v, 0) / chunk.length);
        const voiced = rms > floor;                                // decided on the whole block, as delivered
        while (chunk.length) {
            const room = this.limit - this.samples;
            if (chunk.length <= room) return this.take(chunk, voiced);
            this.take(chunk.subarray(0, room), voiced);
            chunk = chunk.subarray(room);
        }
    }
    take(chunk, voiced) {
        const duration = chunk.length / this.rate;
        if (!this.chunks.length && !voiced) { this.remember(chunk); return; }
        if (!this.chunks.length) { this.onsetAt = this.clock(); this.prepend(chunk.length); }
        this.chunks.push(chunk);
        if (voiced) this.voicedAt = this.clock();
        this.samples += chunk.length;
        this.duration += duration;
        this.silence = voiced ? 0 : this.silence + duration;
        this.speech = voiced ? this.speech + duration : this.speech;
        if (!this.admitted && this.speech >= this.minVoiced) {
            this.admitted = true;
            this.onStart();
        }
        if (this.admitted && this.duration - this.lastProbe >= .75 && this.samples < this.limit && this.silence < .9) {
            this.lastProbe = this.duration;
            this.onProbe(this.chunks.slice()); // One bounded completed WAV snapshot, not a user turn.
        }
        if (this.samples >= this.limit || this.silence >= .9) {
            const chunks = this.chunks;
            const admitted = this.admitted;
            this.lastTurn = { onsetAt: this.onsetAt, voicedAt: this.voicedAt, endedAt: this.clock(), duration: this.duration, cut: this.samples >= this.limit };
            this.reset();
            if (admitted) this.onTurn(chunks);
        }
    }
    // Optional pre-roll: the most recent not-yet-voiced blocks (whole blocks, at least `preRoll` seconds when available).
    remember(chunk) {
        if (!(this.preRoll > 0)) return;
        this.ring.push(chunk); this.ringDuration += chunk.length / this.rate;
        while (this.ring.length > 1 && this.ringDuration - this.ring[0].length / this.rate >= this.preRoll) this.ringDuration -= this.ring.shift().length / this.rate;
    }
    prepend(next) {
        if (!this.ring.length) return;
        // The pre-roll counts toward the turn length, so it must leave room for the block that opens the turn.
        while (this.ring.length && this.ring.reduce((n, c) => n + c.length, 0) + next > this.limit - this.samples) this.ring.shift();
        for (const old of this.ring) { this.chunks.push(old); this.samples += old.length; this.duration += old.length / this.rate; }
        this.lastProbe = this.duration;                              // probes keep their cadence from the onset, not from the pre-roll
        this.ring = []; this.ringDuration = 0;
    }
}

// Applies one capture-worklet message to a SpeechBoundary and returns the input level to show. A plain Float32Array is the
// legacy capture block. With the echo guard a message carries only original microphone samples the guard has released, plus
// `dropped`: samples it withheld for good (echo / ambiguous). Those are time passing as silence and are never the echo itself.
export function feedBoundary(boundary, data) {
    const rmsOf = block => Math.sqrt(block.reduce((sum, value) => sum + value * value, 0) / Math.max(1, block.length));
    if (data?.kind !== 'audio') { boundary.push(data); return rmsOf(data); }
    let level = 0;
    if (data.dropped > 0) boundary.push(new Float32Array(data.dropped), data.floor);
    for (const frame of data.frames) { level = Math.max(level, rmsOf(frame)); boundary.push(frame, data.floor); }
    return level;
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
        this.ducked = false; this.engagedAt = 0; this.lastHeldAt = 0; this.toggles = 0; this.releaseTimer = null; this.maxTimer = null;
    }
    request(held, immediate = false) {
        const now = this.clock();
        if (held) {
            this.lastHeldAt = now;
            this.cancel(this.releaseTimer); this.releaseTimer = null;
            this.cancel(this.maxTimer); this.maxTimer = this.schedule(() => { this.maxTimer = null; this.release(); }, this.maxHoldMs);
            this.maxTimer?.unref?.();
            if (!this.ducked) { this.ducked = true; this.engagedAt = now; this.toggles++; this.apply(this.level, this.attackMs); }
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

export class AudioSession {
    // echoGuard: 'adaptive' | 'strict' | 'off' (anything else = adaptive). echoState: the learned echo path of a previous session
    // (see echoSnapshot()), so a recreated session (retarget) keeps it. duck: overrides of DUCK, for tests.
    constructor({ onStart, onTurn, onProbe = () => {}, onError, maximum, deviceId, onLevel = () => {}, echoGuard = 'adaptive', echoState = null, onEchoMetric = () => {}, duck = {} }) {
        Object.assign(this, { onStart, onTurn, onProbe, onError, maximum, deviceId, onLevel, echoState, onEchoMetric });
        this.echoGuard = ['adaptive', 'strict', 'off'].includes(echoGuard) ? echoGuard : 'adaptive';
        this.duck = new DuckController({ apply: (target, rampMs) => this.rampOutput(target, rampMs), ...duck });
        this.closed = false;
        this.queue = [];
        this.playing = null;
        this.playbackGeneration = 0;
        this.pcm = new Map(); this.pcmSources = new Set(); this.pcmNext = 0;
    }
    emitLevel(kind, value) { try { this.onLevel(kind, value); } catch {} }
    async start() {
        try {
            this.stream = await navigator.mediaDevices.getUserMedia({ audio: {
                deviceId: this.deviceId ? { exact: this.deviceId } : undefined,
                channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
            }});
            if (this.closed) return this.close();
            this.context = new AudioContext();
            this.outputGain = this.context.createGain(); this.outputGain.connect(this.context.destination);
            await this.context.resume();
            // echo_guard 'off' keeps the previous capture (its own, untouched worklet file); otherwise the guard worklet is used.
            const guarded = this.echoGuard !== 'off';
            await this.context.audioWorklet.addModule(guarded ? '/plugins/_convo/webui/audio-worklet-guard.js' : '/plugins/_convo/webui/audio-worklet.js');
            if (this.closed) return this.close();
            this.boundary = new SpeechBoundary({ rate: this.context.sampleRate, maximum: this.maximum,
                onStart: this.onStart, onTurn: chunks => this.onTurn(wavBase64(chunks, this.context.sampleRate)),
                onProbe: chunks => this.onProbe(wavBase64(chunks, this.context.sampleRate)) });
            this.source = this.context.createMediaStreamSource(this.stream);
            this.sink = this.context.createGain(); this.sink.gain.value = 0;
            if (guarded) {
                // Input 0: the microphone. Input 1: the exact post-duck playback output, the reference the guard compares against.
                this.capture = new AudioWorkletNode(this.context, 'convo-capture-guard', { numberOfInputs: 2, numberOfOutputs: 1,
                    processorOptions: { mode: this.echoGuard, nativeAec: this.nativeAec(), state: this.echoState } });
                this.source.connect(this.capture);
                this.outputGain.connect(this.capture, 0, 1);
                this.capture.connect(this.sink).connect(this.context.destination);
                this.capture.port.onmessage = event => {
                    if (this.closed) return;
                    const data = event.data;
                    if (data?.kind === 'echo_metric') { this.echoMetric = data; try { this.onEchoMetric(data); } catch {} return; }
                    if (data?.kind === 'echo_state') { this.echoState = data.state; return; }
                    this.emitLevel('input', Math.min(1, feedBoundary(this.boundary, data) * 8)); // Visuals cannot gate capture.
                };
            } else {
                this.capture = new AudioWorkletNode(this.context, 'convo-capture');
                this.source.connect(this.capture).connect(this.sink).connect(this.context.destination);
                this.capture.port.onmessage = event => {
                    if (this.closed) return;
                    const data = event.data;
                    const rms = Math.sqrt(data.reduce((sum, value) => sum + value * value, 0) / Math.max(1, data.length));
                    this.emitLevel('input', Math.min(1, rms * 8)); // Visuals cannot gate capture.
                    this.boundary.push(data);
                };
            }
        } catch (error) { this.close(); throw error; }
    }
    // Whether the browser says its own echo cancellation is on for this microphone (read from the live track's settings).
    nativeAec() { return !!this.stream?.getAudioTracks?.()[0]?.getSettings?.().echoCancellation; }
    // The learned echo path to hand to the next AudioSession (retarget recreates the session; the room has not changed).
    echoSnapshot() { return this.echoState; }
    // Mode change between adaptive and strict while running ('off' needs a restart: it is a different capture path).
    setEchoGuard(mode) {
        if (!['adaptive', 'strict'].includes(mode) || this.echoGuard === 'off') return false;
        this.echoGuard = mode; this.capture?.port.postMessage({ kind: 'echo_guard', mode, nativeAec: this.nativeAec() }); return true;
    }
    interrupt() {
        this.playbackGeneration++;
        for (const phrase of this.pcm.values()) clearTimeout(phrase.timer);
        this.pcm.clear(); this.pcmNext = 0;
        for (const source of this.pcmSources) { source.onended = null; try { source.stop(); } catch {} source.disconnect(); }
        this.pcmSources.clear();
        this.queue = [];
        const playing = this.playing; this.playing = null;
        if (playing) { playing.onended = null; try { playing.stop(); } catch {} playing.disconnect(); }
        if (this.levelTimer) clearInterval(this.levelTimer);
        this.levelTimer = null; this.outputMeter?.disconnect(); this.outputMeter = null;
        this.emitLevel('output', 0);
    }
    setPolicyHold(held, immediate = false) {
        // Uncertainty ducks the existing output without cancelling its generation,
        // clearing playback, or pretending an interrupted phrase was delivered. The duck ramps and has hysteresis (DuckController);
        // `immediate` is for a decision that has already ended (a new reply must never be born ducked).
        this.duck.request(held, immediate);
    }
    rampOutput(target, rampMs) {
        const gain = this.outputGain?.gain;
        if (!gain) return;
        // setTargetAtTime continues from the value the previous ramp has reached, so quick reversals do not jump.
        if (typeof gain.setTargetAtTime === 'function' && this.context) gain.setTargetAtTime(target, this.context.currentTime, rampMs / 3000);
        else gain.value = target;
    }
    pcmFail(message) { this.interrupt(); this.onError?.(new Error(message)); return false; }
    pcmBegin(event, completed, started = () => {}) {
        if (this.closed) return false;
        if (!this.context || this.playing || this.decoding || this.queue.length || this.pcm.size >= 24 ||
            !['session_id','turn_id','phrase_id'].every(key => typeof event[key] === 'string' && event[key].length > 0 && event[key].length <= 256) ||
            !Number.isSafeInteger(event.epoch) || event.epoch < 0 || this.pcm.has(event.phrase_id) ||
            event.format !== 'pcm_s16le' || event.channels !== 1 ||
            ![8000,16000,22050,24000,32000,44100,48000].includes(event.sample_rate) ||
            [...this.pcm.values()].some(phrase => !phrase.ended)) return this.pcmFail('Invalid or overlapping PCM phrase');
        this.pcm.set(event.phrase_id, { ...event, completed, started, sequence: 0, bytes: 0, pending: 0,
            ended: false, didStart: false, generation: this.playbackGeneration, timer: null });
        return true;
    }
    pcmPhrase(event) {
        const phrase = this.pcm.get(event.phrase_id);
        return phrase && ['session_id','epoch','turn_id'].every(key => phrase[key] === event[key]) ? phrase : null;
    }
    pcmChunk(event) {
        if (this.closed) return false;
        const phrase = this.pcmPhrase(event);
        if (!phrase || phrase.ended || !Number.isSafeInteger(event.sequence) || event.sequence !== phrase.sequence ||
            typeof event.audio !== 'string' || !event.audio.length || event.audio.length > 43692 ||
            event.audio.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(event.audio)) return this.pcmFail('Missing or reordered PCM chunk');
        try {
            const bytes = Uint8Array.from(atob(event.audio), c => c.charCodeAt(0));
            if (!bytes.length || bytes.length % 2 || bytes.length > 32768 || phrase.bytes + bytes.length > 16 * 1024 * 1024 ||
                this.pcmSources.size >= 256) return this.pcmFail('PCM playback exceeded its bounded queue');
            const when = Math.max(this.context.currentTime + .02, this.pcmNext);
            const duration = bytes.length / 2 / phrase.sample_rate;
            if (when + duration - this.context.currentTime > 12) return this.pcmFail('PCM playback fell behind; audio discarded');
            const buffer = this.context.createBuffer(1, bytes.length / 2, phrase.sample_rate);
            const values = buffer.getChannelData(0), view = new DataView(bytes.buffer);
            for (let i = 0; i < values.length; i++) values[i] = view.getInt16(i * 2, true) / 32768;
            const source = this.context.createBufferSource(); source.buffer = buffer;
            if (!this.outputMeter && this.context.createAnalyser) {
                this.outputMeter = this.context.createAnalyser(); this.outputMeter.fftSize = 256;
                this.outputMeter.connect(this.outputGain || this.context.destination);
                const levels = new Float32Array(256);
                this.levelTimer = setInterval(() => {
                    this.outputMeter?.getFloatTimeDomainData(levels);
                    this.emitLevel('output', Math.min(1, Math.sqrt(levels.reduce((n,v) => n + v*v, 0) / levels.length) * 6));
                }, 50);
            }
            source.connect(this.outputMeter || this.outputGain || this.context.destination);
            const markStarted = () => {
                if (this.closed || phrase.generation !== this.playbackGeneration || !this.pcm.has(phrase.phrase_id) || phrase.didStart) return;
                if (this.context.currentTime < when || this.context.state === 'suspended') {
                    phrase.timer = setTimeout(markStarted, 20); return;
                }
                phrase.didStart = true; phrase.started();
            };
            if (phrase.sequence === 0) phrase.timer = setTimeout(markStarted, Math.max(0, (when - this.context.currentTime) * 1000));
            phrase.sequence++; phrase.bytes += bytes.length; phrase.pending++;
            this.pcmSources.add(source); this.pcmNext = when + duration;
            source.onended = () => {
                source.disconnect(); this.pcmSources.delete(source);
                if (phrase.generation !== this.playbackGeneration || this.closed) return;
                markStarted(); phrase.pending--; this.pcmComplete(phrase);
            };
            source.start(when);
            return true;
        } catch (error) { return this.pcmFail(error.message || 'Invalid PCM audio'); }
    }
    pcmEnd(event) {
        if (this.closed) return false;
        const phrase = this.pcmPhrase(event);
        if (!phrase || phrase.ended || !Number.isSafeInteger(event.chunks) || event.chunks !== phrase.sequence || !phrase.bytes)
            return this.pcmFail('Incomplete PCM phrase');
        phrase.ended = true; this.pcmComplete(phrase); return true;
    }
    pcmAbort(event) {
        if (this.pcmPhrase(event)) this.interrupt();
    }
    pcmComplete(phrase) {
        if (!phrase.ended || phrase.pending || !phrase.didStart || this.closed || phrase.generation !== this.playbackGeneration) return;
        clearTimeout(phrase.timer); this.pcm.delete(phrase.phrase_id); phrase.completed();
        if (!this.pcm.size) {
            if (this.levelTimer) clearInterval(this.levelTimer);
            this.levelTimer = null; this.outputMeter?.disconnect(); this.outputMeter = null;
            this.emitLevel('output', 0); void this.drain();
        }
    }
    enqueue(base64, completed, started = () => {}) {
        if (this.closed) return;
        if (this.queue.length >= 24) { this.interrupt(); this.onError(new Error('Playback fell behind; queued audio was discarded.')); return; }
        this.queue.push({ base64, completed, started, generation: this.playbackGeneration });
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
                source.connect(this.outputMeter).connect(this.outputGain || this.context.destination);
                const values = new Float32Array(256);
                this.levelTimer = setInterval(() => {
                    this.outputMeter?.getFloatTimeDomainData(values);
                    const rms = Math.sqrt(values.reduce((sum, value) => sum + value * value, 0) / values.length);
                    this.emitLevel('output', Math.min(1, rms * 6));
                }, 50);
            } else source.connect(this.outputGain || this.context.destination);
            this.playing = source;
            source.onended = () => {
                source.disconnect();
                if (this.levelTimer) clearInterval(this.levelTimer);
                this.levelTimer = null; this.outputMeter?.disconnect(); this.outputMeter = null;
                this.emitLevel('output', 0);
                if (this.playing === source) this.playing = null;
                if (!this.closed && item.generation === this.playbackGeneration) item.completed();
                void this.drain();
            };
            source.start();
            item.started();
        } catch (error) { this.interrupt(); this.onError(error); }
        finally { this.decoding = false; if (!this.playing && !this.closed) void this.drain(); }
    }
    close() {
        this.closed = true; this.duck.dispose(); this.interrupt();
        this.emitLevel('input', 0);
        this.stream?.getTracks().forEach(track => track.stop());
        this.source?.disconnect(); this.capture?.disconnect(); this.sink?.disconnect();
        this.outputGain?.disconnect();
        if (this.context && this.context.state !== 'closed') void this.context.close();
    }
}
