// The shared model-role factory (webui/model-role-setup.js) behind the conversation and classifier cards, on the real settings
// store run Alpine-like reactive, with a compare-and-set server and a virtual clock. It checks what the factory adds beyond the
// conversation card's own tests (model-type-ui.test.mjs, unchanged): one autosave timer per role, Agent Zero's store read at the
// moment of use (providers, details, search, grouping, provider-change hook), and unknown OpenAI-compatible providers.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadModule, hostStubs } from './vm-loader.mjs';
import { defaults, getPath, setPath } from '../webui/defaults.js';
import { modelRoleSetup, roleUrl, hostDetail, compatibleBase, AUTOSAVE_MS } from '../webui/model-role-setup.js';
import { conversationUrl, UNKNOWN_COMPATIBLE } from '../webui/conversation-setup.js';

const plain = value => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const settle = async (rounds = 25) => { for (let i = 0; i < rounds; i++) await new Promise(resolve => setImmediate(resolve)); };
function virtualClock() {
    let now = 0, next = 1;
    const timers = new Map();
    return {
        setTimeout(fn, ms = 0) { const id = next++; timers.set(id, { at: now + ms, fn }); return id; },
        clearTimeout(id) { timers.delete(id); },
        async advance(ms) {
            const end = now + ms;
            for (;;) {
                const due = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
                if (!due) break;
                timers.delete(due[0]); now = due[1].at; due[1].fn(); await settle();
            }
            now = end; await settle();
        },
    };
}

async function rig({ config = {}, host: extraHost = {} } = {}) {
    const clock = virtualClock();
    const server = { config: plain(defaults), revision: 'r1', writes: 0, calls: [] };
    server.config.roles.classifier = { provider: '', key_source: 'provider', api_base: '', context_window: 8192, kind: 'auto', ...server.config.roles.classifier };
    for (const [path, value] of Object.entries(config)) setPath(server.config, path, value);
    const catalog = { providers: { openai: { label: 'OpenAI', group: 'chat', kinds: ['chat'], base: 'https://api.openai.com/v1', host_key: 'openai' } }, disabled: {} };
    const answer = extra => ({ ok: true, config: plain(server.config), revision: server.revision, readiness: [], credentials: {}, decision_backends: [],
                               classifier_catalog: catalog, ...extra });
    const host = {
        chatProviders: [{ value: 'openai', label: 'OpenAI' }, { value: 'openrouter', label: 'OpenRouter' }, { value: 'router_x', label: 'Router X' }],
        chatProviderDetails: [{ value: 'router_x', litellm_provider: 'openai', api_base: 'http://192.168.1.50:4000/v1' }],
        apiKeyStatus: {}, apiKeyValues: {}, apiKeyDirty: {}, searches: [],
        async ensureLoaded() {}, async refreshApiKeyStatus() {},
        async searchModels(...args) { this.searches.push(args); return ['m-1', 'm-2']; },
        groupResults(models) { return { matched: [], rest: models }; },
        ...extraHost,
    };
    const bindings = hostStubs({
        '/js/api.js': { callJsonApi: async (_url, payload) => {
            server.calls.push(plain(payload));
            if (payload.action === 'setup_patch') {
                const conflicts = [];
                for (const change of payload.changes) {
                    const now = getPath(server.config, change.path);
                    if (JSON.stringify(now) === JSON.stringify(change.base)) setPath(server.config, change.path, change.value);
                    else conflicts.push({ path: change.path, mine: change.value ?? null, theirs: now ?? null });
                }
                server.revision = 'r' + (++server.writes + 1);
                return answer({ conflicts });
            }
            if (payload.action === 'discover') return { ok: true, models: [{ id: 'own' }], voices: [], tuning: [], warnings: [], verification: { state: 'unverified', message: '' } };
            return answer();
        } },
        '/plugins/_model_config/webui/model-config-store.js': { store: host },
    }, { reactive: true });
    const { namespace } = await loadModule('webui/setup-store.js', { bindings, globals: { window: { confirm: () => true }, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, URL } });
    const store = namespace.store;
    await store.load();
    await store.conversationInit();
    await store.classifierInit();
    return { store, server, clock, host, compose: namespace.compose, patches: () => server.calls.filter(call => call.action === 'setup_patch') };
}

test('The factory names every method after its role, holds methods only, and two cards compose without a clash', async () => {
    const spec = { paths: {}, trimmed: [], address: [], rows: () => false, catalog: () => ({ providers: {} }), disabledReason: () => null,
                   unknown: () => ({ short: 'u', reason: 'u' }), compatible: () => ({ short: 'c', reason: 'c' }), context: { path: 'x', min: 1, message: '' } };
    const alpha = modelRoleSetup('alpha', spec), beta = modelRoleSetup('beta', spec);
    assert.ok(Object.keys(alpha).length > 30);
    assert.ok(Object.keys(alpha).every(name => name.startsWith('alpha') && typeof alpha[name] === 'function'));
    assert.deepEqual(Object.keys(beta).map(name => name.slice(4)), Object.keys(alpha).map(name => name.slice(5)));
    assert.equal(alpha.alphaProbe, undefined);                                                    // the probe and key copy only when the spec asks
    assert.equal(alpha.alphaCopyKey, undefined);
    const { compose } = await rig();
    assert.doesNotThrow(() => compose({ n: 1 }, alpha, beta));
    assert.throws(() => compose({ n: 1 }, alpha, alpha), /is defined twice/);
});

test('Each role has its own autosave timer: an edit on one card never cancels the other card’s pending save', async () => {
    const f = await rig();
    f.store.config.roles.conversation.model = 'omni'; f.store.conversationEdited('roles.conversation.model', true);
    await f.clock.advance(200);
    f.store.config.roles.classifier.model = 'd1-omni-600M'; f.store.classifierEdited('roles.classifier.model', true);
    await f.clock.advance(AUTOSAVE_MS - 200);
    assert.deepEqual(f.patches().map(call => call.changes.map(change => change.path)), [['roles.conversation.model']]);   // on time, despite the later edit
    await f.clock.advance(200);
    assert.deepEqual(f.patches().map(call => call.changes.map(change => change.path)), [['roles.conversation.model'], ['roles.classifier.model']]);
    assert.deepEqual([f.server.config.roles.conversation.model, f.server.config.roles.classifier.model, f.store.dirty], ['omni', 'd1-omni-600M', false]);
    assert.deepEqual([f.store.conversationStatusText(), f.store.classifierStatusText()], ['Saved', 'Saved']);
});

test('Agent Zero’s provider-change hook gets the role as a model slot: name is the model, ctx_length the context window, the provider read-only', async () => {
    const seen = [];
    const f = await rig({ host: {
        onProviderChanged(sectionKey, model, oldProvider) {
            seen.push([sectionKey, oldProvider, plain(model)]);
            Object.assign(model, { name: 'remembered/' + model.provider, api_base: '', ctx_length: 32768, provider: 'hijacked', kwargs: { temperature: 1 } });
        },
    } });
    f.store.config.roles.conversation.model = 'typed-before';
    assert.equal(f.store.conversationSetProvider('openrouter', true), 'openrouter');
    await settle();
    assert.deepEqual(seen, [['convo_conversation_model', '', { provider: 'openrouter', name: 'typed-before', api_base: '', ctx_length: 16384 }]]);
    const role = f.store.config.roles.conversation;
    assert.deepEqual([role.provider, role.model, f.store.config.compaction.context_tokens, 'kwargs' in role], ['openrouter', 'remembered/openrouter', 32768, false]);
    assert.deepEqual(Object.keys(plain(f.store.ui.conversation.edited)).sort(), ['compaction.context_tokens', 'roles.conversation.api_base', 'roles.conversation.model', 'roles.conversation.provider']);
    // The classifier card keeps its own slot and its own context window.
    f.store.classifierSetProvider('openai');
    await settle();
    assert.deepEqual([seen[1][0], f.store.config.roles.classifier.model, f.store.config.roles.classifier.context_window], ['convo_classifier_model', 'remembered/openai', 32768]);
});

test('The Provider Profiles hook name is used when the store has no onProviderChanged, with the previous slot; a throwing hook changes nothing', async () => {
    const seen = [];
    const f = await rig({ host: { async onProviderProfileChanged(sectionKey, model, previous, snapshot) { seen.push([sectionKey, previous, plain(snapshot)]); model.api_base = 'http://host.docker.internal:1234/v1'; } } });
    f.store.config.roles.conversation.api_base = '';
    f.store.conversationSetProvider('other');
    f.store.config.roles.conversation.api_base = 'http://host.docker.internal:8080/v1';
    f.store.conversationSetProvider('openai');
    await settle();
    assert.deepEqual(seen.at(-1), ['convo_conversation_model', 'other', { provider: 'other', name: '', api_base: 'http://host.docker.internal:8080/v1', ctx_length: 16384 }]);
    assert.equal(f.store.config.roles.conversation.api_base, 'http://host.docker.internal:1234/v1');
    const g = await rig({ host: { onProviderChanged() { throw new Error('plugin bug'); } } });
    assert.equal(g.store.conversationSetProvider('openai'), 'openai');
    await settle();
    assert.deepEqual([g.store.config.roles.conversation.provider, g.store.config.roles.conversation.model, g.store.error], ['openai', '', '']);
    const h = await rig();                                                                          // the v2.11 store has no hook: nothing is called
    assert.equal(await h.store.conversationProviderHook('', ''), false);
});

test('The conversation card offers an unknown OpenAI-compatible provider as "audio not verified", from Agent Zero’s address, with its key', async () => {
    const f = await rig();
    const option = f.store.conversationProviderOptions().find(item => item.id === 'router_x');
    assert.deepEqual([option.disabled, option.label, option.note], [false, 'Router X (audio not verified)', UNKNOWN_COMPATIBLE.reason]);
    assert.equal(f.store.conversationSetProvider('router_x'), 'router_x');
    const role = f.store.config.roles.conversation;
    assert.deepEqual([role.api_base, f.store.conversationUrl(), f.store.conversationProviderInfo().compatible], ['http://192.168.1.50:4000/v1', 'http://192.168.1.50:4000/v1', true]);
    assert.deepEqual([f.store.conversationHostKey(), f.store.conversationUsesHostKey(), f.store.conversationSummary().provider], ['router_x', true, 'Custom endpoint']);
    assert.deepEqual(plain(await f.store.conversationSearch('m')), ['own']);                         // on your LAN: the router's own list
    assert.deepEqual(plain(f.host.searches), []);
    f.host.chatProviderDetails = [];                                                                 // Agent Zero no longer describes it
    assert.equal(f.store.conversationProviderInfo().selectable, false);
    assert.equal(conversationUrl({ provider: 'router_x' }, 'http://h/v1'), 'http://h/v1');
    assert.equal(conversationUrl({ provider: 'router_x' }), '');
});

test('Providers, details and grouping are read from Agent Zero’s store when used, never copied', async () => {
    const f = await rig();
    assert.equal(f.store.conversationProviderOptions().some(option => option.id === 'late'), false);
    f.host.chatProviders = [...f.host.chatProviders, { value: 'late', label: 'Late plugin provider' }];
    assert.equal(f.store.conversationProviderOptions().find(option => option.id === 'late')?.label, 'Late plugin provider (unknown provider)');
    f.host.chatProviderDetails = [...f.host.chatProviderDetails, { value: 'late', litellm_provider: 'hosted_vllm', api_base: 'http://10.0.0.9:8000/v1' }];
    assert.equal(f.store.conversationProviderOptions().find(option => option.id === 'late')?.disabled, false);
    let calls = 0;
    f.host.groupResults = (models, query) => { calls++; return { matched: models.filter(model => model.endsWith(query)), rest: models.filter(model => !model.endsWith(query)) }; };
    assert.deepEqual(plain(f.store.conversationGroup(['a-1', 'b-2'], '2')), { matched: ['b-2'], rest: ['a-1'] });
    assert.equal(calls, 1);
});

test('The pure helpers: the address rule, Agent Zero’s detail shapes and what counts as OpenAI-compatible', () => {
    assert.equal(roleUrl({ provider: '', url: 'http://x/v1' }, null), 'http://x/v1');
    assert.equal(roleUrl({ provider: 'p', api_base: ' http://y/v1// ' }, { base: 'http://z/v1' }), 'http://y/v1');
    assert.equal(roleUrl({ provider: 'p' }, { base: 'http://z/v1' }), 'http://z/v1');
    assert.equal(roleUrl({ provider: 'p', api_base: 'http://y/v1' }, null), '');                 // a provider that cannot be used has no address
    assert.deepEqual(hostDetail([{ value: 'a', n: 1 }, { id: 'b', n: 2 }, { provider: 'c', n: 3 }], 'b'), { id: 'b', n: 2 });
    assert.deepEqual(hostDetail({ c: { n: 3 } }, 'c'), { n: 3 });
    for (const bad of [[null, 'a'], [{}, 'constructor'], [{ a: 'text' }, 'a'], [[{ value: 'a' }], '']]) assert.equal(hostDetail(...bad), null, JSON.stringify(bad));
    assert.equal(compatibleBase({ litellm_provider: 'openai', api_base: 'http://h/v1/' }), 'http://h/v1');
    assert.equal(compatibleBase({ litellm_provider: 'hosted_vllm', kwargs: { api_base: 'http://h:8000/v1' } }), 'http://h:8000/v1');
    for (const detail of [null, { litellm_provider: 'openai' }, { litellm_provider: 'openai', api_base: '  ' }, { litellm_provider: 'ollama', api_base: 'http://h' }, { api_base: 'http://h' }])
        assert.equal(compatibleBase(detail), '', JSON.stringify(detail));
});
