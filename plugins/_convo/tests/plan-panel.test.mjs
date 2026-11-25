// Planning mode in the panel: the Plan button and hotkey, the plan that writes itself and can be edited, an edit Convo wrote under
// meanwhile, going on, handing over, and what the journal and the task list say. The server decides (tests/test_planning.py).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { storeFixture } from './store-fixture.mjs';
import { settingsMarkup } from './settings-markup.mjs';
import { PLANNING, actionFor, actionLabel, comboOwner, DEFAULT_HOTKEYS } from '../webui/hotkeys.js';
import { describeEvent, foldTurns, planState } from '../webui/view-helpers.js';
import { defaults } from '../webui/defaults.js';

const open = { id: 'p', topic: 'a garden shed', text: 'Goal:\n- dry', status: 'open', revision: 3, by: 'convo', on: true, waiting: [] };
const kept = { ...open, status: 'kept', on: false };
const job = { id: 'j1', status: 'queued', prompt: 'Write the report', target: 'a', session: 's' };

// `server`: what the server answers per action; `plan`: what the history carries (changeable).
async function panel({ plan = null, jobs = [], server = {}, ...more } = {}) {
    const posts = [], held = { plan };
    const f = await storeFixture({ ...more, api: async (_url, payload) => {
        if (payload.action === 'history') return { events: [], jobs, steering: true, plan: held.plan };
        if (payload.action?.startsWith('plan_')) {
            posts.push({ ...payload });
            const answer = server[payload.action];
            const result = typeof answer === 'function' ? await answer(payload) : answer;
            if (result?.plan !== undefined) held.plan = result.plan;
            return result;
        }
        return undefined;
    } });
    f.store.target = 'a';
    await f.store.refresh();
    await f.store.history();
    return { ...f, posts, held };
}

test('Plan starts planning, End planning ends it, and the plan is shown when it begins', async () => {
    const { store, posts } = await panel({ server: { plan_start: { ok: true, plan: open }, plan_end: { ok: true, plan: kept } } });
    assert.deepEqual([store.planning, store.plan, store.planOpen], [false, null, false]);
    assert.equal(await store.togglePlanning(), true);
    assert.deepEqual([store.planning, store.planOpen, store.planText, store.planState], [true, true, 'Goal:\n- dry', 'Planning']);
    assert.match(store.note, /without your yes/);
    assert.equal(await store.togglePlanning(), true);
    assert.deepEqual([store.planning, store.planState], [false, 'Plan kept']);
    assert.deepEqual(posts.map(post => [post.action, post.target, post.id]), [['plan_start', 'a', undefined], ['plan_end', 'a', 'p']]);
});

test('A question from before planning goes with it', async () => {
    const { store, handlers } = await panel({ server: { plan_start: { ok: true, plan: open } } });
    await store.start();
    handlers['convo.event']({ data: { type: 'action.pending', session_id: 's', epoch: 0, id: 'q', label: 'Hand over a task', summary: '', seconds: 40 } });
    assert.equal(store.pendingAction.id, 'q');
    await store.togglePlanning();
    assert.equal(store.pendingAction, null);
    await store.stop();
});

test('A plan the voice agent began appears by itself and follows what Convo writes', async () => {
    const { store, handlers, held } = await panel();
    await store.start();
    held.plan = open;
    handlers['convo.event']({ data: { type: 'plan.changed', session_id: 's', epoch: 0, turn_id: 't', plan_id: 'p', on: true } });
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.deepEqual([store.planning, store.planOpen, store.planText], [true, true, 'Goal:\n- dry']);
    store.planOpen = false;                                                       // closed by the user: it stays closed
    held.plan = { ...open, text: 'Goal:\n- dry and light', revision: 4 };
    await store.history();
    assert.deepEqual([store.planOpen, store.planText, store.planBase], [false, 'Goal:\n- dry and light', 4]);
    held.plan = null;
    await store.history();
    assert.deepEqual([store.plan, store.planText], [null, '']);
    await store.stop();
});

test('An edit is saved against the plan it was made on, and typing it back is no edit', async () => {
    const { store, posts } = await panel({ plan: open, server: { plan_save: post => ({ ok: true, plan: { ...open, text: post.text, revision: 4, by: 'user' } }) } });
    store.editPlan('Goal:\n- dry and light');
    assert.deepEqual([store.planDirty, store.planBase], [true, 3]);
    store.editPlan('Goal:\n- dry');
    assert.equal(store.planDirty, false);
    store.editPlan('Goal:\n- mine');
    assert.equal(await store.savePlan(), true);
    assert.deepEqual(posts.map(post => [post.action, post.id, post.text, post.revision]), [['plan_save', 'p', 'Goal:\n- mine', 3]]);
    assert.deepEqual([store.planDirty, store.planText, store.planNote, store.planBase], [false, 'Goal:\n- mine', 'Saved.', 4]);
});

test('What the user is typing is never replaced, and the user decides when Convo wrote meanwhile', async () => {
    const newer = { ...open, text: 'Goal:\n- dry\n- big', revision: 4 };
    const { store, posts, held } = await panel({ plan: open, server: { plan_save: post => post.revision === 4 ? { ok: true, plan: { ...newer, text: post.text, revision: 5 } }
        : { ok: false, code: 'conflict', error: 'The plan was changed meanwhile', plan: newer } } });
    store.editPlan('Goal:\n- mine');
    held.plan = newer;
    await store.history();                                                        // the poll brings Convo's new one
    assert.deepEqual([store.planText, store.planBase, store.plan.revision], ['Goal:\n- mine', 3, 4]);
    assert.equal(await store.savePlan(), false);
    assert.deepEqual([store.planClash, store.planText, store.planDirty], [true, 'Goal:\n- mine', true]);
    assert.match(store.planNote, /while you were editing/);
    assert.equal(await store.savePlan({ mine: true }), true);
    assert.deepEqual(posts.map(post => post.revision), [3, 4]);
    assert.deepEqual([store.planClash, store.planText], [false, 'Goal:\n- mine']);
});

test('Or takes the new one', async () => {
    const newer = { ...open, text: 'Goal:\n- dry\n- big', revision: 4 };
    const { store } = await panel({ plan: open, server: { plan_save: { ok: false, code: 'conflict', error: 'changed', plan: newer } } });
    store.editPlan('Goal:\n- mine');
    await store.savePlan();
    store.revertPlan();
    assert.deepEqual([store.planClash, store.planDirty, store.planText, store.planBase], [false, false, newer.text, 4]);
});

test('Ending or handing over saves an edit first, and stops when it could not be saved', async () => {
    const conflict = { ok: false, code: 'conflict', error: 'changed', plan: { ...open, revision: 9 } };
    for (const [save, expected] of [[{ ok: true, plan: { ...open, text: 'mine', revision: 4 } }, ['plan_save', 'plan_end']], [conflict, ['plan_save']]]) {
        const { store, posts } = await panel({ plan: open, server: { plan_save: save, plan_end: { ok: true, plan: kept } } });
        store.editPlan('mine');
        await store.togglePlanning();
        assert.deepEqual(posts.map(post => post.action), expected);
    }
    const { store, posts } = await panel({ plan: open, server: { plan_save: conflict } });
    store.editPlan('mine');
    assert.equal(await store.handOverPlan(), false);
    assert.deepEqual(posts.map(post => post.action), ['plan_save']);
    assert.equal(store.planText, 'mine');
});

test('Hand over, go on, new plan, discard and copy', async () => {
    const handed = { ...kept, status: 'handed' };
    let confirmed = true, copied = '';
    const { store, posts } = await panel({ plan: kept, globals: { window: { addEventListener() {}, confirm: () => confirmed }, navigator: { clipboard: { async writeText(text) { copied = text; } } } },
        server: { plan_hand_over: { ok: true, plan: handed, job: {} }, plan_start: { ok: true, plan: { ...open, id: 'new' } }, plan_drop: { ok: true, plan: null } } });
    assert.equal(await store.copyPlan(), true);
    assert.equal(copied, 'a garden shed\n\nGoal:\n- dry');
    await store.goOnPlanning();
    assert.deepEqual(posts.at(-1), { action: 'plan_start', target: 'a', id: 'p' });                        // the same one again
    store.takePlan(kept);
    assert.equal(await store.handOverPlan(), true);
    assert.deepEqual([posts.at(-1), store.planState, store.planNote], [{ action: 'plan_hand_over', target: 'a', id: 'p', chat: '' }, 'Plan handed over', 'Handed over as a task.']);
    await store.goOnPlanning();
    assert.deepEqual(posts.at(-1), { action: 'plan_start', target: 'a', new: true });                      // a handed one is done: a new one
    confirmed = false;
    assert.equal(await store.discardPlan(), false);
    confirmed = true;
    store.takePlan(kept);
    assert.equal(await store.discardPlan(), true);
    assert.equal(store.plan, null);
});

test('What is typed while an edit is being saved goes on as the next edit', async () => {
    let release;
    const { store, posts } = await panel({ plan: open, server: { plan_save: payload => new Promise(resolve => { release = () => resolve({ ok: true, plan: { ...open, text: payload.text, revision: payload.revision + 1 } }); }) } });
    store.editPlan('mine');
    const saving = store.savePlan();
    await new Promise(resolve => setTimeout(resolve, 0));
    store.editPlan('mine, and more');
    release();
    assert.equal(await saving, false);                                             // (what waits for a save does not go on without the rest)
    assert.deepEqual([store.planText, store.planDirty, store.planNote], ['mine, and more', true, '']);
    const again = store.savePlan();
    await new Promise(resolve => setTimeout(resolve, 0));
    release();
    assert.equal(await again, true);
    assert.deepEqual(posts.map(post => [post.text, post.revision, post.was]), [['mine', 3, 'Goal:\n- dry'], ['mine, and more', 4, 'mine']]);
    assert.deepEqual([store.planText, store.planDirty, store.planNote], ['mine, and more', false, 'Saved.']);
});

test('An unsaved edit is saved to its own plan when another one takes its place, or kept to be copied', async () => {
    const other = { ...open, id: 'q', topic: 'a party', text: 'Goal:\n- fun' };
    const tick = () => new Promise(resolve => setTimeout(resolve, 5));
    for (const [answer, lost] of [[{ ok: true, plan: other }, null], [{ ok: false, code: 'conflict', error: 'changed', plan: other }, 'mine'], [() => { throw new Error('no answer'); }, 'mine']]) {
        let copied = '';
        const { store, posts, held } = await panel({ plan: open, globals: { navigator: { clipboard: { async writeText(text) { copied = text; } } } }, server: { plan_save: answer } });
        store.editPlan('mine');
        held.plan = other;                                                         // Convo began another one by voice
        await store.history(); await tick();
        assert.deepEqual([store.plan.id, store.planText, store.planDirty], ['q', 'Goal:\n- fun', false]);
        assert.deepEqual([posts[0].action, posts[0].id, posts[0].text, posts[0].was], ['plan_save', 'p', 'mine', 'Goal:\n- dry']);
        assert.equal(store.planLost?.text ?? null, lost);
        if (!lost) continue;
        assert.equal(store.planLost.topic, 'a garden shed');
        assert.equal(await store.copyLost(), true);
        assert.deepEqual([copied, store.planLost], ['mine', null]);
    }
    const { store, posts } = await panel({ plan: open, server: { plan_save: { ok: true, plan: open } } });
    store.editPlan('mine');
    store.target = 'b';                                                            // another chat is selected
    await store.history(); await tick();
    assert.deepEqual([posts[0].action, posts[0].target, posts[0].text], ['plan_save', 'a', 'mine']);
});

test('One answer without a plan does not take an unsaved edit away', async () => {
    const { store, held } = await panel({ plan: open });
    store.editPlan('mine');
    held.plan = null;
    await store.history();
    assert.deepEqual([store.planText, store.planDirty], ['mine', true]);
    store.revertPlan();
    await store.history();
    assert.equal(store.plan, null);
});

test('The hotkey looks first when nobody was looking, and a failure is heard', async () => {
    const said = [];
    const { store, posts, held } = await panel({ plan: open, globals: {}, server: { plan_start: { ok: true, plan: { ...open, id: 'b1' } }, plan_end: { ok: false, code: 'invalid', error: 'That plan is no longer there' } } });
    store.settings = { ...store.settings, enabled: true };
    store.panelOpen = false;
    held.plan = null;                                                              // another chat, which is planning nothing: the first press starts there
    assert.equal(store.hotkey('planning'), true);
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.deepEqual(posts.map(post => post.action), ['plan_start']);
    assert.equal(store.planning, true);
    const failing = await panel({ plan: open, server: { plan_end: { ok: false, code: 'invalid', error: 'That plan is no longer there' } } });
    failing.store.panelOpen = false;
    const note = [];
    Object.defineProperty(failing.store, 'planFailed', { value: () => note.push(failing.store.planNote) });
    await failing.store.togglePlanning();
    assert.deepEqual(note, ['That plan is no longer there']);
    void said;
});

test('Going on and opening an earlier plan save an edit first; what stood there before can be put back', async () => {
    const earlier = [{ id: 'e', topic: 'a trip', updated: 1 }];
    const full = { ...kept, earlier, previous: 'Goal:\n- as I wrote it' };
    const { store, posts } = await panel({ plan: full, server: { plan_save: payload => ({ ok: true, plan: { ...full, text: payload.text, revision: 4 } }), plan_start: { ok: true, plan: open } } });
    assert.deepEqual(Array.from(store.planEarlier, item => item.topic), ['a trip']);
    store.editPlan('mine');
    await store.goOnPlanning();
    assert.deepEqual(posts.map(post => post.action), ['plan_save', 'plan_start']);
    store.takePlan(full);
    await store.openEarlier('e');
    assert.deepEqual(posts.at(-1), { action: 'plan_start', target: 'a', id: 'e' });
    store.takePlan(full);
    store.putBack();
    assert.deepEqual([store.planText, store.planDirty], ['Goal:\n- as I wrote it', true]);      // shown first, saved only by the user
    assert.match(store.planNote, /before Convo changed it/);
    await store.savePlan();
    assert.deepEqual([posts.at(-1).action, posts.at(-1).text], ['plan_save', 'Goal:\n- as I wrote it']);
});

test('Discarding takes what was typed with it, and leaves it when it did not work', async () => {
    const globals = { window: { addEventListener() {}, confirm: () => true } };
    const gone = await panel({ plan: kept, globals, server: { plan_drop: { ok: true, plan: null } } });
    gone.store.editPlan('mine');
    assert.equal(await gone.store.discardPlan(), true);
    assert.deepEqual([gone.store.plan, gone.store.planDirty, gone.store.planLost], [null, false, null]);
    const failed = await panel({ plan: kept, globals, server: { plan_drop: () => { throw new Error('no answer'); } } });
    failed.store.editPlan('mine');
    assert.equal(await failed.store.discardPlan(), false);
    assert.deepEqual([failed.store.planText, failed.store.planDirty], ['mine', true]);
});

test('A question about a plan goes with the plan', async () => {
    const handed = { ...kept, status: 'handed' };
    const { store, handlers } = await panel({ plan: kept, globals: { window: { addEventListener() {}, confirm: () => true } }, server: { plan_hand_over: { ok: true, plan: handed, job: {} }, plan_drop: { ok: true, plan: null } } });
    await store.start();
    const ask = planId => handlers['convo.event']({ data: { type: 'action.pending', session_id: 's', epoch: 0, id: 'q', label: 'Hand over the plan', summary: 'a garden shed', seconds: 40, plan_id: planId } });
    ask('p');
    await store.handOverPlan();
    assert.equal(store.pendingAction, null);
    store.takePlan(kept); ask('p');
    await store.discardPlan();
    assert.equal(store.pendingAction, null);
    store.takePlan(kept); ask('p');
    store.takePlan(handed);                                                        // handed over on another page
    assert.equal(store.pendingAction, null);
    store.takePlan(kept); ask('another');
    await store.handOverPlan();
    assert.equal(store.pendingAction.id, 'q');                                     // a question about another plan stays
    await store.stop();
});

test('Copying that is not allowed says what to do instead', async () => {
    const { store } = await panel({ plan: kept, globals: { navigator: {} } });
    assert.equal(await store.copyPlan(), false);
    assert.match(store.planNote, /select the text/);
});

test('A refusal is said in the plan, a failure in the error line, and nothing is lost', async () => {
    const refused = await panel({ plan: open, server: { plan_hand_over: { ok: false, code: 'invalid', error: 'Nothing is written down in this plan yet', plan: open } } });
    assert.equal(await refused.store.handOverPlan(), false);
    assert.equal(refused.store.planNote, 'Nothing is written down in this plan yet');
    assert.equal(refused.store.error, '');
    const failed = await panel({ plan: open, server: { plan_save: () => { throw new Error('The server did not answer'); } } });
    failed.store.editPlan('mine');
    assert.equal(await failed.store.savePlan(), false);
    assert.match(failed.store.error, /did not answer/);
    assert.deepEqual([failed.store.planText, failed.store.planDirty, failed.store.planBusy], ['mine', true, false]);
});

test('A conversation across chats hands the plan to the chat selected for work', async () => {
    const { store, posts } = await panel({ plan: open, server: { plan_hand_over: { ok: true, plan: { ...kept, status: 'handed' } } } });
    store.target = 'convo:global'; store.work = 'b';
    await store.handOverPlan();
    assert.deepEqual([posts[0].target, posts[0].chat], ['convo:global', 'b']);
});

test('Another chat has its own plan', async () => {
    const { store, held } = await panel({ plan: open });
    store.editPlan('half an edit');
    store.target = 'b'; held.plan = { ...open, id: 'q', text: 'other' };
    await store.history();
    assert.deepEqual([store.planText, store.planDirty], ['other', false]);
});

test('A task that waits says so', async () => {
    const { store } = await panel({ plan: { ...open, waiting: ['j1'] }, jobs: [job, { ...job, id: 'j2' }, { ...job, id: 'j3', status: 'cancel_requested' }] });
    assert.deepEqual(store.jobs.map(item => store.jobState(item)), ['waits while planning', 'queued', 'cancel requested']);
    assert.equal(store.planWaiting, 'One task waits until planning is over.');
    store.takePlan({ ...open, waiting: ['j1', 'j2'] });
    assert.equal(store.planWaiting, '2 tasks wait until planning is over.');
    store.takePlan({ ...kept, waiting: ['j1'] });
    assert.deepEqual([store.planWaiting, store.jobState(job)], ['', 'queued']);
});

test('The journal reads as words, and bookkeeping stays out of the way', () => {
    const told = (kind, payload) => { const { role, label, text } = describeEvent({ kind, payload }); return [role, label, text]; };
    assert.deepEqual(told('plan.started', { how: 'new', topic: 'a shed' }), ['activity', 'Planning started', 'a shed']);
    assert.deepEqual(told('plan.started', { how: 'again', topic: '' }), ['activity', 'Planning goes on', '']);
    assert.deepEqual(told('plan.ended', { how: 'kept', topic: 'a shed' })[1], 'Planning ended · plan kept');
    assert.deepEqual(told('plan.ended', { how: 'handed' })[1], 'Plan handed over');
    assert.deepEqual(told('plan.ended', { how: 'a later one' })[1], 'Planning ended');
    const events = ['plan.started', 'plan.edited', 'plan.resumed', 'plan.ended'].map((kind, index) => ({ seq: index + 1, kind, payload: {}, turn: '', time: index }));
    assert.deepEqual(foldTurns(events).map(event => event.kind), ['plan.started', 'plan.ended']);
    assert.equal(planState({ status: 'something new' }), '');
});

test('A card that opens above the transcript does not push the reader off its end', async () => {
    const watched = [];
    const { store } = await panel({ globals: { ResizeObserver: class { constructor(run) { this.run = run; } observe(element) { watched.push({ run: this.run, element }); } disconnect() { this.gone = true; } } } });
    const element = { scrollHeight: 1000, clientHeight: 400, scrollTop: 600 };
    store.bindTranscript(element);
    await new Promise(resolve => setTimeout(resolve, 10));
    element.scrollTop = 600; element.clientHeight = 150;                          // the plan opens: the transcript is shorter, nothing scrolled
    watched[0].run();
    assert.equal(element.scrollTop, 1000);                                        // (the browser clamps it to the end)
    element.scrollTop = 100; store.onScroll(element);                             // a reader who went up is left alone
    element.clientHeight = 400;
    watched[0].run();
    assert.equal(element.scrollTop, 100);
    store.unbindTranscript(element);
    store.jumpToLatest();
    watched[0].run();
    assert.equal(element.scrollTop, 100);                                         // and after it is gone nothing is touched
});

test('A scroll told about after the layout changed is not the reader turning back; their own move up is', async () => {
    const tick = () => new Promise(resolve => setTimeout(resolve, 10));
    const { store } = await panel({ globals: { ResizeObserver: class { observe() {} disconnect() {} } } });
    // As a browser keeps it: never past the end.
    const element = { scrollHeight: 1000, clientHeight: 400, top: 0, get scrollTop() { return this.top; }, set scrollTop(value) { this.top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); } };
    store.bindTranscript(element); await tick();
    assert.equal(element.scrollTop, 600);
    element.clientHeight = 150;                                                    // a card opened before the event of that move arrived
    store.onScroll(element); store.onScroll(element); await tick();                // (two events before it could be put right)
    assert.deepEqual([store.pinned, element.scrollTop], [true, 850]);              // still at the end
    element.clientHeight = 400; element.scrollTop = 850;                           // the card closes: the browser moves it back ...
    element.scrollHeight = 1300; store.onScroll(element); await tick();            // ... and an entry is added before the event arrives
    assert.deepEqual([store.pinned, store.unseen, element.scrollTop], [true, 0, 900]);
    element.scrollTop = 700; store.onScroll(element); await tick();                // the reader goes up
    assert.deepEqual([store.pinned, element.scrollTop], [false, 700]);
    element.scrollHeight = 1600; store.onScroll(element); await tick();            // what is added now leaves them alone
    assert.deepEqual([store.pinned, element.scrollTop], [false, 700]);
    element.scrollTop = 1200; store.onScroll(element);                             // and at the end again they follow it
    assert.equal(store.pinned, true);
    // Going up while a reply is being written (entries grow between two events): the next event in the same layout is theirs.
    element.scrollHeight = 1700; element.scrollTop = 1190; store.onScroll(element);
    element.scrollTop = 1000; store.onScroll(element);
    assert.equal(store.pinned, false);
});

test('The hotkey is a setting of its own, off until it is set, and starts and ends planning', async () => {
    assert.equal(defaults.planning_hotkey, '');
    assert.ok(!(PLANNING in DEFAULT_HOTKEYS));                                    // an older Convo refuses a table with a key it does not know
    const press = { code: 'KeyP', key: 'p', altKey: true, ctrlKey: false, shiftKey: false, metaKey: false, getModifierState: () => false };
    assert.equal(actionFor(press, { hotkeys: DEFAULT_HOTKEYS, planning_hotkey: 'Alt+P' }), 'planning');
    assert.equal(actionFor(press, { hotkeys: { ...DEFAULT_HOTKEYS, open_panel: 'Alt+P' }, planning_hotkey: 'Alt+P' }), 'open_panel');   // the table comes first
    assert.equal(actionFor(press, { hotkeys: DEFAULT_HOTKEYS, planning_hotkey: '' }), '');
    assert.equal(comboOwner(DEFAULT_HOTKEYS, '', 'mute', 'Alt+P', 'Alt+P'), PLANNING);
    assert.equal(comboOwner(DEFAULT_HOTKEYS, '', PLANNING, 'Alt+P', 'Alt+P'), '');
    assert.match(actionLabel(PLANNING), /Planning/);
    const { store, posts } = await panel({ server: { plan_start: { ok: true, plan: open } } });
    assert.equal(store.hotkey('planning'), true);
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(posts[0].action, 'plan_start');
    store.settings = { ...store.settings, enabled: false };
    assert.equal(store.hotkey('planning'), false);
    assert.match(await settingsMarkup(), /config\.planning_hotkey/);
});

test('The hotkey row records into its own setting, and the other rows refuse its combination', async () => {
    const { hostStubs, loadModule } = await import('./vm-loader.mjs');
    const { namespace } = await loadModule('webui/setup-store.js', { bindings: hostStubs({ '/js/api.js': { callJsonApi: async () => ({ ok: true, config: JSON.parse(JSON.stringify(defaults)), revision: 'r', readiness: [], credentials: {} }) } }), globals: { window: { confirm: () => true } } });
    const setup = namespace.store, listeners = [];
    await setup.load();
    const page = { addEventListener: (type, fn) => listeners.push([type, fn]), removeEventListener: (type, fn) => { const at = listeners.findIndex(([t, f]) => t === type && f === fn); if (at >= 0) listeners.splice(at, 1); } };
    const press = (code, mods) => { const event = { code, key: code.slice(-1).toLowerCase(), altKey: mods.includes('Alt'), ctrlKey: false, shiftKey: mods.includes('Shift'), metaKey: false, getModifierState: () => false, preventDefault() {}, stopImmediatePropagation() {} };
        for (const [type, fn] of [...listeners]) if (type === 'keydown') fn(event); };
    assert.equal(setup.hotkeyText(PLANNING), 'Off');
    setup.hotkeyRecord(PLANNING, page); press('KeyM', 'Alt Shift');                                // mute has it
    assert.match(setup.hotkeyNote(PLANNING), /already used for/);
    press('KeyP', 'Alt');
    assert.deepEqual([setup.config.planning_hotkey, setup.dirty, 'planning_hotkey' in setup.config.hotkeys], ['Alt+P', true, false]);
    setup.hotkeyRecord('open_panel', page); press('KeyP', 'Alt');
    assert.match(setup.hotkeyNote('open_panel'), /Planning mode on or off/);
    setup.hotkeyCancel();
    assert.equal(setup.hotkeyReset(PLANNING), true);
    assert.equal(setup.config.planning_hotkey, '');
});

test('The markup: a labelled button, a labelled field that is not a second scroller, real buttons', () => {
    const html = readFileSync(new URL('../webui/main.html', import.meta.url), 'utf8');
    const card = html.slice(html.indexOf('<section class="convo-plan"'), html.indexOf('</section>', html.indexOf('<section class="convo-plan"')));
    assert.match(card, /:aria-expanded=/);
    assert.match(card, /aria-controls="convo-plan-body"/);
    assert.match(card, /<textarea[^>]*aria-label="The plan, as text"/);
    assert.match(card, /role="status"/);
    assert.doesNotMatch(card, /x-model|<details|onclick=/);
    assert.equal((html.match(/overflow-y:auto/g) || []).length, 1);
    for (const button of card.match(/<button[^>]*>/g)) assert.match(button, /type="button"/);
    assert.match(html, /<button type="button" class="button convo-plan-button"[^>]*:aria-pressed=/);
});
