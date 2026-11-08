// Live check of setting up connections the way a user does, against an ISOLATED test host (default http://127.0.0.1:5028/) with the
// real Agent Zero page, a throwaway headless browser and the servers that really run on this computer.
//
// It checks: Test connection (the classifier's Check server) is on each card, not in Advanced; a speech server typed as
// http://localhost:8890/v1 is reached from inside Agent Zero's container and says so ("This computer …"), listing its models; the
// container's internal port (8080) is refused with the published-port hint and one click on "Use port 8890" fixes it; the decision
// router at https://localhost:18201/v1 is reached, its private certificate is remembered without a click and shown, a missing key
// is reported as refused, and Forget removes the certificate; a model name typed on a server that refuses the key shows that reason
// in the list. Everything is a draft: nothing is saved, the remembered certificate is forgotten again, and the Convo settings it
// found are compared (and put back if needed) at the end. It starts no voice session and loads no model. Speech Test leaves its
// usual evidence record; the speech server only gets read-only GETs.
//
//   node tests/live-setup-smoke.mjs [url]        exit 0 = every check passed
//
// Needs the audio.cpp container on 8890 and the decision router on 18201 of this computer. Not part of the default node test run.
// Refuses any non-loopback address and the production port 5027.
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
const ROUTER = 'https://localhost:18201/v1';
const results = [];
const check = (name, ok, detail = '') => { results.push(!!ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`); };
const canonical = value => JSON.stringify(value, (key, item) => (item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) : item));
let browser, page, original = null;
const watchdog = setTimeout(async () => { console.error('FAIL  overall time limit (8 minutes)'); try { await browser?.close(); } catch { /* nothing more */ } process.exit(2); }, 8 * 60 * 1000);
watchdog.unref();

const shownOutsideAdvanced = selector => page.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); return !!e && e.offsetParent !== null && !e.closest('.advanced-body'); })()`);
const text = selector => page.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); return e && e.offsetParent !== null ? e.innerText.trim() : ''; })()`);
async function type(selector, value) {
    await page.click(selector);
    await page.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); e.select(); return 1; })()`);
    await page.send('Input.insertText', { text: value });
    await sleep(300);
}
const clickButton = (card, label) => page.eval(`(() => { const b = [...document.querySelectorAll(${JSON.stringify(card + ' button')})].find(item => item.textContent.trim() === ${JSON.stringify(label)} && item.offsetParent !== null); if (!b) return false; b.click(); return true; })()`);

browser = await launch({ port: Number(process.env.CONVO_LIVE_DEBUG_PORT || 9337) });
try {
    page = await browser.page(url);
    page.answerDialogs(() => true);
    await page.viewport({ width: 1400, height: 900 });
    await page.waitFor("typeof window.Alpine?.store?.('convo') === 'object'", 240000, 500);
    await page.eval(`(() => { void Alpine.store('convo').openSettingsModal(); return 1; })()`);
    await page.waitFor(`!!document.querySelector('${CONVERSATION}') && !!${S}?.config`, 60000);
    await page.eval(`(() => { ${S}.section = 'Connections'; return 1; })()`);
    await sleep(800);
    original = await page.eval(`(async () => (await ${S}.api('setup_read')).config)()`);
    const network = await page.eval(`(() => ({ ...${S}.network }))()`);
    check('Agent Zero runs in Docker here and translates this computer’s addresses', network.in_docker === true && network.translated === true, JSON.stringify(network));

    // 1. Test is on every card, not in Advanced.
    check('the conversation card’s Test connection is on the card', await shownOutsideAdvanced(`${CONVERSATION} [data-convo-field="connection-test"]`));
    check('the classifier card’s Check server is on the card', await shownOutsideAdvanced(`${CLASSIFIER} [data-convo-field="classifier-check"]`));

    // 2. Speech: the real address typed as localhost.
    const speechUrl = `${SPEECH} input[type="url"]`;
    await page.eval(`(() => { ${S}.config.roles.tts.provider = 'audiocpp'; ${S}.config.roles.tts.connection = 'external'; return 1; })()`);
    await sleep(300);
    await type(speechUrl, 'http://localhost:8890/v1');
    const here = await text(`${SPEECH} [data-convo-field="speech-this-computer"]`);
    check('typing http://localhost:8890/v1 says this computer is reached through host.docker.internal', /through host\.docker\.internal/.test(here), here);
    await clickButton(SPEECH, 'Test connection');
    await page.waitFor(`!${S}.choices.tts?.busy && !!${S}.choices.tts?.verification`, 60000);
    const speech = await page.eval(`(() => ({ state: ${S}.choices.tts.verification.state, models: (${S}.choices.tts.models || []).map(m => m.id), voices: (${S}.choices.tts.voices || []).length }))()`);
    check('the speech server answers at localhost and lists its models', ['ready', 'unverified'].includes(speech.state) && speech.models.includes('qwen3-tts-1.7b-base-bf16'), JSON.stringify(speech));

    // 3. The container's internal port: the hint and one click.
    await type(speechUrl, 'http://localhost:8080/v1');
    await page.eval(`(() => { ${S}.choices.tts = {}; return 1; })()`);
    await clickButton(SPEECH, 'Test connection');
    await page.waitFor(`!${S}.choices.tts?.busy && !!${S}.choices.tts?.verification`, 60000);
    const refused = await page.eval(`(() => ({ message: ${S}.choices.tts.verification.message, suggestion: ${S}.choices.tts.suggestion }))()`);
    const button = await text(`${SPEECH} [data-convo-field="speech-connection-suggestion"] button`);
    check('the internal port is refused with the published-port hint and a Use port 8890 button', /Docker publishes/.test(refused.message) && button === 'Use port 8890', JSON.stringify({ message: refused.message.slice(0, 90), button }));
    await page.click(`${SPEECH} [data-convo-field="speech-connection-suggestion"] button`);
    await page.waitFor(`!${S}.choices.tts?.busy && ${S}.config.roles.tts.url === 'http://localhost:8890/v1' && !!${S}.choices.tts?.verification`, 60000);
    const fixed = await page.eval(`${S}.choices.tts.verification.state`);
    check('one click puts 8890 in the address and the test passes', ['ready', 'unverified'].includes(fixed), fixed);

    // 4. The decision router: HTTPS with your own certificate, remembered without a click.
    await page.eval(`(() => { ${S}.classifierSetProvider('llama_cpp'); return 1; })()`);
    await sleep(400);
    await type(`${CLASSIFIER} [data-convo-field="classifier-api-base"]`, ROUTER);
    await type(`${CLASSIFIER} [data-convo-field="classifier-model"]`, 'd1-omni-600m');
    await page.eval(`(() => { document.activeElement?.blur?.(); return 1; })()`);
    await page.click(`${CLASSIFIER} [data-convo-field="classifier-check"]`);
    await page.waitFor(`${S}.ui.classifier.checking === false && !!${S}.ui.classifier.check`, 120000);
    const router = await page.eval(`(() => ({ state: ${S}.ui.classifier.check.state, message: ${S}.ui.classifier.check.message }))()`);
    const note = await text(`${CLASSIFIER} [data-convo-field="classifier-certificate-note"]`);
    check('the router is reached over HTTPS and its private certificate is remembered without a click', /Private certificate remembered/.test(note) && /DJsLaptop/.test(note), note.slice(0, 120));
    check('without its key the router is reported as refusing the key', router.state === 'refused' && /refused the key/.test(router.message), JSON.stringify(router));
    await page.click(`${CLASSIFIER} [data-convo-field="classifier-certificate-forget"]`);
    await sleep(800);
    const forgotten = await page.eval(`(async () => ({ note: ${S}.certificateNote('classifier'), saved: (await ${S}.api('setup_read')).certificates }))()`);
    check('Forget removes the remembered certificate', forgotten.note === '', JSON.stringify(forgotten));

    // 5. The model list of a server that refuses the key says so.
    await page.eval(`(() => { ${S}.conversationSetProvider('llama_cpp'); return 1; })()`);
    await sleep(400);
    await type(`${CONVERSATION} [data-convo-field="api-base"]`, 'http://127.0.0.1:18080/v1');
    await type(`${CONVERSATION} [data-convo-field="model"]`, 'gem');
    await page.waitFor(`(() => { const item = [...document.querySelectorAll('${CONVERSATION} .model-search-item.disabled')].find(e => e.offsetParent !== null); return !!item && /refused the key/.test(item.innerText); })()`, 60000).catch(() => null);
    const listNote = await text(`${CONVERSATION} .model-search-results .model-search-item.disabled`);
    check('typing a model name asks the server and shows why it found nothing', /refused the key/.test(listNote), listNote.slice(0, 100));

    const problems = page.problems().filter(entry => entry.type !== 'warning');
    check('no console errors or exceptions', problems.length === 0, problems.slice(0, 3).map(p => p.text.slice(0, 200)).join(' | '));
} catch (error) {
    check('the setup check completed', false, error.message);
} finally {
    if (page && original) {
        await page.eval(`(async () => { try { await ${S}.api('tls_forget', { url: ${JSON.stringify(ROUTER)} }); } catch { /* nothing remembered */ } return 1; })()`).catch(() => null);
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
