// Live check of "Draft in composer (continuous)" against an ISOLATED loopback test host (default http://127.0.0.1:5028/): the real
// Agent Zero page and Convo plugin in a throwaway headless browser (tests/live-browser.mjs: private profile, muted, loopback only,
// refuses a DevTools port it did not start) whose microphone is Chrome's fake capture playing a scripted recording of three phrases.
//
//   * No Whisper model is loaded or downloaded: the page's transcription requests (*/plugins/_whisper_stt/transcribe) are answered
//     here through the DevTools Fetch domain with "one", "two", "three".
//   * API-key writes (*api_keys*, Convo's conversation_key_copy, a setup_save carrying keys) are answered here and counted, so the test
//     host's key store is untouched. A message send (*/message_async) is answered here too, so nothing reaches a model.
//   * It changes ONE setting, Agent Zero's voice-message handling (through the Whisper modal and the Convo card, as a user would), and
//     restores the original at the end, also after a failure.
//
//   node tests/live-dictation-smoke.mjs [url] [chat-id]      exit 0 = every check passed
//
// Recording: CONVO_LIVE_DICTATION_WAV (default: the live-test rig's scenarios/dictation_three_phrases.wav and its .json timeline).
// Not part of the default node test run (its name does not end in .test.mjs); run it after deploying to the test host. It proves the
// page-level path only (gesture, recorder, delivery, settings round trip); it says nothing about transcription quality or real voices.
import { existsSync, readFileSync } from 'node:fs';
import { assertLoopback, launch, sleep } from './live-browser.mjs';

const url = assertLoopback(process.argv[2] || process.env.CONVO_LIVE_UI_URL || 'http://127.0.0.1:5028/');
const wantedChat = process.argv[3] || process.env.CONVO_LIVE_CHAT || '';
const wav = process.env.CONVO_LIVE_DICTATION_WAV || 'C:\\speech-to-speech\\.runtime\\convo-live-test\\scenarios\\dictation_three_phrases.wav';
const WORDS = ['one', 'two', 'three'];
const WHISPER_SELECT = 'x-component[path="/plugins/_whisper_stt/webui/config.html"] select[x-model="config.message_mode"]';
const LABEL = 'Dictating into the composer — keeps listening until you send or stop.';
const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok: !!ok }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`); };

if (!existsSync(wav)) { console.error(`FAIL  the scripted recording is missing: ${wav}`); process.exit(2); }
const timeline = (() => { try { return JSON.parse(readFileSync(wav.replace(/\.wav$/i, '.json'), 'utf8')); } catch { return null; } })();

let browser, page, original = null, chat = '';
const watchdog = setTimeout(async () => { console.error('FAIL  overall time limit (10 minutes)'); try { await browser?.close(); } catch { /* nothing more to do */ } process.exit(2); }, 10 * 60 * 1000);
watchdog.unref();

browser = await launch({ port: Number(process.env.CONVO_LIVE_DEBUG_PORT || 9333), extraArgs: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
    `--use-file-for-fake-audio-capture=${wav}%noloop`, '--autoplay-policy=no-user-gesture-required'] });
const api = (path, data = {}) => page.eval(`(async () => (await import('/js/api.js')).callJsonApi(${JSON.stringify(path)}, ${JSON.stringify(data)}))()`);
const effective = status => status?.voice_message_draft === 'continuous' && status?.config?.message_mode === 'draft' ? 'continuous' : status?.config?.message_mode;
const STATE = `(() => { const s = Alpine.store('convo'), w = Alpine.store('whisperStt'), m = document.getElementById('microphone-button');
  return { orb: m.getAttribute('data-convo'), label: m.getAttribute('aria-label'), dictating: s.dictating, mode: s.dictationMode, active: s.active,
    note: s.note || '', error: s.error || '', rec: w?.microphoneInput?.status ?? null, phrases: w?.microphoneInput?.phrasesAdded ?? null,
    composer: Alpine.store('chatInput').message || '', hostMode: w?.config?.message_mode ?? null, flag: s.settings?.voice_message_draft ?? null }; })()`;
const counts = { transcribe: 0, keys: 0, sends: 0 }, sent = [];

// A real control, clicked with real mouse events: tag the element the expression finds, then click it through the DevTools protocol.
async function clickFound(expression, tag) {
    const found = await page.eval(`(() => { const e = (${expression}); if (!e) return false; e.setAttribute('data-live-${tag}', ''); return true; })()`);
    if (!found) throw new Error('Not found: ' + tag);
    await page.click(`[data-live-${tag}]`);
}
// Choose an option of a real <select> through its own change event, which is what Alpine's binding listens to (a headless platform
// select pops up its list on click, which the DevTools protocol cannot drive reliably).
const choose = (expression, value) => page.eval(`(() => { const s = (${expression}); s.focus(); s.value = ${JSON.stringify(value)}; s.dispatchEvent(new Event('input', { bubbles: true })); s.dispatchEvent(new Event('change', { bubbles: true })); return s.value; })()`);
// The host's openModal() resolves only when the modal closes: open without waiting on it, then wait for the markup.
const whisperModal = async () => {
    await page.eval(`(() => { void Alpine.store('whisperStt').openConfig(); return 1; })()`);
    await page.waitFor(`!!document.querySelector(${JSON.stringify(WHISPER_SELECT)})?.querySelector('option[value="continuous"]') && !!document.querySelector('.enhanced-speech-whisper-device')`, 30000, 200);
    await sleep(600);
    return page.eval(`(() => { const s = document.querySelector(${JSON.stringify(WHISPER_SELECT)}); const modal = s.closest('.modal');
        return { options: [...s.options].map(o => o.value + ':' + o.textContent.trim()), value: s.value, edited: modal.__pluginSettingsContext?.hasUnsavedChanges ?? null,
                 hints: modal.querySelectorAll('[data-convo-continuous-hint]').length }; })()`);
};
const whisperGone = () => page.waitFor(`!document.querySelector(${JSON.stringify(WHISPER_SELECT)})`, 30000, 200);
const CARD = `[...document.querySelectorAll('.convo-settings label')].find(l => l.textContent.trim().startsWith('Voice message handling'))?.querySelector('select')`;

try {
    page = await browser.page(url);
    page.answerDialogs(() => false);
    page.on(message => {
        if (message.method !== 'Fetch.requestPaused') return;
        const { requestId, request } = message.params;
        const reply = body => page.send('Fetch.fulfillRequest', { requestId, responseCode: 200, responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
            body: Buffer.from(JSON.stringify(body)).toString('base64') }).catch(() => {});
        const body = request.postData || '';
        if (/\/plugins\/_whisper_stt\/transcribe/.test(request.url)) { reply({ text: WORDS[counts.transcribe] ?? `extra ${counts.transcribe + 1}` }); counts.transcribe++; return; }
        // Agent Zero's own key-status read (api_keys "get": has_key per provider) passes; writing or revealing a key is stubbed.
        if ((/api_keys/.test(request.url) && !/"action"\s*:\s*"get"/.test(body)) || /conversation_key_copy/.test(body) || (/"setup_save"/.test(body) && /"keys":\{"/.test(body))) { counts.keys++; reply({ ok: false, error: 'Stubbed by live-dictation-smoke: key writes never reach the test host.' }); return; }
        if (/\/message_async/.test(request.url)) { counts.sends++; sent.push(body); reply({ ok: true, context: chat }); return; }
        page.send('Fetch.continueRequest', { requestId }).catch(() => {});
    });
    await page.send('Fetch.enable', { patterns: [{ urlPattern: '*/plugins/_whisper_stt/transcribe*', requestStage: 'Request' }, { urlPattern: '*api_keys*', requestStage: 'Request' },
        { urlPattern: '*/plugins/_convo/conversation*', requestStage: 'Request' }, { urlPattern: '*/message_async*', requestStage: 'Request' }] });
    await page.viewport({ width: 1400, height: 900 });
    await page.waitFor("typeof window.Alpine?.store?.('convo') === 'object' && !!document.getElementById('microphone-button')?.getAttribute('data-convo')", 240000, 500);
    // An existing chat to dictate into: a send with no chat selected would create one on the host.
    chat = await page.eval(`(async () => { const chats = Alpine.store('chats'); const id = ${JSON.stringify(wantedChat)} || chats.selected || chats.contexts.find(c => !c.parent_context_id)?.id || '';
        if (id && chats.selected !== id) await chats.selectChat(id); return Alpine.store('chats').selected || ''; })()`);
    check('an existing chat is selected to dictate into', !!chat, chat);
    if (!chat) throw new Error('No chat to dictate into; pass one as the second argument.');
    await sleep(2500);

    // 0. What is stored now (restored at the end), and whether the recording's pauses are longer than the host's end of a phrase.
    const before = await api('/plugins/_convo/stt_status');
    original = effective(before);
    check('Convo\'s Whisper bridge reports the drafting flag beside the host mode', ['single', 'continuous'].includes(before.voice_message_draft) && ['send', 'draft'].includes(before.config?.message_mode), JSON.stringify({ host: before.config?.message_mode, flag: before.voice_message_draft }));
    const phraseEnd = Number(before.config?.silence_duration || 1000) + Number(before.config?.waiting_timeout || 2000);
    check('each pause in the recording is longer than the host takes to end a phrase', !!timeline && timeline.gaps_seconds.every(gap => gap * 1000 > phraseEnd + 1000), JSON.stringify({ gaps: timeline?.gaps_seconds, phraseEndMs: phraseEnd }));

    // 1. Agent Zero's Whisper modal: the third option, chosen on the real select and saved with the modal's own Save button.
    const shown = await whisperModal();
    check('the Whisper modal offers "Draft in composer (continuous)" once, right after "Draft in composer", with one hint', JSON.stringify(shown.options) === JSON.stringify(['send:Send immediately', 'draft:Draft in composer', 'continuous:Draft in composer (continuous)']) && shown.hints === 1, JSON.stringify(shown));
    check('it shows the stored choice without making the modal look edited', shown.value === original && shown.edited === false, JSON.stringify({ shown: shown.value, original, edited: shown.edited }));
    await choose(`document.querySelector(${JSON.stringify(WHISPER_SELECT)})`, 'continuous');
    check('choosing it marks the modal edited', await page.eval(`document.querySelector(${JSON.stringify(WHISPER_SELECT)}).closest('.modal').__pluginSettingsContext?.hasUnsavedChanges`) === (original !== 'continuous'));
    await clickFound(`document.querySelector(${JSON.stringify(WHISPER_SELECT)}).closest('.modal').querySelector('button.btn-ok')`, 'whisper-save');
    await whisperGone();
    const saved = await api('/plugins/_convo/stt_status');
    check('Save stores Agent Zero\'s draft plus Convo\'s continuous flag (never "continuous" as the host value)', saved.config?.message_mode === 'draft' && saved.voice_message_draft === 'continuous', JSON.stringify({ host: saved.config?.message_mode, flag: saved.voice_message_draft }));
    const reopened = await whisperModal();
    check('reopened, it shows "Draft in composer (continuous)" and does not look edited', reopened.value === 'continuous' && reopened.edited === false, JSON.stringify(reopened));
    await clickFound(`document.querySelector(${JSON.stringify(WHISPER_SELECT)}).closest('.modal').querySelector('button.btn-cancel')`, 'whisper-cancel');
    await whisperGone();

    // 2. The Convo card agrees, and its own choice applies at once without a "changed elsewhere" banner.
    await page.eval(`(() => { void Alpine.store('convo').openSettingsModal(); return 1; })()`);
    await page.waitFor(`!!document.querySelector('.convo-tabs')`, 60000);
    await page.eval(`[...document.querySelectorAll('.convo-tabs button')].find(b => b.textContent.trim() === 'Voice & Audio').click()`);
    await page.waitFor(`!!(${CARD})`, 20000);
    await sleep(800);
    check('the Convo card shows the same choice', await page.eval(`(${CARD}).value`) === 'continuous');
    for (const mode of ['draft', 'continuous']) {
        await choose(CARD, mode);
        await page.waitFor(`!Alpine.store('convoSetup').ui.dictation?.applying && !Alpine.store('convoSetup').busy`, 20000, 150);
        await sleep(600);
        const status = await api('/plugins/_convo/stt_status');
        const card = await page.eval(`({ value: (${CARD}).value, stale: Alpine.store('convoSetup').stale, dirty: Alpine.store('convoSetup').dirty, error: Alpine.store('convoSetup').error })`);
        check(`choosing "${mode}" in the Convo card applies at once and the Whisper settings agree`, effective(status) === mode && card.value === mode && !card.stale && !card.dirty && !card.error, JSON.stringify({ stored: effective(status), card }));
    }
    await page.key('Escape'); await sleep(1500);

    // 3. Dictation: hold the orb 2.4 s; three phrases are added to the composer, nothing is sent, and it keeps listening.
    await page.eval(`(() => { Alpine.store('chatInput').reset(); return 1; })()`);
    const point = await page.eval(`(() => { const r = document.getElementById('microphone-button').getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()`);
    const mouse = type => page.send('Input.dispatchMouseEvent', { type, x: point.x, y: point.y, button: 'left', clickCount: 1 });
    await mouse('mouseMoved'); await mouse('mousePressed');
    await sleep(2400);
    await mouse('mouseReleased');
    await sleep(400);
    const latched = await page.eval(STATE);
    check('after the hold the orb says it is dictating into the composer', latched.dictating && latched.mode === 'latched' && latched.label === LABEL, JSON.stringify({ orb: latched.orb, label: latched.label, hostMode: latched.hostMode, flag: latched.flag }));
    const end = Date.now() + 45000;
    while (counts.transcribe < 3 && Date.now() < end) await sleep(250);
    await sleep(1500);
    const drafted = await page.eval(STATE);
    check('three transcriptions were requested (and answered by this script, no model)', counts.transcribe === 3, String(counts.transcribe));
    check('the composer reads "one two three"', drafted.composer.trim() === 'one two three', JSON.stringify(drafted.composer));
    check('nothing was sent', counts.sends === 0, String(counts.sends));
    check('it is still listening', drafted.dictating && ['listening', 'recording', 'waiting'].includes(String(drafted.rec)) && drafted.phrases === 3, JSON.stringify({ rec: drafted.rec, phrases: drafted.phrases, label: drafted.label }));
    check('no Convo voice session was started', !drafted.active);

    // 4. Send stops it. (A running chat would turn the button into "stop the agent": never press it then.)
    if (await page.eval(`!!Alpine.store('chats').contexts.find(c => c.id === ${JSON.stringify(chat)})?.running || Alpine.store('chatInput')._getSendState?.() === 'stop'`))
        throw new Error('The selected chat is running; choose an idle chat so Send does not stop its agent.');
    await page.click('#send-button');
    await sleep(3000);
    const after = await page.eval(STATE);
    check('Send stops dictation and releases the recorder', !after.dictating && (after.rec === null || after.rec === 'inactive'), JSON.stringify({ dictating: after.dictating, rec: after.rec }));
    check('the send emptied the composer', after.composer.trim() === '', JSON.stringify(after.composer));
    check('the dictated text went out once, answered by this script (or held by the model gate)', counts.sends === 0 || (counts.sends === 1 && sent[0].includes('one two three')), JSON.stringify({ sends: counts.sends }));
    check('the end note says what happened', after.note === 'Dictation finished when you sent your message · 3 phrases added.', JSON.stringify(after.note));
    check('no API-key write was attempted', counts.keys === 0, String(counts.keys));
    const problems = page.logs.filter(entry => ['error', 'exception', 'assert'].includes(entry.type));
    const warnings = page.logs.filter(entry => entry.type === 'warning');
    check('no console errors or exceptions', problems.length === 0, problems.slice(0, 3).map(p => p.text.slice(0, 160)).join(' | ') + (warnings.length ? `  (${warnings.length} warning(s): ${warnings.slice(0, 2).map(w => w.text.slice(0, 100)).join(' | ')})` : ''));
} catch (error) {
    check('the dictation check completed', false, error.message);
} finally {
    if (original && page) {
        try {
            await api('/plugins/_convo/stt_config', { message_mode: original });
            const restored = effective(await api('/plugins/_convo/stt_status'));
            check('the original voice-message handling was restored', restored === original, JSON.stringify({ original, restored }));
        } catch (error) { check('the original voice-message handling was restored', false, error.message); }
    }
    page?.close();
    await browser.close();
}
const failed = results.filter(result => !result.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
