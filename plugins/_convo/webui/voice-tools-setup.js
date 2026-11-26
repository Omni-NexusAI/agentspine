import { callJsonApi } from '/js/api.js';

// "What the voice agent may do" (the Conversation section of settings-controls.html). Methods only (see compose in setup-store.js);
// `this` is the shared settings store, transient view state goes in this.ui.voiceTools. The checkboxes bind `voice_tools.*` on the
// shared draft like every other setting; this file holds only the search health line under "Search the web and read pages".
//
// `search_health` answers from the server's memory and never sends a search: it is what showing the section asks. `search_check`
// sends one small search and runs only on the press of "Check search".
const endpoint = '/plugins/_convo/conversation';
export const VOICE_TOOL_GROUPS = ['web', 'memory', 'tasks', 'system', 'timers', 'chats', 'senses', 'interface'];
export const SEARCH_STATES = {
    unknown: 'Search has not been used since Agent Zero started.',
    ok: 'Search is answering.',
    limited: 'Some search engines are refusing; others answer.',
    blocked: 'Every search engine asked is refusing right now.',
};

// "Checked at 14:05." for today, with the day for an older answer; '' when the server gave no time (or one that cannot be read).
export function checkedText(checkedAt, now = new Date()) {
    if (typeof checkedAt !== 'string' || !checkedAt) return '';
    const at = new Date(checkedAt);
    if (Number.isNaN(at.getTime())) return '';
    try {
        const time = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        if (at.toDateString() === now.toDateString()) return `Checked at ${time}.`;
        return `Checked on ${at.toLocaleDateString([], { day: 'numeric', month: 'short' })} at ${time}.`;
    } catch { return ''; }
}

// The one status line, from the view state: what is being done, a plain failure, or the last known state with its time.
export function searchHealthLine(view = {}, now = new Date()) {
    if (view.busy) return 'Checking search…';
    if (view.failure) return view.failure;
    const words = SEARCH_STATES[view.state];
    if (!words) return view.reading ? 'Reading the search state…' : '';
    const checked = checkedText(view.checked_at, now);
    return checked ? `${words} ${checked}` : words;
}

export const voiceToolsSetup = {
    // The section that is showing (the shell calls this when it mounts and whenever the section changes).
    searchHealthShown(section) { if (section === 'Conversation') void this.searchHealthRead(); },
    // The last known state, from memory. Never `search_check`. An answer that arrives after a newer request is dropped.
    async searchHealthRead() {
        const ui = this.ui.voiceTools;
        if (ui.busy) return false;
        const id = ui.asked = (ui.asked || 0) + 1;
        ui.reading = true;
        try {
            const result = await callJsonApi(endpoint, { action: 'search_health' });
            if (id !== ui.asked) return false;
            if (!SEARCH_STATES[result?.state]) throw new Error('unreadable');
            this.searchHealthTake(result);
            return true;
        } catch {
            if (id === ui.asked && !ui.state) ui.failure = 'The search state could not be read.';   // a state already shown stays
            return false;
        } finally { if (id === ui.asked) ui.reading = false; }
    },
    // "Check search": one small search now, only on this press.
    async searchCheck() {
        const ui = this.ui.voiceTools;
        if (ui.busy) return false;
        const id = ui.asked = (ui.asked || 0) + 1;
        ui.busy = true; ui.failure = '';
        try {
            const result = await callJsonApi(endpoint, { action: 'search_check' });
            if (!SEARCH_STATES[result?.state]) throw new Error('unreadable');
            if (id === ui.asked) this.searchHealthTake(result);
            return true;
        } catch {
            if (id === ui.asked) ui.failure = 'The check did not finish, so the search state is not known. Try again in a moment.';
            return false;
        } finally { ui.busy = false; ui.reading = false; }
    },
    searchHealthTake(result) {
        const ui = this.ui.voiceTools;
        ui.state = result.state; ui.checked_at = typeof result.checked_at === 'string' ? result.checked_at : ''; ui.failure = '';
    },
    searchHealthText() { return searchHealthLine(this.ui?.voiceTools || {}); },
};
