import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const read = name => readFile(new URL('../webui/' + name, import.meta.url), 'utf8');

// The eye's methods, lifted from the store's source so they are tested without the host's modules.
async function eye(state) {
    const source = await read('setup-store.js');
    const start = source.indexOf('    setKey(role, value)'), end = source.indexOf('    },', source.indexOf('    async toggleKey(role)')) + 6;
    const methods = new Function('return {' + source.slice(start, end) + '}')();
    return Object.assign(methods, { keys: {}, credentials: {}, revealed: {} }, state);
}

test('A key being typed is shown from the field; nothing is asked of the server', async () => {
    const calls = [], store = await eye({ api: async (...args) => { calls.push(args); return {}; } });
    store.setKey('conversation', 'typed-test-key');
    assert.equal(await store.toggleKey('conversation'), true);
    assert.deepEqual([store.keyShown('conversation'), store.keyValue('conversation'), calls.length], [true, 'typed-test-key', 0]);
    assert.equal(await store.toggleKey('conversation'), false);                                   // second click hides it
    assert.equal(store.keyShown('conversation'), false);
});

test('A saved key is fetched on the click only: whole behind a login, its ending without one, and typing hides it', async () => {
    let answer = { ok: true, whole: true, key: 'saved-test-key-0123', ending: '' };
    const calls = [], store = await eye({ credentials: { tts: true }, api: async (action, input) => { calls.push([action, input]); return answer; } });
    assert.equal(store.keyValue('tts'), '');                                                      // nothing before the click
    assert.equal(await store.toggleKey('tts'), true);
    assert.deepEqual([calls, store.keyValue('tts'), store.keyNote('tts')], [[['key_reveal', { role: 'tts' }]], 'saved-test-key-0123', '']);
    assert.deepEqual(store.keys, {});                                                             // never becomes an edit, so never part of a save
    store.setKey('tts', 'n');
    assert.deepEqual([store.keyShown('tts'), store.keyValue('tts')], [false, 'n']);
    store.setKey('tts', '');
    answer = { ok: true, whole: false, key: '', ending: '0123' };
    assert.equal(await store.toggleKey('tts'), false);
    assert.deepEqual([store.keyShown('tts'), store.keyValue('tts')], [false, '']);
    assert.match(store.keyNote('tts'), /^The saved key ends in 0123\. The whole key is shown only when Agent Zero has a login set/);
    assert.equal(await store.toggleKey('tts'), false);                                            // the note closes on the next click
    assert.equal(store.keyNote('tts'), '');
});

test('No saved key, or a failed request, says so and shows nothing', async () => {
    const store = await eye({ api: async () => { throw new Error('offline'); } });
    await store.toggleKey('classifier');
    assert.equal(store.keyNote('classifier'), 'No key is saved for this card.');
    store.revealed = {}; store.credentials = { classifier: true };
    await store.toggleKey('classifier');
    assert.deepEqual([store.keyShown('classifier'), store.keyNote('classifier')], [false, 'The key could not be shown: offline']);
});

test('Every card with its own key field has the eye, and loading or resetting the settings hides what was shown', async () => {
    for (const [file, name] of [['conversation-model-field.html', 'convo-key-eye'], ['classifier-model-field.html', 'classifier-key-eye'], ['speech-output-field.html', 'speech-key-eye']]) {
        const card = await read(file);
        assert.ok(card.includes(`data-convo-field="${name}"`) && card.includes('@click="$store.convoSetup.toggleKey(role)"'), file);
        assert.ok(card.includes(`:type="$store.convoSetup.keyShown(role) ? 'text' : 'password'"`), file);
        assert.doesNotMatch(card, /type="password"/, file);
    }
    const source = await read('setup-store.js');
    assert.equal(source.split('this.keys = {}; this.revealed = {}').length - 1, 3);
    assert.equal(source.split('this.keys = {}').length - 1, 3);
});
