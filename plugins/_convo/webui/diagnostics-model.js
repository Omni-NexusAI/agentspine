// Pure model of the developer diagnostics panel: no imports, no DOM and no host access, so the store, the markup and the node
// tests share one definition. It joins the server's timing marks (`metrics`, seconds on the server's monotonic clock) with this
// page's own marks (`timingReport()`, performance.now() milliseconds) into per-turn views on ONE time axis.
//
// Origin: written afresh for Convo after the diagnostics view of Omni-NexusAI/speech-to-speech (Apache-2.0,
// web/hf-realtime-voice/main.js renderDiagnostics); nothing is copied. Not carried over from it: failures shown by colour alone,
// rendering while hidden, a position restored without clamping, zero sizes saved while hidden, low-contrast text.

export const MAX_TURNS = 20;          // turns kept in memory
export const MAX_MARKS = 2000;        // server marks kept in memory, all turns together
export const ECHO_SAMPLES = 60;       // echo-guard readings kept while the panel is visible
export const CLOCK_SAMPLES = 20;      // poll round trips kept for the clock mapping (the tightest one wins)
export const DOUBLE_TALK_MS = 450;    // the echo guard's confirmation time (echo-guard.js DOUBLE_TALK_MS; a test keeps them equal)
const LATEST = new Set(['playback.completed']);   // the server keeps only the newest of these per turn; so does the model

// ---- one clock: the server's monotonic seconds mapped onto this page's performance.now() through the poll round trip
// The server read its clock somewhere between sending and receiving, so the mapping is exact to within half the round trip.
export function emptyClock() { return { samples: [], best: null }; }
export function clockSample(clock, { sent, received, serverNow }) {
    if (![sent, received, serverNow].every(Number.isFinite) || received < sent) return clock;
    const sample = { offset: (sent + received) / 2 - serverNow * 1000, uncertainty: (received - sent) / 2 };
    const samples = [...clock.samples, sample].slice(-CLOCK_SAMPLES);
    const best = samples.reduce((a, b) => (b.uncertainty < a.uncertainty ? b : a));
    return { samples, best };
}
// A server time (seconds) as this page's time (milliseconds), or undefined while the clock is unknown.
export function toLocal(clock, seconds) { return clock?.best && Number.isFinite(seconds) ? seconds * 1000 + clock.best.offset : undefined; }

// ---- the bounded store of server marks
export function createModel() { return { turns: new Map(), cursor: null, clock: emptyClock() }; }

// Take in one `metrics` answer. `timing` is {sent, received} (performance.now() around the request) for the clock mapping.
export function ingest(model, response, timing) {
    if (!response || response.ok === false) return model;
    if (response.reset) {                  // the server's log restarted: its marks and its clock are no longer comparable
        model.turns.clear();
        model.clock = emptyClock();
    }
    if (timing && Number.isFinite(response.now)) model.clock = clockSample(model.clock, { ...timing, serverNow: response.now });
    for (const item of response.turns || []) {
        if (!item || typeof item.turn !== 'string' || !Array.isArray(item.marks)) continue;
        const record = model.turns.get(item.turn) || { turn: item.turn, server: [] };
        const known = new Set(record.server.map(mark => mark.seq));
        let marks = record.server.concat(item.marks.filter(mark => mark && typeof mark.name === 'string' && Number.isFinite(mark.t) && !known.has(mark.seq)));
        for (const name of LATEST) {
            const copies = marks.filter(mark => mark.name === name);
            if (copies.length > 1) {
                const newest = copies.reduce((a, b) => ((b.seq ?? 0) > (a.seq ?? 0) ? b : a));
                marks = marks.filter(mark => mark.name !== name || mark === newest);
            }
        }
        record.server = marks.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
        model.turns.delete(item.turn);     // most recently active last, so the oldest go first
        model.turns.set(item.turn, record);
    }
    if (Number.isInteger(response.cursor)) model.cursor = response.cursor;
    bound(model);
    return model;
}

export function markCount(model) { let total = 0; for (const record of model.turns.values()) total += record.server.length; return total; }
function bound(model) {
    while (model.turns.size > MAX_TURNS) model.turns.delete(model.turns.keys().next().value);
    let total = markCount(model);
    while (total > MAX_MARKS && model.turns.size > 1) {
        const oldest = model.turns.keys().next().value;
        total -= model.turns.get(oldest).server.length;
        model.turns.delete(oldest);
    }
    if (total > MAX_MARKS) {               // one turn alone over the bound keeps its newest marks
        const only = model.turns.values().next().value;
        only.server = only.server.slice(-MAX_MARKS);
    }
}

// ---- what went wrong, as icon + words (colour is only a second cue)
export const ISSUES = {
    'policy.timeout': { step: 'classifier', severity: 'error', icon: 'timer_off', text: () => 'Classifier timed out' },
    'policy.error': { step: 'classifier', severity: 'error', icon: 'error', text: m => `Classifier failed (${m.error || 'error'})` },
    'policy.retry': { step: 'classifier', severity: 'warning', icon: 'replay', text: m => `Classifier asked again${m.reason ? ` after ${m.reason}` : ''}` },
    'policy.busy': { step: 'classifier', severity: 'warning', icon: 'hourglass_top', text: () => 'Classifier still busy with an earlier call; this turn waited' },
    'speech.busy': { step: 'first_audio', severity: 'warning', icon: 'hourglass_top', text: () => 'Speech engine still busy; the reply waited' },
    'spec.discard': { step: 'first_text', severity: 'warning', icon: 'cancel', text: m => `Early reply discarded${m.disposition ? ` (${m.disposition})` : ''}` },
    'spec.tool_dropped': { step: 'first_text', severity: 'warning', icon: 'block', text: () => 'Tool call from the early reply dropped' },
    'reply.error': { step: 'first_text', severity: 'error', icon: 'error', text: m => `Reply failed (${m.error || 'error'})` },
    'reply.empty': { step: 'first_text', severity: 'warning', icon: 'warning', text: () => 'The model returned no reply' },
    'reply.markup': { step: 'first_text', severity: 'warning', icon: 'warning', text: () => 'A tool call written as text was removed' },
    'model.listen': { step: 'first_text', severity: 'info', icon: 'hearing', text: () => 'The model chose to stay quiet ([LISTEN])' },
    'model.listen_overridden': { step: 'first_text', severity: 'info', icon: 'record_voice_over', text: () => '[LISTEN] overridden: an explicit request is always answered' },
    'turn.superseded': { step: 'admission', severity: 'info', icon: 'merge', text: () => 'Joined with the continuation that followed' },
};

// Every issue of one turn, repeated kinds folded into one line with a count.
export function classify(marks) {
    const found = new Map();
    const add = (kind, rule, text) => {
        const key = kind + '|' + text;
        const item = found.get(key) || { kind, step: rule.step, severity: rule.severity, icon: rule.icon, text, count: 0 };
        item.count += 1;
        found.set(key, item);
    };
    for (const mark of marks) {
        const rule = ISSUES[mark.name];
        if (rule) add(mark.name, rule, rule.text(mark));
        if (mark.name === 'ctx.estimate' && Number.isFinite(mark.ctx_used) && Number.isFinite(mark.ctx_window) && mark.ctx_used > mark.ctx_window)
            add('ctx.full', { step: 'first_text', severity: 'error', icon: 'data_usage' }, `Context full: ${mark.ctx_used} of ${mark.ctx_window}`);
    }
    const order = { error: 0, warning: 1, info: 2 };
    return [...found.values()].sort((a, b) => order[a.severity] - order[b.severity]);
}

// ---- one turn on one axis
export const STEPS = [
    { id: 'speech', label: 'Speech' },
    { id: 'barge', label: 'Interrupt decision' },
    { id: 'stop', label: 'Playback stopped' },
    { id: 'endpoint', label: 'End of turn' },
    { id: 'classifier', label: 'Classifier' },
    { id: 'admission', label: 'Admission' },
    { id: 'first_text', label: 'First text' },
    { id: 'first_audio', label: 'First audio' },
    { id: 'audible', label: 'Audible' },
    { id: 'playback', label: 'Playback' },
];
const DISPOSITIONS = { admit: 'Answered', ignore: 'Not for the assistant', withhold: 'Held back' };
// Speech judged while it was still going on (a probe) decides about interrupting, not about answering.
const WHILE_SPEAKING = { admit: 'Stopped the assistant (interruption)', ignore: 'Ignored while the assistant spoke', withhold: 'Not decided while still speaking' };

// The marks that decided the turn: the completed utterance's when there is one, else the newest probe's.
function decisive(marks) {
    if (marks.some(mark => mark.final === true)) return marks.filter(mark => mark.final === true);
    const probes = marks.filter(mark => mark.name === 'policy.start' && Number.isInteger(mark.sequence));
    if (!probes.length) return [];
    const sequence = Math.max(...probes.map(mark => mark.sequence));
    return marks.filter(mark => mark.sequence === sequence && mark.final === false);
}
const first = (marks, name) => marks.find(mark => mark.name === name) || null;
const last = (marks, ...names) => { for (let i = marks.length - 1; i >= 0; i--) if (names.includes(marks[i].name)) return marks[i]; return null; };

// `server`: this turn's marks (seq order); `browser`: its timingReport() marks; `floors`: the speech gate settings.
export function turnView(turn, server = [], browser = {}, clock = null, floors = {}) {
    const local = mark => (mark ? toLocal(clock, mark.t) : undefined);
    const page = name => (Number.isFinite(browser?.[name]) ? browser[name] : undefined);
    const chosen = decisive(server);
    const ends = [];
    const step = (id, start, end, cross = false) => { if (Number.isFinite(start) && Number.isFinite(end) && end >= start) ends.push({ id, start, end, cross }); };
    step('speech', page('onset'), page('speech_end'));
    step('barge', page('first_probe_sent'), page('admitted_recv'));
    step('stop', page('admitted_recv'), page('interrupt_done'));
    step('endpoint', page('speech_end'), page('final_sent'));
    step('classifier', local(first(chosen, 'policy.start')), local(last(chosen, 'policy.done', 'policy.timeout', 'policy.error')));
    step('admission', local(last(chosen, 'policy.done')), local(last(chosen, 'state.sent')));
    step('first_text', local(first(server, 'reply.start')), local(first(server, 'answer.first_delta')));
    step('first_audio', local(first(server, 'answer.first_delta')), local(first(server, 'audio.first')));
    step('audible', local(first(server, 'audio.first')), page('source_start'), true);
    step('playback', page('source_start'), local(last(server, 'playback.completed')), true);

    const issues = classify(server);
    const order = STEPS.map(item => item.id);
    ends.sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
    const origin = ends.length ? Math.min(...ends.map(item => item.start)) : undefined;
    const finish = ends.length ? Math.max(...ends.map(item => item.end)) : undefined;
    const total = ends.length ? Math.max(1, finish - origin) : 0;
    const slowest = bottleneck(ends);
    const uncertainty = clock?.best ? Math.round(clock.best.uncertainty) : null;
    const steps = ends.map(item => ({
        id: item.id, label: STEPS.find(s => s.id === item.id).label,
        ms: Math.round(item.end - item.start), cross: item.cross, uncertainty: item.cross ? uncertainty : null,
        left: ((item.start - origin) / total) * 100, width: Math.max(.6, ((item.end - item.start) / total) * 100),
        slowest: item.id === slowest, issues: issues.filter(issue => issue.step === item.id),
    }));
    const outcome = last(chosen, 'state.sent');
    const failed = !outcome && chosen.some(mark => ['policy.timeout', 'policy.error'].includes(mark.name));
    const times = [...server.map(local), ...Object.values(browser || {})].filter(Number.isFinite);
    const context = last(server, 'ctx.estimate');
    const final = server.some(mark => mark.final === true) || Number.isFinite(page('final_sent'));
    const words = final ? DISPOSITIONS : WHILE_SPEAKING;
    return {
        turn, short: String(turn).slice(0, 6), at: times.length ? Math.min(...times) : undefined, final,
        disposition: outcome?.disposition || (failed ? 'failed' : ''),
        outcome: outcome ? (words[outcome.disposition] || outcome.disposition) : failed ? 'Classifier failed; not answered' : chosen.length ? 'Deciding…' : 'No decision recorded',
        steps, total: Math.round(total), slowest,
        issues, worst: issues[0]?.severity || '',
        gate: gateView(last(chosen, 'policy.gate'), floors),
        speculation: speculationView(server),
        context: context && Number.isFinite(context.ctx_used) && Number.isFinite(context.ctx_window)
            ? { used: context.ctx_used, window: context.ctx_window, ratio: context.ctx_window > 0 ? context.ctx_used / context.ctx_window : 0, over: context.ctx_used > context.ctx_window }
            : null,
    };
}

// The slowest step, or '' when there is none. Equal durations: the earlier step.
export function bottleneck(steps) {
    let best = null;
    for (const item of steps) if (!best || item.end - item.start > best.end - best.start) best = item;
    return best ? best.id : '';
}

// ---- the speech gate: probabilities against their floors
const ATTENTION = [['p_addressed', 'Addressed to the assistant'], ['p_backchannel', 'Acknowledgement'], ['p_side', 'Talk to someone else'],
    ['p_unfinished', 'Unfinished thought'], ['p_unclear', 'Unclear']];
export function gateView(mark, floors = {}) {
    if (!mark) return null;
    const calibrated = mark.gate === 'calibrated';
    const words = mark.final === false ? WHILE_SPEAKING : DISPOSITIONS;
    const decision = { disposition: mark.disposition || '', speech: mark.speech || '', label: words[mark.disposition] || mark.disposition || 'No decision' };
    if (!calibrated) return { calibrated, final: mark.final === true, mode: mark.mode || '', decision, attention: [], scores: [] };
    const value = name => (Number.isFinite(mark[name]) ? mark[name] : 0);
    const floor = (given, fallback) => (Number.isFinite(given) ? given : Number.isFinite(fallback) ? fallback : null);
    const score = (id, label, amount, line, hint) => ({ id, label, value: amount, floor: line, reached: line !== null && amount + 1e-9 >= line, hint });
    const respondFloor = floor(mark.floor, mark.mode === 'active' ? floors.respond_min : floors.ambient_respond_min);
    const attention = ATTENTION.map(([name, label]) => ({ id: name, label, value: value(name) }));
    const top = attention.reduce((a, b) => (b.value > a.value ? b : a));
    return {
        calibrated, final: mark.final === true, mode: mark.mode || '', decision,
        attention: attention.map(item => ({ ...item, top: item === top })),
        scores: [
            score('respond', 'Answer (addressed + unclear)', Number.isFinite(mark.p_respond) ? mark.p_respond : value('p_addressed') + value('p_unclear'), respondFloor,
                mark.mode && mark.mode !== 'active' ? 'Floor for ambient listening' : 'Floor for active listening'),
            score('interrupt', 'Interrupt (addressed × explicit request)', value('p_addressed') * value('p_explicit'), floor(floors.interrupt_min), 'Stops the assistant while it speaks'),
            score('unclear', 'Ask which was meant (unclear)', value('p_unclear'), floor(floors.clarify_min), 'A clarifying question also needs little mass elsewhere'),
            score('name', 'Called by name', value('p_name'), floor(floors.name_min), 'Used in name-activated listening'),
        ],
    };
}

// ---- the early (speculative) reply of one turn
export function speculationView(marks) {
    const started = marks.some(mark => mark.name === 'spec.start');
    const promoted = first(marks, 'spec.promote');
    const discarded = first(marks, 'spec.discard');
    const dropped = marks.some(mark => mark.name === 'spec.tool_dropped');
    const tail = dropped ? ' · its tool call was dropped' : '';
    if (promoted) return { state: 'used', icon: 'bolt', text: `Early reply used${Number.isFinite(promoted.chars) ? ` (${promoted.chars} characters held)` : ''}${tail}` };
    if (discarded) return { state: 'discarded', icon: 'cancel', text: `Early reply discarded${discarded.disposition ? `: ${discarded.disposition}` : ''}${tail}` };
    if (started) return { state: 'held', icon: 'pending', text: `Early reply held while the classifier decides${tail}` };
    return { state: 'none', icon: 'remove', text: 'No early reply for this turn' };
}

// ---- rolling distributions over the turns in memory
const rank = (sorted, q) => sorted[Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1))];
export function rolling(views) {
    const buckets = new Map();
    for (const view of views) for (const item of view.steps) {
        if (!buckets.has(item.id)) buckets.set(item.id, []);
        buckets.get(item.id).push(item.ms);
    }
    const steps = STEPS.filter(item => buckets.has(item.id)).map(item => {
        const sorted = [...buckets.get(item.id)].sort((a, b) => a - b);
        return { id: item.id, label: item.label, n: sorted.length, p50: rank(sorted, .5), p90: rank(sorted, .9), max: sorted[sorted.length - 1] };
    });
    const scale = Math.max(1, ...steps.map(item => item.p90));
    const counts = new Map();
    for (const view of views) for (const issue of view.issues) {
        const item = counts.get(issue.kind) || { kind: issue.kind, severity: issue.severity, icon: issue.icon, label: ISSUE_LABELS[issue.kind] || issue.kind, n: 0 };
        item.n += issue.count;
        counts.set(issue.kind, item);
    }
    const decided = { admit: 0, ignore: 0, withhold: 0, failed: 0 };
    for (const view of views) if (view.final && view.disposition in decided) decided[view.disposition] += 1;
    return { steps: steps.map(item => ({ ...item, from: (item.p50 / scale) * 100, span: Math.max(.6, ((item.p90 - item.p50) / scale) * 100) })),
             scale, counts: [...counts.values()], decided, turns: views.length };
}
const ISSUE_LABELS = { 'policy.timeout': 'classifier timeouts', 'policy.error': 'classifier errors', 'policy.retry': 'classifier retries',
    'policy.busy': 'classifier waits', 'speech.busy': 'speech engine waits', 'spec.discard': 'early replies discarded', 'spec.tool_dropped': 'early tool calls dropped',
    'reply.error': 'reply failures', 'reply.empty': 'empty replies', 'reply.markup': 'tool calls written as text', 'model.listen': '[LISTEN] silences',
    'model.listen_overridden': '[LISTEN] overrides', 'turn.superseded': 'joined continuations', 'ctx.full': 'context full' };

// ---- context against the window, newest last, with compaction markers placed by time
export function contextView(views, compactions = []) {
    const history = views.filter(view => view.context && Number.isFinite(view.at)).sort((a, b) => a.at - b.at)
        .map(view => ({ turn: view.turn, at: view.at, ...view.context }));
    const latest = history[history.length - 1] || null;
    const span = history.length > 1 ? [history[0].at, history[history.length - 1].at] : null;
    const markers = compactions.filter(item => Number.isFinite(item.at))
        .filter(item => !span || (item.at >= span[0] && item.at <= span[1]))
        .map(item => ({ ...item, left: span ? ((item.at - span[0]) / Math.max(1, span[1] - span[0])) * 100 : 100 }));
    return { latest, history: history.map(item => ({ ...item, height: Math.max(2, Math.min(100, item.ratio * 100)) })), markers };
}

// ---- echo guard readings (only while the panel is visible): content-free flags and numbers, newest last
export function pushEcho(ring, metric) {
    if (!metric || typeof metric !== 'object') return ring;
    const number = value => (Number.isFinite(value) ? value : 0);
    ring.push({ mode: String(metric.mode || ''), acoustic: String(metric.acousticState || 'unknown'), suppressing: !!metric.suppressing,
                candidateMs: number(metric.candidateMs), independentMs: number(metric.independentMs), doubleTalk: !!metric.doubleTalk,
                playback: !!metric.playbackActive, nativeAec: !!metric.nativeAec });
    while (ring.length > ECHO_SAMPLES) ring.shift();
    return ring;
}
const ACOUSTIC = { coupled: 'Coupled: the microphone hears the speaker', uncoupled: 'Uncoupled: headset or native echo cancellation', unknown: 'Still learning the echo path' };
export function echoView(ring) {
    const latest = ring[ring.length - 1];
    if (!latest) return null;
    const withheld = ring.filter(item => item.suppressing).length;
    return { mode: latest.mode, acoustic: latest.acoustic, acousticText: ACOUSTIC[latest.acoustic] || latest.acoustic, suppressing: latest.suppressing,
             candidateMs: Math.round(latest.candidateMs), confirmMs: DOUBLE_TALK_MS, progress: Math.min(1, latest.candidateMs / DOUBLE_TALK_MS),
             doubleTalk: latest.doubleTalk, playback: latest.playback, nativeAec: latest.nativeAec, withheld, samples: ring.map(item => item.suppressing) };
}

// ---- everything the panel draws, from one poll's inputs
// `browser`: timingReport().turns; `floors`: settings.policy_gate; `compactions`: [{at (page ms), exchanges}].
export function buildView(model, { browser = [], floors = {}, compactions = [], selected = '' } = {}) {
    const pageMarks = new Map((browser || []).filter(item => item && typeof item.turn === 'string').map(item => [item.turn, item.marks || {}]));
    const ids = new Set([...model.turns.keys(), ...pageMarks.keys()]);
    const views = [...ids].map(turn => turnView(turn, model.turns.get(turn)?.server || [], pageMarks.get(turn) || {}, model.clock, floors))
        .filter(view => Number.isFinite(view.at)).sort((a, b) => b.at - a.at).slice(0, MAX_TURNS);
    const chosen = views.find(view => view.turn === selected) || views.find(view => view.final) || views[0] || null;
    const clock = model.clock?.best ? { uncertainty: Math.round(model.clock.best.uncertainty), samples: model.clock.samples.length } : null;
    return { clock, turns: views, selected: chosen, rolling: rolling(views), context: contextView(views, compactions) };
}

// ---- words and numbers
export function formatMs(ms, uncertainty = null) {
    if (!Number.isFinite(ms)) return '—';
    const text = ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
    return Number.isFinite(uncertainty) && uncertainty > 0 ? `${text} ±${Math.round(uncertainty)} ms` : text;
}
export const probability = value => (Number.isFinite(value) ? value.toFixed(2) : '—');
