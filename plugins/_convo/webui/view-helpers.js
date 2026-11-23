// Pure view helpers for the Convo panel. No imports and no host access, so the store, the markup and the node
// tests share one definition.

export const STICK_PX = 48;   // within this distance of the bottom the transcript keeps following new messages
export const PAGE = 60;       // journal events fetched per history page

export function nearBottom(element, threshold = STICK_PX) {
    return !element || element.scrollHeight - element.clientHeight - element.scrollTop <= threshold;
}
export function toBottom(element) { if (element) element.scrollTop = element.scrollHeight; }
export function captureAnchor(element) { return element ? { height: element.scrollHeight, top: element.scrollTop } : null; }
// After older messages were added above the viewport, keep what the reader was looking at where it was.
export function restoreAnchor(element, anchor) {
    if (element && anchor) element.scrollTop = anchor.top + (element.scrollHeight - anchor.height);
}

// Journal events by sequence: ascending, one entry per sequence number, later copies win.
export function mergeEvents(existing, incoming) {
    const known = new Map();
    for (const event of existing) known.set(event.seq, event);
    for (const event of incoming) known.set(event.seq, event);
    return [...known.values()].sort((a, b) => a.seq - b.seq);
}

export const ACTIVE_JOB = new Set(['queued', 'dispatching', 'running', 'cancel_requested']);

// Journal facts that are bookkeeping rather than conversation; developer mode still shows them.
export const HIDDEN_KINDS = new Set(['context.compacted', 'assistant.generated', 'assistant.playback_started', 'user.transcript',
    'job.dispatching', 'job.running', 'job.cancel_requested', 'job.notice_claimed', 'tool.requested']);

const TOOL_LABELS = { web_search: 'Web search', memory_read: 'Memory search', delegate_task: 'Task', camera_context: 'Camera look',
    recall_conversation: 'Conversation lookup' };
const REASONS = { interrupted: 'Reply interrupted', stopped: 'Conversation stopped', retargeted: 'Voice moved to another chat' };
const JOB_END = { 'job.completed': 'Background task completed', 'job.failed': 'Background task failed',
    'job.cancelled': 'Background task cancelled', 'job.uncertain': 'Background task outcome unconfirmed' };

export function clip(value, limit) {
    const text = String(value ?? '').trim();
    return text.length > limit ? text.slice(0, limit - 1).trimEnd() + '…' : text;
}

// ---- one "You" and one "Convo" entry per turn
// The journal writes a turn as several facts: user.accepted, user.transcript (from the conversation model's own transcript line or
// Whisper), assistant.delivered once per phrase heard, assistant.generated once for the whole reply, playback starts, and
// turn.interrupted. The panel shows them folded: the user's entry at the accepted event (its newest transcript, or the audio
// placeholder), the assistant's at its first delivered/generated event with the delivered phrases joined as the server's history joins
// them (each is spoken trimmed: one space between two that do not already meet at a space). A reply nothing of which was delivered is never shown as speech; in developer mode it is listed as generated
// only. Everything else (jobs, segments, interruptions) stays one entry per event. Events without a turn are never folded together.
const TURN_KINDS = new Set(['user.accepted', 'user.transcript', 'assistant.delivered', 'assistant.generated', 'assistant.playback_started', 'turn.held']);
const NOT_ANSWERED = { backchannel: 'an acknowledgement', for_someone_else: 'talk to someone else', waiting_for_more: 'waiting for you to finish' };
// `turn.held` {decision: 'silent'|'wait', reason}: the assistant chose to stay silent, or waits for the user to finish.
export const heldText = held => (held?.decision === 'wait' ? '· waiting for you to finish'
    : `· stayed silent${held?.reason ? ': ' + clip(String(held.reason).replaceAll('_', ' '), 80) : ''}`);
export const notAnsweredText = reason => `Not answered: ${NOT_ANSWERED[reason] || clip(String(reason || '').replaceAll('_', ' '), 60) || 'no reason given'}`;
const squash = text => String(text ?? '').replace(/\s+/g, ' ').trim();
const joinPhrases = phrases => phrases.reduce((text, phrase) => (text && phrase && !/\s$/.test(text) && !/^\s/.test(phrase) ? text + ' ' + phrase : text + phrase), '');
const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;

// `events`: the journal page in sequence order. `developer`: add a secondary line (phrases heard, the unheard remainder, why the
// user was not answered) and keep bookkeeping events. `notes`: not-answered decisions [{turn, reason, at}] (an ignored utterance
// is not journaled), shown in developer mode on its "You" entry or, without one, as an entry of its own placed by time.
// Entries: a folded turn entry {key, seq, kind: 'turn.user'|'turn.assistant'|'turn.note', role, label, text, detail}, or the raw
// event itself. `key` is the sequence number of the event an entry is placed at.
// `assistantName`: the assistant's name for its entries (the history's `assistant_name`; "Convo" when empty). `diagnostics`: show the
// bookkeeping rows (HIDDEN_KINDS); `rejected`: show generated text that was never delivered. The panel passes both only in developer
// mode with the settings show_diagnostic_events / show_rejected_responses on; called without them they follow `developer`.
export function foldTurns(events = [], { developer = false, notes = [], assistantName = '', diagnostics = developer, rejected = developer } = {}) {
    const name = clip(assistantName, 64) || 'Convo';
    const groupOf = event => (event.turn ? `${event.session ?? ''}:${event.turn}` : `seq:${event.seq}`);
    const groups = new Map();
    let lastAccepted = -Infinity;
    for (const event of events) {
        const payload = event.payload || {};
        if (event.kind === 'user.accepted') lastAccepted = Math.max(lastAccepted, event.seq);
        if (!TURN_KINDS.has(event.kind) && event.kind !== 'turn.interrupted') continue;
        const id = groupOf(event);
        const group = groups.get(id) || { turn: event.turn, accepted: null, transcript: '', source: '', phrases: [], generated: '', anchor: null, interrupted: false, held: null };
        groups.set(id, group);
        if (event.kind === 'user.accepted' && !group.accepted) group.accepted = event;
        if (event.kind === 'user.transcript' && payload.text) { group.transcript = String(payload.text); group.source = String(payload.source || ''); }
        if (event.kind === 'assistant.delivered' && payload.text) group.phrases.push(String(payload.text));
        if (event.kind === 'assistant.generated' && payload.text) group.generated = String(payload.text);
        if ((event.kind === 'assistant.delivered' || event.kind === 'assistant.generated') && group.anchor === null) group.anchor = event.seq;
        if (event.kind === 'turn.interrupted') group.interrupted = true;
        if (event.kind === 'turn.held' && !group.held) group.held = { seq: event.seq, time: event.time, session: event.session, decision: payload.decision, reason: payload.reason, text: payload.transcript || payload.text || '' };
    }
    const reasons = new Map((notes || []).filter(note => note?.turn).map(note => [note.turn, note]));
    const placed = new Set();
    const user = (group, event) => {
        const note = developer ? reasons.get(group.turn) : null;
        if (note) placed.add(note.turn);
        const transcript = group.transcript || event.payload?.transcript || '';
        const source = { whisper: 'Transcript: Whisper', model: 'Transcript: the conversation model' }[group.source] || '';
        const held = developer && group.held ? heldText(group.held) : '';
        const detail = developer ? [source, note ? notAnsweredText(note.reason) : '', held].filter(Boolean).join(' · ') : '';
        return { key: event.seq, seq: event.seq, time: event.time, kind: 'turn.user', session: event.session, turn: event.turn, role: 'user', label: 'You',
                 text: transcript || '[Audio input; no verbatim transcript]', detail };
    };
    // A held turn without an accepted event: the user's words still show; the note only in developer mode.
    const heldOnly = group => {
        const text = group.transcript || group.held.text;
        if (!text && !developer) return null;
        return { key: group.held.seq, seq: group.held.seq, time: group.held.time, kind: 'turn.user', session: group.held.session, turn: group.turn, role: 'user', label: 'You',
                 text: text || '', detail: developer ? heldText(group.held) : '' };
    };
    const assistant = (group, event) => {
        const heard = joinPhrases(group.phrases);
        if (!heard && !(rejected && group.generated)) return null;      // nothing delivered: never shown as speech
        const said = squash(heard), written = squash(group.generated);
        const later = lastAccepted > (group.accepted?.seq ?? group.anchor);
        const interrupted = !!heard && (group.interrupted || (later && said.length < written.length));
        let detail = '';
        if (developer) {
            const remainder = rejected && written.length > said.length ? (written.startsWith(said) ? `not heard: “${clip(written.slice(said.length), 160)}”`
                                                                                        : `${written.length - said.length} more characters generated than heard`) : '';
            detail = heard ? [plural(group.phrases.length, 'phrase') + ' heard', remainder].filter(Boolean).join(' · ') : 'Generated, never delivered: nothing of it was heard';
        }
        return { key: event.seq, seq: event.seq, time: event.time, kind: 'turn.assistant', session: event.session, turn: group.turn, role: 'assistant',
                 label: heard ? (interrupted ? `${name} · interrupted` : name) : `${name} · generated, not delivered`, text: heard || group.generated, detail };
    };
    const entries = [];
    for (const event of events) {
        if (TURN_KINDS.has(event.kind)) {
            const group = groups.get(groupOf(event));
            if (event.kind === 'user.accepted' && group.accepted === event) entries.push(user(group, event));
            else if (event.kind === 'turn.held' && !group.accepted && group.held?.seq === event.seq) { const entry = heldOnly(group); if (entry) entries.push(entry); }
            else if (event.seq === group.anchor) { const entry = assistant(group, event); if (entry) entries.push(entry); }
            continue;
        }
        if (diagnostics || !HIDDEN_KINDS.has(event.kind)) entries.push(event);
    }
    if (!developer) return entries;
    // Not-answered utterances with no journal entry of their own: placed by time among the events (the end when times are unknown).
    for (const note of (notes || []).filter(note => note?.turn && !placed.has(note.turn))) {
        const entry = { key: `n:${note.turn}`, kind: 'turn.note', turn: note.turn, role: 'user', label: 'You · not answered', text: '', detail: notAnsweredText(note.reason) };
        const index = Number.isFinite(note.at) ? entries.findIndex(item => Number.isFinite(item.time) && item.time > note.at) : -1;
        if (index < 0) entries.push(entry); else entries.splice(index, 0, entry);
    }
    return entries;
}

// How one journal event reads in the transcript. Only real journal facts are described; nothing is inferred.
export function describeEvent(event, jobs = []) {
    const payload = event.payload || {};
    const job = jobs.find(item => item.id === payload.job_id);
    switch (event.kind) {
        case 'turn.user': case 'turn.assistant': case 'turn.note':      // already folded (foldTurns)
            return { role: event.role, label: event.label, text: event.text || '' };
        case 'user.accepted':
            return { role: 'user', label: 'You', text: payload.transcript || '[Audio input; no verbatim transcript]' };
        case 'assistant.delivered':
            return { role: 'assistant', label: 'Convo', text: payload.text || '' };
        case 'assistant.generated':
            return { role: 'assistant', label: 'Convo · generated, not delivered', text: payload.text || '' };
        case 'user.transcript':
            return { role: 'user', label: 'You · transcript', text: payload.text || '' };
        case 'job.queued':
            return { role: 'activity', label: 'Background task queued', text: clip(job?.prompt, 240) };
        case 'turn.interrupted':
            return { role: 'activity', label: REASONS[payload.reason] || 'Turn ended early', text: '' };
        case 'segment.closed':
            return { role: 'activity', label: 'Voice moved to another chat', text: '' };
        case 'segment.opened':
            return { role: 'activity', label: 'Voice joined this chat', text: '' };
        case 'tool.done': case 'tool.failed':       // a tool the conversation model used (its name and words; the result stays in the journal)
            return { role: 'activity', label: (TOOL_LABELS[payload.name] || 'Tool') + (event.kind === 'tool.failed' ? ' · failed' : ''),
                     text: clip(payload.query, 240) };
        default:
            if (JOB_END[event.kind]) return { role: 'activity', label: JOB_END[event.kind], text: clip(payload.result || job?.result, 400) };
            return { role: event.kind.startsWith('user.') ? 'user' : event.kind.startsWith('assistant.') ? 'assistant' : 'activity',
                     label: event.kind.replaceAll('.', ' '), text: payload.text || payload.reason || payload.result || payload.job_id || '' };
    }
}

// ---- keyboard: the host provides no focus trap for modals, so the settings modal wraps Tab itself
const FOCUSABLE = 'a[href],button,input,select,textarea,summary,[tabindex]';
function visible(element) { return element.offsetParent !== null || (element.getClientRects?.().length ?? 0) > 0; }
export function tabbable(container) {
    return [...container.querySelectorAll(FOCUSABLE)].filter(element =>
        !element.disabled && !element.hidden && element.type !== 'hidden' && element.getAttribute('tabindex') !== '-1' && visible(element));
}
// Returns true when it handled the key. `active` is the focused element.
export function wrapTab(event, container, active) {
    if (event.key !== 'Tab' || event.defaultPrevented || !container) return false;
    const items = tabbable(container);
    if (!items.length) { event.preventDefault(); return true; }
    const first = items[0], last = items[items.length - 1];
    const leaving = event.shiftKey ? active === first || !container.contains(active) : active === last;
    if (!leaving) return false;
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
    return true;
}

// ---- timing: monotonic marks per voice turn, for measurement only (no content, never gates anything)
export const TIMING_TURNS = 50;
export function markTurn(timings, turn, name, at) {
    if (!turn || typeof at !== 'number' || !Number.isFinite(at)) return;
    let record = timings.get(turn);
    if (!record) {
        record = {}; timings.set(turn, record);
        while (timings.size > TIMING_TURNS) timings.delete(timings.keys().next().value);
    }
    if (!(name in record)) record[name] = at;          // the first mark of each kind wins
}
const span = (from, to) => (from != null && to != null && to >= from ? Math.round(to - from) : undefined);
// Intervals in milliseconds; an interval is absent when either end was never observed.
export function deriveTimings(record = {}) {
    const all = {
        utterance_ms: span(record.onset, record.speech_end),
        endpoint_ms: span(record.speech_end, record.final_sent),
        to_admit_ms: span(record.first_probe_sent, record.admitted_recv),
        admit_to_stop_ms: span(record.admitted_recv, record.interrupt_done),
        final_to_audio_event_ms: span(record.final_sent, record.audio_event),
        audio_event_to_source_ms: span(record.audio_event, record.source_start),
        speech_end_to_audible_ms: span(record.speech_end, record.source_start),
    };
    return Object.fromEntries(Object.entries(all).filter(([, value]) => value !== undefined));
}
const rank = (sorted, q) => sorted[Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1))];
export function summarizeTimings(timings) {
    const buckets = {};
    for (const record of timings.values()) {
        for (const [name, value] of Object.entries(deriveTimings(record))) if (value !== undefined) (buckets[name] ??= []).push(value);
    }
    return Object.fromEntries(Object.entries(buckets).map(([name, values]) => {
        const sorted = [...values].sort((a, b) => a - b);
        return [name, { n: sorted.length, p50: rank(sorted, .5), p90: rank(sorted, .9), max: sorted[sorted.length - 1] }];
    }));
}
