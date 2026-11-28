// Convo's pop-out window: geometry clamped on restore and on viewport changes, saved only after a user's drag or resize while
// visible with a non-zero size, storage that throws ignored, keyboard move/resize, Escape kept away from host modals behind it,
// and a bottom sheet on narrow screens. A small fake DOM stands in for the page (bubbling events, pointer capture, focus).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { BIG_STEP, GAP, MIN_HEIGHT, MIN_WIDTH, NARROW, STEP, WINDOW_Z, buildWindow, clampGeometry, createFloatingWindow, defaultGeometry,
         isNarrow, nudge, readGeometry, saveGeometry, validGeometry } from '../webui/floating-window.js';

// ---- a minimal DOM: enough for the window's frame and its event handling
function fakeDocument({ width = 1400, height = 900 } = {}) {
    const listeners = {};
    const doc = {
        documentElement: { clientWidth: width, clientHeight: height }, activeElement: null, listeners,
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter(item => item !== fn); },
        createElement: tag => new FakeElement(tag, doc),
    };
    doc.body = new FakeElement('body', doc);
    return doc;
}
const matchesOne = (element, selector) => {
    const attribute = selector.match(/^\[([\w-]+)\]$/);
    return attribute ? element.attributes.has(attribute[1]) : element.tagName === selector.toUpperCase();
};
class FakeElement {
    constructor(tag, doc) {
        Object.assign(this, { tagName: tag.toUpperCase(), ownerDocument: doc, attributes: new Map(), children: [], parentNode: null, listeners: {}, hidden: false, textContent: '', rect: null });
        const props = new Map();
        this.style = new Proxy({ setProperty: (name, value) => props.set(name, value), removeProperty: name => props.delete(name), getPropertyValue: name => props.get(name) ?? '', props }, {
            get: (target, key) => (key in target ? target[key] : props.get(key) ?? ''), set: (_target, key, value) => { props.set(key, value); return true; } });
        const classes = new Set();
        this.classList = { add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name),
                           toggle: (name, on = !classes.has(name)) => { if (on) classes.add(name); else classes.delete(name); return on; } };
    }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
    hasAttribute(name) { return this.attributes.has(name); }
    append(...nodes) { for (const node of nodes) this.appendChild(node); }
    appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
    remove() { if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; }
    get isConnected() { let node = this; while (node.parentNode) node = node.parentNode; return node === this.ownerDocument.body; }
    matches(selector) { return selector.split(',').some(part => matchesOne(this, part.trim())); }
    closest(selector) { for (let node = this; node && node.matches; node = node.parentNode) if (node.matches(selector)) return node; return null; }
    querySelector(selector) { for (const child of this.children) { if (child.matches(selector)) return child; const found = child.querySelector(selector); if (found) return found; } return null; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(item => item !== fn); }
    getBoundingClientRect() {
        if (this.rect) return this.rect;
        const px = name => parseFloat(this.style.props.get(name)) || 0;
        return { left: px('left'), top: px('top'), width: this.hidden ? 0 : px('width'), height: this.hidden ? 0 : px('height') };
    }
    focus() { this.ownerDocument.activeElement = this; }
    setPointerCapture(id) { this.captured = id; }
    releasePointerCapture() { this.captured = null; }
}
// An event that bubbles from the target to the document, like the browser's, and records what the handlers did.
function fire(target, type, init = {}) {
    const event = { type, target, button: 0, pointerId: 1, clientX: 0, clientY: 0, shiftKey: false, ctrlKey: false, metaKey: false, ...init,
                    defaultPrevented: false, stopped: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; } };
    for (let node = target; node && !event.stopped; node = node.parentNode) for (const fn of [...(node.listeners?.[type] || [])]) fn(event);
    if (!event.stopped) for (const fn of [...(target.ownerDocument.listeners[type] || [])]) fn(event);
    return event;
}
// The pointer is captured by the element it went down on, so moves and the release arrive there and bubble to the document.
const drag = (from, dx, dy, { x = 500, y = 300 } = {}) => {
    fire(from, 'pointerdown', { clientX: x, clientY: y });
    fire(from, 'pointermove', { clientX: x + dx / 2, clientY: y + dy / 2 });
    fire(from, 'pointermove', { clientX: x + dx, clientY: y + dy });
    return fire(from, 'pointerup', { clientX: x + dx, clientY: y + dy });
};
function fakeStorage(initial = {}, { failRead = false, failWrite = false } = {}) {
    const values = new Map(Object.entries(initial)), writes = [];
    return { values, writes,
        getItem(key) { if (failRead) throw new Error('blocked'); return values.has(key) ? values.get(key) : null; },
        setItem(key, value) { if (failWrite) throw new Error('quota'); writes.push([key, value]); values.set(key, value); } };
}
function fakeView(doc) {
    const listeners = {};
    return { listeners, innerWidth: doc.documentElement.clientWidth, innerHeight: doc.documentElement.clientHeight, document: doc,
             addEventListener(type, fn) { (listeners[type] ||= []).push(fn); }, removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter(item => item !== fn); },
             resize(width, height) { doc.documentElement.clientWidth = width; doc.documentElement.clientHeight = height; for (const fn of listeners.resize || []) fn(); } };
}
const KEY = 'convo.diagnostics.window';
function mount({ width = 1400, height = 900, saved, storage: given, onEscape = () => {} } = {}) {
    const doc = fakeDocument({ width, height }), view = fakeView(doc);
    const storage = given || fakeStorage(saved ? { [KEY]: JSON.stringify(saved) } : {});
    const parts = buildWindow(doc, { component: '/plugins/_convo/webui/diagnostics.html' });
    doc.body.appendChild(parts.root);
    const win = createFloatingWindow({ root: parts.root, header: parts.header, grip: parts.grip, corner: parts.corner, storage, key: KEY, onEscape, view });
    win.place();
    const at = () => Object.fromEntries(['left', 'top', 'width', 'height'].map(side => [side, parseFloat(parts.root.style.props.get(side))]));
    return { doc, view, storage, parts, win, at };
}

test('geometry stays inside the viewport with a margin, keeps a minimum size where it can, and reads in whole pixels', () => {
    const viewport = { width: 1000, height: 700 };
    assert.deepEqual(clampGeometry({ left: -50, top: -20, width: 400, height: 300 }, viewport), { left: GAP, top: GAP, width: 400, height: 300 });
    assert.deepEqual(clampGeometry({ left: 900, top: 650, width: 400, height: 300 }, viewport), { left: 1000 - 400 - GAP, top: 700 - 300 - GAP, width: 400, height: 300 });
    assert.deepEqual(clampGeometry({ left: 10, top: 10, width: 5000, height: 5000 }, viewport), { left: GAP, top: GAP, width: 1000 - 2 * GAP, height: 700 - 2 * GAP });
    assert.deepEqual(clampGeometry({ left: 100, top: 100, width: 10, height: 10 }, viewport), { left: 100, top: 100, width: MIN_WIDTH, height: MIN_HEIGHT });
    assert.deepEqual(clampGeometry({ left: 100.4, top: 99.6, width: 400.5, height: 300.2 }, viewport), { left: 100, top: 100, width: 401, height: 300 });
    const tiny = clampGeometry({ left: 0, top: 0, width: 400, height: 400 }, { width: 200, height: 150 });   // smaller than the minimum: fills it
    assert.deepEqual(tiny, { left: GAP, top: GAP, width: 200 - 2 * GAP, height: 150 - 2 * GAP });
    const first = defaultGeometry(viewport);
    assert.deepEqual(clampGeometry(first, viewport), first);
    assert.ok(first.left + first.width <= viewport.width - GAP && first.top + first.height <= viewport.height - GAP);
    assert.equal(isNarrow({ width: NARROW - 1 }), true);
    assert.equal(isNarrow({ width: NARROW }), false);
});

test('a stored position is read only when it is a real geometry, and storage that throws reads as nothing', () => {
    assert.deepEqual(readGeometry(fakeStorage({ k: '{"left":1,"top":2,"width":300,"height":200,"extra":"x"}' }), 'k'), { left: 1, top: 2, width: 300, height: 200 });
    for (const stored of ['not json', '{"left":1,"top":2,"width":0,"height":200}', '{"left":"1","top":2,"width":300,"height":200}', 'null', '[]'])
        assert.equal(readGeometry(fakeStorage({ k: stored }), 'k'), null, stored);
    assert.equal(readGeometry(fakeStorage({}, { failRead: true }), 'k'), null);
    assert.equal(readGeometry(null, 'k'), null);
    assert.equal(validGeometry({ left: 0, top: 0, width: 1, height: NaN }), false);
});

test('a geometry is saved only while the window is visible and never with a zero size; a throwing store is survived', () => {
    const good = { left: 10, top: 20, width: 400, height: 300 };
    const storage = fakeStorage();
    assert.equal(saveGeometry(storage, 'k', good, { visible: false }), false);
    assert.equal(saveGeometry(storage, 'k', { ...good, width: 0 }, { visible: true }), false);
    assert.equal(saveGeometry(storage, 'k', { ...good, height: NaN }, { visible: true }), false);
    assert.deepEqual(storage.writes, []);
    assert.equal(saveGeometry(storage, 'k', good, { visible: true }), true);
    assert.deepEqual(JSON.parse(storage.values.get('k')), good);
    assert.equal(saveGeometry(fakeStorage({}, { failWrite: true }), 'k', good, { visible: true }), false);
});

test('arrow keys step the window by 16 px (64 with Ctrl); with resize they grow it from the lower right', () => {
    const g = { left: 100, top: 100, width: 400, height: 300 };
    assert.deepEqual(nudge(g, 'ArrowRight'), { ...g, left: 100 + STEP });
    assert.deepEqual(nudge(g, 'ArrowUp', { big: true }), { ...g, top: 100 - BIG_STEP });
    assert.deepEqual(nudge(g, 'ArrowDown', { resize: true }), { ...g, height: 300 + STEP });
    assert.deepEqual(nudge(g, 'ArrowLeft', { resize: true, big: true }), { ...g, width: 400 - BIG_STEP });
    assert.equal(nudge(g, 'Enter'), null);
    assert.equal(nudge(null, 'ArrowLeft'), null);
});

test('the frame is a labelled, non-modal dialog with named controls and a keyboard hint, built without innerHTML', async () => {
    const { parts } = mount();
    const root = parts.root;
    assert.deepEqual([root.getAttribute('role'), root.getAttribute('aria-modal')], ['dialog', 'false']);
    const title = root.querySelector('h2');
    assert.equal(root.getAttribute('aria-labelledby'), title.getAttribute('id'));
    assert.equal(title.textContent, 'Convo diagnostics');
    assert.match(root.querySelector('p').textContent, /arrow keys move it and Shift with arrow keys resizes it; Escape docks it/);
    for (const control of [parts.grip, parts.dock, parts.close, parts.corner]) {
        assert.equal(control.tagName, 'BUTTON');
        assert.equal(control.getAttribute('type'), 'button');
        assert.ok(control.getAttribute('aria-label'));
        assert.equal(control.children[0].getAttribute('aria-hidden'), 'true');               // the icon is decoration; the name is the label
    }
    const component = root.querySelector('x-component');
    assert.deepEqual([component.getAttribute('path'), component.getAttribute('mode')], ['/plugins/_convo/webui/diagnostics.html', 'window']);
    assert.ok(root.hasAttribute('x-data'));
    assert.deepEqual([root.style.position, root.style.zIndex], ['fixed', String(WINDOW_Z)]);
    assert.ok(WINDOW_Z > 3400 && WINDOW_Z < 5000);                                           // above the canvas, below every host modal
    const source = await readFile(new URL('../webui/floating-window.js', import.meta.url), 'utf8');
    assert.doesNotMatch(source, /innerHTML|^\s*import\s/m);
});

test('a saved position is restored clamped to the viewport, and restoring writes nothing back', () => {
    const { at, storage } = mount({ width: 1000, height: 700, saved: { left: 1800, top: -40, width: 500, height: 400 } });
    assert.deepEqual(at(), { left: 1000 - 500 - GAP, top: GAP, width: 500, height: 400 });
    assert.deepEqual(storage.writes, []);
    const fresh = mount({ width: 1000, height: 700 });
    assert.deepEqual(fresh.at(), defaultGeometry({ width: 1000, height: 700 }));
});

test('dragging the bar moves the window, clamped, and saves once when released; a click on the bar saves nothing', () => {
    const { parts, at, storage } = mount({ saved: { left: 300, top: 200, width: 400, height: 300 } });
    const event = drag(parts.header, 120, -60);
    assert.deepEqual(at(), { left: 420, top: 140, width: 400, height: 300 });
    assert.deepEqual(storage.writes.map(([key, value]) => [key, JSON.parse(value)]), [[KEY, { left: 420, top: 140, width: 400, height: 300 }]]);
    assert.equal(parts.header.captured, null);                                               // capture released
    assert.ok(event);
    drag(parts.header, -5000, -5000);                                                        // flung off screen: stays inside
    assert.deepEqual(at(), { left: GAP, top: GAP, width: 400, height: 300 });
    const writes = storage.writes.length;
    fire(parts.header, 'pointerdown', { clientX: 10, clientY: 10 }); fire(parts.header, 'pointerup', { clientX: 10, clientY: 10 });
    assert.equal(storage.writes.length, writes);
    drag(parts.grip, 30, 30);                                                                // the move button is a drag handle too
    assert.deepEqual(at(), { left: GAP + 30, top: GAP + 30, width: 400, height: 300 });
    drag(parts.dock, 200, 200);                                                              // other buttons never start a drag
    fire(parts.header, 'pointerdown', { button: 2, clientX: 0, clientY: 0 }); fire(parts.header, 'pointermove', { clientX: 300, clientY: 300 });
    assert.deepEqual(at(), { left: GAP + 30, top: GAP + 30, width: 400, height: 300 });     // nor does a right button
});

test('the corner resizes the window, never below its minimum, and saves the new size', () => {
    const { parts, at, storage } = mount({ saved: { left: 100, top: 100, width: 400, height: 300 } });
    drag(parts.corner, 80, 50);
    assert.deepEqual(at(), { left: 100, top: 100, width: 480, height: 350 });
    drag(parts.corner, -1000, -1000);
    assert.deepEqual(at(), { left: 100, top: 100, width: MIN_WIDTH, height: MIN_HEIGHT });
    assert.deepEqual(JSON.parse(storage.values.get(KEY)), { left: 100, top: 100, width: MIN_WIDTH, height: MIN_HEIGHT });
});

test('hidden, zero-size or storage-less moments never overwrite the saved position', () => {
    const saved = { left: 100, top: 100, width: 400, height: 300 };
    const hidden = mount({ saved });
    hidden.parts.root.hidden = true;
    drag(hidden.parts.header, 50, 50);
    assert.deepEqual(JSON.parse(hidden.storage.values.get(KEY)), saved);
    const zero = mount({ saved });
    zero.parts.root.rect = { left: 0, top: 0, width: 0, height: 0 };                         // display:none somewhere above it
    drag(zero.parts.corner, 50, 50);
    fire(zero.parts.grip, 'keydown', { key: 'ArrowRight' });
    assert.deepEqual(zero.storage.writes, []);
    const detached = mount({ saved });
    detached.parts.root.remove();
    fire(detached.parts.grip, 'keydown', { key: 'ArrowRight' });
    assert.deepEqual(detached.storage.writes, []);
    const throwing = mount({ storage: fakeStorage({}, { failRead: true, failWrite: true }) });
    const before = throwing.at();
    drag(throwing.parts.header, -40, -40);                                                   // still moves; the failed save is ignored
    const chosen = { ...before, left: before.left - 40, top: before.top - 40 };
    assert.deepEqual(throwing.at(), chosen);
    throwing.view.resize(700, 500); throwing.view.resize(1400, 900);                         // the choice outlives a viewport change without storage
    assert.deepEqual(throwing.at(), chosen);
});

test('the keyboard moves (arrows) and resizes (Shift+arrows, or arrows on the corner) and keeps those keys to itself', () => {
    const { parts, at, storage } = mount({ saved: { left: 300, top: 200, width: 400, height: 300 } });
    const moved = fire(parts.grip, 'keydown', { key: 'ArrowRight' });
    assert.deepEqual(at(), { left: 316, top: 200, width: 400, height: 300 });
    assert.ok(moved.defaultPrevented && moved.stopped);
    fire(parts.grip, 'keydown', { key: 'ArrowDown', shiftKey: true });
    fire(parts.grip, 'keydown', { key: 'ArrowLeft', ctrlKey: true });
    assert.deepEqual(at(), { left: 316 - BIG_STEP, top: 200, width: 400, height: 316 });
    fire(parts.corner, 'keydown', { key: 'ArrowRight' });
    assert.deepEqual(at(), { left: 316 - BIG_STEP, top: 200, width: 416, height: 316 });
    assert.equal(storage.writes.length, 4);                                                  // each step is a deliberate move: saved
    const elsewhere = fire(parts.dock, 'keydown', { key: 'ArrowRight' });                   // arrows on other controls are theirs
    assert.equal(elsewhere.defaultPrevented, false);
    assert.equal(fire(parts.grip, 'keydown', { key: 'a' }).defaultPrevented, false);
});

test('Escape inside the window docks it and stops there, so a host modal behind it is not closed', () => {
    let docked = 0, hostClosed = 0;
    const { parts, doc } = mount({ onEscape: () => { docked += 1; } });
    doc.addEventListener('keydown', event => { if (event.key === 'Escape') hostClosed += 1; });   // the host's modal handler (document level)
    const inside = parts.root.querySelector('x-component');
    const event = fire(inside, 'keydown', { key: 'Escape' });
    assert.deepEqual([docked, hostClosed, event.defaultPrevented, event.stopped], [1, 0, true, true]);
    fire(doc.body, 'keydown', { key: 'Escape' });                                            // focus elsewhere: the host's Escape as before
    assert.deepEqual([docked, hostClosed], [1, 1]);
});

test('a smaller viewport clamps the window without saving, and a larger one brings the user\'s choice back', () => {
    const { view, at, storage, parts } = mount({ width: 1400, height: 900, saved: { left: 900, top: 500, width: 420, height: 360 } });
    drag(parts.header, 20, 20);                                                              // the user's choice: 920,520
    const writes = storage.writes.length;
    view.resize(800, 600);
    assert.deepEqual(at(), { left: 800 - 420 - GAP, top: 600 - 360 - GAP, width: 420, height: 360 });
    view.resize(1400, 900);
    assert.deepEqual(at(), { left: 920, top: 520, width: 420, height: 360 });
    assert.equal(storage.writes.length, writes);
});

test('on a narrow screen the window is a bottom sheet: no inline geometry, no dragging, no keyboard moves, nothing saved', () => {
    const { parts, view, storage, win } = mount({ width: 1000, height: 800, saved: { left: 100, top: 100, width: 400, height: 300 } });
    view.resize(NARROW - 40, 700);
    assert.equal(win.sheet, true);
    assert.equal(parts.root.classList.contains('is-sheet'), true);
    assert.deepEqual(['left', 'top', 'width', 'height'].map(side => parts.root.style.props.has(side)), [false, false, false, false]);
    drag(parts.header, 40, 40);
    fire(parts.grip, 'keydown', { key: 'ArrowRight' });
    assert.deepEqual(['left', 'top'].map(side => parts.root.style.props.has(side)), [false, false]);
    assert.deepEqual(storage.writes, []);
    view.resize(1000, 800);
    assert.equal(parts.root.classList.contains('is-sheet'), false);
    assert.equal(parts.root.style.props.get('left'), '100px');
});

test('destroy removes every listener it added', () => {
    const { parts, view, at, win, doc } = mount({ saved: { left: 200, top: 200, width: 400, height: 300 } });
    fire(parts.header, 'pointerdown', { clientX: 0, clientY: 0 });                           // a drag in progress is dropped too
    win.destroy();
    fire(parts.header, 'pointermove', { clientX: 300, clientY: 300 });
    drag(parts.header, 50, 50);
    fire(parts.grip, 'keydown', { key: 'ArrowRight' });
    view.resize(500, 400);
    assert.deepEqual(at(), { left: 200, top: 200, width: 400, height: 300 });
    assert.deepEqual([doc.listeners.pointermove, doc.listeners.pointerup, view.listeners.resize].map(list => (list || []).length), [0, 0, 0]);
});
