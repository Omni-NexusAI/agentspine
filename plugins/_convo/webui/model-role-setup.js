import { getPath, setPath, plain } from './defaults.js';
import { NETWORK_HINT, credentialReferenceProblem, isLocalHost } from './connection-rules.js';

// One model-role card: provider, model name with search, API key, context window, remote consent and the advanced checks, laid
// out like Agent Zero's model fields. The conversation card (conversation-setup.js) and the classifier card (classifier-setup.js)
// are this factory plus a spec. `modelRoleSetup(role, spec)` returns methods named `<role><Name>` (compose in setup-store.js
// refuses a name defined twice); `this` is the shared settings store and transient view state goes in this.ui[role].
// Agent Zero's provider list, provider details, model search, result grouping, provider-change hook and key store are read from
// its `modelConfig` store (/plugins/_model_config/webui/model-config-store.js) at the moment of use, never copied, so whatever
// another plugin (Provider Profiles) adds or patches there reaches both cards. Keys never pass through Convo's settings.

export const AUTOSAVE_MS = 350;
export const FOUND_MS = 60000;                                  // the models a server listed are reused this long while typing
const VERDICT = { ready: ['check_circle', 'Ready'], unverified: ['help', 'Answered'], unavailable: ['error', 'Not reached'], error: ['error', 'Not reached'] };
const COMPATIBLE_ROUTES = new Set(['openai', 'hosted_vllm']);   // LiteLLM routes that speak the OpenAI API at an API base
const PROVIDER_ID = /^[a-z0-9_]{1,64}$/;                         // convo_contract.PROVIDER_ID

// Private to this module, never in reactive state: Agent Zero's store (one for every card) and, per role, the debounce timer and
// the autosave in flight.
let host = null, hostLoading = null;
const timers = {}, flushing = {};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// convo_contract.endpoint's locality rule (connection-rules.js), for advice only: the server decides.
export { credentialReferenceProblem, isLocalHost };

// Why the server would refuse this address ('' when it would not): a complete http(s) URL without credentials, query or
// fragment; somewhere else than this machine or its network only with remote consent, and then over https.
export function addressProblem(url, remote) {
    if (!url) return '';
    let parsed;
    try { parsed = new URL(url); } catch { return 'Enter a complete http(s) address, for example http://host.docker.internal:8080/v1.'; }
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password || parsed.search || parsed.hash)
        return 'Enter a complete http(s) address without a user name, query or fragment.';
    if (isLocalHost(parsed.hostname)) return '';
    if (!remote) return `Sending your speech to ${parsed.hostname} needs remote-processing consent: turn on “Allow remote processing” to save this.`;
    if (parsed.protocol !== 'https:') return `A remote address must use https (${parsed.hostname}).`;
    return '';
}

export function hostnameOf(url) {
    try { return new URL(url).hostname; } catch { return ''; }
}

// Agent Zero's grouping of model search results (names containing what was typed first), used only while its store is not loaded.
export function groupModels(models, query) {
    const q = String(query || '').trim().toLowerCase(), list = Array.isArray(models) ? models.filter(m => typeof m === 'string') : [];
    if (!q) return { matched: [], rest: list };
    return { matched: list.filter(m => m.toLowerCase().includes(q)), rest: list.filter(m => !m.toLowerCase().includes(q)) };
}

// One provider's entry in Agent Zero's `chatProviderDetails` (a list of entries naming their provider, or a table by id).
export function hostDetail(details, id) {
    if (!id || !details || typeof details !== 'object') return null;
    if (Array.isArray(details)) return details.find(item => item && typeof item === 'object' && [item.value, item.id, item.provider].includes(id)) || null;
    const entry = Object.prototype.hasOwnProperty.call(details, id) ? details[id] : null;
    return entry && typeof entry === 'object' ? entry : null;
}

// The OpenAI-compatible base Agent Zero's details give a provider Convo has no entry for, or '' unless they say it is one: its
// LiteLLM route is openai or hosted_vllm and it has an API base.
export function compatibleBase(detail) {
    if (!detail) return '';
    const route = String(detail.litellm_provider || '').toLowerCase();
    const base = [detail.api_base, detail.kwargs?.api_base].find(value => typeof value === 'string' && value.trim());
    return COMPATIBLE_ROUTES.has(route) && base ? base.trim().replace(/\/+$/, '') : '';
}

// The address a model role is reached at, as the server's catalog normalizer derives it: the URL exactly as entered for the
// endpoint-only setup (provider ''), else the API base override or the provider's own base; '' for a provider that cannot be used
// (`provider` is the usable provider's description, or null).
export function roleUrl(role, provider) {
    role = role || {};
    if (!(role.provider || '')) return role.url || '';
    if (!provider) return '';
    const override = (role.api_base || '').trim();
    return override ? override.replace(/\/+$/, '') : (provider.base || '');
}

// spec: {
//   paths: {settings path: label}      every setting the card edits (dirty, conflicts);
//   trimmed, address: [path]           saved trimmed / held together while the address would be refused;
//   rows(id) -> bool                   the readiness rows the card shows;
//   catalog(store) -> {providers: {id: {label, base, host_key, group?, ...}}, disabled: {id: ...}}   Convo's verdicts;
//   disabledReason(id, catalog) -> {short, reason} | null;  unknown(catalog), compatible(catalog) -> {short, reason, ...};
//   endpointOnly: {label, name} | null, offerEndpointOnly?(store, selected, saved) -> bool;
//   context: {path, min, max?, message}; sectionKey: the slot name Agent Zero's provider hook keeps Convo's memory under;
//   role?(store) -> the role as the card shows it (default: the draft); beforeEdit?(), afterProvider?(next): called with the store;
//   probe?: {action, busy}; keyCopy?: {action}                                                     }
export function modelRoleSetup(role, spec) {
    const n = name => role + name;
    const view = store => store.ui[role] || (store.ui[role] = {});
    const prefix = `roles.${role}.`;
    const methods = {
        // Card start: the shared settings, then Agent Zero's provider list and which providers hold a key (never a key itself).
        async Init() {
            await this.load();
            const store = await this[n('Host')]();
            try { await store?.refreshApiKeyStatus?.(); } catch { /* the key state stays unknown; the card still works */ }
        },
        async Host() {
            if (!host) {
                hostLoading ||= (async () => {
                    try {
                        const { store } = await import('/plugins/_model_config/webui/model-config-store.js');
                        await store.ensureLoaded();
                        host = store;
                    } catch { host = null; }
                    finally { hostLoading = null; }
                    return host;
                })();
                await hostLoading;
            }
            if (host && this.ui?.[role]?.hostReady !== true) view(this).hostReady = true;   // reactive: the card re-renders with the host's data
            return host;
        },
        HostStore() { return this.ui?.[role]?.hostReady ? host : null; },
        Role() { return spec.role ? spec.role(this) : (this.config?.roles?.[role] || {}); },
        Saved() { return this.baseline ? (JSON.parse(this.baseline).roles?.[role] || {}) : {}; },
        HostLabels() { return Object.fromEntries((this[n('HostStore')]()?.chatProviders || []).map(p => [p.value, p.label || p.value])); },
        HostDetails() { return this[n('HostStore')]()?.chatProviderDetails || null; },
        // What the card can do with a provider: its catalog entry, or a provider Convo has no entry for that Agent Zero describes as
        // OpenAI-compatible (selectable, never verified, Agent Zero's key for that provider id, as the server routes it); null for
        // one it cannot use (a known one with a reason, an unknown one, or an id the settings contract would refuse).
        Provider(id) {
            if (!id) return null;
            const catalog = spec.catalog(this), entry = catalog.providers?.[id];
            if (entry) return { ...entry, id, label: entry.label || id, base: entry.base || '', hostKey: entry.host_key || '', compatible: false };
            if (!PROVIDER_ID.test(id) || (catalog.disabled && Object.prototype.hasOwnProperty.call(catalog.disabled, id))) return null;
            const base = compatibleBase(hostDetail(this[n('HostDetails')](), id));
            return base ? { ...spec.compatible(catalog), id, label: this[n('HostLabels')]()[id] || id, base, hostKey: id, compatible: true } : null;
        },
        // The provider select: Convo's catalog first, then every other provider Agent Zero lists (an OpenAI-compatible one is
        // selectable and marked not verified, the others are disabled with their reason), and a saved choice that can no longer
        // be used stays visible. The endpoint-only setup ('') is offered only while it is the draft or the saved value.
        ProviderOptions() {
            const selected = this[n('Role')]().provider || '', saved = this[n('Saved')]().provider || '';
            const catalog = spec.catalog(this), providers = catalog.providers || {}, labels = this[n('HostLabels')]();
            const options = [];
            const offer = spec.offerEndpointOnly ? spec.offerEndpointOnly(this, selected, saved) : (!selected || !saved);
            if (spec.endpointOnly && offer) options.push({ id: '', label: spec.endpointOnly.label, name: spec.endpointOnly.name, group: 'endpoint', disabled: false, reason: '', note: '' });
            for (const [id, entry] of Object.entries(providers))
                options.push({ id, label: entry.label || id, name: entry.label || id, group: entry.group || '', disabled: false, reason: '', note: entry.note || '' });
            const others = [...new Set([...Object.keys(labels), ...Object.keys(catalog.disabled || {})])].filter(id => !(id in providers))
                .sort((a, b) => (labels[a] || a).localeCompare(labels[b] || b));
            if (selected && !(selected in providers) && !others.includes(selected)) others.push(selected);
            for (const id of others) {
                const name = labels[id] || id, usable = this[n('Provider')](id);
                if (usable) { options.push({ id, label: `${name} (${usable.short})`, name, group: 'host', disabled: false, reason: '', note: usable.reason, compatible: true }); continue; }
                const why = spec.disabledReason(id, catalog) || spec.unknown(catalog);
                options.push({ id, label: `${name} (${why.short})`, name, group: 'host', disabled: true, reason: why.reason, note: '' });
            }
            return options;
        },
        ProviderInfo() {
            const current = this[n('Role')](), id = current.provider || '', provider = this[n('Provider')](id);
            const option = this[n('ProviderOptions')]().find(item => item.id === id);
            return { id, label: provider?.label || option?.name || 'this endpoint', selectable: !option?.disabled, reason: option?.reason || '', note: option?.note || '',
                     base: provider?.base || '', hostKey: provider?.hostKey || '', compatible: !!provider?.compatible, url: roleUrl(current, provider) };
        },
        Url() { const current = this[n('Role')](); return roleUrl(current, this[n('Provider')](current.provider || '')); },
        // A server you run yourself (its own address is on this machine, like llama.cpp's): its address is a main field, not an
        // advanced override, so pointing it at another computer on your LAN or tailnet is one edit.
        LocalServer() { const base = this[n('ProviderInfo')]().base; return !!base && isLocalHost(hostnameOf(base)); },
        ServerHint() {
            const info = this[n('ProviderInfo')]();
            return (info.base ? `Where your ${info.label} server runs. Leave it empty for this machine (${info.base}). ` : '') + NETWORK_HINT;
        },
        // Choosing a provider. One that cannot be used is refused; the address follows the provider (the server derives the
        // endpoint): each provider's API base is remembered for this visit, an OpenAI-compatible provider Convo has no entry for
        // starts from Agent Zero's address for it, and Other starts from the endpoint used so far, so that address and the key
        // Convo saved for it carry over. Returns the provider in effect, so the select can be put back.
        SetProvider(next, autosave = false) {
            if (!this.config?.roles?.[role]) return '';
            spec.beforeEdit?.(this);
            const draft = this.config.roles[role], previous = draft.provider || '';
            if (next === previous) return previous;
            const provider = next ? this[n('Provider')](next) : null;
            if (next && !provider) return previous;
            const ui = view(this), bases = { ...(ui.bases || {}), [previous]: draft.api_base || '' };
            let base = bases[next] ?? (provider?.compatible ? provider.base : '');
            if (next === 'other' && !base && !previous) base = (draft.url || '').trim();
            draft.provider = next;
            draft.api_base = next ? base : '';
            ui.bases = bases;
            this.choices[role] = {}; this.searches[role] = '';                 // discovery belonged to the old provider
            spec.afterProvider?.(this, next);
            this[n('Edited')]([prefix + 'provider', prefix + 'api_base', ...(spec.providerPaths || [])], autosave);
            void this[n('ProviderHook')](previous, bases[previous], autosave);
            return next;
        },
        // Agent Zero's provider-change hook, when its store has one (Provider Profiles: address defaults, the model last used with
        // each provider). It gets this role as one of its model slots: `name` is Convo's model, `ctx_length` the context window,
        // the provider is read-only (it changes only through the select) and anything else it writes is dropped. What it changes
        // is marked like an edit. A hook that throws changes nothing more.
        async ProviderHook(previous = '', previousBase = '', autosave = false) {
            const store = this[n('HostStore')](), draft = this.config?.roles?.[role];
            const hook = ['onProviderChanged', 'onProviderProfileChanged'].find(name => typeof store?.[name] === 'function');
            if (!hook || !draft) return false;
            const self = this, slot = {};
            const text = (key, value) => { draft[key] = typeof value === 'string' ? value : ''; };
            Object.defineProperties(slot, {
                provider: { enumerable: true, get: () => draft.provider || '', set: () => {} },
                name: { enumerable: true, get: () => draft.model || '', set: value => text('model', value) },
                api_base: { enumerable: true, get: () => draft.api_base || '', set: value => text('api_base', value) },
                ctx_length: { enumerable: true, get: () => getPath(self.config, spec.context.path),
                              set: value => { if (Number.isInteger(value) && value > 0) setPath(self.config, spec.context.path, value); } },
            });
            const watched = [prefix + 'model', prefix + 'api_base', spec.context.path], before = watched.map(path => plain(getPath(this.config, path)));
            const snapshot = { provider: previous, name: draft.model || '', api_base: previousBase || '', ctx_length: getPath(this.config, spec.context.path) };
            try { await store[hook](spec.sectionKey, slot, previous, snapshot); } catch { return false; }
            const changed = watched.filter((path, index) => !same(getPath(this.config, path), before[index]));
            if (changed.length) this[n('Edited')](changed, autosave);
            return changed.length > 0;
        },

        // ---- Autosave (the preset-editor copy of the conversation card): a change marks its path; 350 ms after the last one, the
        // marked paths whose value differs from what this editor loaded are patched against that value. Unchanged paths are never
        // written, a field changed elsewhere waits for a choice, and a value the server would refuse waits with the reason.
        Edited(paths, autosave = false) {
            if (!autosave || !this.config) return;
            const ui = view(this);
            ui.edited = { ...(ui.edited || {}), ...Object.fromEntries([].concat(paths).map(path => [path, true])) };
            this[n('Schedule')]();
        },
        Schedule() {
            clearTimeout(timers[role]); timers[role] = null;
            const ui = view(this), { changes, held } = this[n('Pending')]();
            ui.held = held;
            if (!Object.keys(changes).length) {
                if (ui.status === 'pending') ui.status = 'idle';
                return;
            }
            ui.status = 'pending';
            timers[role] = setTimeout(() => { timers[role] = null; void this[n('Flush')](); }, AUTOSAVE_MS);
        },
        // The preset editor closed: what is still waiting is saved now; anything that could not be (held or refused) stays in the
        // draft as an ordinary unsaved edit for Convo's own Save, and this editor's marks end with it.
        async EditorClosed() {
            await this[n('Flush')]();
            Object.assign(view(this), { edited: {}, status: 'idle', held: '', error: '' });
        },
        AddressProblem() { return addressProblem(this[n('Url')](), !!this[n('Role')]().remote); },
        // A credential environment reference that is not a variable name (an address or a key typed into it) is held, not saved;
        // the server refuses it too and reports one saved earlier until it is cleared.
        ReferenceProblem() { return credentialReferenceProblem((this[n('Role')]().token_env || '').trim()); },
        ClearReference(autosave = false) {
            setPath(this.config, prefix + 'token_env', '');
            this[n('Edited')](prefix + 'token_env', autosave);
        },
        Held() {
            const paths = new Set(), reasons = [], problem = this[n('AddressProblem')](), reference = this[n('ReferenceProblem')]();
            if (problem) { spec.address.forEach(path => paths.add(path)); reasons.push(problem); }
            if (reference) { paths.add(prefix + 'token_env'); reasons.push(reference); }
            const { path, min, max, message } = spec.context, tokens = getPath(this.config, path);
            if (!Number.isInteger(tokens) || tokens < min || (max && tokens > max)) { paths.add(path); reasons.push(message); }
            return { paths: [...paths], reason: reasons.join(' ') };
        },
        // What the marked paths would send now. A mark whose value is back to what is saved is finished and dropped, so a later
        // edit made in Convo's own modal (which does not autosave) is never carried by an old mark.
        Pending() {
            const base = this.baseline ? JSON.parse(this.baseline) : {}, ui = this.ui?.[role];
            const conflicted = new Set((this.conflicts || []).map(item => item.path)), held = this[n('Held')]();
            const changes = {}, edited = { ...(ui?.edited || {}) };
            for (const path of Object.keys(edited)) {
                if (conflicted.has(path) || held.paths.includes(path)) continue;
                let value = getPath(this.config, path);
                if (spec.trimmed.includes(path) && typeof value === 'string') value = value.trim();
                if (!same(value, getPath(base, path))) changes[path] = value; else delete edited[path];
            }
            if (ui && Object.keys(edited).length !== Object.keys(ui.edited || {}).length) ui.edited = edited;
            return { changes, held: held.reason };
        },
        async Flush() {
            clearTimeout(timers[role]); timers[role] = null;
            while (flushing[role]) await flushing[role];
            if (!this.config) return null;
            const ui = view(this), { changes, held } = this[n('Pending')]();
            ui.held = held;
            if (!Object.keys(changes).length) { if (ui.status === 'pending') ui.status = 'idle'; return null; }
            const sent = plain(changes);
            const run = (async () => {
                ui.status = 'saving'; ui.error = '';
                const result = await this.patch(changes);
                if (!result) { ui.status = 'error'; ui.error = this.error; return null; }
                // A path saved with what was sent while it changed again meanwhile is this editor's own newer edit, not a
                // conflict: it stays marked and goes with the next save.
                const own = new Set((this.conflicts || []).filter(item => item.path in sent && same(item.theirs, sent[item.path])).map(item => item.path));
                if (own.size) this.conflicts = this.conflicts.filter(item => !own.has(item.path));
                ui.status = 'saved';
                this[n('Schedule')]();                                       // saved marks end here; a newer edit is sent next
                return result;
            })();
            flushing[role] = run;
            try { return await run; } finally { if (flushing[role] === run) flushing[role] = null; }
        },
        Dirty() {
            const base = this.baseline ? JSON.parse(this.baseline) : {};
            return Object.keys(spec.paths).some(path => !same(getPath(this.config, path), getPath(base, path)));
        },
        Conflicts() { return (this.conflicts || []).filter(item => item.path in spec.paths).map(item => ({ ...item, label: spec.paths[item.path] })); },
        KeepMine(path) {
            this.keepMine(path);
            view(this).edited = { ...(view(this).edited || {}), [path]: true };
            this[n('Schedule')]();
        },
        TakeSaved(path) { this.takeTheirs(path); this[n('Schedule')](); },
        StatusText() {
            const ui = this.ui?.[role] || {};
            if (ui.status === 'saving') return 'Saving…';
            if (this[n('Conflicts')]().length) return 'Changed elsewhere: choose below which value to keep.';
            if (ui.status === 'error') return 'Not saved: ' + (ui.error || 'Convo could not save this change.');
            if (ui.held) return ui.held;
            if (ui.status === 'pending') return 'Unsaved changes…';
            if (ui.status === 'saved') return 'Saved';
            return 'Changes save automatically.';
        },

        // ---- Model name. A server on this machine or your network (llama.cpp, vLLM, Other, the endpoint-only setup, a decision
        // server) is asked for its own list by Convo's discovery: with the key this card uses (a key typed but not saved yet
        // included), this card's certificate and address rules, and every name it serves (audio models too). A cloud provider
        // Agent Zero knows is searched by Agent Zero's own search (its key stays on the server, and a patched search reaches this
        // card too); when that finds nothing, Convo's discovery says why. The list is fetched once per address and key and reused
        // while you type (FOUND_MS); what you type only orders it.
        SearchesHost() {
            const url = this[n('Url')]();
            return !!this[n('Provider')](this[n('Role')]().provider || '')?.hostKey && !(url && isLocalHost(hostnameOf(url)));
        },
        async Found() {
            const current = this[n('Role')](), ui = view(this);
            const key = JSON.stringify([this[n('Url')](), current.provider || '', current.key_source || '', current.token_env || '', this.keys?.[role] || '']);
            if (ui.found?.key === key && Date.now() - ui.found.at < FOUND_MS && ui.found.models.length) return ui.found.models.slice();
            await this.discover(role);
            const models = (this.choices[role]?.models || []).map(item => item.id);
            ui.found = { key, at: Date.now(), models };
            return models;
        },
        async Search(query = '') {
            const current = this[n('Role')](), provider = current.provider || '';
            if (!this[n('SearchesHost')]()) return this[n('Found')]();
            const store = await this[n('Host')]();
            const found = store?.searchModels ? await store.searchModels(provider, query, 'chat', (current.api_base || '').trim()) : [];
            return Array.isArray(found) && found.length ? found : (this[n('Url')]() ? this[n('Found')]() : []);
        },
        // Said in the list when it is empty: why discovery found nothing (its own words), else that the server listed nothing.
        SearchNote() {
            const verification = this.choices[role]?.verification;
            return verification && ['unavailable', 'error'].includes(verification.state) && verification.message ? verification.message : 'No models found';
        },
        // Test connection, as the card shows it: the latest answer with its icon, or what it would do.
        ConnectionView() {
            const choice = this.choices[role] || {};
            if (choice.busy) return { state: 'busy', icon: 'progress_activity', label: 'Checking: ', message: 'asking the server for its models…' };
            const verification = choice.verification;
            if (!verification?.state) return { state: 'none', icon: 'help', label: '', message: 'Test connection lists the models at this address with the key in use (one typed here included, before you save it).' };
            const [icon, label] = VERDICT[verification.state] || VERDICT.unverified;
            return { state: verification.state, icon, label: label + ': ', message: verification.message || '' };
        },
        // One click from a Test answer: the address that answers instead, or the listed name that differs only in letter case.
        UseAddress(url, autosave = false) {
            if (!this.config?.roles?.[role] || typeof url !== 'string') return;
            spec.beforeEdit?.(this);
            if (spec.useAddress) spec.useAddress(this, url, autosave);
            else {
                const path = prefix + (this[n('Role')]().provider ? 'api_base' : 'url');
                setPath(this.config, path, url);
                this[n('Edited')](path, autosave);
            }
            return this.connectionRetest(role);
        },
        CloseMatch() { const close = this.choices[role]?.close_match; return typeof close === 'string' && close !== this[n('Role')]().model ? close : ''; },
        // Agent Zero's own grouping (names containing what was typed first), read from its store when it is used.
        Group(models, query) {
            const list = Array.isArray(models) ? models.filter(model => typeof model === 'string') : [], store = this[n('HostStore')]();
            const grouped = typeof store?.groupResults === 'function' ? store.groupResults(list, query) : groupModels(list, query);
            return { matched: [...(grouped?.matched || [])], rest: [...(grouped?.rest || [])] };
        },
        ChooseModel(id, autosave = false) {
            if (!this.config?.roles?.[role] || typeof id !== 'string') return;
            spec.beforeEdit?.(this);
            this.config.roles[role].model = id;
            this[n('Edited')](prefix + 'model', autosave);
        },

        // ---- Keys. A provider Agent Zero holds keys for uses Agent Zero's key, entered the Agent Zero way (its key store); Other,
        // the endpoint-only setup and "Convo's own key" use Convo's write-only key, bound to the address it is saved for.
        HostKey() { return this[n('Provider')](this[n('Role')]().provider || '')?.hostKey || ''; },
        UsesHostKey() { return !!this[n('HostKey')]() && (this[n('Role')]().key_source || 'provider') === 'provider'; },
        UsesConvoKey() { return !this[n('UsesHostKey')](); },
        HasHostKey() { return !!this[n('HostStore')]()?.apiKeyStatus?.[this[n('HostKey')]()]; },
        HostKeyValue() { return this[n('HostStore')]()?.apiKeyValues?.[this[n('HostKey')]()] || ''; },
        HostKeyInput(value) {
            const provider = this[n('HostKey')]();
            if (provider) this[n('HostStore')]()?.setApiKeyValue?.(provider, value);
            view(this).hostKeyNote = '';
        },
        // On leaving the field. An emptied field never removes Agent Zero's key (that is done in Agent Zero's API Keys), and the
        // typed key does not stay in the page once saved.
        async SaveHostKey() {
            const provider = this[n('HostKey')](), store = this[n('HostStore')](), ui = view(this);
            if (!provider || !store) return false;
            const value = store.apiKeyValues?.[provider] || '';
            const forget = () => {
                store.apiKeyValues = { ...store.apiKeyValues, [provider]: '' };
                store.apiKeyDirty = { ...store.apiKeyDirty, [provider]: false };
            };
            if (!store.apiKeyDirty?.[provider] || !value.trim()) { forget(); return false; }
            ui.hostKeyNote = 'Saving…';
            try {
                await store.saveApiKey(provider, value);
                forget();
                ui.hostKeyNote = `Saved as Agent Zero’s ${this[n('ProviderInfo')]().label} key.`;
                return true;
            } catch (error) { ui.hostKeyNote = error?.message || 'Agent Zero did not save the key.'; return false; }
        },
        ConvoKeyHint(autosave = false) {
            const url = this[n('Url')]();
            return `Convo’s own key, kept for ${url || 'the address above'} only and never shown again. Test connection uses what you type here before it is saved. `
                + (autosave ? 'Saved when you leave the field.' : 'Leave blank to keep the saved key; it is saved with your changes.');
        },
        KeyMoveOffer() { return this.keyMoveOffer(role, this[n('Url')](), this[n('UsesConvoKey')]()); },
        // The preset editor saves Convo's own key at once: pending changes land first (the key is bound to the saved address),
        // then a key-only save that changes no setting.
        async SaveConvoKey() {
            if (!this.config || !(role in (this.keys || {}))) return false;
            const ui = view(this);
            await this[n('Flush')]();
            const value = this.keys[role];
            try {
                const result = await this.api('setup_save', { revision: this.revision, config: JSON.parse(this.baseline), keys: { [role]: value } });
                if (this.keys[role] === value) delete this.keys[role];
                this.adopt(result);
                ui.keyNote = value ? 'Convo’s key saved for this address.' : 'Convo’s saved key was removed.';
                return true;
            } catch (error) { ui.keyNote = error?.message || 'The key was not saved.'; return false; }
        },

        Rows() { return (this.readiness || []).filter(row => typeof row?.id === 'string' && spec.rows(row.id)); },
        // Said before saving: an API base that replaces a provider's own address would receive Agent Zero's key.
        RouteWarning() {
            const current = this[n('Role')](), info = this[n('ProviderInfo')]();
            if (!this[n('UsesHostKey')]() || current.token_env || !(current.api_base || '').trim() || info.url === info.base) return '';
            return `Agent Zero’s ${info.label} key will be sent to ${hostnameOf(info.url) || info.url}. Choose Convo’s own key as the key source if that address should not receive it.`;
        },
        RemoteText() {
            const hostname = hostnameOf(this[n('Url')]());
            if (!hostname) return 'Required only when the address is outside this machine and your network (LAN or Tailscale).';
            return isLocalHost(hostname) ? `${hostname} is on this machine or your network (LAN or Tailscale); consent is not needed for it.`
                : `Required: your speech is sent to ${hostname}.`;
        },
        // Whether the consent switch is needed here: an address off this machine and its network, or a consent already given.
        RemoteShown() {
            const hostname = hostnameOf(this[n('Url')]());
            return this[n('Role')]().remote === true || (!!hostname && !isLocalHost(hostname));
        },
    };
    if (spec.keyCopy) Object.assign(methods, {
        CanCopyKey() {
            const current = this[n('Role')]();
            return this[n('UsesHostKey')]() && !current.token_env && !!this[n('HostStore')]() && !this[n('HasHostKey')]()
                && !!this.credentials?.[role] && (current.provider || '') === (this[n('Saved')]().provider || '');
        },
        // Convo's saved key into Agent Zero's EMPTY slot for the saved provider, server-side: it never overwrites a key, never
        // comes back to the page, and Convo keeps its own copy.
        async CopyKey() {
            const ui = view(this);
            if (ui.keyCopy?.busy) return false;
            await this[n('Flush')]();
            ui.keyCopy = { busy: true, ok: null, message: 'Copying…' };
            try {
                const result = await this.api(spec.keyCopy.action, { provider: this[n('Role')]().provider || '' });
                ui.keyCopy = { busy: false, ok: true, message: result.message || 'Copied.' };
                try { await this[n('HostStore')]()?.refreshApiKeyStatus?.(); } catch { /* the dots appear on the next read */ }
                return true;
            } catch (error) { ui.keyCopy = { busy: false, ok: false, message: error?.message || 'Not copied.' }; return false; }
        },
    });
    // An explicit check of the SAVED setup, only on its click: pending autosaves land first and an unsaved draft is refused.
    if (spec.probe) methods.Probe = async function () {
        const ui = view(this);
        if (ui.probe?.busy || !this.config) return false;
        await this[n('Flush')]();
        if (this[n('Dirty')]()) { ui.probe = { busy: false, state: 'fail', message: 'Save your changes first: the test uses the saved settings.' }; return false; }
        ui.probe = { busy: true, state: 'busy', message: spec.probe.busy };
        try {
            const result = await this.api(spec.probe.action, { provider: this[n('Saved')]().provider || '' });
            ui.probe = { busy: false, state: result.passed ? 'pass' : 'fail', message: result.message || '', reply: result.reply || '' };
            return !!result.passed;
        } catch (error) { ui.probe = { busy: false, state: 'fail', message: error?.message || 'The test could not run.' }; return false; }
    };
    return Object.fromEntries(Object.entries(methods).map(([name, method]) => [role + name, method]));
}
