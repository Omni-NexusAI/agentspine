// The panel's Reasoning and Permissions menus: what each shows for what the model takes, that choosing sends exactly that, that Plan is
// planning mode and Ask or Allow end it, and the kinds of work answered "always". The server decides (tests/test_voice_modes.py).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { storeFixture } from './store-fixture.mjs';

const open = { id: 'p', topic: 'a garden shed', text: 'Goal:\n- dry', status: 'open', revision: 3, by: 'convo', on: true, waiting: [] };
const kept = { ...open, status: 'kept', on: false };
const plain = value => JSON.parse(JSON.stringify(value));             // (the store's arrays come from its own realm)
const MODES = { permission: 'ask', reasoning: 'auto', chosen: false, allowed: [], reasoning_offer: { measured: true, can: true, graded: true } };

async function panel({ modes = MODES, plan = null, server = {} } = {}) {
    const posts = [], held = { modes, plan };
    const f = await storeFixture({ api: async (_url, payload) => {
        if (payload.action === 'history') return { events: [], jobs: [], steering: true, plan: held.plan, modes: held.modes };
        if (payload.action === 'voice_mode_set' || payload.action === 'allowed_forget' || payload.action?.startsWith('plan_')) {
            posts.push({ ...payload });
            const answer = server[payload.action];
            const result = typeof answer === 'function' ? await answer(payload) : answer;
            if (result?.modes !== undefined) held.modes = result.modes;
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

test('The menus show this conversation\'s choice and open one at a time', async () => {
    const { store } = await panel();
    assert.deepEqual([store.reasoningLabel, store.permissionLabel, store.permissionIcon], ['Auto', 'Ask when unsure', 'shield']);
    store.toggleMenu('reasoning');
    assert.equal(store.menu, 'reasoning');
    store.toggleMenu('permission');
    assert.equal(store.menu, 'permission');
    store.closeMenu();
    assert.equal(store.menu, '');
});

test('Reasoning offers what the model takes, says so, and sends only the level chosen', async () => {
    for (const [offer, disabled, note] of [
        [{ measured: true, can: true, graded: true }, [], /first words come later/],
        [{ measured: true, can: true, graded: false }, [], /on or off only/],
        [{ measured: true, can: false, graded: false }, ['low', 'medium', 'high'], /first words come later/],
        [{ measured: false, can: false, graded: false }, [], /Not checked yet/],
    ]) {
        const { store, posts } = await panel({ modes: { ...MODES, reasoning_offer: offer }, server: { voice_mode_set: payload => ({ ok: true, modes: { ...MODES, reasoning: payload.reasoning, reasoning_offer: offer } }) } });
        assert.deepEqual(plain(store.reasoningItems.map(item => item.id)), ['auto', 'off', 'low', 'medium', 'high']);
        assert.deepEqual(plain(store.reasoningItems.filter(item => item.disabled).map(item => item.id)), disabled);
        assert.match(store.reasoningNote, note);
        assert.equal(await store.setReasoning('high'), disabled.length === 0);
        assert.equal(await store.setReasoning('off'), true);
        assert.deepEqual(posts.map(post => [post.action, post.target, post.reasoning]),
                         [...(disabled.length ? [] : [['voice_mode_set', 'a', 'high']]), ['voice_mode_set', 'a', 'off']]);
        assert.equal(store.reasoningLabel, 'Off');
    }
});

test('Plan begins planning; Ask or Allow end it and are kept for the conversation', async () => {
    const { store, posts } = await panel({ server: {
        plan_start: { ok: true, plan: open }, plan_end: { ok: true, plan: kept },
        voice_mode_set: payload => ({ ok: true, modes: { ...MODES, permission: payload.permission } }) } });
    assert.equal(await store.setPermission('plan'), true);
    assert.deepEqual([store.planning, store.permissionLabel, store.permissionIcon], [true, 'Plan', 'edit_note']);
    assert.deepEqual(plain(store.permissionItems.filter(item => item.on).map(item => item.id)), ['plan']);
    assert.equal(await store.setPermission('allow'), true);
    assert.deepEqual([store.planning, store.permissionLabel], [false, 'Allow without asking']);
    assert.deepEqual(posts.map(post => [post.action, post.permission ?? post.fresh ?? post.id]),
                     [['plan_start', true], ['plan_end', 'p'], ['voice_mode_set', 'allow']]);
    assert.equal(await store.setPermission('everything'), false);                 // nothing else exists
});

test('Leaving planning that could not end keeps the permission as it was', async () => {
    const { store, posts } = await panel({ plan: open, server: { plan_end: { ok: false, code: 'invalid', error: 'no', plan: open } } });
    assert.equal(await store.setPermission('ask'), false);
    assert.deepEqual(posts.map(post => post.action), ['plan_end']);
    assert.equal(store.permissionLabel, 'Plan');
});

test('A new plan from the menu keeps the one in hand; a remembered kind can be asked about again', async () => {
    const remembered = { ...MODES, allowed: [{ tool: 'delegate_task', label: 'Hand over a task' }] };
    const { store, posts } = await panel({ plan: open, modes: remembered, server: {
        plan_start: { ok: true, plan: { ...open, id: 'q', topic: '' } }, allowed_forget: { ok: true, modes: MODES } } });
    assert.equal(await store.newPlan(), true);
    assert.equal(await store.forgetAllowed('delegate_task'), true);
    assert.deepEqual(posts.map(post => [post.action, post.new ?? post.tool]), [['plan_start', true], ['allowed_forget', 'delegate_task']]);
    assert.deepEqual(plain(store.modes.allowed), []);
});

test('A refusal says why and changes nothing', async () => {
    const { store } = await panel({ server: { voice_mode_set: { ok: false, code: 'invalid', error: 'Unknown reasoning level', modes: MODES } } });
    assert.equal(await store.setReasoning('high'), false);
    assert.equal(store.modesNote, 'Unknown reasoning level');
    assert.equal(store.reasoningLabel, 'Auto');
});

test('The markup: two menu buttons beside the chat name, real buttons, no second scroller, nothing in the icon row', () => {
    const html = readFileSync(new URL('../webui/main.html', import.meta.url), 'utf8');
    const markup = html.slice(0, html.indexOf('<style>')), style = html.slice(html.indexOf('<style>'));
    const line = markup.slice(markup.indexOf('<div class="convo-target"'), markup.indexOf('<div class="convo-preview"'));
    for (const name of ['reasoning', 'permission']) {
        assert.match(line, new RegExp(`<button type="button" class="btn-icon-action convo-menu-btn" data-menu="${name}"[^>]*aria-haspopup="menu"[^>]*aria-controls="convo-${name}-menu"`));
        assert.match(line, new RegExp(`id="convo-${name}-menu" role="menu"`));
    }
    assert.match(line, /role="menuitemradio"[^>]*:aria-checked=/);
    assert.match(line, /@keydown\.escape="\$store\.convo\.closeMenu\(\$event\)"/);
    for (const button of line.match(/<button[^>]*>/g)) assert.match(button, /type="button"/);
    const icons = markup.slice(markup.indexOf('<div class="convo-icons"'), markup.indexOf('</div>', markup.indexOf('<div class="convo-icons"')));
    assert.doesNotMatch(icons, /plan|reasoning|permission/i);                     // the mute, voice, camera and screen row stays as it is
    assert.doesNotMatch(markup, /convo-plan-button/);
    assert.equal((style.match(/[^{}]+\{[^{}]*overflow-y:auto[^{}]*\}/g) || []).length, 1);
});
