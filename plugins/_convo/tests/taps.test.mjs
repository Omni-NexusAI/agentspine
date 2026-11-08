// Diagnostics taps on the real browser store: optional observers that can never throw into the voice path, and a playback
// snapshot of flags and counts with no audio in it.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { storeFixture } from './store-fixture.mjs';

const plain = value => JSON.parse(JSON.stringify(value));      // values built inside the VM compare by content, not by realm

test('Each echo-guard metric reaches the echo tap when one is set, and a missing or failing tap changes nothing', async () => {
    const f = await storeFixture();
    await f.store.start();
    const audio = f.audios.at(-1);
    assert.equal(typeof audio.onEchoMetric, 'function');
    const seen = [];
    audio.onEchoMetric({ kind: 'echo_metric', mode: 'adaptive', suppressing: true });               // no tap yet: nothing happens
    f.namespace.taps.echo = metric => seen.push(metric);
    audio.onEchoMetric({ kind: 'echo_metric', mode: 'adaptive', suppressing: false });
    assert.deepEqual(seen.map(metric => metric.suppressing), [false]);
    f.namespace.taps.echo = () => { throw new Error('panel bug'); };
    assert.doesNotThrow(() => audio.onEchoMetric({ kind: 'echo_metric' }));                          // a broken panel never breaks capture
    delete f.namespace.taps.echo;
    assert.doesNotThrow(() => audio.onEchoMetric({ kind: 'echo_metric' }));
    assert.equal(f.store.error, '');
});

test('A recreated session (retarget) is tapped the same way', async () => {
    const source = await readFile(new URL('../webui/convo-store.js', import.meta.url), 'utf8');
    const sessions = source.split('new AudioSession({').slice(1).map(part => part.slice(0, part.indexOf('});')));
    assert.equal(sessions.length, 2);
    for (const options of sessions) assert.match(options, /onEchoMetric: m => \{ try \{ taps\.echo\?\.\(m\) \} catch \{\} \}/);
});

test('The playback snapshot holds flags and counts only, and says when there is no session', async () => {
    const f = await storeFixture();
    assert.deepEqual(plain(f.namespace.audioSnapshot()), { active: false, ducked: false, toggles: 0, playing: false, queued: 0, pcm: 0 });
    await f.store.start();
    const audio = f.audios.at(-1);
    Object.assign(audio, { duck: { ducked: true, toggles: 3 }, playing: { buffer: 'audio bytes' }, queue: [{ base64: 'UklGRg==' }, { base64: 'UklGRg==' }],
                           pcm: new Map([['p1', { audio: 'AAAA' }]]) });
    const snapshot = f.namespace.audioSnapshot();
    assert.deepEqual(plain(snapshot), { active: true, ducked: true, toggles: 3, playing: true, queued: 2, pcm: 1 });
    assert.doesNotMatch(JSON.stringify(snapshot), /UklGRg|AAAA|audio bytes/);                      // never the audio itself
    await f.store.stop();
    assert.equal(f.namespace.audioSnapshot().active, false);
});
