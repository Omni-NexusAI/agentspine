// AudioSession capture wiring with the browser globals stubbed: which worklet loads, how the graph is connected, what the
// messages do. The graph is a recording stub, so this proves the wiring, not how it sounds.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { AudioSession } from '../webui/audio.js';

async function withBrowser(settings, run) {
    const saved = { navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator'), AudioContext: globalThis.AudioContext, AudioWorkletNode: globalThis.AudioWorkletNode };
    const log = { modules: [], nodes: [], connects: [], stopped: 0, gains: 0 };
    class Node { constructor(name) { this.name = name; } connect(target, output = 0, input = 0) { log.connects.push([this.name, target.name, output, input]); return target; } disconnect() {} }
    class Gain extends Node { constructor(name) { super(name); this.gain = { value: 1 }; } }
    globalThis.AudioContext = class {
        constructor() { this.sampleRate = 48000; this.currentTime = 0; this.state = 'running'; this.destination = new Node('destination'); this.audioWorklet = { addModule: async url => { log.modules.push(url); } }; }
        createGain() { return new Gain('gain' + (++log.gains)); }
        createMediaStreamSource() { return new Node('source'); }
        async resume() {} async close() { this.state = 'closed'; }
    };
    globalThis.AudioWorkletNode = class extends Node {
        constructor(context, name, options) { super('capture'); this.workletName = name; this.options = options; this.args = arguments.length; this.port = { posted: [], postMessage(message) { this.posted.push(message); } }; log.nodes.push(this); }
    };
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: { getUserMedia: async () => ({
        getTracks: () => [{ stop: () => log.stopped++ }], getAudioTracks: () => [{ getSettings: () => settings }] }) } } });
    try { await run(log); } finally {
        if (saved.navigator) Object.defineProperty(globalThis, 'navigator', saved.navigator); else delete globalThis.navigator;
        for (const key of ['AudioContext', 'AudioWorkletNode']) { if (saved[key] === undefined) delete globalThis[key]; else globalThis[key] = saved[key]; }
    }
}

test('echo_guard adaptive/strict: the guard worklet gets two inputs, the post-duck output as reference and the live nativeAec', async () => {
    for (const [mode, aec] of [['adaptive', true], ['strict', false]]) await withBrowser({ echoCancellation: aec }, async log => {
        const starts = [], levels = [];
        const audio = new AudioSession({ echoGuard: mode, maximum: 20, onStart: () => starts.push(1), onTurn: () => {}, onLevel: (kind, value) => levels.push([kind, value]) });
        await audio.start();
        assert.deepEqual(log.modules, ['/plugins/_convo/webui/audio-worklet-guard.js']);
        const [node] = log.nodes;
        assert.equal(node.workletName, 'convo-capture-guard');
        assert.equal(node.options.numberOfInputs, 2);
        assert.deepEqual(node.options.processorOptions, { mode, nativeAec: aec, state: null });
        assert.ok(log.connects.some(([from, to, output, input]) => from === 'gain1' && to === 'capture' && output === 0 && input === 1), 'outputGain (post-duck) feeds input 1');
        assert.ok(log.connects.some(([from, to, , input]) => from === 'source' && to === 'capture' && input === 0));
        // Messages: voiced original frames admit; state and metrics are kept, not treated as audio.
        const frame = new Float32Array(1920).fill(.05);
        for (let n = 0; n < 12; n++) node.port.onmessage({ data: { kind: 'audio', frames: [frame], dropped: 0, floor: .004 } });
        assert.equal(starts.length, 1, 'about 450 ms of released voiced frames admits the candidate');
        node.port.onmessage({ data: { kind: 'echo_state', state: { inputRate: 48000, filter: new Float32Array(256) } } });
        node.port.onmessage({ data: { kind: 'echo_metric', mode, suppressing: true } });
        assert.equal(audio.echoSnapshot().inputRate, 48000); assert.equal(audio.echoMetric.suppressing, true);
        assert.ok(levels.some(([kind, value]) => kind === 'input' && value > 0));
        assert.equal(audio.setEchoGuard(mode === 'strict' ? 'adaptive' : 'strict'), true);
        assert.equal(node.port.posted.at(-1).kind, 'echo_guard'); assert.equal(node.port.posted.at(-1).nativeAec, aec);
        audio.close(); assert.equal(log.stopped, 1);
    });
});

test('Withheld echo reaches the boundary only as silence: a quiet gap after speech still ends the turn', async () => {
    await withBrowser({ echoCancellation: true }, async log => {
        const turns = [];
        const audio = new AudioSession({ echoGuard: 'adaptive', maximum: 20, onStart: () => {}, onTurn: v => turns.push(v) });
        await audio.start();
        const [node] = log.nodes, frame = new Float32Array(1920).fill(.05);
        for (let n = 0; n < 14; n++) node.port.onmessage({ data: { kind: 'audio', frames: [frame], dropped: 0, floor: .004 } });
        for (let n = 0; n < 24; n++) node.port.onmessage({ data: { kind: 'audio', frames: [], dropped: 1920, floor: .004 } });   // 960 ms of echo that was held back
        assert.equal(turns.length, 1, 'the turn ends on the silence standing in for the withheld echo');
        audio.close();
    });
});

test('echo_guard off keeps the previous capture path: the legacy worklet, no options, no reference tap, same message handling', async () => {
    await withBrowser({ echoCancellation: true }, async log => {
        const turns = [];
        const audio = new AudioSession({ echoGuard: 'off', maximum: 20, onStart: () => {}, onTurn: v => turns.push(v) });
        await audio.start();
        assert.deepEqual(log.modules, ['/plugins/_convo/webui/audio-worklet.js']);
        const [node] = log.nodes;
        assert.equal(node.workletName, 'convo-capture'); assert.equal(node.args, 2, 'constructed without options, as before');
        assert.equal(log.connects.filter(([, to]) => to === 'capture').length, 1, 'only the microphone feeds it');
        assert.equal(audio.nativeAec(), true);
        for (let n = 0; n < 12; n++) node.port.onmessage({ data: new Float32Array(2048).fill(.1) });
        assert.equal(audio.boundary.admitted, true);
        assert.equal(audio.setEchoGuard('adaptive'), false, 'off is a different capture path and needs a restart');
        audio.close();
    });
    assert.equal(new AudioSession({ echoGuard: 'nonsense' }).echoGuard, 'adaptive');
    assert.equal(new AudioSession({}).echoGuard, 'adaptive');
});

test('A recreated session starts from the previous echo path', async () => {
    await withBrowser({ echoCancellation: true }, async log => {
        const first = new AudioSession({ echoGuard: 'adaptive', onStart: () => {}, onTurn: () => {} });
        await first.start();
        log.nodes[0].port.onmessage({ data: { kind: 'echo_state', state: { inputRate: 48000, filter: new Float32Array(256).fill(.01) } } });
        const state = first.echoSnapshot();
        first.close();                                                       // retarget closes the old session first
        const second = new AudioSession({ echoGuard: 'adaptive', echoState: state, onStart: () => {}, onTurn: () => {} });
        await second.start();
        assert.equal(log.nodes[1].options.processorOptions.state, state);
        second.close();
    });
});
