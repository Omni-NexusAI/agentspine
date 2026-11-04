// The Convo conversation model as an Agent Zero model type: the section the preset editor gets and the "Convo — provider /
// model" row of the preset overviews (webui/model-type-mount.js, injected by extensions/webui/modal_content_loaded), and the
// shared card's behaviour (webui/conversation-setup.js on the real settings store): autosave, conflicts, providers, keys and
// the explicit checks. The host markup comes from verbatim v2.11 fixtures (tests/fixtures/host-*.html) on a small fake DOM;
// timers run on a virtual clock. Nothing here proves the rendered host page; tests/live-model-type-smoke.mjs checks that.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { loadModule, hostStubs } from './vm-loader.mjs';
import { defaults, getPath, setPath } from '../webui/defaults.js';
import { CONVERSATION_CATALOG, conversationUrl, addressProblem, isLocalHost, groupModels, AUTOSAVE_MS, CARD_PATHS } from '../webui/conversation-setup.js';
import { watchedModal, mountSection, mountSummary, paintSummary, attach, SECTION_ATTRIBUTE, ROW_CLASS, CARD_PATH } from '../webui/model-type-mount.js';
import { readWebui } from './settings-markup.mjs';

const plain = value => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const fixture = name => readFile(new URL('./fixtures/' + name, import.meta.url), 'utf8');
const settle = async (rounds = 25) => { for (let i = 0; i < rounds; i++) await new Promise(resolve => setImmediate(resolve)); };

// ---- A small DOM: just enough Element/Document/MutationObserver for model-type-mount.js, built from verbatim host markup.
const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);
class FakeNode {
    constructor(doc) { this.ownerDocument = doc; this.parentNode = null; }
    get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
    get isConnected() { let node = this; while (node.parentNode) node = node.parentNode; return node === this.ownerDocument; }
    after(...nodes) { const parent = this.parentNode, next = parent.childNodes[parent.childNodes.indexOf(this) + 1] || null; for (const node of nodes) parent.insertBefore(node, next); }
}
class FakeText extends FakeNode {
    constructor(doc, data) { super(doc); this.nodeType = 3; this.data = data; }
    get textContent() { return this.data; }
    cloneNode() { return new FakeText(this.ownerDocument, this.data); }
}
class FakeElement extends FakeNode {
    constructor(doc, tag) {
        super(doc); this.nodeType = 1; this.localName = tag.toLowerCase(); this.attrs = new Map(); this.childNodes = [];
        this.content = this.localName === 'template' ? new FakeElement(doc, '#content') : null;    // template content is not in the tree
    }
    get tagName() { return this.localName.toUpperCase(); }
    get children() { return this.childNodes.filter(node => node.nodeType === 1); }
    get lastElementChild() { return this.children.at(-1) || null; }
    getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
    hasAttribute(name) { return this.attrs.has(name); }
    setAttribute(name, value) { this.attrs.set(name, String(value)); this.ownerDocument?.wrote('attributes', this); }
    get className() { return this.getAttribute('class') || ''; }
    set className(value) { this.setAttribute('class', value); }
    get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
    set textContent(value) { this.replaceChildren(...(value === '' ? [] : [new FakeText(this.ownerDocument, String(value))])); }
    insertBefore(node, before) {
        node.parentNode?.removeChild(node);
        const index = before ? this.childNodes.indexOf(before) : -1;
        this.childNodes.splice(index < 0 ? this.childNodes.length : index, 0, node);
        node.parentNode = this;
        this.ownerDocument?.wrote('childList', this);
        return node;
    }
    appendChild(node) { return this.insertBefore(node, null); }
    append(...nodes) { for (const node of nodes) this.appendChild(node); }
    removeChild(node) {
        const index = this.childNodes.indexOf(node);
        if (index >= 0) { this.childNodes.splice(index, 1); node.parentNode = null; this.ownerDocument?.wrote('childList', this); }
        return node;
    }
    remove() { this.parentNode?.removeChild(this); }
    replaceChildren(...nodes) { for (const node of [...this.childNodes]) this.removeChild(node); this.append(...nodes); }
    contains(node) { for (; node; node = node.parentNode) if (node === this) return true; return false; }
    cloneNode(deep = false) {
        const copy = new FakeElement(this.ownerDocument, this.localName);
        for (const [name, value] of this.attrs) copy.attrs.set(name, value);
        if (deep) {
            for (const child of this.childNodes) copy.appendChild(child.cloneNode(true));
            if (this.content) for (const child of this.content.childNodes) copy.content.appendChild(child.cloneNode(true));
        }
        return copy;
    }
    querySelectorAll(selector) { return query(this, selector); }
    querySelector(selector) { return query(this, selector)[0] || null; }
}
class FakeDocument extends FakeElement {
    constructor() {
        super(null, '#document');
        this.ownerDocument = this; this.writes = 0; this.observers = new Set(); this.listeners = new Map();
        this.body = new FakeElement(this, 'body'); this.appendChild(this.body);
    }
    get isConnected() { return true; }
    createElement(tag) { return new FakeElement(this, tag); }
    wrote(type, target) { this.writes++; for (const observer of [...this.observers]) observer.notice(type, target); }
    addEventListener(type, listener) { if (!this.listeners.has(type)) this.listeners.set(type, new Set()); this.listeners.get(type).add(listener); }
    removeEventListener(type, listener) { this.listeners.get(type)?.delete(listener); }
    dispatchEvent(event) { for (const listener of [...(this.listeners.get(event.type) || [])]) listener(event); return true; }
}
function observerClass(doc) {
    return class FakeMutationObserver {
        static made = [];
        constructor(callback) { this.callback = callback; this.records = []; this.calls = 0; FakeMutationObserver.made.push(this); }
        observe(target, options) { this.target = target; this.options = options; doc.observers.add(this); }
        disconnect() { this.disconnected = true; doc.observers.delete(this); this.records = []; }
        notice(type, target) {
            if (type !== 'childList' || !this.target.contains(target)) return;
            if (!this.records.length) queueMicrotask(() => {
                const records = this.records; this.records = [];
                if (records.length && doc.observers.has(this)) { this.calls++; this.callback(records, this); }
            });
            this.records.push({ type, target });
        }
    };
}
// Selectors: compounds of tag, .class, #id, [attr] / [attr="v"] and :scope, joined by descendant or child combinators.
function compound(token) {
    const result = { tag: null, classes: [], attrs: [], scope: false }, pattern = /(:scope)|\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]|(\*|[a-zA-Z][\w-]*)/y;
    for (let index = 0; index < token.length; index = pattern.lastIndex) {
        pattern.lastIndex = index;
        const match = pattern.exec(token);
        if (!match) throw new Error('Unsupported selector: ' + token);
        if (match[1]) result.scope = true; else if (match[2]) result.classes.push(match[2]); else if (match[3]) result.attrs.push(['id', match[3]]);
        else if (match[4]) result.attrs.push([match[4], match[5]]); else if (match[6] !== '*') result.tag = match[6].toLowerCase();
    }
    return result;
}
function selectorGroups(selector) {
    return selector.split(',').map(group => {
        const compounds = [], combinators = [];
        for (const token of group.trim().replace(/\s*>\s*/g, ' > ').split(/\s+/)) {
            if (token === '>') { combinators[compounds.length] = '>'; continue; }
            if (compounds.length && combinators[compounds.length] === undefined) combinators[compounds.length] = ' ';
            compounds.push(compound(token));
        }
        return { compounds, combinators };
    });
}
function matchesCompound(node, want, scope) {
    if ((want.scope && node !== scope) || (want.tag && node.localName !== want.tag)) return false;
    const classes = node.className.split(/\s+/);
    return want.classes.every(name => classes.includes(name)) && want.attrs.every(([name, value]) => node.hasAttribute(name) && (value === undefined || node.getAttribute(name) === value));
}
function matches(node, { compounds, combinators }, scope) {
    if (!matchesCompound(node, compounds.at(-1), scope)) return false;
    for (let i = compounds.length - 1; i > 0; i--) {
        if (combinators[i] === '>') { node = node.parentElement; if (!node || !matchesCompound(node, compounds[i - 1], scope)) return false; }
        else { do { node = node.parentElement; } while (node && !matchesCompound(node, compounds[i - 1], scope)); if (!node) return false; }
    }
    return true;
}
function query(root, selector) {
    const groups = selectorGroups(selector), found = [];
    const walk = node => { for (const child of node.children) { if (groups.some(group => matches(child, group, root))) found.push(child); walk(child); } };
    walk(root);
    return found;
}
// Markup to nodes (quote-aware attributes, raw <script>/<style>, <template> children into its content).
function parse(doc, html) {
    const root = new FakeElement(doc, '#parsed'), stack = [root];
    const parent = () => stack.at(-1).content || stack.at(-1);
    let i = 0;
    while (i < html.length) {
        if (html.startsWith('<!--', i)) { const end = html.indexOf('-->', i + 4); i = end < 0 ? html.length : end + 3; continue; }
        if (html.startsWith('</', i)) {
            const end = html.indexOf('>', i), name = html.slice(i + 2, end).trim().toLowerCase();
            for (let k = stack.length - 1; k > 0; k--) if (stack[k].localName === name) { stack.length = k; break; }
            i = end + 1; continue;
        }
        if (html[i] === '<' && /[a-zA-Z]/.test(html[i + 1] || '')) {
            let j = i + 1;
            while (j < html.length && /[^\s/>]/.test(html[j])) j++;
            const node = new FakeElement(doc, html.slice(i + 1, j));
            let selfClosing = false;
            for (;;) {
                while (/\s/.test(html[j])) j++;
                if (html[j] === '>') { j++; break; }
                if (html.startsWith('/>', j)) { selfClosing = true; j += 2; break; }
                let k = j;
                while (k < html.length && /[^\s=>]/.test(html[k]) && !html.startsWith('/>', k)) k++;
                const name = html.slice(j, k);
                let value = '';
                j = k; while (/\s/.test(html[j])) j++;
                if (html[j] === '=') {
                    j++; while (/\s/.test(html[j])) j++;
                    const quote = html[j];
                    if (quote === '"' || quote === "'") { const end = html.indexOf(quote, j + 1); value = html.slice(j + 1, end); j = end + 1; }
                    else { let end = j; while (end < html.length && /[^\s>]/.test(html[end])) end++; value = html.slice(j, end); j = end; }
                }
                if (name) node.attrs.set(name, value);
            }
            parent().appendChild(node);
            if (node.localName === 'script' || node.localName === 'style') {
                const end = html.indexOf('</' + node.localName, j);
                node.appendChild(new FakeText(doc, html.slice(j, end)));
                i = html.indexOf('>', end) + 1; continue;
            }
            if (!selfClosing && !VOID.has(node.localName)) stack.push(node);
            i = j; continue;
        }
        if (html[i] === '<' && html[i + 1] === '!') { i = html.indexOf('>', i) + 1; continue; }
        const next = html.indexOf('<', i + 1), end = next < 0 ? html.length : next;
        parent().appendChild(new FakeText(doc, html.slice(i, end)));
        i = end;
    }
    return root;
}
// What Alpine does with the host's templates: an x-if clone goes after its template; x-for items too, one after the other.
function render(node, items = 3) {
    for (const child of [...node.children]) {
        if (child.localName !== 'template') { render(child, items); continue; }
        const copies = child.hasAttribute('x-if') ? 1 : child.hasAttribute('x-for') ? items : 0;
        let last = child;
        for (let n = 0; n < copies; n++) {
            const clones = child.content.childNodes.map(item => item.cloneNode(true));
            last.after(...clones);
            last = clones.at(-1) || last;
            for (const clone of clones) if (clone.nodeType === 1) render(clone, items);
        }
    }
}
// A host modal (js/modals.js createModalElement) holding a component's body, as importComponent leaves it.
function openModal(doc, path, html) {
    const element = doc.createElement('div'); element.className = 'modal show'; element.setAttribute('data-modal-path', path);
    const inner = doc.createElement('div'); inner.className = 'modal-inner';
    const scroll = doc.createElement('div'); scroll.className = 'modal-scroll';
    const body = doc.createElement('div'); body.className = 'modal-bd';
    for (const child of [...parse(doc, html).querySelector('body').childNodes]) body.appendChild(child);
    scroll.append(body); inner.append(scroll); element.append(inner); doc.body.append(element);
    return { path, element, body };
}
const loadComponentInto = (doc, host, html) => { for (const child of [...parse(doc, html).querySelector('body').childNodes]) host.appendChild(child); };
function closeModal(doc, modal) { modal.element.remove(); doc.dispatchEvent({ type: 'modal-closed', detail: { modalPath: modal.path } }); }
function fakeAlpine() {
    const effects = new Set();
    return { effects, effect(fn) { const handle = { fn }; effects.add(handle); fn(); return handle; }, release(handle) { effects.delete(handle); }, run() { for (const handle of [...effects]) handle.fn(); } };
}
function virtualClock() {
    let now = 0, next = 1;
    const timers = new Map();
    return {
        setTimeout(fn, ms = 0) { const id = next++; timers.set(id, { at: now + ms, fn }); return id; },
        clearTimeout(id) { timers.delete(id); },
        get pending() { return timers.size; },
        async advance(ms) {
            const end = now + ms;
            for (;;) {
                const due = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
                if (!due) break;
                timers.delete(due[0]); now = due[1].at; due[1].fn(); await settle();
            }
            now = end; await settle();
        },
    };
}

// ---- The real settings store (setup-store.js + every card mixin) on a server that behaves like convo_setup: setup_patch is
// compare-and-set per path, every write derives the endpoint from the provider and refuses a remote one without consent.
async function rig({ config = {}, credentials = {}, hostKeys = {}, reactive = true } = {}) {
    const clock = virtualClock();
    const server = { config: plain(defaults), revision: 'r1', writes: 0, calls: [], keys: {}, credentials: { ...credentials }, hold: {}, models: [], answers: {} };
    for (const [path, value] of Object.entries(config)) setPath(server.config, path, value);
    const bump = () => { server.revision = 'r' + (++server.writes + 1); };
    const derive = () => {
        const role = server.config.roles.conversation;
        if (role.provider) role.url = conversationUrl(role);
        const problem = addressProblem(role.url, role.remote);
        if (problem) throw new Error(problem);
    };
    const answer = extra => ({ ok: true, config: plain(server.config), revision: server.revision, readiness: plain(server.readiness || []),
        credentials: plain(server.credentials), decision_backends: [], providers: [], native: {}, ...extra });
    const host = {
        chatProviders: [{ value: 'anthropic', label: 'Anthropic' }, { value: 'google', label: 'Google' }, { value: 'lm_studio', label: 'LM Studio' },
                        { value: 'openai', label: 'OpenAI' }, { value: 'openrouter', label: 'OpenRouter' }, { value: 'other', label: 'Other OpenAI compatible' },
                        { value: 'newcomer', label: 'Newcomer AI' }],
        apiKeyStatus: { ...hostKeys }, apiKeyValues: {}, apiKeyDirty: {}, saved: [], searches: [], refreshed: 0, revealed: 0, loaded: 0,
        async ensureLoaded() { this.loaded++; },
        async refreshApiKeyStatus() { this.refreshed++; },
        setApiKeyValue(provider, value) { this.apiKeyValues = { ...this.apiKeyValues, [provider]: value }; this.apiKeyDirty = { ...this.apiKeyDirty, [provider]: true }; },
        async saveApiKey(provider, value) { this.saved.push([provider, value]); this.apiKeyStatus = { ...this.apiKeyStatus, [provider]: !!value.trim() }; this.apiKeyValues = { ...this.apiKeyValues, [provider]: value }; this.apiKeyDirty = { ...this.apiKeyDirty, [provider]: false }; },
        async revealApiKey() { this.revealed++; return 'never-shown'; },
        async searchModels(...args) { this.searches.push(args); return ['gpt-4o-mini-audio-preview', 'gpt-4o-audio-preview', 'gpt-4.1']; },
        groupResults: groupModels,
    };
    const bindings = hostStubs({
        '/js/api.js': { callJsonApi: async (_url, payload) => {
            server.calls.push(plain(payload));
            if (server.hold[payload.action]) await server.hold[payload.action];
            switch (payload.action) {
                case 'setup_patch': {
                    const conflicts = [], applied = [], before = plain(server.config);
                    for (const change of payload.changes) {
                        const now = getPath(server.config, change.path);
                        if (JSON.stringify(now) === JSON.stringify(change.base)) { setPath(server.config, change.path, change.value); applied.push(change.path); }
                        else conflicts.push({ path: change.path, mine: change.value ?? null, theirs: now ?? null });
                    }
                    try { derive(); } catch (error) { server.config = before; return { ok: false, code: 'invalid', error: error.message }; }
                    if (applied.length) bump();
                    return answer({ conflicts, applied });
                }
                case 'setup_save': {
                    if (payload.revision !== server.revision) return { ok: false, code: 'conflict', error: 'Settings changed in another view.' };
                    server.config = plain(payload.config); derive();
                    for (const [role, value] of Object.entries(payload.keys || {})) { server.keys[role] = value; server.credentials[role] = !!value; }
                    bump();
                    return answer();
                }
                case 'discover': return { ok: true, models: server.models.map(id => ({ id, label: id })), voices: [], tuning: [], warnings: [], verification: { state: 'unverified', message: 'listed' } };
                case 'conversation_key_copy': return server.answers.copy || { ok: true, copied: true, provider: payload.provider, message: 'Copied Convo’s key.' };
                case 'audio_probe': return server.answers.probe || { ok: true, passed: true, code: 'passed', message: 'Accepted.', reply: 'OK' };
                default: return answer();
            }
        } },
        '/plugins/_model_config/webui/model-config-store.js': { store: host },
    }, { reactive });
    const { namespace } = await loadModule('webui/setup-store.js', { bindings, globals: { window: { confirm: () => true }, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, URL } });
    const store = namespace.store;
    await store.load();
    await store.conversationInit();
    return {
        store, server, clock, host,
        role: () => store.config.roles.conversation,
        sent: action => server.calls.filter(call => call.action === action),
        elsewhere(path, value) { setPath(server.config, path, value); derive(); bump(); },
    };
}

// ---- Host contract (verbatim v2.11 markup)
test('The host markup the mount relies on is what the v2.11 preset editor and overviews really render', async () => {
    const editor = await fixture('host-preset-editor.html'), overview = await fixture('host-preset-overview.html'), summary = await fixture('host-models-summary.html');
    assert.match(editor, /<template x-if="selectedPreset">\s*<div class="preset-editor-body">/);               // a body per rendered preset
    assert.equal((editor.match(/<section class="preset-model-section">/g) || []).length, 3);                  // Main, Utility, Embedding
    assert.match(editor, /<div class="preset-model-heading">\s*<div class="section-title">Main Model<\/div>\s*<div class="section-description">/);
    assert.match(editor, /\.preset-name-field,\s*\.preset-model-section \{\s*padding: 1rem;\s*border: 1px solid var\(--color-border\);/);
    assert.match(editor, /<div class="modal-footer" data-modal-footer>/);
    assert.match(overview, /<div class="model-preset-rows" aria-live="polite">\s*<template x-for="model in modelRows" :key="model\.title">/);
    for (const name of ['model-preset-row', 'model-preset-icon', 'model-preset-role', 'model-preset-identity', 'model-preset-provider', 'model-preset-separator'])
        assert.match(overview, new RegExp(`class="${name}"`), name);
    assert.match(overview, /\.model-preset-row \+ \.model-preset-row \{\s*border-top: 1px solid var\(--color-border\);/);   // a row we add is ruled like the host's
    assert.match(summary, /<x-component path="\/plugins\/_model_config\/webui\/preset-overview\.html"><\/x-component>/);
});

test('Only the preset editor and the modals that can show a preset overview are watched', () => {
    for (const path of ['/plugins/_model_config/webui/main.html', 'plugins/_model_config/webui/main.html', '/plugins/_model_config/webui/main.html?v=2']) assert.equal(watchedModal(path), 'editor', path);
    for (const path of ['settings/settings.html', '/components/plugins/plugin-settings.html']) assert.equal(watchedModal(path), 'summary', path);
    for (const path of ['/plugins/_model_config/webui/api-keys.html', '/plugins/_convo/webui/settings-modal.html', 'settings/agent/workdir-file-structure-test.html', '', null, undefined])
        assert.equal(watchedModal(path), null, String(path));
});

// ---- Injection
async function editorScene({ renderFirst = false } = {}) {
    const doc = new FakeDocument(), Observer = observerClass(doc), alpine = fakeAlpine(), f = await rig();
    const modal = openModal(doc, '/plugins/_model_config/webui/main.html', await fixture('host-preset-editor.html'));
    if (renderFirst) render(modal.body);
    const loads = [];
    const realLoad = f.store.load.bind(f.store);
    f.store.load = force => { loads.push(!!force); return realLoad(force); };
    const handle = attach({ modalPath: modal.path, modal, doc: null }, { store: f.store, document: doc, MutationObserver: Observer, Alpine: alpine });
    return { doc, Observer, alpine, f, modal, handle, loads };
}
const sections = root => root.querySelectorAll(`[${SECTION_ATTRIBUTE}]`);

test('The preset editor gets exactly one Convo section, in the body Alpine renders, however the hook and Alpine are ordered', async () => {
    for (const renderFirst of [false, true]) {
        const s = await editorScene({ renderFirst });
        assert.ok(s.handle, 'attached');
        assert.deepEqual([s.handle.observer.target === s.modal.element, plain(s.handle.observer.options)], [true, { childList: true, subtree: true }]);   // this modal only
        if (!renderFirst) render(s.modal.body);                                     // Alpine renders after the hook: the observer mounts it
        await settle();
        const body = s.modal.element.querySelector('.preset-editor-body');
        assert.equal(sections(s.modal.element).length, 1, `renderFirst=${renderFirst}`);
        const section = body.querySelector(`:scope > [${SECTION_ATTRIBUTE}]`);
        assert.equal(body.children.at(-1), section);                               // after Main, Utility and Embedding
        assert.equal(section.className, 'preset-model-section convo-model-type');
        assert.equal(section.querySelector('.section-title').textContent, 'Convo conversation model');
        assert.equal(section.querySelector('.section-description').textContent, 'One setting for all presets; changes save automatically.');
        assert.equal(section.getAttribute('aria-labelledby'), section.querySelector('.section-title').getAttribute('id'));
        const card = section.querySelector('x-component');
        assert.equal(card.getAttribute('path'), CARD_PATH);
        assert.equal(card.parentElement.getAttribute('x-data'), '{ convoAutosave: true }');
        assert.deepEqual(s.loads, [true]);                                         // a clean draft is read fresh, once
    }
});

test('Scans after the section exists write nothing, and a second hook call for the same modal attaches nothing', async () => {
    const s = await editorScene({ renderFirst: true });
    await settle();
    const writes = s.doc.writes, calls = s.handle.observer.calls;
    for (let i = 0; i < 5; i++) s.handle.scan();
    const unrelated = s.doc.createElement('div'); s.modal.element.querySelector('.preset-editor').append(unrelated);   // the host re-rendering something
    await settle();
    assert.equal(s.handle.observer.calls, calls + 1);                             // the observer saw it ...
    assert.equal(s.doc.writes, writes + 1);                                       // ... and wrote nothing of its own (only the host's one append)
    assert.equal(sections(s.modal.element).length, 1);
    assert.equal(attach({ modalPath: s.modal.path, modal: s.modal }, { store: s.f.store, document: s.doc, MutationObserver: s.Observer, Alpine: s.alpine }), null);
    assert.equal(s.Observer.made.length, 1);
});

test('Switching presets keeps the one section; a body Alpine renders anew gets exactly one of its own', async () => {
    const s = await editorScene({ renderFirst: true });
    await settle();
    const select = s.modal.element.querySelector('.preset-editor-toolbar select');
    select.setAttribute('data-selected', '2');                                   // x-model.number="selectedKey": the same body stays
    await settle();
    assert.equal(sections(s.modal.element).length, 1);
    const template = s.modal.element.querySelector('.preset-editor').children.find(node => node.localName === 'template' && node.getAttribute('x-if') === 'selectedPreset');
    const oldBody = s.modal.element.querySelector('.preset-editor-body');
    oldBody.remove();                                                             // x-if false, then true again: a fresh clone
    const fresh = template.content.children[0].cloneNode(true);
    template.after(fresh); render(fresh);
    await settle();
    assert.equal(sections(s.modal.element).length, 1);
    assert.ok(fresh.querySelector(`:scope > [${SECTION_ATTRIBUTE}]`));
    assert.equal(sections(oldBody).length, 1);                                   // the detached old body kept its own, no longer shown
});

test('Closing saves what is still waiting and releases everything; reopening mounts exactly one section again', async () => {
    const s = await editorScene({ renderFirst: true });
    await settle();
    let flushes = 0;
    s.f.store.conversationEditorClosed = async () => { flushes++; };
    const other = openModal(s.doc, '/plugins/_model_config/webui/api-keys.html', '<html><body><div></div></body></html>');
    closeModal(s.doc, other);                                                     // another modal closing changes nothing here
    assert.deepEqual([flushes, s.handle.observer.disconnected, s.doc.listeners.get('modal-closed').size], [0, undefined, 1]);
    closeModal(s.doc, s.modal);
    await settle();
    assert.deepEqual([flushes, s.handle.observer.disconnected, s.doc.listeners.get('modal-closed').size, s.alpine.effects.size], [1, true, 0, 0]);
    s.handle.scan();
    const again = openModal(s.doc, '/plugins/_model_config/webui/main.html', await fixture('host-preset-editor.html'));
    attach({ modalPath: again.path, modal: again }, { store: s.f.store, document: s.doc, MutationObserver: s.Observer, Alpine: s.alpine });
    render(again.body);
    await settle();
    assert.equal(sections(again.element).length, 1);
    assert.equal(sections(s.doc.body).length, 1);
});

test('An unsaved Convo draft is never reloaded over when the editor opens', async () => {
    const doc = new FakeDocument(), f = await rig();
    f.store.config.style = 'typed in the Convo modal, not saved';
    const loads = [], realLoad = f.store.load.bind(f.store);
    f.store.load = force => { loads.push(!!force); return realLoad(force); };
    const modal = openModal(doc, '/plugins/_model_config/webui/main.html', await fixture('host-preset-editor.html'));
    attach({ modalPath: modal.path, modal }, { store: f.store, document: doc, MutationObserver: observerClass(doc), Alpine: fakeAlpine() });
    await settle();
    assert.deepEqual(loads, [false]);
    assert.equal(f.store.config.style, 'typed in the Convo modal, not saved');
});

// ---- Summary rows
async function settingsScene(config = { 'roles.conversation.provider': 'openrouter', 'roles.conversation.remote': true, 'roles.conversation.model': 'google/gemini-2.5-flash' }) {
    const doc = new FakeDocument(), Observer = observerClass(doc), alpine = fakeAlpine(), f = await rig({ config });
    const modal = openModal(doc, 'settings/settings.html', '<html><body><div class="settings-tab-panel" data-settings-tab="agent"><div id="section-models-summary" class="section"></div></div></body></html>');
    const handle = attach({ modalPath: modal.path, modal }, { store: f.store, document: doc, MutationObserver: Observer, Alpine: alpine });
    const section = modal.element.querySelector('#section-models-summary');
    loadComponentInto(doc, section, await fixture('host-models-summary.html'));    // the Agent tab's Models section
    render(section, 0);
    loadComponentInto(doc, section.querySelector('x-component'), await fixture('host-preset-overview.html'));
    return { doc, Observer, alpine, f, modal, handle, section };
}

test('Settings → Agent → Models shows one "Convo — provider / model" row, after the host rows, with the saved choice', async () => {
    const s = await settingsScene();
    await settle();
    const list = s.modal.element.querySelector('.model-preset-rows');
    assert.ok(list.querySelector(`:scope > .${ROW_CLASS}`), 'the row is there before the host renders its own');
    render(list.parentElement, 3);                                                // Alpine renders Main, Utility, Embedding after its template
    await settle();
    const rows = list.children.filter(node => node.className.split(' ').includes('model-preset-row'));
    assert.equal(rows.length, 4);
    assert.equal(rows.at(-1).className, 'model-preset-row ' + ROW_CLASS);
    assert.equal(list.querySelectorAll(`.${ROW_CLASS}`).length, 1);
    const row = rows.at(-1);
    assert.equal(row.querySelector('.model-preset-role').textContent, 'Convo');
    assert.equal(row.querySelector('x-icon').getAttribute('aria-hidden'), 'true');
    const identity = row.querySelector('.model-preset-identity');
    assert.deepEqual(identity.children.map(node => [node.className, node.textContent]), [['model-preset-provider', 'OpenRouter'], ['model-preset-separator', '/'], ['', 'google/gemini-2.5-flash']]);
});

test('A summary row is repainted only when the saved choice changes, never for a draft or an unchanged save', async () => {
    const s = await settingsScene();
    await settle();
    const row = s.modal.element.querySelector(`.${ROW_CLASS}`), identity = () => row.querySelector('.model-preset-identity').textContent;
    let writes = s.doc.writes;
    s.alpine.run();                                                               // the effect re-runs with nothing changed
    assert.equal(s.doc.writes, writes);
    s.f.store.config.roles.conversation.model = 'draft-only';                    // an unsaved edit is not what Convo uses
    s.alpine.run();
    assert.equal(identity(), 'OpenRouter/google/gemini-2.5-flash');
    await s.f.store.patch({ 'roles.conversation.model': 'google/gemini-2.5-pro' });
    s.alpine.run();
    assert.equal(identity(), 'OpenRouter/google/gemini-2.5-pro');
    writes = s.doc.writes;
    await s.f.store.patch({ 'style': 'unrelated' });                             // saved, but the summary is the same
    s.alpine.run();
    assert.equal(s.doc.writes, writes);
    const fresh = s.doc.createElement('div'), list = s.modal.element.querySelector('.model-preset-rows');
    assert.equal(mountSummary(list, s.doc), row);                                 // idempotent
    // Found live: the host re-renders its rows after the Convo row was placed. The row moves back to the end (once: no loop).
    list.appendChild(s.doc.createElement('div'));
    assert.notEqual(list.lastElementChild, row);
    assert.equal(mountSummary(list, s.doc), row);
    assert.equal(list.lastElementChild, row);
    const moves = s.doc.writes;
    assert.equal(mountSummary(list, s.doc), row);
    assert.equal(s.doc.writes, moves);                                             // already last: nothing is touched
    assert.equal(paintSummary(row, { provider: 'OpenRouter', model: 'google/gemini-2.5-pro' }, s.doc), false);
    assert.equal(paintSummary(row, null, s.doc), true);                          // nothing saved yet: the host's dashes
    assert.equal(identity(), '—/—');
    assert.equal(fresh.isConnected, false);
});

test('No row appears before the saved settings are read, so the live region never announces an empty choice', async () => {
    const doc = new FakeDocument(), alpine = fakeAlpine();
    let release; const held = new Promise(resolve => { release = resolve; });
    const store = { config: null, baseline: '', dirty: false, loads: 0,
        load() { this.loads++; return held.then(() => { this.config = {}; }); },
        conversationSummary() { return this.config ? { provider: 'Google', model: 'gemini-2.5-flash' } : null; } };
    const modal = openModal(doc, 'settings/settings.html', '<html><body><div class="host"></div></body></html>');
    attach({ modalPath: modal.path, modal }, { store, document: doc, MutationObserver: observerClass(doc), Alpine: alpine });
    loadComponentInto(doc, modal.element.querySelector('.host'), await fixture('host-preset-overview.html'));
    await settle();
    assert.deepEqual([store.loads, modal.element.querySelectorAll(`.${ROW_CLASS}`).length], [1, 0]);
    release();
    await settle();
    const rows = modal.element.querySelectorAll(`.${ROW_CLASS}`);
    assert.equal(rows.length, 1);
    assert.equal(rows[0].querySelector('.model-preset-identity').textContent, 'Google/gemini-2.5-flash');
    assert.equal(store.loads, 1);                                                 // read once however often the observer ran
});

test('A plugin Config modal is watched only while it shows the model settings plugin', async () => {
    const doc = new FakeDocument(), f = await rig();
    let reads = 0; const realLoad = f.store.load.bind(f.store); f.store.load = force => { reads++; return realLoad(force); };
    const theirs = openModal(doc, '/components/plugins/plugin-settings.html', '<html><body><div class="plugin-settings-body"></div></body></html>');
    theirs.element.__pluginSettingsContext = { pluginName: '_convo' };
    assert.equal(attach({ modalPath: theirs.path, modal: theirs }, { store: f.store, document: doc, MutationObserver: observerClass(doc), Alpine: fakeAlpine() }), null);
    const models = openModal(doc, '/components/plugins/plugin-settings.html', '<html><body><div class="plugin-settings-body"></div></body></html>');
    const handle = attach({ modalPath: models.path, modal: models }, { store: f.store, document: doc, MutationObserver: observerClass(doc), Alpine: fakeAlpine() });
    assert.equal(reads, 0);                                                       // nothing read before an overview shows
    models.element.__pluginSettingsContext = { pluginName: '_model_config' };
    loadComponentInto(doc, models.element.querySelector('.plugin-settings-body'), await fixture('host-preset-overview.html'));
    await settle();
    assert.equal(reads, 1);
    assert.equal(models.element.querySelectorAll(`.${ROW_CLASS}`).length, 1);
    assert.equal(theirs.element.querySelectorAll(`.${ROW_CLASS}`).length, 0);
    assert.ok(handle);
});

test('The hook loads nothing for other modals and mounts the section through the real store for the preset editor', async () => {
    const doc = new FakeDocument(), Observer = observerClass(doc), alpine = fakeAlpine();
    const calls = [];
    const bindings = hostStubs({ '/js/api.js': { callJsonApi: async (_url, payload) => { calls.push(payload.action); return { ok: true, config: plain(defaults), revision: 'r1', readiness: [], credentials: {} }; } } }, { reactive: true });
    const { namespace } = await loadModule('extensions/webui/modal_content_loaded/_20_convo_model_type.js', { bindings, globals: { document: doc, MutationObserver: Observer, Alpine: alpine, window: { confirm: () => true }, URL } });
    const other = openModal(doc, '/plugins/_convo/webui/settings-modal.html', '<html><body><div></div></body></html>');
    await namespace.default({ modalPath: other.path, modal: other, doc: null });
    assert.deepEqual([calls, Observer.made.length], [[], 0]);
    const modal = openModal(doc, '/plugins/_model_config/webui/main.html', await fixture('host-preset-editor.html'));
    await namespace.default({ modalPath: modal.path, modal, doc: null });
    render(modal.body);
    await settle();
    assert.equal(sections(modal.element).length, 1);
    assert.deepEqual(calls, ['setup_read']);
});

// ---- The card's behaviour on the real store
test('An edit in the preset-editor copy is saved once, 350 ms after the last change, against what the editor loaded', async () => {
    const f = await rig();
    f.role().model = 'omni-a'; f.store.conversationEdited('roles.conversation.model', true);
    await f.clock.advance(200);
    f.role().model = 'omni-ab'; f.store.conversationEdited('roles.conversation.model', true);
    await f.clock.advance(AUTOSAVE_MS - 1);
    assert.equal(f.sent('setup_patch').length, 0);
    assert.equal(f.store.conversationStatusText(), 'Unsaved changes…');
    await f.clock.advance(1);
    assert.deepEqual(f.sent('setup_patch').map(call => call.changes), [[{ path: 'roles.conversation.model', base: '', value: 'omni-ab' }]]);
    assert.deepEqual([f.store.conversationStatusText(), f.store.dirty, f.server.config.roles.conversation.model], ['Saved', false, 'omni-ab']);
});

test('Nothing is written when nothing changed, and the Convo modal copy never autosaves', async () => {
    const f = await rig();
    f.role().model = 'x'; f.role().model = ''; f.store.conversationEdited('roles.conversation.model', true);     // back to what is saved
    await f.clock.advance(1000);
    f.role().model = 'typed in Connections'; f.store.conversationEdited('roles.conversation.model', false);
    await f.clock.advance(1000);
    assert.deepEqual([f.sent('setup_patch').length, f.clock.pending, f.store.dirty], [0, 0, true]);
    assert.equal(await f.store.conversationFlush(), null);                       // closing the editor: still nothing it edited
    assert.equal(f.sent('setup_patch').length, 0);
});

test('An unsaved edit elsewhere in Convo survives the autosave and is never sent with it', async () => {
    const f = await rig();
    f.store.config.style = 'unsaved persona'; f.store.setKey('conversation', 'typed-key');
    f.role().model = 'omni'; f.store.conversationEdited('roles.conversation.model', true);
    await f.clock.advance(AUTOSAVE_MS);
    const [call] = f.sent('setup_patch');
    assert.deepEqual(call.changes.map(change => change.path), ['roles.conversation.model']);
    assert.doesNotMatch(JSON.stringify(f.server.calls), /typed-key|unsaved persona/);
    assert.deepEqual([f.store.config.style, f.store.keys.conversation, f.store.dirty], ['unsaved persona', 'typed-key', true]);
});

test('A field changed elsewhere is shown inline, is not resent by later saves, and is saved again only when kept', async () => {
    const f = await rig();
    f.elsewhere('roles.conversation.model', 'their-model');
    f.role().model = 'my-model'; f.store.conversationEdited('roles.conversation.model', true);
    await f.clock.advance(AUTOSAVE_MS);
    assert.deepEqual(plain(f.store.conversationConflicts()), [{ path: 'roles.conversation.model', mine: 'my-model', theirs: 'their-model', label: 'Model name' }]);
    assert.equal(f.store.conversationStatusText(), 'Changed elsewhere: choose below which value to keep.');
    assert.equal(f.server.config.roles.conversation.model, 'their-model');
    f.store.config.compaction.context_tokens = 32768; f.store.conversationEdited('compaction.context_tokens', true);
    await f.clock.advance(AUTOSAVE_MS);
    assert.deepEqual(f.sent('setup_patch').at(-1).changes, [{ path: 'compaction.context_tokens', base: 16384, value: 32768 }]);   // the model waits for a choice
    f.store.conversationKeepMine('roles.conversation.model');
    await f.clock.advance(AUTOSAVE_MS);
    assert.deepEqual(f.sent('setup_patch').at(-1).changes, [{ path: 'roles.conversation.model', base: 'their-model', value: 'my-model' }]);
    assert.deepEqual([f.server.config.roles.conversation.model, f.store.conversationConflicts().length, f.store.conversationStatusText()], ['my-model', 0, 'Saved']);
    f.elsewhere('roles.conversation.model', 'theirs-again');
    f.role().model = 'mine-again'; f.store.conversationEdited('roles.conversation.model', true);
    await f.clock.advance(AUTOSAVE_MS);
    f.store.conversationTakeSaved('roles.conversation.model');
    await f.clock.advance(AUTOSAVE_MS);
    assert.deepEqual([f.role().model, f.store.dirty, f.sent('setup_patch').length], ['theirs-again', false, 4]);
});

test('Typing on while a save is in flight is this editor\'s own newer edit, not a conflict', async () => {
    const f = await rig();
    let release; f.server.hold.setup_patch = new Promise(resolve => { release = resolve; });
    f.role().model = 'omni'; f.store.conversationEdited('roles.conversation.model', true);
    await f.clock.advance(AUTOSAVE_MS);
    f.role().model = 'omni-2'; f.store.conversationEdited('roles.conversation.model', true);
    delete f.server.hold.setup_patch; release();
    await settle();
    assert.deepEqual([f.store.conversationConflicts().length, f.role().model], [0, 'omni-2']);
    await f.clock.advance(AUTOSAVE_MS);
    assert.deepEqual(f.sent('setup_patch').map(call => call.changes[0]), [{ path: 'roles.conversation.model', base: '', value: 'omni' }, { path: 'roles.conversation.model', base: 'omni', value: 'omni-2' }]);
    assert.equal(f.store.dirty, false);
});

test('Closing the editor saves a change still inside its 350 ms at once, and only once; what could not be saved stays a draft', async () => {
    const f = await rig();
    f.role().api_base = 'unused'; f.role().model = 'closing-now'; f.store.conversationEdited('roles.conversation.model', true);
    await f.store.conversationEditorClosed();
    assert.deepEqual(f.sent('setup_patch').map(call => call.changes.map(change => change.path)), [['roles.conversation.model']]);
    await f.store.conversationFlush();
    await f.clock.advance(1000);
    assert.equal(f.sent('setup_patch').length, 1);
    f.store.conversationSetProvider('google', true);                              // held: no remote consent yet
    await f.store.conversationEditorClosed();
    assert.equal(f.sent('setup_patch').length, 1);
    assert.deepEqual([f.role().provider, f.store.dirty, plain(f.store.ui.conversation.edited), f.store.conversationStatusText()], ['google', true, {}, 'Changes save automatically.']);
    f.role().remote = true;                                                       // consented later in Convo's own modal ...
    await f.store.conversationFlush();
    assert.equal(f.sent('setup_patch').length, 1);                                // ... is saved by that modal's Save, not by an old mark
});

test('A value the server would refuse waits with its reason: remote consent, a complete address, a usable context window', async () => {
    const f = await rig();
    assert.equal(f.store.conversationSetProvider('openai', true), 'openai');
    await f.clock.advance(1000);
    assert.equal(f.sent('setup_patch').length, 0);
    assert.match(f.store.conversationStatusText(), /api\.openai\.com needs remote-processing consent/);
    f.role().model = 'gpt-4o-audio-preview'; f.store.conversationEdited('roles.conversation.model', true);
    await f.clock.advance(AUTOSAVE_MS);
    assert.deepEqual(f.sent('setup_patch').map(call => call.changes.map(change => change.path)), [['roles.conversation.model']]);   // the rest still goes
    f.role().remote = true; f.store.conversationEdited('roles.conversation.remote', true);
    await f.clock.advance(AUTOSAVE_MS);
    assert.deepEqual(f.sent('setup_patch').at(-1).changes.map(change => change.path).sort(), ['roles.conversation.provider', 'roles.conversation.remote']);
    assert.deepEqual([f.server.config.roles.conversation.url, f.role().url, f.store.conversationStatusText()], ['https://api.openai.com/v1', 'https://api.openai.com/v1', 'Saved']);
    f.store.conversationSetProvider('other', true);
    f.role().api_base = 'http:/'; f.store.conversationEdited('roles.conversation.api_base', true);
    await f.clock.advance(AUTOSAVE_MS);
    assert.match(f.store.conversationStatusText(), /complete http\(s\) address/);
    f.role().api_base = ' http://host.docker.internal:8080/v1 '; f.store.conversationEdited('roles.conversation.api_base', true);
    await f.clock.advance(AUTOSAVE_MS);
    assert.deepEqual(f.sent('setup_patch').at(-1).changes.find(change => change.path === 'roles.conversation.api_base').value, 'http://host.docker.internal:8080/v1');   // trimmed
    for (const tokens of ['', 2048, 4096.5]) {                                    // a cleared field, too small, not a whole number
        f.store.config.compaction.context_tokens = tokens; f.store.conversationEdited('compaction.context_tokens', true);
        await f.clock.advance(AUTOSAVE_MS);
        assert.match(f.store.conversationStatusText(), /at least 4096 tokens/, String(tokens));
    }
    assert.equal(f.server.calls.filter(call => call.action === 'setup_patch' && call.changes.some(change => change.path === 'compaction.context_tokens')).length, 0);
    f.store.config.compaction.context_tokens = 4096; f.store.conversationEdited('compaction.context_tokens', true);
    await f.clock.advance(AUTOSAVE_MS);
    assert.equal(f.server.config.compaction.context_tokens, 4096);
    assert.equal(f.server.calls.some(call => call.ok === false), false);
});

test('An address or key typed into the credential reference waits with the reason, is never sent, and Clear saves it empty', async () => {
    const f = await rig({ config: { 'roles.conversation.token_env': 'http://host.docker.internal:8081/v1' } });   // the 2026-10-31 lockout
    assert.match(f.store.conversationReferenceProblem(), /looks like an address/);
    f.role().model = 'gemma'; f.store.conversationEdited('roles.conversation.model', true);
    await f.clock.advance(AUTOSAVE_MS);
    assert.deepEqual(f.sent('setup_patch').map(call => call.changes.map(change => change.path)), [['roles.conversation.model']]);   // other fields still save
    f.role().token_env = 'sk-or-v1-0123456789abcdef0123'; f.store.conversationEdited('roles.conversation.token_env', true);
    await f.clock.advance(AUTOSAVE_MS);
    assert.match(f.store.conversationStatusText(), /looks like a key itself/);
    assert.equal(f.server.calls.some(call => JSON.stringify(call).includes('sk-or-v1')), false);   // never sent anywhere
    f.store.conversationClearReference(true);
    await f.clock.advance(AUTOSAVE_MS);
    assert.deepEqual(f.sent('setup_patch').at(-1).changes, [{ path: 'roles.conversation.token_env', base: 'http://host.docker.internal:8081/v1', value: '' }]);
    assert.equal(f.store.conversationReferenceProblem(), '');
});

test('A server you run yourself (llama.cpp) shows its address as a main field, with how to reach another computer', async () => {
    const f = await rig();
    f.store.conversationSetProvider('llama_cpp', true);
    assert.equal(f.store.conversationLocalServer(), true);
    assert.match(f.store.conversationServerHint(), /Leave it empty for this machine \(http:\/\/host\.docker\.internal:8080\/v1\).*LAN or Tailscale/);
    f.role().api_base = 'http://100.101.102.103:8081/v1'; f.store.conversationEdited('roles.conversation.api_base', true);
    await f.clock.advance(AUTOSAVE_MS);
    assert.equal(f.sent('setup_patch').at(-1).changes.find(change => change.path === 'roles.conversation.api_base').value, 'http://100.101.102.103:8081/v1');
    assert.equal(f.store.conversationRemoteShown(), false);                          // no consent box for your tailnet
    f.store.conversationSetProvider('openrouter', true);
    assert.equal(f.store.conversationLocalServer(), false);                         // a cloud provider keeps its override in Advanced
});

test('Choosing a provider: disabled ones are refused, Other keeps the endpoint used so far, and each base is remembered', async () => {
    const f = await rig({ config: { 'roles.conversation.url': 'http://host.docker.internal:8080/v1' } });
    assert.equal(f.store.conversationSetProvider('lm_studio', true), '');
    assert.equal(f.store.conversationSetProvider('made_up', true), '');
    assert.equal(f.role().provider, '');
    f.store.conversationSetProvider('other', true);
    assert.equal(f.role().api_base, 'http://host.docker.internal:8080/v1');     // the address and the key saved for it carry over
    await f.clock.advance(AUTOSAVE_MS);
    assert.deepEqual(f.sent('setup_patch')[0].changes, [{ path: 'roles.conversation.provider', base: '', value: 'other' }, { path: 'roles.conversation.api_base', base: '', value: 'http://host.docker.internal:8080/v1' }]);
    f.role().remote = true; f.store.conversationEdited('roles.conversation.remote', true);
    f.store.conversationSetProvider('openrouter', true);
    assert.equal(f.role().api_base, '');                                          // OpenRouter's own address, not the local server
    f.store.conversationSetProvider('other', true);
    assert.equal(f.role().api_base, 'http://host.docker.internal:8080/v1');
});

test('The provider list: Convo\'s four, every other provider disabled with its reason, and a saved unusable choice stays visible', async () => {
    const f = await rig();
    const options = f.store.conversationProviderOptions();
    assert.deepEqual(plain(options.filter(option => !option.disabled).map(option => option.id)), ['', 'openai', 'openrouter', 'google', 'llama_cpp', 'vllm', 'other']);
    const disabled = Object.fromEntries(options.filter(option => option.disabled).map(option => [option.id, option]));
    assert.equal(disabled.lm_studio.label, 'LM Studio (no audio input)');        // Agent Zero's own label, the short reason ...
    assert.equal(disabled.lm_studio.reason, CONVERSATION_CATALOG.reasons.text.reason);   // ... and the full one
    assert.equal(disabled.anthropic.reason, CONVERSATION_CATALOG.reasons.route.reason);
    assert.equal(disabled.mistral.reason, CONVERSATION_CATALOG.reasons.format.reason);
    assert.equal(disabled.newcomer.reason, CONVERSATION_CATALOG.unknown.reason);  // a provider Convo does not know yet
    for (const id of Object.keys(CONVERSATION_CATALOG.disabled)) assert.ok(disabled[id], id);
    const g = await rig({ config: { 'roles.conversation.provider': 'made_up' } });
    const saved = g.store.conversationProviderOptions().find(option => option.id === 'made_up');
    assert.deepEqual([saved.disabled, saved.reason], [true, CONVERSATION_CATALOG.unknown.reason]);
    assert.equal(g.store.conversationProviderOptions().some(option => option.id === ''), false);   // no endpoint-only choice once a provider is saved
    assert.deepEqual([g.store.conversationProviderInfo().selectable, g.store.conversationProviderInfo().reason], [false, CONVERSATION_CATALOG.unknown.reason]);
});

test('Agent Zero\'s key is entered the Agent Zero way, saved on leaving the field, never revealed and never sent to Convo', async () => {
    const f = await rig({ config: { 'roles.conversation.provider': 'openai', 'roles.conversation.remote': true } });
    assert.deepEqual([f.store.conversationUsesHostKey(), f.store.conversationUsesConvoKey(), f.store.conversationHasHostKey()], [true, false, false]);
    f.store.conversationHostKeyInput('sk-live-check-123');
    assert.deepEqual([f.host.apiKeyValues.openai, f.host.apiKeyDirty.openai], ['sk-live-check-123', true]);
    assert.equal(await f.store.conversationSaveHostKey(), true);
    assert.deepEqual(f.host.saved, [['openai', 'sk-live-check-123']]);
    assert.deepEqual([f.host.apiKeyValues.openai, f.host.apiKeyDirty.openai, f.store.conversationHasHostKey()], ['', false, true]);   // nothing lingers
    assert.match(f.store.ui.conversation.hostKeyNote, /Saved as Agent Zero’s OpenAI key/);
    f.store.conversationHostKeyInput('   ');
    assert.equal(await f.store.conversationSaveHostKey(), false);                // an emptied field never removes Agent Zero's key
    assert.deepEqual([f.host.saved.length, f.host.apiKeyDirty.openai], [1, false]);
    assert.equal(f.host.revealed, 0);
    assert.doesNotMatch(JSON.stringify(f.server.calls), /sk-live-check/);
    f.role().key_source = 'convo';
    assert.deepEqual([f.store.conversationUsesHostKey(), f.store.conversationUsesConvoKey()], [false, true]);
    f.role().key_source = 'provider'; f.role().api_base = 'https://proxy.example/v1';
    assert.match(f.store.conversationRouteWarning(), /OpenAI key will be sent to proxy\.example/);
    f.role().token_env = 'OPENAI_FROM_ENV';
    assert.equal(f.store.conversationRouteWarning(), '');
});

test('The key copy is offered only for an empty Agent Zero slot and a saved provider, and reports without any key', async () => {
    const f = await rig({ config: { 'roles.conversation.provider': 'openrouter', 'roles.conversation.remote': true }, credentials: { conversation: true } });
    assert.equal(f.store.conversationCanCopyKey(), true);
    f.host.apiKeyStatus = { openrouter: true };
    assert.equal(f.store.conversationCanCopyKey(), false);                       // never over a key
    f.host.apiKeyStatus = {};
    f.role().provider = 'openai';
    assert.equal(f.store.conversationCanCopyKey(), false);                       // the draft provider is not saved yet
    f.role().provider = 'openrouter';
    f.store.config.compaction.context_tokens = 32768; f.store.conversationEdited('compaction.context_tokens', true);   // still inside its 350 ms
    assert.equal(await f.store.conversationCopyKey(), true);
    assert.deepEqual(f.server.calls.map(call => call.action).filter(action => ['setup_patch', 'conversation_key_copy'].includes(action)), ['setup_patch', 'conversation_key_copy']);   // what it is bound to lands first
    assert.deepEqual(f.sent('conversation_key_copy').map(call => call.provider), ['openrouter']);
    assert.equal(f.host.refreshed, 2);                                            // the card's start, then after the copy
    f.server.answers.copy = { ok: false, code: 'occupied', error: 'Agent Zero already holds an OpenRouter key; it was not changed.' };
    assert.equal(await f.store.conversationCopyKey(), false);
    assert.deepEqual(plain(f.store.ui.conversation.keyCopy), { busy: false, ok: false, message: 'Agent Zero already holds an OpenRouter key; it was not changed.' });
    const g = await rig({ credentials: { conversation: true } });
    assert.equal(g.store.conversationCanCopyKey(), false);                       // the endpoint-only setup keeps Convo's key
});

test('In the preset editor Convo\'s own key is saved by itself, after the pending change it is bound to', async () => {
    const f = await rig({ config: { 'roles.conversation.provider': 'other', 'roles.conversation.api_base': 'http://host.docker.internal:8080/v1' } });
    f.role().api_base = 'http://host.docker.internal:9090/v1'; f.store.conversationEdited('roles.conversation.api_base', true);
    f.store.config.style = 'unsaved persona';
    f.store.setKey('conversation', 'local-server-key');
    assert.equal(await f.store.conversationSaveConvoKey(), true);
    const actions = f.server.calls.map(call => call.action).filter(action => action.startsWith('setup_') && action !== 'setup_read');
    assert.deepEqual(actions, ['setup_patch', 'setup_save']);
    const save = f.sent('setup_save')[0];
    assert.deepEqual([save.keys, save.config.roles.conversation.api_base, save.config.style], [{ conversation: 'local-server-key' }, 'http://host.docker.internal:9090/v1', '']);
    assert.deepEqual([f.store.keys.conversation, f.store.config.style, f.store.credentials.conversation], [undefined, 'unsaved persona', true]);
});

test('Test audio input runs only on its click, once, against the saved settings', async () => {
    const f = await rig({ config: { 'roles.conversation.provider': 'openrouter', 'roles.conversation.remote': true, 'roles.conversation.model': 'google/gemini-2.5-flash' } });
    assert.equal(f.sent('audio_probe').length, 0);                                // loading and editing never probe
    f.role().model = 'unsaved-model';
    assert.equal(await f.store.conversationProbe(), false);
    assert.equal(f.sent('audio_probe').length, 0);
    assert.match(f.store.ui.conversation.probe.message, /Save your changes first/);
    f.role().model = 'google/gemini-2.5-flash';
    f.role().model = 'google/gemini-2.5-pro'; f.store.conversationEdited('roles.conversation.model', true);   // the preset editor's change, inside its 350 ms
    assert.equal(await f.store.conversationProbe(), true);
    assert.deepEqual(f.server.calls.map(call => call.action).filter(action => ['setup_patch', 'audio_probe'].includes(action)), ['setup_patch', 'audio_probe']);   // saved first, then tested
    assert.equal(f.server.config.roles.conversation.model, 'google/gemini-2.5-pro');
    assert.deepEqual(f.sent('audio_probe').map(call => call.provider), ['openrouter']);
    assert.deepEqual(plain(f.store.ui.conversation.probe), { busy: false, state: 'pass', message: 'Accepted.', reply: 'OK' });
    f.server.answers.probe = { ok: true, passed: false, code: 'refused', message: 'The provider refused the audio request (HTTP 400).' };
    assert.equal(await f.store.conversationProbe(), false);
    assert.equal(f.store.ui.conversation.probe.state, 'fail');
    f.server.answers.probe = { ok: false, code: 'busy', error: 'End the Convo voice session before testing audio input.' };
    await f.store.conversationProbe();
    assert.deepEqual([f.store.ui.conversation.probe.state, f.store.ui.conversation.probe.message], ['fail', 'End the Convo voice session before testing audio input.']);
});

test('Model search asks Agent Zero for its providers and Convo\'s own discovery for Other, grouped like the host', async () => {
    const f = await rig({ config: { 'roles.conversation.provider': 'openai', 'roles.conversation.remote': true, 'roles.conversation.api_base': 'https://proxy.example/v1' } });
    assert.deepEqual(await f.store.conversationSearch('audio'), ['gpt-4o-mini-audio-preview', 'gpt-4o-audio-preview', 'gpt-4.1']);
    assert.deepEqual(f.host.searches, [['openai', 'audio', 'chat', 'https://proxy.example/v1']]);
    assert.deepEqual(plain(f.store.conversationGroup(['gpt-4.1', 'gpt-4o-audio-preview'], 'AUDIO')), { matched: ['gpt-4o-audio-preview'], rest: ['gpt-4.1'] });
    f.store.conversationChooseModel('gpt-4o-audio-preview', true);
    await f.clock.advance(AUTOSAVE_MS);
    assert.equal(f.server.config.roles.conversation.model, 'gpt-4o-audio-preview');
    const g = await rig({ config: { 'roles.conversation.provider': 'other', 'roles.conversation.api_base': 'http://host.docker.internal:8080/v1' } });
    g.server.models = ['gemma-3n-e4b', 'qwen2.5-omni'];
    assert.deepEqual(await g.store.conversationSearch('omni'), ['gemma-3n-e4b', 'qwen2.5-omni']);
    assert.deepEqual([g.host.searches.length, g.sent('discover').length], [0, 1]);   // Agent Zero's key never goes to your own server
});

test('The summary is the saved choice in words', async () => {
    const f = await rig({ config: { 'roles.conversation.provider': 'google', 'roles.conversation.remote': true, 'roles.conversation.model': 'gemini-2.5-flash' } });
    assert.deepEqual(plain(f.store.conversationSummary()), { provider: 'Google', model: 'gemini-2.5-flash' });
    f.role().model = 'draft';
    assert.equal(f.store.conversationSummary().model, 'gemini-2.5-flash');
    const g = await rig({ config: { 'roles.conversation.provider': 'lm_studio' } });
    assert.deepEqual(plain(g.store.conversationSummary()), { provider: 'LM Studio (not usable)', model: '' });
    const h = await rig();
    assert.deepEqual(plain(h.store.conversationSummary()), { provider: 'Custom endpoint', model: '' });
});

test('The browser derives the same address and applies the same locality rule as the server', () => {
    const cases = [   // tests/test_model_catalog.py URL_CASES
        [{ provider: 'openai' }, 'https://api.openai.com/v1'], [{ provider: 'openrouter' }, 'https://openrouter.ai/api/v1'],
        [{ provider: 'google' }, 'https://generativelanguage.googleapis.com/v1beta/openai'], [{ provider: 'other' }, ''],
        [{ provider: 'other', api_base: ' http://host.docker.internal:8080/v1/ ' }, 'http://host.docker.internal:8080/v1'],
        [{ provider: 'openai', api_base: 'https://proxy.example/v1//' }, 'https://proxy.example/v1'],
        [{ provider: '', url: 'http://host.docker.internal:8080/v1', api_base: 'ignored' }, 'http://host.docker.internal:8080/v1'],
        [{ provider: 'lm_studio', api_base: 'http://host.docker.internal:1234/v1' }, ''], [{ provider: 'made_up' }, ''],
    ];
    for (const [role, url] of cases) assert.equal(conversationUrl(role), url, JSON.stringify(role));
    for (const host of ['localhost', 'host.docker.internal', 'convo-audio', '127.0.0.1', '10.1.2.3', '172.16.0.1', '172.31.255.255', '192.168.1.20', '[::1]', 'fd12:3456::1'])
        assert.equal(isLocalHost(host), true, host);
    for (const host of ['nas', '100.101.102.103', 'my-desktop.tail1234.ts.net', '[fd7a:115c:a1e0::1]'])   // your LAN and tailnet
        assert.equal(isLocalHost(host), true, host);
    for (const host of ['api.openai.com', '172.32.0.1', '169.254.1.1', '8.8.8.8', '0.0.0.0', 'fe80::1', '100.128.0.1', '134744072'])
        assert.equal(isLocalHost(host), false, host);
    assert.equal(addressProblem('', false), '');
    assert.equal(addressProblem('http://host.docker.internal:8080/v1', false), '');
    assert.match(addressProblem('https://api.openai.com/v1', false), /remote-processing consent/);
    assert.equal(addressProblem('https://api.openai.com/v1', true), '');
    assert.match(addressProblem('http://api.example.com/v1', true), /must use https/);
    for (const bad of ['host.docker.internal:8080', 'ftp://host.docker.internal/v1', 'http://user:pw@localhost:1/v1', 'http://localhost:1/v1?x=1', 'http://localhost:1/v1#x'])
        assert.notEqual(addressProblem(bad, true), '', bad);
});

// ---- The card's markup
test('The card renders on its own, uses Agent Zero\'s field classes and binds every conversation setting, capabilities included', async () => {
    const html = await readWebui('conversation-model-field.html');
    assert.match(html, /^<html><head><script type="module">import \{ store \} from '\/plugins\/_convo\/webui\/setup-store\.js';<\/script><\/head><body>/);
    assert.doesNotMatch(html, /convo-settings-pending/);                          // every control the plan names is here now
    for (const name of ['field', 'field-label', 'field-title', 'field-description', 'field-control', 'toggle', 'toggler', 'model-search-btn', 'model-search-results', 'model-search-item', 'advanced-section', 'advanced-toggle', 'advanced-body', 'masked-text-input'])
        assert.match(html, new RegExp(`class="[^"]*\\b${name}\\b`), name);
    assert.match(html, /Convo sends your speech as audio — choose a model that accepts audio input \(omni \/ multimodal audio model\)\./);
    const bound = [...html.matchAll(/\s(?:x-model(?:\.[\w.]+)?|:value|:checked|@change|@input)="([^"]*)"/g)].map(match => match[1]);
    for (const path of Object.keys(CARD_PATHS)) assert.ok(bound.some(expression => new RegExp(`config\\.${path.replace(/\./g, '\\.')}(?!\\w)`).test(expression)), path);
    for (const capability of ['audio_input', 'cancellation', 'tool_calls'])
        assert.match(html, new RegExp(`<input type="checkbox" data-convo-field="[\\w-]+" [^>]*x-model="\\$store\\.convoSetup\\.config\\.roles\\.conversation\\.capabilities\\.${capability}" @change="\\$store\\.convoSetup\\.conversationEdited\\('roles\\.conversation\\.capabilities\\.${capability}', autosave\\)">`), capability);
    assert.equal((html.match(/x-model\.number="\$store\.convoSetup\.config\.compaction\.context_tokens"/g) || []).length, 1);
    assert.match(html, /x-data="\{ role: 'conversation' \}"/);                   // the coverage guard's role marker
});

test('Every control is labelled and keyboard reachable, and nothing writes a title or reveals a key', async () => {
    const html = await readWebui('conversation-model-field.html');
    const tag = /<(input|select)\b(?:[^>"']|"[^"]*"|'[^']*')*>/g;                 // quote-aware: bindings may contain ">"
    const controls = [...html.matchAll(tag)].map(match => match[0]);
    assert.ok(controls.length >= 12);
    for (const control of controls) assert.match(control, /:aria-labelledby="\$id\('convo-[\w-]+', 'title'\)"/, control.slice(0, 90));
    assert.doesNotMatch(html, /\stitle=|:title=|x-bind:title/);                  // the host's tooltip observer owns title
    assert.doesNotMatch(html, /revealApiKey|api_key|_convo_key|localStorage|sessionStorage|x-html/);
    const combobox = controls.find(control => control.includes('x-ref="model"'));
    for (const part of ['role="combobox"', ':aria-expanded=', ':aria-controls=', ':aria-activedescendant=', '@keydown.arrow-down.prevent="move(1)"', '@keydown.arrow-up.prevent="move(-1)"', '@keydown.enter.prevent=', '@keydown.escape="if (open) { $event.stopPropagation(); close(); }"'])
        assert.ok(combobox.includes(part), part);                                // Escape closes the list without closing the modal
    assert.match(html, /role="listbox"/); assert.match(html, /role="option"/);
    assert.match(html, /<button type="button" class="advanced-toggle" :aria-expanded="advOpen \? 'true' : 'false'"/);
    assert.match(html, /\.convo-model-card \.toggle input:focus-visible\+\.toggler/);
    assert.match(html, /@media \(prefers-reduced-motion:reduce\)\{\.convo-model-card \.advanced-toggle-icon\{transition:none\}\}/);
    assert.match(html, /<template x-if="autosave"><div class="convo-model-autosave">/);   // status and inline conflicts only where it autosaves
    assert.match(html, /:disabled="\$store\.convoSetup\.isDiscovering\(role\)"/);
    assert.match(html, /<button type="button" class="button" data-convo-field="audio-probe" :disabled="\$store\.convoSetup\?\.ui\?\.conversation\?\.probe\?\.busy === true" @click="\$store\.convoSetup\.conversationProbe\(\)">Test audio input<\/button>/);
});
