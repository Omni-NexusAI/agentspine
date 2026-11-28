// Live interface smoke check against an ISOLATED test host (default http://127.0.0.1:5028/): the real Agent Zero page, the real
// Convo plugin, a throwaway headless browser. It starts no voice session and changes no setting (it edits a draft, then
// discards it). It exists because a freeze (the orb fighting the host's tooltip observer) and several rendering faults were
// invisible to every simulated test and obvious on a real page.
//
//   node tests/live-ui-smoke.mjs [url]        exit 0 = every check passed
//
// Not part of the default node test run (its name does not end in .test.mjs); run it after deploying to the test host.
import { launch, sleep, contrast, assertLoopback } from './live-browser.mjs';

const url = assertLoopback(process.argv[2] || process.env.CONVO_LIVE_UI_URL || 'http://127.0.0.1:5028/');
const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok: !!ok, detail: String(detail) }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`); };
let browser;
const watchdog = setTimeout(async () => { console.error('FAIL  overall time limit (10 minutes)'); try { await browser?.close(); } catch { /* nothing more to do */ } process.exit(2); }, 10 * 60 * 1000);
watchdog.unref();

browser = await launch({ port: Number(process.env.CONVO_LIVE_DEBUG_PORT || 9333) });
let page;
try {
    page = await browser.page(url);
    let confirms = [], accept = false;
    page.answerDialogs(params => { confirms.push(params.message); return accept; });
    await page.viewport({ width: 1400, height: 900 });
    // a host with hundreds of chats keeps its own sidebar render busy for a while after load
    await page.waitFor("typeof window.Alpine?.store?.('convo') === 'object' && !!document.getElementById('microphone-button')?.getAttribute('data-convo')", 240000, 500);
    check('the page stays responsive and Convo engages the composer orb', await page.eval('1 + 1') === 2);

    const orb = await page.eval(`(() => { const m = document.getElementById('microphone-button'); return { state: m.getAttribute('data-convo'), label: m.getAttribute('aria-label'), pressed: m.getAttribute('aria-pressed'), title: m.getAttribute('title'), tip: m.getAttribute('data-bs-original-title'), bootstrap: !!globalThis.bootstrap?.Tooltip, glyphs: m.querySelectorAll('.co-glyph .g').length }; })()`);
    check('the orb starts in a calm state with a spoken label', ['ready', 'idle'].includes(orb.state) && /^Convo /.test(orb.label || '') && orb.pressed === 'false', JSON.stringify(orb));
    check('the tooltip goes through the host tooltip attribute, never a title the host would take away', orb.bootstrap ? (!orb.title && /^Convo /.test(orb.tip || '')) : /^Convo /.test(orb.title || ''), `bootstrap=${orb.bootstrap}`);
    check('nothing was reported in the console while loading', page.problems().length === 0, page.problems().slice(0, 2).map(p => p.text.slice(0, 120)).join(' | '));

    // every orb state has its own glyph, by CSS alone
    // Checked on detached copies of the button (the CSS keys off its id), so the page's own status poll cannot repaint the real one
    // mid-check. Visible = clearly shown (an animated glyph breathes between 0.7 and 1); hidden = clearly off.
    const glyphs = await page.eval(`(async () => { const base = document.getElementById('microphone-button'); const want = { ready: 'g-mic', idle: 'g-idle', connecting: 'g-spin', listening: 'g-bars', hearing: 'g-bars', thinking: 'g-dots', speaking: 'g-voice', muted: 'g-muted', dictation: 'g-pen', error: 'g-err' }; const box = document.createElement('div'); box.style.cssText = 'position:fixed;left:0;top:0;z-index:99999;display:flex;gap:8px'; const copies = {}; for (const state of Object.keys(want)) { const copy = base.cloneNode(true); copy.setAttribute('data-convo', state); copy.removeAttribute('data-convo-hold'); box.append(copy); copies[state] = copy; } document.body.append(box); await new Promise(r => setTimeout(r, 700)); const seen = {}; for (const [state, glyph] of Object.entries(want)) { const shown = [...copies[state].querySelectorAll('.co-glyph .g')].map(g => [g.getAttribute('class').split(' ').find(c => c.startsWith('g-')), Number(getComputedStyle(g).opacity)]); seen[state] = { ok: shown.every(([name, opacity]) => name === glyph ? opacity > .5 : opacity < .1), shown: shown.filter(([, opacity]) => opacity > .05).map(([name, opacity]) => name + ':' + opacity.toFixed(2)).join(',') }; } box.remove(); return seen; })()`);
    check('each orb state shows exactly its own glyph', Object.values(glyphs).every(g => g.ok), JSON.stringify(Object.entries(glyphs).filter(([, g]) => !g.ok).map(([state, g]) => state + ' -> ' + g.shown)));

    // the Convo surface opens (docked when a chat is selected, otherwise as a modal) with its header, target and controls
    await page.eval(`Alpine.store('rightCanvas').open('convo').then(() => 1)`);
    await page.waitFor(`!!document.querySelector('.convo-panel .convo-primary')`, 60000);
    await sleep(1500);
    const panel = await page.eval(`(() => { const p = document.querySelector('.convo-panel'); const wrap = p.closest('.right-canvas-surface-panel'); const q = s => p.querySelector(s); return { chip: q('.convo-chip')?.textContent.trim(), buttons: [...p.querySelectorAll('.convo-controls button')].map(b => b.textContent.trim()), orb: q('.convo-primary.convo-panel-orb')?.textContent.trim(), place: q('.convo-panel-orb')?.getAttribute('data-place'), layers: q('.convo-panel-orb .cf-core') ? 'full' : 'none', mute: getComputedStyle(q('.convo-mute')).opacity + ':' + q('.convo-icons').inert, tools: p.querySelectorAll('.convo-tools button').length, scroller: q('.convo-transcript')?.getBoundingClientRect().height, docked: !!wrap, active: wrap ? wrap.classList.contains('is-active') : null, opacity: wrap ? getComputedStyle(wrap).opacity : null, pointer: wrap ? getComputedStyle(wrap).pointerEvents : null }; })()`);
    check('the panel renders its status, controls and Sound, settings and Studio buttons', !!panel.chip && panel.buttons.length === 5 && /Start conversation/.test(panel.orb) && ['ready', 'dock'].includes(panel.place) && panel.layers === 'full' && panel.buttons.some(b => /Mute microphone/.test(b)) && panel.buttons.some(b => /Float orb/.test(b)) && panel.buttons.some(b => /Silence voice/.test(b)) && panel.buttons.some(b => /Share camera/.test(b)) && panel.buttons.some(b => /Share screen/.test(b)) && !panel.buttons.some(b => /Dictat/.test(b)) && panel.tools === 3 && panel.scroller > 50, JSON.stringify(panel));
    check('The controls stay tucked behind the orb, out of reach, until a conversation runs (none does here)', panel.mute === '0:true', panel.mute);
    if (panel.docked) check('a docked panel is visible and clickable (the host wrapper contract)', panel.active && panel.opacity === '1' && panel.pointer === 'auto');

    // the settings modal: tabs, no console noise, and a draft that is guarded
    await page.click('.convo-panel button[aria-label="Convo settings"]');
    await page.waitFor(`!!document.querySelector('.convo-tabs')`, 60000);
    await sleep(1500);
    const tabs = await page.eval(`[...document.querySelectorAll('.convo-tabs button')].map(b => b.textContent.trim())`);
    check('the settings modal offers the four sections', JSON.stringify(tabs) === JSON.stringify(['Connections', 'Conversation', 'Audio & Video', 'Advanced']), tabs.join(', '));
    for (const name of tabs) {
        await page.eval(`[...document.querySelectorAll('.convo-tabs button')].find(b => b.textContent.trim() === ${JSON.stringify(name)}).click()`);
        await sleep(500);
        const visible = await page.eval(`[...document.querySelectorAll('.convo-settings > .convo-stack')].filter(s => s.offsetParent !== null).map(s => s.innerText.length)`);
        check(`the ${name} section renders content`, visible.length === 1 && visible[0] > 40, JSON.stringify(visible));
    }
    // The Hotkeys card: one row per action; a recording swallows its keys (Escape ends it and leaves the settings window open).
    const hotkeys = await page.eval(`(async () => { [...document.querySelectorAll('.convo-tabs button')].find(b => b.textContent.trim() === 'Conversation').click(); await new Promise(r => setTimeout(r, 300)); const rows = [...document.querySelectorAll('.convo-hotkey')]; const button = rows.find(r => /Mute or unmute/.test(r.textContent))?.querySelector('[data-hotkey]'); button?.click(); await new Promise(r => setTimeout(r, 200)); return { rows: rows.length, reading: button?.textContent.trim(), pressed: button?.getAttribute('aria-pressed') }; })()`);
    await page.key('Escape'); await sleep(300);
    const recorded = await page.eval(`(() => { const s = Alpine.store('convoSetup'); return { recording: s.ui.hotkeys.recording || '', dirty: s.dirty, open: !!document.querySelector('.convo-tabs'), mute: document.querySelector('[data-hotkey=mute]')?.textContent.trim() }; })()`);
    check('the Hotkeys card lists every action and records one combination at a time', hotkeys.rows === 11 && hotkeys.pressed === 'true' && /Press the keys/.test(hotkeys.reading || '') && !recorded.recording && !recorded.dirty && recorded.open, JSON.stringify({ hotkeys, recorded }));
    // The tunables (speech gate included) are in the developer diagnostics panel; the Advanced tab holds the two modes as switches.
    await page.eval(`[...document.querySelectorAll('.convo-tabs button')].find(b => b.textContent.trim() === 'Advanced').click()`);
    await sleep(500);
    const developer = await page.eval(`(() => { const r = [...document.querySelectorAll('.convo-settings > .convo-stack')].find(s => s.offsetParent !== null); const t = r?.innerText || ''; return { shown: !!r, diagnostics: /Developer mode/.test(t), aiLink: /AI Link development mode/.test(t), switches: r ? r.querySelectorAll('label.toggle input[type=checkbox]').length : 0, old: !!document.querySelector('.convo-settings .convo-developer') }; })()`);
    check('the Advanced tab offers Developer mode and AI Link development mode as switches', developer.shown && developer.diagnostics && developer.aiLink && developer.switches >= 4 && !developer.old, JSON.stringify(developer));
    const gate = await page.eval(`(() => { const row = (Alpine.store('convoSetup').readiness || []).find(i => i.id === 'speech_gate'); return { row: row ? { state: row.state, detail: row.detail.slice(0, 70) } : null }; })()`);
    check('readiness says whether the gate is calibrated or unsupported for the selected classifier', !!gate.row && ['ready', 'unsupported', 'unavailable'].includes(gate.row.state), JSON.stringify(gate.row));
    check('opening and browsing every section raised no console warning or error', page.problems().length === 0, page.problems().slice(0, 2).map(p => p.text.slice(0, 160)).join(' | '));
    const disabled = await page.eval(`(() => { const b = [...document.querySelectorAll('.convo-modal-footer button')].find(x => /Save/.test(x.textContent)); const cs = getComputedStyle(b); return { disabled: b.disabled, opacity: cs.opacity }; })()`);
    check('a disabled Save looks disabled', disabled.disabled && Number(disabled.opacity) < .9, JSON.stringify(disabled));

    confirms = []; accept = false;
    await page.eval(`(async () => { const s = Alpine.store('convoSetup'); s.config.style = 'smoke-test draft'; await new Promise(r => setTimeout(r, 300)); return s.dirty; })()`);
    await page.key('Escape'); await sleep(1500);
    const guarded = await page.eval(`({ modals: document.querySelectorAll('.modal.show').length, dirty: Alpine.store('convoSetup').dirty })`);
    check('closing with an unsaved draft asks first and stays open when declined', confirms.length === 1 && guarded.dirty && guarded.modals >= 1, `${confirms.length} prompt(s)`);
    accept = true;
    await page.key('Escape'); await sleep(2000);
    const closed = await page.eval(`({ dirty: Alpine.store('convoSetup').dirty, style: Alpine.store('convoSetup').config?.style, focus: document.activeElement?.getAttribute('aria-label') || document.activeElement?.tagName })`);
    check('accepting discards the draft, closes only the top modal and returns focus to the gear', !closed.dirty && closed.style !== 'smoke-test draft' && closed.focus === 'Convo settings', JSON.stringify(closed));

    // The orb is the primary control and it is a graphic: its own colour against what is behind it, in both themes (WCAG 3:1)
    for (const theme of ['dark', 'light']) {
        const colours = await page.eval(`(() => { document.body.classList.remove('dark-mode', 'light-mode'); document.body.classList.add('${theme}-mode'); const core = document.querySelector('.convo-panel .convo-primary .cf-core'); let behind = core, bg = 'rgba(0, 0, 0, 0)'; while (behind && /rgba\\(0, 0, 0, 0\\)|transparent/.test(bg)) { behind = behind.parentElement; bg = behind ? getComputedStyle(behind).backgroundColor : 'rgb(255, 255, 255)'; } return { fg: getComputedStyle(core).color, bg }; })()`);
        const ratio = contrast(colours.fg, colours.bg);
        check(`the orb stands out from the panel in ${theme} mode`, ratio >= 3, `${ratio.toFixed(2)}:1 ${colours.fg} on ${colours.bg}`);
    }
    await page.eval(`(() => { document.body.classList.remove('light-mode'); document.body.classList.add('dark-mode'); return 1; })()`);

    // a phone-width settings modal must not scroll sideways
    await page.viewport({ width: 390, height: 844, mobile: true });
    await sleep(800);
    const narrow = await page.eval(`({ page: document.documentElement.scrollWidth, width: innerWidth })`);
    check('the interface does not overflow sideways at phone width', narrow.page <= narrow.width + 1, JSON.stringify(narrow));
} catch (error) {
    check('the smoke check completed', false, error.message);
} finally {
    page?.close();
    await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
