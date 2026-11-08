// Live check of the Convo conversation model as an Agent Zero model type, against an ISOLATED loopback test host (default
// http://127.0.0.1:5028/) with a throwaway headless browser (tests/live-browser.mjs): the section in the real preset editor, the
// summary row in Settings → Agent → Models, autosave, and the Convo modal agreeing. It changes Convo's conversation model through
// the real interface and restores every setting it changed at the end. Nothing reaches a provider or Agent Zero's key store:
// writes to /plugins/_model_config/api_keys, the key copy and Test audio input are intercepted with the DevTools Fetch domain and
// answered in the page, so the test host's .env is untouched and no audio is sent anywhere. It starts no voice session and does
// not click a model field (that would make the host list models from a provider).
//
//   node tests/live-model-type-smoke.mjs [url]        exit 0 = every check passed
//
// Not part of the default node test run (its name does not end in .test.mjs); run it after deploying to the test host.
import { launch, sleep, assertLoopback } from './live-browser.mjs';

const url = assertLoopback(process.argv[2] || process.env.CONVO_LIVE_UI_URL || 'http://127.0.0.1:5028/');
const FAKE_KEY = 'convo-live-check-not-a-real-key-7f3a';
const LIVE_MODEL = 'convo-live-check-model';
const EDITOR = '.modal[data-modal-path="/plugins/_model_config/webui/main.html"]';
const SECTION = `${EDITOR} .preset-editor-body > [data-convo-model-type]`;
const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok: !!ok, detail: String(detail) }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`); };
let browser;
const watchdog = setTimeout(async () => { console.error('FAIL  overall time limit (10 minutes)'); try { await browser?.close(); } catch { /* nothing more to do */ } process.exit(2); }, 10 * 60 * 1000);
watchdog.unref();

const KEYS = { ArrowDown: 40, ArrowUp: 38, Escape: 27, Tab: 9, Enter: 13 };
async function press(page, key) {
    for (const type of ['keyDown', 'keyUp']) await page.send('Input.dispatchKeyEvent', { type, key, code: key, windowsVirtualKeyCode: KEYS[key] || 0, nativeVirtualKeyCode: KEYS[key] || 0 });
    await sleep(100);
}
// A real mouse click on the element an in-page expression returns.
async function clickOn(page, expression) {
    const point = await page.eval(`(() => { const e = ${expression}; if (!e) return null; e.scrollIntoView({ block: 'center' }); const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()`);
    if (!point) throw new Error('No element: ' + expression.slice(0, 120));
    for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) await page.send('Input.dispatchMouseEvent', { type, x: point.x, y: point.y, button: 'left', clickCount: 1 });
    await sleep(200);
}
// Type into an input the way a user does: focus it (no click: a click on the model field searches the provider), select its text,
// insert the new text (input events), then leave it with Tab (change event).
async function typeInto(page, selector, text) {
    await page.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); e.focus(); e.select?.(); return 1; })()`);
    await page.send('Input.insertText', { text });
    await sleep(150);
}
// Choose an option of a real <select> with the keyboard (Chrome skips disabled options), as a user would.
async function chooseOption(page, selector, value) {
    await page.eval(`document.querySelector(${JSON.stringify(selector)}).focus(), 1`);
    for (const key of ['ArrowDown', 'ArrowUp']) {
        for (let i = 0; i < 40; i++) {
            if (await page.eval(`document.querySelector(${JSON.stringify(selector)}).value === ${JSON.stringify(value)}`)) return true;
            await press(page, key);
        }
    }
    return page.eval(`document.querySelector(${JSON.stringify(selector)}).value === ${JSON.stringify(value)}`);
}
const statusText = page => page.eval(`document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="autosave-status"]')})?.textContent.trim() || ''`);

browser = await launch({ port: Number(process.env.CONVO_LIVE_DEBUG_PORT || 9333) });
let page, original = null;
const responses = [], requests = new Map(), intercepted = { keyWrites: [], copies: 0, probes: 0 };
try {
    page = await browser.page(url);
    page.answerDialogs(() => true);
    await page.viewport({ width: 1400, height: 900 });

    // Interception first: no key write, key copy or audio test may ever reach the host.
    const fulfil = (requestId, body) => page.send('Fetch.fulfillRequest', { requestId, responseCode: 200, responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
        body: Buffer.from(JSON.stringify(body)).toString('base64') }).catch(() => {});
    page.on(message => {
        if (message.method === 'Fetch.requestPaused') {
            const { requestId, request } = message.params;
            let body = {};
            try { body = JSON.parse(request.postData || '{}'); } catch { /* not JSON */ }
            if (/\/plugins\/_model_config\/api_keys/.test(request.url) && body.action === 'set') {
                intercepted.keyWrites.push(body.keys || {});
                fulfil(requestId, { ok: true });                                       // answered here: the .env is never written
            } else if (/\/plugins\/_convo\/conversation/.test(request.url) && body.action === 'conversation_key_copy') {
                intercepted.copies++;
                fulfil(requestId, { ok: false, code: 'occupied', error: 'Intercepted by the live check: Agent Zero’s key store was not touched.' });
            } else if (/\/plugins\/_convo\/conversation/.test(request.url) && body.action === 'audio_probe') {
                intercepted.probes++;
                fulfil(requestId, { ok: true, passed: false, code: 'intercepted', message: 'Intercepted by the live check: nothing was sent.' });
            } else {
                page.send('Fetch.continueRequest', { requestId }).catch(() => {});
            }
        }
        // Every key write the page attempted, intercepted or not, to prove none went through.
        if (message.method === 'Network.requestWillBeSent' && /\/plugins\/_model_config\/api_keys/.test(message.params.request.url)
            && /"action"\s*:\s*"set"/.test(message.params.request.postData || '')) intercepted.attempts = (intercepted.attempts || 0) + 1;
        // Every Convo answer is kept, to prove no key value is ever in one.
        if (message.method === 'Network.requestWillBeSent' && /\/plugins\/_convo\/conversation/.test(message.params.request.url)) {
            let action = '';
            try { action = JSON.parse(message.params.request.postData || '{}').action || ''; } catch { /* not JSON */ }
            requests.set(message.params.requestId, action);
        }
        if (message.method === 'Network.loadingFinished' && requests.has(message.params.requestId)) {
            const action = requests.get(message.params.requestId);
            page.send('Network.getResponseBody', { requestId: message.params.requestId }).then(result => responses.push({ action, body: result.body })).catch(() => {});
        }
    });
    await page.send('Network.enable');
    await page.send('Fetch.enable', { patterns: [{ urlPattern: '*/plugins/_model_config/api_keys*', requestStage: 'Request' }, { urlPattern: '*/plugins/_convo/conversation*', requestStage: 'Request' }] });

    await page.waitFor("typeof window.Alpine?.store?.('convoSetup') === 'object' || typeof window.Alpine?.store?.('convo') === 'object'", 240000, 500);
    await page.waitFor("!!window.Alpine?.store?.('settings')", 60000);
    check('the page stays responsive', await page.eval('1 + 1') === 2);
    original = await page.eval(`(async () => { const { store } = await import('/plugins/_convo/webui/setup-store.js'); const r = await store.api('setup_read'); return { conversation: r.config.roles.conversation, context: r.config.compaction.context_tokens, style: r.config.style }; })()`);
    check('the saved Convo settings were read for the restore at the end', !!original?.conversation, JSON.stringify({ provider: original?.conversation?.provider, model: original?.conversation?.model }));

    // Settings → Agent → Models: one Convo row after the host's rows.
    await page.eval(`(() => { void Alpine.store('settings').open('agent'); return 1; })()`);   // resolves only when the modal closes
    await page.waitFor(`!!document.querySelector('.model-preset-rows .convo-model-summary-row')`, 60000);
    await sleep(800);
    const row = await page.eval(`(() => { const lists = [...document.querySelectorAll('.model-preset-rows')]; const list = lists[0]; const rows = [...list.querySelectorAll(':scope > .model-preset-row')]; const mine = list.querySelectorAll('.convo-model-summary-row'); return { lists: lists.length, count: mine.length, last: rows.at(-1)?.classList.contains('convo-model-summary-row'), role: mine[0]?.querySelector('.model-preset-role')?.textContent, identity: mine[0]?.querySelector('.model-preset-identity')?.textContent }; })()`);
    check('Settings → Agent → Models shows exactly one Convo row, after the host rows', row.count === 1 && row.last && row.role === 'Convo', JSON.stringify(row));
    check('the row names the saved provider and model', !!row.identity && row.identity.includes(original.conversation.model || '—'), row.identity);

    // The preset editor: exactly one section, however often it is opened or the preset switched.
    const openEditor = async () => {
        await clickOn(page, `[...document.querySelectorAll('.model-preset-actions button')].find(b => /Edit Presets/.test(b.textContent))`);
        await page.waitFor(`!!document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="provider"]')})`, 60000);
        await sleep(800);
    };
    const sectionCount = () => page.eval(`document.querySelectorAll(${JSON.stringify(EDITOR + ' [data-convo-model-type]')}).length`);
    await openEditor();
    const heading = await page.eval(`(() => { const s = document.querySelector(${JSON.stringify(SECTION)}); return { title: s.querySelector('.section-title')?.textContent, note: s.querySelector('.section-description')?.textContent, disclaimer: s.querySelector('.convo-model-note')?.textContent.trim(), status: s.querySelector('[data-convo-field="autosave-status"]')?.textContent.trim() }; })()`);
    check('the preset editor has exactly one Convo section', await sectionCount() === 1);
    check('it says what it is: one setting for all presets, saved automatically, and that the model must accept audio', heading.title === 'Convo conversation model' && /One setting for all presets; changes save automatically\./.test(heading.note) && /accepts audio input/.test(heading.disclaimer), JSON.stringify(heading));
    const presetSelect = `${EDITOR} .preset-editor-toolbar select`;
    const presetCount = await page.eval(`document.querySelector(${JSON.stringify(presetSelect)}).options.length`);
    if (presetCount < 2) await clickOn(page, `[...document.querySelectorAll(${JSON.stringify(EDITOR + ' .preset-editor-toolbar button')})].find(b => /Add/.test(b.textContent))`);   // a draft preset, never saved
    await page.eval(`document.querySelector(${JSON.stringify(presetSelect)}).focus(), 1`);
    await press(page, 'ArrowDown'); await sleep(500); await press(page, 'ArrowUp'); await sleep(500);
    check('switching presets keeps exactly one section', await sectionCount() === 1, `${presetCount} preset(s)`);

    // Disabled providers carry their reason.
    const providerSelect = `${SECTION} [data-convo-field="provider"]`;
    const reasons = await page.eval(`(() => { const s = document.querySelector(${JSON.stringify(SECTION)}); const disabled = [...s.querySelectorAll('[data-convo-field="provider"] option')].filter(o => o.disabled).map(o => o.textContent); const why = s.querySelector('.convo-model-why'); why.open = true; return { disabled, items: [...why.querySelectorAll('li')].map(li => li.textContent.trim()), visible: why.querySelector('li')?.offsetParent !== null }; })()`);
    check('providers Convo cannot send audio to are listed disabled, each with its reason', reasons.disabled.length >= 5 && reasons.disabled.every(text => /\(.+\)$/.test(text)) && reasons.visible
        && reasons.items.some(text => /LM Studio — Convo cannot send audio input through this provider\./.test(text)) && reasons.items.some(text => /Anthropic — Not reachable/.test(text)), JSON.stringify(reasons).slice(0, 300));

    // Autosave, with an unsaved Convo edit elsewhere in the shared draft that must survive and never be sent.
    const unsavedStyle = (original.style || '') + ' [live check, unsaved]';
    await page.eval(`(async () => { const { store } = await import('/plugins/_convo/webui/setup-store.js'); store.config.style = ${JSON.stringify(unsavedStyle)}; return 1; })()`);
    const patchesBefore = responses.filter(r => r.action === 'setup_patch').length;
    await typeInto(page, `${SECTION} [data-convo-field="model"]`, LIVE_MODEL);
    await page.waitFor(`document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="autosave-status"]')})?.textContent.trim() === 'Saved'`, 15000, 200);
    await sleep(500);
    const saved = await page.eval(`(async () => { const { store } = await import('/plugins/_convo/webui/setup-store.js'); const r = await store.api('setup_read'); return { model: r.config.roles.conversation.model, style: r.config.style, draftStyle: store.config.style, dirty: store.dirty }; })()`);
    const patches = responses.filter(r => r.action === 'setup_patch').slice(patchesBefore);
    check('a change in the section is saved by itself and says "Saved"', saved.model === LIVE_MODEL && await statusText(page) === 'Saved', JSON.stringify({ model: saved.model, patches: patches.length }));
    check('the autosave merged with an unsaved Convo edit: kept in the draft, never saved', saved.style === (original.style || '') && saved.draftStyle === unsavedStyle && saved.dirty === true, JSON.stringify(saved));

    // Close, see the summary agree, reopen: still exactly one section showing the saved value. The model list opens as you type,
    // so the first Escape closes the list (a combobox's own Escape) and the next one the editor.
    if (await page.eval(`!![...document.querySelectorAll(${JSON.stringify(SECTION + ' .model-search-results')})].find(e => e.offsetParent !== null)`))
        await press(page, 'Escape');
    await press(page, 'Escape');
    await page.waitFor(`!document.querySelector(${JSON.stringify(EDITOR)})`, 15000);
    await sleep(800);
    const after = await page.eval(`document.querySelector('.model-preset-rows .convo-model-summary-row .model-preset-identity')?.textContent || ''`);
    check('closing the editor leaves the summary row showing the new model', after.includes(LIVE_MODEL), after);
    await openEditor();
    check('reopening the editor still shows exactly one section, with the saved model', await sectionCount() === 1
        && await page.eval(`document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="model"]')}).value`) === LIVE_MODEL);

    // Agent Zero's key, the A0 way: a named provider (held until remote consent), then the key field, intercepted on its way.
    // Start from "no consent" whatever the host had saved (the restore at the end puts the original back).
    const remoteBox = SECTION + ' [data-convo-field="remote"]';
    if (await page.eval(`!!document.querySelector(${JSON.stringify(remoteBox)})?.checked`)) {
        await clickOn(page, `document.querySelector(${JSON.stringify(remoteBox)}).nextElementSibling`);
        await sleep(900);
    }
    check('OpenRouter can be chosen with the real select', await chooseOption(page, providerSelect, 'openrouter'));
    await sleep(900);
    check('a cloud provider waits for remote consent, saying why', /needs remote-processing consent/.test(await statusText(page)), await statusText(page));
    await clickOn(page, `document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="remote"]')}).nextElementSibling`);
    await page.waitFor(`document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="autosave-status"]')})?.textContent.trim() === 'Saved'`, 15000, 200);
    const provider = await page.eval(`(async () => { const { store } = await import('/plugins/_convo/webui/setup-store.js'); const r = await store.api('setup_read'); return { provider: r.config.roles.conversation.provider, url: r.config.roles.conversation.url, remote: r.config.roles.conversation.remote }; })()`);
    check('with consent the provider is saved and the endpoint follows it', provider.provider === 'openrouter' && provider.remote === true && provider.url === 'https://openrouter.ai/api/v1', JSON.stringify(provider));
    // Agent Zero's key field shows only for the key source "Agent Zero's key"; a host saved with Convo's own key starts from that
    // choice (Advanced → Key source, the real select; the restore at the end puts the saved choice back).
    if (!(await page.eval(`!!document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="host-key"]')})`))) {
        if (await page.eval(`document.querySelector(${JSON.stringify(SECTION + ' .advanced-toggle')})?.getAttribute('aria-expanded') !== 'true'`))
            await clickOn(page, `document.querySelector(${JSON.stringify(SECTION + ' .advanced-toggle')})`);
        await chooseOption(page, SECTION + ' [data-convo-field="key-source"]', 'provider');
        await page.eval(`(() => { const e = document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="key-source"]')}); e.dispatchEvent(new Event('change', { bubbles: true })); return 1; })()`);
        await page.waitFor(`document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="autosave-status"]')})?.textContent.trim() === 'Saved'`, 15000, 200);
        if (await page.eval(`document.querySelector(${JSON.stringify(SECTION + ' .advanced-toggle')})?.getAttribute('aria-expanded') === 'true'`))
            await clickOn(page, `document.querySelector(${JSON.stringify(SECTION + ' .advanced-toggle')})`);
    }
    await page.waitFor(`!!document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="host-key"]')})`, 10000);
    await typeInto(page, `${SECTION} [data-convo-field="host-key"]`, FAKE_KEY);
    await press(page, 'Tab');
    await sleep(1200);
    check("Agent Zero's key is written through Agent Zero's key store (intercepted; the .env is untouched)", intercepted.keyWrites.length === 1 && intercepted.keyWrites[0].openrouter === FAKE_KEY, JSON.stringify(intercepted.keyWrites.map(item => Object.keys(item))));
    check('the typed key does not stay in the page', await page.eval(`document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="host-key"]')}).value === ''`));
    await page.eval(`(async () => { const { store } = await import('/plugins/_convo/webui/setup-store.js'); await store.conversationCopyKey(); return 1; })()`);
    check('the key copy goes to the server only when asked (intercepted, nothing written)', intercepted.copies === 1
        && /Intercepted by the live check/.test(await page.eval(`(async () => (await import('/plugins/_convo/webui/setup-store.js')).store.ui.conversation.keyCopy?.message || '')()`)));
    check('Test audio input sends nothing until it is clicked', intercepted.probes === 0);
    // Open Advanced only if it is closed (an earlier step may have opened it), and click the button once it is really visible.
    if (await page.eval(`document.querySelector(${JSON.stringify(SECTION + ' .advanced-toggle')})?.getAttribute('aria-expanded') !== 'true'`))
        await clickOn(page, `document.querySelector(${JSON.stringify(SECTION + ' .advanced-toggle')})`);
    await page.waitFor(`document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="audio-probe"]')})?.offsetParent !== null`, 10000, 150);
    const hit = await page.eval(`(() => { const b = document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="audio-probe"]')}); b.scrollIntoView({ block: 'center' }); const r = b.getBoundingClientRect(); const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        let scoped = null; try { scoped = JSON.parse(JSON.stringify(Alpine.evaluate(b, '$store.convoSetup.ui.conversation') || null)); } catch (e) { scoped = 'error: ' + e.message; }
        return { reachable: top === b || b.contains(top), top: top ? top.tagName + '.' + String(top.className).slice(0, 60) : null, rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)], disabled: b.disabled,
                 attr: b.getAttribute('disabled'), scoped, store: JSON.parse(JSON.stringify(Alpine.store('convoSetup').ui.conversation || null)), same: Alpine.store('convoSetup') === Alpine.evaluate(b, '$store.convoSetup') }; })()`);
    check('Test audio input is visible and not covered by anything', hit.reachable && !hit.disabled, JSON.stringify(hit));
    await clickOn(page, `document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="audio-probe"]')})`);
    await sleep(1200);
    const probe = await page.eval(`document.querySelector(${JSON.stringify(SECTION + ' [data-convo-field="probe-result"]')})?.textContent.trim() || ''`);
    const probeState = await page.eval(`(async () => JSON.parse(JSON.stringify((await import('/plugins/_convo/webui/setup-store.js')).store.ui.conversation.probe || null)))()`);
    check('one click, one audio test, and its answer is shown (intercepted)', intercepted.probes === 1 && /Failed: Intercepted by the live check/.test(probe), JSON.stringify({ probe, probes: intercepted.probes, state: probeState }));
    await press(page, 'Escape');
    await page.waitFor(`!document.querySelector(${JSON.stringify(EDITOR)})`, 15000);
    await press(page, 'Escape');                                                        // the Settings modal
    await sleep(800);

    // The Convo modal edits the same draft: it shows what the preset editor saved.
    await page.eval(`Alpine.store('rightCanvas').open('convo').then(() => 1)`);
    await page.waitFor(`!!document.querySelector('.convo-panel button[aria-label="Convo settings"]')`, 60000);
    await page.click('.convo-panel button[aria-label="Convo settings"]');
    await page.waitFor(`!!document.querySelector('.convo-settings [data-convo-field="model"]')`, 60000);
    await sleep(800);
    const modal = await page.eval(`(() => { const s = document.querySelector('.convo-settings'); return { model: s.querySelector('[data-convo-field="model"]').value, provider: s.querySelector('[data-convo-field="provider"]').value, autosaveStatus: !!s.querySelector('[data-convo-field="autosave-status"]'), legend: s.querySelector('.convo-model-card > legend')?.textContent }; })()`);
    check('the Convo modal agrees: same provider and model, and no autosave there', modal.model === LIVE_MODEL && modal.provider === 'openrouter' && !modal.autosaveStatus && modal.legend === 'Conversation model', JSON.stringify(modal));
} catch (error) {
    check('the live check completed', false, error.message);
} finally {
    // Restore every setting this run changed, and drop the unsaved draft edit.
    if (page && original) {
        try {
            const restored = await page.eval(`(async () => {
                const { store } = await import('/plugins/_convo/webui/setup-store.js');
                const now = (await store.api('setup_read')).config;
                const changes = [];
                if (JSON.stringify(now.roles.conversation) !== JSON.stringify(${JSON.stringify(original.conversation)})) changes.push({ path: 'roles.conversation', base: now.roles.conversation, value: ${JSON.stringify(original.conversation)} });
                if (now.compaction.context_tokens !== ${JSON.stringify(original.context)}) changes.push({ path: 'compaction.context_tokens', base: now.compaction.context_tokens, value: ${JSON.stringify(original.context)} });
                if (changes.length) await store.api('setup_patch', { changes });
                store.discard(); await store.load(true);
                await Alpine.store('modelConfig')?.refreshApiKeyStatus?.();
                const back = (await store.api('setup_read')).config;
                return JSON.stringify(back.roles.conversation) === JSON.stringify(${JSON.stringify(original.conversation)}) && back.compaction.context_tokens === ${JSON.stringify(original.context)} && back.style === ${JSON.stringify(original.style)};
            })()`);
            check('every setting this run changed is restored', restored);
        } catch (error) { check('every setting this run changed is restored', false, error.message); }
        await sleep(1500);
        const leaks = responses.filter(r => [FAKE_KEY, '_convo_key', 'Authorization', 'Bearer '].some(word => r.body.includes(word)) || /\bsk-[A-Za-z0-9_-]{8,}/.test(r.body));
        const actions = [...new Set(responses.map(r => r.action))];
        check('no key appears in any Convo answer (setup_read, setup_patch and the rest)', responses.length > 0 && actions.includes('setup_read') && actions.includes('setup_patch') && leaks.length === 0,
            `${responses.length} answers (${actions.join(', ')}); leaks: ${leaks.map(r => r.action).join(', ') || 'none'}`);
        check('every key write the page attempted was intercepted, none reached the host', (intercepted.attempts || 0) === intercepted.keyWrites.length,
            `${intercepted.attempts || 0} attempted, ${intercepted.keyWrites.length} intercepted`);
        check('nothing was reported in the console', page.problems().length === 0, page.problems().slice(0, 3).map(p => p.text.slice(0, 140)).join(' | '));
    }
    page?.close();
    await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
