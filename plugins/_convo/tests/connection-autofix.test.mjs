import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const read = name => readFile(new URL('../webui/' + name, import.meta.url), 'utf8');

// The fix methods, lifted from the store's source so they run without the host's modules.
async function fixer(state) {
    const source = await read('setup-store.js');
    const start = source.indexOf('    fixes: {},'), end = source.indexOf('    certificateChanged(role)');
    const methods = new Function('return {' + source.slice(start, end) + '}')();
    const store = Object.assign(methods, { fixes: {}, ui: { conversation: {}, classifier: {} }, used: [], chosen: [],
        config: { roles: { conversation: { model: 'my-model' }, classifier: {}, tts: { url: 'http://localhost:8080/v1' } } },
        hints: {}, close: {}, address: 'http://localhost:8080/v1',
        connectionSuggestion(role) { return this.hints[role] || null; },
        conversationUrl() { return this.address; },
        conversationUseAddress(url, autosave) { this.used.push(['conversation', url, autosave]); this.address = url; delete this.hints.conversation; },
        speechUseAddress(url) { this.used.push(['tts', url]); this.config.roles.tts.url = url; delete this.hints.tts; },
        conversationCloseMatch() { return this.close.conversation || ''; },
        conversationChooseModel(id, autosave) { this.chosen.push([id, autosave]); this.config.roles.conversation.model = id; delete this.close.conversation; },
    }, state);
    return store;
}

test('A refused address whose usual port answers is used by itself, once, with a line and Undo', async () => {
    const store = await fixer({});
    store.hints.conversation = { kind: 'port', url: 'http://localhost:8890/v1', label: 'Use port 8890', message: 'x' };
    store.ui.conversation.autosave = true;
    assert.equal(await store.connectionAutoFix('conversation'), 'address');
    assert.deepEqual(store.used, [['conversation', 'http://localhost:8890/v1', true]]);           // saved the way the card saves
    assert.equal(store.connectionFixNote('conversation'), 'Using http://localhost:8890/v1: nothing answers at http://localhost:8080/v1, and this server\u2019s usual port does.');
    assert.equal(await store.connectionFixUndo('conversation'), true);
    assert.deepEqual([store.address, store.connectionFixNote('conversation')], ['http://localhost:8080/v1', '']);
    store.hints.conversation = { kind: 'port', url: 'http://localhost:8890/v1' };                  // the same answer again after Undo
    assert.equal(await store.connectionAutoFix('conversation'), '');                              // is not applied a second time
    assert.equal(store.used.length, 2);
    assert.equal(await store.connectionFixUndo('conversation'), false);
});

test('The private-address suggestion stays a click; the line goes when the address is edited by hand', async () => {
    const store = await fixer({});
    store.hints.tts = { kind: 'private', url: 'http://127.0.0.1:8890/v1' };
    assert.equal(await store.connectionAutoFix('tts'), '');
    assert.deepEqual(store.used, []);
    store.hints.tts = { kind: 'port', url: 'http://localhost:8890/v1' };
    assert.equal(await store.connectionAutoFix('tts'), 'address');
    assert.match(store.connectionFixNote('tts'), /^Using http:\/\/localhost:8890\/v1/);
    store.config.roles.tts.url = 'http://elsewhere:9000/v1';
    assert.equal(store.connectionFixNote('tts'), '');
    for (const hint of [null, {}, { kind: 'port' }, { kind: 'port', url: 5 }, { url: 'http://localhost:1/v1' }]) {
        store.hints.tts = hint;
        assert.equal(await store.connectionAutoFix('tts'), '');
    }
    assert.equal(await store.connectionAutoFix('unknown'), '');
});

test('A model name the server spells with other capitals is replaced by the server\u2019s spelling, with Undo', async () => {
    const store = await fixer({});
    store.close.conversation = 'My-Model';
    assert.equal(await store.connectionAutoFix('conversation'), 'model');
    assert.deepEqual([store.chosen, store.config.roles.conversation.model], [[['My-Model', false]], 'My-Model']);
    assert.equal(store.connectionFixNote('conversation'), 'Using \u201cMy-Model\u201d, the server\u2019s spelling of the model name you entered.');
    await store.connectionFixUndo('conversation');
    assert.equal(store.config.roles.conversation.model, 'my-model');
    store.close.conversation = 'My-Model';
    assert.equal(await store.connectionAutoFix('conversation'), '');                              // undone stays undone
});

test('Every answered check runs the fixes, the server marks which suggestion is which, and each card has the line', async () => {
    const source = await read('setup-store.js');
    assert.ok(source.includes('if (answered) await this.connectionAutoFix(role);'));
    assert.ok((await read('classifier-setup.js')).includes("await this.connectionAutoFix('classifier')"));
    const net = await readFile(new URL('../helpers/convo_net.py', import.meta.url), 'utf8');
    assert.ok(net.includes("'kind': 'port'") && net.includes("'kind': 'private'"));
    for (const [file, prefix] of [['conversation-model-field.html', ''], ['classifier-model-field.html', 'classifier-'], ['speech-output-field.html', 'speech-']]) {
        const card = await read(file);
        assert.ok(card.includes(`data-convo-field="${prefix}fix-note"`) && card.includes(`data-convo-field="${prefix}fix-undo" @click="$store.convoSetup.connectionFixUndo(role)"`), file);
    }
});
