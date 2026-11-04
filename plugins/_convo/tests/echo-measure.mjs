// Offline measurements for the echo guard and the stable duck. Synthetic signals through the real EchoGuard, SpeechBoundary and
// DuckController: they show how the code behaves on known signals, NOT how a real room, speaker or voice behaves, and they
// are not double-talk with a person. Physical acceptance is a separate listening test.
//   node plugins/_convo/tests/echo-measure.mjs
import { pathToFileURL } from 'node:url';
import { DUCK, DuckController } from '../webui/audio.js';
import { concat, mix, noise, replay, silence, speechLike, virtualTime } from './signals.mjs';

export const RATES = [16000, 44100, 48000];
export const LEAK_DB = [-10, -20, -30];

// The assistant's own voice as it reaches the microphone: delayed, attenuated, with two short reflections (a room), over room noise.
export function roomLeak(playback, rate, { db, delay = .12, noiseRms = 0, total, seed = 5, start = 0 }) {
    const length = Math.round(total * rate), out = new Float32Array(length);
    const add = (offset, gain) => { for (let i = 0; i < playback.length; i++) { const at = i + Math.round(offset * rate); if (at < length) out[at] += playback[i] * gain; } };
    const direct = 10 ** (db / 20);
    add(start + delay, direct); add(start + delay + .005, direct * .23); add(start + delay + .0125, direct * .11);
    return noiseRms ? mix(out, noise(total, rate, { rms: noiseRms, seed })) : out;
}

// The assistant starts speaking this long into the session / after the user's turn, so the guard has measured the room noise.
const LEAD = 1.5;

// False candidate starts while the assistant speaks and the user is silent, with the user's microphone otherwise quiet.
export function falseStarts({ rate, db, noiseRms = 0 }) {
    const playback = speechLike(3, rate, { rms: .1, seed: 9 }), total = LEAD + 5.5;
    const mic = roomLeak(playback, rate, { db, noiseRms, total, start: LEAD });
    const reference = concat(silence(LEAD, rate), playback, silence(total - LEAD - 3, rate));
    return {
        legacy: replay(mic, rate).starts.length,                                                      // fixed .02 threshold, no guard
        floorOnly: replay(mic, rate, { guard: 'off', reference }).starts.length,                      // the measured floor without the guard
        adaptive: replay(mic, rate, { guard: 'adaptive', reference }).starts.length,
        strict: replay(mic, rate, { guard: 'strict', reference }).starts.length,
    };
}

// The user speaks over the assistant: seconds from the user's onset to the candidate start (null = never), and how many starts.
export function overlapDelay({ rate, db, userRms, at = 2, seconds = 1.5, guard = 'adaptive' }) {
    const playback = speechLike(5, rate, { rms: .1, seed: 9 }), total = LEAD + 8, userAt = LEAD + at;
    const user = concat(silence(userAt, rate), speechLike(seconds, rate, { rms: userRms, seed: 21, syllables: 3.1 }), silence(total - userAt - seconds, rate));
    const mic = mix(user, roomLeak(playback, rate, { db, total, start: LEAD }));
    const reference = concat(silence(LEAD, rate), playback, silence(total - LEAD - 5, rate));
    const result = guard ? replay(mic, rate, { guard, reference }) : replay(mic, rate);
    const onsets = result.starts.map(start => start - userAt);
    return { first: onsets.find(onset => onset >= 0) ?? null, falseBefore: onsets.filter(onset => onset < 0).length, starts: result.starts.length, turns: result.turns.length };
}

// How often the output gain changes during a reply while the user is (apparently) speaking: a probe every .75 s, each one
// ducking the reply and ending 150 ms later in "no decision to interrupt".
export function duckToggles({ seconds = 6, probe = .75, decision = .15 } = {}) {
    const time = virtualTime(), applied = [];
    const duck = new DuckController({ apply: (target, ramp) => applied.push({ at: time.now(), target, ramp }), clock: time.now, schedule: time.schedule, cancel: time.cancel });
    let instant = 0, last = 1, duckedInstantMs = 0, since = 0;
    const step = held => { const next = held ? DUCK.level : 1; if (next !== last) { instant++; last = next; } };
    for (let t = 0; t < seconds * 1000; t += probe * 1000) {
        duck.request(true); step(true); time.advance(decision * 1000); duck.request(false); step(false); time.advance(probe * 1000 - decision * 1000);
    }
    duckedInstantMs = Math.round(seconds / probe) * decision * 1000;
    time.advance(DUCK.releaseDelayMs + 1);
    const duckedMs = applied.length >= 2 ? applied[1].at - applied[0].at : applied.length ? seconds * 1000 : 0;
    return { instantChanges: instant, rampedChanges: applied.length, duckedInstantMs, duckedMs, applied };
}

function main() {
    const pad = (value, width) => String(value).padEnd(width);
    console.log('False candidate starts while the assistant speaks (user silent), per leak level. legacy = fixed 0.02 threshold without the guard; floor-only = measured floor without the guard.');
    console.log(pad('rate', 7), pad('room noise', 11), pad('leak', 6), pad('legacy', 8), pad('floor-only', 11), pad('adaptive', 9), 'strict');
    for (const rate of RATES) for (const noiseRms of [0, .003]) for (const db of LEAK_DB) {
        const r = falseStarts({ rate, db, noiseRms });
        console.log(pad(rate, 7), pad(noiseRms || 'none', 11), pad(db + ' dB', 6), pad(r.legacy, 8), pad(r.floorOnly, 11), pad(r.adaptive, 9), r.strict);
    }
    console.log('\nUser speech over the assistant (leak, user rms): seconds from the user onset to the candidate start (negative-onset starts are echo false starts).');
    console.log(pad('rate', 7), pad('leak', 7), pad('user', 6), pad('guard: first', 14), pad('false before', 13), pad('no guard: first', 17), 'no guard: false before');
    for (const rate of RATES) for (const [db, userRms] of [[-10, .05], [-15, .05], [-15, .03], [-25, .02]]) {
        const g = overlapDelay({ rate, db, userRms }), n = overlapDelay({ rate, db, userRms, guard: null });
        console.log(pad(rate, 7), pad(db + ' dB', 7), pad(userRms, 6), pad(g.first?.toFixed(2) ?? 'never', 14), pad(g.falseBefore, 13), pad(n.first?.toFixed(2) ?? 'never*', 17), n.falseBefore);
    }
    const one = overlapDelay({ rate: 48000, db: -15, userRms: .05, seconds: .3 });
    console.log('\nOne short word (0.3 s) over the echo at 48 kHz: ' + (one.first === null ? 'not detected (below the 450 ms admission threshold)' : 'detected after ' + one.first.toFixed(2) + ' s'));
    const d = duckToggles();
    console.log('\nDuck during a 6 s reply, a probe every 0.75 s, each ducking then releasing 150 ms later: instant gain changes ' + d.instantChanges +
        ' (8 separate 150 ms ducks); with hysteresis ' + d.rampedChanges + ' (one ramped duck of ' + d.duckedMs + ' ms: the reply stays quiet for as long as the user keeps being heard).');
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
