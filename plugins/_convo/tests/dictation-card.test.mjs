// The dictation card's voice-message handling (dictation-field.html + dictation-setup.js): Agent Zero's "Send immediately" and "Draft in
// composer" plus Convo's "Draft in composer (continuous)". A choice applies at once through the Whisper bridge (api/stt_config.py) and
// the answer is adopted, so this editor's own write never raises the "changed elsewhere" banner and other unsaved edits stay. Run on
// the real settings store over an Alpine-like reactive proxy, with a scripted server.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { hostStubs, loadModule } from './vm-loader.mjs';
import { readWebui } from './settings-markup.mjs';
import { defaults } from '../webui/defaults.js';

const plain = value => JSON.parse(JSON.stringify(value));
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };

async function rig({ flag = 'single', host = 'draft', failBridge = false } = {}) {
    const server = { config: { ...plain(defaults), voice_message_draft: flag }, host, revision: 'r1', calls: [], hold: null };
    const read = () => ({ ok: true, config: plain(server.config), revision: server.revision, readiness: [], credentials: {}, decision_backends: [], providers: [],
        native: { whisper: { plugin: '_whisper_stt', enabled: true, summary: { model_size: 'base', device: 'auto', language: 'en', message_mode: server.host } } } });
    const callJsonApi = async (url, payload) => {
        server.calls.push([url, plain(payload)]);
        if (url === '/plugins/_convo/stt_config') {
            if (server.hold) await server.hold;
            if (failBridge) throw new Error('Whisper settings could not be saved.');
            const mode = payload.message_mode;          // as api/stt_config.py: continuous = host draft + flag; send/draft = flag single
            const next = mode === 'continuous' ? 'continuous' : 'single';
            server.host = mode === 'continuous' ? 'draft' : mode;
            if (next !== server.config.voice_message_draft) { server.config.voice_message_draft = next; server.revision = 'r' + (Number(server.revision.slice(1)) + 1); }
            return { success: true, enabled: true, config: { model_size: 'base', message_mode: server.host, effective_device: 'cpu' }, voice_message_draft: next, convo_revision: server.revision };
        }
        if (payload.action === 'setup_read') return read();
        return { ok: true };
    };
    const whisper = { config: { message_mode: host, silence_duration: 1000 } }, refreshed = [];
    const Alpine = { store: name => ({ whisperStt: whisper, convo: { refresh: () => refreshed.push(1) } })[name] };
    const { namespace } = await loadModule('webui/setup-store.js', { bindings: hostStubs({ '/js/api.js': { callJsonApi } }, { reactive: true }),
        globals: { window: { confirm: () => true }, Alpine } });
    await namespace.store.load();
    const select = { value: '' };
    const shown = () => namespace.store.messageMode(namespace.store.config.voice_message_draft, namespace.store.native.whisper?.summary?.message_mode);
    return { store: namespace.store, server, whisper, refreshed, select, shown };
}

test('The card offers the three choices on one select bound to the stored flag and Agent Zero\'s own handling', async () => {
    const html = await readWebui('dictation-field.html');
    assert.doesNotMatch(html, /convo-settings-pending/);                          // the control has come
    const select = html.match(/<select [^>]*>/)[0];
    assert.match(select, /:value="\$store\.convoSetup\.messageMode\(\$store\.convoSetup\.config\.voice_message_draft, \$store\.convoSetup\.native\.whisper\?\.summary\?\.message_mode\)"/);
    assert.match(select, /@change="\$store\.convoSetup\.applyMessageMode\(\$event\.target\.value, \$event\.target\)"/);
    assert.match(html, /<label>Voice message handling<select /);                  // the label names the control
    assert.deepEqual([...html.matchAll(/<option value="(\w+)">([^<]+)<\/option>/g)].map(match => [match[1], match[2]]),
        [['send', 'Send immediately'], ['draft', 'Draft in composer'], ['continuous', 'Draft in composer (continuous)']]);
    assert.match(html, /nothing is sent for you/);
    const { store } = await rig();
    const table = [['continuous', 'draft', 'continuous'], ['single', 'draft', 'draft'], ['continuous', 'send', 'send'], ['single', 'send', 'send'], ['continuous', undefined, 'send']];
    for (const [flag, host, expected] of table) assert.equal(store.messageMode(flag, host), expected, `${flag}/${host}`);
});

test('A choice applies at once, is adopted without a "changed elsewhere" banner, and other unsaved edits stay', async () => {
    const r = await rig({ flag: 'single', host: 'draft' });
    r.store.config.style = 'an unsaved persona';                                  // another card's edit, not saved yet
    let release; r.server.hold = new Promise(resolve => { release = resolve; });
    const applying = r.store.applyMessageMode('continuous', r.select);
    await flush();
    assert.equal(r.store.ui.dictation.applying, true);
    r.server.revision = 'r2'; await r.store.noteRevision('r2');                  // the status poll sees the write before its answer arrives
    assert.equal(r.store.stale, false);
    release(); assert.equal(await applying, true);
    assert.equal(JSON.stringify(r.server.calls.find(([url]) => url === '/plugins/_convo/stt_config')[1]), JSON.stringify({ message_mode: 'continuous' }));
    assert.equal(r.store.config.voice_message_draft, 'continuous');
    assert.equal(r.store.native.whisper.summary.message_mode, 'draft');
    assert.equal(r.select.value, 'continuous');
    assert.equal(r.shown(), 'continuous');
    assert.equal(r.store.revision, r.server.revision);
    await r.store.noteRevision(r.server.revision);
    assert.deepEqual([r.store.stale, r.store.conflicts.length], [false, 0]);
    assert.equal(r.store.config.style, 'an unsaved persona');                     // kept, still unsaved
    assert.equal(r.store.dirty, true);
    assert.equal(r.whisper.config.message_mode, 'draft');                         // the host microphone follows now
    assert.equal(r.whisper.config.silence_duration, 1000);
    assert.equal(r.refreshed.length, 1);                                          // the orb and recorder read the new flag
    assert.equal(r.store.message, 'Voice messages: Draft in composer (continuous). Applied now.');
    assert.equal(r.store.ui.dictation.applying, false);
    assert.equal(r.store.busy, false);
});

test('Back to a single draft or to sending: the flag goes back to single and the select agrees', async () => {
    const r = await rig({ flag: 'continuous', host: 'draft' });
    assert.equal(r.shown(), 'continuous');
    await r.store.applyMessageMode('draft', r.select);
    assert.deepEqual([r.store.config.voice_message_draft, r.select.value, r.whisper.config.message_mode, r.store.dirty], ['single', 'draft', 'draft', false]);
    await r.store.applyMessageMode('send', r.select);
    assert.deepEqual([r.store.config.voice_message_draft, r.select.value, r.whisper.config.message_mode, r.store.dirty], ['single', 'send', 'send', false]);
});

test('A failed write is reported and the select goes back to what is really stored; a second choice while one applies waits its turn', async () => {
    const failing = await rig({ flag: 'single', host: 'send', failBridge: true });
    failing.select.value = 'continuous';                                          // what the user picked
    assert.equal(await failing.store.applyMessageMode('continuous', failing.select), false);
    assert.match(failing.store.error, /could not be saved/);
    assert.equal(failing.select.value, 'send');                                   // re-synced, never showing what did not happen
    assert.deepEqual([failing.store.busy, failing.store.ui.dictation.applying, failing.store.config.voice_message_draft], [false, false, 'single']);
    const r = await rig({ flag: 'single', host: 'draft' });
    let release; r.server.hold = new Promise(resolve => { release = resolve; });
    const first = r.store.applyMessageMode('continuous', r.select);
    await flush();
    const other = { value: 'send' };
    assert.equal(await r.store.applyMessageMode('send', other), false);           // refused while the first is in flight ...
    assert.equal(other.value, 'draft');                                           // ... and put back to the stored value
    release(); await first;
    assert.equal(r.server.calls.filter(([url]) => url === '/plugins/_convo/stt_config').length, 1);
    assert.equal(await r.store.applyMessageMode('sometimes', other), false);       // not one of the three
    assert.equal(other.value, 'continuous');
});
