import { ACTIONS, DEFAULT_HOTKEYS, PLANNING, SECOND_TOGGLE, actionLabel, altGraph, comboLabel, comboOf, comboOwner, comboProblem, keyOf, modifiersOf, recording } from '/plugins/_convo/webui/hotkeys.js';

// The Hotkeys card (hotkeys-field.html, Settings -> Conversation). Methods only (see compose in setup-store.js); `this` is the shared
// settings store, transient view state goes in this.ui.hotkeys: {recording (the row), pending (the modifiers held so far), notes}.
// A row's button records the next combination into the shared draft (saved by the one Save). Only a usable one that no other row has is
// taken; anything else is answered in words beside the row and the recording goes on. The older `hotkey` setting is one more row, and so
// is planning mode's (`planning_hotkey`).
const SECOND_DEFAULT = 'Ctrl+Shift+Space';
let release = null;                                        // ends the recording in progress (its listeners)

export const hotkeysSetup = {
    hotkeyRows() {
        if (!this.config) return [];
        const rows = ACTIONS.map(action => ({ ...action, hint: action.hint || '' }));
        rows.splice(1, 0, { id: SECOND_TOGGLE, label: actionLabel(SECOND_TOGGLE), hint: 'The shortcut from before there were more; both work.' });
        rows.push({ id: PLANNING, label: actionLabel(PLANNING), hint: 'While planning, nothing is started, changed or saved without your yes.' });
        return rows;
    },
    hotkeyValue(id) { return String((id === SECOND_TOGGLE || id === PLANNING ? this.config?.[id] : this.config?.hotkeys?.[id]) || ''); },
    hotkeyDefault(id) { return id === SECOND_TOGGLE ? SECOND_DEFAULT : DEFAULT_HOTKEYS[id] || ''; },
    hotkeyRecording(id) { return this.ui?.hotkeys?.recording === id; },
    // What the row's button reads: the combination, "Off", or what to do while it records.
    hotkeyText(id) {
        if (this.hotkeyRecording(id)) return this.ui.hotkeys.pending ? `${this.ui.hotkeys.pending}+…` : 'Press the keys…';
        return comboLabel(this.hotkeyValue(id)) || 'Off';
    },
    hotkeyName(id) {
        const label = actionLabel(id);
        return this.hotkeyRecording(id) ? `${label}: recording. Press the keys, Escape to cancel, Backspace to turn it off`
            : `${label}: ${comboLabel(this.hotkeyValue(id)) || 'off'}. Press to change`;
    },
    hotkeyNote(id) { return this.ui?.hotkeys?.notes?.[id] || ''; },
    hotkeySay(id, text) { this.ui.hotkeys.notes = { ...(this.ui.hotkeys.notes || {}), [id]: text }; },
    hotkeySet(id, value) {
        if (id === SECOND_TOGGLE || id === PLANNING) this.config[id] = value; else this.config.hotkeys = { ...this.config.hotkeys, [id]: value };
        this.hotkeySay(id, ''); this.message = '';
    },
    // Why a combination cannot be this row's, in words ('' when it can).
    hotkeyRefusal(id, value) {
        const problem = comboProblem(value);
        if (problem) return problem;
        const owner = comboOwner(this.config?.hotkeys, this.config?.hotkey, id, value, this.config?.planning_hotkey);
        return owner ? `${comboLabel(value)} is already used for “${actionLabel(owner)}”.` : '';
    },
    hotkeyReset(id) {
        if (this.hotkeyRecording(id)) this.hotkeyCancel();
        const value = this.hotkeyDefault(id), refusal = value ? this.hotkeyRefusal(id, value) : '';
        if (refusal) { this.hotkeySay(id, refusal); return false; }
        this.hotkeySet(id, value);
        return true;
    },
    hotkeyCancel() { release?.(); },
    // Records the next combination for a row. Listens on the window before anything else on the page, so the keys pressed meanwhile do
    // nothing else; a press elsewhere, Tab, Escape or leaving the page ends it without a change.
    hotkeyRecord(id, page = globalThis) {
        if (this.hotkeyRecording(id)) { this.hotkeyCancel(); return false; }
        this.hotkeyCancel();
        const ui = this.ui.hotkeys, listeners = [];
        const on = (type, handler) => { page.addEventListener?.(type, handler, true); listeners.push([type, handler]); };
        const end = () => {
            for (const [type, handler] of listeners.splice(0)) page.removeEventListener?.(type, handler, true);
            if (release === end) release = null;
            recording.active = false; ui.recording = ''; ui.pending = '';
        };
        // The press that ended it is still down: its release must not click the button again.
        const swallowRelease = code => {
            const handler = event => { if (event.code !== code) return; event.preventDefault(); event.stopImmediatePropagation(); page.removeEventListener?.('keyup', handler, true); };
            page.addEventListener?.('keyup', handler, true);
        };
        release = end; recording.active = true; ui.recording = id; ui.pending = ''; this.hotkeySay(id, '');
        on('keydown', event => {
            const modifiers = modifiersOf(event), bare = !modifiers.length;
            if (event.key === 'Tab' && modifiers.every(name => name === 'Shift')) { end(); return; }       // the keyboard is never trapped here
            event.preventDefault(); event.stopImmediatePropagation();
            if (event.repeat || event.isComposing) return;
            if (event.key === 'Escape') { end(); return; }
            if (bare && (event.key === 'Backspace' || event.key === 'Delete')) { end(); this.hotkeySet(id, ''); swallowRelease(event.code); return; }
            if (['Control', 'Alt', 'Shift', 'Meta', 'OS', 'AltGraph'].includes(event.key)) { ui.pending = comboLabel(modifiers.concat('A').join('+')).slice(0, -2); return; }
            const combo = comboOf(event);
            const refusal = altGraph(event) ? 'AltGr types characters on this keyboard, so it cannot be used.' : !keyOf(event) ? 'That key cannot be a hotkey.' : this.hotkeyRefusal(id, combo);
            ui.pending = '';
            if (refusal) { this.hotkeySay(id, refusal); return; }
            end(); this.hotkeySet(id, combo); swallowRelease(event.code);
        });
        on('keyup', event => {
            event.stopImmediatePropagation();
            const modifiers = modifiersOf(event);
            ui.pending = modifiers.length ? comboLabel(modifiers.concat('A').join('+')).slice(0, -2) : '';
        });
        on('pointerdown', event => { if (!event.target?.closest?.(`[data-hotkey="${id}"]`)) end(); });
        on('blur', event => { if (event.target === page || event.target === globalThis.document) end(); });
        return true;
    },
};
