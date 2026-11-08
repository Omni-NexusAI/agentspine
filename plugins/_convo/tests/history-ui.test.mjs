import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadModule, hostStubs } from './vm-loader.mjs';
import { describeEvent, wrapTab, mergeEvents, nearBottom, STICK_PX } from '../webui/view-helpers.js';

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
    assert.equal(f.store.canRetarget, true);
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
        [1, 'user', 'You', '[Audio input; no verbatim transcript]'],
        [3, 'assistant', 'Convo', 'spoken'],
        [5, 'activity', 'Background task queued', 'Write the weekly report for the team'],
        [7, 'activity', 'Background task completed', 'Done: report.md'],
        [8, 'activity', 'Reply interrupted', '']]);
    assert.ok(!shown.some(([, , label]) => /generated|not delivered/.test(label)));   // an undelivered draft is never shown as speech
    f.store.settings.developer_mode = true;
    assert.equal(f.store.visibleEvents.length, 9);
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
