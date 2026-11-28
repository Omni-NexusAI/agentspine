// Talking over the assistant (overlap), browser side: the onset ducks at once and probes quicken, every observation says how much
// voiced speech it holds, an overlapping one says so and which phrase was being heard, `continue` lets the reply play on and lifts
// the duck soon after the user stops, `listen` keeps it, `ignore` may say why, and an admission may fade the reply out.
// The audio graph is a recording stub: this proves the logic and the protocol, not how anything sounds.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { AudioSession, CONTINUE_RELEASE_MS, DUCK, OVERLAP_PROBE_SECONDS, OVERLAP_TAIL_MS, PROBE_SECONDS, SpeechBoundary } from '../webui/audio.js';
import { ECHO_TAIL_MS } from '../webui/echo-guard.js';
import { loadModule, hostStubs } from './vm-loader.mjs';

const settle = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
const plain = value => JSON.parse(JSON.stringify(value));
const BLOCK = 2000, RATE = 16000;                                      // 125 ms blocks: exact in binary, so cadences compare exactly
const voiced = () => new Float32Array(BLOCK).fill(.1), quiet = () => new Float32Array(BLOCK);
const seconds = chunks => chunks.reduce((n, chunk) => n + chunk.length, 0) / RATE;

// ---- the speech boundary ---------------------------------------------------------------------------------------------------

test('Overlap probes at the onset and then every 0.5 s; the next utterance is back to the ordinary 0.75 s cadence', () => {
    assert.deepEqual([PROBE_SECONDS, OVERLAP_PROBE_SECONDS], [.75, .5]);
    const probes = [], turns = [];
    let overlapping = true;
    const boundary = new SpeechBoundary({ rate: RATE, onStart: () => { if (overlapping) boundary.quicken(); }, onProbe: chunks => probes.push(seconds(chunks)), onTurn: chunks => turns.push(chunks) });
    for (let n = 0; n < 12; n++) boundary.push(voiced());                // 1.5 s of speech over the reply
    assert.deepEqual(probes, [.5, 1, 1.5]);                              // the first at the onset (admission), then every 0.5 s
    for (let n = 0; n < 8; n++) boundary.push(quiet());
    assert.equal(turns.length, 1);
    assert.equal(boundary.lastTurn.voiced, 1.5);                         // the confirmed voiced speech of the turn
    overlapping = false; probes.length = 0;
    for (let n = 0; n < 12; n++) boundary.push(voiced());
    assert.deepEqual(probes, [.75, 1.5]);                                // an ordinary utterance: unchanged
});

test('The open utterance knows how long ago its first voiced sample was, pre-roll excluded', () => {
    const boundary = new SpeechBoundary({ rate: RATE, preRoll: .25, onStart: () => {}, onTurn: () => {} });
    assert.equal(boundary.spokenFor(), 0);
    boundary.push(new Float32Array(BLOCK).fill(1e-4)); boundary.push(new Float32Array(BLOCK).fill(1e-4));   // 250 ms of pre-roll
    for (let n = 0; n < 4; n++) boundary.push(voiced());
    assert.equal(boundary.spokenFor(), .5);                              // the echo guard's released onset, not the pre-roll
    assert.equal(boundary.duration, .75);
});

// ---- the audio session: what was heard, and when -------------------------------------------------------------------------------

function session(options = {}) {
    const audio = new AudioSession({ onStart() {}, onTurn() {}, ...options });
    audio.context = { state: 'running', currentTime: 10, outputLatency: .1 };
    return audio;
}

test('Overlap counts what the user heard: output latency included, the echo tail after a phrase ended, never a phrase not yet heard', () => {
    assert.equal(OVERLAP_TAIL_MS, ECHO_TAIL_MS);                         // the same tail the echo guard holds back
    const audio = session();
    assert.deepEqual(plain(audio.overlap(.5)), { overlap: false, heard: null });   // nothing played yet
    const first = audio.remember('p1', 8);                               // rendered from 8 s on, still playing
    // Heard now = 10 - .1 latency = 9.9; the user began .5 s before that, at 9.4: 1.4 s of p1 had been heard.
    assert.deepEqual(plain(audio.overlap(.5)), { overlap: true, heard: { phrase_id: 'p1', played_s: 1.4 } });
    audio.heardUntil(first, 9.2);                                        // it ended .2 s before the user began: inside the 350 ms tail
    assert.deepEqual(plain(audio.overlap(.5)), { overlap: true, heard: { phrase_id: 'p1', played_s: 1.2 } });
    first.end = 8.9;                                                     // ended .5 s before: no longer overlap
    assert.deepEqual(plain(audio.overlap(.5)), { overlap: false, heard: null });
    audio.remember('early', 9.95).end = 9.97;                            // scheduled, never heard before the user spoke
    audio.heardUntil(audio.remember('cut', 9.6), 9.5);                   // interrupted before it started: never audible
    assert.equal(audio.overlap(.5).overlap, false);
    audio.remember('p2', 9.6);                                           // a reply that started after the user began: overlap, nothing heard at the start
    assert.deepEqual(plain(audio.overlap(.5)), { overlap: true, heard: null });
    audio.context.state = 'suspended';                                   // a paused output is not heard
    assert.equal(audio.overlap(.5).overlap, false);
    audio.context = { state: 'running', currentTime: 10 };               // no latency reported: rendered = heard
    assert.equal(audio.overlap(0).heard.phrase_id, 'p2');
    for (let n = 0; n < 30; n++) audio.remember('x' + n, 1);
    assert.equal(audio.timeline.length, 16);                             // bounded
});

test('At the onset the true start is the released onset before now', () => {
    const audio = session();
    audio.remember('p1', 9);
    audio.boundary = { spokenFor: () => .6 };                            // the guard confirmed 600 ms of speech and released it at once
    assert.deepEqual(plain(audio.overlapAtOnset()), { overlap: true, heard: { phrase_id: 'p1', played_s: .3 } });   // 9.9 - .6 - 9
    let quickened = null;
    audio.boundary.quicken = value => { quickened = value; };
    audio.quicken();
    assert.equal(quickened, OVERLAP_PROBE_SECONDS);
});

test('`continue`: playback goes on and the duck lifts 400 ms after the user stops talking, unless a new decision ducks again', () => {
    assert.equal(CONTINUE_RELEASE_MS, 400);
    const applied = [];
    const audio = new AudioSession({ onStart() {}, onTurn() {} });
    audio.outputGain = { gain: { set value(v) { applied.push(v); }, get value() { return applied.at(-1) ?? 1; } } };
    audio.boundary = new SpeechBoundary({ rate: RATE, onStart() {}, onTurn() {} });
    try {
        assert.equal(audio.releaseAfterSpeech(), false);                 // nothing ducked: nothing to lift
        for (let n = 0; n < 6; n++) audio.boundary.push(voiced());       // the user is talking over the reply
        audio.setPolicyHold(true);
        assert.deepEqual(applied, [DUCK.level]);
        assert.equal(audio.releaseAfterSpeech(), true);
        audio.boundary.push(voiced()); audio.checkQuiet();
        audio.boundary.push(quiet()); audio.boundary.push(quiet()); audio.boundary.push(quiet()); audio.checkQuiet();
        assert.equal(audio.duck.ducked, true);                           // 375 ms of quiet: not yet
        audio.boundary.push(quiet()); audio.checkQuiet();
        assert.equal(audio.duck.ducked, false);                          // 500 ms: lifted
        assert.deepEqual(applied, [DUCK.level, 1]);
        audio.setPolicyHold(true); audio.releaseAfterSpeech();           // again, then a newer decision ducks once more
        audio.setPolicyHold(true);
        for (let n = 0; n < 6; n++) audio.boundary.push(quiet());
        audio.checkQuiet();
        assert.equal(audio.duck.ducked, true);
        audio.setPolicyHold(false, true); audio.releaseAfterSpeech();
        assert.equal(audio.duck.ducked, false);
        for (let n = 0; n < 8; n++) audio.boundary.push(quiet());          // the utterance ended: no utterance open is quiet enough
        audio.setPolicyHold(true); assert.equal(audio.releaseAfterSpeech(), true);
        assert.equal(audio.duck.ducked, false);
    } finally { audio.duck.dispose(); }
});

// A context whose nodes record what they are asked to do.
function graph() {
    const log = { ramps: [], targets: [], stops: [], disconnected: [] };
    const node = name => ({ name, connect(target) { return target; }, disconnect() { log.disconnected.push(name); } });
    const param = name => ({ value: 1, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime: (value, at) => log.ramps.push([name, value, at]),
                             setTargetAtTime: (value) => log.targets.push(value) });
    let gains = 0;
    const context = { state: 'running', currentTime: 5, destination: node('destination'), async close() { this.state = 'closed'; },
        createGain() { const gain = node('gain' + (++gains)); gain.gain = param(gain.name); return gain; },
        createBuffer: (_, length, rate) => ({ duration: length / rate, values: new Float32Array(length), getChannelData() { return this.values; } }),
        createBufferSource() { const source = { ...node('source'), start(when) { this.when = when; }, stop(when) { log.stops.push(when); } }; return source; } };
    return { context, log };
}

test('An admission with fade_ms fades the playing reply to silence on its own branch, then stops it; nothing is acknowledged after', async () => {
    const { context, log } = graph(), events = [], errors = [];
    const audio = new AudioSession({ onError: error => errors.push(error.message) });
    audio.context = context; audio.outputGain = context.createGain();
    const begin = { session_id: 's', epoch: 0, turn_id: 't', phrase_id: 'p', format: 'pcm_s16le', channels: 1, sample_rate: 24000 };
    audio.pcmBegin(begin, () => events.push('completed'), () => events.push('started'));
    audio.pcmChunk({ ...begin, sequence: 0, audio: Buffer.alloc(4800).toString('base64') });
    const branch = audio.playbackGain, generation = audio.playbackGeneration;
    assert.ok(branch && branch !== audio.outputGain);                    // playback has its own gain behind the duck
    audio.setPolicyHold(true);                                           // the user talks over it: ducked
    assert.deepEqual(log.targets, [DUCK.level]);
    audio.interrupt(100);
    assert.equal(audio.playbackGeneration, generation + 1);              // revoked at once, exactly as interrupt() always did
    assert.deepEqual(log.ramps, [[branch.name, 0, 5.1]]);                 // the old branch ramps to silence over 100 ms
    assert.deepEqual(log.stops, [5.1]);                                  // and its sources stop at the end of the fade
    assert.equal(audio.pcm.size, 0);
    assert.equal(audio.playbackGain, null);                              // the next phrase gets a fresh branch at full level
    audio.setPolicyHold(false, true);                                    // the admission releases the duck ...
    assert.deepEqual(log.targets, [DUCK.level]);                         // ... but the rise waits for the fade, or the old audio would swell
    await settle(160);
    assert.deepEqual(log.targets, [DUCK.level, 1]);
    assert.ok(log.disconnected.includes(branch.name));                   // the faded branch is taken down afterwards
    assert.deepEqual(events, []); assert.deepEqual(errors, []);
    audio.interrupt(5000);                                               // bounded, and nothing to fade: an ordinary stop
    audio.close();
});

// ---- the store: the protocol fields and the decisions -----------------------------------------------------------------------

async function storeRig(t, { respond } = {}) {
    const requests = [], audios = [], handlers = {};
    class Audio {
        constructor(options) { Object.assign(this, options); this.calls = []; this.next = null; audios.push(this); }
        async start() {}
        overlapAtOnset() { this.calls.push('overlap?'); return this.next || { overlap: false, heard: null }; }
        quicken() { this.calls.push('quicken'); }
        setPolicyHold(held, immediate = false) { this.calls.push(['duck', held, immediate]); }
        releaseAfterSpeech() { this.calls.push('releaseAfterSpeech'); return this.ducked !== false; }
        interrupt(fade) { this.calls.push(['interrupt', fade]); }
        pcmBegin() { return true; } pcmChunk() {} pcmEnd() {} pcmAbort() {} enqueue() {} close() {}
    }
    const bindings = hostStubs({
        '/js/api.js': { callJsonApi: async (_url, payload) => payload.action === 'status' ? { settings: { enabled: true, max_audio_seconds: 20 } } : { events: [], jobs: [] } },
        '/js/websocket.js': { createNamespacedClient: () => ({ addHandlers() {}, async on(name, fn) { handlers[name] = fn; }, onDisconnect() {}, disconnect() {},
            async request(event, data) {
                requests.push({ event, data: plain(data) });
                return respond?.(event, data) || { results: [{ handlerId: 'plugin.Realtime', ok: true, data: { ok: true, session: { id: 's', epoch: 0 } } }] };
            } }) },
        '/js/shortcuts.js': { getCurrentContextId: () => 'a', getCurrentContext: () => ({ id: 'a', name: 'Chat a' }), openModal: async () => {} },
        './audio.js': { AudioSession: Audio },
        '/plugins/_whisper_stt/webui/whisper-stt-store.js': { store: { microphoneInput: null, getSelectedDevice: () => ({}), async initRuntime() {} } },
    });
    const { namespace } = await loadModule('webui/convo-store.js', { bindings, globals: {
        document: { hidden: false, addEventListener() {}, getElementById: () => null }, window: { addEventListener() {}, confirm: () => true }, fetch: async () => ({ ok: true }) } });
    const store = namespace.store;
    await store.start();
    t.after(() => store.stop());
    store.holdSafetyMs = 60000;
    const audio = audios[0];
    const send = event => handlers['convo.event']({ data: { session_id: 's', epoch: store.session?.epoch ?? 0, ...event } });
    const sent = () => requests.filter(item => item.event === 'convo.candidate').map(item => item.data);
    return { store, audio, send, sent, requests, ducks: () => audio.calls.filter(call => call[0] === 'duck') };
}

test('Speech that begins over the reply ducks at once and probes at once, before anything was sent; its observations carry overlap and heard', async t => {
    const f = await storeRig(t);
    f.audio.next = { overlap: true, heard: { phrase_id: 'p3', played_s: 1.25 } };
    f.audio.onStart();
    assert.deepEqual(f.audio.calls, ['overlap?', ['duck', true, false], 'quicken']);   // duck, then the quicker probes; no cancel
    assert.equal(f.store.policyHold, true);
    assert.equal(f.sent().length, 0);
    f.audio.onProbe('wave', { voiced_s: .5 }); await settle();
    f.audio.onProbe('wave-2', { voiced_s: 1 }); await settle();
    f.audio.onTurn('final-wave', { voiced_s: 1.375 }); await settle();
    const [first, second, final] = f.sent();
    assert.deepEqual([first.overlap, first.voiced_s, first.heard], [true, .5, { phrase_id: 'p3', played_s: 1.25 }]);
    assert.deepEqual([second.voiced_s, final.voiced_s, final.final, final.overlap], [1, 1.375, true, true]);
    assert.equal(new Set(f.sent().map(item => item.turn_id)).size, 1);
    assert.equal(f.audio.calls.filter(call => call[0] === 'interrupt').length, 0);    // detection never stops playback
});

test('An ordinary utterance carries voiced_s only, keeps the ordinary cadence and is not ducked at its onset', async t => {
    const f = await storeRig(t);
    f.audio.onStart();
    assert.deepEqual(f.audio.calls, ['overlap?']);
    f.audio.onProbe('wave', { voiced_s: .75 }); await settle();
    f.audio.onProbe('wave-2'); await settle();                           // an observation without a measurement sends none
    const [probe, bare] = f.sent();
    assert.deepEqual(Object.keys(probe).filter(key => ['overlap', 'heard', 'voiced_s'].includes(key)), ['voiced_s']);
    assert.equal(probe.voiced_s, .75);
    assert.equal('voiced_s' in bare, false);
});

test('`continue` lets the reply play on and hands the release to the audio session; without a duck it simply releases', async t => {
    const f = await storeRig(t);
    f.audio.next = { overlap: true, heard: null };
    f.audio.onStart(); f.audio.onProbe('wave', { voiced_s: .5 }); await settle();
    const turn = f.sent()[0].turn_id;
    f.send({ type: 'candidate.state', turn_id: turn, sequence: 1, state: 'continue', final: false });
    assert.equal(f.audio.calls.at(-1), 'releaseAfterSpeech');
    assert.equal(f.store.policyHold, false);
    assert.equal(f.audio.calls.filter(call => call[0] === 'interrupt').length, 0);
    f.audio.ducked = false;                                              // nothing was ducked: an ordinary release instead
    f.send({ type: 'candidate.state', turn_id: turn, sequence: 1, state: 'continue', final: false });
    assert.deepEqual(f.audio.calls.slice(-2), ['releaseAfterSpeech', ['duck', false, false]]);
});

test('`listen` keeps the reply ducked while the user speaks; for a finished utterance it turns nothing down and stays open', async t => {
    const f = await storeRig(t);
    f.audio.next = { overlap: true, heard: null };
    f.audio.onStart(); f.audio.onProbe('wave', { voiced_s: .5 }); await settle();
    const probe = f.sent()[0];
    f.send({ type: 'candidate.state', turn_id: probe.turn_id, sequence: 1, state: 'listen', final: false });
    assert.deepEqual(f.ducks().at(-1), ['duck', true, false]);
    assert.equal(f.store.policyHold, true);
    f.audio.onTurn('final', { voiced_s: .9 }); await settle();
    const final = f.sent().at(-1);
    // Whether it goes on with the conversation is being read (Realtime._weigh): the reply plays on at full volume meanwhile.
    f.send({ type: 'candidate.state', turn_id: final.turn_id, sequence: final.sequence, state: 'listen', final: true });
    assert.deepEqual(f.ducks().at(-1), ['duck', false, false]);
    assert.equal(f.store.policyHold, false);
    // The utterance was not forgotten: its later admission still moves the conversation to the new epoch.
    const epoch = f.store.session.epoch;
    f.send({ type: 'candidate.admitted', turn_id: final.turn_id, previous_epoch: epoch, new_session: { ...f.store.session, epoch: epoch + 1 }, sequence: final.sequence });
    assert.equal(f.store.session.epoch, epoch + 1);
});

test('`listen` for a finished utterance does not release the duck of a newer one that is open', async t => {
    const f = await storeRig(t);
    f.audio.next = { overlap: true, heard: null };
    f.audio.onStart(); f.audio.onProbe('wave', { voiced_s: .5 }); await settle();
    f.audio.onTurn('final', { voiced_s: .9 }); await settle();
    const final = f.sent().at(-1);
    f.audio.onStart(); f.audio.onProbe('wave', { voiced_s: .5 }); await settle();
    const newer = f.sent().at(-1);
    f.send({ type: 'candidate.state', turn_id: newer.turn_id, sequence: 1, state: 'considering', final: false });
    const held = f.store.policyHold;
    f.send({ type: 'candidate.state', turn_id: final.turn_id, sequence: final.sequence, state: 'listen', final: true });
    assert.equal(f.store.policyHold, held);
    assert.equal(held, true);
});

test('`ignore` with a reason is remembered for the developer view as "Not answered", bounded', async t => {
    const f = await storeRig(t);
    f.audio.onStart(); f.audio.onProbe('wave', { voiced_s: .5 }); await settle();
    const turn = f.sent()[0].turn_id;
    f.send({ type: 'candidate.state', turn_id: turn, sequence: 1, state: 'ignore', final: false, reason: 'backchannel' });
    assert.deepEqual(plain(f.store.notAnswered.map(note => [note.turn, note.reason])), [[turn, 'backchannel']]);
    f.store.settings.developer_mode = true;
    const entry = f.store.visibleEvents.find(item => item.turn === turn);
    assert.deepEqual([f.store.eventLabel(entry), f.store.eventDetail(entry)], ['You · not answered', 'Not answered: an acknowledgement']);
    f.store.settings.developer_mode = false;
    assert.equal(f.store.visibleEvents.length, 0);                       // never shown outside developer mode
    f.send({ type: 'candidate.state', turn_id: turn, sequence: 1, state: 'ignore', final: false });
    assert.equal(f.store.notAnswered.length, 1);                         // no reason, nothing to say
});

test('An admission with fade_ms passes it to the stop; the duck is released at once as before', async t => {
    const f = await storeRig(t);
    f.audio.onStart(); f.audio.onProbe('wave', { voiced_s: .5 }); await settle();
    const turn = f.sent()[0].turn_id;
    f.store.draft = 'half a reply';
    f.send({ type: 'candidate.admitted', turn_id: turn, sequence: 1, previous_epoch: 0, new_session: { id: 's', epoch: 1 }, fade_ms: 100 });
    assert.deepEqual(f.audio.calls.slice(-2), [['interrupt', 100], ['duck', false, true]]);
    assert.deepEqual([f.store.session.epoch, f.store.draft], [1, '']);
});

test('The real session wires the measurement into every probe and turn, and checks for quiet after every capture message', async () => {
    const source = await readFile(new URL('../webui/audio.js', import.meta.url), 'utf8');
    assert.match(source, /onTurn: chunks => this\.onTurn\(this\.encoder\.encode\(chunks, true\), \{ voiced_s: round3\(this\.boundary\.lastTurn\?\.voiced \|\| 0\), pre_roll_s: round3\(this\.boundary\.lastTurn\?\.preRolled \|\| 0\) \}\)/);
    assert.match(source, /onProbe: chunks => this\.onProbe\(this\.encoder\.encode\(chunks, false\), \{ voiced_s: round3\(this\.boundary\.speech\), pre_roll_s: round3\(this\.boundary\.preRolled\) \}\)/);
    const handlers = source.split('this.capture.port.onmessage = event => {').slice(1).map(part => part.slice(0, part.indexOf('\n                };')));
    assert.equal(handlers.length, 2);                                                                // the guarded and the legacy capture path
    for (const handler of handlers) assert.match(handler, /this\.checkQuiet\(\);$/);
    const store = await readFile(new URL('../webui/convo-store.js', import.meta.url), 'utf8');
    assert.equal((store.match(/onProbe: \(value, meta\) => \{ if \(!this\.settings\.text_input_required\) void sendCandidate\(value, attempt, false, meta\); \}/g) || []).length, 2);
    assert.equal((store.match(/onTurn: \(value, meta\) => \{ if \(attempt === generation\) void this\.turn\(value, attempt, meta\); \}/g) || []).length, 2);
});
