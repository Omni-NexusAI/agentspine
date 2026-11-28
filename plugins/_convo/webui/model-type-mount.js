// Convo's conversation model as its own model type in Agent Zero's model settings, like System One's Decider: one section in
// the preset editor (the shared conversation-model-field.html card, saving each change by itself) and a "Convo — provider /
// model" row wherever the host shows a preset overview (Settings → Agent → Models, a scope's model preset). The
// `modal_content_loaded` hook (extensions/webui/modal_content_loaded/_20_convo_model_type.js) attaches it to those modals only,
// each watched by an observer on that modal alone and released when it closes. A scan writes only what is missing; a summary
// row is repainted only when the saved choice changed. No imports: the hook passes the store and the host globals, so node tests
// drive it on a fake DOM against verbatim host markup.

export const EDITOR_PATH = 'plugins/_model_config/webui/main.html';                       // model-config-store.js openPresetEditor
export const SUMMARY_PATHS = ['settings/settings.html', 'components/plugins/plugin-settings.html'];   // Settings; a plugin's Config
export const CARD_PATH = '/plugins/_convo/webui/conversation-model-field.html';
export const SECTION_ATTRIBUTE = 'data-convo-model-type';
export const ROW_CLASS = 'convo-model-summary-row';
const attached = new WeakSet();

// 'editor' for the preset editor, 'summary' for a modal that can show a preset overview, null for every other modal.
export function watchedModal(path) {
    const clean = String(path || '').split(/[?#]/)[0].replace(/^\/+/, '');
    return clean === EDITOR_PATH ? 'editor' : SUMMARY_PATHS.includes(clean) ? 'summary' : null;
}

function element(doc, tag, className = '', text = '') {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
}

// The section, once per preset-editor body (the body Alpine renders for the selected preset; a new body gets its own).
export function mountSection(body, doc) {
    const existing = body.querySelector(`:scope > [${SECTION_ATTRIBUTE}]`);
    if (existing) return existing;
    const section = element(doc, 'section', 'preset-model-section convo-model-type');
    section.setAttribute(SECTION_ATTRIBUTE, '');
    section.setAttribute('aria-labelledby', 'convo-model-type-title');
    const heading = element(doc, 'div', 'preset-model-heading');
    const title = element(doc, 'div', 'section-title', 'Convo conversation model');
    title.setAttribute('id', 'convo-model-type-title');
    heading.append(title, element(doc, 'div', 'section-description', 'One setting for all presets; changes save automatically.'));
    const scope = element(doc, 'div');
    scope.setAttribute('x-data', '{ convoAutosave: true }');                              // the card saves each change by itself here
    const card = element(doc, 'x-component');
    card.setAttribute('path', CARD_PATH);
    scope.append(card);
    section.append(heading, scope);
    body.append(section);
    return section;
}

// The overview row, once per list, after the host's own rows (Alpine inserts its rows after its template, so this stays last).
export function mountSummary(list, doc) {
    const existing = list.querySelector(`:scope > .${ROW_CLASS}`);
    if (existing) {
        // The host can re-render its rows after this one was placed (seen live): put it back at the end, only when it is not.
        if (list.lastElementChild !== existing) list.appendChild(existing);
        return existing;
    }
    const row = element(doc, 'div', 'model-preset-row ' + ROW_CLASS);
    const icon = element(doc, 'x-icon', 'model-preset-icon');
    icon.setAttribute('name', 'graphic_eq');
    icon.setAttribute('aria-hidden', 'true');
    row.append(icon, element(doc, 'span', 'model-preset-role', 'Convo'), element(doc, 'span', 'model-preset-identity'));
    list.append(row);
    return row;
}

// "Provider / model" in the host's own markup. Returns whether it wrote: the list is a live region, so an unchanged choice is
// never written again.
export function paintSummary(row, summary, doc) {
    const provider = summary?.provider || '—', model = summary?.model || '—', key = JSON.stringify([provider, model]);
    const identity = row.querySelector('.model-preset-identity');
    if (!identity || row.getAttribute('data-convo-summary') === key) return false;
    identity.replaceChildren(element(doc, 'span', 'model-preset-provider', provider), element(doc, 'span', 'model-preset-separator', '/'),
                             element(doc, 'span', '', model));
    row.setAttribute('data-convo-summary', key);
    return true;
}

// Watch one modal (the hook's { modalPath, modal }) until it closes. `env`: the Convo settings store, the document, the
// MutationObserver class and Alpine (effect/release). Returns null for an unwatched or already watched modal.
export function attach(ctx, { store, document: doc, MutationObserver: Observer, Alpine } = {}) {
    const kind = watchedModal(ctx?.modalPath), root = ctx?.modal?.element;
    if (!kind || !root || !store || !doc || !Observer || attached.has(root)) return null;
    attached.add(root);
    const rows = new Set();
    let ready = false, released = false, effect = null, loading = null;
    const summary = () => store.conversationSummary?.() || null;
    // The editor starts from what is saved now unless the shared draft holds unsaved Convo edits (never discarded here); an
    // overview reads the settings only once it actually shows a preset.
    const load = () => loading ||= Promise.resolve(kind === 'editor' && !store.dirty ? store.load(true) : store.load()).then(() => {
        if (released || !store.config) return;
        ready = true;
        effect = Alpine?.effect?.(paint) || null;
        scan();
    }, () => {});
    const paint = () => {
        const value = summary();                                                     // read first: this is what the effect tracks
        for (const row of [...rows]) { if (row.isConnected) paintSummary(row, value, doc); else rows.delete(row); }
    };
    const release = () => {
        if (released) return false;
        released = true;
        doc.removeEventListener('modal-closed', closed);
        observer.disconnect();
        if (effect) Alpine?.release?.(effect);
        return true;
    };
    const scan = () => {
        if (released) return;
        // A plugin's Config modal shows a preset overview only for the model settings plugin: any other plugin's page is left alone.
        const plugin = root.__pluginSettingsContext?.pluginName;
        if (plugin && plugin !== '_model_config') { release(); return; }
        if (kind === 'editor') for (const body of root.querySelectorAll('.preset-editor-body')) mountSection(body, doc);
        const lists = root.querySelectorAll('.model-preset-rows');
        if (!lists.length) return;
        if (!ready) { load(); return; }                                              // a row appears with its value, never empty
        for (const list of lists) {
            const row = mountSummary(list, doc);
            if (!rows.has(row)) { rows.add(row); paintSummary(row, summary(), doc); }
        }
    };
    const observer = new Observer(scan);
    const closed = () => {
        if (root.isConnected || !release()) return;                                  // another modal closed
        if (kind === 'editor') void store.conversationEditorClosed?.();             // a change still waiting is saved on close
    };
    observer.observe(root, { childList: true, subtree: true });
    doc.addEventListener('modal-closed', closed);
    if (kind === 'editor') load();
    scan();
    return released ? null : { kind, scan, closed, observer };
}
