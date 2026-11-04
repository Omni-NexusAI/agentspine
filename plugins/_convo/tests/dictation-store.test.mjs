// The orb gesture against the real convo-store.js (through dictation.js's DOM adapter), and the host recorder's held mode.
// Real pointer / key / focus events, a virtual clock for every timer, and a recording stand-in for the host microphone.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fakeMic } from './store-fixture.mjs';
import { hostStubs, loadModule } from './vm-loader.mjs';
import { resetAnnouncer } from '../webui/orb.js';
import { __test as recorderTest } from '../extensions/webui/initFw_end/enhanced-stt-recorder.js';

const flush = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };

async function rigStore({ settings = {}, init = null, finish = 'delivered', manualSupported = true, failTimes = 0 } = {}) {
    let failuresLeft = failTimes;
    let now = 0, next = 1;
    const timers = new Map();
    const clock = {
        set: (fn, ms) => { const id = next++; timers.set(id, { at: now + ms, fn }); return id; },
        clear: id => timers.delete(id),
        async advance(ms) {
            const target = now + ms;
            for (;;) {
                const due = [...timers].filter(([, t]) => t.at <= target).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
                if (!due) break;
                timers.delete(due[0]); now = due[1].at; due[1].fn();
                await flush();
            }
            now = target; await flush();
        },
    };
    const mic = fakeMic({ 'aria-label': 'Microphone standby', 'aria-pressed': 'false' });
    const log = [], cueNames = [], audios = [], listeners = new Map();
    const live = { children: [], appendChild(child) { this.children.push(child); } };
    const announcements = [];
    const liveElement = () => ({ attrs: new Map(), style: {}, isConnected: true, setAttribute(k, v) { this.attrs.set(k, v); }, remove() { this.isConnected = false; },
        get textContent() { return this.text ?? ''; }, set textContent(value) { this.text = value; if (value) announcements.push(value); } });
    const recorders = [];
    const nativeStore = {
        microphoneInput: null, getSelectedDevice: () => ({ deviceId: 'mic' }), async initRuntime() {},
        async initMicrophone() {
            if (this.microphoneInput) return this.microphoneInput;
            if (typeof init === 'function') await init();
            if (init === 'fail' || failuresLeft-- > 0) return null;
            const recorder = {
                disposed: false, manualHistory: [],
                async toggle() { log.push('recorder.toggle'); },
                dispose() { this.disposed = true; log.push('recorder.dispose'); },
                setManual(value) { this.manualHistory.push(value); },
                async finishConvoDictation() { log.push('recorder.finish'); return typeof finish === 'function' ? finish() : finish; },
                discardConvoDictation() { log.push('recorder.discard'); return 'cancelled'; },
            };
            if (manualSupported) recorder.startManual = async function () { log.push('recorder.startManual'); };
            recorders.push(recorder);
            log.push('recorder.open');
            this.microphoneInput = recorder;
            return recorder;
        },
    };
    const document = {
        hidden: false, body: live, createElement: liveElement,
        addEventListener: (type, fn) => { listeners.set(type, [...(listeners.get(type) || []), fn]); },
        removeEventListener() {},
        getElementById: id => id === 'microphone-button' ? mic : null,
    };
    const window = { confirm: () => true, addEventListener: (type, fn) => { listeners.set('window:' + type, [...(listeners.get('window:' + type) || []), fn]); } };
    const bindings = hostStubs({
        '/js/api.js': { callJsonApi: async (_url, payload) => payload.action === 'status' ? { settings: { enabled: true, max_audio_seconds: 20, ...settings }, runtime: { mode: 'external' } } : { events: [], jobs: [] } },
        '/js/websocket.js': { createNamespacedClient: () => ({ addHandlers() {}, async on() {}, onDisconnect() {}, disconnect() { log.push('socket.close'); },
            async request(event) { log.push(event); return { results: [{ handlerId: 'plugin.Realtime', ok: true, data: { ok: true, session: { id: 's', epoch: 0 } } }] }; } }) },
        '/js/shortcuts.js': { getCurrentContextId: () => 'a', getCurrentContext: () => ({ id: 'a', name: 'Chat a' }), openModal: async () => {} },
        './audio.js': { AudioSession: class {
            constructor(options) { Object.assign(this, options); audios.push(this); }
            async start() { log.push('capture.start'); } interrupt() {} setPolicyHold() {} close() { this.closed = true; log.push('capture.close'); }
            pcmBegin() {} pcmChunk() {} pcmEnd() {} pcmAbort() {} enqueue() {}
        } },
        '/plugins/_whisper_stt/webui/whisper-stt-store.js': { store: nativeStore },
    });
    const globals = { document, window, setTimeout: clock.set, clearTimeout: clock.clear, setInterval: () => 0, clearInterval() {}, fetch: async () => ({ ok: true }),
        dispatchEvent: event => cueNames.push(event.detail.name), CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } } };
    const savedDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
    Object.defineProperty(globalThis, 'document', { configurable: true, value: document });   // the live region is written by the real orb.js
    resetAnnouncer();
    delete globalThis.__convoInstalled;
    const { namespace } = await loadModule('webui/convo-store.js', { bindings, globals });
    namespace.install();
    await flush();
    const orb = { id: 'microphone-button' };
    const event = (type, extra = {}) => ({ type, button: 0, pointerId: 1, isPrimary: true, repeat: false, prevented: false, stopped: false,
        target: { closest: selector => extra.inside === false ? null : (selector === '#microphone-button' ? orb : null) },
        preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; }, stopPropagation() {}, ...extra });
    const fire = (type, extra = {}, key = type) => {
        const e = event(type, extra);
        for (const fn of listeners.get(key) || []) fn(e);
        return e;
    };
    const dispose = () => {
        if (savedDocument) Object.defineProperty(globalThis, 'document', savedDocument); else delete globalThis.document;
        resetAnnouncer(); delete globalThis.__convoInstalled;
    };
    return {
        store: namespace.store, announcements, clock, mic, log, cueNames, audios, recorders, nativeStore, fire, dispose, listeners, document,
        announced: () => live.children[0]?.textContent ?? '',
        orbState: () => mic.attrs.get('data-convo'),
        down: extra => fire('pointerdown', extra), up: extra => fire('pointerup', extra), click: extra => fire('click', extra),
        key: (type, code, extra = {}) => fire(type, { code, key: code === 'Space' ? ' ' : code, ...extra }),
        count: name => log.filter(entry => entry === name).length,
    };
}
async function withRig(options, run) {
    const rig = await rigStore(options);
    try { await rig.store.refresh(); await run(rig); } finally { await rig.store.stop().catch(() => {}); rig.dispose(); }
}

test('Tap: toggles Convo once, opens no dictation microphone, and the release click does not toggle it back', () => withRig({}, async r => {
    r.down(); await r.clock.advance(250);
    assert.equal(r.log.includes('recorder.open'), false);                // a tap never touches the microphone
    assert.equal(r.store.pressing, true);                                // the ring is charging
    assert.equal(r.mic.attrs.has('data-convo-press'), true);
    r.up(); await flush();
    assert.equal(r.store.active, true);
    assert.equal(r.count('convo.start'), 1);
    const click = r.click(); await flush();                              // the browser's click after the release
    assert.equal(click.prevented && click.stopped, true);                // the host's own microphone handler never runs
    assert.equal(r.store.active, true);
    assert.equal(r.count('convo.stop'), 0);
    assert.equal(r.recorders.length, 0);                                 // no dictation microphone was opened by a tap
    assert.equal(r.store.pressing, false);
    assert.equal(r.mic.attrs.has('data-convo-press'), false);
}));

test('Hold to the latch time: Convo is paused cleanly, ONE recorder opens in held mode, and the orb and live region say so', () => withRig({}, async r => {
    await r.store.start();
    assert.equal(r.store.active, true);
    r.down();
    await r.clock.advance(1999);
    assert.deepEqual([r.recorders.length, r.store.active], [0, true]);   // nothing changes before the decision
    await r.clock.advance(1);
    assert.equal(r.store.active, false);                                 // the voice session was closed first: never two captures
    assert.equal(r.audios[0].closed, true);
    assert.equal(r.recorders.length, 1);
    assert.deepEqual(r.log.filter(entry => entry.startsWith('recorder.')), ['recorder.open', 'recorder.startManual']);
    assert.deepEqual(r.recorders[0].manualHistory, [true]);
    assert.equal(r.count('recorder.toggle'), 0);
    assert.equal(r.orbState(), 'dictating-latched');
    assert.equal(r.mic.attrs.get('aria-label'), 'Hands-free dictation. Release to keep listening hands-free; keep holding for push-to-talk.');
    assert.equal(r.announcements.includes(r.mic.attrs.get('aria-label')), true);
    assert.equal(r.cueNames.at(-1), 'dictation-start');
    assert.equal(r.store.pressing, false);                               // the charging ring has done its job
    assert.match(r.store.note, /paused for dictation/);                  // and the conversation is told, not silently resumed
}));

test('Release between the thresholds latches: the same recorder carries on, nothing is sent, and the click does not toggle Convo', () => withRig({}, async r => {
    r.down(); await r.clock.advance(2600); r.up(); await flush();
    assert.equal(r.store.dictating, true);
    assert.deepEqual([r.store.dictationMode, r.store.dictationHolding], ['latched', false]);
    assert.deepEqual(r.recorders[0].manualHistory, [true, false]);       // held, then back to the host's own voice and silence rules
    assert.equal(r.count('recorder.finish'), 0);
    assert.equal(r.mic.attrs.get('aria-label'), 'Hands-free dictation. Listening for speech; click to stop, Escape cancels.');
    assert.equal(r.announced(), 'Hands-free dictation. Listening for speech; click to stop, Escape cancels.');
    r.click(); await flush();
    assert.equal(r.store.active, false);                                 // not toggled into a conversation
    assert.equal(r.count('convo.start'), 0);
    await r.clock.advance(120_000);
    assert.equal(r.store.dictating, true);                               // nothing ends it by itself
}));

test('A press while latched finishes and delivers once; it opens no second capture and its click does nothing', () => withRig({}, async r => {
    r.down(); await r.clock.advance(2200); r.up(); r.click(); await flush();
    r.down(); await flush();                                             // the orb is pressed again
    assert.equal(r.count('recorder.finish'), 1);
    assert.equal(r.store.dictating, false);
    assert.equal(r.orbState(), 'ready');
    assert.equal(r.nativeStore.microphoneInput, null);
    await r.clock.advance(9000);                                         // however long it is held, nothing new starts
    r.up(); r.click(); await flush();
    assert.deepEqual([r.recorders.length, r.count('recorder.finish'), r.count('convo.start'), r.count('recorder.discard')], [1, 1, 0, 0]);
    assert.equal(r.store.note, '');                                      // a delivered transcript is the host's to show; nothing extra is said
}));

test('Hold to the push-to-talk time: own state and cue; release finishes once and the recorder never goes hands-free', () => withRig({}, async r => {
    r.down(); await r.clock.advance(4000);
    assert.equal(r.orbState(), 'dictating-ptt');
    assert.deepEqual(r.cueNames.filter(name => name.startsWith('dictation')), ['dictation-start', 'dictation-ptt']);
    assert.equal(r.mic.attrs.get('aria-label'), 'Push-to-talk. Listening only while held; release to finish.');
    assert.equal(r.announced(), r.mic.attrs.get('aria-label'));
    await r.clock.advance(5000);
    assert.equal(r.count('recorder.finish'), 0);                         // still listening while held
    r.up(); await flush();
    assert.equal(r.count('recorder.finish'), 1);
    assert.equal(r.count('recorder.discard'), 0);
    assert.equal(r.recorders[0].manualHistory.every(value => value === true), true);   // never released to the host's silence rules
    assert.equal(r.cueNames.at(-1), 'dictation-end');
    assert.equal(r.orbState(), 'ready');
    r.click(); await flush();
    assert.equal(r.store.active, false);                                 // the release-click guard
    assert.equal(r.count('convo.start'), 0);
}));

test('Escape cancels latched dictation: nothing is delivered, the user is told, and the orb is free again', () => withRig({}, async r => {
    r.down(); await r.clock.advance(2100); r.up(); r.click(); await flush();
    const escape = r.key('keydown', 'Escape', { key: 'Escape' }); await flush();
    assert.equal(escape.prevented, true);
    assert.equal(r.count('recorder.discard'), 1);
    assert.equal(r.count('recorder.finish'), 0);
    assert.equal(r.store.note, 'Dictation cancelled. Nothing was sent.');
    assert.equal(r.announced(), 'Dictation cancelled. Nothing was sent.');
    assert.equal(r.orbState(), 'ready');
    assert.equal(r.nativeStore.microphoneInput, null);
    assert.equal(r.key('keydown', 'Escape', { key: 'Escape' }).prevented, false);    // with nothing to cancel, Escape belongs to the page again
}));

test('pointercancel, window blur and a hidden page during a hold discard the dictation instead of sending it', async () => {
    for (const interrupt of ['pointercancel', 'blur', 'hidden']) {
        await withRig({}, async r => {
            r.down(); await r.clock.advance(4500);
            assert.equal(r.orbState(), 'dictating-ptt', interrupt);
            if (interrupt === 'pointercancel') r.fire('pointercancel');
            else if (interrupt === 'blur') r.fire('blur', {}, 'window:blur');
            else { r.document.hidden = true; r.fire('visibilitychange', {}); }
            await flush();
            assert.equal(r.count('recorder.discard'), 1, interrupt);
            assert.equal(r.count('recorder.finish'), 0, interrupt);
            assert.equal(r.orbState(), 'ready', interrupt);
            assert.equal(r.store.note, 'Dictation cancelled. Nothing was sent.', interrupt);
            if (interrupt !== 'pointercancel') { r.up(); r.click(); await flush(); }   // a pointercancel is followed by no release or click at all
            else await flush();
            assert.deepEqual([r.count('convo.start'), r.count('recorder.finish')], [0, 0], interrupt);   // the late release is neither a tap nor a send
        });
    }
});

test('A press interrupted before the decision is neither a tap nor dictation; a visible page, or a hands-free latch, is not interrupted', async () => {
    await withRig({}, async r => {
        r.down(); await r.clock.advance(1200);
        r.fire('blur', {}, 'window:blur');
        await r.clock.advance(5000); r.up(); r.click(); await flush();
        assert.deepEqual([r.count('recorder.discard'), r.nativeStore.microphoneInput, r.count('convo.start'), r.store.pressing], [1, null, 0, false]);   // the buffer is gone
    });
    await withRig({}, async r => {
        r.down(); await r.clock.advance(4200);
        r.fire('visibilitychange', {});                                  // the page is still visible: nothing happens
        await flush();
        assert.equal(r.count('recorder.discard'), 0);
        r.up(); await flush();
        assert.equal(r.count('recorder.finish'), 1);
    });
    await withRig({}, async r => {
        r.down(); await r.clock.advance(2200); r.up(); r.click(); await flush();
        r.fire('blur', {}, 'window:blur'); r.document.hidden = true; r.fire('visibilitychange', {}); r.fire('pointercancel');
        await flush();
        assert.equal(r.store.dictating, true);                           // hands-free dictation survives losing focus
        assert.equal(r.count('recorder.discard') + r.count('recorder.finish'), 0);
    });
});

test('Empty or silent speech says so and sends nothing; a failed transcription is reported', async () => {
    for (const [outcome, pattern] of [['empty', /^Nothing was heard, so nothing was sent\.$/], ['silent', /^Nothing was heard/], ['error', /^Transcription failed, so nothing was sent\.$/]]) {
        await withRig({ finish: outcome }, async r => {
            r.down(); await r.clock.advance(4100); r.up(); await flush();
            assert.match(r.store.note, pattern, outcome);
            assert.equal(r.announced(), r.store.note, outcome);
        });
    }
    await withRig({ finish: null }, async r => {                         // nothing was pending when the user stopped: no complaint
        r.down(); await r.clock.advance(2100); r.up(); r.click(); r.down(); await flush();
        assert.equal(r.store.note, '');
        assert.equal(r.announced(), 'Dictation finished.');
    });
});

test('No duplicate capture: arming twice, or the Dictate button during a latch, opens one recorder and the button stops it', () => withRig({}, async r => {
    r.down(); await r.clock.advance(2100); r.up(); r.click(); await flush();
    await r.store.beginDictation();                                      // e.g. the panel's button while already dictating
    await r.store.beginDictation({ held: true });
    assert.equal(r.recorders.length, 1);
    assert.equal(r.count('recorder.startManual'), 1);
    assert.equal(r.count('recorder.toggle'), 0);                         // never toggled off by a repeated start
    await r.store.toggleDictation();
    assert.equal(r.count('recorder.finish'), 1);
    assert.equal(r.store.dictating, false);
}));

test('A new hold opens a fresh recorder only after the previous one finished flushing', () => {
    let release;
    const slow = new Promise(resolve => { release = resolve; });
    return withRig({ finish: () => slow }, async r => {
        r.down(); await r.clock.advance(2100); r.up(); r.click(); await flush();
        r.down(); await flush(); r.up(); r.click(); await flush();     // stop: the recorder is still flushing its last audio
        assert.equal(r.count('recorder.finish'), 1);
        r.down(); await r.clock.advance(2100);
        assert.equal(r.recorders.length, 1, 'no second recorder while the first still flushes');
        release('delivered'); await flush();
        assert.equal(r.recorders.length, 2);
        assert.equal(r.nativeStore.microphoneInput, r.recorders[1]);
        r.up(); await flush();
    });
});

test('Keyboard: Space and Enter on the orb are the same gesture, repeats and modified keys are not, and no click leaks through', () => withRig({}, async r => {
    const first = r.key('keydown', 'Space');
    assert.equal(first.prevented, true);                                 // the button's own click is suppressed
    r.key('keydown', 'Space', { repeat: true }); r.key('keydown', 'Space', { repeat: true });
    await r.clock.advance(2200);
    assert.equal(r.orbState(), 'dictating-latched');
    const keyup = r.key('keyup', 'Space');
    await flush();
    assert.equal(keyup.prevented, true);
    assert.deepEqual(r.recorders[0].manualHistory, [true, false]);       // latched on key release, like a pointer
    r.click({ detail: 0 }); await flush();                               // a browser that clicks anyway
    assert.equal(r.count('convo.start'), 0);
    r.key('keydown', 'Enter'); await flush();                            // Enter while latched stops it
    assert.equal(r.count('recorder.finish'), 1);
    r.key('keyup', 'Enter'); r.click({ detail: 0 }); await flush();
    assert.equal(r.count('convo.start'), 0);
    const hotkey = r.key('keydown', 'Space', { ctrlKey: true, shiftKey: true });
    assert.equal(hotkey.prevented, false);                               // Ctrl+Shift+Space stays the Convo hotkey
    assert.equal(r.store.pressing, false);
}));

test('A click with no press (assistive technology) acts as a tap, or finishes latched dictation', () => withRig({}, async r => {
    r.click(); await flush();
    assert.equal(r.store.active, true);
    await r.store.stop();
    r.down(); await r.clock.advance(2100); r.up(); r.click(); await flush();
    await r.clock.advance(1000);                                         // the swallow window has passed
    r.click(); await flush();
    assert.equal(r.count('recorder.finish'), 1);
}));

test('A second finger while the first is still deciding does not start dictation or toggle Convo', () => withRig({}, async r => {
    r.down({ pointerId: 1 }); await r.clock.advance(300);
    r.down({ pointerId: 2, isPrimary: false });
    await r.clock.advance(5000);
    r.up({ pointerId: 2 }); r.up({ pointerId: 1 }); r.click(); await flush();
    assert.deepEqual([r.count('convo.start'), r.store.active, r.store.dictating, r.count('recorder.finish')], [0, false, false, 0]);
    assert.deepEqual([r.count('recorder.discard'), r.nativeStore.microphoneInput], [1, null]);        // the buffer the first finger opened is discarded
    assert.equal(r.store.pressing, false);
}));

test('Development mode: a tap explains, a hold still dictates', () => withRig({ settings: { ai_link_development_mode: true } }, async r => {
    r.down(); await r.clock.advance(100); r.up(); r.click(); await flush();
    assert.match(r.store.note, /development mode keeps browser voice idle/);
    assert.equal(r.count('convo.start'), 0);
    r.down(); await r.clock.advance(2100); r.up(); await flush();
    assert.equal(r.store.dictating, true);
    assert.equal(r.orbState(), 'dictating-latched');
}));

test('A recorder that cannot be opened is reported, frees the gesture and sends nothing', () => withRig({ init: 'fail' }, async r => {
    r.down(); await r.clock.advance(2100); await flush();
    assert.equal(r.store.dictating, false);
    assert.equal(r.store.dictationMode, '');
    assert.match(r.store.error, /could not be opened for dictation/);
    assert.equal(r.orbState(), 'error');
    r.up(); r.click(); await flush();
    assert.deepEqual([r.count('convo.start'), r.count('recorder.finish')], [0, 0]);
}));

test('Released or cancelled while the recorder is still opening: no capture is left running and nothing is sent', async () => {
    let open;
    await withRig({ init: () => new Promise(resolve => { open = resolve; }) }, async r => {
        r.down(); await r.clock.advance(2100);                           // armed; the recorder is waiting on the microphone permission
        r.key('keydown', 'Escape', { key: 'Escape' }); await flush();
        open(); await flush(); await r.clock.advance(100);
        assert.equal(r.store.dictating, false);
        assert.equal(r.nativeStore.microphoneInput, null);
        assert.deepEqual([r.count('recorder.discard'), r.count('recorder.finish')], [1, 0]);             // nothing recorded is kept
        r.up(); r.click(); await flush();
        assert.equal(r.count('convo.start'), 0);
    });
    await withRig({ init: () => new Promise(resolve => { open = resolve; }) }, async r => {
        r.down(); await r.clock.advance(2100); r.up(); await flush();   // latched before the recorder opened
        open(); await flush();
        assert.deepEqual(r.recorders[0].manualHistory, [false]);         // opened straight into hands-free mode
        assert.equal(r.store.dictating, true);
    });
});

test('A host recorder without held mode still dictates (latched), and says no more than it can do', () => withRig({ manualSupported: false }, async r => {
    r.down(); await r.clock.advance(4100); r.up(); await flush();
    assert.equal(r.count('recorder.toggle'), 1);                         // the host's own voice-activity dictation
    assert.equal(r.count('recorder.startManual'), 0);
    assert.equal(r.count('recorder.finish'), 1);
}));

// ---- the host recorder's held mode, with a fake MediaRecorder and microphone meter -------------------------------------------
async function recorderRig({ transcript = 'hello there', fail = false } = {}) {
    const { EnhancedMicrophoneInput, Status, configure } = recorderTest;
    const sent = [], calls = [], frames = [], blobs = [];
    let level = 0, clock = 1000;
    const saved = { raf: globalThis.requestAnimationFrame, caf: globalThis.cancelAnimationFrame, now: Date.now, window: globalThis.window, reader: globalThis.FileReader };
    globalThis.requestAnimationFrame = fn => frames.push(fn);
    globalThis.cancelAnimationFrame = () => {};
    Date.now = () => clock;
    globalThis.window = {};
    globalThis.FileReader = class { readAsDataURL(blob) { blobs.push(blob); this.result = 'data:audio/webm;base64,AAAA'; queueMicrotask(() => this.onloadend()); } };
    configure({
        speechStore: { stt_waiting_timeout: 2000, stt_silence_threshold: 0.3, stt_silence_duration: 1000, isSpeaking: false },
        microphoneSettingStore: { getSelectedDevice: () => null },
        callJsonApi: async (url, payload) => { calls.push([url, payload]); if (fail) throw new Error('offline'); return { text: transcript }; },
    });
    const mic = new EnhancedMicrophoneInput(async text => { sent.push(text); });
    const listeners = [];
    mic.mediaRecorder = {
        state: 'inactive', mimeType: 'audio/webm', started: 0,
        start() { this.state = 'recording'; this.started++; },
        stop() { this.state = 'inactive'; listeners.splice(0).forEach(fn => fn()); },
        requestData() { mic.mediaRecorder.ondataavailable({ data: new Blob([new Uint8Array(400)]) }); },
        addEventListener(type, fn) { if (type === 'stop') listeners.push(fn); }, removeEventListener() {},
    };
    mic.analyserNode = { fftSize: 8, getByteTimeDomainData(array) { array.fill(128 + level * 128); }, disconnect() {} };
    mic.mimeType = 'audio/webm';
    mic.mediaRecorder.ondataavailable = event => { if (event.data?.size) mic.audioChunks.push(event.data); };
    const frame = (ms = 100) => { clock += ms; const fn = frames.shift(); if (fn) fn(); };
    return {
        mic, sent, calls, blobs, Status, frame, setLevel: value => { level = value; },
        restore() {
            mic.dispose();
            globalThis.requestAnimationFrame = saved.raf; globalThis.cancelAnimationFrame = saved.caf; Date.now = saved.now;
            globalThis.window = saved.window; globalThis.FileReader = saved.reader;
            if (saved.window === undefined) delete globalThis.window;
            if (saved.raf === undefined) delete globalThis.requestAnimationFrame;
            if (saved.caf === undefined) delete globalThis.cancelAnimationFrame;
            if (saved.reader === undefined) delete globalThis.FileReader;
        },
    };
}

test('Recorder held mode records at once, ignores silence while held, and keeps one capture', async () => {
    const r = await recorderRig();
    try {
        await r.mic.startManual();
        assert.equal(r.mic.status, r.Status.RECORDING);
        assert.equal(r.mic.mediaRecorder.started, 1);
        for (let i = 0; i < 40; i++) r.frame(100);                      // four quiet seconds
        assert.equal(r.mic.status, r.Status.RECORDING);                 // no automatic WAITING / PROCESSING while the press is held
        assert.deepEqual(r.calls, []);
        assert.equal(r.mic.mediaRecorder.started, 1);
    } finally { r.restore(); }
});

test('Recorder: release hands the same recording to the host voice rules, but silence so far is not transcribed', async () => {
    const quiet = await recorderRig();
    try {
        await quiet.mic.startManual();
        for (let i = 0; i < 10; i++) quiet.frame(100);
        quiet.mic.setManual(false);
        assert.equal(quiet.mic.status, quiet.Status.LISTENING);         // back to waiting for speech; the silence is discarded
        assert.deepEqual(quiet.calls, []);
    } finally { quiet.restore(); }
    const spoke = await recorderRig();
    try {
        await spoke.mic.startManual();
        spoke.setLevel(0.5); spoke.frame(100); spoke.frame(100);
        spoke.setLevel(0); spoke.mic.setManual(false);
        assert.equal(spoke.mic.status, spoke.Status.RECORDING);         // speech already heard: the recording continues
        for (let i = 0; i < 11; i++) spoke.frame(100);                  // then the host's own silence rule applies
        assert.equal(spoke.mic.status, spoke.Status.WAITING);
    } finally { spoke.restore(); }
});

test('Recorder finish: speech is transcribed once and handed to the host callback; the outcome says so', async () => {
    const r = await recorderRig({ transcript: 'send the report' });
    try {
        await r.mic.startManual();
        r.setLevel(0.5); r.frame(100); r.frame(100);
        const outcome = await r.mic.finishConvoDictation();
        assert.equal(outcome, 'delivered');
        assert.deepEqual(r.sent, ['send the report']);
        assert.equal(r.calls.length, 1);
        assert.equal(r.mic.status, r.Status.INACTIVE);
    } finally { r.restore(); }
});

test('Recorder finish: a held recording of pure silence is not sent for transcription; a blank transcript is not delivered', async () => {
    const silent = await recorderRig({ transcript: 'Thank you.' });
    try {
        await silent.mic.startManual();
        for (let i = 0; i < 20; i++) silent.frame(100);
        assert.equal(await silent.mic.finishConvoDictation(), 'silent');
        assert.deepEqual([silent.calls.length, silent.sent.length], [0, 0]);
    } finally { silent.restore(); }
    const blank = await recorderRig({ transcript: '   ' });
    try {
        await blank.mic.startManual();
        blank.setLevel(0.5); blank.frame(100);
        assert.equal(await blank.mic.finishConvoDictation(), 'empty');
        assert.deepEqual(blank.sent, []);
    } finally { blank.restore(); }
    const parenthetical = await recorderRig({ transcript: '(music)' });
    try {
        await parenthetical.mic.startManual();
        parenthetical.setLevel(0.5); parenthetical.frame(100);
        assert.equal(await parenthetical.mic.finishConvoDictation(), 'empty');
        assert.deepEqual(parenthetical.sent, []);
    } finally { parenthetical.restore(); }
});

test('Recorder: a failed transcription is an error outcome and nothing is delivered', async () => {
    const r = await recorderRig({ fail: true });
    try {
        await r.mic.startManual();
        r.setLevel(0.5); r.frame(100);
        assert.equal(await r.mic.finishConvoDictation(), 'error');
        assert.deepEqual(r.sent, []);
    } finally { r.restore(); }
});

test('Recorder discard: nothing recorded or in flight ever reaches the host', async () => {
    const early = await recorderRig();
    try {
        await early.mic.startManual();
        early.setLevel(0.5); early.frame(100);
        assert.equal(early.mic.discardConvoDictation(), 'cancelled');
        assert.deepEqual([early.sent.length, early.calls.length], [0, 0]);
        assert.equal(early.mic.status, early.Status.INACTIVE);
    } finally { early.restore(); }
    const inflight = await recorderRig({ transcript: 'too late' });
    try {
        await inflight.mic.startManual();
        inflight.setLevel(0.5); inflight.frame(100);
        const finishing = inflight.mic.finishConvoDictation();           // transcription begins ...
        await Promise.resolve();
        inflight.mic.discardConvoDictation();                            // ... and the user cancels before it returns
        await finishing;
        assert.deepEqual(inflight.sent, []);
    } finally { inflight.restore(); }
});

// ---- pre-open: the microphone is held open as a buffer so the first seconds of the press are not lost -------------------------
test('Pre-open: release at 299 ms never touches the microphone; at 301 ms the buffer is opened, discarded and closed before the tap acts', () => withRig({}, async r => {
    r.down(); await r.clock.advance(299); r.up(); r.click(); await flush();
    assert.deepEqual([r.log.includes('recorder.open'), r.count('convo.start')], [false, 1]);
    await r.store.stop();
    r.log.length = 0;
    r.down(); await r.clock.advance(301);
    assert.deepEqual([r.count('recorder.open'), r.count('recorder.startManual')], [1, 1]);   // opened once, as a buffer
    assert.equal(r.store.dictating, false);
    r.up(); r.click(); await flush();
    assert.equal(r.count('recorder.discard'), 1);                         // thrown away, stream closed
    assert.equal(r.count('recorder.finish'), 0);
    assert.equal(r.nativeStore.microphoneInput, null);
    assert.ok(r.log.indexOf('recorder.discard') < r.log.indexOf('convo.start'), 'the buffer is closed before Convo opens its own capture');
    assert.equal(r.count('convo.start'), 1);                              // the existing tap behaviour still applies
    assert.equal(r.store.note, '');                                       // a tap is not a failed dictation: nothing is said
    assert.equal(r.announcements.length, 0);
    assert.equal(r.store.capturing, false);
}));

test('Pre-open: the orb makes no recording promise while buffering, only a quiet capturing mark', () => withRig({}, async r => {
    const label = r.mic.attrs.get('aria-label');
    r.down(); await r.clock.advance(1000);
    assert.equal(r.orbState(), 'ready');
    assert.equal(r.mic.attrs.get('aria-label'), label);                   // no label, state or cue change
    assert.equal(r.mic.attrs.has('data-convo-capture'), true);            // the microphone really is on
    assert.deepEqual(r.cueNames, []);
    assert.equal(r.store.dictating, false);
    r.up(); await flush();
    assert.equal(r.mic.attrs.has('data-convo-capture'), false);
}));

test('Pre-open: release at 1999 ms discards the buffer and closes the stream; no transcript, no send', () => withRig({}, async r => {
    r.down(); await r.clock.advance(1999); r.up(); r.click(); await flush();
    assert.deepEqual([r.count('recorder.discard'), r.count('recorder.finish'), r.nativeStore.microphoneInput], [1, 0, null]);
    assert.equal(r.store.dictating, false);
    assert.equal(r.count('convo.start'), 1);
}));

test('Pre-open: Escape, pointercancel, blur and a hidden page before the latch time discard the buffer and do nothing else', async () => {
    for (const how of ['escape', 'pointercancel', 'blur', 'hidden']) {
        await withRig({}, async r => {
            r.down(); await r.clock.advance(1200);
            if (how === 'escape') r.key('keydown', 'Escape', { key: 'Escape' });
            else if (how === 'pointercancel') r.fire('pointercancel');
            else if (how === 'blur') r.fire('blur', {}, 'window:blur');
            else { r.document.hidden = true; r.fire('visibilitychange', {}); }
            await flush();
            assert.deepEqual([r.count('recorder.discard'), r.count('recorder.finish'), r.nativeStore.microphoneInput], [1, 0, null], how);
            if (how !== 'pointercancel') { r.up(); r.click(); await flush(); }
            assert.deepEqual([r.count('convo.start'), r.store.dictating, r.store.note], [0, false, ''], how);
        });
    }
});

test('Latch at 2000 ms keeps the SAME recording: one microphone open, one start, no restart, no discard', () => withRig({}, async r => {
    r.down(); await r.clock.advance(1999);
    assert.deepEqual([r.count('recorder.open'), r.count('recorder.startManual')], [1, 1]);
    await r.clock.advance(1);
    assert.equal(r.store.dictating, true);
    assert.equal(r.orbState(), 'dictating-latched');
    assert.deepEqual([r.count('recorder.open'), r.count('recorder.startManual'), r.count('recorder.discard'), r.recorders.length], [1, 1, 0, 1]);
    assert.equal(r.nativeStore.microphoneInput, r.recorders[0]);          // no second getUserMedia, no second recorder
    assert.equal(r.mic.attrs.has('data-convo-capture'), false);
    assert.equal(r.recorders[0].manualHistory.at(-1), true);
    r.up(); await flush();
    assert.equal(r.store.dictating, true);
    r.down(); await flush();                                               // finish
    assert.deepEqual([r.count('recorder.finish'), r.recorders.length, r.count('recorder.startManual')], [1, 1, 1]);
}));

test('Push-to-talk at 4000 ms is the same recording too, with the earlier audio included', () => withRig({}, async r => {
    r.down(); await r.clock.advance(4000);
    assert.equal(r.orbState(), 'dictating-ptt');
    assert.deepEqual([r.recorders.length, r.count('recorder.open'), r.count('recorder.startManual'), r.count('recorder.discard')], [1, 1, 1, 0]);
    r.up(); await flush();
    assert.deepEqual([r.count('recorder.finish'), r.recorders.length], [1, 1]);
}));

test('Pre-open never runs next to a Convo voice session; a short press leaves Convo as before, a long one stops it only at the latch time', () => withRig({}, async r => {
    await r.store.start();
    r.down(); await r.clock.advance(1500);
    assert.deepEqual([r.log.includes('recorder.open'), r.store.active], [false, true]);   // no second capture beside the conversation's
    r.up(); r.click(); await flush();
    assert.equal(r.store.active, false);                                   // the tap toggled it, as it always did
    assert.equal(r.recorders.length, 0);
    await r.store.start();
    r.down(); await r.clock.advance(2100);
    assert.equal(r.store.active, false);
    assert.deepEqual([r.recorders.length, r.count('recorder.startManual')], [1, 1]);      // opened fresh at the latch time (first seconds not captured)
    assert.ok(r.log.indexOf('capture.close') < r.log.lastIndexOf('recorder.open'));
}));

test('Pre-open denied or failing degrades gracefully: a note, no stuck state, the tap still works, the latch retries once', async () => {
    await withRig({ init: 'fail' }, async r => {
        r.down(); await r.clock.advance(400); await flush();
        assert.match(r.store.note, /could not be opened early/);
        assert.deepEqual([r.store.capturing, r.store.dictating, r.nativeStore.microphoneInput], [false, false, null]);
        r.up(); r.click(); await flush();
        assert.equal(r.count('convo.start'), 1);                           // not stuck: the tap toggles Convo
    });
    await withRig({ failTimes: 1 }, async r => {
        r.down(); await r.clock.advance(400); await flush();
        assert.match(r.store.note, /could not be opened early/);
        await r.clock.advance(1700);                                       // the latch time retries and succeeds
        assert.equal(r.store.dictating, true);
        assert.equal(r.recorders.length, 1);
        r.up(); await flush();
        assert.equal(r.store.dictating, true);
    });
    await withRig({ init: 'fail' }, async r => {
        r.down(); await r.clock.advance(2200); await flush();              // still denied at the latch time
        assert.match(r.store.error, /could not be opened for dictation/);
        assert.deepEqual([r.store.dictating, r.store.dictationMode], [false, '']);
        r.up(); r.click(); await flush();
        assert.deepEqual([r.count('convo.start'), r.count('recorder.finish')], [0, 0]);
    });
});

test('A Convo session started during the hold takes the microphone: the buffer is not adopted, dictation opens fresh', () => withRig({}, async r => {
    r.down(); await r.clock.advance(500);
    assert.equal(r.recorders.length, 1);
    await r.store.start();                                                 // e.g. the hotkey; start() releases the native recorder
    r.nativeStore.microphoneInput = null;
    await r.clock.advance(1600);
    assert.equal(r.store.active, false);                                   // dictation closed the voice session first
    assert.equal(r.recorders.length, 2);
    assert.equal(r.store.dictating, true);
    assert.equal(r.nativeStore.microphoneInput, r.recorders[1]);
}));

test('Recorder: audio buffered before the latch decision is part of the delivered recording, in order, from ONE recorder start', async () => {
    const r = await recorderRig({ transcript: 'from the very start' });
    try {
        await r.mic.startManual();                                         // pre-open at 300 ms
        r.setLevel(0.5); r.frame(100);
        r.mic.mediaRecorder.ondataavailable({ data: new Blob([new Uint8Array(300).fill(1)]) });   // words spoken before the latch decision
        for (let i = 0; i < 17; i++) r.frame(100);
        r.mic.setManual(true);                                             // the decision changes nothing about the recording
        r.mic.mediaRecorder.ondataavailable({ data: new Blob([new Uint8Array(500).fill(2)]) });   // words after it
        assert.equal(r.mic.mediaRecorder.started, 1);                      // never restarted
        assert.equal(await r.mic.finishConvoDictation(), 'delivered');
        assert.deepEqual(r.sent, ['from the very start']);
        const bytes = new Uint8Array(await r.blobs[0].arrayBuffer());
        assert.deepEqual([bytes[0], bytes[299], bytes[300], bytes[799]], [1, 1, 2, 2]);   // no gap and no reordering
        assert.equal(r.mic.mediaRecorder.started, 1);
    } finally { r.restore(); }
});

test('Recorder: a buffer discarded before the latch decision is never transcribed or delivered', async () => {
    const r = await recorderRig();
    try {
        await r.mic.startManual();
        r.setLevel(0.5); r.frame(100);
        r.mic.mediaRecorder.ondataavailable({ data: new Blob([new Uint8Array(300).fill(1)]) });
        r.mic.discardConvoDictation();
        assert.deepEqual([r.calls.length, r.sent.length, r.mic.status], [0, 0, r.Status.INACTIVE]);
        assert.equal(r.mic.stream, null);                                  // the stream is released
    } finally { r.restore(); }
});

test('Pre-open: a tap released while the buffer is still opening waits for it to close before Convo starts', async () => {
    let open;
    await withRig({ init: () => new Promise(resolve => { open = resolve; }) }, async r => {
        r.down(); await r.clock.advance(400);                           // the microphone permission is still pending
        r.up(); r.click(); await flush();
        assert.equal(r.count('convo.start'), 0);                         // Convo's capture must not open beside a buffer that is not yet closed
        open(); await flush();
        assert.deepEqual([r.count('recorder.dispose'), r.count('recorder.startManual'), r.count('recorder.finish')], [1, 0, 0]);   // closed without ever recording
        assert.equal(r.count('convo.start'), 1);
        assert.ok(r.log.indexOf('recorder.dispose') < r.log.indexOf('convo.start'));
        assert.equal(r.nativeStore.microphoneInput, null);
    });
});

test('Recorder: when the host stops dictation while handling an utterance (draft mode), the recorder stays stopped', async () => {
    const r = await recorderRig({ transcript: 'draft this' });
    try {
        await r.mic.startManual();
        r.setLevel(0.5); r.frame(100);
        r.mic.setManual(false);
        r.mic.updateCallback = async () => { r.mic.status = r.Status.INACTIVE; };   // the host's sendVoiceMessage -> stop() in draft mode
        await r.mic.process();
        assert.equal(r.mic.status, r.Status.INACTIVE);                       // not back to listening by itself
        assert.equal(r.mic.lastOutcome, 'delivered');
    } finally { r.restore(); }
});
