import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { loadModule, hostStubs } from './vm-loader.mjs';
import { readWebui, settingsMarkup } from './settings-markup.mjs';
import { TTS_BACKENDS, TTS_STASH_DEFAULTS, controlsFor, defaults, switchBackend } from '../webui/defaults.js';
import { SPEECH_PROVIDERS, SPEECH_GROUPS, SPEECH_CONTROLS, adoptProvider, backendFor, connectionFields, effectiveProvider, instructionsSupported,
         isLocalUrl, legacyAudiocppOffer, normalizeTts, switchProvider } from '../webui/speech-catalog.js';

const plain = value => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));   // values built inside the VM compare by content, not by realm
const CASES = JSON.parse(await readFile(new URL('./fixtures/speech-normalize-cases.json', import.meta.url), 'utf8'));
const MISSING = Symbol('missing');
const at = (value, path) => path.split('.').reduce((node, key) => (node !== MISSING && node && typeof node === 'object' && key in node ? node[key] : MISSING), value);
// roles.tts as the browser holds it after loading: the defaults, then what was saved
const withDefaults = (input = {}) => ({ ...plain(defaults.roles.tts), ...plain(input), capabilities: { ...plain(defaults.roles.tts.capabilities), ...plain(input.capabilities || {}) } });

test('The browser derives exactly the fields the server derives (shared normalization cases)', () => {
    for (const item of CASES.cases) {
        const input = withDefaults(item.input), frozen = plain(input);
        const out = normalizeTts(input);
        for (const [path, expected] of Object.entries(item.expect)) {
            const value = at(out, path);
            assert.deepEqual(value === MISSING ? null : plain(value), expected, `${item.name}: ${path}`);
            if (expected === null) assert.equal(value, MISSING, `${item.name}: ${path} is absent, not null`);
        }
        assert.deepEqual(plain(normalizeTts(out)), plain(out), `${item.name}: normalizing twice changes nothing`);
        assert.deepEqual(plain(input), frozen, `${item.name}: the input is untouched`);
    }
});

test('The catalog: providers grouped like Agent Zero’s, each resolving to a runtime backend; Strata is not one', () => {
    assert.deepEqual(Object.keys(SPEECH_PROVIDERS), ['kokoro_native', 'audiocpp', 'llamacpp', 'unsloth_studio', 'openai_tts', 'openai_compatible']);
    assert.deepEqual(SPEECH_GROUPS.map(group => group.label), ['Agent Zero built-in', 'Local servers', 'Cloud', 'Other']);
    for (const [name, spec] of Object.entries(SPEECH_PROVIDERS)) {
        assert.ok(SPEECH_GROUPS.some(group => group.id === spec.group), name);
        for (const connection of spec.connections.length ? spec.connections : ['']) assert.ok(backendFor(name, connection) in TTS_BACKENDS, name);
    }
    assert.equal('strata' in SPEECH_PROVIDERS, false);
    assert.deepEqual([backendFor('audiocpp', 'external'), backendFor('audiocpp', 'managed'), backendFor('kokoro_native'), backendFor('nope')], ['openai', 'managed_audio', 'host_kokoro', null]);
    assert.deepEqual(plain(effectiveProvider({ backend: 'openai', url: 'http://127.0.0.1:8890/v1' })), ['openai_compatible', '']);   // derived, never guessed from the URL
    assert.deepEqual(plain(connectionFields('audiocpp', 'managed')), ['model', 'voice', 'tuning_profile', 'instructions', 'native_pcm']);
    assert.equal(isLocalUrl('http://host.docker.internal:8890/v1'), true);
    assert.equal(isLocalUrl('http://192.168.1.20:8080/v1'), true);
    assert.equal(isLocalUrl('http://[::1]:8080/v1'), true);
    assert.equal(isLocalUrl('http://100.101.102.103:8880/v1'), true);                         // Tailscale
    assert.equal(isLocalUrl('http://my-desktop.tail1234.ts.net:8880/v1'), true);
    assert.equal(isLocalUrl('http://desktop:8880/v1'), true);                                  // a LAN machine name
    assert.equal(isLocalUrl('https://api.openai.com/v1'), false);
    assert.equal(isLocalUrl('http://169.254.1.1/v1'), false);                                // link-local is not "this network" for the server either
    assert.equal(isLocalUrl('not a url'), false);
});

const AUDIOCPP = { provider: 'audiocpp', connection: 'external', backend: 'openai', url: 'http://127.0.0.1:8890/v1', model: 'qwen3-tts-1.7b-base-bf16',
                   voice: 'clone:2098c329bc9d', capabilities: { audio_output: true, cloning: true, native_pcm: true } };

test('Switching providers stashes each provider’s own fields, per audio.cpp connection, and nothing leaks between them', () => {
    const start = normalizeTts(withDefaults({ ...AUDIOCPP, saved: { openai: { voice: 'alloy' } } }));
    const frozen = plain(start);
    const openai = switchProvider(start, 'openai_tts');
    assert.deepEqual(plain(start), frozen);                                                     // pure
    assert.deepEqual([openai.provider, openai.connection, openai.backend, openai.url, openai.model, openai.voice, openai.remote],
                     ['openai_tts', '', 'openai', 'https://api.openai.com/v1', 'gpt-4o-mini-tts', '', false]);   // its own defaults, consent not given for you
    assert.equal('native_pcm' in openai.capabilities, false);                                   // OpenAI has no PCM stream
    assert.deepEqual(plain(openai.saved), { openai: { voice: 'alloy' } });                       // the older per-backend stash is left alone
    assert.deepEqual(plain(openai.provider_saved.audiocpp), { connection: 'external', key_source: 'provider', external: {
        url: 'http://127.0.0.1:8890/v1', model: 'qwen3-tts-1.7b-base-bf16', voice: 'clone:2098c329bc9d', token_env: '', remote: false, instructions: '',
        accepts_instructions: false, native_pcm: true } });

    const spoken = normalizeTts({ ...openai, voice: 'coral', remote: true, instructions: 'warm' });
    assert.equal(spoken.capabilities.instruction_control, true);
    const back = switchProvider(spoken, 'audiocpp');                                            // back to the connection it was left on
    assert.deepEqual([back.connection, back.backend, back.url, back.model, back.voice, back.capabilities.native_pcm, back.instructions],
                     ['external', 'openai', 'http://127.0.0.1:8890/v1', 'qwen3-tts-1.7b-base-bf16', 'clone:2098c329bc9d', true, '']);
    assert.equal('instruction_control' in back.capabilities, false);
    assert.deepEqual(plain(back.provider_saved.openai_tts), { url: 'https://api.openai.com/v1', model: 'gpt-4o-mini-tts', voice: 'coral', token_env: '', remote: true, instructions: 'warm', key_source: 'provider' });

    const managed = switchProvider(back, 'audiocpp', 'managed');
    assert.deepEqual([managed.connection, managed.backend, managed.model, managed.voice, managed.tuning_profile], ['managed', 'managed_audio', '', '', '']);   // a managed Studio voice is not your container's
    assert.equal(managed.url, 'http://127.0.0.1:8890/v1');                                      // not its field: kept, hidden
    const studio = { ...managed, model: 'base-f16', voice: 'clone:one', tuning_profile: 'quality' };
    const external = switchProvider(studio, 'audiocpp', 'external');
    assert.deepEqual([external.model, external.voice, external.capabilities.native_pcm], ['qwen3-tts-1.7b-base-bf16', 'clone:2098c329bc9d', true]);
    assert.deepEqual(plain(external.provider_saved.audiocpp.managed), { model: 'base-f16', voice: 'clone:one', tuning_profile: 'quality', instructions: '', native_pcm: false });
    const again = switchProvider(switchProvider(external, 'openai_tts'), 'audiocpp', 'managed');
    assert.deepEqual([again.model, again.voice, again.tuning_profile], ['base-f16', 'clone:one', 'quality']);

    const reopened = switchProvider(external, 'openai_tts');
    assert.deepEqual([reopened.voice, reopened.instructions, reopened.remote, reopened.capabilities.instruction_control], ['coral', 'warm', true, true]);
    const kokoro = switchProvider(reopened, 'kokoro_native');
    assert.deepEqual([kokoro.backend, kokoro.voice, kokoro.model], ['host_kokoro', 'coral', 'gpt-4o-mini-tts']);   // Kokoro owns nothing: hidden values stay
    assert.equal('instruction_control' in kokoro.capabilities, false);
    const llama = switchProvider(kokoro, 'llamacpp');
    assert.deepEqual([llama.url, llama.model, llama.voice, llama.instructions, llama.accepts_instructions], ['http://host.docker.internal:8080/v1', '', '', '', false]);
    const unsloth = switchProvider(llama, 'unsloth_studio');
    assert.equal(unsloth.url, '');                                                               // no documented default address
    assert.equal(switchProvider(unsloth, 'strata'), unsloth);                                    // an unknown id changes nothing
    const home = switchProvider(unsloth, 'audiocpp');
    assert.deepEqual([home.connection, home.voice], ['external', 'clone:2098c329bc9d']);
});

test('A provider that owns no address never keeps a remote one it cannot use (it would make the settings unsavable or claim remote processing)', () => {
    const openai = switchProvider(normalizeTts(withDefaults(AUDIOCPP)), 'openai_tts');                // consent not given yet
    for (const [next, connection] of [['kokoro_native', ''], ['audiocpp', 'managed']]) {
        const away = switchProvider(openai, next, connection);
        assert.deepEqual([away.url, away.remote], [TTS_STASH_DEFAULTS.openai.url, false], next);
        assert.deepEqual([away.provider_saved.openai_tts.url, away.provider_saved.openai_tts.remote], ['https://api.openai.com/v1', false], next);   // kept where it belongs
        const back = switchProvider(away, 'openai_tts');
        assert.deepEqual([back.url, back.remote], ['https://api.openai.com/v1', false], next);
    }
    const local = switchProvider(normalizeTts(withDefaults(AUDIOCPP)), 'kokoro_native');
    assert.equal(local.url, 'http://127.0.0.1:8890/v1');                                        // a local address does no harm and stays
});

test('An older configuration: choosing what it already means only makes it explicit, and its older backend stash is read, never written', () => {
    const legacy = withDefaults({ backend: 'managed_audio', model: 'base-f16', voice: 'clone:one', saved: { openai: { url: 'http://127.0.0.1:8881/v1', model: 'tts-model', voice: 'clone:old' } } });
    assert.deepEqual(plain(effectiveProvider(legacy)), ['audiocpp', 'managed']);
    const explicit = switchProvider(legacy, 'audiocpp', 'managed');
    assert.deepEqual([explicit.provider, explicit.connection, explicit.backend, explicit.model, explicit.voice], ['audiocpp', 'managed', 'managed_audio', 'base-f16', 'clone:one']);
    const compatible = switchProvider(legacy, 'openai_compatible');
    assert.deepEqual([compatible.url, compatible.model, compatible.voice, compatible.backend], ['http://127.0.0.1:8881/v1', 'tts-model', 'clone:old', 'openai']);
    assert.deepEqual(plain(compatible.saved), plain(legacy.saved));
    assert.deepEqual(plain(compatible.provider_saved.audiocpp.managed), { model: 'base-f16', voice: 'clone:one', tuning_profile: '', instructions: '', native_pcm: false });
});

test('Your known audio.cpp port is only offered: nothing changes until you confirm, and confirming keeps the address, model and voice', () => {
    const legacy = withDefaults({ backend: 'openai', url: 'http://127.0.0.1:8890/v1', model: 'qwen3-tts-1.7b-base-bf16', voice: 'clone:2098c329bc9d', capabilities: { native_pcm: true } });
    assert.equal(legacyAudiocppOffer(legacy), 'http://127.0.0.1:8890/v1');
    for (const other of [{ ...legacy, url: 'http://127.0.0.1:8881/v1' }, { ...legacy, url: 'http://192.168.1.20:8890/v1' }, { ...legacy, backend: 'managed_audio' },
                         { ...legacy, provider: 'openai_compatible' }]) assert.equal(legacyAudiocppOffer(other), '');
    const adopted = adoptProvider(legacy, 'audiocpp', 'external');
    assert.deepEqual([adopted.provider, adopted.connection, adopted.backend, adopted.url, adopted.model, adopted.voice, adopted.capabilities.native_pcm],
                     ['audiocpp', 'external', 'openai', 'http://127.0.0.1:8890/v1', 'qwen3-tts-1.7b-base-bf16', 'clone:2098c329bc9d', true]);
    assert.equal(adopted.provider_saved, undefined);
    assert.equal(legacyAudiocppOffer(adopted), '');
});

test('Delivery instructions are offered only where the provider and model honour them', () => {
    const matrix = [
        [{ provider: 'openai_tts', model: 'gpt-4o-mini-tts', voice: 'coral' }, true],
        [{ provider: 'openai_tts', model: 'tts-1', voice: 'alloy' }, false],
        [{ provider: 'openai_tts', model: 'tts-1-hd', voice: 'alloy', accepts_instructions: true }, false],
        [{ ...AUDIOCPP, model: 'qwen3-tts-1.7b-customvoice', voice: 'vivian', accepts_instructions: true }, true],
        [{ ...AUDIOCPP, model: 'qwen3-tts-1.7b-customvoice', voice: 'vivian' }, false],
        [{ ...AUDIOCPP, accepts_instructions: true }, false],                                      // a Base clone model and voice
        [{ ...AUDIOCPP, connection: 'managed', model: 'x', voice: 'vivian', accepts_instructions: true }, false],
        [{ provider: 'kokoro_native', accepts_instructions: true }, false],
        [{ provider: 'llamacpp', model: 'orpheus', voice: 'tara', accepts_instructions: true }, true],
        [{ provider: 'unsloth_studio', model: 'orpheus', voice: 'tara' }, false],
        [{ provider: 'openai_compatible', model: 'm', voice: 'clone:x', accepts_instructions: true }, false],
        [{ provider: 'openai_compatible', model: 'm', voice: 'v', accepts_instructions: true }, true],
        [{ backend: 'openai', model: 'm', voice: 'v', capabilities: { instruction_control: true } }, true],   // an older config's own flag
    ];
    for (const [fields, expected] of matrix) {
        const tts = withDefaults(fields);
        assert.equal(instructionsSupported(tts), expected, JSON.stringify(fields));
    }
});

test('Legacy backend switching still works on its own terms (kept for older callers)', () => {
    const openai = { backend: 'openai', url: 'http://api/v1', model: 'tts-1', voice: 'alloy', token_env: 'MY_KEY', remote: true, instructions: 'warm', tuning_profile: '', capabilities: { audio_output: true } };
    const managed = switchBackend(openai, 'managed_audio');
    assert.deepEqual(plain(managed.saved.openai), { url: 'http://api/v1', model: 'tts-1', voice: 'alloy', token_env: 'MY_KEY', remote: true, instructions: 'warm' });
    assert.deepEqual([switchBackend({ ...managed, voice: 'clone:abc' }, 'openai').voice], ['alloy']);
    for (const [name, spec] of Object.entries(TTS_BACKENDS)) for (const field of spec.fields) assert.ok(field in (TTS_STASH_DEFAULTS[name] || {}), `${name}.${field}`);
    const shown = (role, backend, descriptor) => Object.entries(controlsFor(role, backend, descriptor)).filter(([, on]) => on).map(([name]) => name);
    for (const role of ['conversation', 'classifier']) assert.deepEqual(shown(role, 'host_kokoro'), ['url', 'key', 'remote', 'discover', 'model', 'advanced']);
});

// ---- the setup store (Alpine-like reactive state: structuredClone() on it throws, as in the browser)

async function setupFixture(tts, { reactive = true, extra = {}, sidecar = {}, registry = null, discover = null } = {}) {
    const config = plain(defaults); config.roles.tts = { ...config.roles.tts, ...plain(tts) }; config.sidecar = { ...config.sidecar, ...sidecar };
    const calls = [];
    const result = { ok: true, config, revision: 'r1', readiness: [], credentials: {}, decision_backends: [], providers: [], native: {}, ...extra };
    const bindings = hostStubs({
        '/js/api.js': { callJsonApi: async (_url, payload) => { calls.push(plain(payload));
            return ['discover', 'connection_test'].includes(payload.action) && discover ? discover(payload) : result; } },
        '/components/plugins/plugin-settings-store.js': { store: { openConfig: async () => {} } },
        ...(registry ? { '/js/tts-service.js': { ttsService: { providers: new Map(registry.map(id => [id, { synthesize: async () => ({}) }])) } } } : {}),
    }, { reactive });
    const { namespace } = await loadModule('webui/setup-store.js', { bindings, globals: { window: { confirm: () => true } } });
    await namespace.store.load();
    return { store: namespace.store, calls };
}

const TOUR = [['audiocpp', 'external'], ['audiocpp', 'managed'], ['openai_tts', ''], ['llamacpp', ''], ['unsloth_studio', ''], ['openai_compatible', ''],
              ['kokoro_native', ''], ['audiocpp', 'external'], ['openai_tts', ''], ['audiocpp', 'managed']];

test('Switching providers works again and again through an Alpine-like reactive store, every provider and both audio.cpp connections', async () => {
    const f = await setupFixture(AUDIOCPP);
    // each provider (and each audio.cpp connection) gets its own voice the first time it is visited; every later visit must find it again
    const own = { 'audiocpp/external': 'clone:2098c329bc9d', 'audiocpp/managed': 'clone:studio-voice', 'openai_tts/': 'coral', 'llamacpp/': 'tara',
                  'unsloth_studio/': 'u-voice', 'openai_compatible/': 'g-voice' };
    const visited = new Set();
    for (const round of [1, 2]) {
        for (const [provider, connection] of TOUR) {
            assert.equal(f.store.setSpeechProvider(provider), provider, `round ${round}: ${provider}`);
            if (connection) assert.equal(f.store.setSpeechConnection(connection), connection, `round ${round}: ${provider} ${connection}`);
            const tts = () => f.store.config.roles.tts;
            assert.deepEqual(plain(effectiveProvider(tts())), [provider, connection]);
            assert.equal(tts().backend, backendFor(provider, connection));
            assert.equal(f.store.speechProvider(), provider);
            if (provider === 'openai_tts') assert.ok(!String(tts().voice).startsWith('clone:'), 'no clone voice in OpenAI');
            const key = `${provider}/${connection}`;
            if (!(key in own)) continue;
            if (visited.has(key)) assert.equal(tts().voice, own[key], `round ${round}: ${key} finds its own voice again`);
            else { f.store.speechComboPick('voices', own[key]); visited.add(key); }
        }
    }
    assert.equal(visited.size, Object.keys(own).length);
    assert.equal(f.store.setSpeechProvider('strata'), 'audiocpp');                                  // an unknown id changes nothing and says so
    assert.deepEqual(plain(f.store.choices.tts), {});                                                 // discovery belongs to the provider that was asked
    assert.equal(f.store.dirty, true);
    f.store.discard();
    assert.deepEqual(plain(f.store.config.roles.tts), plain({ ...defaults.roles.tts, ...AUDIOCPP }));
    assert.equal(f.store.dirty, false);
    assert.equal(f.store.setBackend('host_kokoro'), 'host_kokoro');                                   // the older entry point switches providers too
    assert.equal(f.store.config.roles.tts.provider, 'kokoro_native');
});

test('No clone voice leaks into OpenAI: its voices are the fixed list for the chosen model', async () => {
    const f = await setupFixture(AUDIOCPP);
    f.store.choices.tts = { voices: [{ id: 'clone:2098c329bc9d', label: 'J.A.R.V.I.S' }], models: [{ id: 'qwen3-tts-1.7b-base-bf16' }] };
    assert.deepEqual(plain(f.store.speechOptions('voices').map(option => option.id)), ['clone:2098c329bc9d']);
    f.store.setSpeechProvider('openai_tts');
    assert.deepEqual(plain(f.store.choices.tts), {});
    const voices = f.store.speechOptions('voices').map(option => option.id);
    assert.equal(voices.length, 13);
    assert.ok(voices.includes('marin') && !voices.some(id => id.startsWith('clone:')));
    assert.deepEqual(plain(f.store.speechOptions('models').map(option => option.id)), ['gpt-4o-mini-tts', 'tts-1', 'tts-1-hd']);
    f.store.speechComboPick('voices', 'ballad');
    f.store.speechComboPick('models', 'tts-1');
    const narrow = f.store.speechOptions('voices');
    assert.equal(narrow[0].id, 'ballad');
    assert.match(narrow[0].label, /not offered by tts-1/);                                            // kept and marked, never silently replaced
    assert.equal(narrow.filter(option => !option.unavailable).length, 9);
    assert.equal(f.store.config.roles.tts.voice, 'ballad');
});

test('Which controls show: the provider decides, and delivery instructions appear only where they are honoured', async () => {
    const f = await setupFixture(AUDIOCPP);
    const shown = () => Object.entries(f.store.speechControls()).filter(([, on]) => on).map(([name]) => name);
    for (const name of Object.keys(f.store.speechControls())) assert.ok(SPEECH_CONTROLS.includes(name), name);
    assert.deepEqual(plain(shown()), ['connection', 'url', 'key', 'discover', 'test', 'model', 'voice', 'server', 'pcm', 'declare', 'advanced']);
    f.store.config.roles.tts.accepts_instructions = true;
    assert.equal(f.store.speechControls().instructions, false);                                       // a Base clone model ignores them
    assert.match(f.store.speechInstructionsNote(), /Base clone/);
    f.store.speechComboPick('models', 'qwen3-tts-1.7b-customvoice'); f.store.speechComboPick('voices', 'vivian');
    assert.equal(f.store.speechControls().instructions, true);
    f.store.setSpeechConnection('managed');
    assert.deepEqual(plain(shown()), ['connection', 'runtime', 'discover', 'test', 'model', 'voice', 'tuning', 'pcm', 'studio']);
    f.store.setSpeechProvider('openai_tts');
    assert.deepEqual(plain(shown()), ['apibase', 'key', 'keysource', 'remote', 'test', 'model', 'voice', 'instructions', 'advanced']);
    f.store.speechComboPick('models', 'tts-1');
    assert.equal(f.store.speechControls().instructions, false);
    assert.match(f.store.speechInstructionsNote(), /only with gpt-4o-mini-tts/);
    f.store.setSpeechProvider('kokoro_native');
    assert.deepEqual(plain(shown()), ['native']);
    f.store.setSpeechProvider('llamacpp');
    assert.deepEqual(plain(shown()), ['url', 'key', 'discover', 'test', 'model', 'voice', 'declare', 'advanced']);
    f.store.config.roles.tts.url = 'https://speech.example.com/v1';
    assert.equal(f.store.speechControls().remote, true);                                              // a remote address needs consent
    f.store.setSpeechProvider('openai_compatible');
    assert.equal(f.store.speechControls().pcm, true);
});

test('Typed delivery instructions survive a model that cannot use them and come back with one that can', async () => {
    const f = await setupFixture({ provider: 'openai_tts', url: 'https://api.openai.com/v1', model: 'gpt-4o-mini-tts', voice: 'coral', remote: true, capabilities: { audio_output: true, cloning: true, instruction_control: true } });
    f.store.config.roles.tts.instructions = 'calm and slow';
    f.store.speechComboPick('models', 'tts-1');
    assert.deepEqual([f.store.config.roles.tts.instructions, f.store.config.roles.tts.capabilities.instruction_control], ['', undefined]);   // never sent where it is refused
    assert.equal(f.store.config.roles.tts.provider_saved.openai_tts.instructions, 'calm and slow');
    f.store.speechComboPick('models', 'gpt-4o-mini-tts');
    assert.deepEqual([f.store.config.roles.tts.instructions, f.store.config.roles.tts.capabilities.instruction_control], ['calm and slow', true]);
    assert.equal(f.store.config.roles.tts.provider_saved, undefined);
});

test('Waiting delivery instructions are not lost when you switch away and back, per provider and per audio.cpp connection', () => {
    const openai = normalizeTts(withDefaults({ provider: 'openai_tts', url: 'https://api.openai.com/v1', model: 'tts-1', voice: 'alloy', remote: true, instructions: 'warm' }));
    assert.deepEqual([openai.instructions, plain(openai.provider_saved)], ['', { openai_tts: { instructions: 'warm' } }]);
    const away = switchProvider(openai, 'llamacpp');
    assert.equal(away.provider_saved.openai_tts.instructions, 'warm');                          // not replaced by the empty text in use
    const back = switchProvider(away, 'openai_tts');
    assert.deepEqual([back.model, back.instructions, back.provider_saved.openai_tts.instructions], ['tts-1', '', 'warm']);
    const mini = normalizeTts({ ...back, model: 'gpt-4o-mini-tts' });
    assert.deepEqual([mini.instructions, mini.capabilities.instruction_control], ['warm', true]);
    const external = normalizeTts(withDefaults({ ...AUDIOCPP, accepts_instructions: true, instructions: 'calm' }));   // a Base clone: waits under its connection
    assert.deepEqual(plain(external.provider_saved), { audiocpp: { external: { instructions: 'calm' } } });
    const managed = switchProvider(external, 'audiocpp', 'managed');
    assert.equal(managed.provider_saved.audiocpp.external.instructions, 'calm');
    const returned = normalizeTts({ ...switchProvider(managed, 'audiocpp', 'external'), model: 'qwen3-tts-1.7b-customvoice', voice: 'vivian' });
    assert.deepEqual([returned.instructions, returned.capabilities.instruction_control], ['calm', true]);
});

test('Your audio.cpp port is offered, and nothing changes until you confirm one way or the other', async () => {
    const legacy = { backend: 'openai', url: 'http://127.0.0.1:8890/v1', model: 'qwen3-tts-1.7b-base-bf16', voice: 'clone:2098c329bc9d', capabilities: { audio_output: true, cloning: true, native_pcm: true } };
    for (const way of ['accept', 'decline', 'select']) {
        const f = await setupFixture(legacy);
        assert.equal(f.store.speechOffer(), 'http://127.0.0.1:8890/v1');
        assert.equal(f.store.speechProvider(), 'openai_compatible');                                 // derived from the backend until you choose
        assert.deepEqual(plain(['openai', 'managed_audio', 'host_kokoro', ''].map(backend => f.store.speechDerived(backend))), ['openai_compatible', 'audiocpp', 'kokoro_native', 'openai_compatible']);
        assert.equal(f.store.dirty, false);                                                           // reading the offer changes nothing
        if (way === 'accept') f.store.speechAcceptOffer();
        if (way === 'decline') f.store.speechDeclineOffer();
        if (way === 'select') assert.equal(f.store.setSpeechProvider('audiocpp'), 'audiocpp');      // choosing audio.cpp is confirming it
        const tts = f.store.config.roles.tts;
        assert.equal(tts.provider, way === 'decline' ? 'openai_compatible' : 'audiocpp', way);
        assert.deepEqual([tts.backend, tts.url, tts.model, tts.voice, tts.capabilities.native_pcm], ['openai', legacy.url, legacy.model, legacy.voice, true], way);
        assert.equal(f.store.speechOffer(), '');
        assert.equal(f.store.dirty, true);
    }
});

test('Managed audio.cpp shows why it cannot speak while the runtime is off; the inline switch is the existing setting', async () => {
    const f = await setupFixture({ ...AUDIOCPP, connection: 'managed', backend: 'managed_audio' });
    assert.match(f.store.speechReason(), /Manage runtime/);
    f.store.config.sidecar.managed = true;
    assert.equal(f.store.speechReason(), '');
    const html = await readWebui('speech-output-field.html');
    assert.match(html, /x-show="\$store\.convoSetup\.show\('tts', 'runtime'\)"[\s\S]*?x-model="\$store\.convoSetup\.config\.sidecar\.managed"/);
});

test('Kokoro off says so, and other host speech found only in the browser is listed but cannot be chosen', async () => {
    const providers = [{ id: 'host_kokoro', label: 'Native Kokoro', available: false, reason: 'Native Kokoro is off or missing in this host. Turn it on in Plugins to use it.', controls: ['native'] }];
    const f = await setupFixture(AUDIOCPP, { extra: { providers }, registry: ['_kokoro_tts', '_piper_tts'] });
    await f.store.speechRefreshRegistry();
    const builtin = f.store.speechGroups().find(group => group.id === 'builtin');
    assert.deepEqual(plain(builtin.items.map(item => [item.id, item.disabled])), [['kokoro_native', false], ['browser:_piper_tts', true]]);
    assert.match(builtin.items[0].label, /not available now/);
    assert.match(builtin.items[1].label, /browser-only; Convo speaks from the server/);
    assert.deepEqual(plain(f.store.speechGroups().map(group => group.id)), ['builtin', 'local', 'cloud', 'other']);
    assert.equal(f.store.setSpeechProvider('browser:_piper_tts'), 'audiocpp');                         // never selectable
    f.store.setSpeechProvider('kokoro_native');
    assert.match(f.store.speechReason(), /Native Kokoro is off/);
    const none = await setupFixture(AUDIOCPP);                                                         // a host without the registry lists only the catalog
    await none.store.speechRefreshRegistry();
    assert.equal(none.store.speechGroups().find(group => group.id === 'builtin').items.length, 1);
});

test('A check that cannot run says why and is never sent; a check that runs reports what the server answered', async () => {
    let answer = null;
    const f = await setupFixture({ provider: 'unsloth_studio', url: '', backend: 'openai' }, { discover: () => answer });
    assert.match(f.store.speechBlocked(), /no documented default address/);
    assert.match(f.store.speechStatus().message, /no documented OpenAI-style speech route/);
    assert.equal(f.store.speechStatus().label, 'Not verified');
    f.store.setSpeechProvider('openai_tts');
    assert.match(f.store.speechBlocked(), /consent/);
    f.store.config.roles.tts.remote = true;
    assert.equal(f.store.speechBlocked(), '');
    f.store.setSpeechProvider('audiocpp');
    answer = { ok: true, models: [{ id: 'qwen3-tts-1.7b-base-bf16', label: 'qwen3-tts-1.7b-base-bf16' }], voices: [{ id: 'clone:2098c329bc9d', label: 'J.A.R.V.I.S' }], tuning: [],
               warnings: [], checks: [{ path: '/v1/models', status: 200, answer: 'lists 1 model' }, { path: '/v1/audio/speech', status: 405, answer: 'speech route present (it takes POST requests)' }],
               server: { model: 'qwen3-tts-0.6b-base-bf16', loaded: true, state: 'loaded', tuning: { id: 'balanced', label: 'Balanced' } },
               verification: { state: 'unverified', message: 'Your server has qwen3-tts-0.6b-base-bf16 loaded.' } };
    await f.store.discover('tts', true);
    assert.equal(f.calls.at(-1).action, 'connection_test');
    assert.deepEqual(plain(f.store.speechChecks()), ['GET /v1/models → HTTP 200 · lists 1 model', 'GET /v1/audio/speech → HTTP 405 · speech route present (it takes POST requests)']);
    assert.equal(f.store.speechStatus().label, 'Not verified');
    assert.equal(f.store.speechServer().tuning.label, 'Balanced');
    assert.equal(f.store.speechVoiceLabel(), '');
    f.store.speechComboPick('voices', 'clone:2098c329bc9d');
    assert.equal(f.store.speechVoiceLabel(), 'J.A.R.V.I.S');
    f.store.speechUseServerModel();                                                                  // adopts what the server already has; sends nothing
    assert.equal(f.store.config.roles.tts.model, 'qwen3-tts-0.6b-base-bf16');
    assert.equal(f.calls.filter(call => call.action !== 'setup_read').length, 1);
    const html = await settingsMarkup();
    const bindings = [...html.matchAll(/:disabled="([^"]*isDiscovering[^"]*)"/g)].map(match => match[1]);
    const clean = await setupFixture({});
    for (const expression of bindings) assert.equal(new Function('$store', 'role', 'return ' + expression)({ convoSetup: clean.store }, 'conversation'), false, expression);
});

test('The model and voice search is keyboard-operable and Escape closes only the list', async () => {
    const f = await setupFixture(AUDIOCPP);
    f.store.choices.tts = { models: [{ id: 'qwen3-tts-1.7b-base-bf16', label: 'qwen3-tts-1.7b-base-bf16' }, { id: 'qwen3-tts-0.6b-base-bf16', label: 'qwen3-tts-0.6b-base-bf16' }] };
    const key = name => { const event = { key: name, prevented: false, stopped: false, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; } }; f.store.speechComboKey('models', event); return event; };
    f.store.config.roles.tts.model = '';
    assert.equal(f.store.speechComboIsOpen('models'), false);
    assert.equal(key('ArrowDown').prevented, true);
    assert.equal(f.store.speechComboIsOpen('models'), true);
    assert.equal(f.store.speechComboActive('models'), 0);
    key('ArrowDown');
    assert.equal(f.store.speechComboActive('models'), 1);
    const enter = key('Enter');
    assert.equal(enter.prevented, true);
    assert.equal(f.store.config.roles.tts.model, 'qwen3-tts-0.6b-base-bf16');
    assert.equal(f.store.speechComboIsOpen('models'), false);
    key('ArrowDown');
    const escape = key('Escape');
    assert.equal(escape.stopped, true);                                                               // the list closes; the settings modal stays
    assert.equal(f.store.speechComboIsOpen('models'), false);
    assert.equal(key('Escape').stopped, false);                                                       // a closed list lets Escape reach the modal
    f.store.config.roles.tts.model = '0.6b';
    assert.deepEqual(plain(f.store.speechComboItems('models').map(option => option.id)), ['qwen3-tts-0.6b-base-bf16', 'qwen3-tts-1.7b-base-bf16']);   // matches first
});

// ---- markup

const markup = Promise.all(['speech-output-field.html', 'speech-voice-field.html'].map(readWebui)).then(files => files.join('\n'));

test('The speech markup never hard-codes a provider into a control: it asks which controls apply', async () => {
    const html = await markup;
    const asked = [...html.matchAll(/\.show\('tts',\s*'(\w+)'\)/g)].map(match => match[1]);
    for (const control of asked) assert.ok(SPEECH_CONTROLS.includes(control), control);
    for (const control of SPEECH_CONTROLS) assert.ok(asked.includes(control), control + ' is conditioned in the markup');
    assert.doesNotMatch(html, /roles\.tts\.(backend|provider) === '/);                                 // nothing keys on a name
    assert.match(html, /@change="\$event\.target\.value = \$store\.convoSetup\.setSpeechProvider\(\$event\.target\.value\)"/);   // a switch goes through the stash
    assert.match(html, /@change="\$event\.target\.value = \$store\.convoSetup\.setSpeechConnection\(\$event\.target\.value\)"/);
    assert.doesNotMatch(html, /x-model="\$store\.convoSetup\.config\.roles\.tts\.(backend|provider|connection)"/);
    // the select shows the stored provider, or the one the stored backend means; choosing one writes both (the backend is derived)
    assert.match(html, /:value="\$store\.convoSetup\.config\.roles\.tts\.provider \|\| \$store\.convoSetup\.speechDerived\(\$store\.convoSetup\.config\.roles\.tts\.backend\)"/);
    assert.match(html, /<optgroup :label="group\.label">/);
    assert.equal((html.match(/x-show="!\$store\.convoSetup\.inConfigModal"/g) || []).length, 1);     // the native Kokoro button
    assert.doesNotMatch(html, /localStorage|sessionStorage|x-html|:title=|api_key|convo-settings-pending/);
});

test('Delivery instructions carry the agreed label and the declaration is its own checkbox', async () => {
    const html = await markup;
    assert.match(html, /Voice delivery — how the voice should sound \(tone, pace, emotion\)\./);
    assert.match(html, /Sent with each phrase to the speech engine only; it doesn't change the words \(that's Answer length and Personality\)\./);
    assert.match(html, /x-model="\$store\.convoSetup\.config\.roles\.tts\.instructions"/);
    assert.match(html, /:checked="\$store\.convoSetup\.config\.roles\.tts\.accepts_instructions === true" @change="\$store\.convoSetup\.speechSetDeclared\(\$event\.target\.checked\)"/);
    assert.match(html, /Accepts delivery instructions \(declared\)/);
    assert.doesNotMatch(html, /capabilities\.instruction_control"/);                                  // set by normalization, never by hand
});

test('The search lists are labelled listboxes that the input controls', async () => {
    const html = await readWebui('speech-output-field.html');
    for (const kind of ['models', 'voices']) {
        assert.match(html, new RegExp(`role="combobox"[^>]*:aria-expanded="\\$store\\.convoSetup\\.speechComboIsOpen\\('${kind}'\\)`));
        assert.match(html, new RegExp(`@keydown="\\$store\\.convoSetup\\.speechComboKey\\('${kind}', \\$event\\)"`));
    }
    assert.equal((html.match(/role="listbox"/g) || []).length, 2);
    assert.match(html, /role="option"/);
    assert.match(html, /:aria-activedescendant=/);
    assert.match(html, /x-id="\[/);                                                                   // ids are unique per rendered card
});
