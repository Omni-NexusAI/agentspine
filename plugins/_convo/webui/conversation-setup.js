import { modelRoleSetup, roleUrl, addressProblem, isLocalHost, groupModels, AUTOSAVE_MS } from './model-role-setup.js';

// The conversation model card (conversation-model-field.html). It is shown in Convo → Connections and, as its own model type,
// in Agent Zero's preset editor (model-type-mount.js), where the same card edits the same draft and saves each change by
// itself through setup_patch. The card is the shared model-role factory (model-role-setup.js) with this spec, plus the saved
// choice in words for the preset overview. Methods only (see compose in setup-store.js); `this` is the shared settings store,
// transient view state goes in this.ui.conversation. Agent Zero's provider list, model search and key store come from its own
// `modelConfig` store at the moment of use; keys never pass through Convo's settings.
export { addressProblem, isLocalHost, groupModels, AUTOSAVE_MS };

// Convo's audio verdicts, mirrored from helpers/convo_model_catalog.py by contract tests (strict JSON so those tests can read it).
// It is not the provider list: that is Agent Zero's, read when the select renders.
export const CONVERSATION_CATALOG = {
  "providers": {
    "openai": {"label": "OpenAI", "base": "https://api.openai.com/v1", "host_key": "openai"},
    "openrouter": {"label": "OpenRouter", "base": "https://openrouter.ai/api/v1", "host_key": "openrouter"},
    "google": {"label": "Google", "base": "https://generativelanguage.googleapis.com/v1beta/openai", "host_key": "google"},
    "llama_cpp": {"label": "llama.cpp", "base": "http://host.docker.internal:8080/v1", "host_key": "llama_cpp"},
    "vllm": {"label": "vLLM", "base": "http://host.docker.internal:8000/v1", "host_key": "vllm"},
    "other": {"label": "Other (OpenAI-compatible)", "base": "", "host_key": ""}
  },
  "disabled": {"anthropic": "route", "azure": "route", "bedrock": "route", "github_copilot": "route", "mistral": "format", "codex_oauth": "route", "gemini_api_oauth": "route", "github_copilot_oauth": "route", "xai_grok_oauth": "route", "nvidia_nim": "text", "a0_venice": "text", "cerebras": "text", "cometapi": "text", "deepseek": "text", "groq": "text", "huggingface": "text", "lm_studio": "text", "moonshot": "text", "nebius": "text", "ollama": "text", "ollama_cloud": "text", "omlx": "text", "sambanova": "text", "venice": "text", "xai": "text", "zai": "text", "zai_coding": "text"},
  "reasons": {
    "route": {"short": "no compatible audio route", "reason": "Not reachable through the OpenAI-compatible audio route Convo uses."},
    "text": {"short": "no audio input", "reason": "Convo cannot send audio input through this provider."},
    "format": {"short": "different audio format", "reason": "Takes audio in a different format from the one Convo sends."}
  },
  "unknown": {"short": "unknown provider", "reason": "Unknown provider: Convo cannot send audio to it."}
};
// A provider Convo has no verdict for that Agent Zero describes as OpenAI-compatible (another plugin may add one): selectable.
export const UNKNOWN_COMPATIBLE = { short: 'audio not verified',
    reason: 'Agent Zero lists this provider as OpenAI-compatible; Convo has not verified that it accepts audio input. Test audio input checks it.' };

// Every setting this card edits, with the name a conflict is shown under.
export const CARD_PATHS = {
    'roles.conversation.provider': 'Provider', 'roles.conversation.model': 'Model name', 'roles.conversation.api_base': 'API base URL',
    'roles.conversation.url': 'Server URL', 'roles.conversation.key_source': 'Key source', 'roles.conversation.remote': 'Allow remote processing',
    'roles.conversation.token_env': 'Credential environment reference', 'roles.conversation.capabilities.audio_input': 'Accepts audio input',
    'roles.conversation.capabilities.cancellation': 'Supports cancellation', 'roles.conversation.capabilities.tool_calls': 'Supports tool calls',
    'compaction.context_tokens': 'Context window',
};
const ROWS = ['conversation_provider', 'conversation_key_route'];

// The address a conversation role is reached at (helpers/convo_model_catalog.resolve_url). `compatibleBase` is Agent Zero's
// address for an OpenAI-compatible provider Convo has no entry for ('' = such a provider cannot be used).
export function conversationUrl(role, compatibleBase = '') {
    const spec = CONVERSATION_CATALOG.providers[role?.provider || ''];
    return roleUrl(role, spec || (compatibleBase ? { base: compatibleBase } : null));
}

export const conversationSetup = {
    ...modelRoleSetup('conversation', {
        paths: CARD_PATHS,
        trimmed: ['roles.conversation.model', 'roles.conversation.api_base', 'roles.conversation.url', 'roles.conversation.token_env'],
        address: ['roles.conversation.provider', 'roles.conversation.api_base', 'roles.conversation.url', 'roles.conversation.remote'],
        rows: id => ROWS.includes(id),
        catalog: () => CONVERSATION_CATALOG,
        disabledReason: (id, catalog) => catalog.reasons[catalog.disabled[id]] || null,
        unknown: catalog => catalog.unknown,
        compatible: () => UNKNOWN_COMPATIBLE,
        endpointOnly: { label: 'Endpoint as entered (earlier setup)', name: 'Endpoint as entered' },
        context: { path: 'compaction.context_tokens', min: 4096, message: 'Enter a context window of at least 4096 tokens.' },
        sectionKey: 'convo_conversation_model',
        probe: { action: 'audio_probe', busy: 'Sending one short synthetic clip…' },
        keyCopy: { action: 'conversation_key_copy' },
    }),
    // The saved choice, for the "Convo — provider / model" row in Agent Zero's preset overview.
    conversationSummary() {
        if (!this.baseline) return null;
        const saved = this.conversationSaved(), provider = saved.provider || '', usable = this.conversationProvider(provider);
        const name = !provider ? 'Custom endpoint' : usable ? usable.label : `${this.conversationHostLabels()[provider] || provider} (not usable)`;
        return { provider: name, model: saved.model || '' };
    },
};
