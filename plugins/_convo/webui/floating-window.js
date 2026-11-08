// A free-floating, non-modal window for the diagnostics panel: pure geometry (clamping, defaults, keyboard steps, storage
// checks) plus a thin DOM layer (pointer drag with capture, a resize handle, keyboard move/resize, Escape). No imports.
//
// Convo builds its own window instead of using the host's floating surface helper, which parks itself behind other modals,
// writes zeros while hidden and does not keep its position. Rules kept here: the geometry is clamped on restore and on every
// viewport change; it is saved only after a drag or resize the user made while the window was visible with a non-zero size;
// storage that throws is ignored; below NARROW the window is a bottom sheet with no drag. Origin: written afresh after the
// draggable diagnostics panel of Omni-NexusAI/speech-to-speech (Apache-2.0, web/hf-realtime-voice/main.js
// initDiagnosticsGeometry); nothing is copied, and its unclamped restore and zero-size saves are not carried over.

export const GAP = 8;                 // the window keeps this far from every viewport edge
export const MIN_WIDTH = 320;
export const MIN_HEIGHT = 220;
export const NARROW = 640;            // below this viewport width the window becomes a bottom sheet
export const STEP = 16;               // one arrow-key step, in pixels
export const BIG_STEP = 64;           // with Ctrl (or Cmd)
// Above the host canvas (1200; 3400 on phones), below host modals (5000 and up) and tooltips (9000).
export const WINDOW_Z = 4000;

const finite = value => typeof value === 'number' && Number.isFinite(value);
export function validGeometry(value) {
    return !!value && typeof value === 'object' && ['left', 'top', 'width', 'height'].every(key => finite(value[key])) && value.width > 0 && value.height > 0;
}

// Inside the viewport with GAP to spare, at least the minimum size where the viewport allows it, whole pixels.
export function clampGeometry(geometry, viewport, gap = GAP) {
    const maxWidth = Math.max(1, viewport.width - gap * 2), maxHeight = Math.max(1, viewport.height - gap * 2);
    const width = Math.min(Math.max(Math.min(MIN_WIDTH, maxWidth), geometry.width), maxWidth);
    const height = Math.min(Math.max(Math.min(MIN_HEIGHT, maxHeight), geometry.height), maxHeight);
    const left = Math.min(Math.max(gap, geometry.left), Math.max(gap, viewport.width - width - gap));
    const top = Math.min(Math.max(gap, geometry.top), Math.max(gap, viewport.height - height - gap));
    return { left: Math.round(left), top: Math.round(top), width: Math.round(width), height: Math.round(height) };
}

// First placement: lower right, clear of the composer's edge.
export function defaultGeometry(viewport) {
    const width = Math.min(460, viewport.width - GAP * 2), height = Math.min(620, viewport.height - GAP * 2);
    return clampGeometry({ left: viewport.width - width - 24, top: viewport.height - height - 24, width, height }, viewport);
}

export const isNarrow = viewport => viewport.width < NARROW;

export function readGeometry(storage, key) {
    try {
        const value = JSON.parse(storage?.getItem?.(key) || 'null');
        return validGeometry(value) ? { left: value.left, top: value.top, width: value.width, height: value.height } : null;
    } catch { return null; }
}

// Saved only from a user's drag or resize, only while visible, never a zero or broken size. True when stored.
export function saveGeometry(storage, key, geometry, { visible = false } = {}) {
    if (!visible || !validGeometry(geometry)) return false;
    try { storage.setItem(key, JSON.stringify({ left: geometry.left, top: geometry.top, width: geometry.width, height: geometry.height })); return true; }
    catch { return false; }
}

// One arrow-key step: move, or (resize) grow and shrink from the lower right corner. null for any other key.
export function nudge(geometry, key, { resize = false, big = false } = {}) {
    const delta = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[key];
    if (!delta || !validGeometry(geometry)) return null;
    const step = big ? BIG_STEP : STEP, [dx, dy] = delta;
    return resize ? { ...geometry, width: geometry.width + dx * step, height: geometry.height + dy * step }
                  : { ...geometry, left: geometry.left + dx * step, top: geometry.top + dy * step };
}

// ---- the DOM layer

// The window's frame: a labelled, non-modal dialog with a drag bar (move handle, title, dock, close), a body and a resize
// handle. Built with createElement only; the caller puts it on <body>. `component` is the x-component path for the body.
export function buildWindow(doc, { id = 'convo-diag-window', title = 'Convo diagnostics', component = '', mode = 'window' } = {}) {
    const element = (tag, attributes = {}, text = '') => {
        const node = doc.createElement(tag);
        for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
        if (text) node.textContent = text;
        return node;
    };
    const icon = name => element('span', { class: 'material-symbols-outlined', 'aria-hidden': 'true' }, name);
    const button = (attributes, glyph) => { const node = element('button', { type: 'button', ...attributes }); node.appendChild(icon(glyph)); return node; };
    const root = element('section', { class: 'convo-diag-window', role: 'dialog', 'aria-modal': 'false', 'aria-labelledby': id + '-title',
                                      'aria-describedby': id + '-keys', 'data-convo-diag-window': '', 'x-data': '' });
    const header = element('header', { class: 'convo-diag-window-bar', 'data-window-header': '' });
    const grip = button({ class: 'convo-diag-window-icon', 'data-drag-handle': '', 'aria-label': 'Move window', 'aria-describedby': id + '-keys' }, 'drag_indicator');
    const heading = element('h2', { id: id + '-title', class: 'convo-diag-window-title' }, title);
    const dock = button({ class: 'convo-diag-window-icon', 'data-window-dock': '', 'aria-label': 'Dock in the side canvas' }, 'dock_to_right');
    const close = button({ class: 'convo-diag-window-icon', 'data-window-close': '', 'aria-label': 'Close the diagnostics window' }, 'close');
    header.append(grip, heading, dock, close);
    const keys = element('p', { id: id + '-keys', class: 'convo-diag-sr-only' },
        'Drag the bar to move the window. With the move or resize button focused, arrow keys move it and Shift with arrow keys resizes it; Escape docks it back in the side canvas.');
    const body = element('div', { class: 'convo-diag-window-body' });
    if (component) body.appendChild(element('x-component', { path: component, mode }));
    const corner = button({ class: 'convo-diag-window-resize', 'data-resize-handle': '', 'aria-label': 'Resize window', 'aria-describedby': id + '-keys' }, 'south_east');
    root.append(header, keys, body, corner);
    return { root, header, grip, corner, dock, close, body };
}

// Behaviour for a built window. `onEscape` runs for Escape anywhere inside it (the event goes no further, so a host modal
// behind it stays open). Returns { place, geometry, destroy, sheet }.
export function createFloatingWindow({ root, header, grip, corner, storage, key, onEscape = () => {}, view = globalThis, zIndex = WINDOW_Z }) {
    const doc = root.ownerDocument || view.document;
    // `wanted` is what the user last chose in this window's life; a smaller viewport shows a clamped copy and a larger one
    // brings the choice back.
    let geometry = null, wanted = null, drag = null, sheet = false;
    root.style.position = 'fixed';
    root.style.zIndex = String(zIndex);
    const viewport = () => ({ width: doc.documentElement?.clientWidth || view.innerWidth || 0, height: doc.documentElement?.clientHeight || view.innerHeight || 0 });
    const shown = () => {
        if (!root.isConnected || root.hidden) return false;
        const rect = root.getBoundingClientRect?.();
        return !!rect && rect.width > 0 && rect.height > 0;
    };
    const apply = () => {
        for (const side of ['left', 'top', 'width', 'height']) {
            if (sheet) root.style.removeProperty(side); else root.style.setProperty(side, geometry[side] + 'px');
        }
    };
    const commit = () => !sheet && saveGeometry(storage, key, geometry, { visible: shown() });
    // Restore (or default) and clamp. Runs on open and on every viewport change; never saves.
    const place = () => {
        const size = viewport();
        sheet = isNarrow(size);
        root.classList.toggle('is-sheet', sheet);
        if (!sheet) geometry = clampGeometry(wanted || readGeometry(storage, key) || defaultGeometry(size), size);
        apply();
        return geometry;
    };
    const choose = next => { geometry = wanted = clampGeometry(next, viewport()); apply(); };

    const ignored = target => {
        const control = target?.closest?.('button, input, select, textarea, a, summary, [data-no-drag]');
        return !!control && !control.hasAttribute('data-drag-handle');
    };
    const begin = (kind, target, event) => {
        if (event.button !== 0 || sheet || !geometry) return;
        drag = { kind, target, pointer: event.pointerId, x: event.clientX, y: event.clientY, from: { ...geometry } };
        try { target.setPointerCapture?.(event.pointerId); } catch { /* capture is a nicety */ }
        doc.addEventListener('pointermove', move);
        doc.addEventListener('pointerup', end);
        doc.addEventListener('pointercancel', end);
        event.preventDefault();
    };
    const move = event => {
        if (!drag || event.pointerId !== drag.pointer) return;
        const dx = event.clientX - drag.x, dy = event.clientY - drag.y, from = drag.from;
        if (!dx && !dy) return;
        drag.moved = true;
        choose(drag.kind === 'move' ? { ...from, left: from.left + dx, top: from.top + dy } : { ...from, width: from.width + dx, height: from.height + dy });
    };
    const end = event => {
        if (!drag || event.pointerId !== drag.pointer) return;
        try { drag.target.releasePointerCapture?.(drag.pointer); } catch { /* already released */ }
        const moved = drag.moved;
        drag = null;
        doc.removeEventListener('pointermove', move);
        doc.removeEventListener('pointerup', end);
        doc.removeEventListener('pointercancel', end);
        if (moved) commit();                 // a click on the bar is not a drag: nothing is saved
    };
    const onHeader = event => { if (!ignored(event.target)) begin('move', header, event); };
    const onCorner = event => begin('resize', corner, event);
    const onKey = event => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onEscape(); return; }
        const target = event.target;
        const resize = target === corner || (target === grip && event.shiftKey);
        if ((target !== grip && target !== corner) || sheet || !geometry) return;
        const next = nudge(geometry, event.key, { resize, big: event.ctrlKey || event.metaKey });
        if (!next) return;
        event.preventDefault(); event.stopPropagation();
        choose(next);
        commit();
    };
    header.addEventListener('pointerdown', onHeader);
    corner.addEventListener('pointerdown', onCorner);
    root.addEventListener('keydown', onKey);
    view.addEventListener?.('resize', place);
    return {
        place, get sheet() { return sheet; }, geometry: () => (geometry ? { ...geometry } : null),
        destroy() {
            header.removeEventListener('pointerdown', onHeader);
            corner.removeEventListener('pointerdown', onCorner);
            root.removeEventListener('keydown', onKey);
            view.removeEventListener?.('resize', place);
            doc.removeEventListener('pointermove', move);
            doc.removeEventListener('pointerup', end);
            doc.removeEventListener('pointercancel', end);
            drag = null;
        },
    };
}
