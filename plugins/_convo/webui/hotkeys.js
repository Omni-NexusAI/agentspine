// Convo's hotkeys: what a combination is, which ones can be used, and the DOM adapter that turns key presses into actions. No imports.
// Everything up to `bindHotkeys` is pure (events are read, never changed); `bindHotkeys` is the thin adapter (document and window are
// passed in, the store acts). The browser copy of convo_contract's HOTKEY_* tables (tests/fixtures/hotkey_cases.json holds the two
// together).
//
// A combination is written "Ctrl+Alt+Shift+Meta+<key>" (any subset of the modifiers), '' means off. The key is the KeyboardEvent.code,
// so a layout does not matter: letters and digits without their prefix ("M", "1"), everything else by its code ("Space", "F8", "Comma").
export const MODIFIERS = ['Ctrl', 'Alt', 'Shift', 'Meta'];
export const ACTIONS = [
    { id: 'toggle_conversation', label: 'Start or stop the conversation' },
    { id: 'mute', label: 'Mute or unmute the microphone', hint: 'While a conversation runs.' },
    { id: 'push_to_talk', label: 'Push to talk while muted', hint: 'Listens while you hold it; letting go sends what you said.' },
    { id: 'dictate', label: 'Dictate', hint: 'Starts hands-free dictation; press again to finish.' },
    { id: 'stop_reply', label: 'Stop the current reply', hint: 'The conversation goes on.' },
    { id: 'open_panel', label: 'Open the Convo panel' },
    { id: 'open_settings', label: 'Open the Convo settings' },
    { id: 'run_action', label: 'Run what Convo asks about', hint: 'Answers the open question in the Convo panel.' },
    { id: 'dismiss_action', label: 'Dismiss what Convo asks about' },
];
// The setting `hotkey` from before this table: one more combination for the same action, kept because it is saved and known.
export const SECOND_TOGGLE = 'hotkey';
export const DEFAULT_HOTKEYS = { toggle_conversation: 'Alt+Shift+C', mute: 'Alt+Shift+M', push_to_talk: 'Alt+Shift+Space', dictate: 'Alt+Shift+D',
    stop_reply: 'Alt+Shift+S', open_panel: '', open_settings: '', run_action: '', dismiss_action: '' };

// Keys a combination may end with. Escape, Enter, Tab, Backspace and Delete are never hotkeys (the browser, the page and the recorder
// need them); modifiers are not keys.
const NAMED = ['Space', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown', 'Insert', 'Minus', 'Equal', 'BracketLeft',
    'BracketRight', 'Backslash', 'Semicolon', 'Quote', 'Comma', 'Period', 'Slash', 'Backquote', 'IntlBackslash', 'IntlRo', 'IntlYen', 'NumpadAdd',
    'NumpadSubtract', 'NumpadMultiply', 'NumpadDivide', 'NumpadDecimal'];
// Keys that type nothing, so they need no modifier: function keys, media keys and the two that nothing else uses.
const QUIET = ['MediaPlayPause', 'MediaStop', 'MediaTrackNext', 'MediaTrackPrevious', 'Pause', 'ScrollLock'];
const KEY = new RegExp(`^(?:[A-Z]|[0-9]|F(?:[1-9]|1[0-9]|2[0-4])|Numpad[0-9]|${[...NAMED, ...QUIET].join('|')})$`);
const QUIET_KEY = new RegExp(`^(?:F(?:[1-9]|1[0-9]|2[0-4])|${QUIET.join('|')})$`);
// Combinations the browser or Agent Zero's page uses itself (a browser may not even hand them to a page). Meta counts as Ctrl here:
// on a Mac the same shortcuts use Cmd.
export const RESERVED = ['Ctrl+W', 'Ctrl+T', 'Ctrl+N', 'Ctrl+R', 'Ctrl+L', 'Ctrl+F', 'Ctrl+Q', 'Ctrl+Shift+W', 'Ctrl+Shift+T', 'Ctrl+Shift+N', 'Ctrl+Shift+R',
    'Ctrl+A', 'Ctrl+C', 'Ctrl+V', 'Ctrl+X', 'Ctrl+Z', 'Ctrl+Y', 'Ctrl+Shift+Z', 'F5', 'Ctrl+F5', 'Shift+F5', 'F11', 'F12', 'Alt+F4', 'Ctrl+F4',
    'Alt+ArrowLeft', 'Alt+ArrowRight', 'Alt+Space', ...Array.from({ length: 10 }, (_, digit) => `Ctrl+${digit}`)];
const reserved = new Set(RESERVED);

// {modifiers (in MODIFIERS order), key}, or null when the text is not a combination of known names.
export function parseCombo(value) {
    if (typeof value !== 'string' || !value || value.length > 40) return null;
    const parts = value.split('+'), key = parts.pop();
    if (!KEY.test(key) || new Set(parts).size !== parts.length || parts.some(part => !MODIFIERS.includes(part))) return null;
    return { modifiers: MODIFIERS.filter(name => parts.includes(name)), key };
}
const joined = ({ modifiers, key }) => [...modifiers, key].join('+');
// One spelling per combination (modifier order), '' for anything that is not one.
export function normalCombo(value) { const combo = parseCombo(value); return combo ? joined(combo) : ''; }

// Why a combination cannot be a hotkey, in words; '' when it can ('' itself is "off" and always fine).
export function comboProblem(value) {
    if (value === '') return '';
    const combo = parseCombo(value);
    if (!combo) return 'That key cannot be a hotkey.';
    const { modifiers, key } = combo;
    if (!QUIET_KEY.test(key) && !modifiers.some(name => name !== 'Shift')) return 'Add Ctrl, Alt or Cmd: without one the key would be taken from your typing.';
    const ctrl = modifiers.includes('Ctrl') || modifiers.includes('Meta');
    if (reserved.has(joined({ modifiers: ['Ctrl', 'Alt', 'Shift'].filter(name => (name === 'Ctrl' ? ctrl : modifiers.includes(name))), key }))) {
        return `${comboLabel(value)} belongs to the browser or to Agent Zero.`;
    }
    return '';
}

// The action (other than `id`) that already uses a combination, or ''. `hotkeys` is the setting; `second` the older `hotkey` one.
export function comboOwner(hotkeys, second, id, value) {
    const wanted = normalCombo(value);
    if (!wanted) return '';
    const rows = [...ACTIONS.map(action => [action.id, hotkeys?.[action.id]]), [SECOND_TOGGLE, second]];
    return rows.find(([other, combo]) => other !== id && normalCombo(combo) === wanted)?.[0] || '';
}
export function actionLabel(id) { return id === SECOND_TOGGLE ? 'Start or stop the conversation (second)' : ACTIONS.find(action => action.id === id)?.label || id; }

const KEY_LABELS = { ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→', Minus: '-', Equal: '=', BracketLeft: '[', BracketRight: ']',
    Backslash: '\\', Semicolon: ';', Quote: "'", Comma: ',', Period: '.', Slash: '/', Backquote: '`', PageUp: 'Page up', PageDown: 'Page down',
    MediaPlayPause: 'Play/pause key', MediaStop: 'Stop key', MediaTrackNext: 'Next track key', MediaTrackPrevious: 'Previous track key', ScrollLock: 'Scroll lock' };
export function onMac(nav = globalThis.navigator) { try { return /mac|iphone|ipad/i.test(String(nav?.userAgentData?.platform || nav?.platform || '')); } catch { return false; } }
// How a combination reads on this machine; an older hand-written one reads as it was written.
export function comboLabel(value, mac = onMac()) {
    const combo = parseCombo(value);
    if (!combo) return String(value || '');
    const names = { Meta: mac ? 'Cmd' : 'Win', Alt: mac ? 'Option' : 'Alt' };
    return [...combo.modifiers.map(name => names[name] || name), KEY_LABELS[combo.key] || combo.key.replace(/^Numpad/, 'Numpad ')].join('+');
}

// ---- events
// The key a press names, '' for a modifier or a key that cannot be used.
export function keyOf(event) {
    const code = String(event?.code || '');
    const plain = /^(?:Key([A-Z])|Digit([0-9]))$/.exec(code);
    const name = plain ? plain[1] || plain[2] : code;
    return KEY.test(name) && (plain || name.length > 1) ? name : '';
}
// AltGr types characters (and may report itself as Ctrl+Alt): never a hotkey.
export function altGraph(event) { try { return event?.getModifierState?.('AltGraph') === true; } catch { return false; } }
export function modifiersOf(event) { return MODIFIERS.filter(name => !!event?.[name.toLowerCase() + 'Key']); }
export function comboOf(event) { const key = keyOf(event); return key && !altGraph(event) ? joined({ modifiers: modifiersOf(event), key }) : ''; }

// The older `hotkey` setting is compared by the character the key types, as it always was; one recorded here also matches by position.
function secondMatches(value, event) {
    if (!value || typeof value !== 'string') return false;
    const parts = value.split('+'), key = parts.pop();
    if (MODIFIERS.some(name => !!event[name.toLowerCase() + 'Key'] !== parts.includes(name))) return false;
    return (key === 'Space' && event.code === 'Space') || (!!key && String(event.key ?? '').toLowerCase() === key.toLowerCase());
}
// The action a press means under these settings ({hotkeys, hotkey}), or ''.
export function actionFor(event, settings) {
    const combo = comboOf(event);
    const hotkeys = settings?.hotkeys && typeof settings.hotkeys === 'object' ? settings.hotkeys : {};
    const action = combo ? ACTIONS.find(({ id }) => normalCombo(hotkeys[id]) === combo && !comboProblem(hotkeys[id]))?.id : '';
    if (action) return action;
    const second = settings?.[SECOND_TOGGLE];
    return (combo && normalCombo(second) === combo) || (!altGraph(event) && secondMatches(second, event)) ? 'toggle_conversation' : '';
}
// A field whose typing must never be read as anything else (Agent Zero's own masked fields included).
function secretField(event) {
    let target = event?.target;
    try { target = event?.composedPath?.()[0] || target; } catch { /* the target stands */ }
    try { return !!target?.matches?.('input[type="password"],.masked-text-input'); } catch { return false; }
}

// ---- recording (Settings): while one combination is being recorded, no hotkey acts.
export const recording = { active: false };

// ---- DOM adapter: document-level, only while Convo is on. `store`: enabled, settings, hotkey(action) -> whether it did something now
// (only then is the press taken from the page) and pushToTalk(false) for the release of a held push-to-talk.
export function bindHotkeys({ store, document, window }) {
    const taken = new Set();              // codes of presses that were handled and are still down
    let held = null;                      // the push-to-talk press: {code, modifiers}
    const release = () => { if (held) { held = null; store.pushToTalk(false); } };
    const drop = () => { taken.clear(); release(); };
    const listeners = [];
    const on = (target, type, handler, capture = false) => { target.addEventListener(type, handler, capture); listeners.push([target, type, handler, capture]); };

    on(document, 'keydown', event => {
        if (event.repeat && taken.has(event.code)) { event.preventDefault(); return; }        // holding a handled key types nothing
        if (!store.enabled || recording.active || event.repeat || event.isComposing || event.defaultPrevented || secretField(event)) return;
        const action = actionFor(event, store.settings);
        if (!action || !store.hotkey(action)) return;
        event.preventDefault();
        taken.add(event.code);
        if (action === 'push_to_talk') held = { code: event.code, modifiers: modifiersOf(event) };
    });
    // Letting go of any part of the push-to-talk combination ends it (macOS reports no key up for the rest while Cmd is down).
    on(document, 'keyup', event => {
        if (taken.delete(event.code)) event.preventDefault();                                 // a focused button must not click for it
        if (held && (event.code === held.code || held.modifiers.includes({ Control: 'Ctrl', OS: 'Meta' }[event.key] || event.key))) release();
    }, true);
    on(window, 'blur', drop);
    on(document, 'visibilitychange', () => { if (document.hidden) drop(); });
    return { dispose() { for (const [target, type, handler, capture] of listeners.splice(0)) target.removeEventListener?.(type, handler, capture); drop(); } };
}
