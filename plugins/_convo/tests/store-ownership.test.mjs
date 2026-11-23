// The Convo store's side of "sound on the device you choose" (convo-store.js on stubbed host modules): a dropped connection re-attaches
// to the same conversation with the same microphone, a refusal or the grace ending stops it; a network blip in the status poll does not
// stop it but the plugin turned off does; who owns voice is shown with "Take over here"; a page that was taken over stops and never
// restarts itself; Agent Zero's own read-aloud is skipped only on the owning page; the per-device key is the microphone in use and the
// old `default|default` values move to it once; Record raw and sent saves only on press.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadModule, hostStubs } from './vm-loader.mjs';

const settle = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
const memory = () => { const values = new Map(); return { values, getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key) }; };

async function fixture({ settings = {}, answer = null, status = {}, speech = null, globals: extra = {} } = {}) {
    const requests = [], audios = [], sockets = [], api = [];
    let statusError = null;
    const bindings = hostStubs({
        '/js/api.js': { callJsonApi: async (url, payload) => {
            api.push(payload);
            if (payload.action === 'status') {
                if (statusError) throw statusError;
                return { settings: { enabled: true, max_audio_seconds: 20, ...settings }, runtime: { mode: 'external' }, ...status };
            }
            if (payload.mime_type === 'audio/wav') return { text: 'Hello there' };      // Agent Zero's own /transcribe
            if (payload.action === 'recording_save') return { ok: true, id: 'r1', analysis: { raw_s: 1.2, sent_s: 1, clipped_start_ms: 150, silenced: [{}] } };
            return { events: [], jobs: [] };
        } },
        '/js/websocket.js': { createNamespacedClient: () => {
            const socket = { handlers: {}, disconnects: [], addHandlers() {}, async on(name, fn) { this.handlers[name] = fn; }, onDisconnect(fn) { this.disconnects.push(fn); }, disconnect() { this.closed = true; },
                async request(event, data) {
                    requests.push({ event, data });
                    const custom = await answer?.(event, data, socket);
                    if (custom instanceof Error) throw custom;
                    if (custom) return { results: [{ handlerId: 'plugin.Realtime', ok: true, data: custom }] };
                    return { results: [{ handlerId: 'plugin.Realtime', ok: true, data: { ok: true, session: { id: 's', epoch: 0 } } }] };
                } };
            sockets.push(socket);
            return socket;
        } },
        '/js/shortcuts.js': { getCurrentContextId: () => 'a', getCurrentContext: () => ({ id: 'a', name: 'Chat a' }), openModal: async () => {} },
        './audio.js': { AudioSession: class {
            constructor(options) { Object.assign(this, options); audios.push(this); this.closed = false; }
            async start() {} interrupt() { this.interrupted = (this.interrupted || 0) + 1; } setPolicyHold() {} close() { this.closed = true; }
            inputDeviceId() { return 'usb-mic'; } setSound(values) { this.applied = values; }
            async startRawTap() { this.tapping = true; return true; } takeRawTap() { this.tapping = false; return { rate: 48000, chunks: [], audio: 'UklGRraw' }; }
        } },
        '/plugins/_whisper_stt/webui/whisper-stt-store.js': { store: { microphoneInput: null, getSelectedDevice: () => ({ deviceId: 'host-mic' }), async initRuntime() {} } },
        ...(speech ? { '/components/chat/speech/speech-store.js': { store: speech } } : {}),
    });
    const globals = {
        document: { hidden: false, addEventListener() {}, getElementById: () => null }, window: { addEventListener() {}, confirm: () => true },
        fetch: async () => ({ ok: true }), dispatchEvent() {}, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
        ...extra,
    };
    const { namespace } = await loadModule('webui/convo-store.js', { bindings, globals });
    return { store: namespace.store, namespace, requests, audios, sockets, api, failStatus: error => { statusError = error; } };
}

test('A dropped connection re-attaches to the same conversation with the same microphone; nothing is sent meanwhile', async () => {
    let attachTries = 0;
    const f = await fixture({ answer: event => {
        if (event === 'convo.attach') { attachTries++; return attachTries < 3 ? new Error('Not connected') : { ok: true, session: { id: 's', epoch: 4 } }; }
    } });
    f.store.reattachRetryMs = 5;
    await f.store.start();
    const start = f.requests.find(item => item.event === 'convo.start');
    assert.match(start.data.page, /^[A-Za-z0-9-]{8,64}$/, 'a stable page id');
    assert.equal(start.data.page, f.namespace.pageId());
    assert.equal(typeof start.data.device, 'string');
    const [audio] = f.audios, [socket] = f.sockets;
    assert.equal(f.store.reattachGraceMs, 18000, 'the page gives up a little before the server does');
    const pending = f.store.reattach(socket);                      // what onDisconnect calls
    assert.equal(f.store.reconnecting, true);
    audio.onStart();                                               // speech while re-attaching is not sent
    audio.onTurn('UklGRg==', { voiced_s: 1 });
    assert.equal(await pending, true);
    assert.equal(attachTries, 3, 'retried until the connection came back');
    assert.deepEqual([f.store.active, f.store.reconnecting, f.store.session.epoch], [true, false, 4]);
    assert.equal(audio.closed, false, 'the same microphone');
    assert.equal(f.audios.length, 1);
    assert.equal(f.requests.filter(item => item.event === 'convo.candidate').length, 0);
    const attach = f.requests.find(item => item.event === 'convo.attach');
    assert.deepEqual({ ...attach.data }, { session_id: 's', epoch: 0, page: f.namespace.pageId() });
    assert.ok(socket.disconnects.length, 'the disconnect handler is installed');
    await f.store.stop();
});

test('A server that no longer knows the conversation stops it at once; so does the grace ending', async () => {
    const refused = await fixture({ answer: event => event === 'convo.attach' ? { ok: false, unknown: true } : null });
    await refused.store.start();
    assert.equal(await refused.store.reattach(refused.sockets[0]), false);
    assert.equal(refused.store.active, false);
    assert.equal(refused.audios[0].closed, true);
    assert.match(refused.store.error, /ended while the connection was down/);
    const late = await fixture({ answer: event => event === 'convo.attach' ? new Error('Request timeout') : null });
    late.store.reattachRetryMs = 5; late.store.reattachGraceMs = 30;
    await late.store.start();
    assert.equal(await late.store.reattach(late.sockets[0]), false);
    assert.equal(late.store.active, false);
    assert.match(late.store.error, /could not reconnect in time/);
});

test('A status poll that never reached the server keeps the conversation; an answer from the server (plugin off) stops it', async () => {
    const f = await fixture();
    await f.store.start();
    f.failStatus(Object.assign(new TypeError('Failed to fetch')));
    await f.store.refresh();
    assert.equal(f.store.active, true, 'a phone asleep or changing network is not the end');
    f.failStatus(new Error('Not Found'));
    await f.store.refresh();
    assert.equal(f.store.active, false, 'the plugin\'s route gone: stopped at once');
});

test('Who owns voice is shown with Take over here; taking over sends the running conversation\'s id', async () => {
    const f = await fixture({ status: { voice_owner: { session: 'other', page: 'page-phone-0001', device: 'Chrome on Android', since: 1700000000, target: 'a' } } });
    await f.store.refresh();
    assert.equal(f.store.ownerElsewhere, true);
    assert.match(f.store.ownerText, /^Voice is running on Chrome on Android \(since .+, in this chat\)\.$/);
    assert.equal(await f.store.takeOver(), true);
    const start = f.requests.find(item => item.event === 'convo.start');
    assert.equal(start.data.takeover, 'other');
    assert.equal(f.store.ownerElsewhere, false);
    await f.store.stop();
    const html = await (await import('node:fs/promises')).readFile(new URL('../webui/main.html', import.meta.url), 'utf8');
    assert.match(html, /@click="\$store\.convo\.takeOver\(\)">Take over here<\/button>/);
});

test('A page that was taken over stops, says where voice went, and never starts again by itself', async () => {
    const f = await fixture();
    await f.store.start();
    f.sockets[0].handlers['convo.taken_over']({ data: { session_id: 's', device: 'Edge on Windows' } });
    await settle(5);
    assert.equal(f.store.active, false);
    assert.equal(f.store.movedTo, 'Edge on Windows');
    assert.match(f.store.note, /Voice moved to Edge on Windows/);
    await settle(30);
    assert.equal(f.requests.filter(item => item.event === 'convo.start').length, 1, 'no restart');
    assert.equal(f.requests.filter(item => item.event === 'convo.attach').length, 0, 'no re-attach either');
});

test('Agent Zero\'s read-aloud is skipped only while this page owns a conversation; an explicit speak still speaks; the preference is untouched', async () => {
    const spoken = [];
    const speech = { preference: true, speakStream(id, text) { spoken.push([id, text]); return Promise.resolve(); }, speak(text) { return this.speakStream(Math.random(), text, true); } };
    const f = await fixture({ speech });
    speech.speakStream('auto-1', 'before');
    await f.store.start();
    await settle(5);
    speech.speakStream('auto-2', 'during');                         // the host's own read-aloud of a new message
    speech.speak('pressed');                                        // the speak button
    assert.deepEqual(spoken.map(item => item[1]), ['before', 'pressed']);
    assert.equal(f.namespace.audioSnapshot().read_aloud_skipped, 1);
    await f.store.stop();
    speech.speakStream('auto-3', 'after');
    assert.deepEqual(spoken.map(item => item[1]), ['before', 'pressed', 'after']);
    assert.equal(speech.preference, true);
    const other = f.namespace.guardSpeech(speech, () => true);
    assert.equal(other, f.namespace.guardSpeech(speech, () => true), 'installed once');
    assert.equal(f.namespace.guardSpeech({}, () => true), null);
});

test('The per-device key is the microphone in use; the old default|default values move to it once and stay for older pages', async () => {
    const localStorage = memory();
    localStorage.setItem('convo.sound.v1:default|default', JSON.stringify({ noise_gate_db: -41 }));
    const f = await fixture({ globals: { localStorage } });
    await f.store.start();
    assert.equal(f.store.soundKey(), 'convo.sound.v1:usb-mic|default', 'the live track\'s device, not the host setting (host-mic)');
    assert.equal(f.store.soundValues().noise_gate_db, -41);
    assert.equal(localStorage.getItem('convo.sound.v1:usb-mic|default'), JSON.stringify({ noise_gate_db: -41 }));
    assert.equal(localStorage.getItem('convo.sound.v1:default|default'), JSON.stringify({ noise_gate_db: -41 }), 'kept for rollback');
    assert.equal(localStorage.getItem('convo.sound.v1.migrated'), 'convo.sound.v1:usb-mic|default');
    // An output chosen for this microphone is part of the pair.
    localStorage.setItem('convo.device.v1:usb-mic', JSON.stringify({ output: 'hdmi' }));
    assert.equal(f.store.soundKey(), 'convo.sound.v1:usb-mic|hdmi');
    assert.deepEqual(f.store.soundValues().noise_gate_db, -50, 'a new pair starts from the defaults (migrated only once)');
    await f.store.stop();
    // The capture opened with this microphone's processing choices (the stub records the options).
    localStorage.setItem('convo.device.v1:host-mic', JSON.stringify({ noise_suppression: false }));
    await f.store.start();
    assert.deepEqual({ ...f.audios.at(-1).processing }, { echoCancellation: true, noiseSuppression: false, autoGainControl: true });
    await f.store.stop();
});

test('Record raw and sent: developer mode and a running conversation only; the next utterance is saved through recording_save', async () => {
    const plain = await fixture();
    await plain.store.start();
    assert.match(await plain.store.recordRawAndSent(), /developer tool/);
    assert.equal(plain.api.filter(item => item.action === 'recording_save').length, 0);
    await plain.store.stop();
    const f = await fixture({ settings: { developer_mode: true } });
    await f.store.refresh();
    assert.match(await f.store.recordRawAndSent(), /Start a conversation first/);
    await f.store.start();
    const audio = f.audios[0];
    const saving = f.store.recordRawAndSent();
    await settle(0);
    assert.equal(audio.tapping, true);
    assert.equal(f.api.filter(item => item.action === 'recording_save').length, 0, 'nothing written before the utterance ends');
    audio.onStart();
    audio.onTurn('UklGRsent', { voiced_s: 1, pre_roll_s: .4 });
    const message = await saving;
    assert.match(message, /^Saved: raw 1\.2 s, sent 1\.0 s, clipped start 150 ms, 1 withheld or gated stretch\.$/);
    const save = f.api.find(item => item.action === 'recording_save');
    assert.deepEqual([save.raw, save.sent, save.pre_roll_s], ['UklGRraw', 'UklGRsent', .4]);
    assert.equal(f.requests.filter(item => item.event === 'convo.candidate' && item.data.final).length, 1, 'the utterance is answered as usual');
    // A stop while waiting saves nothing.
    const waiting = f.store.recordRawAndSent();
    await settle(0);
    await f.store.stop();
    assert.match(await waiting, /Nothing was recorded/);
    assert.equal(f.api.filter(item => item.action === 'recording_save').length, 1);
});

test('No extra microphones: a touch press never pre-opens one; a mouse press past the pre-open time still does', async () => {
    const { bindOrbGesture, PRE_OPEN_MS } = await import('../webui/dictation.js');
    for (const [pointerType, expected] of [['touch', 0], ['mouse', 1]]) {
        const listeners = {}, calls = { preopen: 0, toggles: 0 };
        const document = { addEventListener(type, fn) { (listeners[type] ||= []).push(fn); }, removeEventListener() {}, hidden: false };
        const window = { addEventListener() {}, removeEventListener() {} };
        const store = { enabled: true, dictationMode: '', setPressing() {}, preOpenDictation() { calls.preopen++; return Promise.resolve(); },
            discardDictationBuffer() {}, dictationBufferSettled() { return null; }, toggle() { calls.toggles++; } };
        const orb = { closest: () => orb };
        const bound = bindOrbGesture({ store, document, window });
        const event = { target: orb, button: 0, pointerId: 1, pointerType, isPrimary: true, preventDefault() {}, stopImmediatePropagation() {} };
        for (const fn of listeners.pointerdown) fn(event);
        await settle(PRE_OPEN_MS + 60);
        for (const fn of listeners.pointerup) fn(event);
        assert.equal(calls.preopen, expected, pointerType);
        assert.equal(calls.toggles, 1, 'still a tap');
        bound.dispose();
    }
});

test('A duplicated tab gets its own page id; a reload of the same tab keeps it (review 2026-11-17)', async () => {
    const pageEvents = () => { const listeners = {}; return { listeners, addEventListener(type, fn) { (listeners[type] ||= []).push(fn); } }; };
    const copy = storage => { const twin = memory(); for (const [key, value] of storage.values) twin.values.set(key, value); return twin; };
    const original = memory(), first = pageEvents();
    const a = await fixture({ globals: { sessionStorage: original, addEventListener: first.addEventListener } });
    const id = a.namespace.pageId();
    assert.match(id, /^[A-Za-z0-9-]{8,64}$/);
    // Duplicate tab: sessionStorage is copied while the first page is still open.
    const b = await fixture({ globals: { sessionStorage: copy(original), addEventListener() {} } });
    assert.notEqual(b.namespace.pageId(), id, 'the copy takes its own id');
    // Reload: the page is left first (pagehide), then the same storage loads again.
    for (const fn of first.listeners.pagehide || []) fn({ persisted: false });
    const c = await fixture({ globals: { sessionStorage: original, addEventListener() {} } });
    assert.equal(c.namespace.pageId(), id, 'a reload keeps the id, so the conversation can re-attach');
});

test('A Whisper request the server asked for carries the turn\'s own epoch after a barge-in (review 2026-11-17)', async () => {
    const f = await fixture({ settings: { transcription_enabled: true }, globals: { justToast() {} } });
    await f.store.start();
    const [socket] = f.sockets;
    f.store.session = { id: 's', epoch: 3 };                        // a barge-in moved the session on since the turn (epoch 1)
    socket.handlers['convo.event']({ data: { session_id: 's', epoch: 3, turn_id: 't1', type: 'transcript.wanted', audio: 'UklGRg==', turn_epoch: 1 } });
    await settle(20);
    const sent = f.requests.find(item => item.event === 'convo.transcript');
    assert.ok(sent, 'the words were posted');
    assert.deepEqual([sent.data.turn_id, sent.data.epoch, sent.data.text], ['t1', 1, 'Hello there']);
    await f.store.stop();
});

test('A text-only turn finished while the page re-attaches is not sent (review 2026-11-17)', async () => {
    const f = await fixture({ settings: { text_input_required: true }, answer: event => (event === 'convo.attach' ? new Error('Not connected') : null) });
    f.store.reattachRetryMs = 5;
    await f.store.start();
    const [audio] = f.audios, [socket] = f.sockets;
    audio.onStart();
    const pending = f.store.reattach(socket);
    audio.onTurn('UklGRg==', { voiced_s: 1 });
    await settle(10);
    assert.equal(f.requests.filter(item => item.event === 'convo.whisper' || item.event === 'convo.candidate').length, 0);
    await f.store.stop();
    await pending;
});
