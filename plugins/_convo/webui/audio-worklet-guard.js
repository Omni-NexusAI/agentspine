// Capture with the playback-reference echo guard (echo_guard = adaptive | strict). Input 0 is the microphone, input 1 is
// the post-duck playback output tapped from the output gain. The legacy `audio-worklet.js` (echo_guard = off) is a separate,
// untouched file, so turning the guard off keeps exactly the previous capture path.
//
// Messages out (main thread), one 'audio' message per 40 ms analysis frame: { kind: 'audio', frames: Float32Array[], voiced:
// boolean[], levels: number[], comfort: boolean[], dropped, floor, level, withheld, voicedNow, acousticState, playback } with ORIGINAL
// microphone samples at the context rate (never predictor residuals) or, where `comfort` is true, floor-level comfort noise standing in
// for withheld time while the user talks over the reply; each frame's voicing verdict and raw level, the raw level of the newest frame
// (the meter), whether audible input was withheld and whether playback (or its tail) is active; { kind: 'echo_metric', ... } and
// { kind: 'echo_state', state }.
// Messages in: { kind: 'echo_guard', mode, nativeAec }, { kind: 'sound', gateDb } (the noise gate threshold voicing must reach),
// { kind: 'echo_reset' }, { kind: 'echo_restore', state }.
import { EchoGuard } from './echo-guard.js';

class ConvoCaptureGuard extends AudioWorkletProcessor {
    constructor(options) {
        super();
        const config = options?.processorOptions ?? {};
        this.guard = new EchoGuard({ inputRate: sampleRate, mode: config.mode, nativeAec: config.nativeAec });
        if (config.state) this.guard.importState(config.state);
        this.port.onmessage = event => {
            const data = event.data;
            if (data?.kind === 'echo_guard') this.guard.configure(data);
            else if (data?.kind === 'sound') this.guard.setGate(data.gateDb);
            else if (data?.kind === 'echo_reset') this.guard.reset();
            else if (data?.kind === 'echo_restore') this.guard.importState(data.state);
        };
    }
    process(inputs) {
        const mic = inputs[0]?.[0];
        if (!mic || !mic.length) return true;
        for (const result of this.guard.ingest(mic, inputs[1]?.[0] || null)) {
            // Each frame's own buffer, once (a held onset's frames are separate arrays; a crossfaded frame is a copy).
            const buffers = [...new Set(result.frames.map(frame => frame.buffer))];
            this.port.postMessage({ kind: 'audio', frames: result.frames, voiced: result.voiced, levels: result.levels, comfort: result.comfort, dropped: result.dropped,
                floor: result.floor, level: result.level, withheld: result.withheld, voicedNow: result.voicedNow, acousticState: result.acousticState,
                playback: result.playbackActive }, buffers);
            if (result.metric) this.port.postMessage(result.metric);
            if (result.state) this.port.postMessage({ kind: 'echo_state', state: result.state });
        }
        return true;
    }
}
registerProcessor('convo-capture-guard', ConvoCaptureGuard);
