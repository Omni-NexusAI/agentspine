// The configurable hotkeys: what a combination is and which can be used (hotkeys.js, held to convo_contract by fixtures/hotkey_cases.json,
// which test_settings_contract.py reads too), which press means which action, the document adapter (repeat, typing, password fields,
// recording, the release of push-to-talk), what each action does in the store, and the Settings card that records them.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { ACTIONS, DEFAULT_HOTKEYS, RESERVED, actionFor, bindHotkeys, comboLabel, comboOf, comboOwner, comboProblem, keyOf, normalCombo, parseCombo, recording } from '../webui/hotkeys.js';
import { defaults } from '../webui/defaults.js';
import { hostStubs, loadModule } from './vm-loader.mjs';
import { storeFixture } from './store-fixture.mjs';
import { settingsMarkup } from './settings-markup.mjs';

const plain = value => JSON.parse(JSON.stringify(value));
const cases = JSON.parse(await readFile(new URL('./fixtures/hotkey_cases.json', import.meta.url), 'utf8'));
const settle = (ms = 5) => new Promise(resolve => setTimeout(resolve, ms));
// A key event as a browser gives it. `mods`: 'Ctrl Alt Shift Meta' (any), `altgr`: the AltGraph state.
function press(code, mods = '', extra = {}) {
    const has = name => mods.split(' ').includes(name);
    return { code, key: extra.key ?? code.replace(/^(Key|Digit)/, '').toLowerCase(), ctrlKey: has('Ctrl'), altKey: has('Alt'), shiftKey: has('Shift'), metaKey: has('Meta'), repeat: false, isComposing: false,
        defaultPrevented: false, prevented: 0, stopped: 0, preventDefault() { this.prevented++; this.defaultPrevented = true; }, stopImmediatePropagation() { this.stopped++; },
        getModifierState: name => name === 'AltGraph' && !!extra.altgr, target: extra.target ?? { matches: () => false }, ...extra };
}

test('Every usable, unknown, unmodified and reserved combination is judged as the server judges it', () => {
    for (const value of cases.usable) { assert.equal(comboProblem(value), '', value); assert.ok(parseCombo(value), value); }
    for (const value of cases.unknown) { assert.equal(parseCombo(value), null, value); assert.match(comboProblem(value), /cannot be a hotkey/, value); }
    for (const value of cases.needs_modifier) assert.match(comboProblem(value), /Add Ctrl, Alt or Cmd/, value);
    for (const value of cases.reserved) assert.match(comboProblem(value), /belongs to the browser or to Agent Zero/, value);
    for (const [one, other] of cases.same) assert.equal(normalCombo(one), normalCombo(other));
    for (const value of RESERVED) assert.notEqual(comboProblem(value), '', value);
    assert.equal(comboProblem(''), '');
    for (const value of [null, undefined, 7, {}, 'Alt+' + 'M'.repeat(60)]) assert.equal(parseCombo(value), null);
});

test('The defaults are few, usable, free of conflicts and the same everywhere they are written', () => {
    assert.deepEqual(Object.keys(DEFAULT_HOTKEYS), ACTIONS.map(action => action.id));
    assert.deepEqual(defaults.hotkeys, DEFAULT_HOTKEYS);
    const used = Object.entries(DEFAULT_HOTKEYS).filter(([, value]) => value);
    assert.deepEqual(used.map(([id]) => id), ['toggle_conversation', 'mute', 'push_to_talk', 'dictate', 'stop_reply']);
    for (const [id, value] of used) { assert.equal(comboProblem(value), '', id); assert.equal(comboOwner(DEFAULT_HOTKEYS, defaults.hotkey, id, value), '', id); }
    assert.equal(comboOwner(DEFAULT_HOTKEYS, defaults.hotkey, 'hotkey', defaults.hotkey), '');
    assert.equal(comboOwner(DEFAULT_HOTKEYS, defaults.hotkey, 'mute', 'Shift+Alt+C'), 'toggle_conversation');
    assert.equal(comboOwner(DEFAULT_HOTKEYS, defaults.hotkey, 'mute', 'Ctrl+Shift+Space'), 'hotkey');           // the older setting counts
});

test('A press is read by its place on the keyboard, never by the character a layout gives it', () => {
    assert.equal(comboOf(press('KeyC', 'Alt Shift', { key: 'Ç' })), 'Alt+Shift+C');
    assert.equal(comboOf(press('Digit1', 'Ctrl Alt', { key: '¡' })), 'Ctrl+Alt+1');
    assert.equal(comboOf(press('Space', 'Alt Shift')), 'Alt+Shift+Space');
    assert.equal(comboOf(press('F8')), 'F8');
    for (const code of ['ShiftLeft', 'AltRight', 'Escape', 'Enter', 'Tab', 'Backspace', 'Delete', '', 'AudioVolumeUp']) assert.equal(keyOf(press(code, 'Alt')), '', code);
    assert.equal(comboOf(press('KeyQ', 'Ctrl Alt', { altgr: true })), '');                      // AltGr types characters
    assert.equal(comboOf({}), ''); assert.equal(comboOf(null), '');
    assert.equal(comboLabel('Meta+Alt+Comma', true), 'Option+Cmd+,');
    assert.equal(comboLabel('Meta+Alt+Comma', false), 'Alt+Win+,');
    assert.equal(comboLabel('Alt+Numpad5', false), 'Alt+Numpad 5');
});

test('A press means the action it is set for; the older shortcut still starts and stops, by character or by place', () => {
    const settings = { hotkeys: { ...DEFAULT_HOTKEYS, run_action: 'Alt+Y' }, hotkey: 'Ctrl+Shift+Space' };
    assert.equal(actionFor(press('KeyM', 'Alt Shift'), settings), 'mute');
    assert.equal(actionFor(press('KeyY', 'Alt'), settings), 'run_action');
    assert.equal(actionFor(press('Space', 'Ctrl Shift'), settings), 'toggle_conversation');     // the older one
    assert.equal(actionFor(press('KeyK', 'Ctrl Alt', { key: 'k' }), { hotkey: 'Ctrl+Alt+K' }), 'toggle_conversation');
    assert.equal(actionFor(press('Comma', 'Alt', { key: 'm' }), { hotkey: 'Alt+Comma' }), 'toggle_conversation');      // recorded by place
    for (const [code, mods] of [['KeyM', 'Alt'], ['KeyM', 'Shift'], ['KeyM', 'Alt Shift Ctrl'], ['KeyM', ''], ['Space', 'Alt']]) assert.equal(actionFor(press(code, mods), settings), '', mods + code);
    assert.equal(actionFor(press('KeyC', 'Alt Shift'), { hotkeys: { toggle_conversation: '' }, hotkey: '' }), '');
    for (const broken of [null, {}, { hotkeys: 'x' }, { hotkeys: { mute: 'Ctrl+W', dictate: 7 } }]) {
        assert.equal(actionFor(press('KeyW', 'Ctrl'), broken), '');                             // a reserved one never acts, however it got there
        assert.equal(actionFor(press('KeyM', 'Alt Shift'), broken), '');
    }
});

// ---- the document adapter
function adapter({ enabled = true, settings = { hotkeys: DEFAULT_HOTKEYS, hotkey: 'Ctrl+Shift+Space' }, does = () => true } = {}) {
    const listeners = new Map(), log = [];
    const target = name => ({ addEventListener(type, fn, capture) { listeners.set(`${name}:${type}`, { fn, capture }); }, removeEventListener(type) { listeners.delete(`${name}:${type}`); } });
    const document = { hidden: false, ...target('document') }, window = target('window');
    const store = { enabled, settings, hotkey(action) { log.push(action); return does(action); }, pushToTalk(value) { log.push('talk:' + value); } };
    const bound = bindHotkeys({ store, document, window });
    const fire = (name, event = {}) => { listeners.get(name)?.fn(event); return event; };
    return { store, log, document, bound, listeners, fire, down: (...args) => fire('document:keydown', press(...args)), up: (...args) => fire('document:keyup', press(...args)) };
}

test('A hotkey acts while Convo is on, also while typing; a press that did nothing is left to the page', () => {
    const a = adapter();
    const composer = { matches: selector => selector === 'textarea' };
    assert.equal(a.down('KeyC', 'Alt Shift', { target: composer }).prevented, 1);
    assert.equal(a.down('KeyX', 'Alt Shift').prevented, 0);
    assert.deepEqual(a.log, ['toggle_conversation']);
    a.store.enabled = false;
    assert.equal(a.down('KeyD', 'Alt Shift').prevented, 0);
    a.store.enabled = true;
    const idle = adapter({ does: () => false });                                                // nothing to mute: the page keeps the press
    assert.equal(idle.down('KeyM', 'Alt Shift').prevented, 0);
    assert.deepEqual(idle.log, ['mute']);
    for (const extra of [{ isComposing: true }, { defaultPrevented: true }]) { a.log.length = 0; a.down('KeyD', 'Alt Shift', extra); assert.deepEqual(a.log, []); }
    a.bound.dispose();
    assert.equal(a.listeners.size, 0);
});

test('Holding a key repeats nothing and types nothing; its release does not click what has the focus', () => {
    const a = adapter();
    a.down('KeyS', 'Alt Shift');
    const repeat = a.down('KeyS', 'Alt Shift', { repeat: true });
    assert.equal(repeat.prevented, 1);                                                          // taken, but nothing is done again
    assert.deepEqual(a.log, ['stop_reply']);
    assert.equal(a.down('KeyA', '', { repeat: true }).prevented, 0);                            // someone else's repeat is left alone
    assert.equal(a.up('KeyS').prevented, 1);
    assert.equal(a.up('KeyS').prevented, 0);
    assert.equal(a.down('KeyD', 'Alt Shift', { repeat: true }).prevented, 0);                   // a repeat never starts anything
    assert.deepEqual(a.log, ['stop_reply']);
});

test('Nothing acts in a password field, or while a combination is being recorded', () => {
    const a = adapter();
    const secret = { matches: selector => /password/.test(selector) };
    assert.equal(a.down('KeyC', 'Alt Shift', { target: secret }).prevented, 0);
    assert.equal(a.down('KeyC', 'Alt Shift', { target: { matches: () => false }, composedPath: () => [secret] }).prevented, 0);   // inside a component
    recording.active = true;
    try { a.down('KeyC', 'Alt Shift'); } finally { recording.active = false; }
    assert.deepEqual(a.log, []);
    a.down('KeyC', 'Alt Shift', { target: null });
    assert.deepEqual(a.log, ['toggle_conversation']);
});

test('Push to talk ends when any part of it is let go of, or the page loses the focus or is hidden', () => {
    const endings = [a => a.up('Space'), a => a.up('AltLeft', '', { key: 'Alt' }), a => a.up('ShiftRight', '', { key: 'Shift' }), a => a.fire('window:blur'),
        a => { a.document.hidden = true; a.fire('document:visibilitychange'); }, a => a.bound.dispose()];
    for (const end of endings) {
        const a = adapter();
        a.down('Space', 'Alt Shift');
        a.up('KeyQ'); a.up('ControlLeft', '', { key: 'Control' });                              // other keys do not end it
        a.document.hidden = false; a.fire('document:visibilitychange');
        assert.deepEqual(a.log, ['push_to_talk']);
        end(a); end(a);
        assert.deepEqual(a.log, ['push_to_talk', 'talk:false'], String(end));                   // once
    }
    const ignored = adapter({ does: () => false });                                             // not muted: nothing is held
    ignored.down('Space', 'Alt Shift'); ignored.up('Space');
    assert.deepEqual(ignored.log, ['push_to_talk']);
});

// ---- what the actions do (the real store)
test('Each action does its one thing, and says when there was nothing to do', async () => {
    const opened = [];
    const f = await storeFixture({ settings: { hotkeys: DEFAULT_HOTKEYS }, globals: { Alpine: { store: name => (name === 'rightCanvas' ? { open: async id => { opened.push(id); } } : null) } } });
    await f.store.refresh();
    for (const idle of ['mute', 'push_to_talk', 'stop_reply', 'run_action', 'dismiss_action', 'unknown']) assert.equal(f.store.hotkey(idle), false, idle);
    assert.equal(f.store.hotkey('open_panel'), true);
    await settle();
    assert.deepEqual(opened, ['convo']);
    assert.equal(f.store.hotkey('toggle_conversation'), true);
    await settle(20);
    assert.equal(f.store.active, true);
    assert.equal(f.store.hotkey('stop_reply'), false);                                          // listening: no reply to stop
    const send = event => f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, ...event } });
    send({ type: 'action.pending', id: 'q', label: 'Save it?', seconds: 30 });
    assert.equal(f.store.hotkey('run_action'), true);
    assert.equal(f.store.hotkey('run_action'), false);                                          // answered already: nothing is sent twice
    await settle();
    assert.deepEqual(f.requests.filter(item => item.event === 'convo.action').map(item => [item.data.id, item.data.run]), [['q', true]]);
    send({ type: 'action.pending', id: 'r', label: 'Delete it?', seconds: 30 });
    assert.equal(f.store.hotkey('dismiss_action'), true);
    await settle();
    assert.deepEqual(f.requests.filter(item => item.event === 'convo.action').map(item => [item.data.id, item.data.run]).at(-1), ['r', false]);
    send({ type: 'state', state: 'speaking' });
    assert.equal(f.store.hotkey('stop_reply'), true);
    await settle();
    assert.equal(f.calls.filter(name => name === 'convo.interrupt').length, 1);
    assert.deepEqual([f.store.active, f.store.state], [true, 'listening']);                     // the conversation goes on
    assert.ok(f.audios[0].interrupted >= 1);
    assert.equal(f.store.hotkey('toggle_conversation'), true);
    await settle(20);
    assert.equal(f.store.active, false);
    assert.equal(f.store.hotkey('dictate'), true);
    await settle(20);
    assert.deepEqual([f.store.dictating, f.store.dictationMode], [true, 'latched']);            // hands-free, as the orb's latched dictation
    assert.equal(f.store.hotkey('dictate'), true);                                              // and the same key finishes it
    await settle(20);
    assert.equal(f.store.dictating, false);
});

test('Stopping a reply that is already over does not end the conversation; a lost connection still does', async () => {
    let refuse = 'stale';
    const f = await storeFixture({ answer: event => (event === 'convo.interrupt' ? (refuse === 'stale' ? { ok: false, stale: true } : new Error('Gone')) : null) });
    await f.store.start();
    f.store.state = 'speaking';
    await f.store.interrupt();
    assert.deepEqual([f.store.active, f.store.error], [true, '']);
    refuse = 'gone';
    await f.store.interrupt();
    assert.deepEqual([f.store.active, f.store.error], [false, 'Gone']);
});

// ---- the Settings card
async function card({ config = plain(defaults) } = {}) {
    const bindings = hostStubs({ '/js/api.js': { callJsonApi: async (_url, payload) => ({ ok: true, config: payload.config ? plain(payload.config) : config, revision: 'r', readiness: [], credentials: {} }) } });
    const { namespace } = await loadModule('webui/setup-store.js', { bindings, globals: { window: { confirm: () => true } } });
    await namespace.store.load();
    const listeners = [];
    const page = { addEventListener: (type, fn) => listeners.push([type, fn]), removeEventListener: (type, fn) => { const i = listeners.findIndex(([t, f]) => t === type && f === fn); if (i >= 0) listeners.splice(i, 1); } };
    const fire = (type, event) => { for (const [name, fn] of [...listeners]) if (name === type) fn(event); return event; };
    return { store: namespace.store, page, listeners, down: (...args) => fire('keydown', press(...args)), up: (...args) => fire('keyup', press(...args)), fire,
        record: id => namespace.store.hotkeyRecord(id, page) };
}

test('A row records the next combination into the draft, and nothing else on the page sees the keys', async () => {
    const f = await card();
    assert.deepEqual(plain(f.store.hotkeyRows().map(row => row.id)), ['toggle_conversation', 'hotkey', ...ACTIONS.slice(1).map(action => action.id), 'planning_hotkey']);
    assert.equal(f.store.hotkeyText('mute'), 'Alt+Shift+M');
    assert.equal(f.store.hotkeyText('open_panel'), 'Off');
    assert.equal(f.record('open_panel'), true);
    assert.deepEqual([f.store.hotkeyRecording('open_panel'), f.store.hotkeyText('open_panel')], [true, 'Press the keys…']);
    f.down('AltLeft', 'Alt', { key: 'Alt' });
    assert.equal(f.store.hotkeyText('open_panel'), 'Alt+…');
    const taken = f.down('KeyO', 'Alt');
    assert.deepEqual([taken.prevented, taken.stopped], [1, 1]);
    assert.deepEqual([f.store.config.hotkeys.open_panel, f.store.hotkeyRecording('open_panel'), f.store.dirty], ['Alt+O', false, true]);
    assert.equal(f.up('KeyO').prevented, 1);                                                    // its release does not press the button again
    assert.deepEqual(f.listeners, []);
    assert.equal(f.store.config.hotkeys.mute, 'Alt+Shift+M');                                   // the others are kept
    f.record('hotkey'); f.down('KeyK', 'Ctrl Alt');
    assert.equal(f.store.config.hotkey, 'Ctrl+Alt+K');                                          // the older setting has its own row
});

test('A combination that cannot be used, or that another row has, is refused in words and the recording goes on', async () => {
    const f = await card();
    f.record('run_action');
    for (const [event, words] of [[['KeyM', 'Alt Shift'], /Alt\+Shift\+M is already used for “Mute or unmute the microphone”\./], [['Space', 'Ctrl Shift'], /already used for “Start or stop the conversation \(second\)”/],
        [['KeyW', 'Ctrl'], /belongs to the browser or to Agent Zero/], [['KeyM', 'Shift'], /Add Ctrl, Alt or Cmd/], [['Enter', 'Alt', { key: 'Enter' }], /cannot be a hotkey/], [['KeyQ', '', { altgr: true }], /AltGr/]]) {
        f.down(...event);
        assert.match(f.store.hotkeyNote('run_action'), words);
        assert.equal(f.store.hotkeyRecording('run_action'), true);
    }
    assert.equal(f.store.config.hotkeys.run_action, '');
    f.down('KeyY', 'Alt');
    assert.deepEqual([f.store.config.hotkeys.run_action, f.store.hotkeyNote('run_action')], ['Alt+Y', '']);
});

test('Escape keeps the old keys, Backspace and Delete turn it off, Tab and a press elsewhere leave; Reset brings the default back', async () => {
    const f = await card();
    f.record('mute');
    const escape = f.down('Escape', '', { key: 'Escape' });
    assert.equal(escape.stopped, 1);                                                            // the settings window behind does not close
    assert.deepEqual([f.store.config.hotkeys.mute, f.store.hotkeyRecording('mute'), f.store.dirty], ['Alt+Shift+M', false, false]);
    for (const key of ['Backspace', 'Delete']) {
        f.record('mute'); f.down(key, '', { key }); f.up(key);
        assert.equal(f.store.config.hotkeys.mute, '', key);
        assert.equal(f.store.hotkeyReset('mute'), true);
        assert.equal(f.store.config.hotkeys.mute, 'Alt+Shift+M');
    }
    f.record('mute');
    assert.equal(f.down('Tab', '', { key: 'Tab' }).prevented, 0);                               // the keyboard is never trapped
    assert.equal(f.store.hotkeyRecording('mute'), false);
    f.record('mute'); f.fire('pointerdown', { target: { closest: () => ({}) } });
    assert.equal(f.store.hotkeyRecording('mute'), true);                                        // its own button
    f.fire('pointerdown', { target: { closest: () => null } });
    assert.equal(f.store.hotkeyRecording('mute'), false);
    f.record('mute'); f.record('dictate');                                                      // one at a time
    assert.deepEqual([f.store.hotkeyRecording('mute'), f.store.hotkeyRecording('dictate')], [false, true]);
    f.record('dictate');                                                                        // the same button again ends it
    assert.equal(f.store.hotkeyRecording('dictate'), false);
    f.record('dictate'); f.fire('blur', { target: f.page });
    assert.equal(f.store.hotkeyRecording('dictate'), false);
    assert.deepEqual(f.listeners, []);
    f.record('open_panel'); f.down('KeyM', 'Alt Shift');                                        // refused: in use
    f.store.hotkeyCancel();
    f.record('mute'); f.down('KeyP', 'Alt'); f.record('open_panel'); f.down('KeyM', 'Alt Shift');
    assert.equal(f.store.hotkeyReset('mute'), false);                                           // its default is another row's now: said, not taken
    assert.match(f.store.hotkeyNote('mute'), /already used for “Open the Convo panel”/);
});

test('A draft with other hotkeys is saved with the other settings, and a configuration from before the setting gets the defaults', async () => {
    const older = plain(defaults); delete older.hotkeys;
    const f = await card({ config: older });
    assert.deepEqual([plain(f.store.config.hotkeys), f.store.dirty], [DEFAULT_HOTKEYS, false]);
    f.record('open_settings'); f.down('F9');
    assert.equal(await f.store.save(), true);
    assert.equal(f.store.config.hotkeys.open_settings, 'F9');
});

test('The card: one row per action with a recording button, a Reset and its note; it says when hotkeys work', async () => {
    const html = await readFile(new URL('../webui/hotkeys-field.html', import.meta.url), 'utf8');
    assert.match(html, /<fieldset class="convo-card convo-hotkeys"[^>]*><legend>Hotkeys<\/legend>/);
    assert.match(html, /Hotkeys work while the Agent Zero page is focused/);
    assert.match(html, /:aria-label="\$store\.convoSetup\.hotkeyName\(row\.id\)" @click="\$store\.convoSetup\.hotkeyRecord\(row\.id\)"/);
    assert.match(html, /@click="\$store\.convoSetup\.hotkeyReset\(row\.id\)">Reset</);
    assert.match(html, /<p class="convo-hotkey-note" role="status"/);
    assert.match(html, /x-destroy="\$store\.convoSetup\?\.hotkeyCancel\(\)"/);                  // closing Settings ends a recording
    const shell = await readFile(new URL('../webui/settings-controls.html', import.meta.url), 'utf8');
    const section = shell.slice(shell.indexOf("section === 'Conversation'"), shell.indexOf("section === 'Audio & Video'"));
    assert.match(section, /hotkeys-field\.html/);
    assert.doesNotMatch(await settingsMarkup(), /Focused-app shortcut/);                        // the older setting is a row of this card now
});
