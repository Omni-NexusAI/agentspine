import { SPEECH_CONTROLS, SPEECH_GROUPS, SPEECH_PROVIDERS, TTS_BACKENDS, adoptProvider, choiceLabel, connectionFields, effectiveProvider, instructionsSupported,
         isLocalUrl, legacyAudiocppOffer, normalizeTts, parkedInstructions, perConnection, providerForBackend, switchProvider } from '/plugins/_convo/webui/speech-catalog.js';
import { NETWORK_HINT, credentialReferenceProblem } from '/plugins/_convo/webui/connection-rules.js';

const STATUS = { ready: 'Verified', unverified: 'Not verified', unavailable: 'Unavailable', error: 'Error', checking: 'Checking…' };
const MANAGED_OFF = 'Convo’s managed runtime is off. Turn on “Manage runtime” below and save to use it; switching it on never downloads or loads a model.';

// The speech output card (speech-output-field.html in Connections, speech-voice-field.html in Voice & Audio): a provider editor
// in the style of Agent Zero's model settings. Methods only (see compose in setup-store.js); `this` is the shared settings store,
// transient view state goes in this.ui.speech. Provider switching, normalization and the catalog live in speech-catalog.js; every
// change to roles.tts made here goes through them, so the draft holds what the server would store.
export const speechSetup = {
    // ---- the server's backend descriptors and the earlier entry points
    provider(id) { return this.providers.find(item => item.id === id); },
    backendOptions() { return Object.entries(TTS_BACKENDS).map(([id, spec]) => ({ id, label: this.provider(id)?.label || spec.label, available: this.provider(id)?.available ?? true })); },
    // "af_bella (60%) + am_adam (40%)" from the native Kokoro summary.
    voiceMix(voices = []) { return voices.filter(voice => voice.id).map(voice => voice.share >= .995 ? voice.id : `${voice.id} (${Math.round(voice.share * 100)}%)`).join(' + ') || 'Not set'; },
    // The earlier backend switch now chooses the provider that backend means; returns the backend in effect.
    setBackend(next) {
        const target = providerForBackend(next);
        if (target) this.setSpeechProvider(...target);
        return this.config.roles.tts.backend;
    },
    async useVoice(id) {
        await this.load(); if (!this.config) return false;
        if (this.dirty) { this.fail(new Error('Save or reload pending settings before selecting a Studio voice.')); return false; }
        this.speechUpdate({ voice: id }); return this.save();
    },

    // ---- the provider in effect
    speechChoice() { return effectiveProvider(this.config?.roles?.tts || {}); },
    speechProvider() { return this.speechChoice()[0]; },
    // The provider an older configuration's stored backend means (the select shows it until a provider is chosen, and choosing
    // one writes the backend: the provider select is the backend's control now).
    speechDerived(backend) { return (providerForBackend(backend || 'openai') || providerForBackend('openai'))[0]; },
    speechConnection() { return this.speechChoice()[1]; },
    speechSpec() { return SPEECH_PROVIDERS[this.speechProvider()]; },
    speechVerify() { const [provider, connection] = this.speechChoice(); return perConnection(SPEECH_PROVIDERS[provider].verify, connection); },
    // Whether a provider (and connection) can speak now, and why not. Honest, from the server's descriptors and the draft.
    speechAvailable(id = this.speechProvider(), connection = id === this.speechProvider() ? this.speechConnection() : '') {
        if (id === 'kokoro_native') {
            const native = this.provider('host_kokoro');
            return native?.available === false ? { available: false, reason: native.reason || 'Native Kokoro is off or missing in this host.' } : { available: true, reason: '' };
        }
        if (id === 'audiocpp' && connection === 'managed' && !this.config?.sidecar?.managed) return { available: false, reason: MANAGED_OFF };
        return { available: true, reason: '' };
    },
    speechReason() { return this.speechAvailable().reason; },
    // A credential environment reference must name a variable (connection-rules.js; the server refuses anything else too).
    speechReferenceProblem() { return credentialReferenceProblem((this.config?.roles?.tts?.token_env || '').trim()); },
    speechClearReference() { if (this.config?.roles?.tts) this.config.roles.tts.token_env = ''; },
    // The provider select, grouped like Agent Zero's providers. Speech providers the host registered only in the browser are
    // listed, never selectable: Convo speaks from the server.
    speechGroups() {
        const groups = SPEECH_GROUPS.map(group => ({ ...group, items: [] }));
        for (const [id, spec] of Object.entries(SPEECH_PROVIDERS)) {
            const off = id === 'kokoro_native' && !this.speechAvailable(id).available;
            groups.find(group => group.id === spec.group).items.push({ id, label: spec.label + (off ? ' (not available now)' : ''), disabled: false });
        }
        for (const id of this.ui.speech?.browser || [])
            groups[0].items.push({ id: 'browser:' + id, label: `${id.replace(/^_+/, '').replace(/_/g, ' ')} — browser-only; Convo speaks from the server`, disabled: true });
        return groups.filter(group => group.items.length);
    },
    async speechRefreshRegistry() {
        let found = [];
        try {
            const { ttsService } = await import('/js/tts-service.js');
            found = [...(ttsService?.providers?.keys?.() || [])].map(String).filter(id => id && id !== '_kokoro_tts');
        } catch { /* a host without the browser speech registry: nothing more to list */ }
        this.ui.speech = { ...this.ui.speech, browser: found };
    },
    // Choosing a provider stashes what the current one has and brings back the chosen one's own fields. Choosing audio.cpp while
    // an older configuration points at its known port is confirming that offer (the address, model and voice are kept). Returns
    // the provider in effect, so the select can be put back when nothing changed.
    setSpeechProvider(id, connection = '') {
        try {
            const tts = this.config.roles.tts;
            const next = id === 'audiocpp' && !connection && legacyAudiocppOffer(tts) ? adoptProvider(tts, 'audiocpp', 'external') : switchProvider(tts, id, connection);
            if (next !== tts) { this.config.roles.tts = next; this.speechReset(); }
        } catch (error) { this.fail(error); }
        return this.speechProvider();
    },
    setSpeechConnection(connection) {
        try {
            const tts = this.config.roles.tts, [provider] = effectiveProvider(tts);
            const next = SPEECH_PROVIDERS[provider].connections.includes(connection) ? switchProvider(tts, provider, connection) : tts;
            if (next !== tts) { this.config.roles.tts = next; this.speechReset(); }
        } catch (error) { this.fail(error); }
        return this.speechConnection();
    },
    // Discovery belongs to the provider that was asked.
    speechReset() { this.choices.tts = {}; this.searches.tts = ''; this.ui.speech = { ...this.ui.speech, open: '', active: -1 }; },
    speechOffer() { return legacyAudiocppOffer(this.config?.roles?.tts); },
    speechAcceptOffer() { this.speechAdopt('audiocpp', 'external'); },
    speechDeclineOffer() { this.speechAdopt('openai_compatible', ''); },
    speechAdopt(provider, connection) {
        try { this.config.roles.tts = adoptProvider(this.config.roles.tts, provider, connection); } catch (error) { this.fail(error); }
    },
    // Set fields and derive the rest as the server will (backend, instruction_control, waiting delivery instructions).
    speechUpdate(changes) {
        try { this.config.roles.tts = normalizeTts({ ...this.config.roles.tts, ...changes }); } catch (error) { this.fail(error); }
    },
    // A Test answer's one-click address (the port Docker publishes, or this computer's own address), then the test again.
    speechUseAddress(url) { if (typeof url !== 'string') return null; this.speechUpdate({ url }); return this.discover('tts', true); },
    speechKeyMoveOffer() { return this.keyMoveOffer('tts', this.config?.roles?.tts?.url || '', this.speechUsesConvoKey()); },
    speechSetDeclared(on) { this.speechUpdate({ accepts_instructions: on === true }); },

    // ---- which controls show (the markup asks show('tts', name)); hidden values are kept, never cleared
    speechRemoteNeeded() {
        const tts = this.config?.roles?.tts || {};
        return this.speechSpec().group === 'cloud' || tts.remote === true || (!!tts.url && !isLocalUrl(tts.url));
    },
    speechControls() {
        const tts = this.config?.roles?.tts || {}, [provider, connection] = effectiveProvider(tts);
        const spec = SPEECH_PROVIDERS[provider], verify = perConnection(spec.verify, connection), fields = connectionFields(provider, connection);
        const fixed = Object.keys(spec.models).length > 0, external = verify !== 'native' && verify !== 'managed';
        const on = {
            native: verify === 'native', connection: spec.connections.length > 0, runtime: verify === 'managed',
            apibase: external && fixed, url: external && !fixed, key: external, keysource: !!spec.host_key, remote: external && this.speechRemoteNeeded(),
            discover: verify !== 'native' && !fixed, test: verify !== 'native', model: verify !== 'native', voice: verify !== 'native',
            tuning: verify === 'managed', server: verify === 'audiocpp', pcm: fields.includes('native_pcm'), studio: verify === 'managed',
            declare: fields.includes('accepts_instructions'), instructions: instructionsSupported(tts, provider, connection), advanced: external,
        };
        return Object.fromEntries(SPEECH_CONTROLS.map(name => [name, !!on[name]]));
    },
    speechUrlPlaceholder() { return this.speechSpec().default_url || 'http://host.docker.internal:8000/v1'; },
    speechUrlHint() {
        const [provider] = this.speechChoice();
        const text = { audiocpp: 'Your own audio.cpp server (the default is your container on port 8890).',
                 llamacpp: 'llama.cpp’s usual address; edit it if your server listens elsewhere.',
                 unsloth_studio: 'Unsloth Studio has no documented default address: enter your server’s URL.' }[provider]
            || 'Any server that speaks the OpenAI speech protocol (POST /audio/speech).';
        return this.speechSpec().group === 'cloud' ? text : text + ' ' + NETWORK_HINT;
    },
    speechRemoteLabel() {
        const spec = this.speechSpec();
        return spec.group === 'cloud' ? `Send Convo’s replies to ${spec.label} to be spoken (remote provider consent)` : 'Remote provider — explicitly permitted';
    },
    // Agent Zero's key for the provider (shared with its models), or Convo's own write-only key bound to this address.
    speechUsesConvoKey() { return !this.speechSpec().host_key || this.config?.roles?.tts?.key_source === 'convo'; },
    speechKeyHint() {
        const spec = this.speechSpec();
        if (spec.host_key) return `${spec.label} needs an API key. Use Agent Zero’s, shared with every Agent Zero model that uses ${spec.label}, or a separate key for Convo only.`;
        return 'Only if your server asks for one. Convo keeps it write-only, bound to this address.';
    },
    speechKeyText() {
        const spec = this.speechSpec();
        if (!spec.host_key || this.config?.roles?.tts?.key_source === 'convo') return '';
        const status = globalThis.Alpine?.store?.('modelConfig')?.apiKeyStatus?.[spec.host_key];
        const known = status === true ? ` Agent Zero has an ${spec.label} key.` : status === false ? ` Agent Zero has no ${spec.label} key yet: add it under Agent Zero’s API keys.` : '';
        return `Uses Agent Zero’s ${spec.label} API key, shared with every Agent Zero model that uses ${spec.label}.${known} If Agent Zero has none, a key saved for Convo at this address is used.`;
    },
    speechCanOpenKeys() { return typeof globalThis.Alpine?.store?.('modelConfig')?.openApiKeysFromSummary === 'function'; },
    async speechOpenKeys() {
        try { await globalThis.Alpine.store('modelConfig').openApiKeysFromSummary(); } catch (error) { this.fail(error); }
    },

    // ---- models and voices: a fixed list (OpenAI) or what the server lists; a typed id is always allowed where lists are discovered
    speechFixed() { return Object.keys(this.speechSpec().models).length > 0; },
    speechOptions(kind) {
        const tts = this.config?.roles?.tts || {}, spec = this.speechSpec();
        if (!Object.keys(spec.models).length) return this.options('tts', kind);
        const ids = kind === 'voices' ? (spec.models[tts.model] || []) : Object.keys(spec.models);
        const selected = tts[kind === 'voices' ? 'voice' : 'model'];
        const list = ids.map(id => ({ id, label: id }));
        if (selected && !ids.includes(selected))
            list.unshift({ id: selected, label: `${selected} (not offered${kind === 'voices' && tts.model ? ' by ' + tts.model : ''})`, unavailable: true });
        return list;
    },
    // The search list under the model/voice field: what matches the typed text first, then the rest (as Agent Zero's model search).
    speechComboItems(kind) {
        const tts = this.config?.roles?.tts || {}, spec = this.speechSpec();
        const base = Object.keys(spec.models).length ? (kind === 'voices' ? (spec.models[tts.model] || []) : Object.keys(spec.models)).map(id => ({ id, label: id }))
                                                     : [...(this.choices.tts?.[kind] || [])];
        const query = String(tts[kind === 'voices' ? 'voice' : 'model'] || '').trim().toLowerCase();
        if (!query) return base;
        const match = item => `${item.id} ${item.label || ''}`.toLowerCase().includes(query);
        return [...base.filter(match), ...base.filter(item => !match(item))];
    },
    speechComboIsOpen(kind) { return this.ui.speech?.open === kind; },
    speechComboActive(kind) { return this.speechComboIsOpen(kind) ? (this.ui.speech.active ?? -1) : -1; },
    speechComboOpen(kind) { this.ui.speech = { ...this.ui.speech, open: kind, active: this.speechComboItems(kind).length ? 0 : -1 }; },
    speechComboClose() { if (this.ui.speech?.open) this.ui.speech = { ...this.ui.speech, open: '', active: -1 }; },
    // The search button: opens the list, and the first time (nothing listed yet) reads the server once. A click, read-only.
    speechComboToggle(kind) {
        if (this.speechComboIsOpen(kind)) return this.speechComboClose();
        if (!this.speechFixed() && !(this.choices.tts?.[kind] || []).length && !this.isDiscovering('tts') && this.speechBlocked() === '') void this.discover('tts');
        this.speechComboOpen(kind);
    },
    // Arrow keys move, Enter picks, Escape closes the list only (the settings modal stays open), Tab leaves.
    speechComboKey(kind, event) {
        const open = this.speechComboIsOpen(kind), items = this.speechComboItems(kind);
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            if (!open) return this.speechComboOpen(kind);
            if (items.length) this.ui.speech = { ...this.ui.speech, active: (this.speechComboActive(kind) + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length };
        } else if (event.key === 'Enter' && open) {
            event.preventDefault();
            const item = items[this.speechComboActive(kind)];
            if (item) this.speechComboPick(kind, item.id); else this.speechComboClose();
        } else if (event.key === 'Escape' && open) {
            event.preventDefault(); event.stopPropagation();
            this.speechComboClose();
        } else if (event.key === 'Tab') this.speechComboClose();
    },
    speechComboPick(kind, id) { this.speechUpdate({ [kind === 'voices' ? 'voice' : 'model']: id }); this.speechComboClose(); },
    speechComboEmpty(kind) {
        if (this.isDiscovering('tts')) return 'Reading what the server lists…';
        return this.choices.tts?.verification ? `The server listed no ${kind}. Type the id instead.` : `Nothing listed yet: use Find models & voices, or type the id.`;
    },
    // The friendly name of the chosen voice when the server listed one ("J.A.R.V.I.S" for clone:2098c329bc9d), else ''.
    speechVoiceLabel() {
        const voice = this.config?.roles?.tts?.voice;
        const found = voice ? (this.choices.tts?.voices || []).find(item => item.id === voice) : null;
        return found?.label && found.label !== voice ? found.label : '';
    },

    // ---- delivery instructions: shown only where honoured; the reason when hidden
    speechInstructionsNote() {
        const tts = this.config?.roles?.tts || {}, [provider, connection] = effectiveProvider(tts);
        if (instructionsSupported(tts, provider, connection)) return '';
        const rule = perConnection(SPEECH_PROVIDERS[provider].instructions, connection);
        const why = Array.isArray(rule) ? `Voice delivery works only with ${rule.join(', ')}.`
            : rule === 'declared' && tts.accepts_instructions === true ? 'Voice delivery is not available with a Base clone model or a clone voice: they copy the reference voice and ignore delivery instructions.'
            : '';
        return why && parkedInstructions(tts, provider, connection) ? `${why} Your text is kept and used again when it applies.` : why;
    },

    // ---- checks: read-only, explicit; what the server answered
    speechBlocked() {
        const tts = this.config?.roles?.tts || {}, [provider] = this.speechChoice(), spec = SPEECH_PROVIDERS[provider], verify = this.speechVerify();
        if (verify === 'native') return 'Kokoro has nothing to check here: its voice is set in Agent Zero’s speech settings.';
        if (verify === 'managed') return '';
        if (!String(tts.url || '').trim())
            return provider === 'unsloth_studio' ? 'Enter your Unsloth Studio server’s URL first: it has no documented default address.' : 'Enter the server URL first.';
        if (this.speechRemoteNeeded() && tts.remote !== true)
            return spec.group === 'cloud' ? `Allow sending Convo’s replies to ${spec.label} first (remote provider consent).` : 'This address is not on this machine or its network: allow the remote provider first (consent).';
        return '';
    },
    speechStatus() {
        const choice = this.choices.tts || {};
        if (choice.busy) return { state: 'checking', label: STATUS.checking, message: 'Reading what your server lists (read-only)…' };
        if (choice.verification?.state) return { state: choice.verification.state, label: STATUS[choice.verification.state] || choice.verification.state, message: choice.verification.message || '' };
        const [provider] = this.speechChoice(), spec = SPEECH_PROVIDERS[provider], verify = this.speechVerify();
        if (verify === 'native') return { state: 'native', label: '', message: '' };
        return { state: 'unverified', label: STATUS.unverified, message: verify === 'speech_route' && provider !== 'openai_compatible'
            ? `${spec.label} has no documented OpenAI-style speech route: it stays not verified until Test connection shows your server answering on one. Nothing is generated.`
            : 'Not verified in this view. Test connection reads what your server lists; it never generates speech.' };
    },
    speechChecks() { return (this.choices.tts?.checks || []).map(check => `GET ${check.path} → ${check.status ? 'HTTP ' + check.status : 'no answer'} · ${check.answer}`); },
    speechServer() { return this.choices.tts?.server || null; },
    speechServerModel() {
        const server = this.speechServer();
        return server ? `${server.model || 'none'} (${server.loaded ? 'loaded' : server.state || 'not loaded'})` : '';
    },
    speechTuningText() {
        const tuning = this.speechServer()?.tuning;
        return tuning ? `Tuning: ${tuning.label} — your server’s current selection. Change it in your server’s own Voice Studio.`
                      : 'Tuning is your server’s own selection; Test connection shows which one it uses.';
    },
    // Use the model the server already has loaded: a draft edit only, nothing is sent to the server.
    speechUseServerModel() { const model = this.speechServer()?.model; if (model) this.speechUpdate({ model }); },
    speechSummary() {
        const tts = this.config?.roles?.tts || {}, [provider, connection] = this.speechChoice(), label = this.speechVoiceLabel();
        return { provider: choiceLabel(provider, connection), model: tts.model || 'Not chosen', voice: label ? `${label} (${tts.voice})` : (tts.voice || 'Not chosen') };
    },
};
