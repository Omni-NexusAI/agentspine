import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { loadModule, hostStubs } from './vm-loader.mjs';
import { settingsMarkup } from './settings-markup.mjs';

const MODAL = '/plugins/_convo/webui/settings-modal.html';

async function fixture({ dirty = false, confirm = () => true, openFails = false } = {}) {
    const opened = [], confirms = [];
    const setup = { dirty, discarded: 0, discard() { this.discarded++; this.dirty = false; } };
    const bindings = hostStubs({
        '/js/websocket.js': { createNamespacedClient: () => ({}) },
        './audio.js': { AudioSession: class {} },
        '/plugins/_convo/webui/setup-store.js': { store: setup },
        '/js/modals.js': {
            ensureModalOpen: async (path, beforeClose) => { if (openFails) throw new Error('Modal host unavailable'); opened.push({ path, beforeClose }); },
            openModal: async () => assert.fail('The settings modal must be focused, not stacked, when it is already open'),
        },
    });
    const { namespace } = await loadModule('webui/convo-store.js', { bindings, globals: {
        document: { hidden: false, getElementById: () => null, addEventListener() {} },
        window: { confirm: message => { confirms.push(message); return confirm(message); }, addEventListener() {} } } });
    return { store: namespace.store, setup, opened, confirms };
}

test('The gear opens the settings modal through the host, with an unsaved-changes guard', async () => {
    const f = await fixture();
    await f.store.openSettingsModal();
    assert.equal(f.opened.length, 1);
    assert.equal(f.opened[0].path, MODAL);
    assert.equal(typeof f.opened[0].beforeClose, 'function');
    await f.store.openSettingsModal();                                       // already open: the host focuses it instead of stacking
    assert.equal(f.opened.length, 2);
    assert.equal(f.opened[1].path, MODAL);
    assert.equal(f.store.error, '');
});

test('Closing a clean modal never asks; a dirty one asks, and keeps the draft when the user stays', async () => {
    const clean = await fixture();
    assert.equal(await clean.store.settingsMayClose(), true);
    assert.deepEqual([clean.confirms.length, clean.setup.discarded], [0, 0]);

    const stay = await fixture({ dirty: true, confirm: () => false });
    assert.equal(await stay.store.settingsMayClose(), false);                // the host keeps the modal open
    assert.deepEqual([stay.confirms.length, stay.setup.discarded, stay.setup.dirty], [1, 0, true]);

    const leave = await fixture({ dirty: true, confirm: () => true });
    await leave.store.openSettingsModal();
    assert.equal(await leave.opened[0].beforeClose(), true);                 // the guard the host actually calls
    assert.deepEqual([leave.setup.discarded, leave.setup.dirty], [1, false]);
});

test('A host that cannot open the modal reports it without throwing', async () => {
    const f = await fixture({ openFails: true });
    await f.store.openSettingsModal();
    assert.match(f.store.error, /Modal host unavailable/);
});

test('The focus trap listens on the whole modal shell and is released on destroy', async () => {
    const f = await fixture();
    const added = [], removed = [];
    const shell = { addEventListener: (type, handler) => added.push([type, handler]), removeEventListener: (type, handler) => removed.push([type, handler]), ownerDocument: { activeElement: null } };
    const body = { closest: selector => selector === '.modal-inner' ? shell : null };
    const release = f.store.trapFocus(body);
    assert.deepEqual(added.map(([type]) => type), ['keydown']);
    release();
    assert.deepEqual(removed, added);
    assert.equal(typeof f.store.trapFocus(null), 'function');                // nothing to trap: harmless
});

const read = name => readFile(new URL('../webui/' + name, import.meta.url), 'utf8');

test('The panel is history-first: no inline settings, one scroller, controls compact', async () => {
    const html = await read('main.html');
    const [markup, style] = [html.slice(0, html.indexOf('<style>')), html.slice(html.indexOf('<style>'))];
    assert.doesNotMatch(markup, /settings-controls\.html|convo-quick|x-model|convoSetup/);       // no editable settings in the panel
    assert.doesNotMatch(markup, /<details/);                                                    // no collapsible dashboards before the history
    assert.match(markup, /aria-label="Convo settings"[^>]*aria-haspopup="dialog"|aria-haspopup="dialog"[^>]*aria-label="Convo settings"/);
    assert.match(markup, /@click="\$store\.convo\.openSettingsModal\(\)"/);
    assert.match(markup, /aria-label="Open Voice Studio"/);
    assert.ok(markup.indexOf('convo-bar') < markup.indexOf('convo-transcript'));                // header before history
    const scrollers = style.match(/([^{}]+)\{[^{}]*overflow-y:auto[^{}]*\}/g) || [];
    assert.equal(scrollers.length, 1);
    assert.match(scrollers[0], /^\s*\.convo-transcript/);
    assert.match(style, /\.convo-panel\{[^}]*overflow:hidden/);                                 // the panel itself never scrolls
    assert.match(markup, /role="log"[^>]*aria-live="off"/);
    assert.match(markup, /data-role="assistant" x-show="\$store\.convo\.draft" aria-live="off"/);
    assert.match(markup, /x-effect="[^"]*queueSettle\(\)"/);                                   // follows the end while the reader is there
    assert.match(markup, /Draft · not yet delivered/);
    // The jump button must not sit inside the scroller: showing it there lengthens the content and un-pins the reader.
    const scroller = markup.slice(markup.indexOf('<section class="convo-transcript"'), markup.indexOf('</section>', markup.indexOf('<section class="convo-transcript"')));
    assert.doesNotMatch(scroller, /convo-latest/);
    assert.match(markup.slice(markup.indexOf('</section>', markup.indexOf('<section class="convo-transcript"'))), /convo-latest/);
    assert.match(style, /\.convo-latest\{position:absolute/);
});

test('The settings modal mounts the shared controls with a footer, an initial focus target and a focus trap', async () => {
    const html = await read('settings-modal.html');
    assert.match(html, /<title>Convo settings<\/title>/);
    assert.match(html, /<x-component path="\/plugins\/_convo\/webui\/settings-controls\.html">/);
    assert.match(html, /data-modal-footer/);
    assert.match(html, /tabindex="-1"[^>]*data-initial-focus|data-initial-focus[^>]*tabindex="-1"/);
    assert.match(html, /\$store\.convo\.trapFocus\(\$el\)/);
    assert.match(html, /\$store\.convoSetup\.load\(\)/);
    assert.match(html, /\.convo-modal \.convo-settings>footer\{display:none\}/);                // the footer lives in the modal footer slot
    assert.match(html, /x-destroy="[^"]*keys = \{\}/);                                          // typed keys never outlive the modal
    const footer = html.slice(html.indexOf('<div class="modal-footer'), html.indexOf('</div>', html.indexOf('<div class="modal-footer'))).replace(/<!--[\s\S]*?-->/g, '');
    assert.doesNotMatch(footer, /x-show/);                                                      // x-show stops reacting once the host moves the footer
    assert.match(footer, /:hidden="!\$store\.convoSetup\?\.dirty"/);
    assert.doesNotMatch(html, /localStorage|sessionStorage/);
});

test('An accessor that can return null is never dereferenced with a bare dot (Alpine evaluates hidden text too)', async () => {
    const html = (await settingsMarkup()) + (await read('settings-modal.html')) + (await read('main.html'));
    // x-show does not stop x-text from running, so a null accessor used with a bare `.` throws a console error on every render
    // (decider-ui.test.mjs checks that the decider card does use it, safely)
    assert.equal((html.match(/selectedBackendCapabilities\(\)\./g) || []).length, 0);
});

test('The sidebar menu entry loads the Convo store itself, so a click before the page finished loading cannot fail', async () => {
    const html = await readFile(new URL('../extensions/webui/sidebar-quick-actions-dropdown-start/convo.html', import.meta.url), 'utf8');
    assert.match(html, /import\('\/plugins\/_convo\/webui\/convo-store\.js'\)\.then\(m => m\.store\.show\(\)\)/);
    assert.doesNotMatch(html, /\$store\.convo\.show/);
});
