// Capture with the playback-reference echo guard (echo_guard = adaptive | strict). Input 0 is the microphone, input 1 is
// the post-duck playback output tapped from the output gain. The legacy `audio-worklet.js` (echo_guard = off) is a separate,
// untouched file, so turning the guard off keeps exactly the previous capture path.
//
// Messages out (main thread): { kind: 'audio', frames: Float32Array[], dropped, floor } with ORIGINAL microphone samples
// at the context rate (never predictor residuals), { kind: 'echo_metric', ... } and { kind: 'echo_state', state }.
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
            else if (data?.kind === 'echo_reset') this.guard.reset();
            else if (data?.kind === 'echo_restore') this.guard.importState(data.state);
        };
    }
    process(inputs) {
        const mic = inputs[0]?.[0];
        if (!mic || !mic.length) return true;
        for (const result of this.guard.ingest(mic, inputs[1]?.[0] || null)) {
            if (result.released.length || result.dropped) {
                this.port.postMessage({ kind: 'audio', frames: result.released, dropped: result.dropped, floor: result.floor },
                    result.released.map(frame => frame.buffer));
            }
            if (result.metric) this.port.postMessage(result.metric);
            if (result.state) this.port.postMessage({ kind: 'echo_state', state: result.state });
        }
        return true;
    }
}
registerProcessor('convo-capture-guard', ConvoCaptureGuard);
