// Kept in sync with helpers/convo_contract.py by the contract tests.
import { TTS_BACKENDS } from './speech-catalog.js';
// The speech catalog lives in speech-catalog.js (owned with the speech editor); re-exported so existing imports keep working.
export { TTS_BACKENDS, TTS_STASH_DEFAULTS, SPEECH_PROVIDERS, switchBackend } from './speech-catalog.js';

export const defaults = {
  "enabled": false,
  "ambient_enabled": false,
  "remote_ambient_consent": false,
  "transcription_enabled": false,
  "voice_message_draft": "single",
  "developer_mode": false,
  "ai_link_development_mode": false,
  "system_one": {"enabled": false, "backend": ""},
  "decider": {"source": "", "kind": "d1_llama_cpp", "url": "", "model": ""},
  "style": "",
  "response_detail": "balanced",
  "spoken_updates": "completion",
  "activation_names": [],
  "hotkey": "Ctrl+Shift+Space",
  "sidecar": {
    "managed": false,
    "url": "http://convo-runtime:8090",
    "token_env": "CONVO_SIDECAR_TOKEN",
    "remote": false
  },
  "roles": {
    "conversation": {
      "provider": "",
      "key_source": "provider",
      "api_base": "",
      "url": "http://host.docker.internal:8080/v1",
      "model": "",
      "token_env": "",
      "remote": false,
      "capabilities": {
        "audio_input": true,
        "cancellation": true,
        "tool_calls": true
      }
    },
    "classifier": {
      "provider": "",
      "key_source": "provider",
      "api_base": "",
      "context_window": 8192,
      "kind": "auto",
      "url": "",
      "model": "",
      "token_env": "",
      "remote": false,
      "capabilities": {
        "audio_input": true,
        "cancellation": true
      }
    },
    "tts": {
      "backend": "openai",
      "provider": "",
      "connection": "",
      "key_source": "provider",
      "accepts_instructions": false,
      "url": "http://convo-audio:8080/v1",
      "model": "",
      "voice": "",
      "token_env": "",
      "remote": false,
      "capabilities": {
        "audio_output": true,
        "cloning": true
      }
    }
  },
  "compaction": {
    "trigger": 0.7,
    "target": 0.5,
    "recent_exchanges": 6,
    "context_tokens": 16384,
    "output_reserve": 2048
  },
  "direct_timeout": 30,
  "clarification_cooldown": 60,
  "audio_token_reserve": 4096,
  "max_audio_seconds": 20,
  "echo_guard": "adaptive",
  "speculative_reply": false,
  "main_listen_decision": false,
  "continuation_seconds": 7,
  "first_clause_speech": false,
  "recent_remarks_seconds": 0,
  "policy_gate": {
    "respond_min": 0.3,
    "ambient_respond_min": 0.9,
    "interrupt_min": 0.6,
    "ignore_min": 1.0,
    "name_min": 0.9,
    "clarify_min": 0.3,
    "clarify_elsewhere_max": 0.5
  }
};
// A plain copy of JSON settings. Never structuredClone() them: in the browser they are Alpine's reactive Proxies, which
// structuredClone() refuses (DataCloneError), while plain test objects pass. Settings are JSON by contract, so this is exact.
export const plain = value => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));

export function fillDefaults(target, source = defaults) {
    for (const [key, value] of Object.entries(source)) {
        if (!(key in target)) target[key] = plain(value);
        else if (value && typeof value === 'object' && !Array.isArray(value) && target[key] && typeof target[key] === 'object') fillDefaults(target[key], value);
    }
    return target;
}

const CONTROLS = ["url", "key", "remote", "discover", "model", "voice", "advanced", "tuning", "studio", "instructions", "pcm", "native"];
const ROLE_CONTROLS = ["url", "key", "remote", "discover", "model", "advanced"];   // the other model roles: one OpenAI-style endpoint

// Control id -> shown, for a role. The server's descriptor wins when it has loaded; the table is the fallback.
export function controlsFor(role, backend, descriptor) {
    const names = role === 'tts' ? (descriptor?.controls || (TTS_BACKENDS[backend] || TTS_BACKENDS.openai).controls) : ROLE_CONTROLS;
    return Object.fromEntries(CONTROLS.map(name => [name, names.includes(name)]));
}

// ---- two editors, one file: three-way merge by leaf path
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
export function getPath(object, path) { return path.split('.').reduce((node, key) => (node == null ? undefined : node[key]), object); }
export function setPath(object, path, value) {
    const keys = path.split('.'), last = keys.pop();
    let node = object;
    for (const key of keys) node = (node[key] = isObject(node[key]) ? node[key] : {});
    if (value === undefined) delete node[last]; else node[last] = plain(value);
}

// base: what the editor loaded; mine: its draft; theirs: what is saved now. A leaf only I changed is kept, a leaf only
// the other side changed is taken, and a leaf both changed differently is a conflict (kept as mine until chosen).
// Arrays are leaves. Credentials never pass through here: they are write-only and handled apart.
export function mergeSettings(base, mine, theirs) {
    const merged = plain(theirs), conflicts = [];
    const walk = (b, m, t, target, path) => {
        for (const key of new Set([...Object.keys(b || {}), ...Object.keys(m || {}), ...Object.keys(t || {})])) {
            const here = path ? `${path}.${key}` : key, bv = b?.[key], mv = m?.[key], tv = t?.[key];
            if ([bv, mv, tv].every(value => value === undefined || isObject(value)) && [bv, mv, tv].some(isObject)) {
                // A subtree removed on one side and untouched on the other, or removed on both, is gone: not an empty shell.
                if ((mv === undefined && (tv === undefined || same(tv, bv))) || (tv === undefined && same(mv, bv))) { delete target[key]; continue; }
                if (!isObject(target[key])) target[key] = {};
                walk(bv, mv, tv, target[key], here);
                continue;
            }
            if (same(mv, bv) || same(mv, tv)) { if (tv === undefined) delete target[key]; else target[key] = plain(tv); }   // only they changed it, or we agree
            else if (same(tv, bv)) { if (mv === undefined) delete target[key]; else target[key] = plain(mv); }                // only I changed it
            else { conflicts.push({ path: here, mine: mv, theirs: tv }); if (mv === undefined) delete target[key]; else target[key] = plain(mv); }
        }
    };
    walk(base, mine, theirs, merged, '');
    return { merged, conflicts };
}
