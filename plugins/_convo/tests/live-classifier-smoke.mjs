// Live check of the Classifier card against an ISOLATED test host (default http://127.0.0.1:5028/), with the real Agent Zero page,
// the real Convo plugin (and System One and the decision plugin, when installed) and a throwaway headless browser.
//
// It checks: the card renders like the Conversation card (the computed title, description, note and field spacing of the three
// Connections cards are identical); the provider select lists Agent Zero's providers in its groups; model search goes through
// Agent Zero's modelConfig store (spied and answered in the page: nothing is searched for real); the System One toggle locks the
// fields with System One's values and a verdict, and unlocking brings Convo's own values back; a public address is refused by
// Check server before anything is sent and by the save without consent, and saved with it; Check server answers inline for the
// local d1 address. This script SAVES settings and ALWAYS puts back the Convo settings it found
// (compared at the end). It starts no voice session and loads no model; Check server asks the address shown and, for a decision
// server, sends one short text decision. Run it only after deploying to the test host, with Convo on.
//
//   node tests/live-classifier-smoke.mjs [url]        exit 0 = every check passed
//   CONVO_LIVE_EXPECT_D1=1 ...                        also require Check server to find a decision model at :9931
//
// Not part of the default node test run (its name does not end in .test.mjs). Refuses any non-loopback address and the
// production port 5027.
import { launch, sleep, contrast, assertLoopback } from './live-browser.mjs';

const url = assertLoopback(process.argv[2] || process.env.CONVO_LIVE_UI_URL || 'http://127.0.0.1:5028/');
if (new URL(url).port === '5027') {
    console.error('FAIL  refusing the production port 5027: this script saves settings');
    process.exit(2);
}
const D1 = 'http://host.docker.internal:9931/v1';
const S = `Alpine.store('convoSetup')`;
const MODAL = '.convo-modal';
const CARD = `${MODAL} .convo-classifier-card`;
const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok: !!ok }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`); };
const canonical = value => JSON.stringify(value, (key, item) => (item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) : item));
let browser, page, original = null, restored = false;
const watchdog = setTimeout(async () => { console.error('FAIL  overall time limit (10 minutes)'); try { await restore(); await browser?.close(); } catch { /* nothing more to do */ } process.exit(2); }, 10 * 60 * 1000);
watchdog.unref();

const visible = selector => page.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); return !!e && e.offsetParent !== null; })()`);
const draft = () => page.eval(`(() => { const s = ${S}, r = s.config.roles.classifier; return { provider: r.provider || '', model: r.model || '', api_base: r.api_base || '', kind: r.kind || '',
    remote: !!r.remote, source: s.config.decider.source, dirty: s.dirty, error: s.error, check: JSON.parse(JSON.stringify(s.ui.classifier.check || null)) }; })()`);
// Choose a provider the way a user does: the select's own change event.
const choose = id => page.eval(`(() => { const el = document.querySelector('${CARD} [data-convo-field="classifier-provider"]'); el.value = ${JSON.stringify(id)}; el.dispatchEvent(new Event('change', { bubbles: true })); return el.value; })()`);
const toggle = async name => { await page.click(`${CARD} [data-convo-field="${name}"] + .toggler`); await sleep(400); };
async function save() {
    const before = await page.eval(`${S}.revision`);
    await page.click(`.convo-modal-footer .convo-primary`);
    await page.waitFor(`!${S}.busy`, 30000);
    await sleep(400);
    return { saved: (await page.eval(`${S}.revision`)) !== before, ...(await draft()) };
}
// Put back exactly what was saved when the script started, through the same revision-aware save the editor uses (once).
async function restore() {
    if (restored) return true;
    if (!original || !page) return false;
    const after = await page.eval(`(async () => { const s = ${S}; const now = await s.api('setup_read');
        await s.api('setup_save', { revision: now.revision, config: ${JSON.stringify(original)}, keys: {} });
        const back = await s.api('setup_read'); s.discard(); await s.load(true); return back.config; })()`);
    restored = canonical(after) === canonical(original);
    return restored;
}
// The computed text styles of a card's first visible field, its note and its legend.
const styleOf = card => page.eval(`(() => {
    const root = document.querySelector(${JSON.stringify(card)}); if (!root) return null;
    const field = [...root.querySelectorAll('.field')].find(e => e.offsetParent !== null); if (!field) return null;
    const pick = (el, names) => { if (!el) return null; const cs = getComputedStyle(el); return Object.fromEntries(names.map(n => [n, cs.getPropertyValue(n)])); };
    const text = ['color', 'font-size', 'font-weight', 'font-family', 'line-height', 'opacity', 'letter-spacing'];
    return { title: pick(field.querySelector('.field-title'), text), description: pick(field.querySelector('.field-description'), [...text, 'margin-top', 'margin-bottom']),
             note: pick(root.querySelector('.convo-model-note'), [...text, 'padding-top', 'border-top-color', 'border-radius']),
             field: pick(field, ['margin-top', 'margin-bottom', 'display']), legend: pick(root.querySelector('legend'), text) };
})()`);

browser = await launch({ port: Number(process.env.CONVO_LIVE_DEBUG_PORT || 9335) });
try {
    page = await browser.page(url);
    page.answerDialogs(() => true);                      // "discard unsaved settings?": yes, the script restores what was saved
    await page.viewport({ width: 1400, height: 900 });
    await page.waitFor("typeof window.Alpine?.store?.('convo') === 'object'", 240000, 500);
    await page.eval(`(() => { void Alpine.store('convo').openSettingsModal(); return 1; })()`);
    await page.waitFor(`!!document.querySelector('${CARD}') && !!${S}?.config && ${S}.ui?.classifier?.hostReady === true`, 60000);
    await page.eval(`(() => { ${S}.section = 'Connections'; return 1; })()`);
    await sleep(800);
    original = await page.eval(`(async () => (await ${S}.api('setup_read')).config)()`);
    if (original?.decider?.source === 'system_one') { await toggle('classifier-system-one'); }   // start from Convo's own fields

    // 1. It renders like the Conversation card: the same title, description, note and spacing as the other two Connections cards.
    const head = await page.eval(`(() => { const c = document.querySelector('${CARD}'); return { legend: c.querySelector('legend')?.innerText, note: c.querySelector('.convo-model-note')?.innerText } })()`);
    check('the card is titled Classifier with its note', head.legend === 'Classifier' && /Decides whether speech is meant for Convo and whether to answer\. A decision model \(System One style\) or a chat model that accepts audio\./.test(head.note || ''), JSON.stringify(head));
    const cards = { conversation: `${MODAL} fieldset.convo-model-card:not(.convo-classifier-card):not(.convo-speech)`, classifier: CARD, speech: `${MODAL} fieldset.convo-speech` };
    const computed = Object.fromEntries(await Promise.all(Object.entries(cards).map(async ([name, selector]) => [name, await styleOf(selector)])));
    for (const part of ['title', 'description', 'note', 'field', 'legend']) {
        const values = Object.values(computed).map(style => canonical(style?.[part] ?? null));
        check(`the three Connections cards share the computed ${part} style`, values.every(value => value === values[0] && value !== 'null'), values[0] === values[1] && values[1] === values[2] ? '' : JSON.stringify(Object.fromEntries(Object.entries(computed).map(([k, v]) => [k, v?.[part]]))));
    }
    const descriptionContrast = await page.eval(`(() => { const d = document.querySelector('${CARD} .field .field-description'); const cs = getComputedStyle(d); let bg = 'rgba(0, 0, 0, 0)'; for (let e = d; e && /rgba\\(0, 0, 0, 0\\)|transparent/.test(bg); e = e.parentElement) bg = getComputedStyle(e).backgroundColor; return { fg: cs.color, bg, opacity: cs.opacity }; })()`);
    check('descriptions stay readable', contrast(descriptionContrast.fg, /rgba\(0, 0, 0, 0\)/.test(descriptionContrast.bg) ? 'rgb(255, 255, 255)' : descriptionContrast.bg) >= 4.5 || Number(descriptionContrast.opacity) < 1, JSON.stringify(descriptionContrast));

    // 2. One provider select, grouped, listing Agent Zero's own providers.
    const select = await page.eval(`(() => { const el = document.querySelector('${CARD} [data-convo-field="classifier-provider"]'); const host = (Alpine.store('modelConfig').chatProviders || []).map(p => p.value);
        const ids = [...el.options].map(o => o.value); return { groups: [...el.querySelectorAll('optgroup')].map(g => g.label), selects: document.querySelectorAll('${CARD} select[data-convo-field="classifier-provider"]').length, host, missing: host.filter(id => !ids.includes(id)), local: [...(el.querySelector('optgroup[label="Local servers"]')?.querySelectorAll('option') || [])].map(o => o.value) }; })()`);
    check('one provider select in the agreed group order', select.selects === 1 && ['Local servers', 'Cloud', 'Other providers', 'Other'].filter(label => select.groups.includes(label)).join() === select.groups.join() && select.groups[0] === 'Local servers', JSON.stringify(select.groups));
    check('every Agent Zero provider is listed', select.host.length > 0 && select.missing.length === 0, JSON.stringify(select.missing));
    check('the local decision servers come first', select.local[0] === 'llama_cpp' && select.local.includes('lm_studio') && select.local.includes('ollama'), JSON.stringify(select.local));
    // For the record (field names only, no values): what Agent Zero's provider details carry, which decides "OpenAI-compatible".
    const detailShape = await page.eval(`(() => { const d = Alpine.store('modelConfig').chatProviderDetails; const first = Array.isArray(d) ? d[0] : d && Object.values(d)[0]; return { array: Array.isArray(d), keys: first && typeof first === 'object' ? Object.keys(first) : [] }; })()`);
    console.log('INFO  chatProviderDetails shape: ' + JSON.stringify(detailShape));

    // 3. Model search goes through Agent Zero's store (spied and answered in the page; nothing is searched for real).
    await choose('llama_cpp');
    await page.eval(`(() => { const m = Alpine.store('modelConfig'); window.__classifierSpy = { search: [], group: 0, original: { search: m.searchModels, group: m.groupResults } };
        m.searchModels = async (...args) => { window.__classifierSpy.search.push(args); return ['spy-d1-omni-600M', 'spy-qwen3']; };
        m.groupResults = function (models, query) { window.__classifierSpy.group++; return window.__classifierSpy.original.group.call(this, models, query); }; return 1; })()`);
    try {
        await page.click(`${CARD} [data-convo-field="classifier-model"] ~ .model-search-btn`);
        await page.waitFor(`document.querySelectorAll('${CARD} .model-search-item[role="option"]').length > 0`, 10000);
        const spied = await page.eval(`(() => ({ search: JSON.parse(JSON.stringify(window.__classifierSpy.search)), group: window.__classifierSpy.group, items: [...document.querySelectorAll('${CARD} .model-search-item[role="option"]')].map(e => e.textContent) }))()`);
        check('model search goes through Agent Zero’s store for the chosen provider', spied.search.length === 1 && spied.search[0][0] === 'llama_cpp' && spied.search[0][2] === 'chat', JSON.stringify(spied.search));
        check('results are grouped by Agent Zero’s own groupResults', spied.group > 0 && spied.items.join() === 'spy-d1-omni-600M,spy-qwen3', JSON.stringify(spied));
        await page.eval(`(() => { document.querySelector('${CARD} [data-convo-field="classifier-model"]').focus(); return 1; })()`);
        await page.key('ArrowDown'); await page.key('Escape');
        check('Escape closes only the list', !(await visible(`${CARD} .model-search-results`)) && await visible(CARD));
    } finally {
        await page.eval(`(() => { const m = Alpine.store('modelConfig'), o = window.__classifierSpy?.original; if (o) { m.searchModels = o.search; m.groupResults = o.group; } delete window.__classifierSpy; return 1; })()`);
    }

    // 4. The System One toggle locks the fields with System One's values and a verdict; off brings Convo's values back.
    await page.eval(`(() => { ${S}.config.roles.classifier.model = 'convo-own-model'; return 1; })()`);
    const available = await page.eval(`${S}.classifierInheritAvailable()`);
    if (available) {
        await toggle('classifier-system-one');
        const locked = await page.eval(`(() => { const s = ${S}, c = document.querySelector('${CARD}'); const v = c.querySelector('[data-convo-field="classifier-verdict"]'); const cs = v && getComputedStyle(v);
            return { on: s.classifierUsesSystemOne(), own: !!c.querySelector('[data-convo-classifier="own"]'), readonly: [...c.querySelectorAll('[data-convo-classifier="system-one"] input')].map(i => [i.readOnly, i.value]),
                     reference: JSON.parse(JSON.stringify(s.classifierReference())), verdict: v?.innerText || '', icon: v?.querySelector('x-icon')?.getAttribute('aria-hidden'), fg: cs?.color, link: !!c.querySelector('[data-convo-field="classifier-open-system-one"]') }; })()`);
        check('on, the own fields are replaced by System One’s, read-only', locked.on && !locked.own && locked.readonly.length === 4 && locked.readonly.every(([ro]) => ro)
            && locked.readonly.map(([, value]) => value).join('|') === [locked.reference.provider, locked.reference.model, locked.reference.host, locked.reference.context].join('|'), JSON.stringify(locked));
        check('the verdict is words plus a hidden icon, with a link to change it', locked.verdict.length > 10 && locked.icon === 'true' && locked.link, JSON.stringify({ verdict: locked.verdict, icon: locked.icon }));
        const opened = await page.eval(`(async () => { const t = Date.now(); const ok = await ${S}.classifierOpenSystemOne(); return { ok, ms: Date.now() - t }; })()`);
        await sleep(900);
        const settings = await page.eval(`(() => ({ tab: Alpine.store('settings')?.activeTab || '', modals: document.querySelectorAll('.modal').length }))()`);
        check('the link opens Agent settings without waiting for it to close', opened.ok && opened.ms < 1500 && settings.tab === 'agent', JSON.stringify({ opened, settings }));
        await page.eval(`(() => { window.closeModal?.(); return 1; })()`);
        await sleep(600);
        await toggle('classifier-system-one');
        const back = await draft();
        check('off, Convo’s own values come back unchanged', back.source !== 'system_one' && back.provider === 'llama_cpp' && back.model === 'convo-own-model' && await visible(`${CARD} [data-convo-field="classifier-provider"]`), JSON.stringify(back));
    } else check('System One is not installed: the toggle is disabled', await page.eval(`document.querySelector('${CARD} [data-convo-field="classifier-system-one"]').disabled`));

    // 5. A public address: refused by Check server before sending and by the save without consent; saved with it.
    await choose('openai');
    await page.eval(`(() => { const s = ${S}; s.config.roles.classifier.remote = false; s.config.roles.classifier.model = 'gpt-4o-mini-audio-preview'; s.error = ''; return 1; })()`);
    check('a public address asks for remote-processing consent', await visible(`${CARD} [data-convo-field="classifier-remote"]`) && /api\.openai\.com/.test(await page.eval(`${S}.classifierRemoteText()`)));
    await page.eval(`(async () => { await ${S}.classifierCheck(); return 1; })()`);
    const refusedCheck = await draft();
    check('Check server refuses it inline before sending anything', refusedCheck.check?.state === 'refused' && /consent/.test(refusedCheck.check?.message || '') && !refusedCheck.error, JSON.stringify(refusedCheck.check));
    const refused = await save();
    check('the server refuses to save it without consent', !refused.saved && refused.dirty && !!refused.error, JSON.stringify(refused));
    await page.eval(`(() => { ${S}.error = ''; return 1; })()`);
    await toggle('classifier-remote');
    const consented = await save();
    check('with consent it is saved', consented.saved && !consented.dirty && consented.provider === 'openai' && consented.remote, JSON.stringify(consented));

    // 6. Check server against the local d1 decision server (it lists the models and sends one short text decision).
    await choose('llama_cpp');
    await page.eval(`(() => { const s = ${S}; s.classifierSet('api_base', ${JSON.stringify(D1)}); s.classifierSetKind('decision'); return 1; })()`);
    await page.eval(`(async () => { await ${S}.classifierCheck(); return 1; })()`);
    const d1 = await draft();
    const shown = await page.eval(`document.querySelector('${CARD} [data-convo-field="classifier-check-result"]')?.innerText || ''`);
    const expect = process.env.CONVO_LIVE_EXPECT_D1 ? ['decision'] : ['decision', 'chat', 'unreachable', 'unverified'];
    check('Check server answers inline for the local d1 address', expect.includes(d1.check?.state) && shown.length > 10 && !d1.error, JSON.stringify({ check: d1.check, shown }));
} catch (error) {
    check('the classifier check completed', false, error.message);
} finally {
    if (original) {
        try { check('the Convo settings found at the start are restored exactly', await restore()); }
        catch (error) { check('restoring the settings', false, error.message); }
        if (!restored) console.log('Restore these Convo settings by hand (setup_save):\n' + JSON.stringify(original));
    }
    if (page) check('no console warning or error', page.problems().length === 0, page.problems().slice(0, 3).map(p => p.text.slice(0, 160)).join(' | '));
    page?.close();
    await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
