import { modelRoleSetup, addressProblem } from './model-role-setup.js';
import { CONVERSATION_CATALOG } from './conversation-setup.js';

// The Classifier card (classifier-model-field.html): which model decides whether speech is meant for Convo and whether to answer.
// Its fields are the shared model-role factory (model-role-setup.js) on `roles.classifier.*`: Provider, Model name, API key,
// Context window, Accepts audio input, Allow remote processing, Advanced (API base URL, Classifier type, credential reference,
// Check server). Underneath, "Use System One's decider" (`decider.source === 'system_one'`) shows System One's own decider
// read-only instead; Convo's values stay in the draft for when it is off. The server's catalog (`classifier_catalog` from
// setup_read) says which providers can classify; the provider list itself is Agent Zero's, read when the select renders. Saving
// is the shared revision-aware Save; the server's normalizer derives the runtime fields (`decider.url`, `decider.model`,
// `system_one`, `roles.classifier.url`) from these, so this card never writes them. Methods only (see compose in setup-store.js).
export const CLASSIFIER_KIND = 'd1_llama_cpp';                              // the decision plugin's backend (decider.kind)
export const DECIDER_DEFAULT_URL = 'http://host.docker.internal:9931';     // the decision plugin's default for an empty decider.url
// Provider groups in the select, in order: catalog groups map onto them, every other Agent Zero provider is "Other providers".
export const GROUPS = [['local', 'Local servers'], ['cloud', 'Cloud'], ['host', 'Other providers'], ['other', 'Other']];
const GROUP_OF = { decision_local: 'local', decision_cloud: 'cloud', chat: 'cloud', other: 'other', endpoint: 'other', host: 'host' };
export const KIND_LABELS = { auto: 'Auto', decision: 'Decision model (System One protocol)', chat: 'Chat model (JSON verdict)' };
export const CARD_PATHS = {
    'roles.classifier.provider': 'Provider', 'roles.classifier.model': 'Model name', 'roles.classifier.api_base': 'API base URL',
    'roles.classifier.url': 'Server URL', 'roles.classifier.key_source': 'Key source', 'roles.classifier.remote': 'Allow remote processing',
    'roles.classifier.token_env': 'Credential environment reference', 'roles.classifier.capabilities.audio_input': 'Accepts audio input',
    'roles.classifier.context_window': 'Context window', 'roles.classifier.kind': 'Classifier type', 'decider.source': 'Use System One’s decider',
};
const MIGRATED = ['provider', 'kind', 'api_base', 'model'];
const VERDICT_ICONS = { audio: 'hearing', text_only: 'text_fields', remote: 'cloud_off', invalid: 'link_off', unknown: 'help',
                        not_configured: 'block', not_installed: 'extension_off' };
const CHECK_STATES = { decision: ['check_circle', 'Verified: decision model'], chat: ['check_circle', 'Verified: chat model'],
                       unverified: ['help', 'Not verified'], unreachable: ['error', 'Not reachable'], refused: ['block', 'Not checked'] };
const STATE_WORDS = { ready: 'Ready', unverified: 'Not checked', unsupported: 'Not usable', missing: 'Needs attention', unavailable: 'Unavailable', idle: 'Not chosen', warning: 'Warning' };
const own = (table, key) => !!table && Object.prototype.hasOwnProperty.call(table, key);
const EMPTY = { providers: {}, disabled: {} };

// A disabled provider's reason as {short, reason}: the server sends the reason (Convo's conversation verdicts), the option label
// shows its short form.
function why(reason, fallback) {
    if (reason && typeof reason === 'object') return { short: reason.short || fallback, reason: reason.reason || '' };
    const text = typeof reason === 'string' ? reason : '';
    return { short: Object.values(CONVERSATION_CATALOG.reasons).find(item => item.reason === text)?.short || fallback, reason: text };
}

// A separate decider saved before this card existed: the decision plugin's backend selected with decider.source 'convo' or ''.
function legacyDecider(config) {
    const decider = config?.decider || {}, selected = config?.system_one || {};
    return ['convo', ''].includes(decider.source || '') && selected.enabled === true && selected.backend === CLASSIFIER_KIND;
}

const factory = modelRoleSetup('classifier', {
    paths: CARD_PATHS,
    trimmed: ['roles.classifier.model', 'roles.classifier.api_base', 'roles.classifier.url', 'roles.classifier.token_env'],
    address: ['roles.classifier.provider', 'roles.classifier.api_base', 'roles.classifier.url', 'roles.classifier.remote'],
    rows: id => id.startsWith('decider_') || id.startsWith('classifier'),
    catalog: store => (store.classifierCatalog && typeof store.classifierCatalog === 'object' ? store.classifierCatalog : EMPTY),
    disabledReason: (id, catalog) => (own(catalog.disabled, id) ? why(catalog.disabled[id], 'cannot classify audio') : null),
    unknown: catalog => why(catalog.unknown || 'Unknown provider: Convo cannot classify speech with it.', 'unknown provider'),
    compatible: catalog => ({ short: 'not verified', kinds: ['chat'], group: 'host',
                              reason: catalog.unknown_compatible || 'Agent Zero lists it as OpenAI-compatible, so it can be a chat classifier; Check server confirms it.' }),
    endpointOnly: { label: 'Endpoint as entered (earlier setup)', name: 'Endpoint as entered' },
    // The earlier endpoint is offered only while the earlier HTTP classifier is what is in effect, never over a saved decider.
    offerEndpointOnly: (store, selected, saved) => (!selected || !saved) && !legacyDecider(store.config),
    context: { path: 'roles.classifier.context_window', min: 1024, max: 1000000, message: 'Enter a context window from 1024 to 1000000 tokens.' },
    sectionKey: 'convo_classifier_model',
    providerPaths: ['roles.classifier.kind'],
    role: store => store.classifierView(),
    beforeEdit: store => store.classifierAdopt(),
    // A type the new provider does not serve goes back to Auto (the provider's own first kind).
    afterProvider: (store, next) => {
        const draft = store.config.roles.classifier, kinds = store.classifierKinds(next);
        if (draft.kind && draft.kind !== 'auto' && !kinds.includes(draft.kind)) draft.kind = 'auto';
    },
});

export const classifierSetup = {
    ...factory,
    // ---- What the card shows. A saved separate decider from before this card reads as llama.cpp, a decision model, at its
    // address + /v1; that is a view only: the draft changes when one of those fields is edited (classifierAdopt), never by itself.
    classifierMigration() {
        const config = this.config, draft = config?.roles?.classifier;
        if (!draft || (draft.provider || '') || !legacyDecider(config)) return null;
        const url = String(config.decider?.url || '').trim().replace(/\/+$/, '') || DECIDER_DEFAULT_URL;
        return { provider: 'llama_cpp', kind: 'decision', api_base: url + '/v1', model: String(config.decider?.model || '') };
    },
    classifierView() {
        const draft = this.config?.roles?.classifier || {}, migration = this.classifierMigration();
        return migration ? { ...draft, ...migration } : draft;
    },
    classifierAdopt() {
        const migration = this.classifierMigration(), draft = this.config?.roles?.classifier;
        if (!migration || !draft) return false;
        for (const field of MIGRATED) draft[field] = migration[field];
        return true;
    },
    // One field edited in Convo's modal (saved with Save): a migrated view becomes the draft first.
    classifierSet(field, value) {
        if (!this.config?.roles?.classifier || !MIGRATED.includes(field) || field === 'provider') return;
        this.classifierAdopt();
        if (field === 'kind') { this.classifierSetKind(value); return; }
        this.config.roles.classifier[field] = typeof value === 'string' ? value : '';
    },
    classifierCatalogMissing() { return !(this.classifierCatalog && typeof this.classifierCatalog === 'object'); },
    // The select: the factory's options in Agent Zero's order of groups (Local servers, Cloud, Other providers, Other).
    classifierProviderGroups() {
        const options = this.classifierProviderOptions();
        return GROUPS.map(([id, label]) => ({ id, label, items: options.filter(option => (GROUP_OF[option.group] || 'host') === id) }))
            .filter(group => group.items.length);
    },
    // Which kinds a provider serves: its catalog kinds, chat for an OpenAI-compatible one, chat for the earlier endpoint ('').
    classifierKinds(id = this.classifierView().provider || '') {
        if (!id) return ['chat'];
        const kinds = this.classifierProvider(id)?.kinds;
        return Array.isArray(kinds) && kinds.length ? kinds.filter(kind => kind === 'decision' || kind === 'chat') : ['chat'];
    },
    // The kind in effect: the chosen one, or for Auto the provider's first kind (the server's rule).
    classifierEffectiveKind() {
        const kind = this.classifierView().kind || 'auto';
        return kind === 'auto' ? this.classifierKinds()[0] : kind;
    },
    classifierKindOptions() {
        const kinds = this.classifierKinds(), name = this.classifierProviderInfo().label;
        return ['auto', 'decision', 'chat'].map(id => ({ id, disabled: id !== 'auto' && !kinds.includes(id),
            label: id === 'auto' ? `Auto (${KIND_LABELS[kinds[0]].split(' (')[0].toLowerCase()})` : KIND_LABELS[id] + (id !== 'auto' && !kinds.includes(id) ? ` — not served by ${name}` : '') }));
    },
    classifierSetKind(next) {
        const draft = this.config?.roles?.classifier;
        if (!draft) return '';
        this.classifierAdopt();
        if (['auto', 'decision', 'chat'].includes(next) && (next === 'auto' || this.classifierKinds().includes(next))) draft.kind = next;
        return draft.kind || 'auto';
    },
    // The address advice shown under the API base; the server decides again when the settings are saved.
    classifierBaseProblem() {
        const view = this.classifierView(), info = this.classifierProviderInfo();
        if (view.provider && info.selectable && !info.url) return `Enter the API base URL: ${info.label} has no default address.`;
        return addressProblem(this.classifierUrl(), view.remote === true);
    },

    classifierContextProblem() {
        const value = this.config?.roles?.classifier?.context_window;
        return Number.isInteger(value) && value >= 1024 && value <= 1000000 ? '' : 'Enter a context window from 1024 to 1000000 tokens.';
    },

    // ---- "Use System One's decider".
    classifierUsesSystemOne() { return this.config?.decider?.source === 'system_one'; },
    // Offered when System One is installed; a saved choice of it stays visible (with its verdict) after it is not.
    classifierInheritAvailable() { return !!this.decider.installed || this.classifierUsesSystemOne(); },
    classifierInheritText() {
        if (!this.classifierInheritAvailable()) return 'System One is not installed in this Agent Zero, so the classifier above is used.';
        return 'Off: the classifier above. On: the decider chosen in System One (Agent settings → Models → Decider Model), shown read-only above; your own settings are kept for when you turn this off.';
    },
    // On: decider.source 'system_one'. Off: the source saved before, unless that was System One ('convo' then). Nothing else is
    // written: the server's normalizer selects the decision backend.
    classifierSetSystemOne(on) {
        if (!this.config) return false;
        if (on && !this.classifierInheritAvailable()) return false;
        if (!this.config.decider || typeof this.config.decider !== 'object') this.config.decider = { source: '', kind: CLASSIFIER_KIND, url: '', model: '' };
        const saved = this.baseline ? (JSON.parse(this.baseline).decider?.source || '') : '';
        this.config.decider.source = on ? 'system_one' : (saved === 'system_one' ? 'convo' : saved);
        Object.assign(this.ui.classifier, { check: null, checking: false });
        return this.classifierUsesSystemOne();
    },
    // The server's verdict on System One's decider for a voice conversation: its words, plus an icon so the state never rests on
    // colour. An older server that sends none is never shown as usable.
    classifierVerdict() {
        const audio = this.decider?.audio, state = audio && own(VERDICT_ICONS, audio.state) ? audio.state : 'unknown';
        return { state, ok: state === 'audio', icon: VERDICT_ICONS[state],
                 label: audio?.label || 'Not described by this server — turn this off and choose a classifier above', detail: audio?.detail || '' };
    },
    // System One's own decider, read-only (decider_reference: names and a host only, never a key or an endpoint path).
    classifierReference() {
        const d = this.decider || {};
        return { provider: d.provider || 'Not configured', model: d.model || 'Not configured', host: d.host || 'Hosted provider or not set',
                 context: Number.isInteger(d.context_window) ? String(d.context_window) : 'Not set' };
    },
    // Agent Zero's settings at Agent Settings → Models, where System One's Decider Model is set. The host's open() resolves only
    // when that modal closes, so it is never awaited here.
    async classifierOpenSystemOne() {
        try {
            const { store } = await import('/components/settings/settings-store.js');
            store.activateSection?.('section-models-summary', { persist: false });
            void Promise.resolve(store.open?.('agent')).catch(() => {});
            setTimeout(() => { try { store.scrollToSection?.('section-models-summary'); } catch { /* the modal shows the tab anyway */ } }, 400);
            return true;
        } catch (error) { this.fail(error); return false; }
    },

    // ---- Check server: on request only, what the card shows now (none of it saved) is sent to classifier_check, which lists the
    // models and, for a decision server, sends one tiny text decision. An address the server would refuse is refused here and
    // nothing is sent; the answer is shown inline (never a toast), and one for a setup edited meanwhile is dropped.
    // The earlier endpoint (provider '') is checked as what it is: an OpenAI-compatible chat server at that address.
    classifierCheckRequest() {
        const view = this.classifierView(), provider = view.provider || '';
        if (!provider) return { provider: 'other', api_base: (view.url || '').trim(), model: (view.model || '').trim(), kind: 'chat' };
        return { provider, api_base: (view.api_base || '').trim(), model: (view.model || '').trim(), kind: view.kind || 'auto' };
    },
    // Check server goes by the SAVED remote-processing consent (the server reads it), so a consent still unsaved is said here.
    classifierCheckProblem() {
        const info = this.classifierProviderInfo(), url = this.classifierUrl();
        if (!info.selectable) return `${info.label} cannot be used: ${info.reason}`;
        if (!url) return this.classifierView().provider ? this.classifierBaseProblem() : 'Enter the server URL first.';
        const shown = addressProblem(url, this.classifierView().remote === true);
        if (shown) return 'Refused: ' + shown;
        if (this.classifierSaved().remote !== true && addressProblem(url, false)) return 'Save first: Check server uses the saved remote-processing consent.';
        return '';
    },
    async classifierCheck() {
        const ui = this.ui.classifier;
        if (!this.config || ui.checking) return false;
        const request = this.classifierCheckRequest(), problem = this.classifierCheckProblem();
        if (problem) { Object.assign(ui, { check: { state: 'refused', message: problem } }); return false; }
        Object.assign(ui, { checking: true, check: null });
        let check;
        try {
            const result = await this.api('classifier_check', request);
            check = { state: own(CHECK_STATES, result.state) ? result.state : 'unverified', message: result.message || '', models: result.models || [], host: result.host || '' };
        } catch (error) { check = { state: 'refused', message: error?.message || String(error) }; }
        const stale = JSON.stringify(this.classifierCheckRequest()) !== JSON.stringify(request) || this.classifierUsesSystemOne();
        Object.assign(this.ui.classifier, { checking: false, check: stale ? null : check });
        return !stale && (check.state === 'decision' || check.state === 'chat');
    },
    classifierChecking() { return this.ui?.classifier?.checking === true; },
    classifierCheckView() {
        const ui = this.ui?.classifier || {};
        if (ui.checking) return { state: 'busy', icon: 'progress_activity', label: 'Checking: ', message: 'asking the server for its models…' };
        if (!ui.check) return { state: 'none', icon: 'help', label: 'Not checked in this view. ', message: 'Check server lists the models and, for a decision server, sends one short text decision; no audio is sent.' };
        const [icon, label] = CHECK_STATES[ui.check.state] || CHECK_STATES.unverified;
        return { state: ui.check.state, icon, label: label + ': ', message: ui.check.message || '' };
    },
    // A check that found the other kind of model than the one in effect offers to use it.
    classifierCheckSuggests() {
        const state = this.ui?.classifier?.check?.state;
        return (state === 'decision' || state === 'chat') && state !== this.classifierEffectiveKind() && this.classifierKinds().includes(state) ? state : '';
    },
    classifierUseCheckedKind() { const kind = this.classifierCheckSuggests(); return kind ? this.classifierSetKind(kind) : ''; },
    classifierStateText(state) { return own(STATE_WORDS, state) ? STATE_WORDS[state] : String(state || ''); },

    // ---- Developer tuning (tuning-controls.html): the registered decision backends, with a saved choice that is not registered
    // kept visible.
    decisionOptions() {
        const values = [...(this.decisionBackends || [])];
        const selected = this.config?.system_one?.backend;
        if (selected && !values.some(value => value.id === selected))
            values.unshift({ id: selected, label: `${selected} (saved; not registered)` });
        return values;
    },
};
