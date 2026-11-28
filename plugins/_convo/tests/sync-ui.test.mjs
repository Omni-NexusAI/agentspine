import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { loadModule, hostStubs, reactive } from './vm-loader.mjs';
import { settingsMarkup } from './settings-markup.mjs';
import { defaults, getPath, mergeSettings, setPath } from '../webui/defaults.js';

const plain = value => JSON.parse(JSON.stringify(value));
const merge = (base, mine, theirs) => plain(mergeSettings(base, mine, theirs));

test('A leaf only I changed is kept, only they changed is taken, and a leaf we both changed is a conflict', () => {
    const base = { a: 1, b: 'x', nested: { c: 1, d: 1 }, list: [1, 2], gone: 1, kept: 1 };
    const mine = { a: 2, b: 'x', nested: { c: 1, d: 5 }, list: [1, 2], gone: 1, kept: 1, added_by_me: true };
    const theirs = { a: 1, b: 'y', nested: { c: 9, d: 1 }, list: [1, 2, 3], gone: 1, kept: 1, added_by_them: 'new' };
    const { merged, conflicts } = merge(base, mine, theirs);
    assert.deepEqual(merged, { a: 2, b: 'y', nested: { c: 9, d: 5 }, list: [1, 2, 3], gone: 1, kept: 1, added_by_me: true, added_by_them: 'new' });
    assert.deepEqual(conflicts, []);
    const clash = merge({ x: 1, deep: { y: 1 } }, { x: 2, deep: { y: 3 } }, { x: 5, deep: { y: 4 } });
    assert.deepEqual(clash.conflicts, [{ path: 'x', mine: 2, theirs: 5 }, { path: 'deep.y', mine: 3, theirs: 4 }]);
    assert.deepEqual(clash.merged, { x: 2, deep: { y: 3 } });                                          // mine stays until the user chooses
});

test('Identical changes agree, arrays are atomic, deletions merge, and a type change is one leaf', () => {
    assert.deepEqual(merge({ a: 1 }, { a: 2 }, { a: 2 }).conflicts, []);
    const lists = merge({ names: ['a'] }, { names: ['a', 'b'] }, { names: ['a', 'c'] });
    assert.deepEqual(lists.conflicts, [{ path: 'names', mine: ['a', 'b'], theirs: ['a', 'c'] }]);        // no element-wise merge of a list
    assert.deepEqual(merge({ k: 1, j: 1 }, { j: 1 }, { k: 1, j: 1 }).merged, { j: 1 });                  // I deleted it, they did not touch it
    assert.deepEqual(merge({ k: 1, j: 1 }, { k: 1, j: 1 }, { j: 1 }).merged, { j: 1 });                  // they deleted it, I did not touch it
    assert.deepEqual(merge({ k: 1 }, { k: 2 }, {}).conflicts, [{ path: 'k', mine: 2, theirs: undefined }].map(plain));   // I edited what they deleted
    const typed = merge({ v: { a: 1 } }, { v: 'text' }, { v: { a: 2 } });
    assert.equal(typed.conflicts.length, 1);
    assert.equal(typed.conflicts[0].path, 'v');
    const empty = merge({}, {}, {});
    assert.deepEqual(empty, { merged: {}, conflicts: [] });
});

test('A subtree removed on one side and untouched on the other, or removed on both, is gone rather than left as an empty shell', () => {
    const base = { tts: { voice: 'a', saved: { openai: { voice: 'x' } } } };
    assert.deepEqual(merge(base, { tts: { voice: 'a' } }, base).merged, { tts: { voice: 'a' } });                    // I removed it
    assert.deepEqual(merge(base, base, { tts: { voice: 'a' } }).merged, { tts: { voice: 'a' } });                    // they removed it
    assert.deepEqual(merge(base, { tts: { voice: 'b' } }, { tts: { voice: 'a' } }).merged, { tts: { voice: 'b' } }); // both removed it
    const edited = merge(base, { tts: { voice: 'a' } }, { tts: { voice: 'a', saved: { openai: { voice: 'y' } } } });  // removed here, edited there
    assert.deepEqual(edited.conflicts, [{ path: 'tts.saved.openai.voice', theirs: 'y' }]);
    const added = merge({}, { s: { a: 1 } }, {});
    assert.deepEqual([added.merged, added.conflicts], [{ s: { a: 1 } }, []]);                                       // an added subtree is kept
});

test('The merge never mutates its inputs and returns independent copies', () => {
    const base = { o: { a: 1 } }, mine = { o: { a: 2 } }, theirs = { o: { a: 1 }, p: { q: 1 } };
    const frozen = plain([base, mine, theirs]);
    const { merged } = mergeSettings(base, mine, theirs);
    assert.deepEqual(plain([base, mine, theirs]), frozen);
    merged.p.q = 99; merged.o.a = 99;
    assert.deepEqual(plain([base, mine, theirs]), frozen);
    const target = { a: { b: { c: 1 } } };
    assert.equal(getPath(target, 'a.b.c'), 1);
    assert.equal(getPath(target, 'a.x.c'), undefined);
    setPath(target, 'a.b.d', { e: 1 }); setPath(target, 'z.y', 'v'); setPath(target, 'a.b.c', undefined);
    assert.deepEqual(plain(target), { a: { b: { d: { e: 1 } } }, z: { y: 'v' } });
});

async function fixture(initial, { revision = 'r1' } = {}) {
    const state = { config: plain(initial), revision, calls: [], confirms: 0, refreshes: 0 };
    const respond = () => ({ ok: true, config: plain(state.config), revision: state.revision, readiness: [], credentials: {}, decision_backends: [], providers: [], native: {} });
    const bindings = hostStubs({ '/js/api.js': { callJsonApi: async (_url, payload) => {
        state.calls.push(payload.action);
        if (payload.action === 'setup_save') { state.config = plain(payload.config); state.revision = 'saved-' + state.calls.length; }
        return respond();
    } } });
    const { namespace } = await loadModule('webui/setup-store.js', { bindings, globals: {
        window: { confirm: () => { state.confirms++; return true; } },
        Alpine: { store: name => name === 'convo' ? { refresh: async () => { state.refreshes++; } } : undefined } } });
    await namespace.store.load();
    return { store: namespace.store, state, elsewhere: (path, value, revision) => { setPath(state.config, path, value); state.revision = revision; } };
}

test('A clean editor refreshes silently when another editor saved; an edited one is flagged, never overwritten', async () => {
    const clean = await fixture(defaults);
    clean.elsewhere('style', 'saved by the other editor', 'r2');
    await clean.store.noteRevision('r2');
    assert.equal(clean.store.config.style, 'saved by the other editor');
    assert.deepEqual([clean.store.revision, clean.store.dirty, clean.store.stale, clean.state.confirms], ['r2', false, false, 0]);   // no prompt: nothing of mine to lose

    const edited = await fixture(defaults);
    edited.store.config.style = 'my unsaved words';
    edited.elsewhere('hotkey', 'Ctrl+Alt+K', 'r2');
    const calls = edited.state.calls.length;
    await edited.store.noteRevision('r2');
    assert.equal(edited.store.stale, true);
    assert.equal(edited.store.config.style, 'my unsaved words');                                        // my edits are intact
    assert.equal(edited.state.calls.length, calls);                                                     // and nothing was fetched or written
    await edited.store.noteRevision('r2');                                                              // the same change is not announced twice
    await edited.store.noteRevision('r1'); await edited.store.noteRevision(''); await edited.store.noteRevision(undefined);
    assert.equal(edited.state.calls.length, calls);
    edited.store.busy = true; edited.store.stale = false; edited.store.revisionSeen = '';
    await edited.store.noteRevision('r9');
    assert.equal(edited.store.stale, false);                                                            // never mid-save
});

test('Merging keeps my edits, brings in theirs, and leaves a choice for fields changed in both places', async () => {
    const f = await fixture(defaults);
    f.store.setKey('conversation', 'typed-key');                                                        // a pending credential
    f.store.config.style = 'mine';
    f.store.config.roles.conversation.model = 'my-model';
    f.elsewhere('hotkey', 'Ctrl+Alt+K', 'r2');
    f.elsewhere('roles.conversation.model', 'their-model', 'r2');
    f.elsewhere('max_audio_seconds', 30, 'r2');
    await f.store.noteRevision('r2');
    await f.store.mergeLatest();
    assert.equal(f.store.config.style, 'mine');                                                         // only I changed it
    assert.equal(f.store.config.hotkey, 'Ctrl+Alt+K');                                                  // only they changed it
    assert.equal(f.store.config.max_audio_seconds, 30);
    assert.equal(f.store.config.roles.conversation.model, 'my-model');                                  // both: mine until chosen
    assert.deepEqual(plain(f.store.conflicts), [{ path: 'roles.conversation.model', mine: 'my-model', theirs: 'their-model' }]);
    assert.deepEqual([f.store.revision, f.store.stale, f.store.dirty], ['r2', false, true]);             // saving now is against the newest revision
    assert.deepEqual(plain(f.store.keys), { conversation: 'typed-key' });                                 // credentials never merge or vanish
    f.store.takeTheirs('roles.conversation.model');
    assert.equal(f.store.config.roles.conversation.model, 'their-model');
    assert.deepEqual(plain(f.store.conflicts), []);
    await f.store.save();
    assert.equal(f.state.config.style, 'mine');
    assert.equal(f.state.config.hotkey, 'Ctrl+Alt+K');
    assert.equal(f.state.refreshes, 1);                                                                 // a saved change reaches the panel at once
});

test('Keep-mine drops the choice, an agreeing merge has no conflicts, and discarding needs no prompt', async () => {
    const f = await fixture(defaults);
    f.store.config.style = 'mine';
    f.elsewhere('style', 'theirs', 'r2');
    await f.store.noteRevision('r2');
    await f.store.mergeLatest();
    assert.equal(f.store.conflicts.length, 1);
    f.store.keepMine('style');
    assert.deepEqual([f.store.conflicts.length, f.store.config.style], [0, 'mine']);
    assert.match(f.store.message, /Some fields changed in both places/);

    const g = await fixture(defaults);
    g.store.config.style = 'same';
    g.elsewhere('style', 'same', 'r2');
    await g.store.noteRevision('r2');
    await g.store.mergeLatest();
    assert.deepEqual([g.store.conflicts.length, g.store.dirty], [0, false]);                            // we agreed: nothing left to save
    assert.match(g.store.message, /Merged: the other changes were brought in/);

    const h = await fixture(defaults);
    h.store.config.style = 'dropped'; h.store.setKey('tts', 'typed');
    h.elsewhere('style', 'kept on server', 'r2');
    await h.store.noteRevision('r2');
    await h.store.discardAndReload();
    assert.deepEqual([h.store.config.style, h.store.stale, h.state.confirms, plain(h.store.keys)], ['kept on server', false, 0, {}]);
});

test('The status poll hands the saved revision to an open editor, and a returning tab checks at once', async () => {
    const noted = [], listeners = {};
    const document = { hidden: true, getElementById: () => null, addEventListener: (type, fn) => { listeners[type] = fn; } };
    const bindings = hostStubs({
        '/js/api.js': { callJsonApi: async (_url, payload) => payload.action === 'status' ? { settings: { enabled: true, max_audio_seconds: 20 }, settings_revision: 'abc123', runtime: { mode: 'external' } } : { events: [], jobs: [] } },
        '/js/websocket.js': { createNamespacedClient: () => ({}) },
        '/js/shortcuts.js': { getCurrentContextId: () => 'a', getCurrentContext: () => ({ id: 'a' }), openModal: async () => {} },
        './audio.js': { AudioSession: class {} },
    });
    const { namespace } = await loadModule('webui/convo-store.js', { bindings, globals: {
        document, window: { addEventListener() {} }, setInterval: () => 0,
        Alpine: { store: name => name === 'convoSetup' ? { noteRevision: async revision => noted.push(revision) } : undefined } } });
    await namespace.store.refresh();
    assert.deepEqual(noted, ['abc123']);
    assert.equal(namespace.store.settingsRevision, 'abc123');
    namespace.install();                                                                                // installing also does one first refresh
    await new Promise(resolve => setTimeout(resolve, 5));
    const settled = noted.length;
    listeners.visibilitychange();                                                                       // still hidden: no request
    await new Promise(resolve => setTimeout(resolve, 5));
    assert.equal(noted.length, settled);
    document.hidden = false;
    listeners.visibilitychange();                                                                       // visible again: ask whether anything changed
    await new Promise(resolve => setTimeout(resolve, 5));
    assert.equal(noted.length, settled + 1);
});

test('Both entry points show the same banner, because both embed the same settings component', async () => {
    const read = name => readFile(new URL('../webui/' + name, import.meta.url), 'utf8');
    const [shell, banner, config, modal] = await Promise.all([read('settings-controls.html'), read('settings-banner.html'), read('config.html'), read('settings-modal.html')]);
    assert.match(config, /settings-controls\.html/);
    assert.match(modal, /settings-controls\.html/);
    assert.match(shell, /<x-component path="\/plugins\/_convo\/webui\/settings-banner\.html">/);
    for (const word of ['These settings changed elsewhere', 'Merge changes', 'Discard mine and reload', 'Changed in both places', 'Keep mine', 'Take theirs']) assert.match(banner, new RegExp(word));
    assert.match(banner, /role="alert" x-show="\$store\.convoSetup\.stale"/);
    assert.doesNotMatch(await settingsMarkup(), /api_key|_convo_key/);
});

test('Merging and setting values work on Alpine-like reactive state, arrays included', () => {
    const base = { activation_names: ['Jarvis'], roles: { tts: { voice: 'a', saved: { openai: { voice: 'a' } } } } };
    const mine = reactive({ activation_names: ['Jarvis', 'Computer'], roles: { tts: { voice: 'b', saved: { openai: { voice: 'a' } } } } });
    const theirs = reactive({ activation_names: ['Jarvis'], roles: { tts: { voice: 'a', saved: { openai: { voice: 'c' } } } } });
    const { merged, conflicts } = mergeSettings(base, mine, theirs);
    assert.deepEqual(plain(merged), { activation_names: ['Jarvis', 'Computer'], roles: { tts: { voice: 'b', saved: { openai: { voice: 'c' } } } } });
    assert.deepEqual(conflicts, []);
    const target = {};
    setPath(target, 'activation_names', reactive(['Jarvis', 'Friday']));
    setPath(target, 'roles.tts.saved', reactive({ openai: { voice: 'x' } }));
    assert.deepEqual(plain(target), { activation_names: ['Jarvis', 'Friday'], roles: { tts: { saved: { openai: { voice: 'x' } } } } });
});
