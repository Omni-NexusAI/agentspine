// One conversation across chats (convo-store.js, main.html, settings-controls.html, defaults.js). The server decides which conversation
// a start is (`session.target`): `convo:global` with the setting "global" or with no chat selected, else the chat's own, as before.
// Across chats the selected chat is only the default place for work: a change of selection is sent as `convo.work`, and nothing is
// stopped, retargeted or rebuilt. The panel's history, its task list and the completion notices follow that one conversation.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { storeFixture } from './store-fixture.mjs';
import { loadModule, hostStubs } from './vm-loader.mjs';
import { settingsMarkup } from './settings-markup.mjs';
import { defaults } from '../webui/defaults.js';
import { GLOBAL_SCOPE } from '../webui/view-helpers.js';

const tick = (ms = 5) => new Promise(resolve => setTimeout(resolve, ms));
const plain = value => JSON.parse(JSON.stringify(value));

// Agent Zero's page and Convo's server, as far as this reaches: the selected chat (`host.chat`, '' for none), chat titles, the
// `conversation_scope` setting, and a server that names the conversation as the real one does.
async function rig({ scope = 'chat', chat = 'a', names = { a: 'Chat a', b: 'Chat b', u: '' }, jobs = [], listed = null, refuseWork = null } = {}) {
    const host = { chat, names, scope, jobs, histories: [], refuseWork };
    const shortcuts = { getCurrentContextId: () => host.chat, openModal: async () => {},
        getCurrentContext: () => (host.chat ? { id: host.chat, name: host.names[host.chat] } : null) };
    const f = await storeFixture({
        bindings: { '/js/shortcuts.js': shortcuts },
        globals: listed ? { Alpine: { store: name => (name === 'chats' ? { contexts: listed } : undefined) } } : {},
        api: (_url, payload) => {
            if (payload.action === 'status') return { settings: { enabled: true, max_audio_seconds: 20, conversation_scope: host.scope, task_chat: 'new' }, runtime: { mode: 'external' } };
            if (payload.action === 'history') { host.histories.push(plain(payload)); return { events: [], jobs: plain(host.jobs) }; }
            return undefined;
        },
        answer: (event, data) => {
            if (event === 'convo.start') return { session: { id: 's', epoch: 0, target: host.scope === 'global' || !data.target ? GLOBAL_SCOPE : data.target } };
            if (event === 'convo.work') return host.refuseWork ? host.refuseWork(data) : { work: data.chat, name: host.names[data.chat] || '' };
            if (event === 'convo.retarget') return { session: { id: 's', epoch: 1, target: data.target } };
            return null;
        },
    });
    f.store.workDebounceMs = 0;
    const sent = name => f.requests.filter(r => r.event === 'convo.' + name).map(r => plain(r.data));
    const select = async id => { host.chat = id; await f.store.refresh(); await tick(); };      // the status poll is where the page looks
    const header = () => f.store.targetPrefix + f.store.targetLabel;
    return { f, host, sent, select, header };
}

test('The browser and the server agree on the key of the conversation across chats', () => {
    assert.equal(GLOBAL_SCOPE, 'convo:global');
    const source = readFileSync(new URL('../webui/convo-store.js', import.meta.url), 'utf8');
    assert.doesNotMatch(source, /'convo:global'/);                                         // one definition, imported
    assert.match(source, /import \{[^}]*\bGLOBAL_SCOPE\b[^}]*\} from '\.\/view-helpers\.js'/);
});

test('Voice starts with no chat selected: an empty target goes out and the conversation is the one across chats', async () => {
    const { f, sent, header } = await rig({ chat: '' });
    await f.store.start();
    assert.equal(f.store.error, '');
    assert.equal(f.store.active, true);
    assert.equal(sent('start').length, 1);
    assert.equal(sent('start')[0].target, '');
    assert.equal(f.store.target, GLOBAL_SCOPE);
    assert.equal(f.store.across, true);
    assert.equal(header(), 'No chat selected');
    assert.equal(f.store.canRetarget, false);
    assert.equal(f.audios.length, 1);                                                      // the microphone opened as for any start
    await f.store.stop();
    await f.store.refresh();                                                               // voice off, the setting is "chat", still no chat
    assert.equal(f.store.target, '');
    assert.equal(header(), 'No chat selected');
});

test('With the setting "global" a start sends the selected chat and the header says where work goes', async () => {
    const { f, sent, header } = await rig({ scope: 'global', chat: 'a' });
    await f.store.start();
    assert.equal(sent('start')[0].target, 'a');                                            // the selected chat, as before: the server decides
    assert.equal(f.store.target, GLOBAL_SCOPE);
    assert.equal(header(), 'Working in: Chat a');
    assert.equal(f.store.canRetarget, false);
    await f.store.stop();
    const untitled = await rig({ scope: 'global', chat: 'u' });
    await untitled.f.store.start();
    assert.equal(untitled.header(), 'Working in: an untitled chat');
    await untitled.f.store.stop();
});

test('A chat\'s own conversation is as before: the chat\'s id, "Chat · name", and the server\'s word wins over the page\'s guess', async () => {
    const { f, host, sent, header } = await rig({ scope: 'chat', chat: 'a' });
    await f.store.start();
    assert.equal(sent('start')[0].target, 'a');
    assert.deepEqual([f.store.target, f.store.across, header()], ['a', false, 'Chat · Chat a']);
    await f.store.stop();
    host.scope = 'global';                                                                 // changed elsewhere; this page has not polled yet
    await f.store.start();
    assert.equal(f.store.target, GLOBAL_SCOPE);
    assert.equal(header(), 'Working in: Chat a');
    await f.store.stop();
});

test('Across chats, selecting another chat sends `work` and nothing else: no retarget, no stop, no new microphone', async () => {
    const { f, sent, select, header, host } = await rig({ scope: 'global', chat: 'a' });
    await f.store.start();
    const session = f.store.session, histories = host.histories.length;
    await select('b');
    assert.deepEqual(sent('work'), [{ session_id: 's', epoch: 0, chat: 'b' }]);
    assert.equal(header(), 'Working in: Chat b');
    assert.deepEqual([f.store.work, f.store.workName], ['b', 'Chat b']);
    await select('b'); await select('b');
    assert.equal(sent('work').length, 1);                                                  // one selection is told once
    await select('');                                                                      // no chat selected (the welcome screen)
    assert.deepEqual(sent('work').at(-1), { session_id: 's', epoch: 0, chat: '' });
    assert.equal(header(), 'No chat selected');
    await select('u');
    assert.equal(header(), 'Working in: an untitled chat');
    assert.equal(sent('work').length, 3);
    assert.deepEqual([sent('retarget').length, sent('stop').length, sent('start').length], [0, 0, 1]);
    assert.equal(f.audios.length, 1);                                                      // the same capture: nothing was rebuilt
    assert.equal(f.audios[0].interrupted || 0, 0);                                         // and nothing playing was cut
    assert.equal(f.store.session, session);
    assert.deepEqual([f.store.target, f.store.active, f.store.canRetarget], [GLOBAL_SCOPE, true, false]);
    assert.equal(host.histories.slice(histories).every(call => call.target === GLOBAL_SCOPE), true);   // the same history throughout
    await f.store.stop();
});

test('A selection is not sent while the conversation starts, and several quick changes send only the last', async () => {
    const { f, sent, host } = await rig({ scope: 'global', chat: 'a' });
    await f.store.start();
    f.store.workDebounceMs = 30;
    f.store.busy = true;                                                                   // as while starting
    host.chat = 'b'; await f.store.refresh(); await tick(60);
    assert.equal(sent('work').length, 0);
    f.store.busy = false;
    host.chat = 'u'; await f.store.refresh();
    host.chat = 'b'; await f.store.refresh();
    host.chat = 'a'; await f.store.refresh(); host.chat = 'b'; await f.store.refresh();
    await tick(80);
    assert.deepEqual(sent('work').map(data => data.chat), ['b']);
    await f.store.stop();
    host.chat = 'a'; await f.store.refresh(); await tick(60);
    assert.equal(sent('work').length, 1);                                                  // voice is off: there is nobody to tell
});

test('In a chat\'s own conversation a chat switch sends no `work`; "Use selected chat" still retargets as before', async () => {
    const { f, sent, select, header } = await rig({ scope: 'chat', chat: 'a' });
    await f.store.start();
    await select('b');
    assert.equal(sent('work').length, 0);
    assert.deepEqual([f.store.target, header(), f.store.canRetarget], ['a', 'Chat · Chat a', true]);   // not followed silently
    await f.store.sendWork();
    assert.equal(sent('work').length, 0);
    await f.store.retarget();
    assert.deepEqual(sent('retarget'), [{ session_id: 's', epoch: 0, target: 'b' }]);
    assert.deepEqual([f.store.target, header(), f.store.canRetarget], ['b', 'Chat · Chat b', false]);
    assert.equal(f.audios.length, 2);                                                      // the chat's own conversation is rebuilt, as before
    await f.store.stop();
});

test('Across chats "retarget" never retargets: it only says where work goes', async () => {
    const { f, sent, host } = await rig({ scope: 'global', chat: 'a' });
    await f.store.start();
    host.chat = 'b';
    await f.store.retarget();
    assert.equal(sent('retarget').length, 0);
    assert.deepEqual(sent('work').map(data => data.chat), ['b']);
    assert.equal(f.audios.length, 1);
    await f.store.stop();
});

test('A refused `work` is said once and not repeated for the same selection; an ordinary "stale" is simply tried again', async () => {
    let mode = 'refuse';
    const { f, sent, select, header } = await rig({ scope: 'global', chat: 'a',
        refuseWork: data => (mode === 'refuse' ? new Error('Internal server error') : mode === 'stale' ? { ok: false, stale: true } : { work: data.chat, name: 'Chat b' }) });
    await f.store.start();
    await select('b');
    assert.match(f.store.error, /Internal server error/);
    assert.equal(header(), 'Working in: Chat a');                                          // what the server last confirmed
    f.store.error = '';
    await select('b'); await select('b');
    assert.equal(sent('work').length, 1);
    assert.equal(f.store.error, '');
    assert.equal(f.store.active, true);                                                    // a refusal never ends the conversation
    mode = 'stale';
    await select('u');
    assert.equal(f.store.error, '');
    mode = 'ok';
    await select('u');
    assert.deepEqual(sent('work').map(data => data.chat), ['b', 'u', 'u']);
    await f.store.stop();
});

test('The panel\'s history is the conversation across chats: while voice runs, and with the setting "global" while it is off', async () => {
    const { f, host, select } = await rig({ scope: 'global', chat: 'a' });
    await f.store.mountPanel();                                                            // voice off
    assert.equal(f.store.target, GLOBAL_SCOPE);
    assert.deepEqual(host.histories.at(-1), { action: 'history', target: GLOBAL_SCOPE, tail: 60 });
    assert.equal(f.store.targetPrefix + f.store.targetLabel, 'Working in: Chat a');        // where work would go
    await select('b');                                                                     // another chat is opened: the same conversation
    await f.store.history();
    assert.equal(f.store.target, GLOBAL_SCOPE);
    assert.equal(host.histories.every(call => call.target === GLOBAL_SCOPE), true);
    assert.equal(f.store.targetPrefix + f.store.targetLabel, 'Working in: Chat b');
    assert.equal(f.requests.some(r => r.event === 'convo.work'), false);                   // voice is off: nothing is sent
    await f.store.start();
    await f.store.history();
    assert.equal(host.histories.every(call => call.target === GLOBAL_SCOPE), true);
    await f.store.stop();
    const own = await rig({ scope: 'chat', chat: 'a' });
    await own.f.store.mountPanel();
    assert.equal(own.host.histories.at(-1).target, 'a');                                   // a chat's own conversation: the chat, as before
    await own.select('b');
    await own.f.store.history();
    assert.equal(own.host.histories.at(-1).target, 'b');
});

test('A finished job is announced whichever chat it ran in; a chat\'s own conversation claims only its own chat\'s jobs', async () => {
    const jobs = [{ id: 'j1', session: 's', target: 'b', status: 'completed', prompt: 'Tidy the notes' },
                  { id: 'j2', session: 'earlier', target: 'b', status: 'completed', prompt: 'From another voice session' }];
    const { f, sent } = await rig({ scope: 'global', chat: 'a', jobs });
    await f.store.start();
    await f.store.history(); await tick();
    assert.deepEqual(sent('notice'), [{ session_id: 's', epoch: 0, job_id: 'j1' }]);       // its chat is `b`, the conversation is across chats
    await f.store.history(); await tick();
    assert.equal(sent('notice').length, 1);                                                // once
    await f.store.stop();
    const own = await rig({ scope: 'chat', chat: 'a', jobs: [{ id: 'j3', session: 's', target: 'b', status: 'completed', prompt: 'Elsewhere' },
                                                             { id: 'j4', session: 's', target: 'a', status: 'failed', prompt: 'Here' }] });
    await own.f.store.start();
    await own.f.store.history(); await tick();
    assert.deepEqual(own.sent('notice').map(data => data.job_id), ['j4']);
    await own.f.store.stop();
});

test('The task list says which chat a task went to: the name the server gave when it was queued, else the host chat list\'s', async () => {
    const jobs = [{ id: 'j1', session: 's', target: 'x1', status: 'running', prompt: 'One' }, { id: 'j2', session: 's', target: 'b', status: 'queued', prompt: 'Two' },
                  { id: 'j3', session: 's', target: 'gone', status: 'queued', prompt: 'Three' }];
    const { f } = await rig({ scope: 'global', chat: 'a', jobs, listed: [{ id: 'a', name: 'Chat a' }, { id: 'b', name: 'Listed b' }] });
    await f.store.start();
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, turn_id: 't', type: 'job.queued', job_id: 'j1', target: 'x1', chat: 'Holiday plans' } });
    await tick();
    assert.deepEqual(f.store.activeJobs.map(job => f.store.jobChat(job)), ['Holiday plans', 'Listed b', '']);   // unknown: the name is left out
    assert.equal(f.store.jobChat({ id: 'j9', target: 'a' }), 'Chat a');                    // the selected chat's own title
    assert.equal(f.store.jobChat(null), '');
    await f.store.stop();
    const own = await rig({ scope: 'chat', chat: 'a', jobs, listed: [{ id: 'b', name: 'Listed b' }] });
    await own.f.store.start();
    await own.f.store.history();
    assert.deepEqual(own.f.store.activeJobs.map(job => own.f.store.jobChat(job)), ['', '', '']);   // a chat's own conversation: unchanged
    await own.f.store.stop();
});

test('"Take over here" says when the voice running elsewhere is the conversation across chats', async () => {
    const { f } = await rig({ scope: 'chat', chat: 'a' });
    await f.store.refresh();
    f.store.owner = { session: 'other', page: 'other-page', device: 'Chrome on Android', target: GLOBAL_SCOPE };
    assert.equal(f.store.ownerText, 'Voice is running on Chrome on Android (across chats).');
    f.store.owner = { session: 'other', page: 'other-page', device: 'Chrome on Android', target: 'a' };
    assert.equal(f.store.ownerText, 'Voice is running on Chrome on Android (in this chat).');
    f.store.owner = { session: 'other', page: 'other-page', device: 'Chrome on Android', target: 'zz' };
    assert.equal(f.store.ownerText, 'Voice is running on Chrome on Android (in another chat).');
});

test('The panel shows the chat line from the store, keeps "Use selected chat" for a chat\'s own conversation, and names a task\'s chat', () => {
    const html = readFileSync(new URL('../webui/main.html', import.meta.url), 'utf8');
    const line = html.slice(html.indexOf('<div class="convo-target">'), html.indexOf('</div>', html.indexOf('<div class="convo-target">')));
    assert.match(line, /<span class="convo-target-name"><span x-text="\$store\.convo\.targetPrefix"><\/span><strong x-text="\$store\.convo\.targetLabel"><\/strong><\/span>/);
    assert.match(line, /x-show="\$store\.convo\.canRetarget" @click="\$store\.convo\.retarget\(\)"[^>]*>Use selected chat<\/button>/);
    assert.doesNotMatch(html, /Select a chat to begin|Select or create a chat/);
    assert.match(html, /<span class="convo-activity-chat" x-show="\$store\.convo\.jobChat\(job\)" x-text="'in ' \+ \$store\.convo\.jobChat\(job\)"><\/span>/);
});

// ---- Settings -> Conversation: the two selects
async function setup() {
    const server = { config: plain(defaults), calls: [] };
    const answer = () => ({ ok: true, config: plain(server.config), revision: 'r1', readiness: [], credentials: {}, decision_backends: [], providers: [], native: {} });
    const bindings = hostStubs({ '/js/api.js': { callJsonApi: async (_url, payload) => {
        server.calls.push(plain(payload));
        if (payload.action === 'setup_save') server.config = plain(payload.config);
        return answer();
    } } });
    const { namespace } = await loadModule('webui/setup-store.js', { bindings, globals: { window: { confirm: () => true } } });
    await namespace.store.load();
    return { store: namespace.store, server };
}

test('The defaults keep the conversation with the selected chat and send unnamed tasks to a new chat', async () => {
    assert.equal(defaults.conversation_scope, 'chat');
    assert.equal(defaults.task_chat, 'new');
    const older = plain(defaults); delete older.conversation_scope; delete older.task_chat;
    const { namespace } = await loadModule('webui/defaults.js', { bindings: hostStubs() });
    assert.deepEqual([namespace.fillDefaults(older).conversation_scope, older.task_chat], ['chat', 'new']);   // a saved file from before the settings
});

test('The Conversation section has the two selects near its top, each with its options and hint, bound to the shared draft', async () => {
    const shell = readFileSync(new URL('../webui/settings-controls.html', import.meta.url), 'utf8');
    const start = shell.indexOf(`<div x-show="$store.convoSetup.section === 'Conversation'" class="convo-stack">`);
    const section = shell.slice(start, shell.indexOf(`section === 'Voice & Audio'`));
    const scope = section.indexOf('<label>Conversation<select x-model="$store.convoSetup.config.conversation_scope">');
    const tasks = section.indexOf('<label>Tasks with no chat named<select x-model="$store.convoSetup.config.task_chat">');
    assert.ok(scope > 0 && tasks > scope);
    assert.ok(tasks < section.indexOf('config.transcription_enabled'));                    // near the top: right after "Enable Convo"
    assert.ok(section.indexOf('config.enabled"') < scope);
    const options = from => [...section.slice(from, section.indexOf('</select>', from)).matchAll(/\['(\w+)','([^']+)'\]/g)].map(match => [match[1], match[2]]);
    assert.deepEqual(options(scope), [['chat', 'Follows the selected chat'], ['global', 'One conversation across chats']]);
    assert.deepEqual(options(tasks), [['new', 'Open a new chat'], ['selected', 'Use the selected chat']]);
    // Like the neighbouring selects: every option binds its selected state, so a saved value never shows as the first option.
    assert.match(section.slice(scope, tasks), /:selected="option\[0\] === \$store\.convoSetup\.config\.conversation_scope"/);
    assert.match(section.slice(tasks), /:selected="option\[0\] === \$store\.convoSetup\.config\.task_chat"/);
    assert.match(section.slice(scope, tasks), /<p class="convo-hint">Across chats, the conversation and its memory stay the same whichever chat is open; tasks go to a chat you name, or to the place chosen below\. Voice started with no chat selected is always the conversation across chats\.<\/p>/);
    assert.match(section.slice(tasks), /<p class="convo-hint">Applies to a conversation across chats\. A new chat is reused for the later tasks of the same voice session\.<\/p>/);
    assert.doesNotMatch(section.slice(scope, section.indexOf('</p>', tasks)), /x-show/);   // both are always shown
    const markup = await settingsMarkup();
    for (const key of ['conversation_scope', 'task_chat']) assert.equal(markup.split(`x-model="$store.convoSetup.config.${key}"`).length - 1, 1, key);
});

test('The two selects change the draft and Save sends them', async () => {
    const shell = readFileSync(new URL('../webui/settings-controls.html', import.meta.url), 'utf8');
    const f = await setup(), assigned = [];
    for (const [key, value, original] of [['conversation_scope', 'global', 'chat'], ['task_chat', 'selected', 'new']]) {
        const expression = shell.match(new RegExp(`<select x-model="([^"]*\\.${key})"`))[1];
        const assign = new Function('$store', 'value', `${expression} = value`);          // what x-model does on a change
        assert.equal(f.store.config[key], original);
        assign({ convoSetup: f.store }, value);
        assert.equal(f.store.config[key], value);
        assert.equal(f.store.dirty, true);
        assign({ convoSetup: f.store }, original);
        assert.equal(f.store.dirty, false);
        assigned.push(() => assign({ convoSetup: f.store }, value));
    }
    for (const again of assigned) again();
    assert.equal(await f.store.save(), true);
    const saved = f.server.calls.at(-1);
    assert.equal(saved.action, 'setup_save');
    assert.deepEqual([saved.config.conversation_scope, saved.config.task_chat], ['global', 'selected']);
    assert.equal(f.store.dirty, false);
});
