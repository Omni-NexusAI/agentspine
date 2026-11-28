import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { loadModule, hostStubs } from './vm-loader.mjs';
import { storeFixture } from './store-fixture.mjs';
import { describeEvent, foldTurns, notAnsweredText, wrapTab, mergeEvents, nearBottom, STICK_PX } from '../webui/view-helpers.js';

const tick = (ms = 5) => new Promise(resolve => setTimeout(resolve, ms));

// A journal of `count` events whose sequence numbers are 1..count. Every third event is hidden bookkeeping.
function journal(count) {
    return Array.from({ length: count }, (_, index) => {
        const seq = index + 1, kind = seq % 3 === 0 ? 'assistant.generated' : seq % 3 === 1 ? 'user.accepted' : 'assistant.delivered';
        return { seq, session: 's', turn: 't' + seq, kind, payload: { text: 'message ' + seq, transcript: 'you said ' + seq } };
    });
}

async function fixture({ events = journal(150), jobs = [], developer = false } = {}) {
    const requests = [];
    const server = { events: [...events], jobs: [...jobs] };
    let selected = 'a';
    const api = async (_url, payload) => {
        requests.push(payload);
        if (payload.action === 'status') return { settings: { enabled: true, max_audio_seconds: 20, developer_mode: developer } };
        if (payload.action !== 'history') return { ok: true };
        const all = server.events.filter(event => payload.target === undefined || payload.target === 'a' || payload.target === 'b');
        if (payload.tail !== undefined) {
            const pool = payload.before === undefined ? all : all.filter(event => event.seq < payload.before);
            return { events: pool.slice(-payload.tail), more: pool.length > payload.tail, jobs: server.jobs };
        }
        return { events: all.filter(event => event.seq > payload.after), more: false, jobs: server.jobs };
    };
    const bindings = hostStubs({
        '/js/api.js': { callJsonApi: api, fetchApi: api },
        '/js/websocket.js': { createNamespacedClient: () => ({}) },
        '/js/shortcuts.js': { getCurrentContextId: () => selected, getCurrentContext: () => ({ id: selected, name: 'Chat ' + selected }), openModal: async () => {} },
        './audio.js': { AudioSession: class {} },
    });
    const { namespace } = await loadModule('webui/convo-store.js', { bindings, globals: {
        document: { hidden: false, getElementById: () => null, addEventListener() {} }, window: { confirm: () => true, addEventListener() {} } } });
    const store = namespace.store;
    return { store, requests, server, select: id => { selected = id; }, history: () => requests.filter(item => item.action === 'history') };
}

// A scrolling element whose content height follows the number of rendered events, like the real transcript.
function scroller(store, { clientHeight = 400, rowHeight = 20, scrollTop = 0 } = {}) {
    const element = { clientHeight, scrollTop };
    Object.defineProperty(element, 'scrollHeight', { get: () => 40 + rowHeight * store.visibleEvents.length });
    return element;
}

test('The first read asks for the newest page, then polling asks only for what is newer', async () => {
    const f = await fixture();
    await f.store.mountPanel();
    assert.deepEqual(Array.from(f.history(), ({ tail, after, before }) => ({ tail, after, before })), [{ tail: 60, after: undefined, before: undefined }]);
    assert.deepEqual([f.store.events[0].seq, f.store.events.at(-1).seq, f.store.events.length], [91, 150, 60]);
    assert.equal(f.store.moreEarlier, true);
    f.server.events.push(...journal(152).slice(150));
    await f.store.history();
    assert.equal(f.history().at(-1).after, 150);
    assert.equal(f.history().at(-1).tail, undefined);
    assert.deepEqual([f.store.events.length, f.store.events.at(-1).seq], [62, 152]);
    await f.store.history();                                   // nothing new: no duplicates
    assert.equal(f.store.events.length, 62);
});

test('A chat with no history keeps asking for the newest page and shows nothing earlier', async () => {
    const f = await fixture({ events: [] });
    await f.store.mountPanel(); await f.store.history();
    assert.equal(f.store.events.length, 0);
    assert.equal(f.store.moreEarlier, false);
    assert.ok(f.history().every(item => item.tail === 60));
});

test('Loading earlier messages prepends in order, keeps the reader in place, and runs once at a time', async () => {
    const f = await fixture();
    await f.store.mountPanel();
    const element = scroller(f.store, { scrollTop: 0 });
    f.store.bindTranscript(element); await tick();
    element.scrollTop = 0; f.store.onScroll(element);
    const before = element.scrollHeight;
    await Promise.all([f.store.loadEarlier(), f.store.loadEarlier()]);       // the second call is ignored while one is in flight
    assert.equal(f.history().filter(item => item.before !== undefined).length, 1);
    assert.equal(f.history().at(-1).before, 91);
    assert.deepEqual(Array.from(f.store.events, event => event.seq), Array.from({ length: 150 }, (_, i) => i + 1).slice(30));
    assert.equal(f.store.moreEarlier, true);
    await tick();
    const added = element.scrollHeight - before;
    assert.ok(added > 0);
    assert.equal(element.scrollTop, added);                                     // the same message is still at the top of the view
    await f.store.loadEarlier();
    assert.equal(f.store.events[0].seq, 1);
    assert.equal(f.store.moreEarlier, false);
    const calls = f.history().length;
    await f.store.loadEarlier();                                                 // nothing earlier: no request
    assert.equal(f.history().length, calls);
});

test('The transcript follows new messages only while the reader is at the end', async () => {
    const f = await fixture({ events: journal(60) });
    await f.store.mountPanel();
    const element = scroller(f.store, { scrollTop: 0 });
    f.store.bindTranscript(element); await tick();
    assert.equal(element.scrollTop, element.scrollHeight);                      // opens at the latest message
    f.store.onScroll(element);
    assert.equal(f.store.pinned, true);

    f.server.events.push(...journal(63).slice(60));                             // a visible event appears while following
    await f.store.history(); await tick();
    assert.equal(element.scrollTop, element.scrollHeight);
    assert.equal(f.store.unseen, 0);

    element.scrollTop = 100; f.store.onScroll(element);                         // the reader scrolls up
    assert.equal(f.store.pinned, false);
    f.server.events.push(...journal(68).slice(63));                             // five more events, one of them hidden bookkeeping
    await f.store.history(); await tick();
    assert.equal(element.scrollTop, 100);                                       // the view is not yanked away
    assert.equal(f.store.unseen, journal(68).slice(63).filter(event => event.kind !== 'assistant.generated').length);

    f.store.jumpToLatest();
    assert.deepEqual([f.store.pinned, f.store.unseen, element.scrollTop], [true, 0, element.scrollHeight]);

    element.scrollTop = 50; f.store.onScroll(element);
    f.server.events.push(...journal(70).slice(68));
    await f.store.history();
    element.scrollTop = element.scrollHeight - element.clientHeight - STICK_PX + 1; f.store.onScroll(element);   // back within reach of the end
    assert.deepEqual([f.store.pinned, f.store.unseen], [true, 0]);
});

test('Changing chat starts a fresh history with no stale paging state', async () => {
    const f = await fixture();
    await f.store.mountPanel();
    assert.equal(f.store.moreEarlier, true);
    f.store.target = 'b';
    f.server.events = journal(5);
    await f.store.history();
    assert.deepEqual([f.store.events.length, f.store.moreEarlier, f.store.unseen], [5, false, 0]);
    assert.equal(f.history().at(-1).tail, 60);
});

test('Active background tasks and the retarget offer are derived from real state', async () => {
    const f = await fixture({ jobs: [{ id: 'j1', status: 'running', prompt: 'Write the report', target: 'a' }, { id: 'j2', status: 'completed', prompt: 'Old', target: 'a' },
                                     { id: 'j3', status: 'queued', prompt: 'Next', target: 'a' }] });
    await f.store.mountPanel();
    assert.deepEqual(Array.from(f.store.activeJobs, job => job.id), ['j1', 'j3']);
    assert.equal(f.store.canRetarget, false);                                    // the selected chat is the target
    f.select('b'); await f.store.refresh();
    f.store.target = 'a';                                                         // an active session stays on its chat
    assert.equal(f.store.canRetarget, false);                                    // and nothing has to be pressed: its work follows the open chat
});

test('Bookkeeping is hidden, delivered speech and real activity are shown, and nothing is invented', async () => {
    const jobs = [{ id: 'j', prompt: 'Write the weekly report for the team', result: 'Done: report.md' }];
    const f = await fixture({ events: [
        { seq: 1, kind: 'user.accepted', payload: {} },
        { seq: 2, kind: 'assistant.generated', payload: { text: 'never heard' } },
        { seq: 3, kind: 'assistant.delivered', payload: { text: 'spoken' } },
        { seq: 4, kind: 'assistant.playback_started', payload: {} },
        { seq: 5, kind: 'job.queued', payload: { job_id: 'j' } },
        { seq: 6, kind: 'job.running', payload: { job_id: 'j' } },
        { seq: 7, kind: 'job.completed', payload: { job_id: 'j', result: 'Done: report.md' } },
        { seq: 8, kind: 'turn.interrupted', payload: { reason: 'interrupted' } },
        { seq: 9, kind: 'context.compacted', payload: {} }], jobs });
    await f.store.mountPanel();
    const shown = Array.from(f.store.visibleEvents, event => [event.seq, f.store.eventRole(event), f.store.eventLabel(event), f.store.eventText(event)]);
    assert.deepEqual(shown, [
        [3, 'assistant', 'Convo', 'spoken'],
        [5, 'activity', 'Background task queued', 'Write the weekly report for the team'],
        [7, 'activity', 'Background task completed', 'Done: report.md'],
        [8, 'activity', 'Reply interrupted', '']]);
    assert.ok(!shown.some(([, , label]) => /generated|not delivered/.test(label)));   // an undelivered draft is never shown as speech
    f.store.settings.developer_mode = true;
    // Developer mode alone: the same entries, and the turn without words as a faint note; bookkeeping rows and undelivered text need their own settings.
    assert.deepEqual(Array.from(f.store.visibleEvents, event => [event.key ?? event.seq, f.store.eventLabel(event)]), [
        [1, 'You'], [3, 'Convo'], [5, 'Background task queued'], [7, 'Background task completed'], [8, 'Reply interrupted']]);
    f.store.settings.show_diagnostic_events = true; f.store.settings.show_rejected_responses = true;
    // With both: the same folded entries (a playback start belongs to its turn and is no row of its own), the reply nothing
    // of which was delivered listed as generated only, and the bookkeeping rows.
    assert.deepEqual(Array.from(f.store.visibleEvents, event => [event.key ?? event.seq, f.store.eventLabel(event)]), [
        [1, 'You'], [2, 'Convo · generated, not delivered'], [3, 'Convo'], [5, 'Background task queued'], [6, 'job running'],
        [7, 'Background task completed'], [8, 'Reply interrupted'], [9, 'context compacted']]);
    assert.equal(f.store.eventLabel(f.store.events[1]), 'Convo · generated, not delivered');
});

test('describeEvent covers every journal kind the store writes', () => {
    assert.equal(describeEvent({ kind: 'turn.interrupted', payload: { reason: 'stopped' } }).label, 'Conversation stopped');
    assert.equal(describeEvent({ kind: 'turn.interrupted', payload: { reason: 'retargeted' } }).label, 'Voice moved to another chat');
    assert.equal(describeEvent({ kind: 'turn.interrupted', payload: {} }).label, 'Turn ended early');
    assert.equal(describeEvent({ kind: 'segment.closed', payload: { next_target: 'x' } }).role, 'activity');
    assert.equal(describeEvent({ kind: 'segment.opened', payload: { previous_target: 'x' } }).label, 'Voice joined this chat');
    assert.equal(describeEvent({ kind: 'job.failed', payload: { job_id: 'zz', result: 'x'.repeat(900) } }).text.length, 400);
    assert.equal(describeEvent({ kind: 'job.queued', payload: { job_id: 'unknown' } }, []).text, '');
    assert.deepEqual(describeEvent({ kind: 'custom.thing', payload: { text: 'hello' } }), { role: 'activity', label: 'custom thing', text: 'hello' });
});

test('mergeEvents is ordered and idempotent; nearBottom uses the follow threshold', () => {
    assert.deepEqual(mergeEvents([{ seq: 3 }, { seq: 1 }], [{ seq: 2 }, { seq: 3, x: 1 }]).map(event => event.seq), [1, 2, 3]);
    assert.equal(mergeEvents([{ seq: 3 }], [{ seq: 3, x: 1 }])[0].x, 1);
    assert.equal(nearBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 552 }), true);
    assert.equal(nearBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 551 }), false);
    assert.equal(nearBottom(null), true);
});

function dialog(labels) {
    const items = labels.map(label => ({ label, disabled: false, hidden: false, type: 'button', offsetParent: {}, getAttribute: () => null, focus() { dialog.focused = label; } }));
    return { items, container: { querySelectorAll: () => items, contains: element => items.includes(element) } };
}
const key = (extra = {}) => ({ key: 'Tab', shiftKey: false, defaultPrevented: false, prevented: false, preventDefault() { this.prevented = true; }, ...extra });

test('Tab wraps inside the dialog and leaves ordinary navigation alone', () => {
    const { items, container } = dialog(['close', 'tab', 'save']);
    let event = key(); assert.equal(wrapTab(event, container, items[2]), true);
    assert.deepEqual([event.prevented, dialog.focused], [true, 'close']);
    event = key({ shiftKey: true }); assert.equal(wrapTab(event, container, items[0]), true);
    assert.deepEqual([event.prevented, dialog.focused], [true, 'save']);
    event = key(); assert.equal(wrapTab(event, container, items[1]), false);
    assert.equal(event.prevented, false);
    event = key({ shiftKey: true }); assert.equal(wrapTab(event, container, { elsewhere: true }), true);   // focus escaped the dialog
    assert.equal(dialog.focused, 'save');
    assert.equal(wrapTab(key({ key: 'Enter' }), container, items[2]), false);
    items[1].disabled = true; items[1].hidden = true; items[2].offsetParent = null;                 // unreachable items are skipped
    event = key(); assert.equal(wrapTab(event, container, items[0]), true);
    assert.equal(dialog.focused, 'close');
    const empty = { querySelectorAll: () => [], contains: () => false };
    event = key(); assert.equal(wrapTab(event, empty, null), true); assert.equal(event.prevented, true);
});

// ---- one "You" and one "Convo" entry per turn (foldTurns) --------------------------------------------------------------------
const ev = (seq, turn, kind, payload = {}, extra = {}) => ({ seq, session: 's', turn, kind, payload, ...extra });
const plainOf = value => JSON.parse(JSON.stringify(value));
const shown = (entries, jobs = []) => entries.map(entry => [entry.key ?? entry.seq, describeEvent(entry, jobs).role, describeEvent(entry, jobs).label, describeEvent(entry, jobs).text]);

test('A reply heard in three phrases is one Convo entry, spoken text joined as delivered, placed at its first reply event', () => {
    const events = [
        ev(1, 't1', 'user.accepted', {}), ev(2, 't1', 'assistant.playback_started', { phrase_id: 'a' }),
        ev(3, 't1', 'assistant.delivered', { text: 'First sentence.', phrase_id: 'a' }), ev(4, 't1', 'user.transcript', { text: 'What is up?', source: 'model' }),
        ev(5, 't1', 'assistant.delivered', { text: ' Second one,', phrase_id: 'b' }), ev(6, 't1', 'assistant.generated', { text: 'First sentence. Second one, and a third.' }),
        ev(7, 't1', 'assistant.delivered', { text: ' and a third.', phrase_id: 'c' }),
        ev(8, 't2', 'user.accepted', { transcript: 'typed words' }), ev(9, 't2', 'assistant.generated', { text: 'Generated, never heard.' }),
        ev(10, '', 'segment.opened', {})];
    assert.deepEqual(shown(foldTurns(events)), [
        [1, 'user', 'You', 'What is up?'],                                              // the newest transcript of the turn
        [3, 'assistant', 'Convo', 'First sentence. Second one, and a third.'],          // no separator added: the phrases carry their own
        [8, 'user', 'You', 'typed words'],
        [10, 'activity', 'Voice joined this chat', '']]);                             // a reply nothing of which was heard: no speech bubble
    assert.equal(events[0].payload.transcript, undefined);                            // the journal itself is never changed
    const reply = foldTurns(events.slice(0, 3));                                        // before the rest arrived: one phrase, same place
    assert.deepEqual(shown(reply).at(-1), [3, 'assistant', 'Convo', 'First sentence.']);
    assert.deepEqual(foldTurns([ev(1, 't', 'user.accepted', {})]), []);                   // no words: no row (the reply to it still shows)
    const early = foldTurns([ev(1, 't', 'user.accepted', {}), ev(2, 't', 'assistant.generated', { text: 'All of it.' }), ev(3, 't', 'assistant.delivered', { text: 'All of it.' })]);
    assert.deepEqual(shown(early).at(-1), [2, 'assistant', 'Convo', 'All of it.']);  // generated first: the entry sits at that event
});

test('A reply is labelled interrupted when the journal says so, or when less was heard than generated and the user went on', () => {
    const base = [ev(1, 't1', 'user.accepted', { transcript: 'Tell me' }), ev(2, 't1', 'assistant.delivered', { text: 'Half of' }), ev(3, 't1', 'assistant.generated', { text: 'Half of the answer.' })];
    assert.equal(foldTurns(base)[1].label, 'Convo');                                     // still the newest turn: it may yet be heard
    assert.equal(foldTurns([...base, ev(4, 't2', 'user.accepted', {})])[1].label, 'Convo · interrupted');
    const said = foldTurns([...base.slice(0, 2), ev(3, 't1', 'turn.interrupted', { reason: 'interrupted' })]);
    assert.deepEqual(shown(said).slice(1), [[2, 'assistant', 'Convo · interrupted', 'Half of'], [3, 'activity', 'Reply interrupted', '']]);
    const whole = [ev(1, 't1', 'user.accepted', { transcript: 'Tell me' }), ev(2, 't1', 'assistant.delivered', { text: 'All  of\nit.' }), ev(3, 't1', 'assistant.generated', { text: 'All of it.' }), ev(4, 't2', 'user.accepted', {})];
    assert.equal(foldTurns(whole)[1].label, 'Convo');                                    // whitespace is not a missing part
});

test('Phrases are spoken trimmed: the panel joins them as the history does, and a reply heard in full is never called interrupted', () => {
    // As journaled live (2026-11-17): each phrase without the space that separated it from the next.
    const turn = [ev(1, 't1', 'user.accepted', { transcript: 'Tell me' }), ev(2, 't1', 'assistant.delivered', { text: 'Octopuses have blue blood.' }),
                  ev(3, 't1', 'assistant.delivered', { text: 'Two hearts pump it.' }),
                  ev(4, 't1', 'assistant.generated', { text: 'Octopuses have blue blood. Two hearts pump it.' }), ev(5, 't2', 'user.accepted', {})];
    const entry = foldTurns(turn)[1];
    assert.equal(entry.text, 'Octopuses have blue blood. Two hearts pump it.');
    assert.equal(entry.label, 'Convo');                                                  // a later turn exists, and nothing is missing
});

test('Turns are folded per session and turn; events without a turn are never folded together', () => {
    const events = [ev(1, 'x', 'user.accepted', { transcript: 'this session' }), { seq: 2, session: 'other', turn: 'x', kind: 'user.accepted', payload: { transcript: 'other session' } },
                    { seq: 3, kind: 'user.accepted', payload: { transcript: 'alone' } }, { seq: 4, kind: 'assistant.delivered', payload: { text: 'loose' } }, { seq: 5, kind: 'user.transcript', payload: { text: 'orphan' } }];
    assert.deepEqual(shown(foldTurns(events)).map(row => row.slice(0, 3)), [[1, 'user', 'You'], [2, 'user', 'You'], [3, 'user', 'You'], [4, 'assistant', 'Convo']]);
});

test('Developer mode: the same entries plus a secondary line, the reply never delivered listed as such, and not-answered speech', () => {
    const events = [
        ev(1, 't1', 'user.accepted', {}, { time: 100 }), ev(2, 't1', 'user.transcript', { text: 'Tell me a story', source: 'whisper' }, { time: 101 }),
        ev(3, 't1', 'assistant.delivered', { text: 'Once upon a time' }, { time: 102 }), ev(4, 't1', 'assistant.generated', { text: 'Once upon a time there was a fox.' }, { time: 103 }),
        ev(5, 't1', 'turn.interrupted', { reason: 'interrupted' }, { time: 104 }), ev(6, 't2', 'user.accepted', {}, { time: 110 }),
        ev(7, 't2', 'assistant.generated', { text: 'Never spoken.' }, { time: 111 }), ev(8, '', 'context.compacted', {}, { time: 120 })];
    const notes = [{ turn: 'gone', reason: 'for_someone_else', at: 105 }, { turn: 't2', reason: 'waiting_for_more', at: 112 }, { turn: 'late', reason: 'odd_new_reason', at: 999 }];
    const entries = foldTurns(events, { developer: true, notes });
    assert.deepEqual(entries.map(entry => [entry.key ?? entry.seq, entry.label ?? entry.kind, entry.detail ?? '']), [
        [1, 'You', 'Transcript: Whisper'],
        [3, 'Convo · interrupted', '1 phrase heard · not heard: “there was a fox.”'],
        [5, 'turn.interrupted', ''],
        ['n:gone', 'You · not answered', 'Not answered: talk to someone else'],          // no journal entry of its own: placed by time
        [6, 'You', 'No words captured · Not answered: waiting for you to finish'],        // its own entry carries the reason
        [7, 'Convo · generated, not delivered', 'Generated, never delivered: nothing of it was heard'],
        [8, 'context.compacted', ''],
        ['n:late', 'You · not answered', 'Not answered: odd new reason']]);
    assert.equal(foldTurns(events, { notes }).some(entry => entry.kind === 'turn.note' || entry.detail), false);   // never outside developer mode
    assert.equal(notAnsweredText('backchannel'), 'Not answered: an acknowledgement');
    const reworded = foldTurns([ev(1, 't', 'user.accepted', {}), ev(2, 't', 'assistant.delivered', { text: 'Done' }), ev(3, 't', 'assistant.generated', { text: '<tool>x</tool> Done' })], { developer: true });
    assert.equal(reworded[1].detail, '1 phrase heard · 15 more characters generated than heard');
});

test('The panel keys entries by their first event, counts new entries (not journal rows) for "new", and shows details only to developers', async () => {
    const turn = (n, seq) => [ev(seq, 't' + n, 'user.accepted', { transcript: 'q' + n }), ev(seq + 1, 't' + n, 'assistant.delivered', { text: 'a' }),
                              ev(seq + 2, 't' + n, 'assistant.delivered', { text: 'b' }), ev(seq + 3, 't' + n, 'assistant.generated', { text: 'ab' })];
    const f = await fixture({ events: turn(1, 1) });
    await f.store.mountPanel();
    const element = scroller(f.store, { clientHeight: 20, rowHeight: 100, scrollTop: 0 });
    f.store.bindTranscript(element); await tick();
    assert.deepEqual(Array.from(f.store.visibleEvents, entry => entry.key), [1, 2]);
    element.scrollTop = 0; f.store.onScroll(element);                                   // the reader scrolled up
    f.server.events.push(ev(5, 't1', 'assistant.delivered', { text: 'c' }));             // a phrase more for a turn already shown: no new entry
    await f.store.history();
    assert.equal(f.store.unseen, 0);
    f.server.events.push(...turn(2, 6));                                                  // a new turn, four journal rows: two entries
    await f.store.history();
    assert.equal(f.store.unseen, 2);
    assert.equal(f.store.eventDetail(f.store.visibleEvents[1]), '');
    f.store.settings.developer_mode = true;
    assert.equal(f.store.eventDetail(f.store.visibleEvents[1]), '3 phrases heard');
    const html = await (await import('node:fs/promises')).readFile(new URL('../webui/main.html', import.meta.url), 'utf8');
    assert.match(html, /<template x-for="event in \$store\.convo\.visibleEvents" :key="event\.key \?\? event\.seq">/);
    assert.match(html, /<p class="convo-detail" x-show="\$store\.convo\.eventDetail\(event\)" x-text="\$store\.convo\.eventDetail\(event\)"><\/p>/);
});

test('Not-answered notes belong to the chat they were heard in: another chat starts without them', async () => {
    const f = await fixture({ events: journal(3), developer: true });
    await f.store.mountPanel();
    f.store.notAnswered = [{ turn: 'gone', reason: 'backchannel', at: 1 }];
    assert.equal(f.store.visibleEvents.some(entry => entry.kind === 'turn.note'), true);
    f.store.target = 'b';
    await f.store.history();
    assert.deepEqual(plainOf(f.store.notAnswered), []);
    assert.equal(f.store.visibleEvents.some(entry => entry.kind === 'turn.note'), false);
});

test('The draft follows one turn and gives way once that turn\'s words are in the history; a new turn starts a new draft', async () => {
    const journal = [];
    const f = await storeFixture({ api: (_url, payload) => (payload.action === 'history' ? { events: journal.slice(), jobs: [], more: false } : undefined) });
    await f.store.start();
    const send = event => f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, ...event } });
    send({ type: 'text.delta', turn_id: 't1', text: 'Hello ' }); send({ type: 'text.delta', turn_id: 't1', text: 'there.' });
    assert.equal(f.store.draft, 'Hello there.');
    journal.push(ev(1, 't1', 'user.accepted', {}), ev(2, 't1', 'assistant.generated', { text: 'Hello there.' }));
    await f.store.history();
    assert.equal(f.store.draft, 'Hello there.');                                        // generated only: nothing heard yet, the draft stays
    journal.push(ev(3, 't1', 'assistant.delivered', { text: 'Hello there.' }));
    await f.store.history();
    assert.equal(f.store.draft, '');                                                     // heard: the history shows it now
    send({ type: 'text.delta', turn_id: 't1', text: ' (late)' });
    assert.equal(f.store.draft, '');                                                     // that turn's words are not drafted again
    send({ type: 'text.delta', turn_id: 't2', text: 'Next' });
    send({ type: 'text.delta', turn_id: 't3', text: 'Other' });
    assert.equal(f.store.draft, 'Other');                                                // a new turn replaces an old draft, never appends to it
    send({ type: 'text.delta', text: ' and more' });                                     // a server that sends no turn id: appended as before
    assert.equal(f.store.draft, 'Other and more');
    await f.store.stop();
});

// ---- turns without words, and turns taken over by what the user said next (turn.superseded)
const rows = (events, options) => foldTurns(events, options).map(entry => [entry.key ?? entry.seq, entry.text ?? '', entry.detail ?? '']);

test('A turn without words is no row outside developer mode; its reply is; developers see a faint note with the reason when it is known', () => {
    const events = [ev(1, 't1', 'user.accepted'), ev(2, 't1', 'assistant.delivered', { text: 'An answer.' }), ev(3, 't2', 'user.accepted'),
        ev(4, 't2', 'turn.interrupted', { reason: 'interrupted' }), ev(5, 't3', 'user.accepted'), ev(6, 't3', 'turn.held', { decision: 'silent', reason: 'side_talk' }),
        ev(7, 't4', 'turn.held', { decision: 'wait', reason: 'unfinished' }), ev(8, 't5', 'user.accepted'), ev(9, 't5', 'turn.superseded', { by: 't6' }), ev(10, 't6', 'user.accepted')];
    assert.deepEqual(rows(events), [[2, 'An answer.', '']]);                               // 4: nothing of that reply was heard, so no row says it was cut
    assert.deepEqual(rows([...events, ev(11, 't2', 'assistant.delivered', { text: 'Half an' })]).map(row => row[0]), [2, 4, 11]);    // some of it was: as before
    const developer = foldTurns(events, { developer: true, diagnostics: false });
    assert.deepEqual(developer.map(entry => [entry.key ?? entry.seq, describeEvent(entry).text, entry.detail ?? '']), [
        [1, '', 'No words captured'], [2, 'An answer.', '1 phrase heard'],
        [3, '', 'No words captured · interrupted before the words were known'], [4, '', ''],
        [5, '', 'No words captured · stayed silent: side talk'], [7, '', 'No words captured · waiting for you to finish'],
        [10, '', 'No words captured · said in 2 parts']]);                                  // one entry for what was said with pauses
    assert.deepEqual(developer.filter(entry => entry.faint).map(entry => entry.key), [1, 3, 5, 7, 10]);
    const known = foldTurns([...events.slice(0, 2), ev(11, 't1', 'user.transcript', { text: 'The words came later', source: 'whisper' })]);
    assert.deepEqual(known.map(entry => entry.text), ['The words came later', 'An answer.']);   // at its own place, before the reply
    assert.equal(describeEvent({ kind: 'user.accepted', payload: {} }).text, '');
    assert.equal(describeEvent({ kind: 'turn.superseded', payload: { by: 'x' } }).label, 'Taken over by what you said next');
});

test('A taken-over turn is hidden once a turn that took it over carries words; while none does, its own words are shown once', () => {
    const taken = [ev(1, 'a', 'user.accepted', { transcript: 'Book a table' }), ev(2, 'a', 'turn.superseded', { by: 'b' }), ev(3, 'b', 'user.accepted')];
    assert.deepEqual(rows(taken), [[1, 'Book a table', '']]);                             // only the earlier words exist
    const joined = [...taken, ev(4, 'b', 'user.transcript', { text: 'Book a table for six' }), ev(5, 'b', 'assistant.delivered', { text: 'Done.' })];
    assert.deepEqual(rows(joined), [[3, 'Book a table for six', ''], [5, 'Done.', '']]);  // the joined words hold them
    // A chain: the last turn with words stands for all before it.
    const chain = [ev(1, 'a', 'user.accepted', { transcript: 'One' }), ev(2, 'a', 'turn.superseded', { by: 'b' }), ev(3, 'b', 'user.accepted', { transcript: 'One two' }),
        ev(4, 'b', 'turn.superseded', { by: 'c' }), ev(5, 'c', 'user.accepted')];
    assert.deepEqual(rows(chain), [[3, 'One two', '']]);
    assert.deepEqual(rows([...chain, ev(6, 'c', 'user.transcript', { text: 'One two three' })]), [[5, 'One two three', '']]);
    // Developers see every row, each with what became of it; the bookkeeping row only with the diagnostics rows.
    assert.deepEqual(rows(chain, { developer: true, diagnostics: false }), [[1, 'One', 'taken over by what you said next'],
        [3, 'One two', 'taken over by what you said next'], [5, '', 'No words captured · said in 3 parts']]);
    assert.ok(foldTurns(chain, { developer: true }).some(entry => entry.kind === 'turn.superseded'));
    // The turn that took it over is not on this page, is in another session, or the two name each other: the words stay shown.
    assert.deepEqual(rows(taken.slice(0, 2)), [[1, 'Book a table', '']]);
    assert.deepEqual(rows([...taken.slice(0, 2), { seq: 9, session: 'other', turn: 'b', kind: 'user.accepted', payload: { transcript: 'elsewhere' } }]), [[1, 'Book a table', ''], [9, 'elsewhere', '']]);
    assert.deepEqual(rows([ev(1, 'a', 'user.accepted', { transcript: 'loop' }), ev(2, 'a', 'turn.superseded', { by: 'b' }), ev(3, 'b', 'user.accepted'), ev(4, 'b', 'turn.superseded', { by: 'a' })]),
        [[1, 'loop', '']]);
    // A held fragment (no accepted event) that was taken over follows the same rule.
    assert.deepEqual(rows([ev(1, 'a', 'turn.held', { decision: 'wait', transcript: 'and then' }), ev(2, 'a', 'turn.superseded', { by: 'b' }), ev(3, 'b', 'user.accepted', { transcript: 'and then home' })]),
        [[3, 'and then home', '']]);
});

test('The panel dims a row without words and never shows a placeholder for one', async () => {
    const html = await readFile(new URL('../webui/main.html', import.meta.url), 'utf8');
    assert.match(html, /:data-faint="event\.faint \? '' : null"/);
    for (const file of ['main.html', 'view-helpers.js', 'convo-store.js']) assert.doesNotMatch(await readFile(new URL('../webui/' + file, import.meta.url), 'utf8'), /no verbatim transcript/);
});
