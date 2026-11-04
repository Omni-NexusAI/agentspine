import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { fakeMic, storeFixture } from './store-fixture.mjs';
import { ORB_STATES, cueFor, emitCue, orbView, releaseOrb, syncOrb } from '../webui/orb.js';
import * as cues from '../webui/sound-cues.js';

const view = snapshot => orbView({ enabled: true, ...snapshot });

test('The orb is a pure function of state, and Convo owns it only when enabled', () => {
    assert.equal(orbView({ enabled: false, active: true, state: 'speaking' }), null);
    assert.equal(orbView(null), null);
    const table = [
        [{ active: false }, 'ready'], [{ active: false, devIdle: true }, 'idle'],
        [{ busy: true }, 'connecting'], [{ state: 'connecting' }, 'connecting'],
        [{ active: true, state: 'listening' }, 'listening'], [{ active: true, state: 'off' }, 'listening'],
        [{ active: true, state: 'listening', hearing: true }, 'hearing'],
        [{ active: true, state: 'considering' }, 'thinking'], [{ active: true, state: 'thinking', hearing: true }, 'thinking'],
        [{ active: true, state: 'speaking' }, 'speaking'], [{ active: true, state: 'speaking', hearing: true }, 'speaking'],
        [{ dictating: true, active: true, state: 'speaking' }, 'dictating-latched'],
        [{ dictating: true, dictationMode: 'latched' }, 'dictating-latched'],
        [{ dictating: true, dictationMode: 'ptt' }, 'dictating-ptt'],
        [{ error: 'Boom', active: true, state: 'listening' }, 'error'],
        [{ error: 'Boom', dictating: true }, 'dictating-latched'],    // an active dictation outranks a stale error
    ];
    for (const [snapshot, expected] of table) assert.equal(view(snapshot).id, expected, JSON.stringify(snapshot));
    assert.deepEqual(new Set(table.map(([, id]) => id)), new Set(ORB_STATES));   // every state is reachable
});

test('Every state has a spoken label; only a live session reads as pressed; a hover is never a state', () => {
    for (const id of ORB_STATES) {
        const snapshot = { ready: { active: false }, idle: { active: false, devIdle: true }, connecting: { busy: true }, listening: { active: true, state: 'listening' },
            hearing: { active: true, state: 'listening', hearing: true }, thinking: { active: true, state: 'considering' }, speaking: { active: true, state: 'speaking' },
            'dictating-latched': { dictating: true }, 'dictating-ptt': { dictating: true, dictationMode: 'ptt' }, error: { error: 'Provider down' } }[id];
        const v = view(snapshot);
        assert.match(v.label, /^[A-Z][^.]+\. [A-Z][^.]+\.$/, id);        // "Name. Hint."
        assert.ok(v.title.includes(' · '), id);
        assert.equal(v.pressed, !['ready', 'idle', 'error'].includes(id), id);
    }
    assert.match(view({ active: false, devIdle: true }).label, /development mode/);
    assert.match(view({ error: 'x'.repeat(500) }).title, /x{140}$/);       // bounded error detail in the tooltip only
    assert.doesNotMatch(view({ error: 'secret detail' }).label, /secret detail/);
    assert.equal(view({ active: true, state: 'speaking', policyHold: true }).hold, true);
    assert.equal(view({ active: true, state: 'listening', policyHold: true }).hold, false);   // ducking only exists while speaking
});

test('Cues follow state transitions, never call sites', () => {
    const expect = (from, to, cue) => assert.equal(cueFor(from, to), cue, `${from} -> ${to}`);
    expect(null, 'ready', null);                                   // the first paint is silent
    expect('ready', 'ready', null);
    expect('ready', 'connecting', 'activate');
    expect('idle', 'listening', 'activate');
    expect('error', 'connecting', 'activate');
    expect('connecting', 'listening', null);                       // still one activation
    expect('listening', 'hearing', null); expect('hearing', 'thinking', null); expect('thinking', 'speaking', null); expect('speaking', 'listening', null);
    expect('listening', 'ready', 'deactivate');
    expect('speaking', 'idle', 'deactivate');
    expect('connecting', 'ready', 'deactivate');
    expect('ready', 'dictating-latched', 'dictation-start');
    expect('speaking', 'dictating-latched', 'dictation-start');
    expect('dictating-latched', 'dictating-ptt', 'dictation-ptt');
    expect('dictating-ptt', 'dictating-latched', null);
    expect('ready', 'dictating-ptt', 'dictation-start');
    expect('dictating-latched', 'ready', 'dictation-end');
    expect('dictating-ptt', 'ready', 'dictation-end');
    expect('dictating-latched', 'connecting', 'activate');
    expect('listening', 'error', 'error');
    expect('ready', 'error', 'error');
    expect('error', 'ready', null);
    expect('listening', null, null);                               // Convo turned off: the native orb returns silently
    expect(null, 'listening', null);
});

test('A cue is an event; delivery failures are swallowed', () => {
    const seen = [];
    const original = { dispatchEvent: globalThis.dispatchEvent };
    try {
        globalThis.dispatchEvent = event => seen.push([event.type, event.detail.name]);
        emitCue('activate');
        assert.deepEqual(seen, [['convo:cue', 'activate']]);
        globalThis.dispatchEvent = () => { throw new Error('listener blew up'); };
        assert.doesNotThrow(() => emitCue('error'));
        delete globalThis.dispatchEvent;
        assert.doesNotThrow(() => emitCue('error'));
    } finally { if (original.dispatchEvent) globalThis.dispatchEvent = original.dispatchEvent; else delete globalThis.dispatchEvent; }
});

test('The orb is layered onto the host button once, and attributes are written only when they change', () => {
    const mic = fakeMic({ 'aria-label': 'Microphone standby', 'aria-pressed': 'false' });
    syncOrb(mic, view({ active: false }), 0);
    assert.equal(mic.children.length, 1);
    assert.match(mic.children[0].innerHTML, /co-glow.*co-ring.*co-ring2.*co-core.*g-mic.*g-bars.*g-dots.*g-voice.*g-pen.*g-ptt.*g-err/s);
    const written = mic.writes.length;
    for (let i = 0; i < 20; i++) syncOrb(mic, view({ active: false }), 0);       // the metering rate must not rewrite anything
    assert.equal(mic.children.length, 1);
    assert.deepEqual(mic.writes.slice(written).filter(([kind]) => kind === 'attr'), []);
    syncOrb(mic, view({ active: true, state: 'listening', hearing: true }), 0.4567);
    assert.equal(mic.attrs.get('data-convo'), 'hearing');
    assert.equal(mic.attrs.get('aria-pressed'), 'true');
    assert.equal(mic.style.props.get('--convo-level'), '0.46');
    syncOrb(mic, view({ active: true, state: 'speaking', policyHold: true }), 9);
    assert.equal(mic.attrs.has('data-convo-hold'), true);
    assert.equal(mic.style.props.get('--convo-level'), '1.00');                    // a level is clamped, whatever the meter says
    syncOrb(mic, view({ active: true, state: 'speaking' }), -3);
    assert.equal(mic.attrs.has('data-convo-hold'), false);
    assert.equal(mic.style.props.get('--convo-level'), '0.00');
});

test('Turning Convo off removes every trace and restores the native button exactly', () => {
    const mic = fakeMic({ 'aria-label': 'Microphone standby', 'aria-pressed': 'false', title: 'Native title' });
    syncOrb(mic, view({ active: true, state: 'speaking' }), 0.5);
    syncOrb(mic, null);
    assert.deepEqual([mic.attrs.get('aria-label'), mic.attrs.get('title'), mic.attrs.get('aria-pressed')], ['Microphone standby', 'Native title', 'false']);
    assert.deepEqual([mic.attrs.has('data-convo'), mic.attrs.has('data-convo-hold'), mic.style.props.size, mic.children.length], [false, false, 0, 0]);
    releaseOrb(mic); releaseOrb(null);                                             // idempotent

    const bare = fakeMic({ 'aria-label': 'Microphone standby' });                  // no native title or pressed state
    syncOrb(bare, view({ active: false }), 0); releaseOrb(bare);
    assert.deepEqual([bare.attrs.has('title'), bare.attrs.has('aria-pressed')], [false, false]);
    assert.equal(bare.attrs.get('aria-label'), 'Microphone standby');
    assert.doesNotThrow(() => syncOrb(null, view({ active: false })));
});

test('When the host rewrites the label or pressed state, the orb puts its own back at once; release stops watching', () => {
    const watchers = [];
    class FakeObserver { constructor(callback) { this.callback = callback; this.live = true; watchers.push(this); } observe(target, options) { this.options = options; } disconnect() { this.live = false; } }
    const original = globalThis.MutationObserver;
    globalThis.MutationObserver = FakeObserver;
    try {
        const mic = fakeMic({ 'aria-label': 'Microphone standby', 'aria-pressed': 'false' });
        mic.nodeType = 1;
        syncOrb(mic, view({ active: true, state: 'listening' }), 0);
        assert.equal(watchers.length, 1);
        assert.deepEqual(watchers[0].options.attributeFilter.sort(), ['aria-label', 'aria-pressed']);   // never the tooltip: the host owns that
        mic.setAttribute('aria-label', 'Microphone inactive');                       // the host's own effect runs
        mic.setAttribute('aria-pressed', 'false');
        watchers[0].callback();
        assert.equal(mic.attrs.get('aria-label'), 'Convo listening. Click to stop; hold to dictate.');
        assert.equal(mic.attrs.get('aria-pressed'), 'true');
        assert.match(mic.attrs.get('title'), /^Convo listening/);                    // written with the view (no host tooltip system here)
        syncOrb(mic, view({ active: true, state: 'speaking' }), 0);                   // a new state keeps the same watcher
        assert.equal(watchers.length, 1);
        watchers[0].callback();
        assert.match(mic.attrs.get('aria-label'), /^Convo speaking/);
        syncOrb(mic, null);
        assert.equal(watchers[0].live, false);
        assert.equal(mic.attrs.get('aria-label'), 'Microphone standby');
        mic.setAttribute('aria-label', 'Microphone inactive');
        watchers[0].callback();                                                       // a stale callback after release must not repaint
        assert.equal(mic.attrs.get('aria-label'), 'Microphone inactive');
        assert.equal(mic.attrs.has('data-convo'), false);
    } finally { if (original) globalThis.MutationObserver = original; else delete globalThis.MutationObserver; }
});

// ---- the host's own tooltip observer: the orb must converge with it, never fight it (a real freeze, found on the live host)
// The host (components/tooltips/tooltip-store.js) watches `title` and `data-bs-original-title` on every element: when an
// element with a Bootstrap tooltip gains a `title`, it moves it to `data-bs-original-title` and REMOVES `title`. An orb that
// keeps re-writing `title` (or watches it) therefore ping-pongs with the host forever, and microtask-driven mutation
// callbacks never let the page breathe. This models both sides, including Bootstrap 5.0.2's own `_fixTitle`.
function liveHost(mic) {
    const observers = [], queue = [], instances = new Map();
    const note = name => { for (const observer of observers) if (observer.live && observer.filter.includes(name)) queue.push([observer, name]); };
    const set = mic.setAttribute.bind(mic), remove = mic.removeAttribute.bind(mic);
    mic.setAttribute = (name, value) => { set(name, value); note(name); };
    mic.removeAttribute = name => { const had = mic.hasAttribute(name); remove(name); if (had) note(name); };
    class Observer {
        constructor(callback) { this.callback = callback; this.live = true; observers.push(this); }
        observe(_target, options) { this.filter = options.attributeFilter; }
        disconnect() { this.live = false; }
    }
    class Tooltip {                                                      // Bootstrap 5.0.2 _fixTitle
        static getInstance(element) { return instances.get(element) ?? null; }
        constructor(element) {
            instances.set(element, this); this.contents = [];
            const title = element.getAttribute('title');
            if (title || typeof element.getAttribute('data-bs-original-title') !== 'string') {
                element.setAttribute('data-bs-original-title', title || '');
                element.setAttribute('title', '');
            }
        }
        setContent(content) { this.contents.push(content); }
    }
    const ensure = element => {                                          // the host's ensureBootstrapTooltip, verbatim in effect
        const existing = Tooltip.getInstance(element);
        const title = element.getAttribute('title') || element.getAttribute('data-bs-original-title');
        if (!title) return;
        if (existing) {
            if (element.getAttribute('title')) { element.setAttribute('data-bs-original-title', title); element.removeAttribute('title'); }
            existing.setContent({ '.tooltip-inner': title });
            return;
        }
        if (element.getAttribute('title')) { element.setAttribute('data-bs-original-title', title); element.removeAttribute('title'); }
        element.setAttribute('data-bs-toggle', 'tooltip');
        element.setAttribute('data-bs-trigger', 'hover');
        element.setAttribute('data-bs-tooltip-initialized', 'true');
        new Tooltip(element);
    };
    new Observer(mutations => mutations.forEach(m => { if (m.attributeName === 'title' || m.attributeName === 'data-bs-original-title') ensure(m.target); }))
        .observe(mic, { attributes: true, attributeFilter: ['title', 'data-bs-original-title'] });
    const flush = (limit = 300) => {                                    // microtask semantics: run until quiet, or call it a loop
        let turns = 0;
        while (queue.length) {
            if (++turns > limit) throw new Error(`mutation ping-pong with the host: still running after ${limit} observer callbacks`);
            const [observer, name] = queue.shift();
            if (observer.live) observer.callback([{ type: 'attributes', attributeName: name, target: mic }]);
        }
        return turns;
    };
    return { Observer, Tooltip, ensure, flush, instances };
}

test('The orb converges with the host tooltip observer instead of ping-ponging with it', () => {
    const original = { observer: globalThis.MutationObserver, bootstrap: globalThis.bootstrap };
    try {
        const mic = fakeMic({ 'aria-label': 'Microphone standby', 'aria-pressed': 'false', title: 'Voice input' });
        mic.nodeType = 1;
        const host = liveHost(mic);
        globalThis.MutationObserver = host.Observer; globalThis.bootstrap = { Tooltip: host.Tooltip };
        host.ensure(mic); host.flush();                                   // the host has already attached its tooltip to the button
        assert.equal(host.instances.size, 1);
        for (const snapshot of [{ active: false }, { active: true, state: 'listening', hearing: true }, { active: true, state: 'speaking', policyHold: true }, { dictating: true }, { dictating: true, dictationMode: 'ptt', holding: true }]) {
            syncOrb(mic, view(snapshot), .3);
            assert.ok(host.flush() < 30, 'the orb and the host tooltip settle in a few steps');
        }
        syncOrb(mic, view({ active: true, state: 'listening' }), 0); host.flush();
        // the visible tooltip is the host's, fed through its own attribute; the orb leaves `title` alone
        assert.match(mic.attrs.get('data-bs-original-title'), /^Convo listening/);
        assert.ok(!mic.attrs.get('title'), 'no title is left for the host to take away and the orb to put back');
        assert.equal(mic.attrs.get('aria-label'), 'Convo listening. Click to stop; hold to dictate.');
        assert.equal(host.instances.get(mic).contents.at(-1)['.tooltip-inner'], mic.attrs.get('data-bs-original-title'));
        // steady state does not touch the DOM at all, even with the host listening
        const writes = mic.writes.length;
        for (let i = 0; i < 25; i++) { syncOrb(mic, view({ active: true, state: 'listening' }), 0); host.flush(); }
        assert.deepEqual(mic.writes.slice(writes).filter(([kind]) => kind === 'attr'), []);
        // and giving the button back restores it exactly as the host had it
        syncOrb(mic, null); host.flush();
        assert.equal(mic.attrs.get('aria-label'), 'Microphone standby');
        assert.equal(mic.attrs.has('data-convo'), false);
        assert.doesNotMatch(mic.attrs.get('data-bs-original-title') || '', /Convo/);
    } finally {
        if (original.observer) globalThis.MutationObserver = original.observer; else delete globalThis.MutationObserver;
        if (original.bootstrap) globalThis.bootstrap = original.bootstrap; else delete globalThis.bootstrap;
    }
});

test('Without the host tooltip system the orb still offers a native title, and giving the button back removes it', () => {
    const original = { observer: globalThis.MutationObserver, bootstrap: globalThis.bootstrap };
    try {
        delete globalThis.bootstrap;
        const mic = fakeMic({ 'aria-label': 'Microphone standby', 'aria-pressed': 'false' });
        syncOrb(mic, view({ active: true, state: 'speaking' }), 0);
        assert.match(mic.attrs.get('title'), /^Convo speaking/);
        assert.equal(mic.attrs.has('data-bs-original-title'), false);
        syncOrb(mic, null);
        assert.equal(mic.attrs.has('title'), false);
    } finally {
        if (original.observer) globalThis.MutationObserver = original.observer; else delete globalThis.MutationObserver;
        if (original.bootstrap) globalThis.bootstrap = original.bootstrap;
    }
});

test('The orb stylesheet has a glyph and animation for every state, honours reduced motion and never reacts to hover', async () => {
    const css = (await readFile(new URL('../extensions/webui/page-head/zz-convo-orb.html', import.meta.url), 'utf8'));
    assert.match(css, /touch-action:none/);                                         // a touch hold is dictation, not a scroll
    assert.match(css, /@media\(prefers-reduced-motion:reduce\)/);
    const glyphFor = { ready: 'g-mic', idle: 'g-idle', connecting: 'g-spin', listening: 'g-bars', hearing: 'g-bars', thinking: 'g-dots', speaking: 'g-voice', 'dictating-latched': 'g-pen', 'dictating-ptt': 'g-ptt', error: 'g-err' };
    for (const id of ORB_STATES) assert.match(css, new RegExp(`\\[data-convo=${id}\\] \\.${glyphFor[id]}`), id);
    const hover = css.match(/#microphone-button\[data-convo\]:hover\{[^}]*\}/)[0];
    assert.doesNotMatch(hover, /--co|data-convo=|opacity/);                         // hover brightens only: it is not microphone-on proof
    for (const [, name] of css.matchAll(/animation:(co-[a-z-]+)/g)) assert.match(css, new RegExp(`@keyframes ${name}\\{`), name);
    const reduced = css.slice(css.indexOf('@media(prefers-reduced-motion:reduce)'));
    assert.match(reduced, /animation:none!important/);
    assert.match(reduced, /transition:none!important/);
    assert.doesNotMatch(css, /localStorage|document\.|<script/);
});

// ---- sound cues ---------------------------------------------------------------------------------------------
async function withGlobals(values, run) {
    const saved = Object.fromEntries(Object.keys(values).map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
    Object.assign(globalThis, values); cues.reset();
    try { return await run(); } finally {
        for (const [key, descriptor] of Object.entries(saved)) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key]; }
        cues.reset();
    }
}
const manifestOf = value => async () => ({ ok: true, json: async () => value });

test('A cue with no clip is silence: nothing is created, requested or thrown', async () => {
    const made = [];
    await withGlobals({ fetch: manifestOf({ version: 1, cues: { activate: null } }), Audio: class { constructor(url) { made.push(url); } } }, async () => {
        assert.equal(await cues.play('activate'), false);
        assert.equal(await cues.play('unknown-cue'), false);
        assert.deepEqual(made, []);
    });
});

test('A named clip plays once created, volume bounded; autoplay refusal and fetch failure are silent', async () => {
    const made = [], played = [];
    class FakeAudio { constructor(url) { this.url = url; made.push(this); } async play() { played.push(this.url); if (FakeAudio.refuse) throw new Error('NotAllowedError'); } }
    await withGlobals({ fetch: manifestOf({ volume: 7, cues: { activate: 'on.wav', error: '../escape.wav', 'dictation-end': 'a b.wav' } }), Audio: FakeAudio }, async () => {
        assert.equal(await cues.play('activate'), true);
        assert.equal(await cues.play('activate'), true);                           // the clip is reused
        assert.equal(made.length, 1);
        assert.deepEqual([made[0].url, made[0].volume, played.length], ['/plugins/_convo/webui/sounds/on.wav', 1, 2]);
        assert.equal(await cues.play('error'), false);                             // only a plain file name inside sounds/
        assert.equal(await cues.play('dictation-end'), false);
        FakeAudio.refuse = true;
        assert.equal(await cues.play('activate'), false);                          // autoplay blocked: silence, not an error
    });
    await withGlobals({ fetch: async () => { throw new Error('offline'); }, Audio: FakeAudio }, async () => {
        assert.equal(await cues.play('activate'), false);
    });
    await withGlobals({ fetch: async () => ({ ok: false }), Audio: FakeAudio }, async () => { assert.equal(await cues.play('activate'), false); });
});

test('The installed listener turns convo:cue events into playback, once', async () => {
    const listeners = new Map(), played = [];
    const target = { addEventListener: (type, fn) => listeners.set(type, fn), removeEventListener: type => listeners.delete(type) };
    await withGlobals({ fetch: manifestOf({ cues: { 'dictation-start': 'dictate.ogg' } }), Audio: class { constructor(url) { this.url = url; } async play() { played.push(this.url); } } }, async () => {
        const handle = cues.install(target);
        assert.equal(cues.install(target), handle);                                // installing twice keeps one listener
        listeners.get('convo:cue')({ detail: { name: 'dictation-start' } });
        await new Promise(resolve => setTimeout(resolve, 10));
        assert.deepEqual(played, ['/plugins/_convo/webui/sounds/dictate.ogg']);
        handle.dispose();
        assert.equal(listeners.has('convo:cue'), false);
        assert.equal(target.__convoCues, undefined);
    });
});

test('The shipped manifest names every cue and ships no clips', async () => {
    const manifest = JSON.parse(await readFile(new URL('../webui/sounds/manifest.json', import.meta.url), 'utf8'));
    assert.deepEqual(Object.keys(manifest.cues).sort(), ['activate', 'deactivate', 'dictation-end', 'dictation-ptt', 'dictation-start', 'error']);
    assert.ok(Object.values(manifest.cues).every(value => value === null));
    assert.ok(manifest.volume > 0 && manifest.volume <= 1);
});

// ---- the store drives the orb and emits the cues -----------------------------------------------------------------
const state = f => f.mic.attrs.get('data-convo');

test('The store paints the orb from real state and cues each transition once', async () => {
    const f = await storeFixture();
    await f.store.refresh();
    assert.equal(state(f), 'ready');
    assert.deepEqual(f.cueNames, []);                                             // first paint is silent
    assert.equal(f.mic.attrs.get('aria-label'), 'Convo ready, microphone off. Click to start a conversation; hold to dictate.');

    await f.store.start();
    assert.equal(state(f), 'listening');
    assert.deepEqual(f.cueNames, ['activate']);                                   // connecting -> listening is one activation
    f.audios[0].onStart();                                                        // the existing detector heard something
    assert.equal(state(f), 'hearing');
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, type: 'state', state: 'considering' } });
    assert.equal(state(f), 'thinking');
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, type: 'audio.begin', turn_id: 't', phrase_id: 'p' } });
    assert.equal(state(f), 'speaking');
    assert.equal(f.mic.attrs.has('data-convo-hold'), false);
    assert.deepEqual(f.cueNames, ['activate']);                                   // the conversation's inner states do not cue
    await f.store.stop();
    assert.equal(state(f), 'ready');
    assert.deepEqual(f.cueNames, ['activate', 'deactivate']);
    assert.equal(f.mic.attrs.get('aria-pressed'), 'false');
});

test('The orb keeps reading speaking while audio is still playing after the server finished its turn', async () => {
    assert.equal(view({ active: true, state: 'listening', playing: true }).id, 'speaking');
    assert.equal(view({ active: true, state: 'listening', playing: false }).id, 'listening');
    assert.equal(view({ active: true, state: 'listening', hearing: true, playing: true }).id, 'speaking');      // playback leads; a duck shows as the hold modifier
    assert.equal(view({ active: true, state: 'listening', hearing: true, playing: true, policyHold: true }).hold, true);
    assert.equal(view({ active: true, state: 'considering', playing: true }).id, 'speaking');                   // a second reply being prepared while the first is audible
    assert.equal(view({ active: false, playing: true }).id, 'ready');                                          // no session: nothing is speaking
    assert.equal(view({ active: true, state: 'speaking', error: 'x', playing: true }).id, 'error');              // an error still wins
    const f = await storeFixture();
    await f.store.refresh();
    await f.store.start();
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, type: 'audio.begin', turn_id: 't', phrase_id: 'p' } });
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, type: 'state', state: 'listening' } });     // the turn is over on the server
    assert.equal(state(f), 'listening');                                                                       // ... and nothing is audible yet
    f.audios[0].playing = {};                                                                                  // a buffered phrase is audible
    f.audios[0].onLevel('output', 0.4);
    assert.equal(state(f), 'speaking');
    f.audios[0].playing = null;
    f.audios[0].onLevel('output', 0);                                                                          // playback ended
    assert.equal(state(f), 'listening');
    await f.store.stop();
});

test('Dictation, errors and development mode each read as themselves', async () => {
    const f = await storeFixture();
    await f.store.refresh();
    await f.store.beginDictation();
    assert.equal(state(f), 'dictating-latched');
    assert.equal(f.mic.attrs.get('aria-pressed'), 'true');
    await f.store.endDictation();
    assert.equal(state(f), 'ready');
    assert.deepEqual(f.cueNames, ['dictation-start', 'dictation-end']);

    const failing = await storeFixture({ startFails: true });
    await failing.store.refresh();
    await failing.store.start();                                                  // a rejected start is shown, not swallowed
    assert.equal(state(failing), 'error');
    assert.match(failing.mic.attrs.get('title') || '', /Provider unreachable/);
    assert.equal(failing.mic.attrs.get('aria-pressed'), 'false');
    assert.deepEqual(failing.cueNames, ['activate', 'error']);                    // the attempt activated, then failed
    const gone = await storeFixture();
    await gone.store.refresh();
    gone.store.settings = null;                                                   // the plugin went away: the native orb comes back
    await gone.store.stop();
    assert.equal(gone.mic.attrs.has('data-convo'), false);
    assert.equal(gone.mic.attrs.get('aria-label'), 'Microphone standby');

    const dev = await storeFixture({ settings: { ai_link_development_mode: true } });
    await dev.store.refresh();
    assert.equal(state(dev), 'idle');
    assert.match(dev.mic.attrs.get('aria-label'), /development mode/);
    await dev.store.toggle();                                                     // a click explains instead of failing a start
    assert.equal(dev.calls.length, 0);
    assert.match(dev.store.note, /development mode keeps browser voice idle/);
    assert.equal(state(dev), 'idle');
    assert.equal(dev.store.error, '');
});

test('Continuous drafting changes only the words of latched dictation: same state, glyph, pressed truth and cues', () => {
    const latched = view({ dictating: true, dictationMode: 'latched', continuous: true });
    assert.equal(latched.id, 'dictating-latched');
    assert.equal(latched.label, 'Dictating into the composer — keeps listening until you send or stop.');
    assert.equal(latched.title, 'Dictating into the composer · keeps listening until you send or stop; click to stop, Escape drops the phrase in progress');
    assert.equal(latched.pressed, true);
    const held = view({ dictating: true, dictationMode: 'latched', continuous: true, holding: true });   // the press is still down
    assert.equal(held.label, 'Dictating into the composer. Release to keep listening hands-free; keep holding for push-to-talk.');
    const ptt = view({ dictating: true, dictationMode: 'ptt', continuous: true });                    // push-to-talk reads as itself
    assert.equal(ptt.label, 'Push-to-talk. Listening only while held; release to finish.');
    assert.equal(view({ dictating: true }).label, 'Hands-free dictation. Listening for speech; click to stop, Escape cancels.');
    assert.equal(view({ active: false, continuous: true }).id, 'ready');                              // a flag alone is not dictation
    assert.equal(cueFor('ready', 'dictating-latched'), 'dictation-start');
});

test('The store says continuous only when the host drafts and Convo continues; the label follows a change of mind', async () => {
    const f = await storeFixture({ settings: { voice_message_draft: 'continuous' }, hostConfig: { message_mode: 'draft' } });
    await f.store.refresh();
    await f.store.beginDictation();
    assert.equal(state(f), 'dictating-latched');
    assert.equal(f.mic.attrs.get('aria-label'), 'Dictating into the composer — keeps listening until you send or stop.');
    assert.equal(f.mic.attrs.has('title'), true);                                 // no host tooltip system here: a plain title, written once
    f.nativeStore.config = { message_mode: 'send' };                              // Agent Zero now sends immediately: no longer continuous
    f.store.pressing = true; f.store.setPressing(false);                          // the next repaint
    assert.equal(f.mic.attrs.get('aria-label'), 'Hands-free dictation. Listening for speech; click to stop, Escape cancels.');
    await f.store.endDictation();
    const single = await storeFixture({ settings: { voice_message_draft: 'single' }, hostConfig: { message_mode: 'draft' } });
    await single.store.refresh();
    await single.store.beginDictation();
    assert.equal(single.mic.attrs.get('aria-label'), 'Hands-free dictation. Listening for speech; click to stop, Escape cancels.');
    await single.store.endDictation();
});

test('Playback ducked while a decision is pending shows as a modifier of speaking only', async () => {
    const f = await storeFixture();
    await f.store.start();
    f.audios[0].onStart(); f.audios[0].onProbe('wave');
    await new Promise(resolve => setTimeout(resolve, 5));
    const candidate = f.requests.find(item => item.event === 'convo.candidate').data;
    const send = event => f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, ...event } });
    send({ type: 'audio.begin', turn_id: 'reply', phrase_id: 'p' });
    assert.equal(state(f), 'speaking');
    assert.equal(f.mic.attrs.has('data-convo-hold'), false);
    send({ type: 'candidate.state', turn_id: candidate.turn_id, sequence: 1, state: 'withhold', final: false });   // a decision is pending
    assert.equal(f.store.policyHold, true);
    assert.equal(f.mic.attrs.has('data-convo-hold'), true);
    assert.equal(state(f), 'speaking');                                           // a modifier, not a different state
    send({ type: 'candidate.state', turn_id: candidate.turn_id, sequence: 1, state: 'ignore', final: false });
    assert.equal(f.mic.attrs.has('data-convo-hold'), false);
    send({ type: 'candidate.state', turn_id: candidate.turn_id, sequence: 1, state: 'withhold', final: false });
    await f.store.stop();
    assert.deepEqual([f.store.policyHold, f.mic.attrs.has('data-convo-hold'), state(f)], [false, false, 'ready']);
});
