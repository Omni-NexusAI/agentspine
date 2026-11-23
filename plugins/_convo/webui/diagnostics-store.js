// Developer diagnostics: a second canvas surface (`convo-diagnostics`) that exists only while the saved `developer_mode` is on,
// and Convo's own pop-out window for it. Observational only: it reads timing marks (`metrics`, cursor-based, developer mode
// only), this page's own marks, the playback snapshot and the echo tap; it never changes capture, playback or admission.
//
// It works only while someone can see it: polling (1 s with a voice session, 4 s without), the 60-reading echo tap and the
// level meters run while the docked surface is visible or the window is open, and never in a hidden tab. Tunables are
// edited through the same revision-aware convoSetup draft as Settings (diagnostics.html, Tune).
import { createStore } from '/js/AlpineStore.js';
import { callJsonApi } from '/js/api.js';
import { store as convo, taps, audioSnapshot } from '/plugins/_convo/webui/convo-store.js';
import { defaults } from '/plugins/_convo/webui/defaults.js';
import { buildView, createModel, echoView, formatMs, ingest, inspectView, memoryView, probability, pushEcho } from '/plugins/_convo/webui/diagnostics-model.js';
import { buildWindow, createFloatingWindow } from '/plugins/_convo/webui/floating-window.js';

export const SURFACE = 'convo-diagnostics';
export const POLL_SESSION_MS = 1000, POLL_IDLE_MS = 4000, LEVEL_MS = 250, LEVEL_REDUCED_MS = 1000, MORE_READS = 5;
export const KEYS = { geometry: 'convo.diagnostics.window', floating: 'convo.diagnostics.floating', seen: 'convo.diagnostics.enabled', sections: 'convo.diagnostics.sections' };
const endpoint = '/plugins/_convo/conversation';
const COMPONENT = '/plugins/_convo/webui/diagnostics.html';

// Owned, non-reactive resources: Alpine must not proxy the canvas store, timers, the model or DOM nodes.
let canvas = null, registered = false, active = false, generation = 0, more = false, pollTimer = null, levelTimer = null;
let model = createModel(), frame = null, controller = null, opener = null;
const ring = [], panels = new Set();
const onEcho = metric => { pushEcho(ring, metric); };
const clock = () => globalThis.performance?.now?.() ?? Date.now();

function storage() { try { return globalThis.localStorage || null; } catch { return null; } }
function readFlag(key) { try { return storage()?.getItem(key) === '1'; } catch { return false; } }
function writeFlag(key, on) { try { storage()?.setItem(key, on ? '1' : '0'); } catch { /* storage is a convenience */ } }
function reducedMotion() { try { return !!globalThis.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches; } catch { return false; } }
function pageHidden() { return !!globalThis.document?.hidden; }
function toast(message, kind = 'info') { try { globalThis.justToast?.(message, kind); } catch { /* best effort */ } }
// The canvas can show the surface docked: a desktop-width page with a chat (otherwise the host would want a modal path).
function dockable() { return !!canvas && !canvas.isMobileMode && canvas.shouldRender?.() !== false; }
function hostModalOpen() { try { return !!globalThis.document?.querySelector?.('.modal.show'); } catch { return false; } }
// Focus once the target exists (a docked panel renders after its component has been fetched).
function focusWhenReady(pick, tries = 20) {
    setTimeout(() => {
        const target = pick();
        if (target?.isConnected && typeof target.focus === 'function') target.focus();
        else if (tries > 1) focusWhenReady(pick, tries - 1);
    }, 50);
}
const dockedButton = () => globalThis.document?.querySelector?.(`[data-surface-id="${SURFACE}"] [data-diag-popout]`) || null;
const sessionLive = () => !!(convo.active || convo.busy);
const safe = (run, fallback) => { try { return run(); } catch { return fallback; } };

export const store = createStore('convoDiagnostics', {
    available: false, floating: false, dockedVisible: false, view: null, live: null, error: '', selectedTurn: '', tone: null,
    sections: { live: true, turns: true, gate: true, speculation: true, rolling: true, context: true, saw: false, memory: false, clips: false, tune: false },
    lastRequest: null,                       // `metrics` last_request: what the conversation model was sent last (developer mode only)
    clipLabel: 'request',                    // the label the test-clip recorder arms with
    // "What the model saw / wrote" (`inspect_turns`, developer mode, memory only on the server): read again only when the server's
    // `inspect_revision` changed and the section is open. Saved turns (`replay_*`) are written only on "Save for replay".
    inspected: [], inspectRevision: -1, inspectNote: '', replays: [], openTurn: '',
    memory: null, memoryNote: '',            // `memory_view` for the conversation's chat, read when the Memory section opens or on Refresh

    // "What the model saw" as text: the system text, then each message by role; audio is shown as "[audio 2.3 s]", never played.
    modelSawText() {
        const request = this.lastRequest;
        if (!request || typeof request !== 'object') return '';
        const lines = [];
        if (request.system) lines.push('SYSTEM:\n' + String(request.system));
        for (const message of Array.isArray(request.messages) ? request.messages.slice(-60) : []) lines.push(String(message?.role || '?').toUpperCase() + ':\n' + String(message?.text ?? ''));
        if (Number(request.audio_s) > 0) lines.push(`[audio ${Number(request.audio_s).toFixed(1)} s]`);
        if (Array.isArray(request.tools) && request.tools.length) lines.push('TOOLS: ' + request.tools.map(tool => String(tool?.name ?? tool)).join(', '));
        return lines.join('\n\n').slice(0, 200000);
    },
    modelSawWhen() { const at = Number(this.lastRequest?.at); return Number.isFinite(at) && at > 0 ? new Date(at * 1000).toLocaleTimeString() : ''; },
    wallTime(seconds) { const at = Number(seconds); return Number.isFinite(at) && at > 0 ? new Date(at * 1000).toLocaleTimeString() : ''; },
    openInspected(turn) { this.openTurn = this.openTurn === turn ? '' : turn; },
    async loadInspected() {
        try {
            const result = await callJsonApi(endpoint, { action: 'inspect_turns' });
            if (!result?.ok) { this.inspectNote = result?.error || 'The turns could not be read.'; return false; }
            this.inspected = inspectView(result.turns);
            this.inspectRevision = Number.isFinite(result.revision) ? result.revision : this.inspectRevision;
            this.inspectNote = '';
            return true;
        } catch (error) { this.inspectNote = 'The turns could not be read: ' + (error?.message || String(error)); return false; }
    },
    async replayCall(action, payload = {}) {
        try {
            const result = await callJsonApi(endpoint, { action, ...payload });
            if (Array.isArray(result?.saved)) this.replays = result.saved;
            if (!result?.ok && result?.error) this.inspectNote = result.error;
            return result || null;
        } catch (error) { this.inspectNote = 'Saved turns could not be read: ' + (error?.message || String(error)); return null; }
    },
    async replaySave(turn) {
        const result = await this.replayCall('replay_save', { turn });
        if (result?.ok) this.inspectNote = 'Saved for replay as ' + result.id + ' (with its audio) in Convo’s data folder on the Agent Zero host.';
        return !!result?.ok;
    },
    replayList() { return this.replayCall('replay_list'); },
    replayDelete(id) { return this.replayCall('replay_delete', { id }); },
    async loadMemory() {
        const target = convo.target || '';
        if (!target) { this.memory = null; this.memoryNote = 'Choose a chat first: memory belongs to one conversation.'; return false; }
        try {
            const result = await callJsonApi(endpoint, { action: 'memory_view', target });
            if (!result?.ok) { this.memory = null; this.memoryNote = result?.error || 'Memory could not be read.'; return false; }
            this.memory = memoryView(result);
            this.memoryNote = '';
            return true;
        } catch (error) { this.memory = null; this.memoryNote = 'Memory could not be read: ' + (error?.message || String(error)); return false; }
    },

    // ---- registration: only while the saved developer_mode is on
    // `watch(fn)` re-runs fn when the settings change: Alpine's effect in the page, a test's own trigger otherwise.
    bindCanvas(host, { watch } = {}) {
        canvas = host;
        try {                                                  // which sections were open: known names and true/false only
            const stored = Object.entries(JSON.parse(storage()?.getItem(KEYS.sections) || '{}') || {});
            this.sections = { ...this.sections, ...Object.fromEntries(stored.filter(([name, open]) => name in this.sections && typeof open === 'boolean')) };
        } catch { /* defaults */ }
        // Seen on last time and the settings are not loaded yet: register now so a reload can restore the surface where it
        // was; the first settings read confirms it or takes it away again.
        if (!convo.settings && readFlag(KEYS.seen)) this.register();
        this.sync();
        (watch || defaultWatch)(() => this.sync());
        globalThis.document?.addEventListener?.('visibilitychange', () => this.refreshActivity());
    },
    sync() {
        const settings = convo.settings;
        if (!settings) return;                                 // not read yet, or unreadable: keep what is there
        const on = !!settings.developer_mode;
        if (readFlag(KEYS.seen) !== on) writeFlag(KEYS.seen, on);
        if (on && !registered) this.register();
        if (on && !this.floating && readFlag(KEYS.floating)) this.popOut({ focus: false });   // a window left open before a reload
        if (!on && registered) void this.unregister();
    },
    register() {
        if (!canvas || registered) return;
        registered = true; this.available = true;
        // No modalPath: the host would offer its own floating modal, which hides behind other modals. Convo's window is the pop-out.
        canvas.registerSurface({ id: SURFACE, title: 'Convo diagnostics', icon: 'monitoring', order: 66,
            canOpen: payload => this.canOpen(payload), open: payload => this.surfaceOpened(payload), close: () => {} });
    },
    // The host has no unregister: take the entry out of the rail, unmount it and close it. The host's own registry keeps a
    // copy (getSurface falls back to it), so canOpen refuses from now on.
    async unregister() {
        registered = false; this.available = false;
        this.closeWindow({ focus: false });
        const host = canvas;
        if (host) {
            const showing = host.activeSurfaceId === SURFACE;
            if (showing && host.isOpen) await host.close();
            const index = (host.surfaces || []).findIndex(item => item.id === SURFACE);
            if (index >= 0) host.surfaces.splice(index, 1);
            host.markSurfaceUnmounted?.(SURFACE);
            if (showing) { host.ensureActiveSurface?.(); host.persist?.(); }
        }
        this.refreshActivity();
    },
    canOpen() {
        if (!registered) return false;
        if (!dockable()) { this.popOut(); return false; }      // a phone-width page or no chat: the window (a bottom sheet when narrow)
        return true;
    },
    surfaceOpened() { if (this.floating) this.closeWindow({ focus: false }); this.refreshActivity(); },
    // Docked visibility comes from the wrapper's x-effect (the host never calls close() when the canvas closes). Write without
    // reading, so the effect does not depend on this value; act in a microtask, outside the effect.
    setDockedVisible(value) {
        this.dockedVisible = !!value;
        queueMicrotask(() => this.refreshActivity());
    },

    // ---- showing it
    async open() {
        if (!registered) { toast('Turn on Developer diagnostics in Convo settings and save first.'); return false; }
        if (this.floating && frame) { focusWhenReady(() => frame?.querySelector?.('[data-drag-handle]')); return true; }
        if (!dockable()) return this.popOut();
        const opened = await canvas.open(SURFACE);
        if (opened && hostModalOpen()) toast('Convo diagnostics is open in the side canvas; close this dialog to see it.');
        return opened;
    },
    popOut({ focus = true } = {}) {
        if (!registered) return false;
        const current = globalThis.document?.activeElement;
        if (current && typeof current.focus === 'function' && !frame?.contains?.(current)) opener = current;
        if (canvas?.activeSurfaceId === SURFACE && canvas.isOpen) void canvas.close();   // one place at a time
        if (!this.floating) { this.floating = true; writeFlag(KEYS.floating, true); }
        if (!frame) mountWindow(this);
        if (focus) focusWhenReady(() => frame?.querySelector?.('[data-drag-handle]'));
        this.refreshActivity();
        return true;
    },
    // Back into the canvas (Escape, or the dock button); focus returns to where the user was.
    async dock() {
        const back = opener;
        this.closeWindow({ focus: false });
        if (registered && dockable()) { await canvas.open(SURFACE); focusWhenReady(() => dockedButton() || back); }
        else focusWhenReady(() => back);
    },
    closeWindow({ focus = true } = {}) {
        const had = !!frame;
        controller?.destroy(); controller = null;
        frame?.remove(); frame = null;
        if (this.floating) { this.floating = false; writeFlag(KEYS.floating, false); }
        if (had && focus) focusWhenReady(() => opener);
        this.refreshActivity();
    },

    // ---- the panel's own state
    toggle(section) {
        this.sections = { ...this.sections, [section]: !this.sections[section] };
        try { storage()?.setItem(KEYS.sections, JSON.stringify(this.sections)); } catch { /* convenience only */ }
    },
    select(turn) { this.selectedTurn = this.selectedTurn === turn ? '' : turn; render(); },
    bindPanel(root) { if (root) panels.add(root); paintLevels(); },
    unbindPanel(root) { panels.delete(root); },
    ms: (value, uncertainty) => formatMs(value, uncertainty),
    prob: value => probability(value),
    clockTime(at) {
        const origin = globalThis.performance?.timeOrigin;
        if (!Number.isFinite(at) || !Number.isFinite(origin)) return '';
        return new Date(origin + at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    },

    // ---- activity: polling, the echo tap and the meters run only while it can be seen
    get running() { return active; },
    refreshActivity() {
        const run = registered && !pageHidden() && (this.floating ? !!frame : this.dockedVisible);
        if (run && !active) start(); else if (!run && active) stop();
    },
    // One read now (tests and the "refresh" button); the schedule carries on as before.
    async pollNow() { await poll(); },
    // "Play test sound": a short soft tone through the conversation's own output (or a temporary one without a voice session), so
    // a silent output can be told apart from a silent reply. Says where it went; whether it was heard is for the listener.
    async testSound() {
        if (this.tone?.busy) return false;
        this.tone = { busy: true, ok: false, text: 'Playing a short tone…' };
        let result;
        try { result = await convo.playTestSound?.(); } catch (error) { result = { ok: false, message: error?.message || String(error) }; }
        result = result || { ok: false, message: 'Convo cannot play a test sound in this page.' };
        const device = result.sink && result.sink !== 'default' ? `output device ${result.sink}` : 'the system default output device';
        const where = result.path === 'temporary' ? 'a temporary audio output (no voice session is running)' : 'the conversation’s own audio output';
        this.tone = { busy: false, ok: !!result.ok,
                      text: result.ok ? `Played a short tone through ${where}, on ${device}. Heard nothing? Check that device and its volume.` : (result.message || 'The test sound could not be played.') };
        render();
        return !!result.ok;
    },
});

function defaultWatch(fn) {
    const alpine = globalThis.Alpine;
    // Read the setting inside the effect so it re-runs on change; act outside it so nothing the action touches is tracked.
    if (alpine?.effect) alpine.effect(() => { void convo.settings?.developer_mode; queueMicrotask(fn); });
    else setInterval(fn, 2000);
}

function mountWindow(owner) {
    const doc = globalThis.document;
    const parts = buildWindow(doc, { component: COMPONENT });
    frame = parts.root;
    doc.body.appendChild(frame);
    controller = createFloatingWindow({ root: frame, header: parts.header, grip: parts.grip, corner: parts.corner, storage: storage(), key: KEYS.geometry,
                                        onEscape: () => void owner.dock() });
    parts.dock.addEventListener('click', () => void owner.dock());
    parts.close.addEventListener('click', () => owner.closeWindow());
    controller.place();
}

function start() {
    active = true;
    const owner = ++generation;
    taps.echo = onEcho;
    clearInterval(levelTimer);
    levelTimer = setInterval(paintLevels, reducedMotion() ? LEVEL_REDUCED_MS : LEVEL_MS);
    paintLevels();
    void tick(owner);
}
function stop() {
    active = false;
    generation++;
    clearTimeout(pollTimer); pollTimer = null;
    clearInterval(levelTimer); levelTimer = null;
    if (taps.echo === onEcho) delete taps.echo;
    ring.length = 0;                                           // readings are kept only while someone is looking
}
async function tick(owner) {
    if (owner !== generation) return;
    let reads = 0;
    do { await poll(); reads++; } while (owner === generation && more && reads < MORE_READS);
    if (owner !== generation) return;
    pollTimer = setTimeout(() => void tick(owner), sessionLive() ? POLL_SESSION_MS : POLL_IDLE_MS);
}
async function poll() {
    const sent = clock();
    try {
        const result = await callJsonApi(endpoint, model.cursor === null ? { action: 'metrics' } : { action: 'metrics', cursor: model.cursor });
        const received = clock();
        if (!result?.ok) {
            more = false;
            store.error = /developer mode/i.test(result?.error || '') ? 'The saved settings have Developer diagnostics off, so the server shares no timing marks.'
                                                                       : (result?.error || 'Diagnostics could not be read.');
        } else {
            store.error = '';
            if ('last_request' in result) store.lastRequest = result.last_request && typeof result.last_request === 'object' ? result.last_request : null;
            // The kept turns are read again only when they changed and someone has the section open.
            if (store.sections.saw && Number.isFinite(result.inspect_revision) && result.inspect_revision !== store.inspectRevision) void store.loadInspected();
            ingest(model, result, { sent, received });
            more = !!result.more;
        }
    } catch (error) {
        more = false;
        store.error = 'Diagnostics could not be read: ' + (error?.message || String(error));
    }
    render();
}
function render() {
    const settings = convo.settings || {};
    const report = safe(() => convo.timingReport?.(), null) || { turns: [] };
    const origin = globalThis.performance?.timeOrigin;
    const compactions = Number.isFinite(origin) ? (convo.events || []).filter(event => event?.kind === 'context.compacted' && Number.isFinite(event.time))
        .map(event => ({ at: event.time * 1000 - origin, exchanges: event.payload?.exchanges })) : [];
    store.view = buildView(model, { browser: report.turns, floors: { ...defaults.policy_gate, ...(settings.policy_gate || {}) }, compactions, selected: store.selectedTurn });
    const playback = safe(() => audioSnapshot(), {}) || {};
    store.live = { state: convo.statusLabel || '', chip: convo.chipState || '', session: sessionLive(), hold: !!convo.policyHold,
                   ducked: !!playback.ducked, toggles: playback.toggles || 0, playing: !!playback.playing, queued: playback.queued || 0,
                   context: playback.context || '', sink: playback.sink || '',
                   echoSetting: settings.echo_guard || '', echo: echoView(ring) };
}
// Level meters are written straight to the DOM (no reactive state), four times a second (once with reduced motion).
function paintLevels() {
    const values = { input: convo.inputLevel, output: convo.outputLevel };
    for (const root of panels) for (const meter of root.querySelectorAll?.('[data-diag-level]') || []) {
        const value = Math.max(0, Math.min(1, Number(values[meter.dataset.diagLevel]) || 0));
        const text = Math.round(value * 100) + '%';
        if (meter.dataset.value === text) continue;
        meter.dataset.value = text;
        meter.style.setProperty('--level', value.toFixed(3));
        const label = meter.querySelector('[data-diag-level-text]');
        if (label) label.textContent = text;
    }
}
