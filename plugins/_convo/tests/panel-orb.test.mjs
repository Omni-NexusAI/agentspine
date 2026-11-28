// The panel's orb: one element that rests on the ready page and moves beside the controls when a conversation starts; the
// controls stay tucked away until then; the orb can be let out of the panel to float over the window.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { storeFixture, fakeMic } from './store-fixture.mjs';

const box = (left, top, width, height) => ({ getBoundingClientRect: () => ({ left, top, width, height, right: left + width, bottom: top + height }) });

function panelOrb({ slot = box(140, 200, 120, 120), dock = box(12, 60, 46, 46) } = {}) {
    const parts = { '.convo-orb-slot': slot, '.convo-orb-dock': dock };
    const panel = { ...box(0, 0, 400, 600), querySelector: name => parts[name] || null };
    const orb = fakeMic({});
    orb.classList = { contains: name => name === 'convo-panel-orb' };
    orb.closest = name => name === '.convo-panel' ? panel : null;
    return { orb, parts };
}
const placed = orb => [orb.getAttribute('data-place'), orb.style.left, orb.style.top, orb.style.width, orb.hasAttribute('data-still')];

test('The orb rests on the ready page, travels to its dock when a conversation starts, and comes back when it ends', async () => {
    const f = await storeFixture();
    await f.store.refresh();
    const { orb } = panelOrb();
    f.store.bindPanelOrb(orb);
    assert.equal(f.store.resting, true);
    assert.deepEqual(placed(orb), ['ready', '140px', '200px', '120px', true]);       // put there at once: nothing to animate from
    assert.match(orb.children[0].innerHTML, /cf-core/);                              // the full orb, in the same state as the composer's
    assert.equal(orb.getAttribute('data-convo'), 'ready');
    await f.store.start();
    f.store.queuePlaceOrb();
    assert.equal(f.store.resting, false);
    assert.deepEqual(placed(orb), ['dock', '12px', '60px', '46px', false]);          // a change of place is animated (no data-still)
    f.store.queuePlaceOrb();
    assert.equal(orb.getAttribute('data-place'), 'dock');
    await f.store.stop();
    f.store.queuePlaceOrb();
    assert.deepEqual(placed(orb).slice(0, 4), ['ready', '140px', '200px', '120px']);
    f.store.bindPanelOrb(null);
});

test('With something already said in the chat there is no ready page: the orb waits in its dock and one line says how to start', async () => {
    const f = await storeFixture();
    await f.store.refresh();
    const { orb } = panelOrb({ slot: box(0, 0, 0, 0) });                             // the empty state is not shown
    f.store.bindPanelOrb(orb);
    assert.deepEqual(placed(orb).slice(0, 4), ['dock', '12px', '60px', '46px']);
    assert.equal(f.store.hintText, '');                                              // the ready page itself says how to start
    Object.defineProperty(f.store, 'visibleEvents', { get: () => [{ seq: 1 }], configurable: true });
    assert.equal(f.store.resting, false);
    assert.equal(f.store.hintText, 'Click the orb to start a conversation');
    f.store.settings = { ...f.store.settings, ai_link_development_mode: true };
    assert.equal(f.store.hintText, 'Voice idle · development mode');
    f.store.bindPanelOrb(null);
});

test('During a conversation the line stays empty unless there is something to say: the mute button and the chip carry the microphone', async () => {
    const f = await storeFixture();
    await f.store.start();
    assert.equal(f.store.hintText, '');
    await f.store.setMuted(true);
    assert.equal(f.store.hintText, '');
    f.store.settings = { ...f.store.settings, remote_processing: true };
    assert.equal(f.store.hintText, 'Remote processing configured');
    f.store.note = 'Whisper could not write down that turn';
    assert.equal(f.store.hintText, 'Whisper could not write down that turn');
    await f.store.stop();
});

test('The orb can float: only during a conversation, kept for this browser, and one button puts it back', async () => {
    const kept = new Map(), body = [];
    const element = () => {
        const node = fakeMic({});
        node.listeners = {}; node.addEventListener = (name, fn) => { node.listeners[name] = fn; };
        node.remove = () => { body.splice(body.indexOf(node), 1); };
        node.classList = { contains: name => String(node.className || '').split(' ').includes(name) };
        return node;
    };
    const f = await storeFixture({ globals: { innerWidth: 1000, innerHeight: 800,
        localStorage: { getItem: key => kept.get(key) ?? null, setItem: (key, value) => kept.set(key, value) },
        document: { hidden: false, addEventListener() {}, getElementById: () => null, createElement: element, body: { appendChild: node => { body.push(node); return node; } } } } });
    await f.store.refresh();
    assert.equal(f.store.orbFloating, false);
    f.store.toggleFloat();
    assert.equal(JSON.parse(kept.get('convo.orb.v1')).floating, true);
    assert.equal(body.length, 0);                                                    // nothing floats without a conversation
    await f.store.start();
    assert.equal(body.length, 1);
    const [float] = body, [orb, back] = float.children;
    assert.equal(float.className, 'convo-float');
    assert.equal(orb.className, 'convo-panel-orb convo-float-orb');
    assert.equal(orb.getAttribute('data-convo'), 'listening');                       // the same state as the other orbs
    assert.deepEqual([float.style.left, float.style.top], ['896px', '574px']);
    // Dragged: it follows the pointer, stays inside the window, and the click that ends the drag does not stop the conversation.
    float.listeners.pointerdown({ button: 0, clientX: 900, clientY: 600, pointerId: 1, target: { closest: () => null } });
    float.listeners.pointermove({ clientX: 500, clientY: 300 });
    float.listeners.pointermove({ clientX: -400, clientY: 300 });
    assert.deepEqual([float.style.left, float.style.top], ['4px', '274px']);
    float.listeners.pointerup({});
    orb.listeners.click({ preventDefault() {} });
    assert.equal(f.store.active, true);
    assert.deepEqual(JSON.parse(kept.get('convo.orb.v1')), { floating: true, x: 4, y: 274 });
    back.listeners.click({});
    assert.equal(f.store.orbFloating, false);
    assert.equal(body.length, 0);
    f.store.toggleFloat();
    assert.equal(body.length, 1);
    await new Promise(resolve => setTimeout(resolve, 5));
    body[0].children[0].listeners.click({ preventDefault() {} });                    // a plain click is the orb's own action: stop
    await new Promise(resolve => setTimeout(resolve, 5));
    assert.equal(f.store.active, false);
    assert.equal(body.length, 0);
});

test('The panel markup: one orb, a slot on the ready page, a dock beside the controls, names on the header buttons', async () => {
    const html = await readFile(new URL('../webui/main.html', import.meta.url), 'utf8');
    assert.equal((html.match(/class="convo-primary convo-panel-orb"/g) || []).length, 1);
    assert.match(html, /<span class="convo-orb-dock" aria-hidden="true"><\/span>/);
    assert.match(html, /<div class="convo-empty"[^>]*><span class="convo-orb-slot" aria-hidden="true" :data-open="\$store\.convo\.active \|\| \$store\.convo\.busy \? 'false' : 'true'"><\/span>/);
    assert.match(html, /'Click the orb to start a conversation\.'/);
    assert.doesNotMatch(html, /Tap to start a conversation|Microphone off · click the composer orb/);
    assert.match(html, /:data-rest="\$store\.convo\.resting \? 'true' : 'false'"/);
    assert.match(html, /\.convo-stage\[data-rest=true\]\{height:0/);                 // no empty row above the ready page
    assert.match(html, /\.convo-primary\{position:absolute;[^}]*transition:left \.6s[^}]*top \.6s[^}]*width \.6s[^}]*height \.6s/);
    assert.match(html, /\.convo-primary\[data-still\]\{transition:transform/);
    for (const [name, label] of [['convo-sound-button', 'Sound'], ['convo-studio-button', 'Voice Studio'], ['convo-settings-button', 'Settings']])
        assert.match(html, new RegExp(`class="button convo-iconbtn ${name}"[^>]* aria-label="[^"]+"[^>]*><span class="material-symbols-outlined" aria-hidden="true">\\w+</span><span class="convo-iconlabel">${label}</span></button>`), name);
    const tools = html.slice(html.indexOf('<div class="convo-tools">'), html.indexOf('</header>'));
    assert.doesNotMatch(tools, / title="/);                                          // the growing label is the name: no second tooltip
});
