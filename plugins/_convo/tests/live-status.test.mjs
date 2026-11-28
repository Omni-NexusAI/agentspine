// The panel's live status: the chip says what the orb shows (one state, the orb's priority), with a pulse and a small level bar
// while Convo hears you; a server activity ("Searching the web…") is the status until the next state, text or error. A Whisper
// failure is a note, never the error state, and a written-down turn refreshes the history.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { storeFixture } from './store-fixture.mjs';

const settle = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
const plain = value => JSON.parse(JSON.stringify(value));
const read = name => readFile(new URL('../webui/' + name, import.meta.url), 'utf8');
const chip = f => [f.store.statusLabel, f.store.chipState];

test('The chip follows the orb: ready, starting, listening, hearing you (with its level), thinking, speaking while audio plays, needs attention', async () => {
    const f = await storeFixture();
    await f.store.refresh();
    assert.deepEqual(chip(f), ['Ready · microphone off', 'ready']);
    f.store.busy = true; f.store.setPressing(true);                                // any repaint: a start in progress
    assert.deepEqual(chip(f), ['Starting', 'connecting']);
    f.store.busy = false; f.store.setPressing(false);
    await f.store.start();
    assert.deepEqual(chip(f), ['Listening', 'listening']);
    const audio = f.audios[0], send = event => f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, ...event } });
    audio.onStart();
    assert.deepEqual(chip(f), ['Hearing you', 'hearing']);
    audio.onLevel('input', .53); await settle(60); audio.onLevel('input', .53);
    assert.equal(f.store.liveLevel, .55);                                          // in 5% steps: not repainted at the metering rate
    send({ type: 'state', state: 'considering' });
    assert.deepEqual(chip(f), ['Thinking', 'thinking']);
    assert.equal(f.store.liveLevel, 0);                                            // the bar belongs to hearing only
    send({ type: 'audio.begin', turn_id: 'r', phrase_id: 'p' });
    assert.deepEqual(chip(f), ['Speaking', 'speaking']);
    send({ type: 'state', state: 'listening' });                                   // the server's turn is over ...
    await settle(60);                                                              // (levels repaint at most every 50 ms)
    audio.playing = {}; audio.onLevel('output', .4);                               // ... while its audio still plays
    assert.deepEqual(chip(f), ['Speaking', 'speaking']);
    audio.playing = null; audio.onLevel('output', 0);
    f.store.error = 'Provider down'; f.store.setPressing(true);
    assert.deepEqual(chip(f), ['Needs attention', 'error']);
    f.store.error = ''; f.store.setPressing(false);
    await f.store.stop();
    assert.deepEqual(chip(f), ['Ready · microphone off', 'ready']);
    await f.store.beginDictation();
    assert.deepEqual(chip(f), ['Dictating', 'dictation']);
    await f.store.endDictation();
    f.store.settings = null; f.store.setPressing(true);                             // Convo does not own the orb: the old wording
    assert.equal(f.store.live, null);
    assert.deepEqual(chip(f), ['Ready · microphone off', 'ready']);
});

test('Waiting for a model, then an activity as the status until the next state, text or error; the activity never hides speech', async () => {
    const f = await storeFixture();
    await f.store.start();
    const audio = f.audios[0], send = event => f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, ...event } });
    send({ type: 'notice', kind: 'loading', message: 'Loading Gemma on desktop…' });
    assert.deepEqual(chip(f), ['Waiting for a model', 'loading']);
    send({ type: 'state', state: 'considering' });
    for (const ending of [{ type: 'state', state: 'considering' }, { type: 'text.delta', turn_id: 't', text: 'Here' }, { type: 'error', message: 'Model failed' }]) {
        f.store.error = '';
        send({ type: 'notice', kind: 'activity', message: 'Searching the web…' });
        assert.deepEqual(chip(f), ['Searching the web…', 'activity'], JSON.stringify(ending));
        send(ending);
        assert.equal(f.store.activity, '', JSON.stringify(ending));
    }
    f.store.error = '';
    send({ type: 'notice', kind: 'activity', message: 'x'.repeat(400) });
    assert.equal(f.store.activity.length, 200);                                    // bounded
    send({ type: 'audio.begin', turn_id: 'r', phrase_id: 'p' });
    assert.deepEqual(chip(f), ['Speaking', 'speaking']);                           // what is audible outranks what the reply is doing
    audio.onStart();
    send({ type: 'notice', kind: 'other', message: 'A note' });                    // other notices stay toasts
    assert.equal(f.store.activity, 'x'.repeat(200));
    await f.store.stop();
    assert.deepEqual([f.store.activity, f.store.loading], ['', '']);
});

test('The chip markup: the level bar is hidden from assistive technology and shown only while hearing; the pulse respects reduced motion', async () => {
    const html = await read('main.html');
    const markup = html.slice(0, html.indexOf('<style>')), css = html.slice(html.indexOf('<style>'));
    assert.match(markup, /<span class="convo-chip" :data-state="\$store\.convo\.chipState" role="status" aria-live="polite"><span class="convo-dot" aria-hidden="true"><\/span><span x-text="\$store\.convo\.statusLabel"><\/span><span class="convo-level" aria-hidden="true" x-show="\$store\.convo\.live === 'hearing'" :style="'--level:' \+ \$store\.convo\.liveLevel"><i><\/i><\/span><\/span>/);
    assert.match(css, /\.convo-chip\[data-state=hearing\] \.convo-dot\{[^}]*animation:convo-pulse/);
    assert.match(css, /@keyframes convo-pulse\{/);
    for (const state of ['hearing', 'loading', 'activity', 'listening', 'speaking', 'thinking', 'connecting', 'dictation', 'error'])
        assert.match(css, new RegExp(`\\.convo-chip\\[data-state=${state}\\]`), state);
    const reduced = css.slice(css.indexOf('@media(prefers-reduced-motion:reduce)'));
    assert.match(reduced, /\.convo-chip \.convo-dot\{animation:none\}/);
    assert.match(reduced, /\.convo-level>i\{transition:none\}/);
    assert.match(markup, /<p class="convo-hint" x-show="\$store\.convo\.hintText" x-text="\$store\.convo\.hintText"><\/p>/);
    assert.match(await read('convo-store.js'), /get hintText\(\) \{\s*if \(this\.note \|\| this\.waiting\) return this\.note \|\| this\.waiting;/);    // what the session waits for, in words
});

async function admittedTurn(transcribe, { wanted = true, globals = {} } = {}) {
    const toasts = [], history = [], asked = [];
    const f = await storeFixture({ settings: { transcription_enabled: true }, globals: { justToast: (message, kind) => toasts.push([message, kind]), ...globals },
        api: async (url, payload) => {
            if (url === '/plugins/_whisper_stt/transcribe') { asked.push(payload); return transcribe(); }
            if (payload.action === 'history') { history.push(payload); return { events: [], jobs: [] }; }
            return undefined;
        } });
    await f.store.start();
    const audio = f.audios[0];
    audio.onStart(); audio.onTurn('final-wave', { voiced_s: .8 });
    await settle();
    const final = f.requests.find(item => item.event === 'convo.candidate' && item.data.final).data;
    const before = history.length;
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, type: 'candidate.state', turn_id: final.turn_id, sequence: final.sequence, state: 'admit', final: true } });
    await settle(10);
    const atAdmit = asked.length;
    // The server asks for the words only after the reply was heard (and never for a turn the model declined), with the turn's audio.
    if (wanted) f.handlers['convo.event']({ data: { session_id: 's', epoch: 1, type: 'transcript.wanted', turn_id: final.turn_id, audio: 'joined-wave' } });
    await settle(10);
    return { f, toasts, final, asked, atAdmit, history, refreshed: history.length - before };
}

test('Admitting a turn asks Whisper nothing: the server asks once the reply was heard, and never for a declined turn', async () => {
    const { f, asked } = await admittedTurn(async () => ({ text: 'Background noise' }), { wanted: false });
    assert.equal(asked.length, 0);
    assert.equal(f.requests.some(item => item.event === 'convo.transcript' || item.event === 'convo.turn_audio'), false);
    await f.store.stop();
});

test('The browser asks Agent Zero\'s Whisper for the audio the server sent, one request at a time', async () => {
    let release;
    const { f, asked, final } = await admittedTurn(() => new Promise(resolve => { release = resolve; }));
    assert.equal(asked.length, 1);
    assert.equal(asked[0].audio, 'joined-wave');                                   // the whole joined utterance, from the server
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 1, type: 'transcript.wanted', turn_id: 'later', audio: 'other-wave' } });
    await settle(10);
    assert.equal(asked.length, 1);                                                 // one at a time: the later turn is skipped
    release({ text: 'Remember the milk' });
    await settle(10);
    assert.deepEqual(f.requests.filter(item => item.event === 'convo.transcript').map(item => item.data.turn_id), [final.turn_id]);
    await f.store.stop();
});

test('Convo stops waiting for a Whisper request after about 15 seconds, and the request still counts until the host answers', async () => {
    const timers = [];
    const fakeTimeout = (fn, ms) => { if (ms === 15000) { timers.push(fn); return timers.length; } return setTimeout(fn, ms); };
    const { f, asked } = await admittedTurn(() => new Promise(() => {}), { globals: { setTimeout: fakeTimeout, clearTimeout: id => { if (typeof id !== 'number' || id > 1000) clearTimeout(id); } } });
    assert.equal(f.namespace.WHISPER_ABORT_MS, 15000);
    assert.equal(asked.length, 1);
    assert.equal(timers.length, 1);
    timers[0]();                                                                   // 15 s passed
    await settle(10);
    assert.match(f.store.note, /Whisper could not write down that turn/);
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 1, type: 'transcript.wanted', turn_id: 'later', audio: 'other-wave' } });
    await settle(10);
    assert.equal(asked.length, 1);                                                 // the host has not answered the first one yet
    await f.store.stop();
});

test('A turn Whisper wrote down is sent as the transcript and the history refreshes to show it', async () => {
    const { f, final, refreshed } = await admittedTurn(async () => ({ text: 'Remember the milk' }));
    const sent = f.requests.find(item => item.event === 'convo.transcript');
    assert.deepEqual(plain(sent.data), { session_id: 's', epoch: 0, turn_id: final.turn_id, text: 'Remember the milk' });
    assert.equal(refreshed, 1);
    await f.store.stop();
});

test('A Whisper failure is a note and a toast, never the error state: the audio conversation is unaffected', async () => {
    const { f, toasts, refreshed } = await admittedTurn(async () => { throw new Error('Whisper is not loaded'); });
    assert.equal(f.store.error, '');
    assert.match(f.store.note, /Whisper could not write down that turn[\s\S]*the conversation is unaffected/);
    assert.deepEqual(plain(toasts), [[f.store.note, 'info']]);
    assert.equal(f.requests.some(item => item.event === 'convo.transcript'), false);
    assert.equal(refreshed, 0);
    assert.notEqual(f.store.live, 'error');
    await f.store.stop();
});

test('A transcript the server wrote down itself refreshes the history, whatever the epoch now', async () => {
    const { f, history, asked } = await admittedTurn(async () => ({ text: 'unused' }), { wanted: false });
    const before = history.length;
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 7, type: 'transcript.saved', turn_id: 'x' } });
    await settle(10);
    assert.equal(asked.length, 0);
    assert.equal(history.length - before, 1);
    await f.store.stop();
});

test('A text-only conversation model\'s turn asks Convo\'s Whisper worker on the server, not the host, before it is sent', async () => {
    const asked = [];
    const f = await storeFixture({ settings: { text_input_required: true },
        api: async (url) => { if (url === '/plugins/_whisper_stt/transcribe') { asked.push(url); return { text: 'never' }; } return undefined; } });
    await f.store.start();
    const audio = f.audios[0];
    audio.onStart(); audio.onTurn('final-wave', { voiced_s: .8 });
    await settle(10);
    const whisper = f.requests.find(item => item.event === 'convo.whisper');
    assert.equal(whisper?.data.audio, 'final-wave');
    assert.equal(asked.length, 0);
    await f.store.stop();
});

test('The setting says what it does now, and the unreachable direct-turn path is gone', async () => {
    const settings = await read('settings-controls.html');
    assert.match(settings, /<div class="field-title">Remember what you said \(Whisper transcripts\)<\/div>.*?x-model="\$store\.convoSetup\.config\.transcription_enabled"[^>]*><span class="toggler"><\/span><\/label><\/div><\/div>\s*<p class="convo-hint">[^<]*the conversation model sees your words in later turns/);
    assert.doesNotMatch(settings, /Show parallel Whisper transcripts/);
    const store = await read('convo-store.js');
    assert.doesNotMatch(store, /request\('turn'/);                                  // a direct-audio turn always goes through the candidate path
    assert.doesNotMatch(store, /this\.error = 'Transcription unavailable/);
});
