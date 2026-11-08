// A started Convo browser store on stubbed host modules: the real convo-store.js, orb.js and view-helpers.js,
// a recording socket, a scriptable audio session and a fake host microphone button.
import { loadModule, hostStubs } from './vm-loader.mjs';

export function fakeMic(attributes = {}) {
    const attrs = new Map(Object.entries(attributes)), children = [], writes = [];
    const style = { props: new Map(), setProperty(name, value) { this.props.set(name, value); writes.push(['style', name, value]); }, removeProperty(name) { this.props.delete(name); } };
    const mic = {
        attrs, children, writes, style,
        getAttribute: name => attrs.has(name) ? attrs.get(name) : null,
        hasAttribute: name => attrs.has(name),
        setAttribute(name, value) { attrs.set(name, String(value)); writes.push(['attr', name, String(value)]); },
        removeAttribute(name) { attrs.delete(name); },
        querySelector: selector => selector === ':scope > .convo-orb' ? children.find(child => child.className === 'convo-orb') || null : null,
        appendChild(child) { children.push(child); return child; },
        ownerDocument: { createElement: () => {
            const element = { className: '', innerHTML: '', setAttribute() {}, remove() { children.splice(children.indexOf(element), 1); } };
            return element;
        } },
    };
    return mic;
}

// `hostConfig`: the host Whisper store's config (its message_mode decides, with settings.voice_message_draft, continuous drafting).
export async function storeFixture({ settings = {}, startFails = false, performance = undefined, hostConfig = undefined } = {}) {
    const mic = fakeMic({ 'aria-label': 'Microphone standby', 'aria-pressed': 'false' }), cueNames = [], calls = [], requests = [], audios = [];
    const handlers = {};
    const nativeMic = { async toggle() {}, dispose() {}, async finishConvoDictation() {} };
    const nativeStore = { microphoneInput: null, config: hostConfig, getSelectedDevice: () => ({ deviceId: 'mic' }), async initRuntime() {}, async initMicrophone() { this.microphoneInput = nativeMic; } };
    const bindings = hostStubs({
        '/js/api.js': { callJsonApi: async (_url, payload) =>
            payload.action === 'status' ? { settings: { enabled: true, max_audio_seconds: 20, ...settings }, runtime: { mode: 'external' } } : { events: [], jobs: [] } },
        '/js/websocket.js': { createNamespacedClient: () => ({ addHandlers() {}, async on(name, fn) { handlers[name] = fn; }, onDisconnect() {}, disconnect() {},
            async request(event, data) {
                calls.push(event); requests.push({ event, data });
                if (startFails && event === 'convo.start') return { results: [{ handlerId: 'plugin.Realtime', ok: false, error: { error: 'Provider unreachable' } }] };
                return { results: [{ handlerId: 'plugin.Realtime', ok: true, data: { ok: true, session: { id: 's', epoch: 0 } } }] };
            } }) },
        '/js/shortcuts.js': { getCurrentContextId: () => 'a', getCurrentContext: () => ({ id: 'a', name: 'Chat a' }), openModal: async () => {} },
        './audio.js': { AudioSession: class {
            constructor(options) { Object.assign(this, options); audios.push(this); }
            async start() {} interrupt() { this.interrupted = (this.interrupted || 0) + 1; } setPolicyHold() {} close() {}
            pcmBegin(_event, done, started) { this.done = done; this.started = started; return true; } pcmChunk() {} pcmEnd() {} pcmAbort() {}
            enqueue(_audio, done, started) { this.done = done; this.started = started; } } },
        '/plugins/_whisper_stt/webui/whisper-stt-store.js': { store: nativeStore },
    });
    const globals = {
        document: { hidden: false, addEventListener() {}, getElementById: id => id === 'microphone-button' ? mic : null }, window: { addEventListener() {}, confirm: () => true },
        fetch: async () => ({ ok: true }), dispatchEvent: event => cueNames.push(event.detail.name),
        CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    };
    if (performance) globals.performance = performance;
    const { namespace } = await loadModule('webui/convo-store.js', { bindings, globals });
    return { store: namespace.store, namespace, mic, cueNames, calls, requests, audios, handlers, nativeStore };
}
