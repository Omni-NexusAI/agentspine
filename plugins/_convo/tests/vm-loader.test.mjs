import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadModule, hostStubs, pluginPath } from './vm-loader.mjs';

test('Plugin-local specifiers resolve from disk; relative paths follow the importing file', async () => {
    assert.equal(pluginPath('/plugins/_convo/webui/defaults.js', 'x'), 'webui/defaults.js');
    assert.equal(pluginPath('./audio.js', 'webui/convo-store.js'), 'webui/audio.js');
    assert.equal(pluginPath('../helpers/x.js', 'webui/convo-store.js'), 'helpers/x.js');
    assert.equal(pluginPath('/js/api.js', 'webui/a.js'), null);
    const { namespace } = await loadModule('webui/defaults.js');
    assert.equal(namespace.defaults.roles.tts.backend, 'openai');
    assert.equal(typeof namespace.fillDefaults, 'function');
});

test('Host imports must be supplied; an unlisted one fails loudly instead of loading nothing', async () => {
    await assert.rejects(() => loadModule('webui/setup-store.js'), /Unexpected import "\/js\/AlpineStore\.js" \(host or foreign-plugin module/);
    const { namespace } = await loadModule('webui/setup-store.js', { bindings: hostStubs() });
    assert.equal(typeof namespace.store.load, 'function');
});

test('A dynamic import inside a store resolves through the same bindings', async () => {
    const calls = [];
    const bindings = hostStubs({ '/js/websocket.js': { createNamespacedClient: () => ({ addHandlers() {}, async on() {}, onDisconnect() {}, disconnect() {}, async request(event) { calls.push(event); return { results: [] }; } }) } ,
        '/js/shortcuts.js': { getCurrentContextId: () => 'a', getCurrentContext: () => ({ id: 'a' }), openModal: async () => {} },
        './audio.js': { AudioSession: class {} } });
    const { namespace } = await loadModule('webui/convo-store.js', { bindings, globals: { document: { getElementById: () => null }, fetch: async () => ({ ok: false }) } });
    assert.equal(typeof namespace.store.statusLabel, 'string');
});
