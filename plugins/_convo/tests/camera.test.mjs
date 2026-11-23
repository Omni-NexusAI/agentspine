import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { storeFixture } from './store-fixture.mjs';

const tick = (ms = 5) => new Promise(resolve => setTimeout(resolve, ms));
const JPEG = 'data:image/jpeg;base64,/9j/4AAQ';

// A page with a camera: getUserMedia gives a stream whose one track can be stopped; the video reports its size; the canvas records
// what it was asked to encode.
function cameraPage({ width = 1920, height = 1080, refuse = false } = {}) {
    const page = { stopped: 0, asked: [], encoded: [] };
    const track = { stop() { page.stopped++; }, addEventListener() {} };
    const stream = { getTracks: () => [track], getVideoTracks: () => [track] };
    const document = { hidden: false, addEventListener() {}, getElementById: () => null, createElement(tag) {
        if (tag === 'video') return { videoWidth: width, videoHeight: height, set srcObject(value) { this.stream = value; }, play: async () => {} };
        if (tag === 'canvas') return { width: 0, height: 0, getContext: () => ({ drawImage() {} }),
            toDataURL(type, quality) { page.encoded.push({ type, quality, width: this.width, height: this.height }); return JPEG; } };
        return {};
    } };
    const navigator = { mediaDevices: { getUserMedia: async constraints => { page.asked.push(constraints); if (refuse) throw new Error('Permission denied'); return stream; } } };
    return { page, globals: { document, navigator } };
}

test('Snapshot size: the longest edge is at most 768 px and the shape is kept; a small frame is never enlarged', async () => {
    const f = await storeFixture();
    assert.deepEqual({ ...f.namespace.snapshotSize(1920, 1080) }, { width: 768, height: 432 });
    assert.deepEqual({ ...f.namespace.snapshotSize(480, 640) }, { width: 480, height: 640 });
    assert.deepEqual({ ...f.namespace.snapshotSize(1080, 1920) }, { width: 432, height: 768 });
    assert.equal(f.namespace.SNAPSHOT_EDGE, 768);
    assert.equal(f.namespace.SNAPSHOT_QUALITY, 0.7);
});

test('Share camera tells the session; a picture is taken only when the model asks, once, at 768 px and JPEG 0.7', async () => {
    const { page, globals } = cameraPage();
    const f = await storeFixture({ globals });
    await f.store.start();
    await f.store.shareCamera();
    assert.equal(f.store.cameraShared, true);
    assert.equal(page.asked.length, 1);
    assert.equal(page.asked[0].audio, false);                                      // the camera only: the microphone is Convo's own
    const shared = f.requests.filter(r => r.event === 'convo.camera');
    assert.deepEqual(shared.map(r => r.data.shared), [true]);
    assert.equal(page.encoded.length, 0);                                           // nothing is taken until the model looks
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, turn_id: 't', type: 'camera.request', request_id: 'r1', max_edge: 768, quality: 0.7 } });
    await tick();
    assert.deepEqual(page.encoded, [{ type: 'image/jpeg', quality: 0.7, width: 768, height: 432 }]);
    const sent = f.requests.filter(r => r.event === 'convo.camera_snapshot');
    assert.equal(sent.length, 1);
    assert.deepEqual({ request_id: sent[0].data.request_id, turn_id: sent[0].data.turn_id, image: sent[0].data.image }, { request_id: 'r1', turn_id: 't', image: JPEG });
});

test('Stop camera releases the device and tells the session; stopping the conversation releases it too', async () => {
    const { page, globals } = cameraPage();
    const f = await storeFixture({ globals });
    await f.store.start();
    await f.store.shareCamera();
    f.store.stopCamera();
    await tick();
    assert.equal(page.stopped, 1);
    assert.equal(f.store.cameraShared, false);
    assert.deepEqual(f.requests.filter(r => r.event === 'convo.camera').map(r => r.data.shared), [true, false]);
    await f.store.shareCamera();
    await f.store.stop();
    assert.equal(page.stopped, 2);
    assert.equal(f.store.cameraShared, false);
});

test('A refused camera is reported and nothing is shared', async () => {
    const { globals } = cameraPage({ refuse: true });
    const f = await storeFixture({ globals });
    await f.store.start();
    await f.store.shareCamera();
    assert.equal(f.store.cameraShared, false);
    assert.match(f.store.error, /Permission denied/);
    assert.equal(f.requests.filter(r => r.event === 'convo.camera').length, 0);
});

test('The panel has a Share camera button bound to the store', () => {
    const html = readFileSync(new URL('../webui/main.html', import.meta.url), 'utf8');
    assert.match(html, /@click="\$store\.convo\.toggleCamera\(\)"/);
    assert.match(html, /x-text="\$store\.convo\.cameraLabel"/);
});

test('A tool the model used reads as its name and words in the history; the request itself is bookkeeping', async () => {
    const { describeEvent, HIDDEN_KINDS } = await import('../webui/view-helpers.js');
    assert.deepEqual(describeEvent({ kind: 'tool.done', payload: { name: 'web_search', query: 'time in Paris', result: 'long text' } }),
                     { role: 'activity', label: 'Web search', text: 'time in Paris' });
    assert.equal(describeEvent({ kind: 'tool.failed', payload: { name: 'memory_read', query: 'my dog' } }).label, 'Memory search · failed');
    assert.equal(HIDDEN_KINDS.has('tool.requested'), true);
});
