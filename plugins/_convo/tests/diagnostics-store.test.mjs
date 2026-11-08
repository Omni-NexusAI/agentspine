// The diagnostics store on the REAL v2.11 canvas store (verbatim fixtures host-right-canvas-store.js and host-surfaces.js),
// reached through Convo's real registration extension: the surface exists only while the saved developer_mode is on, leaves the
// rail when it is turned off (the host has no unregister), polls `metrics` with its cursor only while someone can see it, and
// keeps the echo tap and the level meters to those moments. Virtual timers make every cadence exact.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadModule } from './vm-loader.mjs';

const flush = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };
const plain = value => JSON.parse(JSON.stringify(value));      // values built inside the VM compare by content, not by realm
const SURFACE = 'convo-diagnostics';

function virtualTimers() {
    let now = 0, id = 0;
    const queue = new Map();
    const add = (fn, ms, every) => { const handle = ++id; queue.set(handle, { at: now + Math.max(0, Number(ms) || 0), fn, every }); return handle; };
    return {
        now: () => now, queue,
        setTimeout: (fn, ms) => add(fn, ms, 0), clearTimeout: handle => { queue.delete(handle); },
        setInterval: (fn, ms) => add(fn, ms, Math.max(1, Number(ms) || 1)), clearInterval: handle => { queue.delete(handle); },
        async advance(ms) {
            const end = now + ms;
            for (;;) {
                const due = [...queue.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
                if (!due) break;
                const [handle, timer] = due;
                now = timer.at;
                if (timer.every) timer.at += timer.every; else queue.delete(handle);
                timer.fn();
                await flush();
            }
            now = end;
            await flush();
        },
    };
}
function fakeStorage(initial = {}) {
    const values = new Map(Object.entries(initial));
    return { values, getItem: key => (values.has(key) ? values.get(key) : null), setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key) };
}
function fakeElement(name, extra = {}) {
    return { name, isConnected: true, focus() { focused.push(name); }, contains: () => false, remove() { this.isConnected = false; },
             querySelector: () => null, ...extra };
}
let focused = [];

async function setup({ developer = true, settings, seen = false, floating = false, chat = 'chat-1', width = 1400, reduced = false, responses = [], stored = {} } = {}) {
    focused = [];
    const timers = virtualTimers(), stores = {}, effects = [], calls = [], toasts = [], mounted = [];
    const convo = {
        settings: settings === undefined ? { developer_mode: developer, echo_guard: 'adaptive', policy_gate: { respond_min: .3, interrupt_min: .6 } } : settings,
        active: false, busy: false, inputLevel: .42, outputLevel: 0, statusLabel: 'Listening', chipState: 'listening', policyHold: false, events: [],
        panelOpen: false, async refresh() {}, async history() {},
        timingReport: () => ({ turns: [{ turn: 'page-turn', marks: { onset: 10, speech_end: 600, final_sent: 900 } }], summary: {} }),
    };
    const taps = {};
    let cursor = 0;
    const api = async (_url, payload) => {
        calls.push(payload);
        const scripted = responses.shift();
        if (scripted) return typeof scripted === 'function' ? scripted(payload) : scripted;
        return { ok: true, now: timers.now() / 1000, cursor, turns: [], summary: {} };
    };
    const storage = fakeStorage({ 'convo.diagnostics.enabled': seen ? '1' : '0', 'convo.diagnostics.floating': floating ? '1' : '0', ...stored });
    const popout = fakeElement('popout-button');
    const doc = {
        hidden: false, listeners: {}, activeElement: fakeElement('opener'),
        addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
        body: { appendChild(node) { node.isConnected = true; mounted.push(node); return node; } },
        querySelector: selector => (selector === `[data-surface-id="${SURFACE}"] [data-diag-popout]` ? popout : selector === '.modal.show' ? (doc.modalOpen ? {} : null) : null),
    };
    const windows = [];
    const floatingWindow = {
        buildWindow: (_doc, options) => {
            const listeners = {};
            const root = fakeElement('window', { contains: node => node?.inWindow === true, querySelector: selector => (selector === '[data-drag-handle]' ? grip : null) });
            const grip = fakeElement('grip', { inWindow: true });
            const button = name => ({ name, addEventListener: (type, fn) => { listeners[name + ':' + type] = fn; } });
            windows.push({ options, root, listeners });
            return { root, header: {}, grip, corner: {}, dock: button('dock'), close: button('close'), body: {} };
        },
        createFloatingWindow: options => {
            const record = windows[windows.length - 1];
            record.controller = options;
            return { place() { record.placed = (record.placed || 0) + 1; }, destroy() { record.destroyed = true; }, geometry: () => null, sheet: false };
        },
    };
    const bindings = {
        '/js/AlpineStore.js': { createStore: (name, state) => (stores[name] = state) },
        '/js/api.js': { callJsonApi: api, fetchApi: async () => ({ ok: true }) },
        '/plugins/_convo/webui/convo-store.js': { store: convo, taps, install() {}, audioSnapshot: () => ({ active: true, ducked: true, toggles: 2, playing: true, queued: 1, pcm: 0 }) },
        '/plugins/_convo/webui/floating-window.js': floatingWindow,
    };
    const globals = {
        setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, setInterval: timers.setInterval, clearInterval: timers.clearInterval, queueMicrotask,
        performance: { now: timers.now, timeOrigin: 1_700_000_000_000, getEntriesByType: () => [] }, localStorage: storage, sessionStorage: fakeStorage(),
        matchMedia: query => ({ matches: reduced && /reduce/.test(query) }), document: doc, justToast: message => toasts.push(message),
        Alpine: { effect: fn => { effects.push(fn); fn(); } },
    };
    // The host: its surfaces module, then its canvas store, whose init() calls Convo's registration extension.
    const surfaces = (await loadModule('tests/fixtures/host-surfaces.js', { bindings: { './icons.js': { setIconName() {} } }, globals: { document: { addEventListener() {} } } })).namespace;
    const extension = (await loadModule('extensions/webui/right_canvas_register_surfaces/convo.js', { bindings, globals })).namespace;
    const hostDocument = { documentElement: { clientWidth: width, style: { setProperty() {} } }, body: { classList: { toggle() {}, add() {}, remove() {} } }, getElementById: () => null };
    const host = (await loadModule('tests/fixtures/host-right-canvas-store.js', {
        bindings: {
            '/js/AlpineStore.js': { createStore: (_name, state) => state },
            '/components/sidebar/chats/chats-store.js': { store: { selected: chat } },
            '/js/extensions.js': { callJsExtensions: async (point, canvas) => { if (point === 'right_canvas_register_surfaces') await extension.default(canvas); } },
            '/js/surfaces.js': surfaces,
        },
        globals: { document: hostDocument, innerWidth: width, addEventListener() {}, localStorage: fakeStorage(), sessionStorage: fakeStorage(),
                   performance: { getEntriesByType: () => [] }, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, CustomEvent: class {}, dispatchEvent() {} },
    })).namespace;
    const canvas = host.store;
    await canvas.init(null);
    await flush();
    const diagnostics = stores.convoDiagnostics;
    // What the wrapper's x-effect does whenever the canvas changes (right-canvas-panels/convo-diagnostics.html).
    const wrapper = async () => { diagnostics.setDockedVisible(!!canvas.isSurfaceVisible(SURFACE)); await flush(); };
    const settingsChanged = async next => { convo.settings = next; for (const fn of effects) fn(); await flush(); };
    const rail = () => plain(canvas.railSurfaces.map(item => item.id));
    const metricsCalls = () => plain(calls.filter(call => call.action === 'metrics'));
    return { timers, convo, taps, calls, metricsCalls, toasts, storage, doc, canvas, surfaces, diagnostics, wrapper, settingsChanged, rail, windows, mounted, popout,
             setCursor: value => { cursor = value; }, responses };
}

test('developer mode off: no surface, nothing polled, and Open diagnostics says what to do', async () => {
    const f = await setup({ developer: false });
    assert.deepEqual(f.rail(), ['convo']);
    assert.equal(f.diagnostics.available, false);
    assert.equal(await f.diagnostics.open(), false);
    assert.match(f.toasts.at(-1), /Turn on Developer diagnostics/);
    await f.timers.advance(20000);
    assert.deepEqual(f.metricsCalls(), []);
});

test('developer mode on: a second rail entry with no host modal path, so the host offers no window of its own', async () => {
    const f = await setup();
    assert.deepEqual(f.rail(), ['convo', SURFACE]);
    const surface = f.canvas.getSurface(SURFACE);
    assert.deepEqual([surface.title, surface.icon, surface.order, surface.modalPath], ['Convo diagnostics', 'monitoring', 66, '']);
    assert.equal(await f.canvas.open(SURFACE), true);
    assert.equal(f.canvas.currentSurfaceCanUndock(), false);                                  // the host's "Open as window" stays hidden
    assert.equal(f.storage.values.get('convo.diagnostics.enabled'), '1');                     // remembered for the next page load
});

test('polling runs only while the surface is visible: at once, then every 4 s idle and every second in a session', async () => {
    const f = await setup();
    await f.timers.advance(10000);
    assert.equal(f.metricsCalls().length, 0);                                                 // registered but not shown
    f.setCursor(7);
    await f.canvas.open(SURFACE); await f.wrapper();
    assert.deepEqual(f.metricsCalls(), [{ action: 'metrics' }]);                              // the first read takes the recent turns
    assert.equal(f.diagnostics.running, true);
    await f.timers.advance(3999);
    assert.equal(f.metricsCalls().length, 1);
    await f.timers.advance(1);
    assert.deepEqual(f.metricsCalls()[1], { action: 'metrics', cursor: 7 });                 // then only what is newer
    f.convo.active = true;
    await f.timers.advance(4000);                                                             // the 4 s wait already scheduled
    await f.timers.advance(3000);
    assert.equal(f.metricsCalls().length, 6);                                                 // then one a second
    await f.canvas.close(); await f.wrapper();                                                // the host never calls surface.close()
    assert.equal(f.diagnostics.running, false);
    await f.timers.advance(10000);
    assert.equal(f.metricsCalls().length, 6);
});

test('a hidden tab stops polling, and coming back reads at once', async () => {
    const f = await setup();
    await f.canvas.open(SURFACE); await f.wrapper();
    f.doc.hidden = true;
    for (const fn of f.doc.listeners.visibilitychange) fn();
    await flush();
    await f.timers.advance(20000);
    assert.equal(f.metricsCalls().length, 1);
    f.doc.hidden = false;
    for (const fn of f.doc.listeners.visibilitychange) fn();
    await flush();
    assert.equal(f.metricsCalls().length, 2);
});

test('the cursor follows each read, a full page is read again at once, and a restarted log starts over', async () => {
    const turn = (name, marks) => ({ turn: name, marks });
    const responses = [
        { ok: true, now: 1, cursor: 1000, more: true, turns: [turn('a', [{ name: 'policy.start', t: .5, seq: 1, sequence: 1, final: true }])] },
        { ok: true, now: 1.1, cursor: 1200, more: false, turns: [turn('a', [{ name: 'policy.done', t: .9, seq: 1001, sequence: 1, final: true }])] },
        { ok: true, now: 5, cursor: 3, reset: true, turns: [turn('b', [{ name: 'policy.start', t: 4.9, seq: 3, sequence: 1, final: true }])] },
        { ok: false, code: 'invalid', error: 'Timing marks are available in developer mode' },
    ];
    const f = await setup({ responses });
    await f.canvas.open(SURFACE); await f.wrapper();
    assert.deepEqual(f.metricsCalls(), [{ action: 'metrics' }, { action: 'metrics', cursor: 1000 }]);
    const turns = () => plain(f.diagnostics.view.turns.map(item => item.turn).sort());
    assert.deepEqual(turns(), ['a', 'page-turn']);
    assert.equal(f.diagnostics.view.turns.find(item => item.turn === 'a').steps[0].id, 'classifier');
    await f.timers.advance(4000);
    assert.deepEqual(f.metricsCalls()[2], { action: 'metrics', cursor: 1200 });
    assert.deepEqual(turns(), ['b', 'page-turn']);                                            // the old log's turns are gone
    await f.timers.advance(4000);
    assert.deepEqual(f.metricsCalls()[3], { action: 'metrics', cursor: 3 });
    assert.match(f.diagnostics.error, /Developer diagnostics off/);                            // a refusal is shown, polling goes on
    await f.timers.advance(4000);
    assert.equal(f.metricsCalls().length, 5);
    assert.equal(f.diagnostics.error, '');
});

test('the echo tap and the level meters run only while it is shown, the meters at 4 Hz (1 Hz with reduced motion)', async () => {
    for (const reduced of [false, true]) {
        const f = await setup({ reduced });
        const writes = [];
        const meter = { dataset: { diagLevel: 'input' }, style: { setProperty: (name, value) => writes.push(value) }, querySelector: () => ({ set textContent(text) { writes.push(text); } }) };
        f.diagnostics.bindPanel({ querySelectorAll: () => [meter] });
        assert.equal(f.taps.echo, undefined);
        await f.canvas.open(SURFACE); await f.wrapper();
        assert.equal(typeof f.taps.echo, 'function');
        f.taps.echo({ kind: 'echo_metric', mode: 'adaptive', acousticState: 'uncoupled', suppressing: true, candidateMs: 90, audio: [1, 2] });
        await f.diagnostics.pollNow();
        assert.deepEqual([f.diagnostics.live.echo.acoustic, f.diagnostics.live.echo.candidateMs, f.diagnostics.live.ducked, f.diagnostics.live.toggles], ['uncoupled', 90, true, 2]);
        assert.deepEqual(writes, ['0.420', '42%']);                                           // painted at once, straight to the DOM
        writes.length = 0;
        let changes = 0;
        for (let step = 0; step < 8; step++) { f.convo.inputLevel = step / 10; await f.timers.advance(250); }
        changes = writes.length / 2;
        assert.equal(changes, reduced ? 2 : 8, reduced ? 'once a second' : 'four times a second');
        await f.canvas.close(); await f.wrapper();
        assert.equal(f.taps.echo, undefined);                                                 // nothing collects while nobody looks
        writes.length = 0;
        f.convo.inputLevel = .9;
        await f.timers.advance(2000);
        assert.deepEqual(writes, []);
        await f.canvas.open(SURFACE); await f.wrapper();
        assert.equal(f.diagnostics.live.echo, null);                                          // the old readings were not kept
    }
});

test('turning developer mode off closes the surface, takes it out of the rail and refuses to reopen it', async () => {
    const f = await setup();
    await f.canvas.open(SURFACE); await f.wrapper();
    assert.equal(f.canvas.isSurfaceVisible(SURFACE), true);
    await f.settingsChanged({ ...f.convo.settings, developer_mode: false });
    await f.wrapper();
    assert.deepEqual(f.rail(), ['convo']);
    assert.deepEqual([f.canvas.isOpen, f.canvas.isSurfaceMounted(SURFACE), f.canvas.activeSurfaceId], [false, false, 'convo']);
    assert.equal(f.diagnostics.running, false);
    assert.ok(f.surfaces.getSurface(SURFACE));                                                // the host's registry still knows it ...
    assert.equal(await f.canvas.open(SURFACE), false);                                        // ... so canOpen is what keeps it shut
    assert.equal(f.canvas.isOpen, false);
    assert.equal(f.storage.values.get('convo.diagnostics.enabled'), '0');
    await f.settingsChanged({ ...f.convo.settings, developer_mode: true });                    // and on again: back in the rail
    assert.deepEqual(f.rail(), ['convo', SURFACE]);
    assert.equal(await f.canvas.open(SURFACE), true);
});

test('the pop-out window: one place at a time, Escape docks and returns focus, close returns focus, and it survives a reload', async () => {
    const f = await setup();
    await f.canvas.open(SURFACE); await f.wrapper();
    assert.equal(f.diagnostics.popOut(), true);
    await f.wrapper();
    assert.deepEqual([f.canvas.isOpen, f.diagnostics.floating, f.mounted.length, f.windows[0].placed], [false, true, 1, 1]);
    assert.equal(f.windows[0].options.component, '/plugins/_convo/webui/diagnostics.html');
    assert.equal(f.windows[0].controller.key, 'convo.diagnostics.window');
    assert.equal(f.storage.values.get('convo.diagnostics.floating'), '1');
    assert.equal(f.diagnostics.running, true);                                                // the window is visible: polling goes on
    await f.timers.advance(100);
    assert.deepEqual(focused, ['grip']);                                                      // focus moves into the window
    f.windows[0].controller.onEscape();                                                       // Escape inside the window
    await flush(); await f.wrapper(); await f.timers.advance(100);
    assert.deepEqual([f.diagnostics.floating, f.windows[0].destroyed, f.windows[0].root.isConnected], [false, true, false]);
    assert.deepEqual([f.canvas.isOpen, f.canvas.activeSurfaceId], [true, SURFACE]);
    assert.equal(focused.at(-1), 'popout-button');                                            // back to the button that popped it out
    assert.equal(f.storage.values.get('convo.diagnostics.floating'), '0');
    f.diagnostics.popOut(); await f.wrapper();
    f.windows[1].listeners['close:click']();                                                  // the close button: no docking
    await f.wrapper(); await f.timers.advance(100);
    assert.deepEqual([f.diagnostics.floating, f.canvas.isOpen, f.diagnostics.running], [false, false, false]);
    assert.equal(focused.at(-1), 'opener');
    // A window left open comes back after a reload once the settings confirm developer mode.
    const reloaded = await setup({ floating: true });
    assert.deepEqual([reloaded.diagnostics.floating, reloaded.mounted.length], [true, 1]);
    const off = await setup({ developer: false, floating: true });
    assert.deepEqual([off.diagnostics.floating, off.mounted.length], [false, 0]);
});

test('where the canvas cannot dock it (phone width, or no chat yet), opening it pops out the window instead', async () => {
    const phone = await setup({ width: 700 });
    assert.equal(await phone.canvas.open(SURFACE), false);                                    // canOpen said no to docking ...
    assert.deepEqual([phone.diagnostics.floating, phone.mounted.length], [true, 1]);          // ... and showed the window (a sheet when narrow)
    const welcome = await setup({ chat: '' });
    assert.equal(await welcome.diagnostics.open(), true);
    assert.equal(welcome.diagnostics.floating, true);
    const modal = await setup();
    modal.doc.modalOpen = true;
    assert.equal(await modal.diagnostics.open(), true);
    assert.match(modal.toasts.at(-1), /close this dialog to see it/);
});

test('open sections are remembered per browser, and only known sections with true or false are read back', async () => {
    const f = await setup({ stored: { 'convo.diagnostics.sections': JSON.stringify({ live: false, turns: 'yes', evil: true }) } });
    assert.deepEqual(plain(f.diagnostics.sections), { live: false, turns: true, gate: true, speculation: true, rolling: true, context: true, tune: false });
    f.diagnostics.toggle('tune');
    assert.equal(JSON.parse(f.storage.values.get('convo.diagnostics.sections')).tune, true);
    const broken = await setup({ stored: { 'convo.diagnostics.sections': '{not json' } });
    assert.equal(broken.diagnostics.sections.live, true);
});

test('seen on last time: registered before the settings arrive so a reload can restore it, then corrected by them', async () => {
    const f = await setup({ settings: null, seen: true });
    assert.deepEqual(f.rail(), ['convo', SURFACE]);
    await f.settingsChanged({ developer_mode: false });
    assert.deepEqual(f.rail(), ['convo']);
    const unseen = await setup({ settings: null, seen: false });
    assert.deepEqual(unseen.rail(), ['convo']);
    await unseen.settingsChanged({ developer_mode: true });
    assert.deepEqual(unseen.rail(), ['convo', SURFACE]);
});
