// Live check of the connection rules every model card shares, against an ISOLATED test host (default http://127.0.0.1:5028/), with
// the real Agent Zero page and a throwaway headless browser.
//
// It checks: Settings opens and lists no credential-reference problem; llama.cpp (a server you run yourself) shows its address as
// a main field with the LAN/Tailscale hint, not as an Advanced override, in the Conversation and Classifier cards; a Tailscale or
// LAN address asks for no consent while a public one does; typing an address into a credential environment reference shows the
// reason with Clear (real keyboard input), the Save is refused naming the field and never echoing the value, and Clear empties
// it; the speech card shows the same hint and check. The script saves nothing it keeps: the refused save writes nothing, and the
// Convo settings it found are compared (and put back if needed) at the end. It starts no voice session, loads no model and
// contacts no model server.
//
//   node tests/live-connection-smoke.mjs [url]        exit 0 = every check passed
//
// Not part of the default node test run (its name does not end in .test.mjs). Refuses any non-loopback address and the
// production port 5027.
import { launch, sleep, assertLoopback } from './live-browser.mjs';

const url = assertLoopback(process.argv[2] || process.env.CONVO_LIVE_UI_URL || 'http://127.0.0.1:5028/');
if (new URL(url).port === '5027') {
    console.error('FAIL  refusing the production port 5027');
    process.exit(2);
}
const S = `Alpine.store('convoSetup')`;
const MODAL = '.convo-modal';
const CONVERSATION = `${MODAL} fieldset.convo-model-card:not(.convo-classifier-card):not(.convo-speech)`;
const CLASSIFIER = `${MODAL} .convo-classifier-card`;
const SPEECH = `${MODAL} fieldset.convo-speech`;
const TAILNET = 'http://my-desktop.tail1234.ts.net:8081/v1';
const KEYLIKE = 'sk-or-v1-0123456789abcdef0123';
const results = [];
const check = (name, ok, detail = '') => { results.push(!!ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`); };
const canonical = value => JSON.stringify(value, (key, item) => (item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) : item));
let browser, page, original = null;
const watchdog = setTimeout(async () => { console.error('FAIL  overall time limit (6 minutes)'); try { await browser?.close(); } catch { /* nothing more */ } process.exit(2); }, 6 * 60 * 1000);
watchdog.unref();

const visible = selector => page.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); return !!e && e.offsetParent !== null; })()`);
const exists = selector => page.eval(`!!document.querySelector(${JSON.stringify(selector)})`);
// Type like a user: focus, select what is there, insert the text (real input events).
async function type(selector, text) {
    await page.click(selector);
    await page.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); e.select(); return 1; })()`);
    if (text) await page.send('Input.insertText', { text });
    else await page.key('Backspace');
    await sleep(300);
}
const chooseConversation = id => page.eval(`(() => { const el = document.querySelector('${CONVERSATION} [data-convo-field="provider"]'); el.value = ${JSON.stringify(id)}; el.dispatchEvent(new Event('change', { bubbles: true })); return el.value; })()`);
const chooseClassifier = id => page.eval(`(() => { const el = document.querySelector('${CLASSIFIER} [data-convo-field="classifier-provider"]'); el.value = ${JSON.stringify(id)}; el.dispatchEvent(new Event('change', { bubbles: true })); return el.value; })()`);
const openAdvanced = card => page.eval(`(() => { const t = document.querySelector('${card} .convo-model-advanced .advanced-toggle'); if (t && t.getAttribute('aria-expanded') !== 'true') t.click(); return 1; })()`);

browser = await launch({ port: Number(process.env.CONVO_LIVE_DEBUG_PORT || 9336) });
try {
    page = await browser.page(url);
    page.answerDialogs(() => true);
    await page.viewport({ width: 1400, height: 900 });
    await page.waitFor("typeof window.Alpine?.store?.('convo') === 'object'", 240000, 500);
    await page.eval(`(() => { void Alpine.store('convo').openSettingsModal(); return 1; })()`);
    await page.waitFor(`!!document.querySelector('${CONVERSATION}') && !!${S}?.config`, 60000);
    await page.eval(`(() => { ${S}.section = 'Connections'; return 1; })()`);
    await sleep(800);
    const read = await page.eval(`(async () => { const r = await ${S}.api('setup_read'); return { ok: r.ok, config: r.config, rows: r.readiness.filter(c => c.id.endsWith('token_env')).map(c => c.id) }; })()`);
    original = read.config;
    check('Settings opens and reports no credential-reference problem', read.ok === true && read.rows.length === 0, JSON.stringify(read.rows));

    // 1. llama.cpp in the Conversation card: the address is a main field with the LAN/Tailscale hint.
    await chooseConversation('llama_cpp');
    await sleep(400);
    await openAdvanced(CONVERSATION);
    const base = await page.eval(`(() => { const f = document.querySelector('${CONVERSATION} [data-convo-field="api-base"]'); const field = f?.closest('.field');
        return { shown: !!f && f.offsetParent !== null, placeholder: f?.placeholder, text: field?.querySelector('.field-description')?.innerText || '', override: !!document.querySelector('${CONVERSATION} [data-convo-field="api-base-override"]') }; })()`);
    check('llama.cpp shows API base URL as a main field, not an Advanced override', base.shown && !base.override && base.placeholder === 'http://host.docker.internal:8080/v1', JSON.stringify({ ...base, text: undefined }));
    check('its hint says how to reach another computer over LAN or Tailscale', /Leave it empty for this machine/.test(base.text) && /LAN or Tailscale/.test(base.text), base.text);

    // 2. A Tailscale address needs no consent; a public one does. Start from consent off (draft only; the saved settings are
    // compared at the end), since a consent already given keeps its box visible on purpose.
    await page.eval(`(() => { ${S}.config.roles.conversation.remote = false; return 1; })()`);
    await type(`${CONVERSATION} [data-convo-field="api-base"]`, TAILNET);
    const tail = await page.eval(`(() => ({ base: ${S}.config.roles.conversation.api_base, shown: ${S}.conversationRemoteShown(), problem: ${S}.conversationAddressProblem(), text: ${S}.conversationRemoteText() }))()`);
    check('a Tailscale address asks for no consent', tail.base === TAILNET && tail.shown === false && tail.problem === '' && /LAN or Tailscale/.test(tail.text), JSON.stringify(tail));
    await type(`${CONVERSATION} [data-convo-field="api-base"]`, 'http://203.0.113.9:8081/v1');
    const pub = await page.eval(`(() => ({ shown: ${S}.conversationRemoteShown(), problem: ${S}.conversationAddressProblem() }))()`);
    check('a public address asks for consent', pub.shown === true && /consent/.test(pub.problem), JSON.stringify(pub));
    await type(`${CONVERSATION} [data-convo-field="api-base"]`, 'http://192.168.1.50:8081/v1');
    check('a LAN address asks for no consent', (await page.eval(`${S}.conversationAddressProblem()`)) === '');

    // 3. An address typed into the credential reference: the reason with Clear; the Save is refused naming the field.
    await type(`${CONVERSATION} [data-convo-field="token-env"]`, 'http://host.docker.internal:8081/v1');
    const inline = await page.eval(`(() => { const p = document.querySelector('${CONVERSATION} [data-convo-field="token-env-problem"]'), i = document.querySelector('${CONVERSATION} [data-convo-field="token-env"]');
        return { shown: !!p && p.offsetParent !== null, text: p?.innerText || '', invalid: i?.getAttribute('aria-invalid'), clear: !!p?.querySelector('[data-convo-field="token-env-clear"]') }; })()`);
    check('the credential reference shows why, with Clear', inline.shown && /looks like an address/.test(inline.text) && inline.invalid === 'true' && inline.clear, JSON.stringify(inline));
    await type(`${CONVERSATION} [data-convo-field="token-env"]`, KEYLIKE);
    const revision = await page.eval(`${S}.revision`);
    await page.click(`.convo-modal-footer .convo-primary`);
    await page.waitFor(`!${S}.busy`, 30000);
    await sleep(500);
    const refused = await page.eval(`(async () => ({ error: ${S}.error || '', revision: ${S}.revision, saved: (await ${S}.api('setup_read')).config.roles.conversation.token_env }))()`);
    check('Save is refused naming the field, and the value is neither echoed nor stored', refused.revision === revision && /Conversation model/.test(refused.error) && !refused.error.includes(KEYLIKE) && refused.saved === original.roles.conversation.token_env,
        JSON.stringify({ error: refused.error.slice(0, 160), stored_unchanged: refused.saved === original.roles.conversation.token_env }));
    await page.click(`${CONVERSATION} [data-convo-field="token-env-clear"]`);
    const cleared = await page.eval(`(() => ({ value: ${S}.config.roles.conversation.token_env, problem: ${S}.conversationReferenceProblem(), shown: !!document.querySelector('${CONVERSATION} [data-convo-field="token-env-problem"]')?.offsetParent }))()`);
    check('Clear empties it and the reason goes away', cleared.value === '' && cleared.problem === '' && !cleared.shown, JSON.stringify(cleared));

    // 4. The Classifier card: llama.cpp's address is a main field too.
    await chooseClassifier('llama_cpp');
    await sleep(400);
    const classifier = await page.eval(`(() => { const f = document.querySelector('${CLASSIFIER} [data-convo-field="classifier-api-base"]'); return { shown: !!f && f.offsetParent !== null,
        text: f?.closest('.field')?.querySelector('.field-description')?.innerText || '', override: !!document.querySelector('${CLASSIFIER} [data-convo-field="classifier-api-base-override"]') }; })()`);
    check('the Classifier card shows llama.cpp’s address as a main field with the same hint', classifier.shown && !classifier.override && /LAN or Tailscale/.test(classifier.text), JSON.stringify({ ...classifier, text: classifier.text.slice(-80) }));

    // 5. The speech card: the hint and the credential reference check.
    const speech = await page.eval(`(() => ({ hint: ${S}.speechUrlHint(), cloud: ${S}.speechSpec().group }))()`);
    check('the speech server hint names LAN or Tailscale', speech.cloud === 'cloud' || /LAN or Tailscale/.test(speech.hint), speech.hint);
    if (await exists(`${SPEECH} [data-convo-field="speech-token-env"]`)) {
        await page.eval(`(() => { ${S}.config.roles.tts.token_env = 'http://x:1/v1'; return 1; })()`);
        await sleep(300);
        const sp = await page.eval(`(() => ({ problem: ${S}.speechReferenceProblem() }))()`);
        await page.eval(`(() => { ${S}.speechClearReference(); return 1; })()`);
        check('the speech card checks its credential reference too', /looks like an address/.test(sp.problem) && (await page.eval(`${S}.speechReferenceProblem()`)) === '', sp.problem.slice(0, 120));
    }

    const problems = page.problems().filter(entry => entry.type !== 'warning');
    check('no console errors or exceptions', problems.length === 0, problems.slice(0, 3).map(p => p.text.slice(0, 200)).join(' | '));
} catch (error) {
    check('the connection check completed', false, error.message);
} finally {
    if (page && original) {
        const after = await page.eval(`(async () => { const s = ${S}; s.discard?.(); const now = await s.api('setup_read'); return now.config; })()`).catch(() => null);
        if (canonical(after) !== canonical(original)) {
            await page.eval(`(async () => { const s = ${S}; const now = await s.api('setup_read'); await s.api('setup_save', { revision: now.revision, config: ${JSON.stringify(original)}, keys: {} }); return 1; })()`).catch(() => null);
        }
        const back = await page.eval(`(async () => (await ${S}.api('setup_read')).config)()`).catch(() => null);
        check('the saved Convo settings are exactly as found', canonical(back) === canonical(original));
    }
    await browser.close();
}
console.log(`\n${results.filter(Boolean).length}/${results.length} checks passed`);
process.exit(results.every(Boolean) ? 0 : 1);
