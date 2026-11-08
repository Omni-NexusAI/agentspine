// The orb's hold gestures end-to-end against the REAL host recorder (enhanced-stt-recorder.js, with Convo's delivery adapter) and a
// stand-in for the host's Whisper store whose delivery is the live v2.11 host's logic (whisper-stt-store.js: deliverVoiceMessage
// honouring a finalTranscriptHandler, sendVoiceMessage draft vs send, stop(), handleMicrophoneClick) and whose composer is index.js
// updateChatInput (appends with a separating space). Manual sends go through the real send_message_before extension. Everything runs
// on one virtual clock: timers, animation frames, Date.now and the recorder's 250 ms data slices. No microphone, model or network.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fakeMic } from './store-fixture.mjs';
import { hostStubs, loadModule } from './vm-loader.mjs';
import { readWebui } from './settings-markup.mjs';
import { resetAnnouncer } from '../webui/orb.js';
import { __test as recorderModule } from '../extensions/webui/initFw_end/enhanced-stt-recorder.js';

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };

// mode: the host's message_mode (send | draft); draft: Convo's voice_message_draft (single | continuous); enabled: Convo owns the orb
// (false = the host microphone button works as the host's own, served by Convo's recorder); texts: what each transcription returns.
async function hostRig({ mode = 'send', stopStyle = 'inactive', draft = 'single', enabled = true, texts = [] } = {}) {
    // ---- one virtual clock for both realms
    let now = 0, nextId = 1, level = 0, marker = 0;
    const timers = new Map(), frames = new Map();
    const setTimer = (fn, ms = 0) => { const id = nextId++; timers.set(id, { at: now + Math.max(0, ms), fn }); return id; };
    const clearTimer = id => timers.delete(id);
    const saved = {};
    for (const key of ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame', 'window', 'FileReader', 'MediaRecorder', 'navigator'])
        saved[key] = Object.getOwnPropertyDescriptor(globalThis, key);
    const savedNow = Date.now;

    // ---- fake media: one stream per getUserMedia, MediaRecorder slices, an analyser that reads the scripted speech level
    const streams = [], recorders = [], chunks = [], transcribed = [];
    class FakeMediaRecorder {
        static isTypeSupported(type) { return type === 'audio/webm'; }
        constructor(stream) { this.stream = stream; this.state = 'inactive'; this.mimeType = 'audio/webm'; this.stopListeners = []; recorders.push(this); }
        start() { this.state = 'recording'; this.lastSlice = now; this.starts = (this.starts || 0) + 1; }
        emit() { marker += 1; chunks.push({ marker, at: now }); this.ondataavailable?.({ data: new Blob([new Uint8Array(300).fill(marker % 256)]) }); this.lastSlice = now; }
        requestData() { this.emit(); }
        stop() { this.state = 'inactive'; this.stopListeners.splice(0).forEach(fn => fn()); }
        addEventListener(type, fn) { if (type === 'stop') this.stopListeners.push(fn); }
        removeEventListener(type, fn) { this.stopListeners = this.stopListeners.filter(item => item !== fn); }
    }
    class FakeAudioContext {
        createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
        createAnalyser() { return { fftSize: 2048, connect() {}, disconnect() {}, getByteTimeDomainData(array) { array.fill(128 + Math.round(level * 128)); } }; }
        close() { return Promise.resolve(); }
    }
    const navigatorStub = { mediaDevices: { async getUserMedia(constraints) {
        const track = { stopped: false, stop() { this.stopped = true; } };
        const stream = { getTracks: () => [track], track, probe: constraints?.audio === true };   // `audio: true` is the host's permission probe
        streams.push(stream);
        return stream;
    } } };
    const define = (key, value) => Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
    define('setTimeout', setTimer); define('clearTimeout', clearTimer);
    define('requestAnimationFrame', fn => { const id = nextId++; frames.set(id, fn); return id; });
    define('cancelAnimationFrame', id => frames.delete(id));
    define('window', { AudioContext: FakeAudioContext });
    define('MediaRecorder', FakeMediaRecorder);
    define('navigator', navigatorStub);
    define('FileReader', class { readAsDataURL(blob) { this.blob = blob; this.result = 'data:audio/webm;base64,' + blob.size; Promise.resolve().then(() => this.onloadend()); } });
    Date.now = () => now;

    // ---- the composer: index.js updateChatInput, verbatim in effect (append with a separating space, then a trailing space)
    const composer = [], sent = [], manualSends = [];
    let composerText = '';
    const updateChatInput = text => {
        const needsSpace = composerText.length > 0 && !composerText.endsWith(' ');
        composerText = composerText + (needsSpace ? ' ' : '') + text + ' ';
        composer.push(text);
    };
    // ---- the host's Whisper store: delivery is the live v2.11 host's logic, verbatim in effect
    const { createConvoRecorder } = recorderModule;
    const host = {
        microphoneInput: null, finalTranscriptHandler: null, statusNotices: 0,
        config: { message_mode: mode, silence_threshold: 0.3, silence_duration: 1000, waiting_timeout: 2000 },
        get sendsImmediately() { return this.config.message_mode !== 'draft'; },
        getSelectedDevice: () => null, async initRuntime() {}, notifyStatusChange() { this.statusNotices += 1; },
        async initMicrophone() {                                       // the Convo-patched initMicrophone, verbatim in effect
            if (this.microphoneInput) return this.microphoneInput;
            this.microphoneInput = createConvoRecorder(this);
            const ok = await this.microphoneInput.initialize();
            if (!ok) { this.microphoneInput = null; return null; }
            return this.microphoneInput;
        },
        async handleMicrophoneClick(finalTranscriptHandler = null) {    // the host's microphone button (status refresh and TTS stop left out)
            this.finalTranscriptHandler = typeof finalTranscriptHandler === 'function' ? finalTranscriptHandler : null;
            if (!this.microphoneInput) await this.initMicrophone();
            if (this.microphoneInput) await this.microphoneInput.toggle();
        },
        async deliverVoiceMessage(text) {
            if (this.finalTranscriptHandler) { await this.finalTranscriptHandler(text, { messageMode: this.config.message_mode, sendImmediately: this.sendsImmediately }); return; }
            await this.sendVoiceMessage(text);
        },
        async sendVoiceMessage(text) {
            const message = String(text || '').trim();
            if (!message) return;
            updateChatInput(message);
            if (!this.sendsImmediately) { this.stop(); return; }
            if (!this.microphoneInput?.messageSent) { this.microphoneInput.messageSent = true; sent.push(message); }   // await sendMessage()
        },
        stop() {                                                       // 'dispose' is the live v2.11 stop(); 'inactive' an older host's
            if (stopStyle === 'dispose') {
                if (this.microphoneInput) { this.microphoneInput.status = 'inactive'; this.microphoneInput.dispose(); this.microphoneInput = null; }
                this.finalTranscriptHandler = null; this.notifyStatusChange();
            } else if (this.microphoneInput) this.microphoneInput.status = 'inactive';
        },
    };
    // Transcriptions answer in order (`texts`, else "utterance N"); hold() keeps the next answers back until its release is called.
    let holdAnswers = null;
    const answer = async index => { if (holdAnswers) await holdAnswers.promise; return { text: texts[index - 1] ?? `utterance ${index}` }; };
    let namespace = null;
    recorderModule.configure({
        speechStore: { get stt_waiting_timeout() { return host.config.waiting_timeout; }, get stt_silence_threshold() { return host.config.silence_threshold; },
            get stt_silence_duration() { return host.config.silence_duration; }, isSpeaking: false },
        microphoneSettingStore: { getSelectedDevice: () => null },
        callJsonApi: async (_url, payload) => {
            const index = transcribed.length + 1;
            transcribed.push({ size: payload.audio, markers: lastBlobMarkers() });
            return answer(index);
        },
        updateChatInput,
        convoDraftFlag: () => namespace?.store.settings?.voice_message_draft,      // what the browser reads from Convo's store
    });
    // Which slices went into the transcribed blob: the recorder concatenates audioChunks in order, each slice is 300 bytes of its marker.
    let pendingBlob = null;
    const OriginalBlob = globalThis.Blob;
    const lastBlobMarkers = () => pendingBlob;
    const reader = globalThis.FileReader;
    define('FileReader', class extends reader { readAsDataURL(blob) {
        pendingBlob = null;
        blob.arrayBuffer().then(buffer => { const bytes = new Uint8Array(buffer); const list = []; for (let i = 0; i < bytes.length; i += 300) list.push(bytes[i]); pendingBlob = list; super.readAsDataURL(blob); });
    } });

    // ---- the Convo store, with the gesture adapter, on the same clock
    const mic = fakeMic({ 'aria-label': 'Microphone standby', 'aria-pressed': 'false' });
    const log = [], listeners = new Map();
    const live = { children: [], appendChild(child) { this.children.push(child); } };
    const document = {
        hidden: false, body: live, createElement: () => ({ attrs: new Map(), style: {}, textContent: '', isConnected: true, setAttribute(k, v) { this.attrs.set(k, v); }, remove() {} }),
        addEventListener: (type, fn) => { listeners.set(type, [...(listeners.get(type) || []), fn]); }, removeEventListener() {},
        getElementById: id => id === 'microphone-button' ? mic : null,
    };
    const windowStub = { confirm: () => true, addEventListener: (type, fn) => { listeners.set('window:' + type, [...(listeners.get('window:' + type) || []), fn]); } };
    // Convo's status: `status.fail` makes it unreachable (a plugin turned off disappears from the host API). `saved`: what Convo's
    // Whisper bridge reports (api/stt_status.py), which can differ from the host store's own copy until something reads it.
    const status = { settings: { enabled, max_audio_seconds: 20, voice_message_draft: draft }, fail: false, saved: { message_mode: mode, voice_message_draft: draft } };
    const bindings = hostStubs({
        '/js/api.js': { callJsonApi: async (url, payload) => {
            if (url === '/plugins/_convo/stt_status') {
                if (status.fail) throw new Error('Not found');
                return { success: true, enabled: true, config: { model_size: 'base', message_mode: status.saved.message_mode }, voice_message_draft: status.saved.voice_message_draft };
            }
            if (payload.action !== 'status') return { events: [], jobs: [] };
            if (status.fail) throw new Error('Not found');
            return { settings: { ...status.settings }, runtime: { mode: 'external' } };
        } },
        '/js/websocket.js': { createNamespacedClient: () => ({ addHandlers() {}, async on() {}, onDisconnect() {}, disconnect() {},
            async request(event) { log.push(event); return { results: [{ handlerId: 'plugin.Realtime', ok: true, data: { ok: true, session: { id: 's', epoch: 0 } } }] }; } }) },
        '/js/shortcuts.js': { getCurrentContextId: () => 'a', getCurrentContext: () => ({ id: 'a', name: 'Chat a' }), openModal: async () => {} },
        './audio.js': { AudioSession: class { constructor(o) { Object.assign(this, o); } async start() { log.push('capture.start'); } close() { log.push('capture.close'); } interrupt() {} setPolicyHold() {} } },
        '/plugins/_whisper_stt/webui/whisper-stt-store.js': { store: host },
    });
    const savedDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
    define('document', document);
    resetAnnouncer(); delete globalThis.__convoInstalled;
    const chatInput = { get message() { return composerText; } };                 // the host's composer store, as Alpine.store('chatInput')
    const Alpine = { store: name => ({ chatInput, whisperStt: host, convo: namespace?.store })[name] };
    ({ namespace } = await loadModule('webui/convo-store.js', { bindings, globals: {
        document, window: windowStub, setTimeout: setTimer, clearTimeout: clearTimer, setInterval: () => 0, clearInterval() {}, fetch: async () => ({ ok: true }),
        dispatchEvent() {}, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } }, Alpine } }));
    namespace.install();
    await flush();
    await namespace.store.refresh();
    // The real send_message_before extension, importing this store as the browser's module map would hand it over.
    const { namespace: sendHook } = await loadModule('extensions/webui/send_message_before/_10_convo_dictation.js',
        { bindings: { '/plugins/_convo/webui/convo-store.js': { store: namespace.store } } });
    // The host's sendMessage, in effect: read the composer, run send_message_before, ask the model check (`checkMs` on the virtual clock),
    // then empty the composer (inputStore.reset()) and send.
    const send = async ({ checkMs = 100 } = {}) => {
        const sendCtx = { message: composerText.trim(), attachments: [], context: 'a', cancel: false };
        await sendHook.default(sendCtx);
        if (sendCtx.cancel) return;
        await new Promise(resolve => setTimer(resolve, checkMs));
        composerText = '';
        manualSends.push(sendCtx.message);
    };

    const orb = { id: 'microphone-button' };
    const fire = (type, extra = {}, key = type) => {
        const e = { type, button: 0, pointerId: 1, isPrimary: true, repeat: false, target: { closest: s => s === '#microphone-button' ? orb : null },
            preventDefault() {}, stopImmediatePropagation() {}, ...extra };
        for (const fn of listeners.get(key) || []) fn(e);
    };
    // Advance in 50 ms steps: due timers, one animation frame, a recorder slice every 250 ms, then let promises settle.
    const advance = async ms => {
        const target = now + ms;
        while (now < target) {
            now = Math.min(target, now + 50);
            for (;;) {
                const due = [...timers].filter(([, t]) => t.at <= now).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
                if (!due) break;
                timers.delete(due[0]); due[1].fn();
                await flush();
            }
            const batch = [...frames]; frames.clear();
            for (const [, fn] of batch) fn();
            for (const recorder of recorders) if (recorder.state === 'recording' && now - recorder.lastSlice >= 250) recorder.emit();
            await flush();
        }
    };
    const restore = () => {
        for (const [key, descriptor] of Object.entries(saved)) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key]; }
        Date.now = savedNow;
        if (savedDocument) Object.defineProperty(globalThis, 'document', savedDocument); else delete globalThis.document;
        resetAnnouncer(); delete globalThis.__convoInstalled;
        void OriginalBlob;
    };
    return {
        store: namespace.store, host, mic, log, composer, sent, manualSends, transcribed, streams, recorders, chunks, status, send,
        speak: value => { level = value; }, advance, restore, fire,
        down: () => fire('pointerdown'), up: () => fire('pointerup'), click: () => fire('click'),
        escape: () => fire('keydown', { key: 'Escape', code: 'Escape' }),
        orbState: () => mic.attrs.get('data-convo'),
        markersBetween: (from, to) => chunks.filter(c => c.at >= from && c.at < to).map(c => c.marker % 256),
        composerText: () => composerText,
        recording: () => streams.filter(stream => !stream.probe),                // microphone captures, not the host's permission probes
        hold() { let release; holdAnswers = { promise: new Promise(resolve => { release = resolve; }) }; return () => { holdAnswers = null; release(); }; },
    };
}
async function withHost(options, run) {
    const r = await hostRig(options);
    try { await run(r); } finally { try { await r.store.stop(); } catch {} r.restore(); }
}

// Speak from the start of the press, stop at 1.5 s, release at 2.1 s: hands-free dictation from the latch time.
async function latchWithEarlySpeech(r) {
    r.speak(0.5); r.down();
    await r.advance(1500);
    r.speak(0);
    await r.advance(600);
    assert.equal(r.store.dictating, true);
    assert.equal(r.orbState(), 'dictating-latched');
    r.up(); r.click();
    await r.advance(50);
}

test('Hands-free (send): after release the host recorder runs on its own voice rules; an utterance ending in silence is sent and it keeps listening', () => withHost({ mode: 'send' }, async r => {
    await latchWithEarlySpeech(r);
    assert.equal(r.host.microphoneInput.manual, false);                  // the recorder's normal voice-activity mode, not a manual recording
    assert.equal(r.transcribed.length, 0);
    await r.advance(900);                                                // under silence_duration (1000 ms) after the release: nothing yet
    assert.equal(r.transcribed.length, 0);
    await r.advance(2600);                                               // silence_duration, then waiting_timeout (2000 ms): the utterance ends
    assert.deepEqual(r.sent, ['utterance 1']);
    assert.deepEqual(r.composer, ['utterance 1']);
    const early = r.markersBetween(300, 1500);
    assert.ok(early.length >= 3, 'slices were recorded while the press was deciding');
    assert.ok(early.every(m => r.transcribed[0].markers.includes(m)), 'speech since the pre-open is in the first utterance');
    assert.equal(r.store.dictating, true);                               // send mode keeps listening
    assert.equal(r.host.microphoneInput.status, 'listening');
    r.speak(0.5); await r.advance(600); r.speak(0);                      // a second utterance, hands-free
    await r.advance(3500);
    assert.deepEqual(r.sent, ['utterance 1', 'utterance 2']);
    assert.equal(r.streams.length, 1);                                   // one stream for the whole dictation
    assert.equal(r.recorders[0].starts >= 1, true);
    r.down(); await r.advance(50); r.up(); r.click(); await r.advance(50); // a tap on the orb turns it off
    assert.equal(r.store.dictating, false);
    assert.equal(r.streams[0].track.stopped, true);
    assert.equal(r.log.includes('convo.start'), false);
}));

test('Hands-free (draft): the first utterance goes to the composer, is not sent, and dictation stops as the host decides', async () => {
    for (const stopStyle of ['inactive', 'dispose']) {
        await withHost({ mode: 'draft', stopStyle }, async r => {
            await latchWithEarlySpeech(r);
            await r.advance(3500);
            assert.deepEqual(r.composer, ['utterance 1'], stopStyle);
            assert.deepEqual(r.sent, [], stopStyle);
            assert.equal(r.store.dictating, false, stopStyle);           // the orb follows the host's stop()
            assert.equal(r.orbState(), 'ready', stopStyle);
            assert.equal(r.streams[0].track.stopped, true, stopStyle);   // and the stream is released
            r.speak(0.5); await r.advance(600); r.speak(0); await r.advance(4000);
            assert.equal(r.transcribed.length, 1, stopStyle);            // a stopped recorder does not start listening again by itself
        });
    }
});

test('While the press is still held between 2 s and 4 s, silence does not end the utterance', () => withHost({ mode: 'send' }, async r => {
    r.speak(0.5); r.down();
    await r.advance(800); r.speak(0);
    await r.advance(2700);                                               // 3.5 s, still held, 2.7 s of silence
    assert.equal(r.store.dictating, true);
    assert.equal(r.transcribed.length, 0);
    assert.equal(r.host.microphoneInput.status, 'recording');
    r.up(); r.click();
    await r.advance(3500);                                               // the thresholds start at the release
    assert.deepEqual(r.sent, ['utterance 1']);
}));

test('Push-to-talk delivers the whole held recording once on release, with the pre-open audio, in send and in draft mode', async () => {
    for (const mode of ['send', 'draft']) {
        await withHost({ mode }, async r => {
            r.speak(0.5); r.down();
            await r.advance(3000); r.speak(0);
            await r.advance(1500);                                       // 4.5 s held, the last 1.5 s silent: no segmentation
            assert.equal(r.orbState(), 'dictating-ptt', mode);
            assert.equal(r.transcribed.length, 0, mode);
            r.up(); r.click();
            await r.advance(200);
            assert.equal(r.transcribed.length, 1, mode);
            assert.deepEqual(r.composer, ['utterance 1'], mode);
            assert.deepEqual(r.sent, mode === 'send' ? ['utterance 1'] : [], mode);
            const early = r.markersBetween(300, 3000);
            assert.ok(early.every(m => r.transcribed[0].markers.includes(m)), mode);
            assert.equal(r.store.dictating, false, mode);
            assert.equal(r.streams.length, 1, mode);
            assert.equal(r.streams[0].track.stopped, true, mode);
            await r.advance(5000);
            assert.equal(r.transcribed.length, 1, mode);                 // nothing more
        });
    }
});

test('A tap still toggles Convo: the pre-open stream is closed, nothing is transcribed', () => withHost({ mode: 'send' }, async r => {
    r.speak(0.5); r.down();
    await r.advance(1000);
    assert.equal(r.streams.length, 1);                                   // pre-opened as a buffer
    r.up(); r.click();
    await r.advance(100);
    assert.equal(r.streams[0].track.stopped, true);
    assert.equal(r.log.filter(e => e === 'convo.start').length, 1);
    assert.equal(r.store.active, true);
    await r.advance(5000);
    assert.deepEqual([r.transcribed.length, r.sent.length, r.composer.length], [0, 0, 0]);
    r.down(); await r.advance(100); r.up(); r.click(); await r.advance(50);   // and off again
    assert.equal(r.store.active, false);
}));

test('Escape during hands-free dictation discards the utterance in progress and sends nothing', () => withHost({ mode: 'send' }, async r => {
    await latchWithEarlySpeech(r);
    r.speak(0.5); await r.advance(400);
    r.escape();
    r.speak(0); await r.advance(5000);
    assert.deepEqual([r.transcribed.length, r.sent.length, r.composer.length], [0, 0, 0]);
    assert.equal(r.store.dictating, false);
    assert.equal(r.streams[0].track.stopped, true);
}));

test('The dictation card offers the native speech-to-text settings only outside the host Config modal', async () => {
    const html = await readWebui('dictation-field.html');
    assert.equal((html.match(/x-show="!\$store\.convoSetup\.inConfigModal"/g) || []).length, 1);     // openConfig() would tear down the modal it is in
    assert.match(html, /@click="\$store\.convoSetup\.openNative\('_whisper_stt'\)">Open native speech-to-text settings</);
    assert.doesNotMatch(html, /localStorage|sessionStorage/);
});

// ---- Draft in composer (continuous): host message_mode draft + Convo voice_message_draft continuous ---------------------------------
const CONTINUOUS = { mode: 'draft', draft: 'continuous', texts: ['one', 'two', 'three'], stopStyle: 'dispose' };
const LABEL = 'Dictating into the composer — keeps listening until you send or stop.';

// A 2.1 s hold in silence, then release: latched hands-free dictation with nothing recorded yet.
async function latchQuietly(r) {
    r.down(); await r.advance(2100);
    assert.equal(r.orbState(), 'dictating-latched');
    r.up(); r.click(); await r.advance(50);
}
// One phrase ended by the host's own thresholds: 600 ms of speech, then silence_duration (1 s) and waiting_timeout (2 s) and the answer.
async function phrase(r) { r.speak(0.5); await r.advance(600); r.speak(0); await r.advance(3500); }

test('Continuous: each finished phrase is added to the composer, nothing is sent, and it keeps listening on one stream', () => withHost(CONTINUOUS, async r => {
    await latchQuietly(r);
    assert.equal(r.mic.attrs.get('aria-label'), LABEL);
    assert.equal(r.host.microphoneInput.status, 'listening');            // the silent hold was not kept, so no blank first phrase
    for (const word of ['one', 'two', 'three']) {
        await phrase(r);
        assert.equal(r.composer.at(-1), word);
        assert.equal(r.store.dictating, true, 'still dictating after ' + word);
        assert.equal(r.host.microphoneInput?.status, 'listening', 'listening again after ' + word);
    }
    assert.equal(r.composerText(), 'one two three ');                    // index.js updateChatInput: separated by one space each
    assert.deepEqual([r.sent, r.manualSends], [[], []]);                 // nothing was sent
    assert.equal(r.transcribed.length, 3);
    assert.equal(r.host.microphoneInput.phrasesAdded, 3);
    assert.equal(r.recording().length, 1);                               // one microphone stream for the whole dictation
    assert.equal(r.recording()[0].track.stopped, false);
    assert.equal(r.mic.attrs.get('aria-label'), LABEL);
    assert.equal(r.log.includes('convo.start'), false);
}));

test('Continuous: a tap stops it, the phrase in progress is added once, and the end says how many phrases were added', () => withHost(CONTINUOUS, async r => {
    await latchQuietly(r);
    await phrase(r); await phrase(r);
    r.speak(0.5); await r.advance(600); r.speak(0); await r.advance(300);   // "three" is still being recorded
    r.down(); await r.advance(50); r.up(); r.click(); await r.advance(200);
    assert.equal(r.composerText(), 'one two three ');
    assert.equal(r.transcribed.length, 3);
    assert.equal(r.store.dictating, false);
    assert.equal(r.recording()[0].track.stopped, true);
    assert.equal(r.store.note, 'Dictation finished · 3 phrases added; nothing was sent.');
    await r.advance(6000);
    assert.deepEqual([r.transcribed.length, r.sent.length, r.manualSends.length], [3, 0, 0]);
}));

test('Continuous: push-to-talk adds its held recording once and stops; nothing is sent', () => withHost(CONTINUOUS, async r => {
    r.speak(0.5); r.down();
    await r.advance(3000); r.speak(0);
    await r.advance(1500);                                               // 4.5 s held, the last 1.5 s silent: still one recording
    assert.equal(r.orbState(), 'dictating-ptt');
    assert.equal(r.transcribed.length, 0);
    r.up(); r.click(); await r.advance(200);
    assert.equal(r.composerText(), 'one ');
    assert.deepEqual(r.sent, []);
    assert.equal(r.store.dictating, false);
    assert.equal(r.recording()[0].track.stopped, true);
    assert.equal(r.store.note, 'Dictation finished · 1 phrase added; nothing was sent.');
    await r.advance(5000);
    assert.equal(r.transcribed.length, 1);
}));

test('Continuous: Escape keeps what was added and drops the phrase in progress, even one already being transcribed', async () => {
    await withHost(CONTINUOUS, async r => {                              // dropped while it is still being recorded
        await latchQuietly(r);
        await phrase(r); await phrase(r);
        r.speak(0.5); await r.advance(400);
        r.escape(); r.speak(0); await r.advance(5000);
        assert.deepEqual([r.composerText(), r.transcribed.length, r.sent.length], ['one two ', 2, 0]);
        assert.equal(r.store.dictating, false);
        assert.equal(r.recording()[0].track.stopped, true);
        assert.equal(r.store.note, 'Dictation cancelled · 2 phrases added; the phrase in progress was dropped. Nothing was sent.');
    });
    await withHost(CONTINUOUS, async r => {                              // dropped while its transcription is in flight
        await latchQuietly(r);
        await phrase(r); await phrase(r);
        const release = r.hold();
        await phrase(r);
        assert.equal(r.host.microphoneInput.status, 'processing');
        r.escape(); await r.advance(50);
        release(); await r.advance(500);
        assert.deepEqual([r.composerText(), r.transcribed.length, r.sent.length], ['one two ', 3, 0]);
        assert.equal(r.store.dictating, false);
    });
});

test('Continuous: a manual send stops it, and a phrase still being transcribed lands in the composer the send emptied', () => withHost(CONTINUOUS, async r => {
    await latchQuietly(r);
    await phrase(r); await phrase(r);
    const release = r.hold();
    await phrase(r);                                                     // "three" is being transcribed when the user sends
    assert.equal(r.host.microphoneInput.status, 'processing');
    const sending = r.send({ checkMs: 100 });
    await r.advance(50);
    assert.equal(r.store.dictating, false);                              // listening stopped at once
    release(); await r.advance(20);                                      // the answer arrives before the host has emptied the composer
    assert.equal(r.composerText(), 'one two ');                          // so it waits instead of being wiped with the sent text
    await r.advance(300); await sending;
    assert.deepEqual(r.manualSends, ['one two']);
    assert.equal(r.composerText(), 'three ');                            // landed in the emptied composer, not sent
    assert.deepEqual(r.sent, []);
    assert.equal(r.recording()[0].track.stopped, true);
    assert.equal(r.host.microphoneInput, null);
    assert.equal(r.store.note, 'Dictation finished when you sent your message · 3 phrases added.');
    await r.advance(6000);
    assert.deepEqual([r.transcribed.length, r.composer.length], [3, 3]);
}));

test('Continuous: a send while nothing is in flight just stops it; ordinary dictation and no dictation ignore sends', async () => {
    await withHost(CONTINUOUS, async r => {
        await latchQuietly(r); await phrase(r);
        const sending = r.send(); await r.advance(300); await sending;
        assert.deepEqual([r.manualSends, r.composerText(), r.store.dictating, r.transcribed.length], [['one'], '', false, 1]);
        assert.equal(r.store.note, 'Dictation finished when you sent your message · 1 phrase added.');
    });
    await withHost({ mode: 'send', texts: ['one'] }, async r => {         // send mode: the host keeps listening after a send by design
        await latchQuietly(r);
        const sending = r.send(); await r.advance(300); await sending;
        assert.equal(r.store.dictating, true);
        assert.equal(r.host.microphoneInput.status, 'listening');
    });
    await withHost(CONTINUOUS, async r => {                              // a send with no dictation running changes nothing
        assert.equal(r.store.composerSent({ message: 'hello' }), false);
        const sending = r.send(); await r.advance(300); await sending;
        assert.deepEqual([r.store.dictating, r.host.microphoneInput, r.store.note], [false, null, '']);
    });
});

test('A host finalTranscriptHandler gets the transcript instead, in continuous drafting too', async () => {
    await withHost(CONTINUOUS, async r => {
        const handled = [];
        await latchQuietly(r);
        r.host.finalTranscriptHandler = async (text, meta) => { handled.push([text, meta]); };
        await phrase(r);
        assert.deepEqual(handled, [['one', { messageMode: 'draft', sendImmediately: false }]]);
        assert.deepEqual([r.composerText(), r.sent], ['', []]);
    });
    await withHost({ mode: 'send', enabled: false, texts: ['one'], stopStyle: 'dispose' }, async r => {   // the host microphone, send mode
        const handled = [];
        await r.host.handleMicrophoneClick(text => { handled.push(text); });
        await phrase(r);
        assert.deepEqual([handled, r.sent, r.composerText()], [['one'], [], '']);
    });
});

test('The host microphone drafts continuously too; a manual send or a second click stops it and gives the button back', async () => {
    const hostMic = { ...CONTINUOUS, enabled: false };
    await withHost(hostMic, async r => {
        await r.host.handleMicrophoneClick(); await r.advance(50);
        assert.equal(r.host.microphoneInput.status, 'listening');
        await phrase(r); await phrase(r);
        assert.deepEqual([r.composerText(), r.sent], ['one two ', []]);
        assert.equal(r.host.microphoneInput.status, 'listening');
        const sending = r.send(); await r.advance(300); await sending;
        assert.deepEqual(r.manualSends, ['one two']);
        assert.equal(r.host.microphoneInput, null);
        assert.equal(r.recording()[0].track.stopped, true);
        assert.ok(r.host.statusNotices > 0, 'the host button is told');
    });
    await withHost(hostMic, async r => {                                  // Escape: added text stays, the phrase in progress is dropped
        await r.host.handleMicrophoneClick(); await r.advance(50);
        await phrase(r);
        r.speak(0.5); await r.advance(400);
        r.escape(); r.speak(0); await r.advance(5000);
        assert.deepEqual([r.host.microphoneInput, r.composerText(), r.transcribed.length, r.sent.length], [null, 'one ', 1, 0]);
        assert.equal(r.recording()[0].track.stopped, true);
    });
    await withHost({ mode: 'send', enabled: false, texts: ['one'], stopStyle: 'dispose' }, async r => {   // ordinary host dictation: Escape is not ours
        await r.host.handleMicrophoneClick(); await r.advance(50);
        r.escape(); await r.advance(50);
        assert.equal(r.host.microphoneInput?.status, 'listening');
    });
    await withHost(hostMic, async r => {
        await r.host.handleMicrophoneClick(); await r.advance(50);
        await phrase(r);
        await r.host.handleMicrophoneClick(); await r.advance(50);       // a second click: the host's own stop()
        assert.equal(r.host.microphoneInput, null);
        assert.equal(r.recording()[0].track.stopped, true);
        r.speak(0.5); await r.advance(600); r.speak(0); await r.advance(4000);
        assert.deepEqual([r.composerText(), r.transcribed.length, r.sent.length], ['one ', 1, 0]);
    });
});

test('Continuous: speech playback starting stops it (the host stops listening); the orb follows and says what was added', () => withHost(CONTINUOUS, async r => {
    await latchQuietly(r);
    await phrase(r); await phrase(r);
    r.host.stop();                                                       // what the host's TTS listener does when speech starts playing
    await r.advance(50);
    assert.equal(r.store.dictating, false);
    assert.equal(r.orbState(), 'ready');
    assert.equal(r.recording()[0].track.stopped, true);
    assert.equal(r.store.note, 'Dictation finished · 2 phrases added; nothing was sent.');
    assert.deepEqual([r.composerText(), r.sent], ['one two ', []]);
}));

test('Continuous: turning Convo off stops it; the text already added stays and the phrase in progress is dropped', async () => {
    for (const how of ['disabled', 'plugin off']) {
        await withHost(CONTINUOUS, async r => {
            await latchQuietly(r); await phrase(r);
            r.speak(0.5); await r.advance(400);                          // a phrase is being recorded
            if (how === 'disabled') r.status.settings.enabled = false; else r.status.fail = true;
            await r.store.refresh(); await r.advance(50);
            r.speak(0); await r.advance(5000);
            assert.equal(r.store.dictating, false, how);
            assert.deepEqual([r.composerText(), r.transcribed.length, r.sent.length], ['one ', 1, 0], how);
            assert.equal(r.recording()[0].track.stopped, true, how);
            assert.equal(r.store.note, 'Dictation stopped because Convo was turned off · 1 phrase added; nothing was sent.', how);
        });
    }
    await withHost({ ...CONTINUOUS, enabled: false }, async r => {        // the host microphone, when the plugin stops answering
        await r.host.handleMicrophoneClick(); await r.advance(50);
        await phrase(r);
        r.speak(0.5); await r.advance(400);
        r.status.fail = true; await r.store.refresh(); await r.advance(50);
        r.speak(0); await r.advance(5000);
        assert.deepEqual([r.host.microphoneInput, r.composerText(), r.transcribed.length], [null, 'one ', 1]);
        assert.equal(r.recording()[0].track.stopped, true);
    });
});

test('A choice saved in the Whisper settings reaches orb dictation at its start, even before the host store has read it', () => withHost({ mode: 'send', draft: 'single', texts: ['one', 'two'], stopStyle: 'dispose' }, async r => {
    r.status.saved = { message_mode: 'draft', voice_message_draft: 'continuous' };   // saved elsewhere; the host store still says "send"
    r.status.settings.voice_message_draft = 'continuous';                // (Convo's next status poll would say so too)
    assert.equal(r.host.config.message_mode, 'send');
    await latchQuietly(r);
    assert.equal(r.host.config.message_mode, 'draft');                   // read from Convo's bridge at the start, without the host's refresh
    assert.equal(r.store.settings.voice_message_draft, 'continuous');
    assert.equal(r.mic.attrs.get('aria-label'), LABEL);
    await phrase(r); await phrase(r);
    assert.deepEqual([r.composerText(), r.sent, r.store.dictating], ['one two ', [], true]);
}));

test('Single draft is unchanged: the first phrase goes to the composer through the host, and the host stops', () => withHost({ mode: 'draft', draft: 'single', texts: ['one'], stopStyle: 'dispose' }, async r => {
    await latchQuietly(r);
    assert.equal(r.mic.attrs.get('aria-label'), 'Hands-free dictation. Listening for speech; click to stop, Escape cancels.');
    await phrase(r);
    assert.deepEqual([r.composerText(), r.sent], ['one ', []]);
    assert.equal(r.store.dictating, false);
    assert.equal(r.host.microphoneInput, null);
}));
