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
    'job.dispatching', 'job.running', 'job.cancel_requested', 'job.notice_claimed']);

const REASONS = { interrupted: 'Reply interrupted', stopped: 'Conversation stopped', retargeted: 'Voice moved to another chat' };
const JOB_END = { 'job.completed': 'Background task completed', 'job.failed': 'Background task failed',
    'job.cancelled': 'Background task cancelled', 'job.uncertain': 'Background task outcome unconfirmed' };

export function clip(value, limit) {
    const text = String(value ?? '').trim();
    return text.length > limit ? text.slice(0, limit - 1).trimEnd() + '…' : text;
}

// How one journal event reads in the transcript. Only real journal facts are described; nothing is inferred.
export function describeEvent(event, jobs = []) {
    const payload = event.payload || {};
    const job = jobs.find(item => item.id === payload.job_id);
    switch (event.kind) {
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
