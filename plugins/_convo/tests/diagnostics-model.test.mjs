// The diagnostics model: server marks (seconds, server clock) and this page's marks (performance.now() ms) joined into one turn
// on one time axis, with the clock mapped through the poll round trip, failures classified as icon + words, and memory bounded.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import * as model from '../webui/diagnostics-model.js';
import { DOUBLE_TALK_MS } from '../webui/echo-guard.js';

const { buildView, bottleneck, classify, clockSample, contextView, createModel, echoView, emptyClock, formatMs, gateView, ingest, markCount,
        pushEcho, rolling, speculationView, toLocal, turnView, MAX_MARKS, MAX_TURNS, ECHO_SAMPLES, CLOCK_SAMPLES } = model;

// A completed, answered turn. The page sent its final audio at 3400 ms; the server received it at its own 100.000 s.
// The poll's round trip (sent 3400, received 3440, server clock 100.000) puts server 100.000 s at page 3420 ms, within ±20 ms.
const CLOCK = clockSample(emptyClock(), { sent: 3400, received: 3440, serverNow: 100 });
let seq = 0;
const at = (name, t, fields = {}) => ({ name, t, seq: ++seq, ...fields });
const SERVER = [
    at('candidate.received', 100.000, { sequence: 1, final: true }), at('policy.start', 100.010, { sequence: 1, final: true }),
    at('policy.done', 100.410, { sequence: 1, final: true }),
    at('policy.gate', 100.411, { sequence: 1, final: true, gate: 'calibrated', speech: 'speak', disposition: 'admit', mode: 'active', floor: .3,
                                 p_respond: .65, p_addressed: .55, p_backchannel: .1, p_side: .2, p_unfinished: .05, p_unclear: .1, p_explicit: .4, p_name: .15 }),
    at('admitted', 100.412, { sequence: 1, epoch: 1, previous_epoch: 0 }), at('state.sent', 100.415, { sequence: 1, final: true, disposition: 'admit' }),
    at('reply.start', 100.420), at('ctx.estimate', 100.425, { ctx_used: 9000, ctx_window: 16384 }), at('answer.first_delta', 100.900),
    at('speech.request', 100.950), at('audio.first', 101.150), at('reply.done', 101.200, { chars: 40 }), at('playback.started', 101.300),
    at('playback.completed', 103.000),
];
const PAGE = { onset: 1000, speech_end: 2500, final_sent: 3400, audio_event: 4600, source_start: 4700 };
const FLOORS = { respond_min: .3, ambient_respond_min: .9, interrupt_min: .6, clarify_min: .3, name_min: .9 };
const near = (actual, expected, tolerance = .01) => assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} is not ${expected}`);

test('the module is pure: no imports, so the store, the markup and these tests share one definition', async () => {
    const source = await readFile(new URL('../webui/diagnostics-model.js', import.meta.url), 'utf8');
    assert.doesNotMatch(source, /^\s*import\s/m);
    assert.doesNotMatch(source, /document|window\.|localStorage|fetch\(/);
    assert.match(source, /Omni-NexusAI\/speech-to-speech \(Apache-2\.0/);                    // the origin of the idea is credited
    assert.equal(model.DOUBLE_TALK_MS, DOUBLE_TALK_MS);                                       // the echo guard's confirmation time
});

test('the clock maps server seconds onto the page to within half the tightest round trip', () => {
    assert.deepEqual(CLOCK.best, { offset: 3420 - 100000, uncertainty: 20 });
    assert.equal(toLocal(CLOCK, 100.5), 3920);
    assert.equal(toLocal(emptyClock(), 100.5), undefined);                                    // unknown until the first answer
    let clock = clockSample(CLOCK, { sent: 9000, received: 9200, serverNow: 105.6 });       // a slow read does not displace a tight one
    assert.equal(clock.best.uncertainty, 20);
    clock = clockSample(clock, { sent: 10000, received: 10004, serverNow: 106.582 });        // a tighter read wins
    assert.deepEqual(clock.best, { offset: 10002 - 106582, uncertainty: 2 });
    assert.equal(clockSample(clock, { sent: 5, received: 4, serverNow: 1 }), clock);          // impossible or missing readings are ignored
    assert.equal(clockSample(clock, { sent: 5, received: 6, serverNow: NaN }), clock);
    for (let n = 0; n < CLOCK_SAMPLES + 5; n++) clock = clockSample(clock, { sent: n, received: n + 50, serverNow: n / 1000 });
    assert.equal(clock.samples.length, CLOCK_SAMPLES);                                        // bounded; the old tight sample aged out
    assert.equal(clock.best.uncertainty, 25);
});

test('marks of one turn from the page and the server line up on one axis, with the slowest step outlined', () => {
    const view = turnView('turn-answered-1', SERVER, PAGE, CLOCK, FLOORS);
    assert.deepEqual(view.steps.map(step => [step.id, step.ms]), [
        ['speech', 1500], ['endpoint', 900], ['classifier', 400], ['admission', 5], ['first_text', 480], ['first_audio', 250], ['audible', 130], ['playback', 1720]]);
    assert.equal(view.slowest, 'playback');
    assert.deepEqual(view.steps.filter(step => step.slowest).map(step => step.id), ['playback']);
    assert.equal(view.total, 5420);                                                           // from the onset to the last completion
    near(view.steps[0].left, 0); near(view.steps[0].width, 1500 / 5420 * 100);
    near(view.steps[2].left, (3430 - 1000) / 5420 * 100);                                     // the classifier sits after the page's final send
    assert.deepEqual(view.steps.filter(step => step.cross).map(step => [step.id, step.uncertainty]), [['audible', 20], ['playback', 20]]);
    assert.ok(view.steps.filter(step => !step.cross).every(step => step.uncertainty === null));   // one-clock steps are exact
    assert.deepEqual([view.final, view.disposition, view.outcome, view.short, view.at], [true, 'admit', 'Answered', 'turn-a', 1000]);
    assert.deepEqual(view.context, { used: 9000, window: 16384, ratio: 9000 / 16384, over: false });
    assert.deepEqual(view.speculation.state, 'none');
    assert.deepEqual(view.issues, []);
});

test('a step needs both of its ends: nothing is guessed, and an unknown clock leaves out only what crosses it', () => {
    const view = turnView('t', SERVER.filter(mark => mark.name !== 'answer.first_delta'), { onset: 1000 }, CLOCK, FLOORS);
    assert.deepEqual(view.steps.map(step => step.id), ['classifier', 'admission']);           // first text / first audio lost an end
    const unmapped = turnView('t', SERVER, PAGE, emptyClock(), FLOORS);
    assert.deepEqual(unmapped.steps.map(step => step.id), ['speech', 'endpoint']);            // server times cannot be placed yet
    assert.deepEqual(turnView('t', [], {}, CLOCK).steps, []);
    assert.equal(bottleneck([]), '');
    assert.equal(bottleneck([{ id: 'a', start: 0, end: 5 }, { id: 'b', start: 5, end: 10 }]), 'a');   // a tie goes to the earlier step
});

test('speech judged while it went on is a probe: its newest classification decides, worded as an interruption', () => {
    const marks = [
        at('candidate.received', 98.0, { sequence: 1, final: false }), at('policy.start', 98.01, { sequence: 1, final: false }),
        at('policy.done', 98.31, { sequence: 1, final: false }), at('state.sent', 98.32, { sequence: 1, final: false, disposition: 'withhold' }),
        at('candidate.received', 98.75, { sequence: 2, final: false }), at('policy.start', 98.76, { sequence: 2, final: false }),
        at('policy.done', 99.26, { sequence: 2, final: false }),
        at('policy.gate', 99.261, { sequence: 2, final: false, gate: 'calibrated', speech: 'speak', disposition: 'admit', mode: 'active', floor: .3, p_addressed: .9, p_explicit: .8 }),
        at('admitted', 99.262, { sequence: 2, epoch: 2, previous_epoch: 1 }), at('state.sent', 99.27, { sequence: 2, final: false, disposition: 'admit' })];
    const view = turnView('probe', marks, { first_probe_sent: 1300, admitted_recv: 1900, interrupt_done: 1950 }, CLOCK, FLOORS);
    assert.deepEqual(view.steps.map(step => [step.id, step.ms]), [['barge', 600], ['stop', 50], ['classifier', 500], ['admission', 10]]);
    assert.deepEqual([view.final, view.outcome], [false, 'Stopped the assistant (interruption)']);
    assert.equal(view.gate.decision.label, 'Stopped the assistant (interruption)');
    near(view.gate.scores.find(score => score.id === 'interrupt').value, .72);
    assert.equal(view.gate.scores.find(score => score.id === 'interrupt').reached, true);
});

test('failures are icon + words with a count, attached to the step they belong to, worst first', () => {
    const issues = classify([
        at('policy.timeout', 1, { sequence: 1, final: true }), at('policy.retry', 2, { sequence: 1, final: true, reason: 'TimeoutError' }),
        at('policy.timeout', 3, { sequence: 1, final: true }), at('policy.busy', 3.5), at('speech.busy', 4), at('spec.discard', 5, { disposition: 'ignore', chars: 0 }),
        at('spec.tool_dropped', 6), at('reply.error', 7, { error: 'ValueError' }), at('reply.empty', 8), at('model.listen', 9),
        at('ctx.estimate', 10, { ctx_used: 20000, ctx_window: 16384 }), at('ctx.estimate', 11, { ctx_used: 100, ctx_window: 16384 })]);
    const line = kind => issues.find(issue => issue.kind === kind);
    assert.deepEqual([line('policy.timeout').count, line('policy.timeout').text, line('policy.timeout').icon, line('policy.timeout').step], [2, 'Classifier timed out', 'timer_off', 'classifier']);
    assert.equal(line('policy.retry').text, 'Classifier asked again after TimeoutError');
    assert.equal(line('spec.discard').text, 'Early reply discarded (ignore)');
    assert.equal(line('reply.error').text, 'Reply failed (ValueError)');
    assert.equal(line('ctx.full').text, 'Context full: 20000 of 16384');                       // the one that fitted is not an issue
    assert.equal(line('ctx.full').count, 1);
    const ranks = issues.map(issue => ({ error: 0, warning: 1, info: 2 })[issue.severity]);
    assert.deepEqual(ranks, [...ranks].sort((a, b) => a - b));                                // every error before every warning, then information
    assert.deepEqual([...new Set(issues.map(issue => issue.severity))], ['error', 'warning', 'info']);
    assert.ok(issues.every(issue => issue.icon && issue.text));                               // never colour alone
    // A classifier that only failed leaves the turn unanswered, said in words, and its issues stay on the classifier step.
    const failed = turnView('f', [at('candidate.received', 100, { sequence: 1, final: true }), at('policy.start', 100.01, { sequence: 1, final: true }),
                                  at('policy.timeout', 101.51, { sequence: 1, final: true }), at('policy.retry', 101.6, { sequence: 1, final: true, reason: 'TimeoutError' }),
                                  at('policy.start', 101.61, { sequence: 1, final: true }), at('policy.error', 101.9, { sequence: 1, final: true, error: 'ValueError' })], {}, CLOCK);
    assert.deepEqual([failed.disposition, failed.outcome, failed.worst], ['failed', 'Classifier failed; not answered', 'error']);
    assert.deepEqual(failed.steps.map(step => [step.id, step.ms]), [['classifier', 1890]]);
    assert.deepEqual(failed.steps[0].issues.map(issue => issue.kind), ['policy.timeout', 'policy.error', 'policy.retry']);
});

test('the speech gate shows every probability and each score against its floor, or says the thresholds are fixed', () => {
    const gate = gateView(SERVER.find(mark => mark.name === 'policy.gate'), FLOORS);
    assert.deepEqual(gate.attention.map(item => [item.id, item.value, item.top]), [
        ['p_addressed', .55, true], ['p_backchannel', .1, false], ['p_side', .2, false], ['p_unfinished', .05, false], ['p_unclear', .1, false]]);
    const score = id => gate.scores.find(item => item.id === id);
    assert.deepEqual([score('respond').value, score('respond').floor, score('respond').reached], [.65, .3, true]);
    near(score('interrupt').value, .22); assert.deepEqual([score('interrupt').floor, score('interrupt').reached], [.6, false]);
    assert.deepEqual([score('unclear').value, score('unclear').floor, score('unclear').reached], [.1, .3, false]);
    assert.deepEqual([score('name').value, score('name').floor, score('name').reached], [.15, .9, false]);
    assert.deepEqual([gate.calibrated, gate.final, gate.mode, gate.decision.label, gate.decision.speech], [true, true, 'active', 'Answered', 'speak']);
    // The mark's own floor wins over the current settings (the session may have used another value); without one, the mode picks.
    assert.equal(gateView({ gate: 'calibrated', mode: 'ambient', final: true, p_addressed: .5, p_unclear: .1 }, FLOORS).scores[0].floor, .9);
    assert.equal(gateView({ gate: 'calibrated', mode: 'active', floor: .45, final: true }, FLOORS).scores[0].floor, .45);
    const fixed = gateView({ gate: 'fixed', speech: 'speak', disposition: 'admit', final: true, mode: 'active' }, FLOORS);
    assert.deepEqual([fixed.calibrated, fixed.attention, fixed.scores, fixed.decision.label], [false, [], [], 'Answered']);
    assert.equal(gateView(null), null);
});

test('the early reply of a turn is reported as used, discarded with its reason, held, or absent', () => {
    assert.deepEqual(speculationView([at('spec.start', 1), at('spec.promote', 2, { chars: 120 })]), { state: 'used', icon: 'bolt', text: 'Early reply used (120 characters held)' });
    assert.equal(speculationView([at('spec.start', 1), at('spec.discard', 2, { disposition: 'ignore' })]).text, 'Early reply discarded: ignore');
    assert.equal(speculationView([at('spec.start', 1), at('spec.tool_dropped', 2)]).text, 'Early reply held while the classifier decides · its tool call was dropped');
    assert.equal(speculationView([]).state, 'none');
});

test('rolling p50/p90 use nearest rank over the turns in memory, with counts of what went wrong and how turns were decided', () => {
    const views = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000].map((ms, index) => ({
        final: true, disposition: index < 7 ? 'admit' : 'ignore', steps: [{ id: 'classifier', ms }],
        issues: index % 5 === 0 ? [{ kind: 'policy.timeout', severity: 'error', icon: 'timer_off', count: 2 }] : [] }));
    const roll = rolling(views);
    assert.deepEqual(roll.steps.map(step => [step.id, step.n, step.p50, step.p90, step.max]), [['classifier', 10, 500, 900, 1000]]);
    assert.deepEqual(roll.counts.map(item => [item.kind, item.n, item.label]), [['policy.timeout', 4, 'classifier timeouts']]);
    assert.deepEqual(roll.decided, { admit: 7, ignore: 3, withhold: 0, failed: 0 });
    near(roll.steps[0].from, 500 / 900 * 100); near(roll.steps[0].span, 400 / 900 * 100);
    assert.deepEqual(rolling([]).steps, []);
});

test('context against the window: newest last, over the window flagged, compactions placed by time within the span', () => {
    const views = [{ turn: 'a', at: 1000, context: { used: 4000, window: 16000, ratio: .25, over: false } },
                   { turn: 'b', at: 3000, context: { used: 18000, window: 16000, ratio: 1.125, over: true } },
                   { turn: 'c', at: 2000, context: null }];
    const view = contextView(views, [{ at: 2000, exchanges: 6 }, { at: 99999, exchanges: 2 }]);
    assert.deepEqual(view.history.map(item => [item.turn, item.height, item.over]), [['a', 25, false], ['b', 100, true]]);
    assert.equal(view.latest.turn, 'b');
    assert.deepEqual(view.markers.map(item => [item.exchanges, item.left]), [[6, 50]]);       // outside the span: not drawn
    assert.equal(contextView([]).latest, null);
});

test('echo-guard readings: a 60-reading ring of flags and numbers only, with the 450 ms confirmation shown as progress', () => {
    const ring = [];
    for (let n = 0; n < ECHO_SAMPLES + 15; n++) {
        pushEcho(ring, { kind: 'echo_metric', mode: 'adaptive', acousticState: 'coupled', suppressing: n % 3 === 0, candidateMs: 225, independentMs: 300,
                         doubleTalk: false, playbackActive: true, nativeAec: true, correlation: .9, residual: { frames: [1, 2, 3] } });
    }
    pushEcho(ring, null); pushEcho(ring, 'noise');
    assert.equal(ring.length, ECHO_SAMPLES);
    assert.deepEqual(Object.keys(ring[0]).sort(), ['acoustic', 'candidateMs', 'doubleTalk', 'independentMs', 'mode', 'nativeAec', 'playback', 'suppressing']);
    const view = echoView(ring);
    assert.deepEqual([view.mode, view.acoustic, view.candidateMs, view.confirmMs, view.progress, view.samples.length], ['adaptive', 'coupled', 225, 450, .5, 60]);
    assert.equal(view.withheld, ring.filter(item => item.suppressing).length);
    assert.match(view.acousticText, /^Coupled/);
    assert.equal(echoView([]), null);
});

test('memory is bounded: marks are taken once by sequence, the newest turns are kept, and a restarted log starts over', () => {
    const state = createModel();
    ingest(state, { ok: true, now: 10, cursor: 3, turns: [{ turn: 'a', marks: [at('policy.start', 1), at('policy.done', 2)] }] }, { sent: 0, received: 10 });
    const known = state.turns.get('a').server.map(mark => mark.seq);
    ingest(state, { ok: true, cursor: 5, turns: [{ turn: 'a', marks: [{ ...state.turns.get('a').server[0] }, at('playback.completed', 3)] }] });
    assert.deepEqual(state.turns.get('a').server.map(mark => mark.name), ['policy.start', 'policy.done', 'playback.completed']);   // a repeated seq is taken once
    ingest(state, { ok: true, cursor: 6, turns: [{ turn: 'a', marks: [at('playback.completed', 4)] }] });
    assert.deepEqual(state.turns.get('a').server.map(mark => mark.t), [1, 2, 4]);                                                 // only the newest completion
    assert.equal(state.cursor, 6);
    assert.ok(known.every(n => state.turns.get('a').server.some(mark => mark.seq === n)));
    for (let n = 0; n < MAX_TURNS + 7; n++) ingest(state, { ok: true, turns: [{ turn: 'turn-' + n, marks: [at('policy.start', n)] }] });
    assert.equal(state.turns.size, MAX_TURNS);
    assert.ok(!state.turns.has('a') && state.turns.has('turn-' + (MAX_TURNS + 6)));                                                // the oldest go first
    const heavy = createModel();
    for (let n = 0; n < 5; n++) ingest(heavy, { ok: true, turns: [{ turn: 'h' + n, marks: Array.from({ length: 600 }, (_, i) => at('tick', i)) }] });
    assert.ok(markCount(heavy) <= MAX_MARKS);
    assert.deepEqual([...heavy.turns.keys()], ['h2', 'h3', 'h4']);
    ingest(state, { ok: false, error: 'Timing marks are available in developer mode' });                                         // a refusal changes nothing
    assert.equal(state.turns.size, MAX_TURNS);
    ingest(state, { ok: true, reset: true, now: 1, cursor: 2, turns: [{ turn: 'fresh', marks: [at('policy.start', .5)] }] }, { sent: 5, received: 7 });
    assert.deepEqual([[...state.turns.keys()], state.cursor, state.clock.samples.length], [['fresh'], 2, 1]);                      // marks and clock from before are dropped
    ingest(state, { ok: true, turns: [{ turn: 7, marks: [] }, { turn: 'x', marks: 'nope' }, null, { turn: 'y', marks: [{ name: 'a' }, null] }] });
    assert.deepEqual(state.turns.get('y').server, []);                                                                             // malformed marks are dropped
    assert.ok(!state.turns.has('x'));
});

test('one view per poll: turns newest first, the selection kept, the latest completed turn chosen by default', () => {
    const state = createModel();
    ingest(state, { ok: true, now: 100, cursor: 99, turns: [{ turn: 'turn-answered-1', marks: SERVER }] }, { sent: 3400, received: 3440 });
    const browser = [{ turn: 'turn-answered-1', marks: PAGE }, { turn: 'page-only', marks: { onset: 9000, first_probe_sent: 9400 } }];
    const view = buildView(state, { browser, floors: FLOORS });
    assert.deepEqual(view.turns.map(turn => turn.turn), ['page-only', 'turn-answered-1']);
    assert.equal(view.selected.turn, 'turn-answered-1');                                        // the newest completed turn
    assert.equal(buildView(state, { browser, floors: FLOORS, selected: 'page-only' }).selected.turn, 'page-only');
    assert.deepEqual(view.clock, { uncertainty: 20, samples: 1 });
    assert.equal(view.rolling.turns, 2);
    assert.equal(view.context.latest.used, 9000);
    assert.deepEqual(buildView(createModel()).turns, []);
    assert.equal(buildView(createModel()).selected, null);
});

test('times read as words: milliseconds, seconds and the clock uncertainty', () => {
    assert.equal(formatMs(420), '420 ms');
    assert.equal(formatMs(1420), '1.42 s');
    assert.equal(formatMs(12500), '12.5 s');
    assert.equal(formatMs(130, 20), '130 ms ±20 ms');
    assert.equal(formatMs(undefined), '—');
    assert.equal(model.probability(.456), '0.46');
});
