// Live fallback checks against an ISOLATED test host (default http://127.0.0.1:5028/). Starts no voice session.
//
//   node tests/live-fallback-smoke.mjs dev [url]   Convo ON with AI Link development mode ON: the orb is idle, a click starts nothing and
//                                                  explains why, and the page opens no voice session.
//   node tests/live-fallback-smoke.mjs off [url]   Convo OFF: the host's own microphone button is untouched and nothing of Convo is on the page.
//
// The caller sets the state first (toggle the plugin / save the setting); this script only observes. Exit 0 = every check passed.
import { launch, sleep, assertLoopback } from './live-browser.mjs';

const phase = process.argv[2];
if (!['dev', 'off'].includes(phase)) { console.error('usage: node tests/live-fallback-smoke.mjs dev|off [url]'); process.exit(64); }
const url = assertLoopback(process.argv[3] || process.env.CONVO_LIVE_UI_URL || 'http://127.0.0.1:5028/');
const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok: !!ok, detail: String(detail) }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`); };
let browser;
const watchdog = setTimeout(async () => { console.error('FAIL  overall time limit (6 minutes)'); try { await browser?.close(); } catch { /* nothing more to do */ } process.exit(2); }, 6 * 60 * 1000);
watchdog.unref();

browser = await launch({ port: Number(process.env.CONVO_LIVE_DEBUG_PORT || 9333) });
let page;
try {
    page = await browser.page(url);
    page.answerDialogs(() => false);
    await page.viewport({ width: 1400, height: 900 });
    if (phase === 'dev') {
        await page.waitFor("typeof window.Alpine?.store?.('convo') === 'object' && !!document.getElementById('microphone-button')?.getAttribute('data-convo')", 240000, 500);
        const before = await page.eval(`(() => { const s = Alpine.store('convo'); return { orb: document.getElementById('microphone-button').getAttribute('data-convo'), idle: !!s.settings?.ai_link_development_mode, active: s.active }; })()`);
        check('the host loaded the Convo orb with AI Link development mode on', before.idle && before.orb === 'idle' && !before.active, JSON.stringify(before));
        const after = await page.eval(`(async () => { const s = Alpine.store('convo'); await s.toggle(); await new Promise(r => setTimeout(r, 1500)); return { note: s.note, active: s.active, busy: s.busy, state: s.state, error: s.error, orb: document.getElementById('microphone-button').getAttribute('data-convo') }; })()`);
        check('a click explains the mode and starts nothing', /AI Link development mode/.test(after.note) && !after.active && !after.busy && after.orb === 'idle' && !after.error, JSON.stringify(after));
    } else {
        await page.waitFor("!!document.getElementById('microphone-button')", 240000, 500);
        await sleep(6000);
        const view = await page.eval(`(() => { const m = document.getElementById('microphone-button'); return { convoAttr: m.hasAttribute('data-convo'), orbs: m.querySelectorAll('.convo-orb').length, store: typeof window.Alpine?.store?.('convo'), surface: !!document.querySelector('[data-surface-id="convo"]'), hostChildren: [...m.children].filter(c => !c.classList.contains('convo-orb') && getComputedStyle(c).visibility === 'hidden').length }; })()`);
        check('the host microphone button carries nothing of Convo', !view.convoAttr && view.orbs === 0 && view.hostChildren === 0, JSON.stringify(view));
        check('no Convo store or canvas panel is on the page', view.store === 'undefined' && !view.surface, JSON.stringify(view));
    }
    check('nothing was reported in the console', page.problems().length === 0, page.problems().slice(0, 2).map(p => p.text.slice(0, 140)).join(' | '));
} catch (error) {
    check('the fallback check completed', false, error.message);
} finally {
    page?.close();
    await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
