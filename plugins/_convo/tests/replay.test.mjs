// Acoustic replay: known synthetic signals through the real SpeechBoundary and WAV path. Baseline tests pin what
// the browser does today (including its defects); `todo` tests state the behaviour a backend work package adds
// and are reported as todo until then. Synthetic signals only: nothing here says how real rooms or voices behave.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
    BLOCK, SERVER_RATE, concat, delayed, fadeIn, framesFor, gainDb, mix, noise, replay, rmsOf, silence, speechLike, wavFrames,
} from './signals.mjs';
import { RATES as MEASURED_RATES, duckToggles, falseStarts, overlapDelay, roomLeak } from './echo-measure.mjs';

const RATES = [16000, 44100, 48000];                      // what real capture devices report
const SERVER_BOUND = SERVER_RATE * 20;                     // plugin max_audio_seconds=20 -> frames the server accepts (test_behavior_corpus.AudioBound)
const block = rate => BLOCK / rate;                        // one worklet block, in seconds

test('Admission needs 450 ms of voiced audio, then probes follow every 750 ms; the turn ends 900 ms after the last voice', () => {
    for (const rate of RATES) {
        const r = replay(concat(silence(1, rate), speechLike(3, rate), silence(2, rate)), rate);
        assert.equal(r.starts.length, 1, rate);
        // whole blocks are counted, so the onset block may be only partly voiced: within one block either side of 450 ms
        assert.ok(r.starts[0] - 1 >= .45 - block(rate) && r.starts[0] - 1 <= .45 + 2 * block(rate), `admitted ${(r.starts[0] - 1).toFixed(3)} s after onset at ${rate}`);
        assert.ok(r.probes[0].seconds >= .75 && r.probes[0].seconds <= .75 + 2 * block(rate), `first probe at ${r.probes[0].seconds.toFixed(3)} s at ${rate}`);
        for (let n = 1; n < r.probes.length; n++) {
            assert.ok(r.probes[n].seconds > r.probes[n - 1].seconds);                     // cumulative, never a window
            assert.ok(r.probes[n].seconds - r.probes[n - 1].seconds >= .75 - 1e-9);
        }
        assert.equal(r.turns.length, 1, rate);
        const [turn] = r.turns, voicedEnd = r.boundary.lastTurn.voicedAt / 1000;
        assert.ok(turn.at - voicedEnd >= .9 && turn.at - voicedEnd <= .9 + 2 * block(rate), `endpoint ${(turn.at - voicedEnd).toFixed(3)} s after the last voiced block at ${rate}`);
        assert.ok(turn.seconds >= 3 && turn.seconds <= 3 + 1.2);                          // the utterance plus its trailing silence
    }
});

test('Frame counts match the WAV the browser really uploads', () => {
    const r = replay(speechLike(2, 48000), 48000);
    const chunks = [new Float32Array(48000).fill(.1)];
    assert.equal(wavFrames(chunks, 48000), framesFor(chunks, 48000));
    assert.equal(wavFrames(r.boundary.chunks.length ? r.boundary.chunks : chunks, 44100), framesFor(r.boundary.chunks.length ? r.boundary.chunks : chunks, 44100));
});

test('Baseline: a pause up to 900 ms stays one turn; a longer pause splits the request into two turns', () => {
    for (const rate of RATES) {
        const request = pause => replay(concat(speechLike(1.2, rate, { seed: 1 }), silence(pause, rate), speechLike(1.2, rate, { seed: 2 }), silence(1.5, rate)), rate);
        assert.equal(request(.8).turns.length, 1, `0.8 s pause at ${rate}`);
        assert.equal(request(1.1).turns.length, 2, `1.1 s pause at ${rate}`);              // BP5 merges these on the server; the boundary itself stays fixed
        assert.equal(request(1.1).starts.length, 2);
    }
});

test('BP3a: the maximum-length cut falls on the exact sample, so every uploaded turn fits inside the length the server accepts', () => {
    for (const rate of RATES) {
        const r = replay(speechLike(25, rate), rate);
        assert.equal(r.turns.length, 1, rate);                                              // the 5 s that follow are still being spoken, not a second turn yet
        const cut = r.turns[0];
        assert.equal(cut.last.cut, true);
        assert.equal(Math.round(cut.seconds * rate), rate * 20, `cut after exactly ${rate * 20} samples at ${rate}`);   // not a block more (it was up to 2048 samples more)
        assert.ok(cut.frames <= SERVER_BOUND, `${cut.frames} frames at ${rate} must fit in ${SERVER_BOUND}`);
        for (const probe of r.probes) assert.ok(probe.frames <= SERVER_BOUND, `probes stay inside the bound (${probe.frames})`);
    }
});

test('BP3a: the audio after the cut is not lost: it opens the next utterance', () => {
    for (const rate of RATES) {
        const signal = speechLike(25, rate), r = replay(signal, rate), pushed = Math.floor(signal.length / BLOCK) * BLOCK;
        const held = r.boundary.chunks.reduce((n, chunk) => n + chunk.length, 0);
        assert.equal(Math.round(r.turns[0].seconds * rate) + held, pushed, `every pushed sample is in the turn or the open utterance at ${rate}`);
        assert.ok(held > 0 && held < pushed / 3, `${held} samples carried at ${rate}`);
        assert.ok(r.boundary.chunks[0].length <= BLOCK);
        // the carried remainder is a real continuation: the next cut is another full 20 s further on
        const longer = replay(speechLike(45, rate), rate);
        assert.equal(longer.turns.length, 2, rate);
        assert.deepEqual(longer.turns.map(turn => turn.frames <= SERVER_BOUND), [true, true]);
        assert.equal(Math.round(longer.turns[1].seconds * rate), rate * 20);
    }
});

test('BP3a: a turn that ends on silence before the maximum is not a cut and keeps all of its audio', () => {
    for (const rate of RATES) {
        const r = replay(concat(speechLike(18, rate), silence(2, rate)), rate);
        assert.equal(r.turns.length, 1, rate);
        assert.equal(r.turns[0].last.cut, false);
        assert.ok(r.turns[0].seconds > 18 && r.turns[0].seconds < 20, `${r.turns[0].seconds} s at ${rate}`);
        assert.equal(r.boundary.chunks.length, 0, 'nothing is carried over');
    }
});

test('Baseline: the fixed 0.02 RMS threshold opens a candidate on loud steady noise and never hears quiet speech', () => {
    for (const rate of RATES) {
        assert.equal(replay(noise(30, rate, { rms: .01 }), rate).starts.length, 0, 'quiet room noise is ignored');
        assert.equal(replay(noise(3, rate, { rms: .05 }), rate).starts.length, 1, 'loud steady noise is a candidate; the classifier, not the boundary, decides');
        const quiet = replay(concat(silence(1, rate), speechLike(3, rate, { rms: .012 }), silence(2, rate)), rate);
        assert.equal(quiet.starts.length, 0, 'speech quieter than the threshold (distant microphone) is invisible');
        assert.equal(quiet.turns.length, 0);
    }
});

test('Baseline: there is no pre-roll, so a soft onset loses its first words', () => {
    for (const rate of RATES) {
        const onset = 1;                                                                   // seconds; the sound begins here
        const loud = replay(concat(silence(onset, rate), speechLike(3, rate), silence(2, rate)), rate);
        const soft = replay(concat(silence(onset, rate), fadeIn(speechLike(3, rate), .8, rate), silence(2, rate)), rate);
        const lost = replay => replay.turns[0].startsAt - onset;                           // >0: audio from this long after the sound began
        assert.ok(lost(loud) < block(rate) + 1e-9, `an abrupt onset is kept to within one block (${lost(loud).toFixed(3)} s)`);
        assert.ok(lost(soft) > .1, `a soft onset loses ${lost(soft).toFixed(3)} s at ${rate}`);
        assert.ok(loud.turns[0].startsAt >= onset - block(rate) - 1e-9);                   // never any audio from before the onset
    }
});

test('BP3 option: a pre-roll keeps the sound that came before the first loud block, so a soft onset is not cut off', () => {
    for (const rate of RATES) {
        const onset = 1, signal = concat(silence(onset, rate), fadeIn(speechLike(3, rate), .8, rate), silence(2, rate));
        const off = replay(signal, rate), kept = replay(signal, rate, { preRoll: .2 });
        const lostOff = off.turns[0].startsAt - onset, lostKept = kept.turns[0].startsAt - onset;
        assert.ok(lostOff - lostKept >= .2 - 1e-9 && lostOff - lostKept <= .2 + block(rate) + 1e-9, `${lostOff.toFixed(3)} s lost without, ${lostKept.toFixed(3)} s with, at ${rate}`);
        assert.ok(lostKept < .03 + block(rate), `a soft onset now starts within ${lostKept.toFixed(3)} s of the sound`);
        assert.deepEqual([kept.starts.length, off.starts.length], [1, 1]);
        assert.equal(kept.starts[0], off.starts[0], 'admission time is unchanged: the pre-roll adds audio, not earlier decisions');
        assert.ok(kept.turns[0].frames <= SERVER_BOUND);
    }
});

test('Baseline (BP9): assistant playback leaking into the microphone opens a candidate at -10 dB and not at -30 dB', () => {
    for (const rate of RATES) {
        const playback = speechLike(3, rate, { rms: .1, seed: 9 });                         // what the speaker plays
        const leak = level => mix(silence(5, rate), delayed(gainDb(playback, level), .12, rate));   // what a poor AEC lets through, 120 ms later
        assert.equal(replay(leak(-10), rate).starts.length, 1, `-10 dB leak at ${rate}`);
        assert.equal(replay(leak(-30), rate).starts.length, 0, `-30 dB leak at ${rate}`);
        assert.ok(rmsOf(gainDb(playback, -10)) > .02 && rmsOf(gainDb(playback, -30)) < .02);
    }
});

test('BP9: playback that is not the user never starts a candidate when the exact playback reference is known', () => {
    for (const rate of RATES) {
        const playback = speechLike(3, rate, { rms: .1, seed: 9 });
        const mic = mix(silence(5, rate), delayed(gainDb(playback, -10), .12, rate));
        const reference = concat(playback, silence(2, rate));
        assert.equal(replay(mic, rate).starts.length, 1, 'without the guard the -10 dB leak starts a candidate (the baseline above)');
        for (const guard of ['adaptive', 'strict']) {
            const r = replay(mic, rate, { guard, reference });
            assert.equal(r.starts.length, 0, `${guard} at ${rate}`);
            assert.equal(r.turns.length, 0); assert.equal(r.probes.length, 0);
        }
    }
});

test('BP9: false candidate starts at -10/-20/-30 dB leak, with and without the guard, with and without room noise', () => {
    for (const rate of MEASURED_RATES) for (const noiseRms of [0, .003]) for (const db of [-10, -20, -30]) {
        const r = falseStarts({ rate, db, noiseRms });
        assert.equal(r.legacy, db === -10 ? 1 : 0, `fixed threshold, no guard: ${db} dB at ${rate}`);
        assert.equal(r.adaptive, 0, `adaptive: ${db} dB, noise ${noiseRms}, ${rate}`);
        assert.equal(r.strict, 0, `strict: ${db} dB, noise ${noiseRms}, ${rate}`);
        if (!noiseRms && db < -10) assert.equal(r.floorOnly, 1, 'the measured floor alone would hear the quiet leak: the guard is what keeps it out');
    }
});

test('BP9: speech over the echo is detected about half a second after it begins, and the turn holds the speech but none of the echo', () => {
    for (const rate of MEASURED_RATES) for (const [db, userRms] of [[-10, .05], [-15, .05], [-15, .03], [-25, .02]]) {
        const r = overlapDelay({ rate, db, userRms });
        assert.equal(r.falseBefore, 0, `no false start before the user spoke (${db} dB, ${rate})`);
        assert.ok(r.first !== null && r.first >= .4 && r.first <= .75, `${db} dB / user ${userRms} at ${rate}: first start ${r.first}`);
        assert.equal(r.turns, 1);
        const base = overlapDelay({ rate, db, userRms, guard: null });
        if (db >= -15) assert.ok(base.falseBefore >= 1 && base.first === null, 'without the guard the echo opens the utterance first and the user is swallowed by it');
    }
    // What is uploaded: the user's speech, and silence where the echo was withheld; never the echo itself.
    const rate = 48000, lead = 1.5, at = 3.5, total = 9.5;
    const playback = speechLike(5, rate, { rms: .1, seed: 9 });
    const user = concat(silence(at, rate), speechLike(1.5, rate, { rms: .05, seed: 21, syllables: 3.1 }), silence(total - at - 1.5, rate));
    const mic = mix(user, roomLeak(playback, rate, { db: -10, total, start: lead }));
    const result = replay(mic, rate, { guard: 'adaptive', reference: concat(silence(lead, rate), playback, silence(total - lead - 5, rate)) });
    const [turn] = result.turns;
    assert.equal(result.turns.length, 1);
    assert.ok(turn.startsAt >= at - .1 && turn.startsAt <= at + .1, `the turn starts with the user's speech (${turn.startsAt.toFixed(3)} s)`);
    const samples = turn.chunks.reduce((all, chunk) => { all.push(...chunk); return all; }, []);
    const tail = samples.slice(-Math.round(.5 * rate));
    assert.equal(tail.every(value => value === 0), true, 'where the echo continued after the user stopped, the turn holds silence, not the echo');
});

test('BP9: a one-word interruption shorter than the 450 ms admission threshold is not detected (documented limit)', () => {
    const r = overlapDelay({ rate: 48000, db: -15, userRms: .05, seconds: .3 });
    assert.equal(r.first, null); assert.equal(r.falseBefore, 0);
    const longer = overlapDelay({ rate: 48000, db: -15, userRms: .05, seconds: .7 });
    assert.ok(longer.first !== null, 'a 0.7 s word is detected');
});

test('BP9: the guard is only as good as its reference: with no reference the leak is not recognised', () => {
    const rate = 48000, playback = speechLike(3, rate, { rms: .1, seed: 9 });
    const mic = mix(silence(5, rate), delayed(gainDb(playback, -10), .12, rate));
    assert.equal(replay(mic, rate, { guard: 'adaptive', reference: null }).starts.length, 1, 'no playback reference, no echo protection (the mic passes through)');
});

test('BP6: probes every 0.75 s through a 6 s reply make 2 gain changes, not 16, and the duck is one ramp', () => {
    const d = duckToggles();
    assert.equal(d.instantChanges, 16); assert.equal(d.rampedChanges, 2);
    assert.deepEqual(d.applied.map(entry => entry.ramp), [40, 250]);
});

test('Two utterances separated by silence are two admissions and two turns, each starting clean', () => {
    for (const rate of RATES) {
        const r = replay(concat(speechLike(1.5, rate, { seed: 4 }), silence(1.5, rate), speechLike(1.5, rate, { seed: 5 }), silence(1.5, rate)), rate);
        assert.deepEqual([r.starts.length, r.turns.length], [2, 2], rate);
        assert.ok(r.turns[1].startsAt >= r.turns[0].at - 1e-9);                            // the second turn contains none of the first
    }
});

test('The probe window never reaches the maximum, so a probe is always a valid length', () => {
    const rate = 48000, r = replay(speechLike(19.5, rate), rate, { maximum: 20 });
    assert.ok(r.probes.length > 20);
    assert.ok(Math.max(...r.probes.map(probe => probe.seconds)) < 20);
});
