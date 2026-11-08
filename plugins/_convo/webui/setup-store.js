import { createStore } from '/js/AlpineStore.js';
import { callJsonApi } from '/js/api.js';
import { fillDefaults, defaults, controlsFor, mergeSettings, getPath, setPath, plain } from '/plugins/_convo/webui/defaults.js';
import { conversationSetup } from '/plugins/_convo/webui/conversation-setup.js';
import { classifierSetup } from '/plugins/_convo/webui/classifier-setup.js';
import { speechSetup } from '/plugins/_convo/webui/speech-setup.js';
import { dictationSetup } from '/plugins/_convo/webui/dictation-setup.js';

const clone = value => JSON.parse(JSON.stringify(value));
const endpoint = '/plugins/_convo/conversation';
let loading = null, patching = null, saving = null;    // the read in flight; the patch and the save in flight (writes never overlap)
const asked = {};                                      // per role: the newest discovery request (an older answer is not shown)
// What a discovery answer depends on: an answer for another address, provider or key source than the card shows now is stale.
const addressOf = role => JSON.stringify(['provider', 'api_base', 'url', 'key_source', 'token_env', 'kind', 'connection'].map(field => role?.[field] ?? ''));
const LOOPBACK = /^(localhost|localhost\.localdomain|ip6-localhost|ip6-loopback|127(\.\d{1,3}){3}|::1|\[::1\])$/i;

// One shared draft, edited by cards that each own a methods-only mixin in their own file, so the cards can change
// independently. State and getters stay in the core: spreading an object copies a getter's current value, not the getter.
// A mixin member that is not a method, or a name defined twice, is refused when the store is built, never overridden.
export function compose(core, ...mixins) {
    const result = Object.defineProperties({}, Object.getOwnPropertyDescriptors(core));
    for (const mixin of mixins) {
        for (const [name, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(mixin))) {
            if (typeof descriptor.value !== 'function') throw new Error(`Settings mixin member "${name}" must be a method`);
            if (name in result) throw new Error(`Settings member "${name}" is defined twice`);
            result[name] = descriptor.value;
        }
    }
    return result;
}

export const store = createStore('convoSetup', compose({
    config: null, revision: null, baseline: '', keys: {}, credentials: {}, readiness: [], decisionBackends: [], busy: false,
    providers: [], native: {}, deciderReference: null, classifierCatalog: null, inConfigModal: false,
    stale: false, conflicts: [], theirs: null, revisionSeen: '',
    section: 'Connections', sections: ['Connections', 'Conversation', 'Voice & Audio'],   // tuning lives in the developer diagnostics panel
    roles: ['conversation', 'classifier', 'tts'], choices: {}, searches: {}, error: '', message: '',
    importDocument: null, importPreview: null, importPaths: [],
    network: {}, certificates: {}, keyBound: {},       // where Agent Zero runs, remembered certificates, the address each saved key is for
    ui: { conversation: {}, classifier: {}, speech: {}, dictation: {} },  // each card's transient view state; its mixin holds methods only
    get dirty() { return !!this.config && (JSON.stringify(this.config) !== this.baseline || Object.keys(this.keys).length > 0); },
    // System One's own decider as the server describes it (decider_reference), with its verdict `audio`.
    get decider() { return this.deciderReference || { installed: false, enabled: false, consumer_enabled: false, registered: [], reasons: [] }; },
    setKey(role, value) { if (value) this.keys[role] = value; else delete this.keys[role]; },
    clearKey(role) { this.keys[role] = ''; },
    label(role) { return {conversation:'Conversation model', classifier:'Classifier', tts:'Speech output'}[role]; },
    isDiscovering(role) { return !!this.choices[role]?.busy; },
    // Which controls a role shows. Speech output asks its own card (speechControls); the other roles are one endpoint each.
    controls(role) { return role === 'tts' ? this.speechControls() : controlsFor(role); },
    show(role, control) { return !!this.controls(role)[control]; },
    async openNative(plugin) {
        try { const { store } = await import('/components/plugins/plugin-settings-store.js'); await store.openConfig(plugin); }
        catch (error) { this.fail(error); }
    },
    async api(action, data = {}) {
        const result = await callJsonApi(endpoint, {action, ...data});
        if (!result?.ok) throw new Error(result?.error || 'Convo could not complete this operation.');
        return result;
    },
    accept(result) {
        this.config = fillDefaults(clone(result.config)); this.revision = result.revision;
        this.baseline = JSON.stringify(this.config); this.readiness = result.readiness || []; this.credentials = result.credentials || {};
        this.decisionBackends = result.decision_backends || [];
        this.providers = result.providers || []; this.native = result.native || {}; this.deciderReference = result.decider_reference || null;
        this.classifierCatalog = result.classifier_catalog || null;
        this.takeFacts(result);
        this.keys = {}; this.error = ''; this.stale = false; this.conflicts = []; this.theirs = null; this.revisionSeen = '';
    },
    takeFacts(result) {
        this.network = result.network || this.network || {};
        this.certificates = result.certificates || this.certificates || {};
        this.keyBound = result.key_bound || this.keyBound || {};
    },
    // Take in a write's result without losing what is still being edited: merged three ways (what this editor last loaded,
    // its draft, what is saved now), so other unsaved edits stay, saved fields come in, and a field changed in both places is
    // listed as a conflict and kept as mine until chosen. Pending keys are kept. Returns the new conflicts.
    adopt(result) {
        const theirs = fillDefaults(clone(result.config));
        const { merged, conflicts } = mergeSettings(this.baseline ? JSON.parse(this.baseline) : theirs, this.config || theirs, theirs);
        // An earlier unresolved conflict stays listed while mine still differs from what is saved now.
        const earlier = this.conflicts.filter(item => !conflicts.some(next => next.path === item.path)
            && JSON.stringify(getPath(merged, item.path)) !== JSON.stringify(getPath(theirs, item.path))).map(item => ({ ...item, theirs: getPath(theirs, item.path) }));
        this.config = merged; this.baseline = JSON.stringify(theirs); this.revision = result.revision; this.theirs = theirs;
        this.readiness = result.readiness || []; this.credentials = result.credentials || {};
        this.decisionBackends = result.decision_backends || this.decisionBackends;
        this.providers = result.providers || this.providers; this.native = result.native || this.native;
        this.deciderReference = result.decider_reference || this.deciderReference;
        this.classifierCatalog = result.classifier_catalog || this.classifierCatalog;
        this.takeFacts(result);
        this.conflicts = [...conflicts, ...earlier]; this.stale = false; this.revisionSeen = ''; this.error = '';
        return conflicts;
    },
    async load(force = false) {
        if (loading) return loading;
        if (this.config && !force) return;
        if (force && this.dirty && !window.confirm('Discard unsaved Convo settings and reload?')) return;
        loading = this.api('setup_read').then(result => this.accept(result)).catch(error => this.fail(error)).finally(() => { loading = null; });
        return loading;
    },
    fail(error) { this.error = error?.message || String(error); globalThis.justToast?.(this.error, 'error'); },
    // The status poll, opening the editor and the tab returning to view all report the saved revision. A clean draft just
    // refreshes; an edited one is never overwritten or lost: it is flagged so the user can merge or discard. A write of this
    // editor's own (save or patch) is not "elsewhere".
    async noteRevision(latest) {
        if (!latest || !this.config || this.busy || patching || latest === this.revision || latest === this.revisionSeen) return;
        this.revisionSeen = latest;
        if (!this.dirty) { await this.load(true); this.revisionSeen = ''; return; }
        this.stale = true;
    },
    async mergeLatest() {
        if (this.busy || !this.config) return;
        try {
            const result = await this.api('setup_read');
            const theirs = fillDefaults(clone(result.config));
            const { merged, conflicts } = mergeSettings(JSON.parse(this.baseline), this.config, theirs);
            this.config = merged; this.baseline = JSON.stringify(theirs); this.revision = result.revision; this.theirs = theirs;
            this.readiness = result.readiness || []; this.credentials = result.credentials || {};
            this.providers = result.providers || this.providers; this.native = result.native || this.native;
            this.deciderReference = result.decider_reference || this.deciderReference;
            this.classifierCatalog = result.classifier_catalog || this.classifierCatalog;
            this.takeFacts(result);
            this.conflicts = conflicts; this.stale = false; this.revisionSeen = '';
            this.message = conflicts.length ? 'Merged. Some fields changed in both places: choose which to keep.' : 'Merged: the other changes were brought in and yours were kept.';
        } catch (error) { this.fail(error); }
    },
    keepMine(path) { this.conflicts = this.conflicts.filter(item => item.path !== path); },
    takeTheirs(path) { setPath(this.config, path, getPath(this.theirs, path)); this.conflicts = this.conflicts.filter(item => item.path !== path); },
    display(value) { const text = typeof value === 'string' ? value : JSON.stringify(value); return text === undefined ? '(not set)' : text.length > 80 ? text.slice(0, 79) + '…' : (text || '(empty)'); },
    async discardAndReload() { this.discard(); await this.load(true); },
    // Drop unsaved edits (the caller has already asked); the saved baseline becomes the draft again.
    discard() {
        if (!this.baseline) return;
        this.config = fillDefaults(clone(JSON.parse(this.baseline)));
        this.keys = {}; this.error = ''; this.message = '';
    },
    async save() {
        if (this.busy || !this.config) return false;
        this.busy = true; this.error = '';
        if (patching) await patching;                       // a patch in flight lands first, so this save carries the revision it made
        const submitted = clone(this.config), keys = clone(this.keys);
        let finished; saving = new Promise(resolve => { finished = resolve; });
        try {
            const result = await this.api('setup_save', {revision:this.revision, config:submitted, keys});
            const newer = JSON.stringify(this.config) !== JSON.stringify(submitted) ? clone(this.config) : null;
            const newerKeys = Object.fromEntries(Object.entries(this.keys).filter(([role,value]) => value !== keys[role]));
            this.accept(result);
            if (newer) this.config = newer;
            this.keys = newerKeys;
            this.message = 'Saved. Model changes apply next session; voice changes apply next response.';
            globalThis.justToast?.('Convo settings saved.', 'success');
            void globalThis.Alpine?.store?.('convo')?.refresh?.();        // the panel and the orb see the new settings at once
            return true;
        } catch (error) { this.fail(error); return false; }
        finally { this.busy = false; saving = null; finished(); }
    },
    // Change some fields and save just those now (an autosaving editor), leaving the rest of the draft as it is. Each path is
    // compare-and-set on the server against what this editor last loaded; the result is adopted three ways, so other unsaved
    // edits survive and a field changed elsewhere meanwhile comes back as a conflict. `changes`: {path: value}, undefined
    // removes the field. Credentials never go this way. Resolves to the server's answer, or null after reporting a failure.
    async patch(changes) {
        const entries = Object.entries(changes || {});
        if (!this.config || !entries.length) return null;
        for (const [path, value] of entries) setPath(this.config, path, value);          // the draft shows the change at once
        const previous = patching;
        const run = (async () => {
            if (previous) await previous;
            if (saving) await saving;
            const base = this.baseline ? JSON.parse(this.baseline) : {};
            const items = entries.map(([path, value]) => {
                const item = { path }, before = getPath(base, path);
                if (before !== undefined) item.base = before;
                if (value !== undefined) item.value = plain(value);
                return item;
            });
            try { const result = await this.api('setup_patch', { changes: items }); this.adopt(result); return result; }
            catch (error) { this.fail(error); return null; }
        })();
        patching = run;
        try { return await run; } finally { if (patching === run) patching = null; }
    },
    // Discovery (and Test connection, `test`) for what the card shows now, with a key typed but not saved yet for this request
    // only. Only the newest request's answer is shown, and only while the card's address, provider and key source are unchanged:
    // typing a model name meanwhile does not throw the answer away.
    async discover(role, test = false) {
        if (!this.config?.roles?.[role]) return;
        const id = asked[role] = (asked[role] || 0) + 1;
        this.choices[role] = {...this.choices[role], busy:true};
        const provider = clone(this.config.roles[role]), draft = this.keys[role];
        try {
            const result = await this.api(test ? 'connection_test' : 'discover', {role, provider, ...(draft ? {draft_key: draft} : {})});
            if (id !== asked[role] || addressOf(provider) !== addressOf(this.config.roles[role])) return;
            this.choices[role] = {...result, busy:false};
            if ('certificate' in result) this.certificates = {...this.certificates, [role]: result.certificate};
        } catch (error) { if (id === asked[role]) { this.choices[role] = {...this.choices[role], verification:{state:'error', message:error.message}}; this.fail(error); } }
        finally { if (id === asked[role]) this.choices[role] = {...this.choices[role], busy:false}; }
    },

    // ---- One connection's extras, the same on every card: where "localhost" goes, the certificate remembered for this server
    // (or a changed one), the address that answers after a refused connection, and the saved key's own address.
    connectionResult(role) { return role === 'classifier' ? (this.ui?.classifier?.check || {}) : (this.choices[role] || {}); },
    thisComputerText(url) {
        let host = '';
        try { host = new URL(url).hostname; } catch { return ''; }
        if (!LOOPBACK.test(host)) return '';
        if (this.network?.translated) return `This computer: Agent Zero reaches ${host} through ${this.network.this_computer}, so the address works as typed.`;
        return this.network?.in_docker ? `Inside Agent Zero’s container ${host} is the container itself, and host.docker.internal is not available here: use this computer’s LAN address.` : '';
    },
    connectionSuggestion(role) { return this.connectionResult(role).suggestion || null; },
    certificateChanged(role) { return this.connectionResult(role).certificate_changed || null; },
    certificateNote(role) {
        const result = this.connectionResult(role), certificate = 'certificate' in result ? result.certificate : this.certificates?.[role];
        if (!certificate) return '';
        const names = (certificate.names || []).slice(0, 4).join(', ');
        return `Private certificate remembered${certificate.subject ? ' · ' + certificate.subject : ''}${names ? ' · ' + names : ''} · SHA-256 ${String(certificate.sha256 || '').slice(0, 23)}…`
            + (certificate.expires ? ` · valid until ${certificate.expires}` : '');
    },
    async certificateAccept(role, url) {
        try { await this.api('tls_accept', { url }); } catch (error) { this.fail(error); return false; }
        return this.connectionRetest(role);
    },
    async certificateForget(role, url) {
        try { await this.api('tls_forget', { url }); } catch (error) { this.fail(error); return false; }
        this.certificates = {...this.certificates, [role]: null};
        if (role === 'classifier') { if (this.ui.classifier.check) this.ui.classifier.check = {...this.ui.classifier.check, certificate: null}; }
        else this.choices[role] = {...this.choices[role], certificate: null};
        return true;
    },
    async connectionRetest(role) { return role === 'classifier' ? this.classifierCheck() : this.discover(role, true); },
    // A saved Convo key bound to another address than the card shows: offered for this one ("Use it for this address").
    keyMoveOffer(role, url, usesConvoKey = true) {
        const bound = this.keyBound?.[role] || '';
        return usesConvoKey && !(role in (this.keys || {})) && bound && url && bound.replace(/\/+$/, '') !== url.replace(/\/+$/, '') ? bound : '';
    },
    async keyMove(role) {
        try { this.adopt(await this.api('key_rebind', { role, provider: clone(this.config.roles[role]) })); }
        catch (error) { this.fail(error); return false; }
        return this.connectionRetest(role);
    },
    options(role, kind = 'models') {
        const values = this.choices[role]?.[kind] || [];
        const field = {voices:'voice',tuning:'tuning_profile',models:'model'}[kind] || 'model';
        const selected = this.config?.roles[role]?.[field];
        const query = (this.searches[role] || '').toLowerCase();
        const filtered = values.filter(value => value.id === selected || `${value.label || ''} ${value.id}`.toLowerCase().includes(query));
        if (selected && !values.some(value => value.id === selected)) filtered.unshift({id:selected,label:`${selected} (saved; not verified)`});
        return filtered;
    },
    async runtime(action) {
        try { await this.api(action); this.message = action === 'runtime_start' ? 'Runtime start requested. Check readiness before talking.' : 'Owned runtime stop requested.';
            const result = await this.api('setup_read'); this.readiness = result.readiness || [];
        } catch (error) { this.fail(error); }
    },
    async previewImport(file) {
        if (!file) return;
        try {
            if (file.size > 1024 * 1024) throw new Error('Settings imports must be smaller than 1 MB. Use Studio for profile bundles.');
            this.importDocument = JSON.parse(await file.text());
            this.importPreview = await this.api('import_preview', {document:this.importDocument});
            this.importPaths = [];
        } catch (error) { this.importPreview = null; this.fail(error); }
    },
    async applyImport() {
        if (this.dirty) { this.fail(new Error('Save or reload your current changes before applying an import.')); return; }
        try { this.accept(await this.api('import_apply', {document:this.importDocument, paths:this.importPaths, revision:this.importPreview.revision}));
            this.importPreview = null; this.importDocument = null; this.importPaths = []; this.message = 'Selected settings imported.';
        } catch (error) { this.fail(error); }
    },
    async bindNative(context) {
        this.inConfigModal = true;                                // the host's own Config modal: no "open native settings" from inside it
        await this.load();
        if (!this.config || context.__convoSharedSettings) return;
        context.__convoSharedSettings = true;
        Object.defineProperty(context, 'hasUnsavedChanges', {configurable:true,get:() => this.dirty});
        // Only this modal instance is adapted; never patch the host prototype.
        context.save = async () => {
            context.isSaving = true;
            try { if (await this.save()) {
                context.settings = {...context.settings,convo:clone(this.config),_convo_revision:this.revision};
                context.settingsSnapshotJson = JSON.stringify(context.settings);
                if (!this.dirty) globalThis.closeModal?.();
            } } finally { context.isSaving = false; }
        };
        context.resetToDefault = () => {
            if (!window.confirm('Reset Convo controls to defaults? Native speech settings are not changed. Save to apply.')) return;
            const reset = (target, values) => { for (const [key,value] of Object.entries(values)) {
                if (value && typeof value === 'object' && !Array.isArray(value)) { target[key] ||= {}; reset(target[key],value); }
                else target[key] = clone(value);
            } };
            reset(this.config, defaults); this.keys = {};
        };
    },
}, conversationSetup, classifierSetup, speechSetup, dictationSetup));
