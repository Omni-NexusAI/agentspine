// The panel's Steer: a field under the task, what it says it does, and what it shows afterwards.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { storeFixture } from './store-fixture.mjs';

const running = { id: 'j1', status: 'running', prompt: 'Write the weekly report', target: 'a', session: 's' };
const queued = { ...running, id: 'j2', status: 'queued' };

async function panel({ steering = true, jobs = [running, queued], steer = () => ({ ok: true, how: 'waiting' }) } = {}) {
    const posts = [];
    const f = await storeFixture({ api: async (_url, payload) => {
        if (payload.action === 'history') return { events: [], jobs, steering };
        if (payload.action === 'steer_job') { posts.push({ ...payload }); return steer(payload); }
        return undefined;
    } });
    f.store.target = 'a';
    await f.store.history();
    return { ...f, posts };
}

test('One field at a time, under its task; Escape and Close empty it', async () => {
    const { store } = await panel();
    store.openSteer(running);
    assert.equal(store.steerOpen, 'j1');
    store.steerText = 'half a thought';
    store.openSteer(queued);
    assert.deepEqual([store.steerOpen, store.steerText], ['j2', '']);
    store.openSteer(queued);
    assert.equal(store.steerOpen, '');
    store.openSteer(running); store.steerText = 'x'; store.closeSteer();
    assert.deepEqual([store.steerOpen, store.steerText], ['', '']);
});

test('Sending passes the words on and closes the field', async () => {
    const { store, posts } = await panel();
    store.openSteer(running);
    store.steerText = '  use euros  ';
    await store.steerJob(running);
    assert.deepEqual(posts, [{ action: 'steer_job', target: 'a', job_id: 'j1', text: 'use euros' }]);
    assert.deepEqual([store.steerOpen, store.steerText, store.steerBusy], ['', '', false]);
    await store.steerJob(running);                       // nothing typed: nothing sent
    assert.equal(posts.length, 1);
});

test('When it fails the words stay in the field and the panel says so', async () => {
    const { store } = await panel({ steer: () => { throw new Error('Job is not running in this process'); } });
    store.openSteer(running);
    store.steerText = 'use euros';
    await store.steerJob(running);
    assert.deepEqual([store.steerOpen, store.steerText, store.steerBusy], ['j1', 'use euros', false]);
    assert.match(store.error, /not running/);
});

test('The button says what it does: steering with the plugin, interrupting without, adding to a task that has not started', async () => {
    const on = (await panel()).store, off = (await panel({ steering: false })).store;
    assert.deepEqual([on.steerLabel(running), off.steerLabel(running), off.steerLabel(queued)], ['Steer', 'Interrupt', 'Steer']);
    assert.match(on.steerHint(running), /next step/);
    assert.match(off.steerHint(running), /interrupts/);
    assert.match(off.steerHint(queued), /before it starts/);
    for (const status of ['dispatching', 'cancel_requested', 'completed']) assert.equal(on.canSteer({ ...running, status }), false);
});

test('A task shows what became of its newest change', async () => {
    const { store } = await panel({ jobs: [{ ...running, change: 'waiting' }, { ...queued, change: 'added' }] });
    assert.deepEqual(store.activeJobs.map(job => store.changeState(job)), ['change waiting', 'change added']);
    assert.equal(store.changeState({ ...running, change: 'taken' }), 'change taken up');
    assert.equal(store.changeState(running), '');
    assert.equal(store.changeState({ ...running, change: 'something new' }), '');
});

test('The history tells a change and what became of it in words', async () => {
    const { describeEvent } = await import('../webui/view-helpers.js');
    const told = (kind, payload) => { const { role, label, text } = describeEvent({ kind, payload }); return [role, label, text]; };
    assert.deepEqual(told('job.steered', { how: 'waiting', text: 'use euros' }), ['activity', 'Change passed to the work in progress', 'use euros']);
    assert.deepEqual(told('job.steered', { how: 'added', text: 'x' }), ['activity', 'Change added to the task', 'x']);
    assert.deepEqual(told('job.steered', { how: 'interrupted', text: 'x' })[1], 'Change sent, interrupting the work');
    assert.deepEqual(told('job.steered', { how: 'later version', text: 'x' })[1], 'Change passed on');
    assert.deepEqual(told('job.steer_taken', { text: 'use euros' }), ['activity', 'Change taken up', 'use euros']);
    assert.deepEqual(told('job.steer_dropped', { text: 'use euros' })[1], 'Change not taken up');
});

test('A change made by voice refreshes the list', async () => {
    let reads = 0;
    const f = await storeFixture({ api: async (_url, payload) => { if (payload.action === 'history') reads++; return payload.action === 'history' ? { events: [], jobs: [], steering: true } : undefined; } });
    await f.store.start();
    const before = reads;
    f.handlers['convo.event']({ data: { type: 'job.steered', session_id: 's', epoch: 0, turn_id: 't', job_id: 'j1', how: 'waiting' } });
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.ok(reads > before);
    await f.store.stop();
});

test('The markup: a labelled field, real buttons, no prompt window', () => {
    const html = readFileSync(new URL('../webui/main.html', import.meta.url), 'utf8');
    const store = readFileSync(new URL('../webui/convo-store.js', import.meta.url), 'utf8');
    const form = html.slice(html.indexOf('<form class="convo-steer"'), html.indexOf('</form>'));
    assert.match(form, /<input type="text"[^>]*aria-label="What should change about this task"/);
    assert.match(form, /@keydown\.escape\.stop\.prevent/);
    assert.match(form, /<button type="submit"[^>]*:disabled=/);
    assert.match(html, /:aria-expanded="\$store\.convo\.steerOpen === job\.id/);
    assert.doesNotMatch(store, /window\.prompt\(/);
});
