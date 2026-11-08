// Live check of the developer diagnostics surface against an ISOLATED test host (default http://127.0.0.1:5028/): the real Agent
// Zero page, the real Convo plugin, a throwaway headless browser (tests/live-browser.mjs: private profile, muted, loopback only).
// It starts no voice session. It saves ONE setting through the real Settings Save button (Developer diagnostics on), and puts back
// the value it found at the end, also on failure. Its window geometry and section state live in the throwaway profile only.
//
//   node tests/live-diagnostics-smoke.mjs [url]        exit 0 = every check passed
//
// Covered: the rail entry appears with developer mode; every section renders; polling stops when the panel is hidden; pop out, drag
// and resize with real mouse events; a reload restores the window and clamps an off-screen position; a smaller viewport keeps it
// inside; hiding never zeroes the saved position; Escape docks and returns focus; contrast in both themes; reduced motion; no long
// task over budget; developer mode off removes the rail entry and the window; a clean console. Not covered: a filled waterfall and
// gate bars from real turns (run e2e_run.mjs with the stand-in servers or the real models while the panel is open for that).
import { launch, sleep, contrast, assertLoopback } from './live-browser.mjs';

const url = assertLoopback(process.argv[2] || process.env.CONVO_LIVE_UI_URL || 'http://127.0.0.1:5028/');
const SURFACE = 'convo-diagnostics', KEY = 'convo.diagnostics.window', GAP = 8;
const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok: !!ok, detail: String(detail) }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`); };
let browser;
const watchdog = setTimeout(async () => { console.error('FAIL  overall time limit (12 minutes)'); try { await browser?.close(); } catch { /* nothing more to do */ } process.exit(2); }, 12 * 60 * 1000);
watchdog.unref();

browser = await launch({ port: Number(process.env.CONVO_LIVE_DEBUG_PORT || 9333) });
let page, original = null, changed = false;
const metrics = [];                                   // times (ms) at which the page asked for timing marks

// ---- helpers (they read `page` when called)
const rect = selector => page.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e) return null; const r = e.getBoundingClientRect(); return { left: Math.round(r.left), top: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) }; })()`);
const mouseDrag = async (from, to, steps = 8) => {
    await page.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: from.x, y: from.y });
    await page.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: from.x, y: from.y, button: 'left', buttons: 1, clickCount: 1 });
    for (let i = 1; i <= steps; i++) {
        await page.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: from.x + (to.x - from.x) * i / steps, y: from.y + (to.y - from.y) * i / steps, button: 'left', buttons: 1 });
        await sleep(30);
    }
    await page.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: to.x, y: to.y, button: 'left', buttons: 0, clickCount: 1 });
    await sleep(200);
};
const saved = () => page.eval(`(() => { try { return JSON.parse(localStorage.getItem(${JSON.stringify(KEY)}) || 'null'); } catch { return 'unreadable'; } })()`);
const tag = async (expression, name) => page.eval(`(() => { const e = ${expression}; if (!e) return false; e.setAttribute('data-smoke', ${JSON.stringify(name)}); return true; })()`);
const viewport = () => page.eval(`({ width: document.documentElement.clientWidth, height: document.documentElement.clientHeight })`);
const inside = (box, size) => !!box && box.left >= GAP - 1 && box.top >= GAP - 1 && box.left + box.width <= size.width - GAP + 1 && box.top + box.height <= size.height - GAP + 1;
// Developer diagnostics through the real Settings modal: open it from the Convo panel's gear, click the checkbox, click Save.
async function setDeveloper(on) {
    await page.eval(`Alpine.store('rightCanvas').open('convo').then(() => 1)`);
    await page.waitFor(`!!document.querySelector('.convo-panel button[aria-label="Convo settings"]')`, 60000);
    await page.click('.convo-panel button[aria-label="Convo settings"]');
    await page.waitFor(`!!document.querySelector('.convo-modal .convo-developer')`, 60000);
    await sleep(800);
    const found = await tag(`[...document.querySelectorAll('.convo-modal .convo-developer label')].find(l => /Developer diagnostics/.test(l.textContent))?.querySelector('input')`, 'developer');
    if (!found) throw new Error('The Developer diagnostics checkbox was not found');
    const checked = await page.eval(`document.querySelector('[data-smoke="developer"]').checked`);
    if (checked !== on) await page.click('[data-smoke="developer"]');
    await page.waitFor(`Alpine.store('convoSetup').dirty === ${checked !== on}`, 10000);
    if (checked !== on) {
        await page.click('.convo-modal-footer .convo-primary');
        await page.waitFor(`!Alpine.store('convoSetup').busy && !Alpine.store('convoSetup').dirty`, 30000);
        await page.waitFor(`!!Alpine.store('convo').settings && Alpine.store('convo').settings.developer_mode === ${on}`, 30000);
        changed = on !== original;
    }
    await tag(`[...document.querySelectorAll('.convo-modal-footer button')].find(b => b.textContent.trim() === 'Close')`, 'close-settings');
    await page.click('[data-smoke="close-settings"]');
    await sleep(1200);
}

try {
    page = await browser.page(url);
    page.answerDialogs(() => false);
    await page.viewport({ width: 1400, height: 900 });
    await page.send('Network.enable');
    page.on(message => {
        if (message.method !== 'Network.requestWillBeSent') return;
        const body = message.params.request?.postData || '';
        if (message.params.request?.url?.includes('/plugins/_convo/conversation') && /"action":\s*"metrics"/.test(body)) metrics.push(Date.now());
    });
    const ready = "typeof window.Alpine?.store?.('convo') === 'object' && typeof Alpine.store('rightCanvas') === 'object' && typeof Alpine.store('convoDiagnostics') === 'object' && !!Alpine.store('convo').settings";
    await page.waitFor(ready, 240000, 500);
    await page.eval(`(() => { window.__longTasks = []; try { new PerformanceObserver(list => { for (const entry of list.getEntries()) window.__longTasks.push(Math.round(entry.duration)); }).observe({ type: 'longtask', buffered: false }); } catch {} return 1; })()`);
    original = await page.eval(`!!Alpine.store('convo').settings.developer_mode`);
    check('the page loaded Convo and its diagnostics store', true, `developer mode was ${original ? 'on' : 'off'}`);

    // The canvas docks surfaces only with a chat selected: select an existing one (nothing is created).
    const chat = await page.eval(`(async () => { const chats = Alpine.store('chats'); if (!chats.selected && chats.contexts?.length) await chats.selectChat(chats.contexts[0].id); await new Promise(r => setTimeout(r, 800)); return chats.selected || ''; })()`);
    check('a chat is selected, so the canvas can dock surfaces', !!chat, chat ? '' : 'no chat exists on this host; docked checks will fail');

    // ---- developer mode on: the rail entry appears
    await setDeveloper(true);
    await page.waitFor(`Alpine.store('rightCanvas').railSurfaces.some(s => s.id === '${SURFACE}') && !!document.querySelector('nav.right-canvas-rail button[aria-label="Convo diagnostics"]')`, 20000);
    check('Developer diagnostics saved through the real Save button adds a rail entry', true);
    const surface = await page.eval(`(() => { const s = Alpine.store('rightCanvas').getSurface('${SURFACE}'); return { modal: s.modalPath, title: s.title }; })()`);
    check('the surface has no host modal path, so the host offers no window of its own', surface.modal === '' && surface.title === 'Convo diagnostics', JSON.stringify(surface));

    // ---- docked: the wrapper contract and every section
    await page.click('nav.right-canvas-rail button[aria-label="Convo diagnostics"]');
    await page.waitFor(`!!document.querySelector('[data-surface-id="${SURFACE}"].is-active .convo-diag-body')`, 60000);
    await sleep(1500);
    const wrapper = await page.eval(`(() => { const w = document.querySelector('[data-surface-id="${SURFACE}"]'); const cs = getComputedStyle(w); return { opacity: cs.opacity, pointer: cs.pointerEvents, hidden: w.getAttribute('aria-hidden') }; })()`);
    check('the docked panel is visible and clickable (host wrapper contract)', wrapper.opacity === '1' && wrapper.pointer === 'auto' && wrapper.hidden === 'false', JSON.stringify(wrapper));
    const toggles = await page.eval(`[...document.querySelectorAll('[data-surface-id="${SURFACE}"] .convo-diag-toggle')].map(b => b.textContent.trim().replace(/^expand_(more|less)\s*/, ''))`);   // without the arrow icon's ligature text
    check('the panel offers its seven sections', JSON.stringify(toggles) === JSON.stringify(['Live', 'Turns', 'Speech gate', 'Early reply', 'Over recent turns', 'Context', 'Tune']), toggles.join(', '));
    for (const [index, name] of toggles.entries()) {
        await tag(`document.querySelectorAll('[data-surface-id="${SURFACE}"] .convo-diag-toggle')[${index}]`, 'toggle-' + index);
        if (await page.eval(`document.querySelector('[data-smoke="toggle-${index}"]').getAttribute('aria-expanded')`) !== 'true') await page.click(`[data-smoke="toggle-${index}"]`);
        await sleep(name === 'Tune' ? 2500 : 300);
        const content = await page.eval(`(() => { const b = document.querySelector('[data-smoke="toggle-${index}"]'); const c = document.getElementById(b.getAttribute('aria-controls')); return { expanded: b.getAttribute('aria-expanded'), visible: !!c && c.offsetParent !== null, text: c ? c.innerText.trim().length : 0 }; })()`);
        check(`the ${name} section renders`, content.expanded === 'true' && content.visible && content.text > 20, JSON.stringify(content));
    }
    const tune = await page.eval(`(() => { const t = document.querySelector('[data-surface-id="${SURFACE}"] .convo-diag-tune'); const gate = t && [...t.querySelectorAll('details')].find(d => /Speech gate/.test(d.querySelector('summary')?.textContent || '')); return { gate: gate ? gate.querySelectorAll('input[type=number]').length : 0, legacy: !!t?.querySelector('.convo-legacy-classifier'), echo: !!t?.querySelector('select'), save: !!t?.querySelector('.convo-diag-save button') }; })()`);
    // The legacy classifier is now the Classifier card's endpoint-as-entered provider, not a second copy in Tune.
    check('Tune holds the speech gate floors, echo protection and its own Save, and no duplicate classifier', tune.gate === 7 && !tune.legacy && tune.echo && tune.save, JSON.stringify(tune));
    check('opening every section raised no console warning or error', page.problems().length === 0, page.problems().slice(0, 2).map(p => p.text.slice(0, 160)).join(' | '));

    // ---- polling only while shown
    let before = metrics.length;
    await sleep(9000);
    check('while shown, timing marks are read (every 4 s without a voice session)', metrics.length - before >= 2, `${metrics.length - before} reads in 9 s`);
    await page.eval(`Alpine.store('rightCanvas').open('convo').then(() => 1)`);               // another surface in front
    await sleep(1500);
    before = metrics.length;
    await sleep(9000);
    check('hidden behind another surface, nothing is read', metrics.length === before, `${metrics.length - before} reads in 9 s`);
    await page.click('.right-canvas-close-button');
    await sleep(1500);
    before = metrics.length;
    await sleep(6000);
    check('with the canvas closed, nothing is read', metrics.length === before, `${metrics.length - before} reads in 6 s`);
    await page.click('nav.right-canvas-rail button[aria-label="Convo diagnostics"]');
    await page.waitFor(`!!document.querySelector('[data-surface-id="${SURFACE}"].is-active .convo-diag-body')`, 30000);
    await sleep(1500);

    // ---- pop out, drag and resize with real mouse events
    await page.click(`[data-surface-id="${SURFACE}"] [data-diag-popout]`);
    await page.waitFor(`!!document.querySelector('.convo-diag-window .convo-diag-body')`, 30000);
    await sleep(1200);
    const popped = await page.eval(`(() => { const w = document.querySelector('.convo-diag-window'); return { role: w.getAttribute('role'), modal: w.getAttribute('aria-modal'), label: document.getElementById(w.getAttribute('aria-labelledby'))?.textContent, z: getComputedStyle(w).zIndex, canvasOpen: Alpine.store('rightCanvas').isOpen, focusInside: w.contains(document.activeElement) }; })()`);
    check('pop out opens a labelled non-modal window and closes the docked copy', popped.role === 'dialog' && popped.modal === 'false' && popped.label === 'Convo diagnostics' && !popped.canvasOpen, JSON.stringify(popped));
    check('the window sits above the canvas and below host modals, with focus moved into it', Number(popped.z) > 3400 && Number(popped.z) < 5000 && popped.focusInside, JSON.stringify(popped));
    const start = await rect('.convo-diag-window');
    const title = await rect('.convo-diag-window-title');
    await mouseDrag({ x: title.left + 20, y: title.top + title.height / 2 }, { x: title.left + 20 - 220, y: title.top + title.height / 2 - 140 });
    const moved = await rect('.convo-diag-window'), afterMove = await saved();
    check('dragging the bar moves the window', Math.abs(moved.left - (start.left - 220)) <= 2 && Math.abs(moved.top - (start.top - 140)) <= 2, `${JSON.stringify(start)} -> ${JSON.stringify(moved)}`);
    check('the moved position is saved', afterMove && Math.abs(afterMove.left - moved.left) <= 1 && Math.abs(afterMove.top - moved.top) <= 1 && afterMove.width > 0, JSON.stringify(afterMove));
    const corner = await rect('.convo-diag-window-resize');
    await mouseDrag({ x: corner.left + corner.width / 2, y: corner.top + corner.height / 2 }, { x: corner.left + corner.width / 2 + 90, y: corner.top + corner.height / 2 + 70 });
    const grown = await rect('.convo-diag-window'), afterResize = await saved();
    check('the corner resizes the window', Math.abs(grown.width - (moved.width + 90)) <= 2 && Math.abs(grown.height - (moved.height + 70)) <= 2, `${JSON.stringify(moved)} -> ${JSON.stringify(grown)}`);
    check('the new size is saved', afterResize && Math.abs(afterResize.width - grown.width) <= 1 && Math.abs(afterResize.height - grown.height) <= 1, JSON.stringify(afterResize));

    // ---- a smaller viewport keeps it inside; growing back restores it; a narrow one makes a sheet; none of it touches what is saved
    await page.viewport({ width: 900, height: 640 });
    await sleep(600);
    const small = await rect('.convo-diag-window');
    check('a smaller viewport keeps the window inside', inside(small, await viewport()), JSON.stringify(small));
    await page.viewport({ width: 520, height: 800, mobile: true });
    await sleep(600);
    const sheet = await page.eval(`(() => { const w = document.querySelector('.convo-diag-window'); const r = w.getBoundingClientRect(); return { sheet: w.classList.contains('is-sheet'), left: Math.round(r.left), bottom: Math.round(r.bottom), width: Math.round(r.width), vw: document.documentElement.clientWidth, vh: document.documentElement.clientHeight }; })()`);
    check('a narrow screen shows it as a bottom sheet', sheet.sheet && sheet.left === 0 && Math.abs(sheet.bottom - sheet.vh) <= 1 && sheet.width === sheet.vw, JSON.stringify(sheet));
    await page.viewport({ width: 1400, height: 900 });
    await sleep(600);
    const back = await rect('.convo-diag-window');
    check('back at full size, the window returns to where it was put', Math.abs(back.left - grown.left) <= 1 && Math.abs(back.top - grown.top) <= 1 && back.width === grown.width, JSON.stringify(back));
    check('changing the viewport and the sheet saved nothing new', JSON.stringify(await saved()) === JSON.stringify(afterResize), JSON.stringify(await saved()));

    // ---- reload: restored, and an off-screen position is clamped
    await page.send('Page.reload', { ignoreCache: false });
    await page.waitFor(ready, 240000, 500);
    await page.waitFor(`!!document.querySelector('.convo-diag-window')`, 30000);
    await sleep(1500);
    const restored = await rect('.convo-diag-window');
    check('after a reload the window comes back where it was', Math.abs(restored.left - afterResize.left) <= 1 && Math.abs(restored.top - afterResize.top) <= 1 && restored.width === afterResize.width, JSON.stringify(restored));
    await page.eval(`(() => { localStorage.setItem(${JSON.stringify(KEY)}, JSON.stringify({ left: 5000, top: 4000, width: 520, height: 430 })); return 1; })()`);
    await page.send('Page.reload', { ignoreCache: false });
    await page.waitFor(ready, 240000, 500);
    await page.waitFor(`!!document.querySelector('.convo-diag-window')`, 30000);
    await sleep(1500);
    const clamped = await rect('.convo-diag-window');
    check('an off-screen saved position is clamped into view on restore', inside(clamped, await viewport()) && clamped.width === 520, JSON.stringify(clamped));
    check('restoring did not rewrite the saved position', JSON.stringify(await saved()) === JSON.stringify({ left: 5000, top: 4000, width: 520, height: 430 }), JSON.stringify(await saved()));
    await page.eval(`(() => { localStorage.setItem(${JSON.stringify(KEY)}, ${JSON.stringify(JSON.stringify(afterResize))}); return 1; })()`);

    // ---- Escape docks and returns focus; hiding never zeroes the saved position
    await page.eval(`(() => { document.querySelector('.convo-diag-window [data-drag-handle]').focus(); return 1; })()`);
    await page.key('Escape');
    await sleep(1500);
    const docked = await page.eval(`(() => ({ window: !!document.querySelector('.convo-diag-window'), open: Alpine.store('rightCanvas').isOpen, active: Alpine.store('rightCanvas').activeSurfaceId, focus: document.activeElement?.hasAttribute('data-diag-popout') || false, modals: document.querySelectorAll('.modal.show').length }))()`);
    check('Escape docks the window back into the canvas', !docked.window && docked.open && docked.active === SURFACE, JSON.stringify(docked));
    check('Escape returns focus to the Pop out button', docked.focus, JSON.stringify(docked));
    const keptPosition = await saved();
    check('hiding the window kept the saved position (never zeroed)', keptPosition && keptPosition.width > 0 && keptPosition.height > 0 && JSON.stringify(keptPosition) === JSON.stringify(afterResize), JSON.stringify(keptPosition));

    // ---- contrast in both themes (text >= 4.5:1, bars >= 3:1)
    // Colours are painted onto a 1x1 canvas so color-mix(), color(srgb ...) and alpha all come back as the rgb() actually shown.
    const readings = await page.eval(`(() => {
        const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        const paint = (...colors) => { ctx.clearRect(0, 0, 1, 1); for (const color of ['#ffffff', ...colors]) { ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1); } const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data; return 'rgb(' + r + ', ' + g + ', ' + b + ')'; };
        const transparent = color => !color || color === 'transparent' || /^rgba\\(.*,\\s*0\\)$/.test(color);
        const background = element => { const layers = []; for (let e = element; e; e = e.parentElement) { const c = getComputedStyle(e).backgroundColor; if (!transparent(c)) layers.unshift(c); } return paint(...layers); };
        const out = {};
        for (const theme of ['dark', 'light']) {
            document.body.classList.remove('dark-mode', 'light-mode'); document.body.classList.add(theme + '-mode');
            const root = document.querySelector('[data-surface-id="${SURFACE}"] .convo-diag');
            const texts = [...root.querySelectorAll('.convo-diag-note, .convo-diag-k, .convo-diag-v, .convo-diag-status, .convo-diag-toggle, [data-severity]')].filter(e => e.offsetParent !== null && e.textContent.trim());
            const bars = [...root.querySelectorAll('.convo-diag-bar > i, .convo-diag-track > i')].filter(e => e.offsetParent !== null);
            out[theme] = { texts: texts.map(e => { const bg = background(e); return [paint(bg, getComputedStyle(e).color), bg, e.className || e.tagName]; }),
                           bars: bars.map(e => { const bg = background(e.parentElement); return [paint(bg, getComputedStyle(e).backgroundColor), bg]; }) };
        }
        document.body.classList.remove('light-mode'); document.body.classList.add('dark-mode');
        return out; })()`);
    for (const theme of ['dark', 'light']) {
        const texts = readings[theme].texts.map(([fg, bg, name]) => [contrast(fg, bg), name]);
        const worst = texts.reduce((a, b) => (b[0] < a[0] ? b : a), [99, '']);
        check(`every visible text reads at 4.5:1 or more in ${theme} mode`, texts.length > 5 && worst[0] >= 4.5, `${texts.length} texts, lowest ${worst[0].toFixed(2)}:1 (${worst[1]})`);
        const bars = readings[theme].bars.map(([fg, bg]) => contrast(fg, bg));
        check(`bars stand out from their tracks at 3:1 or more in ${theme} mode`, bars.every(value => value >= 3), bars.length ? `lowest ${Math.min(...bars).toFixed(2)}:1` : 'no bars visible');
    }

    // ---- reduced motion: no transitions, meters once a second
    const cadence = async () => page.eval(`(async () => {
        const meter = document.querySelector('[data-surface-id="${SURFACE}"] [data-diag-level="input"]'); let writes = 0;
        const observer = new MutationObserver(() => { writes++; }); observer.observe(meter, { attributes: true, attributeFilter: ['style'] });
        const store = Alpine.store('convo'), before = store.inputLevel;
        for (let i = 0; i < 30; i++) { store.inputLevel = (i % 10) / 10; await new Promise(r => setTimeout(r, 100)); }
        store.inputLevel = before; observer.disconnect();
        return { writes, transition: getComputedStyle(meter.querySelector('.convo-diag-bar > i')).transitionDuration }; })()`);
    const normal = await cadence();
    await page.viewport({ width: 1400, height: 900, reduced: true });
    await page.eval(`Alpine.store('rightCanvas').open('convo').then(() => 1)`); await sleep(500);   // restart the meters under the new preference
    await page.eval(`Alpine.store('rightCanvas').open('${SURFACE}').then(() => 1)`); await sleep(1500);
    const reduced = await cadence();
    check('meters update about four times a second', normal.writes >= 9 && normal.writes <= 14, JSON.stringify(normal));
    check('with reduced motion: no transitions and meters about once a second', reduced.transition.split(',').every(value => parseFloat(value) === 0) && reduced.writes <= 4, JSON.stringify(reduced));
    await page.viewport({ width: 1400, height: 900, reduced: false });
    const longest = await page.eval(`Math.max(0, ...(window.__longTasks || []))`);
    check('no long task over 250 ms while the panel was in use', longest <= 250, `${longest} ms`);

    // ---- developer mode off: the rail entry and the window go
    await page.click(`[data-surface-id="${SURFACE}"] [data-diag-popout]`);
    await page.waitFor(`!!document.querySelector('.convo-diag-window')`, 30000);
    await setDeveloper(false);
    await sleep(3000);
    const gone = await page.eval(`({ rail: Alpine.store('rightCanvas').railSurfaces.some(s => s.id === '${SURFACE}'), button: !!document.querySelector('nav.right-canvas-rail button[aria-label="Convo diagnostics"]'), window: !!document.querySelector('.convo-diag-window'), active: Alpine.store('rightCanvas').activeSurfaceId })`);
    check('developer mode off removes the rail entry and the window', !gone.rail && !gone.button && !gone.window && gone.active !== SURFACE, JSON.stringify(gone));
    const refused = await page.eval(`Alpine.store('rightCanvas').open('${SURFACE}')`);
    check('and the surface cannot be opened behind its back', refused === false, String(refused));
    check('nothing was reported in the console', page.problems().length === 0, page.problems().slice(0, 3).map(p => p.text.slice(0, 160)).join(' | '));
} catch (error) {
    check('the diagnostics smoke check completed', false, error.message);
} finally {
    // Put back the developer mode this run found, through the same Save (or the store's own revision-aware patch if the page broke).
    if (page && original !== null) {
        try {
            if (await page.eval(`!!Alpine.store('convo').settings?.developer_mode`) !== original) {
                try { await page.key('Escape'); await setDeveloper(original); }
                catch { await page.eval(`(async () => { const s = Alpine.store('convoSetup'); await s.load(); await s.patch({ developer_mode: ${original} }); await Alpine.store('convo').refresh(); return 1; })()`); }
            }
            check('developer mode is back to what it was', await page.eval(`!!Alpine.store('convo').settings?.developer_mode`) === original, `restored to ${original ? 'on' : 'off'}${changed ? '' : ' (unchanged)'}`);
        } catch (error) { check('developer mode is back to what it was', false, error.message); }
    }
    page?.close();
    await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
