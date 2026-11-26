// The Convo store's side of the sound, history and diagnostics changes (convo-store.js on stubbed host modules): this device's
// sound values reach the capture, overlap probes carry "Interrupt after", held decisions stay developer-only, the history names the
// assistant, a new build offers a reload, and a test clip is saved instead of being sent as a turn.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { storeFixture } from './store-fixture.mjs';
import { foldTurns, heldText } from '../webui/view-helpers.js';

const plain = value => JSON.parse(JSON.stringify(value));
const memory = () => { const values = new Map(); return { values, getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key) }; };

test('The capture starts with this device\'s sound values over the saved defaults, and overlap probes carry barge_in_s', async () => {
    const localStorage = memory();
    localStorage.setItem('convo.sound.v1:mic|default', JSON.stringify({ barge_in_seconds: 1.6, noise_gate_db: -42 }));
    const f = await storeFixture({ settings: { noise_gate_db: -50, end_of_turn_seconds: 1.1, echo_guard: 'strict' }, globals: { localStorage } });
    await f.store.start();
    const [audio] = f.audios;
    assert.deepEqual({ ...audio.sound }, { noise_gate_db: -42, barge_in_seconds: 1.6, end_of_turn_seconds: 1.1, echo_guard: 'strict' });
    assert.equal(audio.echoGuard, 'strict');
    audio.overlapAtOnset = () => ({ overlap: true, heard: { phrase_id: 'p', played_s: .4 } });
    audio.onStart();
    audio.onProbe('UklGRg==', { voiced_s: .5 });
    await new Promise(resolve => setTimeout(resolve, 0));
    const probe = f.requests.find(item => item.event === 'convo.candidate');
    assert.equal(probe.data.barge_in_s, 1.6); assert.equal(probe.data.overlap, true);
    audio.onTurn('UklGRg==', { voiced_s: .9 });
    await new Promise(resolve => setTimeout(resolve, 0));
    const final = f.requests.filter(item => item.event === 'convo.candidate').at(-1);
    assert.equal(final.data.final, true); assert.equal('barge_in_s' in final.data, false, 'only probes carry it');
    // Live: a change from the drawer reaches the capture; Off waits for the next start.
    audio.setSound = values => { audio.applied = values; }; audio.setEchoGuard = mode => { audio.mode = mode; }; audio.echoGuard = 'strict';
    assert.equal(f.store.applySound({ ...audio.sound, noise_gate_db: -30, echo_guard: 'adaptive' }, 'all'), '');
    assert.equal(audio.applied.noise_gate_db, -30); assert.equal(audio.mode, 'adaptive');
    assert.match(f.store.applySound({ ...audio.sound, echo_guard: 'off' }, 'echo_guard'), /next time you start/);
    audio.onMeter({ level: .2, withheld: true, voiced: false, state: 'coupled', playback: true });
    assert.deepEqual({ ...f.store.meter }, { level: .2, withheld: true, voiced: false, state: 'coupled', playback: true });
    await f.store.stop();
});

test('A held decision: the live notice is developer-only; the history keeps the words and adds the note only for developers', async () => {
    const toasts = [];
    const f = await storeFixture({ globals: { justToast: message => toasts.push(message) } });
    await f.store.start();
    f.handlers['convo.event']({ data: { type: 'notice', kind: 'held', session_id: 's', decision: 'silent', reason: 'side_talk' } });
    assert.equal(f.store.note, ''); assert.deepEqual(toasts, []);
    f.store.settings.developer_mode = true;
    f.handlers['convo.event']({ data: { type: 'notice', kind: 'held', session_id: 's', decision: 'wait', reason: '' } });
    assert.equal(f.store.note, 'Waiting for you to finish');
    await f.store.stop();
    const ev = (seq, turn, kind, payload) => ({ seq, session: 's', turn, kind, payload });
    const events = [ev(1, 't1', 'user.accepted', { transcript: 'what about Bob' }), ev(2, 't1', 'turn.held', { decision: 'silent', reason: 'side_talk' }),
                    ev(3, 't2', 'turn.held', { decision: 'wait', reason: 'unfinished', transcript: 'and then' }), ev(4, 't3', 'turn.held', { decision: 'silent', reason: 'noise' })];
    assert.deepEqual(foldTurns(events).map(entry => [entry.key, entry.text, entry.detail]), [[1, 'what about Bob', ''], [3, 'and then', '']]);
    assert.deepEqual(foldTurns(events, { developer: true }).map(entry => [entry.key, entry.detail]),
        [[1, '· stayed silent: side talk'], [3, '· waiting for you to finish'], [4, 'No words captured · stayed silent: noise']]);
    assert.equal(heldText({ decision: 'silent' }), '· stayed silent');
});

test('The history names the assistant from assistant_name; generated and diagnostic rows need their developer settings', async () => {
    const ev = (seq, turn, kind, payload) => ({ seq, session: 's', turn, kind, payload });
    const events = [ev(1, 't', 'user.accepted', { transcript: 'hello' }), ev(2, 't', 'assistant.playback_started', {}), ev(3, 't', 'assistant.delivered', { text: 'Hi.' }),
                    ev(4, 'u', 'user.accepted', { transcript: 'and' }), ev(5, 'u', 'assistant.generated', { text: 'Never spoken.' }), { seq: 6, kind: 'context.compacted', payload: {} }];
    const f = await storeFixture({ settings: { developer_mode: true }, api: (_url, payload) => (payload.action === 'history' ? { events, jobs: [], more: false, assistant_name: 'J.A.R.V.I.S' } : undefined) });
    await f.store.mountPanel();
    assert.deepEqual(plain(f.store.visibleEvents.map(entry => [entry.key ?? entry.seq, entry.label ?? entry.kind])), [[1, 'You'], [3, 'J.A.R.V.I.S'], [4, 'You']]);
    f.store.settings.show_rejected_responses = true;
    assert.deepEqual(f.store.visibleEvents.map(entry => entry.label ?? entry.kind).at(-1), 'J.A.R.V.I.S · generated, not delivered');
    f.store.settings.show_diagnostic_events = true;
    assert.ok(f.store.visibleEvents.some(entry => entry.kind === 'context.compacted'));
    f.store.settings.developer_mode = false;                                  // the two settings mean nothing outside developer mode
    assert.deepEqual(plain(f.store.visibleEvents.map(entry => entry.label ?? entry.kind)), ["You", "J.A.R.V.I.S", "You"]);
    assert.equal(foldTurns(events.slice(0, 3))[1].label, 'Convo', 'no name given: Convo');
    assert.ok(!foldTurns(events, { developer: true, diagnostics: false }).some(entry => entry.kind === 'context.compacted'));
});

test('A new build offers a reload once, never reloads by itself', async () => {
    let build = 'b1';
    const f = await storeFixture({ api: (_url, payload) => (payload.action === 'status' ? { settings: { enabled: true, max_audio_seconds: 20 }, build } : undefined) });
    await f.store.refresh();
    assert.equal(f.store.build, 'b1'); assert.equal(f.store.updateAvailable, false);
    await f.store.refresh(); assert.equal(f.store.updateAvailable, false);
    build = 'b2'; await f.store.refresh();
    assert.equal(f.store.updateAvailable, true); assert.equal(f.store.build, 'b1');
});

test('Test clips: armed only in a session; the next utterance is saved through clip_save and never sent as a turn', async () => {
    const saved = [];
    const f = await storeFixture({ api: (_url, payload) => {
        if (payload.action === 'clip_save') { saved.push(payload); return { ok: true, id: 'c1' }; }
        if (payload.action === 'clip_list') return { ok: true, clips: saved.map((item, i) => ({ id: 'c' + (i + 1), label: item.label, seconds: 1, at: 1, device: item.device })) };
        if (payload.action === 'clip_delete') { saved.splice(0, 1); return { ok: true }; }
        return undefined;
    } });
    assert.equal(f.store.armClip('request'), false);
    await f.store.start();
    assert.equal(f.store.armClip('nonsense'), false);
    assert.equal(f.store.armClip('side_talk'), true);
    const [audio] = f.audios;
    audio.onStart();
    audio.onProbe('UklGRg==', { voiced_s: .5 });
    const wav = Buffer.alloc(44 + 32000).toString('base64');                 // one second of 16 kHz PCM16
    audio.onTurn(wav, { voiced_s: .9 });
    for (let n = 0; n < 5; n++) await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(f.requests.some(item => item.event === 'convo.candidate'), false, 'no probe and no turn reached the server');
    assert.equal(saved.length, 1); assert.equal(saved[0].label, 'side_talk'); assert.equal(saved[0].audio, wav);
    assert.equal(typeof saved[0].device, 'string'); assert.ok(saved[0].device.length <= 40);
    assert.match(f.store.clipNote, /Saved a 1\.0 s “side talk” clip/);
    assert.equal(f.store.clips.length, 1);
    await f.store.clipDelete('c1');
    assert.equal(f.store.clips.length, 0);
    await f.store.stop();
});
