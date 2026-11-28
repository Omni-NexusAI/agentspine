// Agent Zero's Whisper settings modal gains Convo's third voice-message choice, "Draft in composer (continuous)"
// (extensions/webui/modal_content_loaded/_30_convo_whisper_mode.js). Checked against the VERBATIM v2.11 host markup
// (fixtures/host-whisper-config.html, fetched from the test host's /plugins/_whisper_stt/webui/config.html) mounted the way the host's
// component loader mounts it, and against the verbatim plugin settings store (fixtures/host-plugin-settings-store.js), whose
// `hasUnsavedChanges` decides whether the modal looks edited. The DOM below is small but keeps the browser semantics this depends on:
// a select can show only a value one of its options has, and observers are told about mutations after the current task.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { hostStubs, loadModule } from './vm-loader.mjs';

const read = path => readFile(new URL(path, import.meta.url), 'utf8');
const EXTENSION = 'extensions/webui/modal_content_loaded/_30_convo_whisper_mode.js';
const WHISPER_PATH = '/plugins/_whisper_stt/webui/config.html';
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };

// ---- a small DOM ------------------------------------------------------------------------------------------------------------------
class Doc {
    constructor() { this.root = new El(this, 'body'); this.observers = new Set(); this.listeners = new Map(); this.dirty = new Set(); this.deliveries = 0; }
    createElement(tag) { return new El(this, tag); }
    addEventListener(type, fn) { this.listeners.set(type, [...(this.listeners.get(type) || []), fn]); }
    removeEventListener(type, fn) { this.listeners.set(type, (this.listeners.get(type) || []).filter(item => item !== fn)); }
    dispatch(type, detail = {}) { for (const fn of this.listeners.get(type) || []) fn({ type, detail }); }
    mutated(node) {
        if (!this.dirty.size) queueMicrotask(() => {
            const nodes = [...this.dirty]; this.dirty.clear();
            if (++this.deliveries > 200) throw new Error('mutation loop');
            for (const observer of [...this.observers]) if (nodes.some(item => observer.target.contains(item))) observer.callback([], observer);
        });
        this.dirty.add(node);
    }
}
class El {
    constructor(doc, tag) { this.ownerDocument = doc; this.localName = tag.toLowerCase(); this.attrs = new Map(); this.children = []; this.parentNode = null; this.ownText = ''; this.selected = false; }
    get tagName() { return this.localName.toUpperCase(); }
    getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
    hasAttribute(name) { return this.attrs.has(name); }
    setAttribute(name, value) { this.attrs.set(name, String(value)); this.ownerDocument.mutated(this); }
    get className() { return this.getAttribute('class') ?? ''; }
    set className(value) { this.setAttribute('class', value); }
    get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this.children = []; this.ownText = String(value); this.ownerDocument.mutated(this); }
    appendChild(child) { child.remove(); child.parentNode = this; this.children.push(child); this.ownerDocument.mutated(this); return child; }
    after(node) { const parent = this.parentNode; node.remove(); parent.children.splice(parent.children.indexOf(this) + 1, 0, node); node.parentNode = parent; this.ownerDocument.mutated(parent); }
    remove() { const parent = this.parentNode; if (!parent) return; parent.children.splice(parent.children.indexOf(this), 1); this.parentNode = null; this.ownerDocument.mutated(parent); }
    contains(node) { for (let item = node; item; item = item.parentNode) if (item === this) return true; return false; }
    get isConnected() { return this.ownerDocument.root.contains(this); }
    *descendants() { for (const child of this.children) { yield child; yield* child.descendants(); } }
    matches(selector) { return matchSelector(this, parseSelector(selector)); }
    closest(selector) { for (let item = this; item; item = item.parentNode) if (item.matches(selector)) return item; return null; }
    querySelectorAll(selector) { const parts = parseSelector(selector); return [...this.descendants()].filter(element => matchSelector(element, parts)); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    get options() { return [...this.descendants()].filter(element => element.localName === 'option'); }
    // As in a browser: a select set to a value no option has selects nothing and reads ''.
    get value() {
        if (this.localName === 'select') return this.options.find(option => option.selected)?.value ?? '';
        return this.getAttribute('value') ?? (this.localName === 'option' ? this.textContent : '');
    }
    set value(value) {
        if (this.localName !== 'select') { this.setAttribute('value', value); return; }
        const match = this.options.find(option => option.value === String(value));
        for (const option of this.options) option.selected = option === match;
    }
}
function parseSelector(selector) {
    return selector.trim().split(/\s+(?![^[]*\])/).map(compound => ({
        tag: compound.match(/^[a-z][\w-]*/i)?.[0].toLowerCase() || null,
        attrs: [...compound.matchAll(/\[([^\]=\s]+)(?:="([^"]*)")?\]/g)].map(match => [match[1], match[2]]),
        classes: [...compound.replace(/\[[^\]]*\]/g, '').matchAll(/\.([\w-]+)/g)].map(match => match[1]),
    }));
}
function matchCompound(element, compound) {
    if (compound.tag && element.localName !== compound.tag) return false;
    if (!compound.attrs.every(([name, value]) => value === undefined ? element.hasAttribute(name) : element.getAttribute(name) === value)) return false;
    const classes = element.className.split(/\s+/);
    return compound.classes.every(name => classes.includes(name));
}
function matchSelector(element, parts) {           // descendant combinators only; an ancestor outside the searched element may match
    if (!matchCompound(element, parts.at(-1))) return false;
    let index = parts.length - 2;
    for (let item = element.parentNode; item && index >= 0; item = item.parentNode) if (matchCompound(item, parts[index])) index--;
    return index < 0;
}
const VOID = new Set(['input', 'br', 'img', 'meta', 'link', 'hr']);
function parseHtml(doc, html) {
    const fragment = doc.createElement('#fragment'), stack = [fragment];
    for (const match of html.matchAll(/<!--[\s\S]*?-->|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=>/]+(?:\s*=\s*"[^"]*")?)*)\s*(\/?)>|([^<]+)/g)) {
        if (match[1]) { const at = stack.map(element => element.localName).lastIndexOf(match[1].toLowerCase()); if (at > 0) stack.length = at; }
        else if (match[2]) {
            const element = doc.createElement(match[2]);
            for (const attribute of (match[3] || '').matchAll(/([^\s=>/]+)(?:\s*=\s*"([^"]*)")?/g)) element.attrs.set(attribute[1], attribute[2] ?? '');
            element.parentNode = stack.at(-1); stack.at(-1).children.push(element);
            if (!VOID.has(element.localName) && !match[4]) stack.push(element);
        } else if (match[5]?.trim()) stack.at(-1).ownText += match[5].trim().replace(/\s+/g, ' ');
    }
    return fragment;
}

// ---- the modal, mounted as the host mounts it ------------------------------------------------------------------------------------
// openModal() -> .modal > .modal-inner > .modal-scroll > .modal-bd > plugin-settings.html; its body x-html renders
// <x-component path="/plugins/<plugin>/webui/config.html">, into which the component loader appends config.html's body nodes.
async function mountModal(doc, { path = WHISPER_PATH } = {}) {
    const fixture = parseHtml(doc, await read('./fixtures/host-whisper-config.html'));
    const body = [...fixture.descendants()].find(element => element.localName === 'body');
    const modal = doc.createElement('div'); modal.attrs.set('class', 'modal show');
    let parent = modal;
    for (const name of ['modal-inner', 'modal-scroll', 'modal-bd', 'plugin-settings-body']) { const element = doc.createElement('div'); element.attrs.set('class', name); parent.appendChild(element); parent = element; }
    const component = doc.createElement('x-component'); component.attrs.set('path', path); parent.appendChild(component);
    for (const child of [...body.children]) component.appendChild(child);
    return modal;
}

// The verbatim host plugin settings store (its model; each modal instantiates its own context from it, as $instantiate does).
const hostSettings = (await loadModule('tests/fixtures/host-plugin-settings-store.js', { bindings: hostStubs({
    '/js/confirmDialog.js': { showConfirmDialog: async () => true }, '/components/plugins/toggle/plugin-toggle-store.js': { store: {} } }) })).namespace.store;
function settingsContext(settings, pluginName = '_whisper_stt') {
    const context = Object.create(hostSettings);
    context.pluginName = pluginName; context.settings = settings;
    context.settingsSnapshotJson = context._toComparableJson(settings);          // what loadSettings() does once the settings arrive
    return context;
}
const whisperSettings = (message_mode = 'draft') => ({ model_size: 'base', language: 'en', message_mode, silence_threshold: 0.3, silence_duration: 1000, waiting_timeout: 2000 });

class Observers { constructor(doc) { this.doc = doc; } create() { const doc = this.doc; return class { constructor(callback) { this.callback = callback; } observe(target, options) { this.target = target; this.options = options; doc.observers.add(this); } disconnect() { this.disconnected = true; doc.observers.delete(this); } }; } }

async function rig({ flag = 'continuous', host = 'draft', status = null, pluginName = '_whisper_stt', path = WHISPER_PATH, modalPath = '/components/plugins/plugin-settings.html', mounted = true } = {}) {
    const doc = new Doc(), calls = [];
    const answer = status || (() => ({ success: true, enabled: true, config: { message_mode: host }, voice_message_draft: flag }));
    const { namespace } = await loadModule(EXTENSION, { bindings: { '/js/api.js': { callJsonApi: async (url, payload) => { calls.push([url, payload]); return answer(); } } },
        globals: { MutationObserver: new Observers(doc).create() } });
    const element = await mountModal(doc, { path });
    if (mounted) doc.root.appendChild(element);
    const context = settingsContext(whisperSettings(host), pluginName);
    element.__pluginSettingsContext = context;                                    // plugin-settings.html's x-create does this
    const select = () => element.querySelector('select[x-model="config.message_mode"]');
    const alpine = () => { if (select()) select().value = context.settings.message_mode; };     // x-model: the select shows the model
    const choose = value => { select().value = value; context.settings.message_mode = select().value; };   // x-model on change
    alpine();
    const loaded = (extra = {}) => namespace.default({ modalPath, modal: { path: modalPath, element }, doc: {}, ...extra });
    await loaded(); await settle(); alpine();
    return { doc, element, context, select, calls, alpine, choose, loaded, namespace };
}
const values = select => select.options.map(option => [option.value, option.textContent]);

test('The host markup this extends: one Voice Message Handling select bound to config.message_mode, offering send and draft', async () => {
    const html = await read('./fixtures/host-whisper-config.html');
    assert.match(html, /<div class="field-title">Voice Message Handling<\/div>/);
    assert.equal(html.match(/x-model="config\.message_mode"/g).length, 1);
    const doc = new Doc(), element = await mountModal(doc);
    const select = element.querySelector('x-component[path="/plugins/_whisper_stt/webui/config.html"] select[x-model="config.message_mode"]');
    assert.ok(select);
    assert.deepEqual(values(select), [['send', 'Send immediately'], ['draft', 'Draft in composer']]);
    select.value = 'continuous';
    assert.equal(select.value, '');                                              // without the option the select cannot show it
    // The host store keeps only send|draft (whisper-stt-store.js: anything but "draft" is "send"), hence Convo's own flag.
    assert.match(await read('../extensions/webui/initFw_end/enhanced-stt-recorder.js'), /continuousDraft/);
});

test('The third option follows Draft in composer with a hint; a stored continuous choice shows and the modal does not look edited', async () => {
    const r = await rig({ flag: 'continuous', host: 'draft' });
    assert.deepEqual(values(r.select()), [['send', 'Send immediately'], ['draft', 'Draft in composer'], ['continuous', 'Draft in composer (continuous)']]);
    assert.equal(r.context.settings.message_mode, 'continuous');
    assert.equal(r.select().value, 'continuous');
    assert.equal(r.context.hasUnsavedChanges, false);                             // the host's own check: nothing looks edited
    assert.deepEqual(JSON.parse(r.context.settingsSnapshotJson), { ...whisperSettings('draft'), message_mode: 'continuous' });
    const field = r.select().closest('.field');
    assert.equal(field.querySelectorAll('[data-convo-continuous-hint]').length, 1);
    assert.match(field.querySelector('[data-convo-continuous-hint]').textContent, /keeps listening and adds each phrase/);
    assert.equal(field.querySelector('[data-convo-continuous-hint]').className, 'field-description');
    assert.equal(JSON.stringify(r.calls), JSON.stringify([['/plugins/_convo/stt_status', {}]]));   // Convo's flag, read once (a read)
    for (const key of ['model_size', 'language', 'silence_threshold', 'silence_duration', 'waiting_timeout']) assert.equal(r.context.settings[key], whisperSettings()[key], key);
});

test('It is idempotent: a second load and any number of mutations leave one option, one hint and one status read', async () => {
    const r = await rig();
    await r.loaded(); await settle();
    for (let i = 0; i < 5; i++) { r.select().setAttribute('style', 'width: 100%'); r.element.querySelector('.plugin-settings-body').appendChild(r.doc.createElement('span')); await settle(); }
    assert.equal(r.select().querySelectorAll('option[value="continuous"]').length, 1);
    assert.equal(r.element.querySelectorAll('[data-convo-continuous-hint]').length, 1);
    assert.equal(r.calls.length, 1);
    assert.equal(r.context.hasUnsavedChanges, false);
    assert.ok(r.doc.deliveries < 50);
});

test('A stored single draft or send shows as the host stored it; the option is still offered and nothing looks edited', async () => {
    for (const [flag, host] of [['single', 'draft'], ['single', 'send'], ['continuous', 'send']]) {
        const r = await rig({ flag, host });
        assert.equal(r.select().querySelectorAll('option[value="continuous"]').length, 1, flag + host);
        assert.equal(r.context.settings.message_mode, host, flag + host);         // continuous without the host's draft does not apply
        assert.equal(r.select().value, host, flag + host);
        assert.equal(r.context.hasUnsavedChanges, false, flag + host);
    }
});

test('A value already changed before it got there is left alone', async () => {
    const doc = new Doc(), calls = [];
    const { namespace } = await loadModule(EXTENSION, { bindings: { '/js/api.js': { callJsonApi: async url => { calls.push(url); return { voice_message_draft: 'continuous' }; } } },
        globals: { MutationObserver: new Observers(doc).create() } });
    const element = await mountModal(doc); doc.root.appendChild(element);
    const context = settingsContext(whisperSettings('draft'));
    context.settings.message_mode = 'send';                                      // the user picked "Send immediately" first
    element.__pluginSettingsContext = context;
    await namespace.default({ modalPath: '/components/plugins/plugin-settings.html', modal: { element }, doc: {} }); await settle();
    assert.equal(context.settings.message_mode, 'send');
    assert.equal(context.hasUnsavedChanges, true);                                // still an edit, as the user made it
    assert.equal(JSON.parse(context.settingsSnapshotJson).message_mode, 'draft');
    // The host stored "send" and the user picked "Draft in composer" before the option arrived: their draft stays a draft.
    const second = new Doc();
    const { namespace: again } = await loadModule(EXTENSION, { bindings: { '/js/api.js': { callJsonApi: async () => ({ voice_message_draft: 'continuous' }) } },
        globals: { MutationObserver: new Observers(second).create() } });
    const other = await mountModal(second); second.root.appendChild(other);
    const picked = settingsContext(whisperSettings('send'));
    picked.settings.message_mode = 'draft';
    other.__pluginSettingsContext = picked;
    await again.default({ modalPath: '/components/plugins/plugin-settings.html', modal: { element: other }, doc: {} }); await settle();
    assert.deepEqual([picked.settings.message_mode, picked.hasUnsavedChanges, JSON.parse(picked.settingsSnapshotJson).message_mode], ['draft', true, 'send']);
});

test('Scoped to the Whisper settings: another plugin, another modal or a page still loading are handled correctly', async () => {
    const kokoro = await rig({ pluginName: '_kokoro_tts', path: '/plugins/_kokoro_tts/webui/config.html' });
    assert.deepEqual(values(kokoro.select()), [['send', 'Send immediately'], ['draft', 'Draft in composer']]);
    assert.deepEqual(kokoro.calls, []);                                           // not even a status read
    assert.equal(kokoro.doc.observers.size, 0);                                   // and it stopped watching that modal
    const other = await rig({ modalPath: '/plugins/_convo/webui/main.html' });
    assert.deepEqual([values(other.select()).length, other.calls.length, other.doc.observers.size], [2, 0, 0]);
    // Whisper's page renders after the hook (x-if): the option arrives when the select does.
    const doc = new Doc();
    const { namespace } = await loadModule(EXTENSION, { bindings: { '/js/api.js': { callJsonApi: async () => ({ voice_message_draft: 'continuous' }) } },
        globals: { MutationObserver: new Observers(doc).create() } });
    const element = doc.createElement('div'); element.attrs.set('class', 'modal show'); doc.root.appendChild(element);
    await namespace.default({ modalPath: 'components/plugins/plugin-settings.html', modal: { element }, doc: {} }); await settle();
    assert.equal(doc.observers.size, 1);
    const full = await mountModal(doc);
    const context = settingsContext(whisperSettings('draft'));
    element.__pluginSettingsContext = context;
    element.appendChild(full.children[0]); await settle();
    const select = element.querySelector('select[x-model="config.message_mode"]');
    assert.equal(select.querySelectorAll('option[value="continuous"]').length, 1);
    assert.deepEqual([context.settings.message_mode, context.hasUnsavedChanges], ['continuous', false]);
});

test('Choosing it is what Save sends: the modal is edited, and the legacy Whisper save forwards message_mode to the bridge', async () => {
    const r = await rig({ flag: 'single', host: 'draft' });
    r.choose('continuous');
    assert.equal(r.context.settings.message_mode, 'continuous');
    assert.equal(r.context.hasUnsavedChanges, true);
    r.choose('draft');
    assert.equal(r.context.hasUnsavedChanges, false);
    r.choose('continuous');
    // persistWhisperSettings (page-head/_10_convo_legacy_ui.html, unchanged) copies these keys from the modal's settings to stt_config.
    const legacy = await read('../extensions/webui/page-head/_10_convo_legacy_ui.html');
    const persist = legacy.slice(legacy.indexOf('async function persistWhisperSettings'), legacy.indexOf('async function getKokoroModalContext'));
    assert.match(persist, /"model_size", "device", "language", "message_mode",/);
    assert.match(persist, /callJson\("\/plugins\/_convo\/stt_config", payload\)/);
    const keys = [...persist.matchAll(/"(\w+)"/g)].map(match => match[1]).filter(key => key in r.context.settings || key === 'device');
    const payload = Object.fromEntries(keys.filter(key => key in r.context.settings).map(key => [key, r.context.settings[key]]));
    assert.equal(payload.message_mode, 'continuous');                            // which api/stt_config.py stores as draft + continuous
});

test('A settings reload (a new settings object) shows the stored choice again without looking edited', async () => {
    const r = await rig({ flag: 'continuous', host: 'draft' });
    r.context.settings = whisperSettings('draft');                               // loadSettings(): fresh host values ...
    r.context.settingsSnapshotJson = r.context._toComparableJson(r.context.settings);
    r.element.querySelector('.plugin-settings-body').setAttribute('style', '');   // ... and x-show toggles while it loads
    await settle(); r.alpine();
    assert.deepEqual([r.context.settings.message_mode, r.select().value, r.context.hasUnsavedChanges], ['continuous', 'continuous', false]);
    assert.equal(r.calls.length, 1);
});

test('Without Convo\'s bridge nothing is offered (it could not be stored)', async () => {
    for (const status of [() => { throw new Error('Not found'); }, () => ({ success: true, config: { message_mode: 'draft' } }), () => ({ voice_message_draft: 'sometimes' })]) {
        const r = await rig({ status });
        assert.deepEqual(values(r.select()), [['send', 'Send immediately'], ['draft', 'Draft in composer']]);
        assert.deepEqual([r.context.settings.message_mode, r.context.hasUnsavedChanges], ['draft', false]);
    }
});

test('Closing the modal releases its observer and listener', async () => {
    const r = await rig();
    assert.equal(r.doc.observers.size, 1);
    assert.equal((r.doc.listeners.get('modal-closed') || []).length, 1);
    r.element.remove();
    r.doc.dispatch('modal-closed', { modalPath: '/components/plugins/plugin-settings.html', remainingModalCount: 0 });
    assert.equal(r.doc.observers.size, 0);
    assert.equal((r.doc.listeners.get('modal-closed') || []).length, 0);
    const open = await rig();                                                     // another modal closing leaves an open one watched
    open.doc.dispatch('modal-closed', { modalPath: '/plugins/_convo/webui/settings-modal.html', remainingModalCount: 1 });
    assert.equal(open.doc.observers.size, 1);
});
