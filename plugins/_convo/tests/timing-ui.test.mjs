import assert from 'node:assert/strict';
import { test } from 'node:test';
import { storeFixture } from './store-fixture.mjs';
import { SpeechBoundary } from '../webui/audio.js';
import { TIMING_TURNS, deriveTimings, markTurn, summarizeTimings } from '../webui/view-helpers.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 5));

// A controllable clock for the page: every mark in a scenario reads from `now`.
function clock(start = 1000) {
    const state = { now: start };
    return { state, performance: { now: () => state.now }, at(value) { state.now = value; } };
}

test('Marks are first-wins, bounded, and intervals exist only when both ends were observed', () => {
    const timings = new Map();
    markTurn(timings, 't', 'onset', 100); markTurn(timings, 't', 'onset', 999);           // the first mark of a kind wins
    markTurn(timings, '', 'onset', 1); markTurn(timings, 't', 'x', NaN); markTurn(timings, 't', 'y', 'soon');
    assert.deepEqual(Object.keys(timings.get('t')), ['onset']);
    assert.equal(timings.get('t').onset, 100);
    for (let n = 0; n < TIMING_TURNS + 10; n++) markTurn(timings, 'turn-' + n, 'onset', n);
    assert.equal(timings.size, TIMING_TURNS);
    assert.equal(timings.has('t'), false);                                                // the oldest turns go first
    assert.deepEqual(deriveTimings({ onset: 100, speech_end: 600 }), { utterance_ms: 500 });
    assert.deepEqual(deriveTimings({ speech_end: 600, final_sent: 400 }), {});            // an impossible order is not reported
    assert.deepEqual(deriveTimings(), {});
});

test('summaries use nearest-rank percentiles over the observed turns only', () => {
    const timings = new Map();
    for (let n = 1; n <= 10; n++) markTurn(timings, 't' + n, 'speech_end', 0), markTurn(timings, 't' + n, 'source_start', n * 100);
    markTurn(timings, 'unfinished', 'speech_end', 5);                                        // never became audible: not counted
    const summary = summarizeTimings(timings);
    assert.deepEqual(summary.speech_end_to_audible_ms, { n: 10, p50: 500, p90: 900, max: 1000 });
    assert.equal(Object.keys(summary).length, 1);
});

test('A spoken reply is timed from the end of speech to the first audible sound, with each stage separate', async () => {
    const c = clock();
    const f = await storeFixture({ performance: c.performance });
    await f.store.start();
    const audio = f.audios[0];
    audio.boundary = { onsetAt: 2000, lastTurn: { voicedAt: 2500 } };                      // what the real boundary reports
    c.at(2000); audio.onStart();
    c.at(2750); audio.onProbe('wave');
    c.at(3400); audio.onTurn('final-wave');                                               // 0.9 s of silence after the last voiced chunk
    await tick();
    const turn = f.requests.find(item => item.event === 'convo.candidate' && item.data.final).data.turn_id;
    c.at(4000); f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, type: 'audio.begin', turn_id: turn, phrase_id: 'p' } });
    c.at(4100); audio.started();                                                          // the first source actually started
    const report = f.store.timingReport();
    const record = report.turns.find(item => item.turn === turn);
    assert.equal(record.utterance_ms, 500);
    assert.equal(record.endpoint_ms, 900);
    assert.equal(record.final_to_audio_event_ms, 600);
    assert.equal(record.audio_event_to_source_ms, 100);
    assert.equal(record.speech_end_to_audible_ms, 1600);
    assert.equal(report.summary.speech_end_to_audible_ms.p50, 1600);
    assert.deepEqual(Object.keys(record.marks).sort(), ['audio_event', 'final_sent', 'first_probe_sent', 'onset', 'source_start', 'speech_end']);
    assert.doesNotMatch(JSON.stringify(report), /wave|final-wave/);                       // no audio or content, only identifiers and numbers
});

test('A stop request is timed from its probe to the revocation and to playback actually halting', async () => {
    const c = clock();
    const f = await storeFixture({ performance: c.performance });
    await f.store.start();
    const audio = f.audios[0];
    c.at(5000); audio.onStart();
    c.at(5750); audio.onProbe('stop-wave');
    await tick();
    const probe = f.requests.find(item => item.event === 'convo.candidate').data;
    c.at(6050);
    await f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, previous_epoch: 0, new_session: { id: 's', epoch: 1 }, turn_id: probe.turn_id, type: 'candidate.admitted', sequence: 1 } });
    assert.equal(audio.interrupted, 1);
    const record = f.store.timingReport().turns.find(item => item.turn === probe.turn_id);
    assert.equal(record.to_admit_ms, 300);                                                // probe sent -> server revoked the epoch
    assert.equal(record.admit_to_stop_ms, 0);                                             // revocation -> playback torn down (synchronous here)
    assert.ok(record.marks.admitted_recv <= record.marks.interrupt_done);
});

test('The browser boundary records when speech actually ended, on an injected clock', () => {
    let now = 0;
    const turns = [];
    const boundary = new SpeechBoundary({ rate: 16000, onStart() {}, onTurn: value => turns.push(value), clock: () => now });
    const voiced = new Float32Array(1600).fill(.1), quiet = new Float32Array(1600);
    for (let n = 0; n < 8; n++) { now = 1000 + n * 100; boundary.push(voiced); }
    for (let n = 0; n < 10; n++) { now = 1800 + 100 + n * 100; boundary.push(quiet); }
    assert.equal(turns.length, 1);
    assert.deepEqual(boundary.lastTurn.onsetAt, 1000);
    assert.equal(boundary.lastTurn.voicedAt, 1700);                                       // the last voiced chunk, not the end of the silence
    assert.ok(boundary.lastTurn.endedAt > boundary.lastTurn.voicedAt + 800);
    assert.equal(boundary.lastTurn.cut, false);
    assert.equal(boundary.onsetAt, 0);                                                    // reset for the next utterance
});
