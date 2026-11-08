// The composer orb: state, labels, sound-cue transitions and DOM painting. No imports. The host's microphone
// button stays the single control: Convo only layers a visual state on it and restores the native one when off.
// A hover is a hover: nothing here shows "listening" unless a session actually is.

const LABELS = {
    ready: ['Convo ready, microphone off', 'Click to start a conversation; hold to dictate'],
    idle: ['Convo voice idle, AI Link development mode', 'Hold to dictate with native speech'],
    connecting: ['Convo starting', 'Click to cancel'],
    listening: ['Convo listening', 'Click to stop; hold to dictate'],
    hearing: ['Convo hearing you', 'Click to stop'],
    thinking: ['Convo thinking', 'Click to stop'],
    speaking: ['Convo speaking', 'Click to stop'],
    'dictating-latched': ['Hands-free dictation', 'Listening for speech; click to stop, Escape cancels'],
    'dictating-ptt': ['Push-to-talk', 'Listening only while held; release to finish'],
    error: ['Convo needs attention', 'Click to try again'],
};
export const ORB_STATES = Object.keys(LABELS);
const ACTIVE = new Set(['connecting', 'listening', 'hearing', 'thinking', 'speaking']);
const DICTATING = new Set(['dictating-latched', 'dictating-ptt']);
// While the press that started latched dictation is still down, the label says what releasing does.
const HELD_LATCHED_HINT = 'Release to keep listening hands-free; keep holding for push-to-talk';
// Draft in composer (continuous): the same latched glyph and ring, but the words say that it goes on until the user sends or stops.
const CONTINUOUS = ['Dictating into the composer', 'keeps listening until you send or stop', 'click to stop, Escape drops the phrase in progress'];

// snapshot: { enabled, dictating, dictationMode, continuous, holding, pressing, error, busy, active, state, hearing, playing, devIdle, policyHold }
// `continuous`: latched dictation is drafting continuously into the composer (only the words change, never the state id).
// `dictationMode` is 'latched' (the default) or 'ptt'; `holding` is the press still being down; `pressing` is a press that has not yet
// reached the first threshold (the ring charges, nothing else changes). `capturing` is the silent pre-open buffer: the microphone is
// really on (the browser says so), but nothing is promised or delivered yet, so the only change is a quiet solid inner ring.
// `playing`: assistant audio is audible right now. The server's turn can end (state back to listening) while buffered audio is still
// playing, and the orb must keep reading "speaking" for as long as it is.
// Returns null when Convo does not own the orb (native look and behaviour stay untouched).
export function orbView(snapshot) {
    const s = snapshot || {};
    if (!s.enabled) return null;
    let id;
    if (s.dictating) id = s.dictationMode === 'ptt' ? 'dictating-ptt' : 'dictating-latched';
    else if (s.error) id = 'error';
    else if (s.busy || s.state === 'connecting') id = 'connecting';
    else if (!s.active) id = s.devIdle ? 'idle' : 'ready';
    else if (s.state === 'speaking' || s.playing) id = 'speaking';
    else if (s.state === 'considering' || s.state === 'thinking') id = 'thinking';
    else if (s.hearing) id = 'hearing';
    else id = 'listening';
    const continuous = id === 'dictating-latched' && !!s.continuous;
    const [name, labelHint] = continuous ? CONTINUOUS : LABELS[id];
    const hint = id === 'dictating-latched' && s.holding ? HELD_LATCHED_HINT : labelHint;
    const detail = id === 'error' && s.error ? ` ${String(s.error).slice(0, 140)}` : continuous && !s.holding ? `; ${CONTINUOUS[2]}` : '';
    return { id, hold: !!s.policyHold && id === 'speaking', press: !!s.pressing && !DICTATING.has(id), capture: !!s.capturing && !DICTATING.has(id),
             pressed: id !== 'ready' && id !== 'idle' && id !== 'error',
             label: continuous && !s.holding ? `${name} — ${hint}.` : `${name}. ${hint}.`, title: `${name} · ${hint}${detail}` };
}

// Sound hooks follow state transitions (never call sites), so every path to a state cues the same way.
export function cueFor(previous, next) {
    if (!previous || !next || previous === next) return null;
    if (DICTATING.has(next)) return DICTATING.has(previous) ? (next === 'dictating-ptt' ? 'dictation-ptt' : null) : 'dictation-start';
    if (DICTATING.has(previous)) return ACTIVE.has(next) ? 'activate' : 'dictation-end';
    if (next === 'error') return 'error';
    if (!ACTIVE.has(previous) && ACTIVE.has(next)) return 'activate';
    if (ACTIVE.has(previous) && !ACTIVE.has(next) && previous !== 'error') return 'deactivate';
    return null;
}

// Fire-and-forget: a missing, blocked or failing sound can never affect capture, playback or lifecycle.
export function emitCue(name) {
    try {
        globalThis.dispatchEvent?.(new globalThis.CustomEvent('convo:cue', { detail: { name } }));
    } catch { /* cues are optional */ }
}

const GLYPHS = '<svg class="co-glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" focusable="false">'
    + '<g class="g g-mic"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/></g>'
    + '<g class="g g-idle"><path d="M9 6.5v11M15 6.5v11"/></g>'
    + '<g class="g g-spin"><circle cx="12" cy="12" r="7" stroke-dasharray="30 14"/></g>'
    + '<g class="g g-bars" fill="currentColor" stroke="none"><rect class="b b0" x="2.6" y="3" width="3.2" height="18" rx="1.6"/><rect class="b b1" x="7.6" y="3" width="3.2" height="18" rx="1.6"/><rect class="b b2" x="12.6" y="3" width="3.2" height="18" rx="1.6"/><rect class="b b3" x="17.6" y="3" width="3.2" height="18" rx="1.6"/></g>'
    + '<g class="g g-dots" fill="currentColor" stroke="none"><circle class="d0" cx="5.5" cy="12" r="2"/><circle class="d1" cx="12" cy="12" r="2"/><circle class="d2" cx="18.5" cy="12" r="2"/></g>'
    + '<g class="g g-voice"><path d="M3.5 9.5h3.2L11 6v12l-4.3-3.5H3.5z" fill="currentColor" stroke="none"/><path class="w w1" d="M14.5 9.2a4.2 4.2 0 0 1 0 5.6"/><path class="w w2" d="M17.5 6.4a8.2 8.2 0 0 1 0 11.2"/></g>'
    + '<g class="g g-pen"><path d="M4.5 19.5l.9-3.9L16 5a2 2 0 0 1 3 3L8.4 18.6z"/><path d="M14.3 6.7l3 3"/></g>'
    + '<g class="g g-ptt"><rect x="9" y="2.5" width="6" height="10" rx="3"/><path d="M6 10.5a6 6 0 0 0 12 0"/><path d="M7 20.5h10" stroke-width="3"/></g>'
    + '<g class="g g-err"><path d="M12 5.5v8.5"/><circle cx="12" cy="18.3" r="1.2" fill="currentColor" stroke="none"/></g></svg>';

// A polite, visually hidden live region for gesture transitions (latched, push-to-talk, cancelled, nothing heard). The orb's own
// label changes too, but a screen reader does not announce a label change on a button that is not focused. The text is written only
// when it changes, and the region lives on the document so it never alters the host's composer markup.
let liveRegion = null;
export function announce(text) {
    try {
        const doc = globalThis.document;
        if (!doc?.createElement || !doc.body?.appendChild) return false;
        if (!liveRegion || !liveRegion.isConnected) {
            liveRegion = doc.createElement('div');
            liveRegion.setAttribute('role', 'status'); liveRegion.setAttribute('aria-live', 'polite'); liveRegion.setAttribute('aria-atomic', 'true');
            liveRegion.setAttribute('data-convo-live', '');
            liveRegion.style.cssText = 'position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0';
            doc.body.appendChild(liveRegion);
        }
        const value = String(text || '');
        if (liveRegion.textContent === value) liveRegion.textContent = '';        // the same words twice in a row still announce
        liveRegion.textContent = value;
        return true;
    } catch { return false; }
}
export const resetAnnouncer = () => { liveRegion?.remove?.(); liveRegion = null; };

const saved = new WeakMap();      // native attributes, the last view, and the watcher that keeps ours in place

function layers(mic) {
    let orb = mic.querySelector(':scope > .convo-orb');
    if (!orb) {
        const doc = mic.ownerDocument;
        orb = doc.createElement('span');
        orb.className = 'convo-orb';
        orb.setAttribute('aria-hidden', 'true');
        orb.innerHTML = '<i class="co-glow"></i><i class="co-ring"></i><i class="co-ring2"></i><i class="co-core">' + GLYPHS + '</i>';
        mic.appendChild(orb);
    }
    return orb;
}

function setIfChanged(element, name, value) {
    if (element.getAttribute(name) !== value) element.setAttribute(name, value);
}

// Where the hover tooltip lives. The host's tooltip system (Bootstrap, driven by a MutationObserver on `title` and
// `data-bs-original-title`) takes over `title` on any element it decorates: it moves the text into
// `data-bs-original-title` and REMOVES `title`. Anything that keeps writing `title` (or watches it) would ping-pong with
// that observer forever and freeze the page, so with that system present the text goes straight into its own attribute,
// and without it a plain native `title` is used. Never watched, never fought over.
const tooltipAttribute = () => (globalThis.bootstrap?.Tooltip ? 'data-bs-original-title' : 'title');
const TOOLTIP_ATTRIBUTES = ['title', 'data-bs-original-title'];

function applyView(mic, view) {
    setIfChanged(mic, 'data-convo', view.id);
    if (view.hold) setIfChanged(mic, 'data-convo-hold', ''); else mic.removeAttribute('data-convo-hold');
    if (view.press) setIfChanged(mic, 'data-convo-press', ''); else mic.removeAttribute('data-convo-press');
    if (view.capture) setIfChanged(mic, 'data-convo-capture', ''); else mic.removeAttribute('data-convo-capture');
    setIfChanged(mic, 'aria-label', view.label);
    setIfChanged(mic, tooltipAttribute(), view.title);
    setIfChanged(mic, 'aria-pressed', view.pressed ? 'true' : 'false');
}

// Apply a view to the host microphone button. Attributes are written only when they change (never at the metering rate).
// The host rewrites the label and pressed state whenever its own speech state changes; a watcher puts ours back at once,
// so an assistive technology never reads a stale native label while Convo owns the button. The tooltip text is written
// with the view but is deliberately not watched (see tooltipAttribute).
export function syncOrb(mic, view, level = 0) {
    if (!mic) return;
    if (!view) { releaseOrb(mic); return; }
    let record = saved.get(mic);
    if (!record) {
        record = { label: mic.getAttribute('aria-label'), tips: TOOLTIP_ATTRIBUTES.map(name => mic.getAttribute(name)), pressed: mic.getAttribute('aria-pressed'), view, watcher: null };
        saved.set(mic, record);
        const Observer = globalThis.MutationObserver;
        if (Observer && mic.nodeType === 1) {
            record.watcher = new Observer(() => { if (saved.get(mic) === record) applyView(mic, record.view); });
            record.watcher.observe(mic, { attributes: true, attributeFilter: ['aria-label', 'aria-pressed'] });
        }
    }
    record.view = view;
    layers(mic);
    applyView(mic, view);
    mic.style.setProperty('--convo-level', String(Math.max(0, Math.min(1, Number(level) || 0)).toFixed(2)));
}

// Convo off, or its plugin disabled: remove every trace and give the native button back exactly as it was.
export function releaseOrb(mic) {
    if (!mic || !mic.hasAttribute?.('data-convo')) return;
    mic.removeAttribute('data-convo');
    mic.removeAttribute('data-convo-hold');
    mic.removeAttribute('data-convo-press');
    mic.removeAttribute('data-convo-capture');
    mic.style.removeProperty('--convo-level');
    mic.querySelector(':scope > .convo-orb')?.remove();
    const original = saved.get(mic);
    original?.watcher?.disconnect();
    saved.delete(mic);                      // before restoring, so the watcher cannot fight the restore
    for (const [attribute, key] of [['aria-label', 'label'], ['aria-pressed', 'pressed']]) {
        if (original?.[key] == null) mic.removeAttribute(attribute); else mic.setAttribute(attribute, original[key]);
    }
    TOOLTIP_ATTRIBUTES.forEach((attribute, index) => {                // the tooltip text goes back exactly as the host had it
        const before = original?.tips?.[index] ?? null;
        if (before == null) { if (mic.hasAttribute(attribute)) mic.removeAttribute(attribute); }
        else if (mic.getAttribute(attribute) !== before) mic.setAttribute(attribute, before);
    });
}
