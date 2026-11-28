// The browser speech catalog: the speech providers the settings offer (grouped like Agent Zero's own providers), the runtime
// backend each resolves to, the fields each owns, and the pure switching and normalization over roles.tts. Mirrored from
// helpers/speech_providers.py by contract tests (strict JSON tables so those tests can read them; normalization by the shared
// cases in tests/fixtures/speech-normalize-cases.json). Its one import is the address rule every connection card shares, so the
// store, the markup and node tests share one definition; defaults.js re-exports the backend tables.
import { isLocalHost } from './connection-rules.js';

export const TTS_BACKENDS = {
  "openai": {"label": "External compatible speech API", "controls": ["url", "key", "remote", "discover", "model", "voice", "advanced", "instructions", "pcm"], "fields": ["url", "model", "voice", "token_env", "remote", "instructions"]},
  "managed_audio": {"label": "Convo-owned audio.cpp", "controls": ["discover", "model", "voice", "tuning", "studio", "instructions", "pcm"], "fields": ["model", "voice", "tuning_profile", "instructions"]},
  "host_kokoro": {"label": "Native Kokoro", "controls": ["native"], "fields": []}
};
export const TTS_STASH_DEFAULTS = {
  "openai": {"url": "http://convo-audio:8080/v1", "model": "", "voice": "", "token_env": "", "remote": false, "instructions": ""},
  "managed_audio": {"model": "", "voice": "", "tuning_profile": "", "instructions": ""}
};
// roles.tts.provider ids ('' = derived from the backend). A value given per connection is a {connection: value} map. `backend`:
// the runtime protocol; `default_url`: editable default ('' = none documented); `host_key`: the Agent Zero provider whose stored
// key it uses ('' = Convo's own key); `models`: fixed {model: voices} ({} = what the server lists); `instructions`: 'never',
// 'declared' or the models that honour them; `pcm`: offers the native PCM16 stream; `verify`: how Test connection reads it.
export const SPEECH_PROVIDERS = {
  "kokoro_native": {"label": "Kokoro", "group": "builtin", "backend": "host_kokoro", "connections": [], "default_url": "", "host_key": "",
                    "models": {}, "instructions": "never", "pcm": false, "verify": "native"},
  "audiocpp": {"label": "audio.cpp", "group": "local", "backend": {"external": "openai", "managed": "managed_audio"}, "connections": ["external", "managed"],
               "default_url": "http://host.docker.internal:8890/v1", "host_key": "", "models": {},
               "instructions": {"external": "declared", "managed": "never"}, "pcm": true, "verify": {"external": "audiocpp", "managed": "managed"}},
  "llamacpp": {"label": "llama.cpp", "group": "local", "backend": "openai", "connections": [], "default_url": "http://host.docker.internal:8080/v1",
               "host_key": "", "models": {}, "instructions": "declared", "pcm": false, "verify": "speech_route"},
  "unsloth_studio": {"label": "Unsloth Studio", "group": "local", "backend": "openai", "connections": [], "default_url": "", "host_key": "",
                     "models": {}, "instructions": "declared", "pcm": false, "verify": "speech_route"},
  "openai_tts": {"label": "OpenAI", "group": "cloud", "backend": "openai", "connections": [], "default_url": "https://api.openai.com/v1", "host_key": "openai",
                 "models": {"gpt-4o-mini-tts": ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse", "marin", "cedar"],
                            "tts-1": ["alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"],
                            "tts-1-hd": ["alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"]},
                 "instructions": ["gpt-4o-mini-tts"], "pcm": false, "verify": "models"},
  "openai_compatible": {"label": "OpenAI-compatible speech endpoint", "group": "other", "backend": "openai", "connections": [], "default_url": "",
                        "host_key": "", "models": {}, "instructions": "declared", "pcm": true, "verify": "speech_route"}
};
// The provider groups in the order the editor lists them.
export const SPEECH_GROUPS = [
  {"id": "builtin", "label": "Agent Zero built-in"},
  {"id": "local", "label": "Local servers"},
  {"id": "cloud", "label": "Cloud"},
  {"id": "other", "label": "Other"}
];
// The speech card's control vocabulary (speechControls in speech-setup.js decides which apply; the markup asks with show('tts', name)).
export const SPEECH_CONTROLS = ['native', 'connection', 'runtime', 'apibase', 'url', 'key', 'keysource', 'remote', 'discover', 'test', 'model', 'voice',
                                'tuning', 'server', 'pcm', 'studio', 'declare', 'instructions', 'advanced'];

// Settings are JSON by contract; in the browser they are Alpine's reactive Proxies, which structuredClone() refuses.
const plain = value => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const per = (value, connection) => (isObject(value) ? value[connection] : value);
const LEGACY = { openai: ['openai_compatible', ''], managed_audio: ['audiocpp', 'managed'], host_kokoro: ['kokoro_native', ''] };
const KEY_SOURCES = ['provider', 'convo'];
const BOOLEAN_FIELDS = ['remote', 'accepts_instructions', 'native_pcm'];
const BASE_MODEL = /(^|[^a-z0-9])base([^a-z0-9]|$)/;
const AUDIOCPP_PORT = '8890';
const AUDIOCPP_HOSTS = ['127.0.0.1', 'localhost', 'host.docker.internal', '::1'];

// A catalog value that may be given per connection; the provider an older configuration's backend means.
export const perConnection = per;
export const providerForBackend = backend => LEGACY[backend] || null;
export const choiceLabel = (provider, connection = '') => SPEECH_PROVIDERS[provider].label + ({ external: ' (external server)', managed: ' (managed by Convo)' }[connection] || '');

export function backendFor(provider, connection = '') {
    const spec = SPEECH_PROVIDERS[provider];
    if (!spec) return null;
    return isObject(spec.backend) ? spec.backend[connection || spec.connections[0]] ?? null : spec.backend;
}

// [provider, connection] in effect: the stored provider, or for an older configuration the one its backend means. Never the URL.
export function effectiveProvider(tts = {}) {
    const spec = SPEECH_PROVIDERS[tts?.provider || ''];
    if (spec) return [tts.provider, spec.connections.length ? (spec.connections.includes(tts.connection) ? tts.connection : spec.connections[0]) : ''];
    return LEGACY[tts?.backend || 'openai'] || LEGACY.openai;
}

// roles.tts fields one provider (and connection) owns: its backend's fields, the declaration and the PCM toggle where they apply.
export function connectionFields(provider, connection = '') {
    const spec = SPEECH_PROVIDERS[provider];
    const fields = [...TTS_BACKENDS[backendFor(provider, connection)].fields];
    if (per(spec.instructions, connection) === 'declared') fields.push('accepts_instructions');
    if (per(spec.pcm, connection)) fields.push('native_pcm');
    return fields;
}

export const pcmSupported = (provider, connection = '') => !!per(SPEECH_PROVIDERS[provider]?.pcm, connection);
// A Base (voice-cloning) model or a clone voice copies a reference voice and ignores delivery instructions.
export const isBaseClone = (model = '', voice = '') => String(voice || '').startsWith('clone:') || BASE_MODEL.test(String(model || '').toLowerCase());

// Whether delivery instructions are honoured for this provider, connection, model and voice. For an older configuration (no
// provider) its hand-set instruction_control flag counts as the declaration.
export function instructionsSupported(tts, provider, connection) {
    if (provider === undefined) [provider, connection] = effectiveProvider(tts);
    const rule = per(SPEECH_PROVIDERS[provider].instructions, connection || '');
    const model = String(tts.model || ''), voice = String(tts.voice || '');
    if (rule === 'never') return false;
    if (rule === 'declared') return (tts.accepts_instructions === true || (!tts.provider && tts.capabilities?.instruction_control === true)) && !isBaseClone(model, voice);
    return rule.some(name => model === name || model.startsWith(name + '-'));
}

function parkSlot(tts, provider, connection, create) {
    if (!isObject(tts.provider_saved)) { if (!create) return null; tts.provider_saved = {}; }
    let node = tts.provider_saved;
    for (const key of SPEECH_PROVIDERS[provider].connections.length ? [provider, connection] : [provider]) {
        if (!isObject(node[key])) { if (!create) return null; node[key] = {}; }
        node = node[key];
    }
    return node;
}

function prune(tts, provider, connection) {
    const saved = tts.provider_saved;
    if (!isObject(saved)) return;
    const record = saved[provider];
    if (SPEECH_PROVIDERS[provider].connections.length && isObject(record?.[connection]) && !Object.keys(record[connection]).length) delete record[connection];
    if (isObject(record) && !Object.keys(record).length) delete saved[provider];
    if (!Object.keys(saved).length) delete tts.provider_saved;
}

// Delivery instructions kept for this provider while its model or voice cannot use them ('' when none wait).
export function parkedInstructions(tts, provider, connection) {
    if (provider === undefined) [provider, connection] = effectiveProvider(tts);
    const slot = parkSlot(tts, provider, connection || '', false);
    return typeof slot?.instructions === 'string' ? slot.instructions : '';
}

// Sent only where honoured (the speech gateway refuses them otherwise); unusable text waits in the stash and comes back.
function park(tts, provider, connection, supported) {
    if (!connectionFields(provider, connection).includes('instructions')) return;
    const active = tts.instructions || '';
    const slot = parkSlot(tts, provider, connection, false);
    const parked = typeof slot?.instructions === 'string' ? slot.instructions : '';
    if (supported) {
        if (!active && parked) tts.instructions = parked;
        if (slot && 'instructions' in slot) { delete slot.instructions; prune(tts, provider, connection); }
    } else if (active) {
        parkSlot(tts, provider, connection, true).instructions = active;
        tts.instructions = '';
    }
}

// The derived speech fields, exactly as helpers/speech_providers.normalize writes them on save. Returns a new object; a field is
// written only when its value changes, so an already normalized draft stays byte-identical (and clean).
export function normalizeTts(input) {
    const tts = { ...input, capabilities: { ...(input.capabilities || {}) } };
    if (input.provider_saved !== undefined) tts.provider_saved = plain(input.provider_saved);
    const [provider, connection] = effectiveProvider(tts);
    const spec = SPEECH_PROVIDERS[tts.provider || ''];
    if (spec) {
        if ((tts.connection ?? '') !== connection) tts.connection = connection;
        const backend = backendFor(provider, connection);
        if (tts.backend !== backend) tts.backend = backend;
        if (connectionFields(provider, connection).includes('url') && !tts.url && spec.default_url) tts.url = spec.default_url;
        if (!pcmSupported(provider, connection) && 'native_pcm' in tts.capabilities) delete tts.capabilities.native_pcm;
    }
    const supported = instructionsSupported(tts, provider, connection);
    park(tts, provider, connection, supported);
    if (supported) { if (tts.capabilities.instruction_control !== true) tts.capabilities.instruction_control = true; }
    else if ('instruction_control' in tts.capabilities) delete tts.capabilities.instruction_control;
    return tts;
}

const fieldDefault = (provider, field) => field === 'url' ? SPEECH_PROVIDERS[provider].default_url
    : field === 'model' ? (Object.keys(SPEECH_PROVIDERS[provider].models)[0] || '')
    : BOOLEAN_FIELDS.includes(field) ? false : '';
const readField = (tts, field) => (field === 'native_pcm' ? tts.capabilities?.native_pcm === true : tts[field]);
function writeField(tts, field, value) {
    if (field !== 'native_pcm') tts[field] = value;
    else if (value === true) tts.capabilities.native_pcm = true;
    else delete tts.capabilities.native_pcm;
}

// What the provider (and connection) has now goes into its provider_saved record. Delivery instructions waiting for a model that
// can use them are kept rather than replaced by the empty text in use. audio.cpp keeps one record per connection.
function stash(saved, provider, connection, tts) {
    const fields = connectionFields(provider, connection);
    if (!fields.length) return;
    const spec = SPEECH_PROVIDERS[provider];
    const record = isObject(saved[provider]) ? saved[provider] : {};
    if (spec.connections.length) {
        // an earlier flat record holds one connection's fields directly: move them under that connection first
        const flat = Object.keys(record).filter(key => !['connection', 'key_source', ...spec.connections].includes(key));
        if (flat.length) {
            const owner = spec.connections.includes(record.connection) ? record.connection : spec.connections[0];
            if (!isObject(record[owner])) record[owner] = Object.fromEntries(flat.filter(key => connectionFields(provider, owner).includes(key)).map(key => [key, record[key]]));
            for (const key of flat) delete record[key];
        }
    }
    const previous = spec.connections.length ? record[connection] : record;
    const values = Object.fromEntries(fields.map(field => [field, readField(tts, field) ?? fieldDefault(provider, field)]));
    if ('instructions' in values && !values.instructions && typeof previous?.instructions === 'string') values.instructions = previous.instructions;
    const key_source = KEY_SOURCES.includes(tts.key_source) ? tts.key_source : 'provider';
    saved[provider] = spec.connections.length ? { ...record, connection, key_source, [connection]: values } : { ...values, key_source };
}

// The target's own fields come back from its record, an earlier flat record, or (for what an older configuration's backend
// meant) the older per-backend stash, which is read and never written; otherwise its defaults. Never another provider's values.
function restore(result, saved, provider, connection, legacy) {
    const spec = SPEECH_PROVIDERS[provider];
    const fields = connectionFields(provider, connection);
    const record = isObject(saved[provider]) ? saved[provider] : null;
    let values = spec.connections.length ? record?.[connection] : record;
    if (!isObject(values) && spec.connections.length && record && (spec.connections.includes(record.connection) ? record.connection : spec.connections[0]) === connection) values = record;
    if (!isObject(values)) {
        const backend = Object.keys(LEGACY).find(name => LEGACY[name][0] === provider && LEGACY[name][1] === connection);
        if (backend && isObject(legacy?.[backend])) values = legacy[backend];
    }
    for (const field of fields) writeField(result, field, isObject(values) && field in values ? values[field] : fieldDefault(provider, field));
    if (fields.length) result.key_source = KEY_SOURCES.includes(record?.key_source) ? record.key_source : 'provider';
}

// Switch the speech provider (and, for audio.cpp, its connection) without losing or mixing settings: what the current one has is
// stashed under provider_saved, the target's own fields come back. Fields the target does not own keep their value. Choosing what
// is already in effect only makes it explicit. An unknown id changes nothing (the same object comes back).
export function switchProvider(tts, next, wanted = '') {
    const spec = SPEECH_PROVIDERS[next];
    if (!spec) return tts;
    const [current, currentConnection] = effectiveProvider(tts);
    const saved = isObject(tts.provider_saved) ? plain(tts.provider_saved) : {};
    const stashed = saved[next]?.connection;
    const connection = !spec.connections.length ? ''
        : spec.connections.includes(wanted) ? wanted
        : current === next ? currentConnection
        : spec.connections.includes(stashed) ? stashed : spec.connections[0];
    if (current === next && currentConnection === connection)
        return tts.provider === next && (tts.connection || '') === connection ? tts : adoptProvider(tts, next, connection);
    const result = { ...tts, capabilities: { ...(tts.capabilities || {}) } };
    stash(saved, current, currentConnection, tts);
    restore(result, saved, next, connection, tts.saved);
    // A provider that owns no address (Kokoro, managed audio.cpp) drops a remote one and its consent: they are in the stash of the
    // provider that had them, and left in place they would make the settings unsavable (no consent) or claim remote processing.
    if (!connectionFields(next, connection).includes('url')) {
        if (result.url && !isLocalUrl(result.url)) result.url = TTS_STASH_DEFAULTS.openai.url;
        result.remote = false;
    }
    result.provider = next; result.connection = connection; result.backend = backendFor(next, connection);
    result.provider_saved = saved;
    if (!Object.keys(saved).length) delete result.provider_saved;
    return normalizeTts(result);
}

// Name the provider of the fields as they are (no stash, no restore): an older configuration confirmed as what it already is.
export function adoptProvider(tts, provider, connection = '') {
    if (!SPEECH_PROVIDERS[provider]) return tts;
    return normalizeTts({ ...tts, provider, connection, backend: backendFor(provider, connection) });
}

// {host, port} of an http(s) address ('' port when none is written), or null. Parsed here because not every page context (or
// test VM) provides URL.
function hostPort(url) {
    const match = /^https?:\/\/(?:[^@/?#]*@)?(\[[^\]/?#]+\]|[^:/?#]+)(?::(\d*))?(?:[/?#]|$)/i.exec(String(url || '').trim());
    return match ? { host: match[1].toLowerCase().replace(/^\[|\]$/g, ''), port: match[2] || '' } : null;
}

// The address of an older openai configuration on the known audio.cpp port of this machine, or ''. Only an offer.
export function legacyAudiocppOffer(tts) {
    if (!tts || tts.provider || (tts.backend || 'openai') !== 'openai') return '';
    const parsed = hostPort(tts.url);
    return parsed && parsed.port === AUDIOCPP_PORT && AUDIOCPP_HOSTS.includes(parsed.host) ? tts.url : '';
}

// Whether the server treats an address as this machine or its network (helpers/convo_contract.endpoint); only clearly local
// addresses count, so a remote one always shows its consent box.
export function isLocalUrl(url) {
    const host = hostPort(url)?.host;
    return !!host && isLocalHost(host);
}

// Change the speech backend without losing or mixing settings (the earlier editor's switch, kept for older callers): what the old
// backend had is stashed under roles.tts.saved, and the new backend's own fields come back from its stash (or start clean, never
// from the old backend's values: a clone voice is not an OpenAI voice). Fields the new backend does not own keep their value.
export function switchBackend(tts, next) {
    const previous = tts.backend || 'openai';
    if (previous === next || !(next in TTS_BACKENDS)) return tts;
    const saved = plain(tts.saved || {});
    if (TTS_BACKENDS[previous]?.fields.length) {
        saved[previous] = Object.fromEntries(TTS_BACKENDS[previous].fields.map(field => [field, tts[field] ?? TTS_STASH_DEFAULTS[previous][field]]));
    }
    const result = { ...tts, backend: next };
    for (const field of TTS_BACKENDS[next].fields) result[field] = saved[next]?.[field] ?? TTS_STASH_DEFAULTS[next][field];
    if (Object.keys(saved).length) result.saved = saved; else delete result.saved;
    return result;
}
