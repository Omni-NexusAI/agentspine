// The Classifier card (classifier-model-field.html, classifier-setup.js on the shared model-role factory) on the real settings
// store, run Alpine-like reactive (store state is a deep Proxy, as Alpine hands it over, so a structuredClone of it would throw
// here as in the browser). Agent Zero's modelConfig store and settings store are stand-ins with spies; the classifier catalog is
// the shape the server sends in setup_read (`classifier_catalog`). Nothing here proves the rendered page:
// tests/live-classifier-smoke.mjs checks that on the test host.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadModule, hostStubs } from './vm-loader.mjs';
import { readWebui } from './settings-markup.mjs';
import { defaults, getPath, setPath } from '../webui/defaults.js';
import { CLASSIFIER_KIND, DECIDER_DEFAULT_URL, GROUPS, CARD_PATHS } from '../webui/classifier-setup.js';

const plain = value => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const settle = async (rounds = 10) => { for (let i = 0; i < rounds; i++) await new Promise(resolve => setImmediate(resolve)); };
const LOCAL_NOTE = 'Check server confirms whether it answers the System One decision route';
// The catalog as the server delivers it (the brief's contract, helpers/convo_classifier_catalog.py).
const CATALOG = {
    providers: {
        llama_cpp: { label: 'llama.cpp', group: 'decision_local', kinds: ['decision', 'chat'], base: 'http://host.docker.internal:8080/v1', host_key: 'llama_cpp', verified: true, text_only: false, note: 'llama-server serves d1 models on /v1/systemone' },
        lm_studio: { label: 'LM Studio', group: 'decision_local', kinds: ['decision', 'chat'], base: 'http://host.docker.internal:1234/v1', host_key: 'lm_studio', verified: false, text_only: false, note: LOCAL_NOTE },
        unsloth_studio: { label: 'Unsloth Studio', group: 'decision_local', kinds: ['decision', 'chat'], base: '', host_key: '', verified: false, text_only: false, note: LOCAL_NOTE },
        ollama: { label: 'Ollama', group: 'decision_local', kinds: ['decision', 'chat'], base: 'http://host.docker.internal:11434/v1', host_key: 'ollama', verified: false, text_only: false, note: LOCAL_NOTE },
        vllm: { label: 'vLLM', group: 'decision_local', kinds: ['decision', 'chat'], base: 'http://host.docker.internal:8000/v1', host_key: 'vllm', verified: false, text_only: false, note: LOCAL_NOTE },
        omlx: { label: 'oMLX', group: 'decision_local', kinds: ['decision', 'chat'], base: 'http://host.docker.internal:8000/v1', host_key: 'omlx', verified: false, text_only: false, note: LOCAL_NOTE },
        jev: { label: 'TypeSafe Jev', group: 'decision_cloud', kinds: ['decision'], base: 'https://api.typesafe.ai', host_key: '', verified: false, text_only: true, note: 'Text only: unusable for an audio conversation.' },
        openai: { label: 'OpenAI', group: 'chat', kinds: ['chat'], base: 'https://api.openai.com/v1', host_key: 'openai', verified: false, text_only: false, note: '' },
        openrouter: { label: 'OpenRouter', group: 'chat', kinds: ['chat'], base: 'https://openrouter.ai/api/v1', host_key: 'openrouter', verified: false, text_only: false, note: '' },
        google: { label: 'Google', group: 'chat', kinds: ['chat'], base: 'https://generativelanguage.googleapis.com/v1beta/openai', host_key: 'google', verified: false, text_only: false, note: '' },
        other: { label: 'Other', group: 'other', kinds: ['decision', 'chat'], base: '', host_key: '', verified: false, text_only: false, note: '' },
    },
    disabled: { anthropic: 'Not reachable through the OpenAI-compatible audio route Convo uses.', groq: 'Convo cannot send audio input through this provider.',
                mistral: 'Takes audio in a different format from the one Convo sends.' },
    unknown_compatible: 'Agent Zero lists it as OpenAI-compatible: usable as a chat classifier, not verified until Check server.',
    unknown: 'Unknown provider: Convo cannot classify speech with it.',
};
const TEXT_ONLY = { state: 'text_only', label: 'Decides from text — not usable for an audio conversation', detail: 'System One’s decider reads text.' };
const SYSTEM_ONE = { installed: true, enabled: true, consumer_enabled: false, provider: 'openrouter', model: 'typesafe/jev-latest', host: 'api.typesafe.ai',
                     context_window: 32000, kind: 'openrouter', audio: TEXT_ONLY, registered: [], selected: '', ready: false, reasons: ['No decision backend is registered with Convo.'] };
// The classifier keys of the contract, for as long as defaults.js does not carry them yet (the server's mirror adds them).
const CLASSIFIER = { provider: '', key_source: 'provider', api_base: '', context_window: 8192, kind: 'auto' };
const SEPARATE = { 'decider.source': 'convo', 'decider.kind': CLASSIFIER_KIND, 'decider.url': 'http://host.docker.internal:9940', 'decider.model': 'd1-omni-600M',
                   'system_one.enabled': true, 'system_one.backend': CLASSIFIER_KIND };

function hostStore() {
    return {
        chatProviders: [{ value: 'anthropic', label: 'Anthropic' }, { value: 'google', label: 'Google' }, { value: 'groq', label: 'Groq' },
                        { value: 'llama_cpp', label: 'llama.cpp' }, { value: 'mystery', label: 'Mystery AI' }, { value: 'openai', label: 'OpenAI' },
                        { value: 'router_x', label: 'Router X' }],
        chatProviderDetails: [{ value: 'router_x', litellm_provider: 'openai', api_base: 'http://192.168.1.50:4000/v1/' }, { value: 'mystery', litellm_provider: 'cohere', api_base: 'https://mystery.example/v1' },
                              { value: 'groq', litellm_provider: 'openai', api_base: 'https://api.groq.com/openai/v1' }],   // OpenAI-compatible, yet Convo's reason wins
        apiKeyStatus: {}, apiKeyValues: {}, apiKeyDirty: {}, saved: [], searches: [], grouped: 0, revealed: 0,
        async ensureLoaded() {},
        async refreshApiKeyStatus() {},
        setApiKeyValue(provider, value) { this.apiKeyValues = { ...this.apiKeyValues, [provider]: value }; this.apiKeyDirty = { ...this.apiKeyDirty, [provider]: true }; },
        async saveApiKey(provider, value) { this.saved.push([provider, value]); this.apiKeyStatus = { ...this.apiKeyStatus, [provider]: true }; },
        async revealApiKey() { this.revealed++; return 'never-shown'; },
        async searchModels(...args) { this.searches.push(args); return ['qwen3-chat', 'd1-omni-600M', 'd1-omni-1B']; },
        groupResults(models, query) { this.grouped++; const q = String(query || '').toLowerCase(); return { matched: models.filter(m => q && m.toLowerCase().includes(q)), rest: models.filter(m => !q || !m.toLowerCase().includes(q)) }; },
    };
}

async function rig({ config = {}, extra = {}, respond = {}, host = hostStore() } = {}) {
    const calls = [], toasts = [], settings = { opened: [], sections: [], scrolled: [] };
    const server = plain(defaults);
    server.roles.classifier = { ...CLASSIFIER, ...server.roles.classifier };
    for (const [path, value] of Object.entries(config)) setPath(server, path, value);
    const read = () => ({ ok: true, config: plain(server), revision: 'r1', readiness: [], credentials: {}, decision_backends: [], decider_reference: SYSTEM_ONE,
                          classifier_catalog: CATALOG, ...extra });
    const bindings = hostStubs({
        '/js/api.js': { callJsonApi: async (_url, payload) => {
            calls.push(plain(payload));
            if (respond[payload.action]) return respond[payload.action](payload);
            if (payload.action === 'setup_save') return { ...read(), config: plain(payload.config), revision: 'r2' };
            if (payload.action === 'discover') return { ok: true, models: [{ id: 'own-server-model' }], voices: [], tuning: [], warnings: [], verification: { state: 'unverified', message: 'listed' } };
            return read();
        } },
        '/plugins/_model_config/webui/model-config-store.js': { store: host },
        '/components/settings/settings-store.js': { store: {
            activateSection(id, options) { settings.sections.push([id, plain(options)]); return true; },
            open(tab) { settings.opened.push(tab); return new Promise(() => {}); },           // resolves only when the modal closes
            scrollToSection(id) { settings.scrolled.push(id); },
        } },
    }, { reactive: true });
    const { namespace } = await loadModule('webui/setup-store.js', { bindings, globals: { window: { confirm: () => true }, justToast: (...args) => toasts.push(args), URL } });
    const store = namespace.store;
    await store.load();
    await store.classifierInit();
    return { store, calls, toasts, host, settings, role: () => store.config.roles.classifier, sent: action => calls.filter(call => call.action === action) };
}

test('The toggle is decider.source: on shows System One’s decider read-only with its verdict, off brings Convo’s own values back', async () => {
    const f = await rig({ config: { 'roles.classifier.provider': 'openai', 'roles.classifier.model': 'gpt-4o-mini-audio', 'roles.classifier.remote': true } });
    assert.deepEqual([f.store.classifierUsesSystemOne(), f.store.dirty], [false, false]);
    assert.equal(f.store.classifierSetSystemOne(true), true);
    assert.equal(f.store.config.decider.source, 'system_one');
    assert.deepEqual(plain(f.store.config.system_one), plain(defaults.system_one));                    // derived fields are the server's to write
    assert.deepEqual([f.role().provider, f.role().model, f.store.dirty], ['openai', 'gpt-4o-mini-audio', true]);   // Convo's values are kept
    assert.deepEqual(plain(f.store.classifierReference()), { provider: 'openrouter', model: 'typesafe/jev-latest', host: 'api.typesafe.ai', context: '32000' });
    assert.deepEqual(plain(f.store.classifierVerdict()), { state: 'text_only', ok: false, icon: 'text_fields', label: TEXT_ONLY.label, detail: TEXT_ONLY.detail });
    assert.equal(f.store.classifierSetSystemOne(false), false);
    assert.deepEqual([f.store.config.decider.source, f.store.dirty], ['', false]);                     // back to the source saved before: nothing to save
    const g = await rig({ config: { 'decider.source': 'system_one' } });
    assert.equal(g.store.classifierUsesSystemOne(), true);
    g.store.classifierSetSystemOne(false);
    assert.equal(g.store.config.decider.source, 'convo');                                            // off is written as 'convo' by the card
    // System One not installed: the toggle is offered only while it is the saved choice; an older server's missing verdict is never usable.
    const none = await rig({ extra: { decider_reference: null } });
    assert.deepEqual([none.store.classifierInheritAvailable(), none.store.classifierSetSystemOne(true), none.store.config.decider.source], [false, false, '']);
    assert.match(none.store.classifierInheritText(), /not installed/);
    for (const audio of [undefined, null, { state: 'constructor' }, { state: 'audio-ish' }]) {
        f.store.deciderReference = { ...SYSTEM_ONE, audio };
        assert.deepEqual([f.store.classifierVerdict().state, f.store.classifierVerdict().ok], ['unknown', false], JSON.stringify(audio));
    }
    f.store.deciderReference = { ...SYSTEM_ONE, audio: { state: 'audio', label: 'Listens to audio — Convo can use it', detail: '' } };
    assert.deepEqual([f.store.classifierVerdict().ok, f.store.classifierVerdict().icon], [true, 'hearing']);
    f.store.deciderReference = { ...SYSTEM_ONE, context_window: null, host: '' };
    assert.deepEqual([f.store.classifierReference().context, f.store.classifierReference().host], ['Not set', 'Hosted provider or not set']);
});

test('One provider select in four groups: local servers, cloud, Agent Zero’s other providers (with reasons), other', async () => {
    const f = await rig();
    const groups = f.store.classifierProviderGroups();
    assert.deepEqual(plain(groups.map(group => group.label)), GROUPS.map(([, label]) => label));
    const ids = Object.fromEntries(groups.map(group => [group.id, group.items.map(item => item.id)]));
    assert.deepEqual(plain(ids.local), ['llama_cpp', 'lm_studio', 'unsloth_studio', 'ollama', 'vllm', 'omlx']);
    assert.deepEqual(plain(ids.cloud), ['jev', 'openai', 'openrouter', 'google']);
    assert.deepEqual(plain(ids.other), ['', 'other']);                                               // the earlier endpoint while it is in effect
    const host = Object.fromEntries(groups.find(group => group.id === 'host').items.map(item => [item.id, plain(item)]));
    assert.deepEqual(Object.keys(host), ['anthropic', 'groq', 'mistral', 'mystery', 'router_x']);
    assert.deepEqual([host.anthropic.disabled, host.anthropic.label, host.anthropic.reason], [true, 'Anthropic (no compatible audio route)', CATALOG.disabled.anthropic]);
    assert.deepEqual([host.groq.disabled, host.groq.label, host.mistral.label], [true, 'Groq (no audio input)', 'mistral (different audio format)']);   // a reason the host does not list keeps its id
    assert.equal(f.store.classifierSetProvider('groq'), '');
    assert.deepEqual([host.mystery.disabled, host.mystery.label, host.mystery.reason], [true, 'Mystery AI (unknown provider)', CATALOG.unknown]);
    assert.deepEqual([host.router_x.disabled, host.router_x.label, host.router_x.note], [false, 'Router X (not verified)', CATALOG.unknown_compatible]);
    assert.equal(f.store.classifierCatalogMissing(), false);
    // A saved provider that can no longer be used stays visible, disabled; an older server without the catalog still lists Agent Zero's providers.
    const gone = await rig({ config: { 'roles.classifier.provider': 'vanished' } });
    const saved = gone.store.classifierProviderOptions().find(option => option.id === 'vanished');
    assert.deepEqual([saved.disabled, gone.store.classifierProviderInfo().selectable], [true, false]);
    assert.equal(gone.store.classifierProviderOptions().some(option => option.id === ''), false);
    const old = await rig({ extra: { classifier_catalog: undefined } });
    assert.equal(old.store.classifierCatalogMissing(), true);
    assert.deepEqual(plain(old.store.classifierProviderGroups().map(group => group.id)), ['host', 'other']);
    assert.equal(old.store.classifierSetProvider('llama_cpp'), '');                                  // nothing is invented without the server's catalog
});

test('A provider added to Agent Zero later appears at once; an OpenAI-compatible one is a chat classifier starting from Agent Zero’s address', async () => {
    const f = await rig();
    f.host.chatProviders = [...f.host.chatProviders, { value: 'late_router', label: 'Late Router' }];
    f.host.chatProviderDetails = { late_router: { litellm_provider: 'hosted_vllm', api_base: 'http://10.0.0.8:8000/v1' } };   // a table by id works too
    assert.ok(f.store.classifierProviderOptions().some(option => option.id === 'late_router' && !option.disabled));
    assert.equal(f.store.classifierSetProvider('mystery'), '');                                      // not OpenAI-compatible: refused
    assert.equal(f.store.classifierSetProvider('late_router'), 'late_router');
    assert.deepEqual([f.role().provider, f.role().api_base], ['late_router', 'http://10.0.0.8:8000/v1']);
    assert.deepEqual(plain(f.store.classifierKinds()), ['chat']);
    assert.deepEqual(plain(f.store.classifierKindOptions().map(option => [option.id, option.disabled])), [['auto', false], ['decision', true], ['chat', false]]);
    assert.equal(f.store.classifierEffectiveKind(), 'chat');
    // Agent Zero's key for that provider id, as the server routes it (convo_classifier_catalog.host_key); a server on your network
    // is asked for its own models by Convo's discovery (with that key), not by Agent Zero's search.
    assert.deepEqual([f.store.classifierProviderInfo().compatible, f.store.classifierHostKey(), f.store.classifierUsesHostKey(), f.store.classifierSearchesHost()], [true, 'late_router', true, false]);
    assert.equal(f.store.classifierRemoteShown(), false);                                            // a private network address: no consent needed
    f.host.chatProviders = [...f.host.chatProviders, { value: 'Bad-Id', label: 'Bad id' }];
    f.host.chatProviderDetails = { ...f.host.chatProviderDetails, 'Bad-Id': { litellm_provider: 'openai', api_base: 'http://10.0.0.9/v1' } };
    assert.equal(f.store.classifierProviderOptions().find(option => option.id === 'Bad-Id')?.disabled, true);   // an id the contract refuses
});

test('Model search: a cloud provider through Agent Zero’s store at the moment of use and its own grouping; a server on your network lists its own models', async () => {
    const f = await rig({ config: { 'roles.classifier.provider': 'openai', 'roles.classifier.remote': true } });
    assert.deepEqual(plain(await f.store.classifierSearch('d1')), ['qwen3-chat', 'd1-omni-600M', 'd1-omni-1B']);
    assert.deepEqual(plain(f.host.searches), [['openai', 'd1', 'chat', '']]);
    assert.deepEqual(plain(f.store.classifierGroup(['qwen3-chat', 'd1-omni-600M', 7], 'd1')), { matched: ['d1-omni-600M'], rest: ['qwen3-chat'] });
    assert.equal(f.host.grouped, 1);                                                                 // the host's own grouping, not a copy
    let patched = 0;
    f.host.groupResults = models => { patched++; return { matched: [], rest: [...models].reverse() }; };   // another plugin patches the store
    assert.deepEqual(plain(f.store.classifierGroup(['a', 'b'], 'x')), { matched: [], rest: ['b', 'a'] });
    assert.equal(patched, 1);
    f.store.classifierChooseModel('d1-omni-1B');
    assert.equal(f.role().model, 'd1-omni-1B');
    const g = await rig({ config: { 'roles.classifier.provider': 'other', 'roles.classifier.api_base': 'http://host.docker.internal:9931/v1' } });
    assert.deepEqual(plain(await g.store.classifierSearch('x')), ['own-server-model']);
    assert.deepEqual([g.host.searches.length, g.sent('discover').length, g.sent('discover')[0].role], [0, 1, 'classifier']);
    assert.deepEqual(plain(await g.store.classifierSearch('xy')), ['own-server-model']);
    assert.equal(g.sent('discover').length, 1);                                                      // typing reuses the list it fetched
    const h = await rig({ config: { 'roles.classifier.provider': 'llama_cpp', 'roles.classifier.api_base': 'http://100.75.98.97:18201/v1' } });
    assert.deepEqual(plain(await h.store.classifierSearch('d1')), ['own-server-model']);              // llama.cpp on your tailnet: its own list
    assert.deepEqual([h.host.searches.length, h.sent('discover').length], [0, 1]);
    g.store.classifierSetProvider('jev');
    await g.store.classifierSearch('');
    assert.deepEqual([g.host.searches.length, g.sent('discover').length], [0, 2]);                  // no Agent Zero key goes to TypeSafe through Convo
});

test('Keys: Agent Zero’s key store for a provider it holds keys for, Convo’s write-only key otherwise; neither reaches Convo’s settings', async () => {
    const f = await rig({ config: { 'roles.classifier.provider': 'openai', 'roles.classifier.remote': true } });
    assert.deepEqual([f.store.classifierUsesHostKey(), f.store.classifierHostKey()], [true, 'openai']);
    f.store.classifierHostKeyInput('sk-classifier-123');
    assert.equal(await f.store.classifierSaveHostKey(), true);
    assert.deepEqual(f.host.saved, [['openai', 'sk-classifier-123']]);
    assert.match(f.store.ui.classifier.hostKeyNote, /Saved as Agent Zero’s OpenAI key/);
    assert.equal(f.host.revealed, 0);
    f.role().api_base = 'https://proxy.example/v1';
    assert.match(f.store.classifierRouteWarning(), /OpenAI key will be sent to proxy\.example/);
    f.role().key_source = 'convo';
    assert.deepEqual([f.store.classifierUsesHostKey(), f.store.classifierUsesConvoKey()], [false, true]);
    f.store.setKey('classifier', 'own-key');
    assert.equal(await f.store.save(), true);
    const save = f.sent('setup_save').at(-1);
    assert.deepEqual(save.keys, { classifier: 'own-key' });
    assert.doesNotMatch(JSON.stringify(f.calls), /sk-classifier-123/);
    for (const provider of ['other', 'jev', 'unsloth_studio', '']) {
        f.role().provider = provider; f.role().key_source = 'provider';
        assert.equal(f.store.classifierUsesConvoKey(), true, provider);
    }
    assert.match(f.store.classifierConvoKeyHint(), /Leave blank to keep the saved key; it is saved with your changes\./);
});

test('Remote processing: this machine and private networks need no consent; a public address asks for it and Check refuses it before sending', async () => {
    const f = await rig({ config: { 'roles.classifier.provider': 'llama_cpp' } });
    assert.equal(f.store.classifierRemoteShown(), false);
    f.role().api_base = 'http://192.168.1.20:8080/v1';
    assert.deepEqual([f.store.classifierRemoteShown(), f.store.classifierRemoteText()], [false, '192.168.1.20 is on this machine or your network (LAN or Tailscale); consent is not needed for it.']);
    f.store.classifierSetProvider('openai');
    assert.deepEqual([f.store.classifierRemoteShown(), f.store.classifierRemoteText()], [true, 'Required: your speech is sent to api.openai.com.']);
    assert.match(f.store.classifierBaseProblem(), /needs remote-processing consent/);
    assert.equal(await f.store.classifierCheck(), false);
    assert.equal(f.sent('classifier_check').length, 0);
    assert.match(f.store.classifierCheckView().message, /^Refused: Sending your speech to api\.openai\.com needs remote-processing consent/);
    f.role().remote = true;
    assert.equal(f.store.classifierBaseProblem(), '');
    f.role().api_base = 'http://api.example.com/v1';
    assert.match(f.store.classifierBaseProblem(), /must use https/);
    f.role().api_base = '';
    await f.store.classifierCheck();                                                                 // the server checks with the SAVED consent
    assert.deepEqual([f.sent('classifier_check').length, f.store.classifierCheckView().message], [0, 'Save first: Check server uses the saved remote-processing consent.']);
    assert.equal(await f.store.save(), true);
    await f.store.classifierCheck();
    assert.deepEqual(f.sent('classifier_check').at(-1), { action: 'classifier_check', provider: 'openai', api_base: '', model: '', kind: 'auto' });
    f.role().remote = false;                                                                         // consent withdrawn in the draft: nothing is sent
    await f.store.classifierCheck();
    assert.equal(f.sent('classifier_check').length, 1);
    f.role().remote = false;
    f.store.classifierSetProvider('llama_cpp');
    assert.equal(f.store.classifierRemoteShown(), false);
});

test('A separate decider saved before this card shows as llama.cpp, a decision model at its address, and the draft changes only when edited', async () => {
    const f = await rig({ config: SEPARATE });
    assert.deepEqual(plain(f.store.classifierMigration()), { provider: 'llama_cpp', kind: 'decision', api_base: 'http://host.docker.internal:9940/v1', model: 'd1-omni-600M' });
    assert.deepEqual([f.store.classifierView().provider, f.store.classifierProviderInfo().label, f.store.classifierUrl(), f.store.classifierEffectiveKind()],
                     ['llama_cpp', 'llama.cpp', 'http://host.docker.internal:9940/v1', 'decision']);
    assert.deepEqual([f.role().provider, f.store.dirty], ['', false]);                               // a view only: nothing to save
    assert.equal(f.store.classifierProviderOptions().some(option => option.id === ''), false);       // the earlier endpoint is not offered over it
    assert.deepEqual(plain(f.store.classifierCheckRequest()), { provider: 'llama_cpp', api_base: 'http://host.docker.internal:9940/v1', model: 'd1-omni-600M', kind: 'decision' });
    for (const [overrides, base] of [[{ 'decider.url': '' }, DECIDER_DEFAULT_URL + '/v1'], [{ 'decider.source': '' }, 'http://host.docker.internal:9940/v1']]) {
        const g = await rig({ config: { ...SEPARATE, ...overrides } });
        assert.equal(g.store.classifierMigration()?.api_base, base, JSON.stringify(overrides));
    }
    for (const overrides of [{ 'system_one.enabled': false }, { 'system_one.backend': 'another' }, { 'roles.classifier.provider': 'openai' }, { 'decider.source': 'system_one' }]) {
        const g = await rig({ config: { ...SEPARATE, ...overrides } });
        assert.equal(g.store.classifierMigration(), null, JSON.stringify(overrides));
    }
    // An edit makes the view the draft, then applies; the derived fields stay the server's to write.
    f.store.classifierSet('model', 'd1-omni-1B');
    assert.deepEqual(plain({ provider: f.role().provider, kind: f.role().kind, api_base: f.role().api_base, model: f.role().model }),
                     { provider: 'llama_cpp', kind: 'decision', api_base: 'http://host.docker.internal:9940/v1', model: 'd1-omni-1B' });
    assert.deepEqual([f.store.classifierMigration(), f.store.dirty], [null, true]);
    f.store.classifierSetProvider('openai');
    f.store.classifierSetProvider('llama_cpp');
    assert.equal(f.role().api_base, 'http://host.docker.internal:9940/v1');                         // each provider's base is remembered
    await f.store.save();
    const sent = f.sent('setup_save').at(-1).config;
    assert.deepEqual([sent.decider.url, sent.decider.source, sent.system_one.enabled], ['http://host.docker.internal:9940', 'convo', true]);
    // Choosing another provider from the view adopts it first, so the saved decider's address is remembered for llama.cpp.
    const h = await rig({ config: SEPARATE });
    assert.equal(h.store.classifierSetProvider('openrouter'), 'openrouter');
    assert.deepEqual([h.role().kind, h.role().model], ['auto', 'd1-omni-600M']);                    // decision is not served by OpenRouter
    h.store.classifierSetProvider('llama_cpp');
    assert.equal(h.role().api_base, 'http://host.docker.internal:9940/v1');
    // Toggling System One on and off over it shows the saved decider again.
    const t = await rig({ config: SEPARATE });
    t.store.classifierSetSystemOne(true); t.store.classifierSetSystemOne(false);
    assert.deepEqual([t.store.config.decider.source, t.store.classifierView().provider, t.store.dirty], ['convo', 'llama_cpp', false]);
});

test('The earlier HTTP classifier stays as entered: the endpoint option, its URL, a chat route; Other carries its address over', async () => {
    const f = await rig({ config: { 'roles.classifier.url': 'http://host.docker.internal:8085/v1', 'roles.classifier.model': 'qwen3-chat' } });
    assert.deepEqual([f.store.classifierView().provider, f.store.classifierUrl(), f.store.classifierEffectiveKind(), f.store.classifierMigration()],
                     ['', 'http://host.docker.internal:8085/v1', 'chat', null]);
    // Checked as what it is (the server's Check needs a provider): an OpenAI-compatible chat server at that address.
    assert.deepEqual(plain(f.store.classifierCheckRequest()), { provider: 'other', api_base: 'http://host.docker.internal:8085/v1', model: 'qwen3-chat', kind: 'chat' });
    f.store.classifierSetProvider('other');
    assert.equal(f.role().api_base, 'http://host.docker.internal:8085/v1');
    f.role().api_base = '';
    assert.match(f.store.classifierBaseProblem(), /^Enter the API base URL: Other has no default address/);
    assert.equal(await f.store.classifierCheck(), false);
    assert.equal(f.sent('classifier_check').length, 0);
});

test('Check server: what is shown is sent, answered inline (never a toast); a stale answer is dropped and the other kind can be adopted', async () => {
    const answers = [];
    const f = await rig({ config: { 'roles.classifier.provider': 'llama_cpp' }, respond: { classifier_check: payload => answers.shift()(payload) } });
    assert.match(f.store.classifierCheckView().label, /^Not checked/);
    answers.push(() => ({ ok: true, state: 'decision', host: 'host.docker.internal:8080', models: ['d1-omni-600M'], message: 'host.docker.internal:8080 answered a decision.' }));
    assert.equal(await f.store.classifierCheck(), true);
    assert.deepEqual(f.sent('classifier_check').at(-1), { action: 'classifier_check', provider: 'llama_cpp', api_base: '', model: '', kind: 'auto' });
    assert.deepEqual(plain(f.store.classifierCheckView()), { state: 'decision', icon: 'check_circle', label: 'Verified: decision model: ', message: 'host.docker.internal:8080 answered a decision.' });
    assert.equal(f.store.classifierCheckSuggests(), '');                                             // Auto is a decision model here already
    answers.push(() => ({ ok: true, state: 'chat', host: 'h', models: [], message: 'Only chat answers.' }));
    await f.store.classifierCheck();
    assert.equal(f.store.classifierCheckSuggests(), 'chat');
    assert.equal(f.store.classifierUseCheckedKind(), 'chat');
    assert.deepEqual([f.role().kind, f.store.classifierCheckSuggests()], ['chat', '']);
    answers.push(() => ({ ok: true, state: 'surprising', message: 'odd' }));
    await f.store.classifierCheck();
    assert.equal(f.store.classifierCheckView().state, 'unverified');                                 // an unknown state is never shown as verified
    answers.push(() => ({ ok: false, code: 'remote', error: 'Refused: a public address needs consent.' }));
    assert.equal(await f.store.classifierCheck(), false);
    assert.deepEqual([f.store.classifierCheckView().state, f.store.classifierCheckView().message], ['refused', 'Refused: a public address needs consent.']);
    assert.deepEqual([f.toasts.length, f.store.error], [0, '']);
    let release;
    answers.push(() => new Promise(resolve => { release = () => resolve({ ok: true, state: 'decision', message: 'old answer' }); }));
    const pending = f.store.classifierCheck();
    assert.deepEqual([f.store.classifierChecking(), f.store.classifierCheckView().state], [true, 'busy']);
    await f.store.classifierCheck();                                                                 // a second press while checking sends nothing
    assert.equal(f.sent('classifier_check').length, 5);
    f.store.classifierSet('model', 'edited-meanwhile');
    release(); await pending;
    assert.deepEqual([f.store.classifierChecking(), f.store.ui.classifier.check], [false, null]);
    answers.push(() => ({ ok: true, state: 'decision', message: 'fine' }));
    await f.store.classifierCheck();
    f.store.classifierSetSystemOne(true);                                                            // switching the source clears an answer
    assert.equal(f.store.ui.classifier.check, null);
});

test('Classifier type: Auto is the provider’s first kind, a kind it does not serve is refused, and a switch resets it', async () => {
    const f = await rig({ config: { 'roles.classifier.provider': 'llama_cpp' } });
    assert.deepEqual(plain(f.store.classifierKindOptions().map(option => option.label)), ['Auto (decision model)', 'Decision model (System One protocol)', 'Chat model (JSON verdict)']);
    assert.equal(f.store.classifierSetKind('decision'), 'decision');
    f.store.classifierSetProvider('openai');
    assert.equal(f.role().kind, 'auto');
    assert.equal(f.store.classifierSetKind('decision'), 'auto');                                     // OpenAI does not serve the decision route
    assert.match(f.store.classifierKindOptions()[1].label, /not served by OpenAI/);
    assert.equal(f.store.classifierSetKind('chat'), 'chat');
    f.store.classifierSetProvider('google');
    assert.equal(f.role().kind, 'chat');                                                             // still served: kept
    f.store.classifierSetProvider('jev');
    assert.deepEqual([f.role().kind, f.store.classifierEffectiveKind()], ['auto', 'decision']);
    assert.equal(f.store.classifierSetKind('bogus'), 'auto');
});

test('The context window is the classifier’s own, 1024 to 1000000 tokens, and readiness rows are listed with words', async () => {
    const readiness = [{ id: 'plugin', label: 'Plugin enabled', state: 'ready' }, { id: 'decider_source', label: 'Classifier', state: 'unverified', detail: 'Separate.' },
                       { id: 'classifier_provider', label: 'Classifier provider', state: 'missing', detail: 'x' }];
    const f = await rig({ extra: { readiness } });
    assert.equal(f.store.classifierContextProblem(), '');
    for (const value of [512, 1000001, 8192.5, '', null]) {
        f.role().context_window = value;
        assert.match(f.store.classifierContextProblem(), /from 1024 to 1000000/, String(value));
        assert.match(f.store.classifierHeld().reason, /from 1024 to 1000000/, String(value));
    }
    f.role().context_window = 1024;
    assert.equal(f.store.classifierContextProblem(), '');
    assert.deepEqual(plain(f.store.classifierRows().map(row => row.id)), ['decider_source', 'classifier_provider']);
    assert.deepEqual(['unverified', 'missing', 'ready', 'odd', 'constructor'].map(state => f.store.classifierStateText(state)), ['Not checked', 'Needs attention', 'Ready', 'odd', 'constructor']);
    assert.ok(Object.keys(CARD_PATHS).every(path => path.startsWith('roles.classifier.') || path === 'decider.source'));
    assert.equal(f.store.label('classifier'), 'Classifier');
});

test('The link opens Agent settings → Models without waiting for that modal to close', async () => {
    const f = await rig({ config: { 'decider.source': 'system_one' } });
    const result = await Promise.race([f.store.classifierOpenSystemOne(), new Promise(resolve => setTimeout(() => resolve('waited'), 200))]);
    assert.equal(result, true);
    assert.deepEqual(f.settings.opened, ['agent']);
    assert.deepEqual(f.settings.sections, [['section-models-summary', { persist: false }]]);
    await new Promise(resolve => setTimeout(resolve, 450));
    assert.deepEqual(f.settings.scrolled, ['section-models-summary']);
});

// ---- The card's markup
const card = () => readWebui('classifier-model-field.html');
const section = (html, from, to) => html.slice(html.indexOf(from), to ? html.indexOf(to, html.indexOf(from)) : undefined);

test('The card renders on its own, with the Conversation card’s structure and classes and the agreed title and note', async () => {
    const html = await card();
    assert.match(html, /^<html><head><script type="module">import \{ store \} from '\/plugins\/_convo\/webui\/setup-store\.js';<\/script><\/head><body>/);
    assert.match(html, /<fieldset class="convo-card convo-model-card convo-classifier-card" x-data="\{ role: 'classifier' \}" x-init="\$store\.convoSetup\.classifierInit\(\)">/);
    assert.match(html, /<legend x-text="\$store\.convoSetup\.label\(role\)"><\/legend>/);
    assert.match(html, /<span>Decides whether speech is meant for Convo and whether to answer\. A decision model \(System One style\) or a chat model that accepts audio\.<\/span>/);
    for (const name of ['field', 'field-label', 'field-title', 'field-description', 'field-control', 'toggle', 'toggler', 'model-search-btn', 'model-search-results', 'model-search-item', 'advanced-section', 'advanced-toggle', 'advanced-body', 'masked-text-input', 'convo-model-note', 'convo-model-fields'])
        assert.match(html, new RegExp(`class="[^"]*\\b${name}\\b`), name);
    const own = section(html, 'data-convo-classifier="own"', 'data-convo-classifier="system-one"');
    const titles = [...own.matchAll(/class="field-title"[^>]*>([^<]+)</g)].map(match => match[1]);
    assert.deepEqual(titles, ['Provider', 'API base URL', 'Server URL', 'Model name', 'API key', 'API key', 'Context window (tokens)', 'Accepts audio input', 'Allow remote processing',
                              'API base URL', 'Key source', 'Classifier type', 'Credential environment reference']);   // Agent Zero's order; then Advanced
    assert.ok(own.indexOf('class="advanced-section') > own.indexOf('Allow remote processing'));
    assert.ok(html.indexOf('Use System One’s decider</div>') > html.indexOf('data-convo-classifier="system-one"'));   // the toggle is underneath
    assert.equal((own.match(/<select\b/g) || []).length, 3);                                        // provider, key source, type: one provider list
    assert.match(own, /<template x-for="group in \$store\.convoSetup\.classifierProviderGroups\(\)" :key="group\.id"><optgroup :label="group\.label">/);
});

test('Read-only System One fields, the toggle binding and the migrated fields; nothing writes a title or reveals a key', async () => {
    const html = await card();
    const locked = section(html, 'data-convo-classifier="system-one"', 'convo-classifier-inherit');
    assert.deepEqual([...locked.matchAll(/class="field-title"[^>]*>([^<]+)</g)].map(match => match[1]), ['Provider', 'Model name', 'Endpoint host', 'Context window (tokens)']);
    assert.equal((locked.match(/<input type="text" readonly aria-readonly="true"/g) || []).length, 4);
    assert.doesNotMatch(locked, /x-model|<select|type="password"|setKey|token_env|api_key/);
    assert.match(locked, /<x-icon :name="\$store\.convoSetup\.classifierVerdict\(\)\.icon" aria-hidden="true"><\/x-icon><strong x-text="\$store\.convoSetup\.classifierVerdict\(\)\.label"><\/strong>/);
    assert.match(locked, /@click="\$store\.convoSetup\.classifierOpenSystemOne\(\)">Change them in Agent settings → Models<\/button>/);
    assert.match(html, /:disabled="!\$store\.convoSetup\.classifierInheritAvailable\(\)" :checked="\$store\.convoSetup\.config\.decider\.source === 'system_one'"\s+@change="\$event\.target\.checked = \$store\.convoSetup\.classifierSetSystemOne\(\$event\.target\.checked\)"/);
    for (const field of ['provider', 'api_base', 'model', 'kind'])
        assert.match(html, new RegExp(`:value="\\$store\\.convoSetup\\.classifierMigration\\(\\)\\?\\.${field} \\?\\? \\$store\\.convoSetup\\.config\\.roles\\.classifier\\.${field}"`), field);
    assert.doesNotMatch(html, /x-model[^=]*="\$store\.convoSetup\.config\.roles\.classifier\.(provider|api_base|model|kind)"/);   // a migrated view is never written by showing it
    const tag = /<(input|select)\b(?:[^>"']|"[^"]*"|'[^']*')*>/g;
    for (const [control] of html.matchAll(tag)) assert.match(control, /:aria-labelledby="\$id\('convo-classifier-[\w-]+', 'title'\)"/, control.slice(0, 90));
    assert.doesNotMatch(html, /\stitle=|:title=|x-bind:title|x-html|revealApiKey|api_key|_convo_key|localStorage|sessionStorage/);
    assert.doesNotMatch(html, /Turn-taking|decider-field/);
    assert.match(html, /:disabled="\$store\.convoSetup\?\.ui\?\.classifier\?\.checking === true" @click="\$store\.convoSetup\.classifierCheck\(\)">Check server<\/button>/);
    assert.match(html, /@keydown\.escape="if \(open\) \{ \$event\.stopPropagation\(\); close\(\); \}"/);   // Escape closes the list, not the modal
    assert.doesNotMatch(html, /#[0-9a-f]{3,6}\b|rgb\(/i);                                            // host tokens only
});

// Alpine evaluates every binding once when the card renders and never re-runs one that threw (found live), so each expression the
// card binds is evaluated here against the real store in every state the card can be in. Loop and component-local expressions are
// left to the live check.
test('Every card binding evaluates without throwing, whichever state the card is in', async () => {
    const html = await card();
    const LOCAL = /\b(?:option|group|row|reason|item|open|index|busy|note|items|advOpen|autosave|id|i)\b(?!\()|\$event|\$el|\$refs/;
    const expressions = [...html.matchAll(/\s(?:x-text|x-show|x-if|:value|:checked|:disabled|:aria-invalid|:placeholder|:data-state|:data-ok|:name|:label)="([^"]*)"/g)]
        .map(match => match[1].replace(/&amp;/g, '&')).filter(expression => !LOCAL.test(expression.replace(/\$store\.convoSetup\??\.[\w?.]+/g, '')));
    assert.ok(expressions.length > 40, String(expressions.length));
    for (const name of new Set([...html.matchAll(/\$store\.convoSetup\??\.(\w+)/g)].map(match => match[1])))
        assert.ok(name in (await rig()).store, name + ' is not on the store');
    const states = [
        ['the earlier endpoint', {}, {}], ['a local decision server', { 'roles.classifier.provider': 'llama_cpp' }, {}], ['a saved separate decider', SEPARATE, {}],
        ['System One’s decider', { 'decider.source': 'system_one' }, {}], ['an older server', { 'decider.source': 'system_one' }, { decider_reference: null, classifier_catalog: undefined }],
        ['a provider that is gone', { 'roles.classifier.provider': 'vanished' }, { classifier_catalog: undefined }],
        ['Other without a base', { 'roles.classifier.provider': 'other', 'roles.classifier.context_window': 'x' }, {}],
    ];
    for (const [label, config, extra] of states) {
        const f = await rig({ config, extra });
        f.store.ui.classifier.check = { state: 'decision', message: 'm' };
        for (const expression of expressions)
            assert.doesNotThrow(() => new Function('$store', 'role', '$id', `return (${expression});`)({ convoSetup: f.store }, 'classifier', (...parts) => parts.join('-')), `${label}: ${expression}`);
    }
});

test('The shell composes the Classifier card after the Conversation card, whose styles it shares', async () => {
    const shell = await readWebui('settings-controls.html');
    const conversation = shell.indexOf('conversation-model-field.html'), classifier = shell.indexOf('classifier-model-field.html');
    assert.ok(conversation > 0 && classifier > conversation);
    assert.doesNotMatch(shell, /decider-field\.html/);
    assert.match(await readWebui('conversation-model-field.html'), /\.convo-model-card \.field\{margin-block:\.6rem\}/);
});
