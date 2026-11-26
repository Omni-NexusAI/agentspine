// Errors go away: when things work again (a later turn's words or sound, a phrase that plays, the same thing working later, a new
// start), else after a while; one that ended the conversation keeps its words until dismissed, only no longer red. Every error is in the
// log of recent problems. And what is not an error: a server that does not answer (`unavailable`) and a notice that is offered again
// (`notice_retry`).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { storeFixture } from './store-fixture.mjs';

const plain = value => JSON.parse(JSON.stringify(value));
const settle = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
const read = name => readFile(new URL('../webui/' + name, import.meta.url), 'utf8');
const look = f => [f.store.error, f.store.chipState, f.mic.attrs.get('data-convo')];
async function talking(options = {}) {
    const f = await storeFixture(options);
    f.store.errorShowMs = 100000;
    await f.store.start();
    f.send = event => f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, ...event } });
    return f;
}

test('An error about one turn clears when a later turn is answered, in words or in sound, and the chip and orb are live again', async () => {
    for (const answer of [{ type: 'text.delta', text: 'Hi' }, { type: 'audio.begin', phrase_id: 'p' }, { type: 'audio.phrase', phrase_id: 'p', audio: 'UklGRg==' }]) {
        const f = await talking();
        f.send({ type: 'text.delta', turn_id: 'one', text: 'A' });
        f.send({ type: 'error', turn_id: 'two', message: 'The model failed.' });
        assert.deepEqual(look(f), ['The model failed.', 'error', 'error']);
        f.send({ ...answer, turn_id: 'two' });                                          // the same turn proves nothing yet
        f.send({ ...answer, turn_id: 'one' });                                          // nor does what is left of an earlier one
        assert.equal(f.store.error, 'The model failed.', answer.type);
        f.send({ ...answer, turn_id: 'three' });
        assert.equal(f.store.error, '', answer.type);
        assert.notEqual(f.store.chipState, 'error');
        assert.notEqual(f.mic.attrs.get('data-convo'), 'error');
        assert.equal(f.store.active, true);
        await f.store.stop();
    }
});

test('An error without a turn belongs to the reply it came with; a phrase of a later turn that plays clears it too', async () => {
    const f = await talking();
    f.send({ type: 'text.delta', turn_id: 'one', text: 'A' });
    f.send({ type: 'error', message: 'Something failed.' });
    f.send({ type: 'text.delta', turn_id: 'one', text: 'B' });
    assert.equal(f.store.error, 'Something failed.');
    f.send({ type: 'audio.begin', turn_id: 'two', phrase_id: 'p' });
    assert.equal(f.store.error, '');
    f.send({ type: 'error', turn_id: 'one', message: 'Late news about the first.' });
    f.audios[0].started();                                                              // the second turn's phrase plays
    assert.equal(f.store.error, '');
    await f.store.stop();
});

test('An undecided or unanswered utterance is that turn’s error: the reply that plays on does not clear it, the next answered turn does', async () => {
    const f = await talking();
    const audio = f.audios[0];
    f.send({ type: 'audio.begin', turn_id: 'reply', phrase_id: 'p' });
    audio.onStart();
    audio.onTurn('UklGRg==', {});
    await settle();
    const turn = f.requests.find(item => item.event === 'convo.candidate').data.turn_id;
    f.send({ type: 'candidate.state', turn_id: turn, sequence: 1, final: true, state: 'degraded', reason: 'The classifier timed out' });
    assert.match(f.store.error, /timed out, so this turn was not answered\./);
    f.send({ type: 'audio.begin', turn_id: 'reply', phrase_id: 'p2' });
    assert.match(f.store.error, /not answered/);
    f.send({ type: 'text.delta', turn_id: 'next', text: 'Yes?' });
    assert.equal(f.store.error, '');
    assert.equal(f.store.problems.at(-1).turn, turn);                                   // the log says which turn it was
    await f.store.stop();
});

test('Without any sign that things work, an error goes by itself after a while', async () => {
    const f = await talking();
    f.store.errorShowMs = 20;
    f.send({ type: 'error', turn_id: 'one', message: 'The model failed.' });
    assert.deepEqual(look(f), ['The model failed.', 'error', 'error']);
    await settle(40);
    assert.deepEqual(look(f), ['', 'listening', 'listening']);
    assert.equal(f.store.problems.length, 1);
    await f.store.stop();
    const { ERROR_SHOW_MS } = f.namespace;
    assert.equal(ERROR_SHOW_MS, 12000);
});

test('A second error restarts the time; the first one’s timer never clears it', async () => {
    const f = await talking();
    f.store.errorShowMs = 60;
    f.send({ type: 'error', turn_id: 'one', message: 'First.' });
    await settle(40);
    f.send({ type: 'error', turn_id: 'two', message: 'Second.' });
    await settle(40);
    assert.equal(f.store.error, 'Second.');
    await settle(40);
    assert.equal(f.store.error, '');
    await f.store.stop();
});

test('Why a conversation ended stays in the panel, no longer red after a while, until dismissed or the next start', async () => {
    let fails = true;
    const f = await storeFixture({ answer: event => (fails && event === 'convo.start' ? new Error('Provider unreachable') : null) });
    f.store.errorShowMs = 20;
    await f.store.start();
    assert.deepEqual(look(f), ['Provider unreachable', 'error', 'error']);
    await settle(40);
    assert.deepEqual(look(f), ['Provider unreachable', 'ready', 'ready']);
    assert.equal(f.store.statusLabel, 'Ready · microphone off');
    await f.store.refresh();                                                            // the status poll does not clear it
    assert.equal(f.store.error, 'Provider unreachable');
    // Another error replaces it for a while; then the reason the conversation ended is back, quiet.
    await f.store.shareCamera();
    assert.match(f.store.error, /cannot share a camera/);
    assert.equal(f.store.chipState, 'error');
    await settle(40);
    assert.deepEqual(look(f), ['Provider unreachable', 'ready', 'ready']);
    f.store.dismissError();
    assert.deepEqual(look(f), ['', 'ready', 'ready']);
    await f.store.start();
    assert.equal(f.store.error, 'Provider unreachable');
    fails = false;
    await f.store.start();
    assert.deepEqual([f.store.error, f.store.active], ['', true]);
    await settle(40);
    assert.equal(f.store.error, '');                                                    // and it does not come back
    await f.store.stop();
});

test('A conversation lost with its connection, or refused a chat switch, keeps its reason too', async () => {
    const f = await talking({ answer: event => (event === 'convo.retarget' ? new Error('That chat is gone') : null) });
    f.store.errorShowMs = 20;
    f.store.target = 'b';
    await f.store.retarget();
    assert.deepEqual([f.store.active, f.store.error], [false, 'That chat is gone']);
    await settle(40);
    assert.deepEqual(look(f), ['That chat is gone', 'ready', 'ready']);
});

test('What failed and works later clears its own error, and nothing else’s', async () => {
    let down = false;
    const f = await storeFixture({ api: () => { if (down) throw new Error('Agent Zero does not answer'); } });
    f.store.errorShowMs = 100000;
    await f.store.refresh();
    down = true;
    await f.store.refresh();
    assert.equal(f.store.error, 'Agent Zero does not answer');
    assert.equal(f.store.chipState, 'error');
    await f.store.refresh();
    assert.deepEqual(plain(f.store.problems.map(item => [item.message, item.count])), [['Agent Zero does not answer', 2]]);   // merged, not repeated
    down = false;
    await f.store.refresh();
    assert.deepEqual([f.store.error, f.store.chipState], ['', 'ready']);
    await f.store.shareCamera();
    assert.match(f.store.error, /camera/);
    await f.store.refresh(); await f.store.mountPanel();
    assert.match(f.store.error, /camera/);                                              // a status read says nothing about the camera
    f.store.dismissError();
    await f.store.start();
    f.audios[0].onError(new Error('A reply phrase could not be played.'));
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, type: 'text.delta', turn_id: 'later', text: 'Words' } });
    assert.match(f.store.error, /could not be played/);                                 // words are no proof that sound works
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, type: 'audio.begin', turn_id: 'later', phrase_id: 'p' } });
    f.audios[0].started();
    assert.equal(f.store.error, '');                                                    // a phrase that plays is
    await f.store.stop();
});

test('A playback acknowledgement that comes too late is no error', async () => {
    const f = await talking({ answer: event => (event === 'convo.playback' ? { ok: false, stale: true } : null) });
    f.send({ type: 'audio.begin', turn_id: 'one', phrase_id: 'p' });
    f.audios[0].started(); f.audios[0].done();
    await settle(5);
    assert.deepEqual([f.store.error, f.store.problems.length], ['', 0]);
    await f.store.stop();
});

test('The log keeps the last fifty problems with their time and turn, merges a repeated one, and can be cleared', async () => {
    const f = await talking();
    for (let n = 0; n < 60; n++) f.send({ type: 'error', turn_id: 't' + n, message: 'Problem ' + n });
    assert.equal(f.store.problems.length, 50);
    assert.deepEqual([f.store.problems[0].message, f.store.problems.at(-1).message, f.store.problems.at(-1).turn], ['Problem 10', 'Problem 59', 't59']);
    assert.equal(new Set(f.store.problems.map(item => item.id)).size, 50);
    assert.ok(Math.abs(f.store.problems.at(-1).at - Date.now()) < 5000);
    f.send({ type: 'error', turn_id: 'again', message: 'Problem 59' });
    assert.deepEqual([f.store.problems.length, f.store.problems.at(-1).count, f.store.problems.at(-1).turn], [50, 2, 'again']);
    f.store.noteProblem('Settings: x'.padEnd(900, 'x'));
    assert.equal(f.store.problems.at(-1).message.length, 400);                          // bounded
    f.store.noteProblem('');
    assert.equal(f.store.problems.at(-1).count, 1);
    f.audios[0].onMicrophone({ kind: 'ended', restart: true, message: 'The microphone stopped.' });
    assert.equal(f.store.problems.at(-1).message, 'The microphone stopped.');
    f.store.clearProblems();
    assert.deepEqual(plain(f.store.problems), []);
    await assert.rejects((async () => { const g = await talking({ answer: event => (event === 'convo.tune' ? new Error('HTTP 503') : null) });
        try { await g.store.tune('low', {}); } finally { assert.equal(g.store.problems.at(-1).message, 'HTTP 503'); assert.equal(g.store.error, ''); await g.store.stop(); } })(), /503/);
    await f.store.stop();
});

test('The panel: the message with a dismiss button, an alert only while it is news; the log in the diagnostics panel with Clear', async () => {
    const html = await read('main.html');
    assert.match(html, /<div class="convo-error-line" x-show="\$store\.convo\.error"><p class="convo-error" :role="\$store\.convo\.errorQuiet \? null : 'alert'" x-text="\$store\.convo\.error"><\/p><button type="button" class="button convo-icon convo-dismiss" @click="\$store\.convo\.dismissError\(\)" aria-label="Dismiss this message">/);
    const diagnostics = await read('diagnostics.html');
    assert.match(diagnostics, /<\/span>Recent problems<\/button>/);
    assert.match(diagnostics, /@click="\$store\.convo\?\.clearProblems\(\)">Clear</);
});

// ---- a server that does not answer
test('`unavailable` reads as waiting, never as an error, and clears on `ready`, on a reply’s words or sound, and when voice stops', async () => {
    const endings = [{ type: 'notice', kind: 'ready', message: '' }, { type: 'text.delta', turn_id: 't', text: 'Hi' }, { type: 'audio.begin', turn_id: 't', phrase_id: 'p' }, { type: 'audio.phrase', turn_id: 't', phrase_id: 'p', audio: 'UklGRg==' }];
    for (const ending of endings) {
        const toasts = [];
        const f = await talking({ globals: { justToast: text => toasts.push(text) } });
        f.send({ type: 'notice', kind: 'unavailable', message: 'The classifier at its address does not answer.' });
        assert.deepEqual([f.store.statusLabel, f.store.chipState, f.mic.attrs.get('data-convo')], ['Waiting for a server', 'loading', 'loading']);
        assert.equal(f.store.waiting, 'The classifier at its address does not answer.');
        assert.equal(f.store.error, '');
        f.send({ type: 'state', state: 'listening' }); f.send({ type: 'error', turn_id: 'x', message: 'Not answered.' });
        assert.equal(f.store.unavailable, 'The classifier at its address does not answer.');   // neither says that it answers again
        f.store.dismissError();
        f.send(ending);
        assert.deepEqual([f.store.unavailable, f.store.waiting], ['', ''], JSON.stringify(ending));
        assert.notEqual(f.store.chipState, 'loading');
        assert.ok(!toasts.length, 'nothing pops up');
        assert.ok(f.store.problems.some(item => /does not answer/.test(item.message)));        // it is in the log
        await f.store.stop();
    }
    const f = await talking();
    f.send({ type: 'notice', kind: 'loading', message: 'Loading the model…' });
    f.send({ type: 'notice', kind: 'unavailable', message: 'It stopped answering.' });
    assert.deepEqual([f.store.waiting, f.store.statusLabel], ['It stopped answering.', 'Waiting for a server']);   // the newest of the two
    f.send({ type: 'notice', kind: 'loading', message: 'Loading the model…' });
    assert.deepEqual([f.store.waiting, f.store.statusLabel], ['Loading the model…', 'Waiting for a model']);
    f.send({ type: 'notice', kind: 'ready', message: '' });                                    // `ready` also ends an ordinary wait
    assert.deepEqual([f.store.waiting, f.store.statusLabel], ['', 'Listening']);
    f.send({ type: 'notice', kind: 'unavailable' });
    assert.equal(f.store.unavailable, 'A server Convo needs does not answer.');
    await f.store.stop();
    assert.deepEqual([f.store.unavailable, f.store.chipState], ['', 'ready']);
});

test('`unavailable` shows while the conversation is still starting too', async () => {
    let finish;
    const f = await storeFixture({ answer: event => (event === 'convo.start' ? new Promise(resolve => { finish = () => resolve({}); }) : null) });
    const starting = f.store.start();
    await settle(10);
    f.handlers['convo.event']({ data: { type: 'notice', kind: 'unavailable', session_id: 's', message: 'The speech server does not answer.' } });
    assert.equal(f.store.waiting, 'The speech server does not answer.');
    f.handlers['convo.event']({ data: { type: 'notice', kind: 'ready', session_id: 's', message: '' } });
    assert.equal(f.store.waiting, '');
    finish(); await starting;
    assert.equal(f.store.active, true);
    await f.store.stop();
});

// ---- a notice that is offered again
test('`notice_retry` offers a finished task’s spoken notice again, and only that one', async () => {
    const jobs = ['one', 'two'].map(id => ({ id, session: 's', target: 'a', status: 'completed', prompt: id }));
    const f = await talking({ api: (_url, payload) => (payload.action === 'history' ? { events: [], jobs } : undefined) });
    const told = () => f.requests.filter(item => item.event === 'convo.notice').map(item => item.data.job_id);
    const offer = async () => { f.store.state = 'listening'; await f.store.history(); await settle(5); };
    await offer(); await offer(); await offer();
    assert.deepEqual(told(), ['one', 'two']);                                                  // each once
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 7, type: 'notice_retry', job_id: 'one' } });   // whatever reply it belonged to
    f.handlers['convo.event']({ data: { session_id: 'another', epoch: 0, type: 'notice_retry', job_id: 'two' } });
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, type: 'notice_retry' } });
    await offer(); await offer();
    assert.deepEqual(told(), ['one', 'two', 'one']);
    assert.equal(f.store.error, '');
    await f.store.stop();
});

test('A notice answered as busy is offered again; one whose retry arrives with its answer is not lost', async () => {
    let mode = 'busy';
    const jobs = [{ id: 'one', session: 's', target: 'a', status: 'completed', prompt: 'one' }];
    const f = await talking({ api: (_url, payload) => (payload.action === 'history' ? { events: [], jobs } : undefined), answer: event => {
        if (event !== 'convo.notice') return null;
        if (mode === 'busy') return { busy: true };
        if (mode === 'retry') f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, type: 'notice_retry', job_id: 'one' } });   // before the answer is read
        return {};
    } });
    const told = () => f.calls.filter(name => name === 'convo.notice').length;
    const offer = async () => { f.store.state = 'listening'; await f.store.history(); await settle(5); };
    await offer(); assert.equal(told(), 1);
    mode = 'retry'; await offer(); assert.equal(told(), 2);
    mode = 'ok'; await offer(); assert.equal(told(), 3);
    await offer(); assert.equal(told(), 3);
    await f.store.stop();
});
