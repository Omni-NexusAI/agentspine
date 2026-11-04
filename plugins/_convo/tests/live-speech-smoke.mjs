// Live check of the speech output editor against an ISOLATED test host (default http://127.0.0.1:5028/): the real Agent Zero page,
// the real Convo settings modal, a throwaway headless browser (tests/live-browser.mjs). It edits a draft and discards it at the end:
// settings writes, key writes and runtime commands are blocked inside the page, no voice session is started and no model is loaded.
//
// What it checks: every speech provider is selected twice through the real <select> (and both audio.cpp connections), the store and
// the select agree each time; delivery instructions appear only where they are honoured; managed audio.cpp shows its reason while
// the runtime is off; browser-only host speech is listed but cannot be chosen; read-only discovery against your audio.cpp container
// (only when it answers a read-only GET; skipped with a note otherwise) lists its models and clone voices and leaves the container's
// model state exactly as it was; the console stays clean.
//
//   node tests/live-speech-smoke.mjs [url]        exit 0 = every check passed
//   CONVO_LIVE_AUDIOCPP          the container as this machine reaches it (default http://127.0.0.1:8890/v1, loopback only)
//   CONVO_LIVE_AUDIOCPP_SERVER   the same container as the test host's server reaches it (default http://host.docker.internal:8890/v1)
//
// Not part of the default node test run (its name does not end in .test.mjs); run it after deploying to the test host.
import { launch, sleep, assertLoopback } from './live-browser.mjs';

const url = assertLoopback(process.argv[2] || process.env.CONVO_LIVE_UI_URL || 'http://127.0.0.1:5028/');
const container = assertLoopback(process.env.CONVO_LIVE_AUDIOCPP || 'http://127.0.0.1:8890/v1').replace(/\/$/, '');
const containerFromServer = process.env.CONVO_LIVE_AUDIOCPP_SERVER || 'http://host.docker.internal:8890/v1';
const READ_ROUTES = ['/models', '/backend/models', '/voices', '/tuning/profiles', '/audio/speech'];
const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok: !!ok, detail: String(detail) }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`); };
const skip = (name, why) => console.log(`SKIP  ${name}  ${why}`);
let browser;
const watchdog = setTimeout(async () => { console.error('FAIL  overall time limit (10 minutes)'); try { await browser?.close(); } catch { /* nothing more to do */ } process.exit(2); }, 10 * 60 * 1000);
watchdog.unref();

// One read-only GET from this machine (never a POST): whether the container answers, and its model state for the before/after check.
async function containerState() {
    try {
        const response = await fetch(container + '/backend/models', { signal: AbortSignal.timeout(3000), redirect: 'manual' });
        if (!response.ok) return null;
        const state = await response.json();
        return { current: state.current, state: state.state, loaded: [...(state.loaded_models || [])].sort() };
    } catch { return null; }
}

browser = await launch({ port: Number(process.env.CONVO_LIVE_DEBUG_PORT || 9334) });
let page;
try {
    page = await browser.page(url);
    page.answerDialogs(() => false);                                                           // a confirm would mean something wanted to throw away or overwrite: decline
    await page.viewport({ width: 1400, height: 900 });
    await page.waitFor("typeof window.Alpine?.store?.('convo') === 'object' && !!document.getElementById('microphone-button')?.getAttribute('data-convo')", 240000, 500);

    // Writes are blocked in the page: this check edits a draft only. Every Convo settings action is recorded.
    await page.eval(`(() => {
        const log = window.__convoSpeechSmoke = { actions: [], blocked: [] };
        const original = window.fetch.bind(window);
        const WRITES = ['setup_save', 'setup_patch', 'import_apply', 'runtime_start', 'runtime_stop', 'conversation_key_copy', 'audio_probe'];
        window.fetch = async (input, init = {}) => {
            const target = String(input?.url || input);
            let action = '';
            try { action = JSON.parse(init.body || '{}').action || ''; } catch { /* not JSON */ }
            if (target.includes('/plugins/_convo/conversation')) log.actions.push(action);
            // Agent Zero's own key-status read (api_keys 'get': has_key per provider) is allowed; writing or revealing a key is not.
            if ((target.includes('/plugins/_convo/conversation') && WRITES.includes(action)) || (target.includes('/api_keys') && action !== 'get')) {
                log.blocked.push(action || target);
                return new Response(JSON.stringify({ ok: false, error: 'Blocked by the live speech check' }), { status: 200, headers: { 'Content-Type': 'application/json' } });
            }
            return original(input, init);
        };
        return 1;
    })()`);

    await page.eval(`Alpine.store('rightCanvas').open('convo').then(() => 1)`);
    await page.waitFor(`!!document.querySelector('.convo-panel .convo-primary')`, 60000);
    await sleep(1500);
    await page.click('.convo-panel button[aria-label="Convo settings"]');
    await page.waitFor(`!!document.querySelector('.convo-speech select')`, 60000);
    await sleep(1500);
    await page.eval(`[...document.querySelectorAll('.convo-tabs button')].find(b => b.textContent.trim() === 'Connections').click()`);
    await sleep(500);
    const original = await page.eval(`JSON.stringify(Alpine.store('convoSetup').config.roles.tts)`);
    check('the speech editor opens on a clean draft', await page.eval(`Alpine.store('convoSetup').dirty === false`));
    const catalog = await page.eval(`(() => { const select = document.querySelector('.convo-speech select'); return { groups: [...select.querySelectorAll('optgroup')].map(g => g.label), options: [...select.options].map(o => ({ id: o.value, disabled: o.disabled, label: o.textContent.trim() })) }; })()`);
    check('providers are grouped like Agent Zero’s', JSON.stringify(catalog.groups.slice(0, 4)) === JSON.stringify(['Agent Zero built-in', 'Local servers', 'Cloud', 'Other']), catalog.groups.join(', '));
    const browserOnly = catalog.options.filter(option => option.id.startsWith('browser:'));
    check('host speech found only in the browser is listed but cannot be chosen', browserOnly.every(option => option.disabled && /browser-only; Convo speaks from the server/.test(option.label)), browserOnly.length ? browserOnly.map(o => o.label).join(' | ') : 'none registered on this host');

    // The page side of one step: choose through the real select (or another real control), then read what the store, the select
    // and the visible markup say.
    const state = () => page.eval(`(() => {
        const s = Alpine.store('convoSetup'), card = document.querySelector('.convo-speech');
        const visible = element => !!element && element.offsetParent !== null;
        const labelled = text => [...card.querySelectorAll('select')].find(el => document.getElementById(el.getAttribute('aria-labelledby'))?.textContent.trim() === text);
        const reason = card.querySelector('.convo-reason');
        return { store: s.speechProvider(), connection: s.speechConnection(), select: labelled('Provider')?.value, connectionSelect: labelled('Connection')?.value,
                 backend: s.config.roles.tts.backend, model: s.config.roles.tts.model, voice: s.config.roles.tts.voice,
                 instructions: visible(card.querySelector('textarea')), supported: s.speechControls().instructions,
                 reason: visible(reason) ? reason.textContent.trim() : '', managed: !!s.config.sidecar.managed };
    })()`);
    const choose = async (label, value) => {
        await page.eval(`(() => { const card = document.querySelector('.convo-speech'); const el = [...card.querySelectorAll('select')].find(x => document.getElementById(x.getAttribute('aria-labelledby'))?.textContent.trim() === ${JSON.stringify(label)}); el.focus(); el.value = ${JSON.stringify(value)}; el.dispatchEvent(new Event('change', { bubbles: true })); return 1; })()`);
        await sleep(400);
        return state();
    };
    // An older configuration on your audio.cpp port is offered as audio.cpp, and confirming keeps its address, model and voice.
    const offered = await page.eval(`Alpine.store('convoSetup').speechOffer()`);
    if (!offered) skip('the audio.cpp suggestion for an older configuration', 'the saved speech settings are not an older configuration on port 8890');
    else {
        const banner = await page.eval(`(() => { const b = document.querySelector('.convo-speech .convo-banner'); return !!b && b.offsetParent !== null; })()`);
        const kept = await page.eval(`(() => { const t = Alpine.store('convoSetup').config.roles.tts; return { url: t.url, model: t.model, voice: t.voice }; })()`);
        await page.eval(`(() => { const b = [...document.querySelectorAll('.convo-speech .convo-banner button')].find(x => /Use audio\\.cpp/.test(x.textContent)); b.setAttribute('data-smoke', 'accept'); return 1; })()`);
        await page.click('[data-smoke="accept"]');
        await sleep(400);
        const accepted = await state();
        const fields = await page.eval(`(() => { const t = Alpine.store('convoSetup').config.roles.tts; return { url: t.url, model: t.model, voice: t.voice }; })()`);
        check('your audio.cpp port is offered, and confirming keeps the address, model and voice', banner && accepted.store === 'audiocpp' && accepted.connection === 'external'
              && JSON.stringify(fields) === JSON.stringify(kept), `${offered} -> ${JSON.stringify(accepted)}`);
        await page.eval(`(() => { Alpine.store('convoSetup').discard(); return 1; })()`);
        await sleep(300);
    }
    const tour = [['kokoro_native', ''], ['audiocpp', 'external'], ['audiocpp', 'managed'], ['llamacpp', ''], ['unsloth_studio', ''], ['openai_tts', ''], ['openai_compatible', '']];
    for (const round of [1, 2]) {
        for (const [provider, connection] of tour) {
            let now = await choose('Provider', provider);
            if (connection) now = await choose('Connection', connection);
            const name = `round ${round}: ${provider}${connection ? ' (' + connection + ')' : ''}`;
            check(`${name}: the store and the select agree`, now.store === provider && now.select === provider && (!connection || (now.connection === connection && now.connectionSelect === connection)), JSON.stringify(now));
            check(`${name}: delivery instructions show exactly where they are honoured`, now.instructions === now.supported, `shown=${now.instructions} honoured=${now.supported} model=${now.model} voice=${now.voice}`);
            if (provider === 'openai_tts') check(`${name}: no clone voice in OpenAI`, !String(now.voice).startsWith('clone:'), now.voice);
            if (provider === 'audiocpp' && connection === 'managed')
                check(`${name}: managed audio.cpp ${now.managed ? 'has its runtime on, so no reason is shown' : 'shows its reason while the runtime is off'}`, now.managed ? !now.reason : /Manage runtime/.test(now.reason), now.reason);
            if (['kokoro_native', 'llamacpp', 'unsloth_studio'].includes(provider) || connection === 'managed') check(`${name}: no delivery instructions here`, !now.instructions);
            if (provider === 'kokoro_native') check(`${name}: Kokoro is ready or says why not`, !now.reason || /Kokoro/.test(now.reason), now.reason || 'available');
        }
    }
    // OpenAI: gpt-4o-mini-tts honours them, tts-1 does not; the typed text waits and comes back.
    await choose('Provider', 'openai_tts');
    let openai = await choose('Model', 'gpt-4o-mini-tts');
    check('OpenAI gpt-4o-mini-tts offers voice delivery', openai.instructions, JSON.stringify(openai));
    await page.eval(`(() => { const t = document.querySelector('.convo-speech textarea'); t.focus(); t.value = 'calm and unhurried'; t.dispatchEvent(new Event('input', { bubbles: true })); return 1; })()`);
    openai = await choose('Model', 'tts-1');
    const parked = await page.eval(`Alpine.store('convoSetup').config.roles.tts.provider_saved?.openai_tts?.instructions || ''`);
    check('OpenAI tts-1 hides voice delivery and keeps the text waiting', !openai.instructions && parked === 'calm and unhurried', `shown=${openai.instructions} waiting=${parked}`);
    openai = await choose('Model', 'gpt-4o-mini-tts');
    check('back on gpt-4o-mini-tts the text returns', openai.instructions && await page.eval(`document.querySelector('.convo-speech textarea').value`) === 'calm and unhurried');
    // A generic endpoint: the declaration is a real checkbox; ticking it shows the field (no clone voice chosen), unticking hides it.
    await choose('Provider', 'openai_compatible');
    const declare = async on => {
        // A host toggle now (Agent Zero's field style): the input is hidden behind its .toggler, which is what a person clicks.
        if (await page.eval(`document.querySelector('[data-convo-field="speech-declare"]').checked`) !== on)
            await page.click('[data-convo-field="speech-declare"] + .toggler');
        await sleep(300);
        return state();
    };
    const declared = await declare(true);
    check('a generic endpoint shows voice delivery once you declare it (no clone voice)', declared.instructions === !String(declared.voice).startsWith('clone:'), JSON.stringify(declared));
    check('and hides it again when you untick the declaration', !(await declare(false)).instructions);

    // Read-only discovery against your audio.cpp container, only when it answers.
    const before = await containerState();
    if (!before) skip('read-only discovery against your audio.cpp container', `${container} did not answer a read-only GET; nothing else was sent to it`);
    else {
        await choose('Provider', 'audiocpp');
        await choose('Connection', 'external');
        await page.eval(`(() => { const input = [...document.querySelectorAll('.convo-speech input[type=url]')].find(x => x.offsetParent !== null); input.focus(); input.value = ${JSON.stringify(containerFromServer)}; input.dispatchEvent(new Event('input', { bubbles: true })); return 1; })()`);
        await sleep(300);
        await page.eval(`(() => { const b = [...document.querySelectorAll('.convo-speech button')].find(x => x.textContent.trim() === 'Find models & voices'); b.setAttribute('data-smoke', 'discover'); return 1; })()`);
        await page.click('[data-smoke="discover"]');
        await page.waitFor(`(() => { const c = Alpine.store('convoSetup').choices.tts; return !!c && !c.busy && !!c.verification; })()`, 90000, 300);
        const found = await page.eval(`(() => { const c = Alpine.store('convoSetup').choices.tts; return { models: (c.models || []).map(m => m.id), voices: (c.voices || []).map(v => v.id), checks: c.checks || [], server: c.server || null, verification: c.verification }; })()`);
        check('discovery lists your container’s models', found.models.length > 0, found.models.join(', '));
        check('discovery lists clone voices by the id speech requests use', found.voices.some(id => id.startsWith('clone:')), found.voices.slice(0, 6).join(', '));
        check('only read-only routes were asked', found.checks.length > 0 && found.checks.every(c => READ_ROUTES.includes(c.path.replace(/^\/v1/, ''))), found.checks.map(c => `${c.path}=${c.status}`).join(' '));
        check('the server’s own model and tuning are shown as it reports them', !!found.server && typeof found.server.model === 'string', JSON.stringify(found.server));
        check('the check says what it verified, never that speech was produced', /No audio was generated|not loaded|never loads/.test(found.verification.message) || found.verification.state !== 'ready', JSON.stringify(found.verification));
        const after = await containerState();
        check('your container’s model state is exactly as before', JSON.stringify(after) === JSON.stringify(before), `${JSON.stringify(before)} -> ${JSON.stringify(after)}`);
    }

    const log = await page.eval(`window.__convoSpeechSmoke`);
    check('the editor never tried to save, patch, write a key or command a runtime', log.blocked.length === 0, log.blocked.join(', ') || [...new Set(log.actions)].join(', '));
    await page.eval(`(() => { Alpine.store('convoSetup').discard(); return 1; })()`);
    await sleep(500);
    const discarded = await page.eval(`({ same: JSON.stringify(Alpine.store('convoSetup').config.roles.tts) === ${JSON.stringify(original)}, dirty: Alpine.store('convoSetup').dirty })`);
    check('discarding brings back exactly the saved speech settings', discarded.same && !discarded.dirty, JSON.stringify(discarded));
    await page.key('Escape'); await sleep(1200);
    check('the clean modal closes without a prompt', await page.eval(`!document.querySelector('.convo-speech')`));
    check('nothing was reported in the console', page.problems().length === 0, page.problems().slice(0, 3).map(p => p.text.slice(0, 160)).join(' | '));
} catch (error) {
    check('the speech check completed', false, error.message);
} finally {
    page?.close();
    await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
