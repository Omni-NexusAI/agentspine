// The pending-action card (convo-store.js + main.html): Convo asks before it goes ahead (`action.pending`), the panel shows one card
// with Run and Dismiss, a press sends `convo.action`, and the card goes on `action.resolved`, after its seconds, when voice stops
// and when a newer question replaces it.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { storeFixture } from './store-fixture.mjs';

const tick = (ms = 5) => new Promise(resolve => setTimeout(resolve, ms));
const plain = value => JSON.parse(JSON.stringify(value));
const pending = (extra = {}) => ({ data: { session_id: 's', epoch: 0, type: 'action.pending', id: 'a1', name: 'delegate_task', label: 'Hand over a task',
    summary: 'Tidy the downloads folder and list what was moved.', seconds: 30, ...extra } });
const sent = f => f.requests.filter(r => r.event === 'convo.action').map(r => plain(r.data));

test('A pending action shows one card with its label and summary; nothing is sent until a button is pressed', async () => {
    const f = await storeFixture();
    await f.store.start();
    assert.equal(f.store.pendingAction, null);
    f.handlers['convo.event'](pending());
    assert.deepEqual(plain(f.store.pendingAction), { id: 'a1', label: 'Hand over a task', summary: 'Tidy the downloads folder and list what was moved.', busy: false });
    assert.deepEqual(sent(f), []);
    await f.store.stop();
});

test('Run sends run: true with the conversation\'s identity and the card goes; Dismiss sends run: false', async () => {
    const f = await storeFixture();
    await f.store.start();
    f.handlers['convo.event'](pending());
    assert.equal(await f.store.answerAction(true), true);
    assert.deepEqual(sent(f), [{ session_id: 's', epoch: 0, id: 'a1', run: true }]);
    assert.equal(f.store.pendingAction, null);
    f.handlers['convo.event'](pending({ id: 'a2' }));
    assert.equal(await f.store.answerAction(false), true);
    assert.deepEqual(sent(f).at(-1), { session_id: 's', epoch: 0, id: 'a2', run: false });
    assert.equal(f.store.pendingAction, null);
    assert.equal(await f.store.answerAction(true), false);                               // no card: nothing to answer
    assert.equal(sent(f).length, 2);
    await f.store.stop();
});

test('A press while the answer is on its way is not sent twice; a refused send keeps the card for another press', async () => {
    let release, refuse = false;
    const held = new Promise(resolve => { release = resolve; });
    const f = await storeFixture({ answer: async event => { if (event !== 'convo.action') return null; if (refuse) return new Error('Internal server error'); await held; return null; } });
    await f.store.start();
    f.handlers['convo.event'](pending());
    const first = f.store.answerAction(true);
    assert.equal(f.store.pendingAction.busy, true);                                      // the buttons are disabled meanwhile
    assert.equal(await f.store.answerAction(false), false);
    release();
    assert.equal(await first, true);
    assert.equal(sent(f).length, 1);
    refuse = true;
    f.handlers['convo.event'](pending({ id: 'a3' }));
    assert.equal(await f.store.answerAction(true), false);
    assert.equal(f.store.pendingAction?.id, 'a3');
    assert.equal(f.store.pendingAction.busy, false);
    assert.match(f.store.error, /Internal server error/);
    await f.store.stop();
});

test('A press on a question the server no longer holds (answered by voice, replaced, expired) just removes the card', async () => {
    const f = await storeFixture({ answer: event => (event === 'convo.action' ? { ok: false, stale: true } : null) });
    await f.store.start();
    f.handlers['convo.event'](pending());
    assert.equal(await f.store.answerAction(true), false);
    assert.equal(f.store.pendingAction, null);
    assert.equal(f.store.error, '');                                                     // not an error: there is nothing left to answer
    assert.equal(sent(f).length, 1);
    await f.store.stop();
});

test('The card goes when the server resolves it, whatever the outcome and whatever the epoch; another id leaves it', async () => {
    const f = await storeFixture();
    await f.store.start();
    for (const outcome of ['run', 'declined', 'expired', 'replaced']) {
        f.handlers['convo.event'](pending());
        f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, type: 'action.resolved', id: 'other', outcome } });
        assert.equal(f.store.pendingAction?.id, 'a1', outcome);
        f.handlers['convo.event']({ data: { session_id: 's', epoch: 7, type: 'action.resolved', id: 'a1', outcome } });   // a barge-in moved the epoch on
        assert.equal(f.store.pendingAction, null, outcome);
    }
    f.handlers['convo.event'](pending({ epoch: 7 }));                                     // the question itself is not bound to one reply either
    assert.equal(f.store.pendingAction?.id, 'a1');
    f.handlers['convo.event']({ data: { session_id: 'another', epoch: 0, type: 'action.resolved', id: 'a1', outcome: 'run' } });
    assert.equal(f.store.pendingAction?.id, 'a1');                                        // another conversation's event is not ours
    assert.deepEqual(sent(f), []);                                                        // resolving shows and sends nothing else
    await f.store.stop();
});

test('The card expires after its seconds, a newer question replaces it, and stopping voice removes it', async () => {
    const f = await storeFixture();
    await f.store.start();
    f.handlers['convo.event'](pending({ seconds: 0.03 }));
    assert.equal(f.store.pendingAction?.id, 'a1');
    await tick(80);
    assert.equal(f.store.pendingAction, null);
    f.handlers['convo.event'](pending({ id: 'old', seconds: 0.03 }));
    f.handlers['convo.event'](pending({ id: 'new', label: 'Save to memory', summary: 'The door code changed.', seconds: 30 }));
    assert.deepEqual(plain(f.store.pendingAction), { id: 'new', label: 'Save to memory', summary: 'The door code changed.', busy: false });
    await tick(80);
    assert.equal(f.store.pendingAction?.id, 'new');                                       // the replaced card's timer does not take the new one
    await f.store.stop();
    assert.equal(f.store.pendingAction, null);
    f.handlers['convo.event'](pending());                                                 // no conversation: nothing to ask
    assert.equal(f.store.pendingAction, null);
});

test('The card\'s text is bounded and kept as text; a question without an id is ignored', async () => {
    const f = await storeFixture();
    await f.store.start();
    f.handlers['convo.event'](pending({ id: '' }));
    assert.equal(f.store.pendingAction, null);
    f.handlers['convo.event'](pending({ label: 'L'.repeat(200), summary: '<b>bold</b> ' + 's'.repeat(400) }));
    assert.equal(f.store.pendingAction.label.length, 80);
    assert.equal(f.store.pendingAction.summary.length, 300);
    assert.equal(f.store.pendingAction.summary.startsWith('<b>bold</b> '), true);         // markup stays characters: the panel renders it with x-text
    await f.store.stop();
});

test('In the history the asking is bookkeeping, and every voice tool reads as words', async () => {
    const { describeEvent, foldTurns, HIDDEN_KINDS } = await import('../webui/view-helpers.js');
    const asked = ['action.asked', 'action.confirmed', 'action.declined', 'action.expired', 'action.replaced'];
    for (const kind of asked) assert.equal(HIDDEN_KINDS.has(kind), true, kind);
    const journal = asked.map((kind, index) => ({ seq: index + 1, kind, turn: 't', payload: { id: 'a1', name: 'remember' } }));
    assert.deepEqual(foldTurns(journal, { developer: false }), []);                       // shown only with developer diagnostics on
    assert.equal(foldTurns(journal, { developer: true, diagnostics: true }).length, asked.length);
    const labels = { web_search: 'Web search', web_read: 'Page read', memory_read: 'Memory search', remember: 'Memory save', delegate_task: 'Task',
        task_status: 'Task check', cancel_task: 'Cancellation', system_status: 'System check', world_time: 'Time lookup', set_timer: 'Timer',
        chats: 'Chat lookup', open: 'Opening', voice_speed: 'Voice speed', listen_back: 'Listening back', camera_context: 'Camera look',
        screen_context: 'Screen look', recall_conversation: 'Conversation lookup' };
    for (const [name, label] of Object.entries(labels)) {
        assert.equal(describeEvent({ kind: 'tool.done', payload: { name, query: 'words' } }).label, label, name);
        assert.equal(describeEvent({ kind: 'tool.failed', payload: { name, query: 'words' } }).label, label + ' · failed', name);
    }
});

test('The panel renders the card with real buttons, accessible names and a polite announcement', () => {
    const html = readFileSync(new URL('../webui/main.html', import.meta.url), 'utf8');
    const start = html.indexOf('<div class="convo-ask"'), card = html.slice(start, html.indexOf('<ul class="convo-links"'));
    assert.ok(start > 0 && start < html.indexOf('class="convo-log"'));                    // in the panel, above the history
    assert.match(card, /role="group" aria-label="[^"]+" x-show="\$store\.convo\.pendingAction"/);
    assert.match(card, /role="status"><strong x-text="\$store\.convo\.pendingAction\?\.label \|\| ''"><\/strong>/);
    assert.match(card, /x-text="\$store\.convo\.pendingAction\?\.summary \|\| ''"/);
    assert.doesNotMatch(card, /x-html|role="alert"|aria-live="assertive"/);               // text only, never assertive
    const buttons = [...card.matchAll(/<button type="button"[^>]*>([^<]*)<\/button>/g)];
    assert.deepEqual(buttons.map(match => match[1]), ['Run', 'Dismiss']);
    assert.match(buttons[0][0], /@click="\$store\.convo\.answerAction\(true\)"/);
    assert.match(buttons[1][0], /@click="\$store\.convo\.answerAction\(false\)"/);
    assert.match(buttons[0][0], /:aria-label="'Run: ' \+ /);                              // the name starts with the visible word
    assert.match(buttons[1][0], /:aria-label="'Dismiss: ' \+ /);
    for (const [button] of buttons) assert.match(button, /:disabled="\$store\.convo\.pendingAction\?\.busy === true"/);
    const style = html.slice(html.indexOf('<style>'));
    const rule = style.match(/\.convo-ask\{[^}]*\}/)?.[0] || '';
    assert.match(rule, /var\(--color-border\)/); assert.match(rule, /var\(--color-panel\)/);   // the panel's own tokens, so both themes
    assert.doesNotMatch(style.match(/\.convo-ask[^{]*\{[^}]*\}/g).join(''), /overflow-y/);     // the history stays the only scroller
});
