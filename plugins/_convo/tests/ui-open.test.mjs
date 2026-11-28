// `ui.open` (convo-store.js + main.html): the open tool asks the page to do one thing in Agent Zero's interface and the page answers
// with `convo.ui_result`. A fake host stands in for Agent Zero's own stores and globals (the chat list's `selectChat`, the task
// list's `selectTask`, `ensureModalOpen`/`openModal`, the composer's `browseFiles`, the file browser's `open`):
// each is called when it exists, and a missing one is answered with a plain reason instead of an error.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { storeFixture } from './store-fixture.mjs';

const tick = (ms = 5) => new Promise(resolve => setTimeout(resolve, ms));
const plain = value => JSON.parse(JSON.stringify(value));
const never = () => new Promise(() => {});                                      // a host modal's promise: it settles only when the modal closes

// Agent Zero's page, as far as `ui.open` reaches into it. `stores`: what `Alpine.store(name)` gives; `globals`: page globals.
function host({ chats = ['c1', 'c2'], tasks = [], stores = {}, globals = {} } = {}) {
    const log = [];
    const chatStore = { selected: 'c0', contains: id => chats.includes(id), async selectChat(id) { log.push(['selectChat', id]); this.selected = id; } };
    const taskStore = { contains: id => tasks.includes(id), selectTask(id) { log.push(['selectTask', id]); chatStore.selected = id; } };
    const all = { chats: chatStore, tasks: taskStore,
        chatInput: { browseFiles() { log.push(['browseFiles']); return never(); } },
        fileBrowser: { open() { log.push(['fileBrowser.open']); return never(); } }, ...stores };
    return { log, chatStore, globals: { Alpine: { store: name => all[name] },
        ensureModalOpen: path => { log.push(['ensureModalOpen', path]); return never(); },
        openModal: path => { log.push(['openModal', path]); return never(); }, ...globals } };
}
async function rig(options = {}, fixture = {}) {
    const page = host(options);
    const f = await storeFixture({ globals: page.globals, ...fixture });
    const budget = f.store.uiWaitMs;                                                // the real wait for a host call; the tests use a short one
    f.store.uiWaitMs = 30;
    await f.store.start();
    const results = () => f.requests.filter(r => r.event === 'convo.ui_result').map(r => plain(r.data));
    let count = 0;
    const open = async (what, target, more = {}) => {
        const request_id = 'r' + (++count), before = results().length;
        f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, turn_id: 't', type: 'ui.open', request_id, what, target, title: '', ...more } });
        for (let waited = 0; results().length === before && waited < 3000; waited += 5) await tick();
        const answer = results().at(-1);
        assert.equal(answer?.request_id, request_id, 'every request is answered');
        return answer;
    };
    return { f, page, open, results, budget };
}
const sentence = answer => { assert.equal(typeof answer.detail, 'string'); assert.ok(answer.detail.length > 0 && answer.detail.length <= 160, answer.detail); return answer.detail; };

test('chat: the chat is selected with the host chat list\'s own selectChat, and the answer carries the conversation\'s identity', async () => {
    const { f, page, open } = await rig();
    const answer = await open('chat', 'c2');
    assert.deepEqual(page.log, [['selectChat', 'c2']]);
    assert.deepEqual(answer, { session_id: 's', epoch: 0, request_id: 'r1', ok: true, detail: 'The chat is selected in Agent Zero.' });
    assert.equal(f.store.target, 'a');                                               // the running conversation keeps its own chat
    assert.equal(f.store.active, true);                                              // and is neither stopped nor restarted
    await f.store.stop();
});

test('chat: a scheduler task\'s chat is selected through the task list; a chat that is not listed is refused without a call', async () => {
    const { f, page, open } = await rig({ tasks: ['task-9'] });
    assert.equal((await open('chat', 'task-9')).ok, true);
    assert.deepEqual(page.log, [['selectTask', 'task-9']]);
    const missing = await open('chat', 'gone');
    assert.deepEqual([missing.ok, missing.detail], [false, 'That chat is not in the chat list of Agent Zero.']);
    const unnamed = await open('chat', '');
    assert.deepEqual([unnamed.ok, unnamed.detail], [false, 'No chat was named.']);
    assert.equal(page.log.length, 1);                                                // neither refusal touched the host
    await f.store.stop();
});

test('chat: a host without the chat list, one whose selectChat throws, and one that does not switch all answer ok: false plainly', async () => {
    const none = await rig({ stores: { chats: undefined } });
    assert.deepEqual(await none.open('chat', 'c1').then(a => [a.ok, a.detail]), [false, 'The chat list of Agent Zero cannot be reached from this page.']);
    await none.f.store.stop();
    const other = await rig({ stores: { chats: { contains: () => true } } });          // another host: the list exists, the function does not
    assert.equal((await other.open('chat', 'c1')).ok, false);
    await other.f.store.stop();
    const throwing = await rig({ stores: { chats: { contains: () => true, selectChat() { throw new Error('secret internal detail c1'); } } } });
    const thrown = await throwing.open('chat', 'c1');
    assert.deepEqual([thrown.ok, thrown.detail], [false, 'Agent Zero could not select that chat.']);
    await throwing.f.store.stop();
    const stuck = await rig({ stores: { chats: { selected: 'c0', contains: () => true, async selectChat() {} } } });   // e.g. a chat being deleted
    assert.deepEqual(await stuck.open('chat', 'c1').then(a => [a.ok, a.detail]), [false, 'Agent Zero did not switch to that chat.']);
    await stuck.f.store.stop();
    const bare = await rig({ globals: { Alpine: undefined } });                        // no Alpine at all
    assert.equal((await bare.open('chat', 'c1')).ok, false);
    await bare.f.store.stop();
});

test('settings: opened as the host header button opens them, without waiting for the modal to close', async () => {
    const { f, page, open } = await rig();
    const answer = await open('settings');
    assert.deepEqual(page.log, [['ensureModalOpen', 'settings/settings.html']]);      // focuses an open Settings instead of stacking another
    assert.deepEqual([answer.ok, answer.detail], [true, 'The settings of Agent Zero are open.']);
    await f.store.stop();
    const older = await rig({ globals: { ensureModalOpen: undefined } });             // a host with openModal only
    assert.equal((await older.open('settings')).ok, true);
    assert.deepEqual(older.page.log, [['openModal', 'settings/settings.html']]);
    await older.f.store.stop();
});

test('settings: a host with neither opener, or one that throws, answers ok: false with a plain reason', async () => {
    const none = await rig({ globals: { ensureModalOpen: undefined, openModal: undefined } });
    assert.deepEqual(await none.open('settings').then(a => [a.ok, a.detail]), [false, 'The settings of Agent Zero cannot be opened from this page.']);
    await none.f.store.stop();
    const throwing = await rig({ globals: { ensureModalOpen: () => { throw new Error('boom'); } } });
    assert.deepEqual(await throwing.open('settings').then(a => [a.ok, a.detail]), [false, 'Agent Zero could not open its settings.']);
    await throwing.f.store.stop();
    const rejecting = await rig({ globals: { ensureModalOpen: async () => { throw new Error('boom'); } } });
    assert.equal((await rejecting.open('settings')).ok, false);
    await rejecting.f.store.stop();
});

test('files: the sidebar\'s own Files action, else the file browser store; neither present answers ok: false', async () => {
    const { f, page, open } = await rig();
    const answer = await open('files', '/etc');                                       // a target is not a path to follow
    assert.deepEqual(page.log, [['browseFiles']]);
    assert.deepEqual([answer.ok, answer.detail], [true, 'The file browser of Agent Zero is open.']);
    await f.store.stop();
    const fallback = await rig({ stores: { chatInput: {} } });
    assert.equal((await fallback.open('files')).ok, true);
    assert.deepEqual(fallback.page.log, [['fileBrowser.open']]);
    await fallback.f.store.stop();
    const none = await rig({ stores: { chatInput: undefined, fileBrowser: undefined } });
    assert.deepEqual(await none.open('files').then(a => [a.ok, a.detail]), [false, 'The file browser of Agent Zero cannot be opened from this page.']);
    await none.f.store.stop();
    const throwing = await rig({ stores: { chatInput: { browseFiles() { throw new Error('boom'); } } } });
    assert.deepEqual(await throwing.open('files').then(a => [a.ok, a.detail]), [false, 'Agent Zero could not open its file browser.']);
    await throwing.f.store.stop();
});

test('page: an http(s) address becomes a link card the user taps; the answer says so', async () => {
    const { f, open } = await rig();
    const answer = await open('page', 'https://example.org/docs?q=1#top', { title: 'Example  docs' });
    assert.deepEqual([answer.ok, answer.detail], [true, 'A link is shown in the Convo panel.']);
    assert.deepEqual(plain(f.store.links).map(({ url, title }) => ({ url, title })), [{ url: 'https://example.org/docs?q=1#top', title: 'Example docs' }]);
    assert.equal((await open('page', 'http://example.org')).ok, true);                // http too; without a title the host names it
    assert.equal(f.store.links.at(-1).title, 'example.org');
    await f.store.stop();
});

test('page: only http and https; anything else answers ok: false and shows no card', async () => {
    const { f, open } = await rig();
    for (const target of ['javascript:alert(1)', 'file:///etc/passwd', 'ftp://example.org/a', 'about:blank', 'data:text/html,hi', 'example.org', '//example.org',
                          '', 'https://', 'https://example.org/a b', 'https://user@example.org/', 'https://example.org\\@elsewhere.example/', ' \n']) {
        const answer = await open('page', target, { title: 'A page' });
        assert.deepEqual([answer.ok, answer.detail], [false, 'Only http and https addresses can be opened.'], JSON.stringify(target));
    }
    assert.equal((await open('page', undefined)).ok, false);
    assert.deepEqual(plain(f.store.links), []);
    assert.equal(f.namespace.webAddress('HTTPS://Example.org/x'), 'HTTPS://Example.org/x');
    assert.equal(f.namespace.webAddress('https://example.org/' + 'a'.repeat(2000)), '');
    await f.store.stop();
});

test('page: at most three link cards, the newest kept; the same address again is one card', async () => {
    const { f, open } = await rig();
    for (const n of [1, 2, 3, 4]) await open('page', `https://example.org/${n}`, { title: `Page ${n}` });
    assert.deepEqual(plain(f.store.links).map(link => link.title), ['Page 2', 'Page 3', 'Page 4']);
    await open('page', 'https://example.org/3', { title: 'Page three again' });
    assert.deepEqual(plain(f.store.links).map(link => link.title), ['Page 2', 'Page 4', 'Page three again']);
    assert.equal(new Set(f.store.links.map(link => link.id)).size, 3);
    await f.store.stop();
});

test('page: a card goes when it is tapped (after the tap), when it is dismissed, and when voice stops', async () => {
    const { f, open } = await rig();
    for (const n of [1, 2, 3]) await open('page', `https://example.org/${n}`, { title: `Page ${n}` });
    const [first, second] = f.store.links.map(link => link.id);
    f.store.linkTapped(first);
    assert.equal(f.store.links.length, 3);                                            // still there while the browser follows the link
    await tick();
    assert.deepEqual(plain(f.store.links).map(link => link.title), ['Page 2', 'Page 3']);
    f.store.closeLink(second);
    assert.deepEqual(plain(f.store.links).map(link => link.title), ['Page 3']);
    await f.store.stop();
    assert.deepEqual(plain(f.store.links), []);
});

test('page: Agent Zero\'s own browser panel is never navigated, even where the host answers address intents', async () => {
    // That browser runs with the user's saved sign-ins and is theirs to steer: a page the voice agent names is only ever a link.
    const intents = [];
    const surfaces = { handleUrlIntent: async intent => { intents.push(intent); return true; }, open: async () => { intents.push('open'); return true; },
                       openLatest: async () => { intents.push('openLatest'); return true; } };
    const { f, page, open } = await rig({}, { bindings: { '/js/surfaces.js': surfaces } });
    const answer = await open('page', 'https://example.org/a', { title: 'Example' });
    assert.deepEqual([answer.ok, answer.detail], [true, 'A link is shown in the Convo panel.']);
    assert.equal(f.store.links.length, 1);
    assert.deepEqual(intents, []);
    assert.deepEqual(page.log, []);                                                    // no host function at all
    const source = readFileSync(new URL('../webui/convo-store.js', import.meta.url), 'utf8');
    assert.doesNotMatch(source, /handleUrlIntent\(|surfaces\.js|openUrlIntent|browserPage/);
    await f.store.stop();
});

test('An unknown kind is refused; a request for another conversation is ignored; the epoch does not matter', async () => {
    const { f, page, open, results } = await rig();
    const unknown = await open('terminal', 'x');
    assert.deepEqual([unknown.ok, unknown.detail], [false, 'Convo cannot open that kind of thing.']);
    f.handlers['convo.event']({ data: { session_id: 'another', epoch: 0, type: 'ui.open', request_id: 'x', what: 'settings' } });
    await tick(20);
    assert.equal(results().some(answer => answer.request_id === 'x'), false);
    assert.deepEqual(page.log, []);
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 5, type: 'ui.open', request_id: 'late', what: 'settings' } });   // a barge-in since the request
    for (let waited = 0; !results().some(answer => answer.request_id === 'late') && waited < 3000; waited += 5) await tick();
    assert.equal(results().find(answer => answer.request_id === 'late').ok, true);
    await f.store.stop();
});

test('Every answer is one short plain sentence that never carries the target, and it arrives well within three seconds', async () => {
    const hanging = { chats: { selected: 'c0', contains: () => true, selectChat: never } };       // a host call that never settles
    const { f, open, budget } = await rig({ stores: hanging });
    assert.ok(budget >= 500 && budget <= 2000);                                        // the server waits three seconds in all
    f.store.uiWaitMs = budget;
    const answers = [];
    for (const [what, target] of [['chat', 'c1'], ['settings'], ['files'], ['terminal'], ['page', 'ftp://private.example/secret'], ['page', 'https://example.org/'], ['chat', '']]) {
        const started = Date.now();
        answers.push(await open(what, target));
        assert.ok(Date.now() - started < 2900, what + ' answered within its budget');
    }
    assert.deepEqual(answers.map(answer => answer.ok), [false, true, true, false, false, true, false]);   // a chat that never switched is not called selected
    for (const answer of answers) {
        const detail = sentence(answer);
        assert.doesNotMatch(detail, /c1|secret|private\.example|ftp:/);
        assert.match(detail, /^[A-Z][^\n]*\.$/);
        assert.deepEqual(Object.keys(answer).sort(), ['detail', 'epoch', 'ok', 'request_id', 'session_id']);
    }
    await f.store.stop();
});

test('The panel renders link cards as real links in a new tab with a dismiss button', () => {
    const html = readFileSync(new URL('../webui/main.html', import.meta.url), 'utf8');
    const start = html.indexOf('<ul class="convo-links"'), list = html.slice(start, html.indexOf('</ul>', start));
    assert.ok(start > 0 && start < html.indexOf('class="convo-log"'));
    assert.match(list, /x-show="\$store\.convo\.links\.length"/);
    assert.match(list, /<template x-for="link in \$store\.convo\.links" :key="link\.id">/);
    const anchor = list.match(/<a [^>]*>/)[0];
    assert.match(anchor, /:href="link\.url"/);
    assert.match(anchor, /target="_blank" rel="noopener noreferrer"/);
    assert.match(anchor, /@click="\$store\.convo\.linkTapped\(link\.id\)"/);
    assert.doesNotMatch(anchor, /\.prevent/);                                          // the tap itself opens the tab
    assert.match(list, /x-text="'Open ' \+ link\.title"/);
    assert.match(list, /<button type="button"[^>]*@click="\$store\.convo\.closeLink\(link\.id\)"[^>]*:aria-label="'Dismiss the link to ' \+ link\.title"/);
    assert.doesNotMatch(list, /x-html/);
});
