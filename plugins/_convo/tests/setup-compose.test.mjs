// The settings store is one core plus methods-only card mixins; `patch` writes some fields now and `adopt` merges the answer
// three ways. Run on an Alpine-like reactive store (structuredClone of store state would throw there, as in the browser).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadModule, hostStubs } from './vm-loader.mjs';
import { defaults, getPath, setPath } from '../webui/defaults.js';

const plain = value => JSON.parse(JSON.stringify(value));
const MIXINS = { 'webui/conversation-setup.js': 'conversationSetup', 'webui/classifier-setup.js': 'classifierSetup',
                 'webui/speech-setup.js': 'speechSetup', 'webui/dictation-setup.js': 'dictationSetup' };

// A server with one saved settings file: setup_save replaces it, setup_patch is compare-and-set per path (as the real one).
async function rig({ reactive = true, hold = null } = {}) {
    const server = { config: plain(defaults), revision: 'r1', calls: [], writes: 0 };
    const answer = extra => ({ ok: true, config: plain(server.config), revision: server.revision, readiness: [], credentials: {}, decision_backends: [], providers: [], native: {}, ...extra });
    const bindings = hostStubs({ '/js/api.js': { callJsonApi: async (_url, payload) => {
        server.calls.push(plain(payload));
        if (hold?.[payload.action]) await hold[payload.action];
        if (payload.action === 'setup_save') { server.config = plain(payload.config); server.revision = 'r' + (++server.writes + 1); }
        if (payload.action === 'setup_patch') {
            const conflicts = [];
            for (const change of payload.changes) {
                const now = getPath(server.config, change.path);
                if (JSON.stringify(now) === JSON.stringify(change.base)) setPath(server.config, change.path, change.value);
                else conflicts.push({ path: change.path, mine: change.value ?? null, theirs: now ?? null });
            }
            if (conflicts.length < payload.changes.length) server.revision = 'r' + (++server.writes + 1);
            return answer({ conflicts });
        }
        if (payload.action === 'fail') return { ok: false, error: 'no' };
        return answer();
    } } }, { reactive });
    const { namespace } = await loadModule('webui/setup-store.js', { bindings, globals: { window: { confirm: () => true } } });
    await namespace.store.load();
    return { store: namespace.store, compose: namespace.compose, server,
             elsewhere: (path, value) => { setPath(server.config, path, value); server.revision = 'r' + (++server.writes + 1); } };
}

test('Each card mixin holds methods only, and every one of them reaches the store', async () => {
    const { store } = await rig({ reactive: false });
    for (const [file, name] of Object.entries(MIXINS)) {
        const { namespace } = await loadModule(file, { bindings: hostStubs() });
        const mixin = namespace[name];
        assert.ok(mixin && typeof mixin === 'object', file);
        for (const [key, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(mixin))) {
            assert.equal(typeof descriptor.value, 'function', `${file}: ${key} must be a method`);
            assert.equal(descriptor.get, undefined, `${file}: ${key} must not be a getter`);
            assert.equal(typeof store[key], 'function', `${file}: ${key} reaches the store`);
        }
    }
    assert.equal(typeof store.setBackend, 'function');
    assert.equal(typeof store.decisionOptions, 'function');                              // the developer backend picker in Tune
    // The model-role factory gives both model cards the same role-prefixed methods; compose would refuse one defined twice.
    for (const name of ['Init', 'ProviderOptions', 'SetProvider', 'Search', 'Group', 'Flush', 'UsesHostKey', 'SaveHostKey', 'Rows'])
        for (const role of ['conversation', 'classifier']) assert.equal(typeof store[role + name], 'function', role + name);
});

test('Composition keeps the core getters live and refuses a getter, a value or a name defined twice', async () => {
    const { store, compose } = await rig({ reactive: false });
    const core = Object.getOwnPropertyDescriptors(store);                                // the plain stub store is the composed object itself
    assert.equal(typeof core.dirty?.get, 'function');                                    // still a getter on the composed store
    assert.equal(typeof core.decider?.get, 'function');
    assert.equal(store.dirty, false);
    store.config.style = 'edited';
    assert.equal(store.dirty, true);                                                     // computed now, not copied at build time
    const base = { n: 1, get twice() { return this.n * 2; }, hello() { return 'core'; } };
    const built = compose(base, { add() { this.n++; } });
    built.add();
    assert.equal(built.twice, 4);
    assert.throws(() => compose(base, { get computed() { return 1; } }), /"computed" must be a method/);
    assert.throws(() => compose(base, { value: 3 }), /"value" must be a method/);
    assert.throws(() => compose(base, { hello() {} }), /"hello" is defined twice/);
    assert.throws(() => compose(base, { a() {} }, { a() {} }), /"a" is defined twice/);
});

test('A patch saves only its paths against what this editor loaded, and every other unsaved edit survives', async () => {
    const f = await rig();
    f.store.config.style = 'unsaved words';                                               // an edit that is not part of the patch
    f.store.setKey('conversation', 'typed-key');                                          // a pending key
    const result = await f.store.patch({ 'roles.conversation.model': 'omni', 'voice_message_draft': 'continuous' });
    assert.ok(result);
    const sent = f.server.calls.at(-1);
    assert.equal(sent.action, 'setup_patch');
    assert.deepEqual(sent.changes, [{ path: 'roles.conversation.model', base: '', value: 'omni' }, { path: 'voice_message_draft', base: 'single', value: 'continuous' }]);
    assert.doesNotMatch(JSON.stringify(sent), /typed-key|unsaved words/);                 // nothing else, and never a key
    assert.deepEqual([f.server.config.roles.conversation.model, f.server.config.voice_message_draft, f.server.config.style], ['omni', 'continuous', '']);
    assert.equal(f.store.revision, f.server.revision);
    assert.equal(f.store.config.style, 'unsaved words');                                  // kept, and still unsaved
    assert.deepEqual(plain(f.store.keys), { conversation: 'typed-key' });
    assert.equal(f.store.dirty, true);
    f.store.config.style = '';
    f.store.keys = {};
    assert.equal(f.store.dirty, false);                                                   // the patched paths are saved, so clean
});

test('A field changed elsewhere meanwhile comes back as a conflict; other changes from there come in', async () => {
    const f = await rig();
    f.elsewhere('roles.conversation.model', 'their-model');                               // this editor has not seen these yet
    f.elsewhere('hotkey', 'Ctrl+Alt+K');
    const result = await f.store.patch({ 'roles.conversation.model': 'my-model' });
    assert.deepEqual(plain(result.conflicts), [{ path: 'roles.conversation.model', mine: 'my-model', theirs: 'their-model' }]);
    assert.equal(f.server.config.roles.conversation.model, 'their-model');                // compare-and-set refused the stale base
    assert.equal(f.store.config.roles.conversation.model, 'my-model');                    // mine is kept until chosen...
    assert.deepEqual(plain(f.store.conflicts), [{ path: 'roles.conversation.model', mine: 'my-model', theirs: 'their-model' }]);
    assert.equal(f.store.config.hotkey, 'Ctrl+Alt+K');                                    // ...and the other change came in
    f.store.takeTheirs('roles.conversation.model');
    assert.equal(f.store.config.roles.conversation.model, 'their-model');
    assert.deepEqual([f.store.conflicts.length, f.store.dirty], [0, false]);
});

test('An unresolved conflict stays listed across a later patch, with what is saved now', async () => {
    const f = await rig();
    f.store.config.style = 'mine';
    f.elsewhere('style', 'theirs');
    await f.store.noteRevision(f.server.revision);
    await f.store.mergeLatest();
    assert.deepEqual(plain(f.store.conflicts.map(item => item.path)), ['style']);
    await f.store.patch({ 'roles.tts.voice': 'alloy' });
    assert.deepEqual(plain(f.store.conflicts), [{ path: 'style', mine: 'mine', theirs: 'theirs' }]);
    assert.equal(f.store.config.style, 'mine');
});

test('A patch waits for a save in flight, and a save waits for a patch in flight; their own writes are not "elsewhere"', async () => {
    let releaseSave; const saveHeld = new Promise(resolve => { releaseSave = resolve; });
    const f = await rig({ hold: { setup_save: saveHeld } });
    f.store.config.style = 'saved by save';
    const saving = f.store.save();
    const patching = f.store.patch({ 'hotkey': 'Ctrl+Alt+P' });
    await new Promise(resolve => setTimeout(resolve, 5));
    assert.deepEqual(f.server.calls.filter(call => call.action !== 'setup_read').map(call => call.action), ['setup_save']);   // the patch has not been sent
    releaseSave();
    assert.equal(await saving, true);
    await patching;
    const patch = f.server.calls.at(-1);
    assert.equal(patch.action, 'setup_patch');
    assert.deepEqual(patch.changes, [{ path: 'hotkey', base: 'Ctrl+Shift+Space', value: 'Ctrl+Alt+P' }]);   // its base is what the save stored
    assert.deepEqual([f.server.config.style, f.server.config.hotkey], ['saved by save', 'Ctrl+Alt+P']);
    assert.equal(f.store.dirty, false);

    let releasePatch; const patchHeld = new Promise(resolve => { releasePatch = resolve; });
    const g = await rig({ hold: { setup_patch: patchHeld } });
    const pending = g.store.patch({ 'style': 'patched' });
    await new Promise(resolve => setTimeout(resolve, 5));
    await g.store.noteRevision('r-from-our-own-patch');
    assert.equal(g.store.stale, false);                                                    // not flagged while our patch is in flight
    g.store.config.hotkey = 'Ctrl+Alt+S';
    const saved = g.store.save();
    await new Promise(resolve => setTimeout(resolve, 5));
    assert.equal(g.server.calls.filter(call => call.action === 'setup_save').length, 0);  // the save waits for the patch
    releasePatch();
    await pending; assert.equal(await saved, true);
    assert.equal(g.server.calls.at(-1).revision, 'r2');                                    // the save carries the patch's revision
    assert.deepEqual([g.server.config.style, g.server.config.hotkey], ['patched', 'Ctrl+Alt+S']);
});

test('A failed patch is reported, keeps the draft, and a removal is sent without a value', async () => {
    const f = await rig();
    f.store.api = async () => { throw new Error('Settings could not be written'); };
    assert.equal(await f.store.patch({ 'style': 'kept' }), null);
    assert.match(f.store.error, /could not be written/);
    assert.equal(f.store.config.style, 'kept');
    assert.equal(f.store.dirty, true);
    const g = await rig();
    g.server.config.roles.tts.saved = { openai: { voice: 'a' } };
    await g.store.load(true);
    await g.store.patch({ 'roles.tts.saved': undefined });
    assert.deepEqual(g.server.calls.at(-1).changes, [{ path: 'roles.tts.saved', base: { openai: { voice: 'a' } } }]);
    assert.equal('saved' in g.store.config.roles.tts, false);
    assert.equal(await g.store.patch({}), null);                                          // nothing to write: no request
    assert.equal(g.server.calls.at(-1).action, 'setup_patch');
});
