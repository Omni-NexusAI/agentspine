// Settings -> Conversation -> "What the voice agent may do" (settings-controls.html, voice-tools-setup.js, defaults.js): one checkbox
// per `voice_tools` group on the shared draft, and under the web checkbox a search health line. Showing the section reads the last
// known state (`search_health`, which never searches); only the press of "Check search" sends a search (`search_check`).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { loadModule, hostStubs } from './vm-loader.mjs';
import { settingsMarkup } from './settings-markup.mjs';
import { defaults } from '../webui/defaults.js';

const plain = value => JSON.parse(JSON.stringify(value));
const tick = (ms = 5) => new Promise(resolve => setTimeout(resolve, ms));
const read = name => readFile(new URL('../webui/' + name, import.meta.url), 'utf8');
const GROUPS = {
    web: ['Search the web and read pages', 'Looks things up with Agent Zero’s search service and opens a public page to read it.'],
    memory: ['Use long-term memory', 'Reads what the agent remembers; saving something new always asks you first.'],
    tasks: ['Hand over and manage tasks', 'Hands longer work to the chat’s agent, reports how it is going and cancels it on request.'],
    system: ['Check the system and the time', 'Reads the machine’s load, memory, disk and services, and the time anywhere.'],
    timers: ['Set timers and reminders', 'Kept by Convo; you get a notification, and it is said aloud while voice is running.'],
    chats: ['Look at your chats', 'Lists your chats and reads a chat’s latest messages.'],
    senses: ['See and listen back', 'Camera and screen pictures you share with a button, and replaying the last sounds it heard.'],
    interface: ['Open things and change the voice’s speed', 'Opens a chat, Settings, the file browser or a link, and speaks slower or faster for this conversation.'],
};
const escape = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// The real settings store over a scripted server. `health(action)`: the answer for search_health / search_check (or a promise of one).
async function rig({ config = plain(defaults), health = () => ({ ok: true, state: 'unknown', checked_at: '', answered: 0, refused: 0 }) } = {}) {
    const server = { config, revision: 'r1', calls: [] };
    const answer = () => ({ ok: true, config: plain(server.config), revision: server.revision, readiness: [], credentials: {}, decision_backends: [], providers: [], native: {} });
    const bindings = hostStubs({ '/js/api.js': { callJsonApi: async (url, payload) => {
        server.calls.push(plain(payload));
        assert.equal(url, '/plugins/_convo/conversation');
        if (payload.action === 'search_health' || payload.action === 'search_check') return health(payload.action);
        if (payload.action === 'setup_save') { server.config = plain(payload.config); server.revision = 'r2'; }
        return answer();
    } } });
    const { namespace } = await loadModule('webui/setup-store.js', { bindings, globals: { window: { confirm: () => true } } });
    await namespace.store.load();
    const actions = () => server.calls.map(call => call.action).filter(action => action.startsWith('search_'));
    return { store: namespace.store, server, actions };
}
const helpers = async () => (await loadModule('webui/voice-tools-setup.js', { bindings: hostStubs() })).namespace;
const group = async () => {
    const shell = await read('settings-controls.html');
    const start = shell.indexOf('<fieldset class="convo-card convo-voice-tools"');
    return { shell, start, html: shell.slice(start, shell.indexOf('</fieldset>', start)) };
};

test('The defaults turn every voice tool group on, and a saved configuration from before the setting gets them', async () => {
    assert.deepEqual(defaults.voice_tools, { web: true, memory: true, tasks: true, system: true, timers: true, chats: true, senses: true, interface: true });
    assert.deepEqual(Object.keys(defaults.voice_tools), Object.keys(GROUPS));
    const older = plain(defaults); delete older.voice_tools;
    const f = await rig({ config: older });
    assert.deepEqual(plain(f.store.config.voice_tools), defaults.voice_tools);
    assert.equal(f.store.dirty, false);                                                // filling a default is not an edit
    assert.deepEqual(plain((await helpers()).VOICE_TOOL_GROUPS), Object.keys(GROUPS));
});

test('The Conversation section has one labelled checkbox per group, with its hint, in the group "What the voice agent may do"', async () => {
    const { shell, start, html } = await group();
    const section = shell.indexOf(`<div x-show="$store.convoSetup.section === 'Conversation'" class="convo-stack">`);
    assert.ok(section > 0 && start > section && start < shell.indexOf(`section === 'Audio & Video'`));
    assert.match(html, /<legend>What the voice agent may do<\/legend>/);
    // Agent Zero's switch in its row layout: the title and hint on the left, the toggle on the right.
    const boxes = [...html.matchAll(/<div class="field convo-switch"><div class="field-label"><div class="field-title">([^<]+)<\/div><div class="field-description">([^<]+)<\/div><\/div><div class="field-control"><label class="toggle"><input type="checkbox" x-model="\$store\.convoSetup\.config\.voice_tools\.(\w+)" aria-label="[^"]+"><span class="toggler"><\/span><\/label><\/div><\/div>/g)];
    assert.deepEqual(boxes.map(match => match[3]), Object.keys(GROUPS));               // every key, once, in the catalogue's order
    for (const [, label, hint, key] of boxes) assert.deepEqual([label, hint], GROUPS[key], key);
    assert.equal((html.match(/type="checkbox"/g) || []).length, 8);
    // The same switch as the neighbouring ones of that section, and the one shared draft (no store or save path of its own).
    assert.match(shell.slice(section, start), /<label class="toggle"><input type="checkbox" x-model="\$store\.convoSetup\.config\.enabled" aria-label="Enable Convo composer control">/);
    assert.doesNotMatch(html, /@change|setup_patch|\.patch\(|localStorage/);
    // Both settings entry points (the plugin Config and the canvas settings modal) embed this one shell.
    for (const key of Object.keys(GROUPS)) assert.equal((await settingsMarkup()).split(`config.voice_tools.${key}"`).length - 1, 1, key);
});

test('Each checkbox changes the shared draft, and Save sends it like every other setting', async () => {
    const { html } = await group();
    const f = await rig();
    for (const key of Object.keys(GROUPS)) {
        const expression = html.match(new RegExp(`x-model="([^"]*voice_tools\\.${key})"`))[1];
        const assign = new Function('$store', 'value', `${expression} = value`);         // what x-model does on a click
        assert.equal(f.store.config.voice_tools[key], true, key);
        assign({ convoSetup: f.store }, false);
        assert.equal(f.store.config.voice_tools[key], false, key);
        assert.equal(f.store.dirty, true, key);
        assert.deepEqual(Object.entries(plain(f.store.config.voice_tools)).filter(([, on]) => !on).map(([name]) => name), [key]);   // only that group
        assign({ convoSetup: f.store }, true);
        assert.equal(f.store.dirty, false, key);
    }
    f.store.config.voice_tools.web = false; f.store.config.voice_tools.interface = false;
    assert.equal(await f.store.save(), true);
    const saved = f.server.calls.at(-1);
    assert.equal(saved.action, 'setup_save');
    assert.deepEqual(saved.config.voice_tools, { ...defaults.voice_tools, web: false, interface: false });
    assert.deepEqual(plain(f.server.config.voice_tools), saved.config.voice_tools);
    assert.equal(f.store.dirty, false);
    assert.deepEqual(f.actions(), []);                                                 // editing and saving never ask about search
});

test('The health line says each state in plain words, with the time when one is known', async () => {
    const { searchHealthLine, checkedText, SEARCH_STATES } = await helpers();
    assert.deepEqual(plain(SEARCH_STATES), {
        unknown: 'Search has not been used since Agent Zero started.',
        ok: 'Search is answering.',
        limited: 'Some search engines are refusing; others answer.',
        blocked: 'Every search engine asked is refusing right now.' });
    const now = new Date(2026, 10, 22, 15, 0, 0), earlier = new Date(2026, 10, 22, 14, 5, 0), yesterday = new Date(2026, 10, 21, 9, 30, 0);
    const time = date => date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    for (const [state, words] of Object.entries(plain(SEARCH_STATES))) {
        assert.equal(searchHealthLine({ state, checked_at: '' }, now), words, state);
        assert.equal(searchHealthLine({ state, checked_at: earlier.toISOString() }, now), `${words} Checked at ${time(earlier)}.`, state);
    }
    assert.equal(checkedText(yesterday.toISOString(), now), `Checked on ${yesterday.toLocaleDateString([], { day: 'numeric', month: 'short' })} at ${time(yesterday)}.`);
    assert.equal(checkedText('not a time', now), '');
    assert.equal(checkedText(undefined, now), '');
    assert.equal(searchHealthLine({}, now), '');                                        // nothing read yet: a quiet, empty line
    assert.equal(searchHealthLine({ state: 'surprising' }, now), '');
    assert.equal(searchHealthLine({ busy: true, state: 'ok' }, now), 'Checking search…');
    assert.equal(searchHealthLine({ failure: 'The search state could not be read.' }, now), 'The search state could not be read.');
});

test('Showing the Conversation section reads the last known state and never sends a search', async () => {
    const { html } = await group();
    const mount = html.match(/^<fieldset class="convo-card convo-voice-tools" x-init="([^"]+)"/)[1];
    assert.doesNotMatch(mount, /searchCheck|search_check/);
    const checkedAt = new Date().toISOString();                                         // today: the line then gives only the time
    const f = await rig({ health: () => ({ ok: true, state: 'limited', checked_at: checkedAt, answered: 2, refused: 1 }) });
    // The shell's own x-init, run as Alpine runs it: once when the group mounts, then on every section change.
    const watchers = [];
    const run = () => new Function('$store', '$watch', mount)({ convoSetup: f.store }, (expression, callback) => watchers.push([expression, callback]));
    assert.equal(f.store.section, 'Connections');
    run();
    await tick();
    assert.deepEqual(f.actions(), []);                                                 // another section is showing: nothing is asked
    assert.equal(f.store.searchHealthText(), '');
    assert.deepEqual(watchers.map(([expression]) => expression), ['$store.convoSetup.section']);
    f.store.section = 'Conversation'; watchers[0][1]('Conversation');
    await tick();
    assert.deepEqual(f.actions(), ['search_health']);
    assert.match(f.store.searchHealthText(), /^Some search engines are refusing; others answer\. Checked at \S/);
    f.store.section = 'Audio & Video'; watchers[0][1]('Audio & Video');
    f.store.section = 'Conversation'; watchers[0][1]('Conversation');
    await tick();
    assert.deepEqual(f.actions(), ['search_health', 'search_health']);                 // shown again: read again, still from memory
    run();                                                                             // the modal reopened on the Conversation section
    await tick();
    assert.deepEqual(f.actions(), ['search_health', 'search_health', 'search_health']);
    assert.equal(f.actions().includes('search_check'), false);
    assert.deepEqual(f.server.calls.filter(call => call.action === 'search_health').map(call => Object.keys(call)), [['action'], ['action'], ['action']]);
    assert.equal(f.store.dirty, false);                                                // reading never touches the draft
});

test('Each state the server reports is shown; an answer without a known state, or a failed read, is a plain line', async () => {
    for (const [state, words] of [['unknown', 'Search has not been used since Agent Zero started.'], ['ok', 'Search is answering.'],
                                  ['limited', 'Some search engines are refusing; others answer.'], ['blocked', 'Every search engine asked is refusing right now.']]) {
        const f = await rig({ health: () => ({ ok: state !== 'blocked', state, checked_at: '', answered: 0, refused: 0 }) });   // `ok` is not what decides
        f.store.searchHealthShown('Conversation');
        await tick();
        assert.equal(f.store.searchHealthText(), words, state);
    }
    const odd = await rig({ health: () => ({ ok: true, state: 'sideways' }) });
    assert.equal(await odd.store.searchHealthRead(), false);
    assert.equal(odd.store.searchHealthText(), 'The search state could not be read.');
    let fail = false;
    const flaky = await rig({ health: () => { if (fail) throw new Error('HTTP 500: traceback text'); return { ok: true, state: 'ok', checked_at: '' }; } });
    assert.equal(await flaky.store.searchHealthRead(), true);
    fail = true;
    assert.equal(await flaky.store.searchHealthRead(), false);
    assert.equal(flaky.store.searchHealthText(), 'Search is answering.');                // what was known stays; no error text is shown
    assert.equal(flaky.store.error, '');                                                // and it is not a settings error
});

test('"Check search" sends one search on the press, shows a busy state, then the result', async () => {
    let release;
    const held = new Promise(resolve => { release = resolve; });
    const checkedAt = new Date().toISOString();                                         // today: the line then gives only the time
    const f = await rig({ health: async action => {
        if (action === 'search_health') return { ok: true, state: 'unknown', checked_at: '' };
        await held;
        return { ok: true, state: 'blocked', checked_at: checkedAt, answered: 0, refused: 3 };
    } });
    f.store.searchHealthShown('Conversation');
    await tick();
    assert.equal(f.store.searchHealthText(), 'Search has not been used since Agent Zero started.');
    const pressed = f.store.searchCheck();
    assert.equal(f.store.ui.voiceTools.busy, true);                                    // the button is disabled and reads "Checking…"
    assert.equal(f.store.searchHealthText(), 'Checking search…');
    assert.equal(await f.store.searchCheck(), false);                                  // a second press while it runs sends nothing
    f.store.searchHealthShown('Conversation');                                         // nor does showing the section again
    await tick();
    assert.deepEqual(f.actions(), ['search_health', 'search_check']);
    release();
    assert.equal(await pressed, true);
    assert.equal(f.store.ui.voiceTools.busy, false);
    assert.match(f.store.searchHealthText(), /^Every search engine asked is refusing right now\. Checked at \S/);
    assert.deepEqual(f.actions(), ['search_health', 'search_check']);
    assert.deepEqual(Object.keys(f.server.calls.find(call => call.action === 'search_check')), ['action']);
});

test('A check that fails, or answers without a known state, is a plain failure line; the next read replaces it', async () => {
    let mode = 'throw';
    const f = await rig({ health: action => {
        if (action === 'search_health') return { ok: true, state: 'ok', checked_at: '' };
        if (mode === 'throw') throw new Error('upstream said: 429 from engine at http://private.example');
        return { ok: false, error: 'Unknown action' };
    } });
    assert.equal(await f.store.searchCheck(), false);
    assert.equal(f.store.searchHealthText(), 'The check did not finish, so the search state is not known. Try again in a moment.');
    assert.doesNotMatch(f.store.searchHealthText(), /429|private\.example/);            // never the provider's or the exception's text
    assert.equal(f.store.ui.voiceTools.busy, false);
    mode = 'refuse';
    assert.equal(await f.store.searchCheck(), false);
    assert.match(f.store.searchHealthText(), /did not finish/);
    assert.equal(await f.store.searchHealthRead(), true);
    assert.equal(f.store.searchHealthText(), 'Search is answering.');
});

test('A slow read that answers after a check does not overwrite the check\'s result', async () => {
    let releaseRead;
    const heldRead = new Promise(resolve => { releaseRead = resolve; });
    const f = await rig({ health: async action => {
        if (action === 'search_check') return { ok: true, state: 'ok', checked_at: '' };
        await heldRead;
        return { ok: true, state: 'blocked', checked_at: '' };
    } });
    const reading = f.store.searchHealthRead();
    await tick();
    assert.equal(await f.store.searchCheck(), true);
    releaseRead();
    assert.equal(await reading, false);
    assert.equal(f.store.searchHealthText(), 'Search is answering.');
});

test('The status line and the button sit under the web checkbox; only the button\'s press can check', async () => {
    const { html } = await group();
    const web = html.indexOf('config.voice_tools.web"'), health = html.indexOf('<div class="convo-search-health">'), memory = html.indexOf('config.voice_tools.memory"');
    assert.ok(web > 0 && health > web && health < memory);
    const block = html.slice(health, html.indexOf('</div>', health));
    assert.match(block, /<p class="convo-hint" role="status" x-text="\$store\.convoSetup\.searchHealthText\(\)"><\/p>/);    // quiet and polite
    assert.match(block, /<button type="button" class="button" :disabled="\$store\.convoSetup\?\.ui\?\.voiceTools\?\.busy === true" @click="\$store\.convoSetup\.searchCheck\(\)"/);
    assert.match(block, /'Checking…' : 'Check search'/);
    assert.doesNotMatch(block, /role="alert"|aria-live="assertive"/);
    const markup = await settingsMarkup();
    assert.equal(markup.split('searchCheck(').length - 1, 1);                           // one place, and it is the button's @click
    const source = await read('voice-tools-setup.js');
    assert.equal(source.split("action: 'search_check'").length - 1, 1);
    const check = source.slice(source.indexOf('async searchCheck()'), source.indexOf('searchHealthTake(result) {'));
    assert.ok(check.includes("action: 'search_check'"));                                // sent only from searchCheck
    assert.equal(source.slice(0, source.indexOf('async searchCheck()')).includes("action: 'search_check'"), false);
});
