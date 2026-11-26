// "Share screen" (convo-store.js + main.html), the camera's twin: the browser's own picker, the stream kept in the page, the session
// told only that one is shared, one frame only on the server's request, and sharing ended by the button, by the browser's own
// "Stop sharing" bar (the track's `ended`) and when voice stops.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { storeFixture } from './store-fixture.mjs';

const tick = (ms = 5) => new Promise(resolve => setTimeout(resolve, ms));
const JPEG = 'data:image/jpeg;base64,/9j/4AAQ';

// A page that can share a screen: getDisplayMedia gives a stream whose one track can be stopped and can end by itself; the video
// reports its size; the canvas records what it was asked to encode. `camera`: getUserMedia too, with its own track.
function screenPage({ width = 2560, height = 1440, refuse = null, picker = true } = {}) {
    const page = { stopped: 0, asked: [], encoded: [], listeners: {}, cameraStopped: 0, cameraAsked: 0 };
    const share = () => {                                                           // every share is a stream and a track of its own
        const track = { stop() { page.stopped++; }, addEventListener(name, fn) { page.listeners[name] = fn; } };
        return { getTracks: () => [track], getVideoTracks: () => [track] };
    };
    const cameraTrack = { stop() { page.cameraStopped++; }, addEventListener() {} };
    const cameraStream = { getTracks: () => [cameraTrack], getVideoTracks: () => [cameraTrack] };
    const document = { hidden: false, addEventListener() {}, getElementById: () => null, createElement(tag) {
        if (tag === 'video') return { videoWidth: width, videoHeight: height, set srcObject(value) { this.stream = value; }, play: async () => {} };
        if (tag === 'canvas') return { width: 0, height: 0, getContext: () => ({ drawImage() {} }),
            toDataURL(type, quality) { page.encoded.push({ type, quality, width: this.width, height: this.height }); return JPEG; } };
        return {};
    } };
    const mediaDevices = { getUserMedia: async () => { page.cameraAsked++; return cameraStream; } };
    if (picker) mediaDevices.getDisplayMedia = async constraints => { page.asked.push(constraints); if (refuse) throw refuse; return share(); };
    return { page, globals: { document, navigator: { mediaDevices } } };
}
const shared = f => f.requests.filter(r => r.event === 'convo.screen').map(r => r.data.shared);

test('Share screen tells the session; a picture is taken only when the model asks, once, with the camera\'s edge and quality', async () => {
    const { page, globals } = screenPage();
    const f = await storeFixture({ globals });
    await f.store.start();
    assert.equal(f.store.canShareScreen, true);
    await f.store.shareScreen();
    assert.equal(f.store.screenShared, true);
    assert.equal(page.asked.length, 1);
    assert.equal(page.asked[0].audio, false);                                       // the picture only: no tab or system sound
    assert.equal(page.cameraAsked, 0);                                              // the screen is not the camera
    assert.deepEqual(shared(f), [true]);
    const told = f.requests.find(r => r.event === 'convo.screen');
    assert.deepEqual({ session_id: told.data.session_id, epoch: told.data.epoch }, { session_id: 's', epoch: 0 });
    assert.equal(page.encoded.length, 0);                                           // nothing is taken until the model looks
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, turn_id: 't', type: 'screen.request', request_id: 'r1', max_edge: 768, quality: 0.7 } });
    await tick();
    assert.deepEqual(page.encoded, [{ type: 'image/jpeg', quality: 0.7, width: 768, height: 432 }]);
    const sent = f.requests.filter(r => r.event === 'convo.screen_snapshot');
    assert.equal(sent.length, 1);
    assert.deepEqual({ ...sent[0].data }, { session_id: 's', epoch: 0, turn_id: 't', request_id: 'r1', image: JPEG });
    assert.equal(f.requests.some(r => r.event === 'convo.camera_snapshot' || r.event === 'convo.camera'), false);
    await f.store.stop();
});

test('The request may lower the edge and the quality but never raise them; a request for another epoch takes nothing', async () => {
    const { page, globals } = screenPage();
    const f = await storeFixture({ globals });
    await f.store.start();
    await f.store.shareScreen();
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, turn_id: 't', type: 'screen.request', request_id: 'r1', max_edge: 512, quality: 0.5 } });
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, turn_id: 't', type: 'screen.request', request_id: 'r2', max_edge: 4096, quality: 1 } });
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 3, turn_id: 't', type: 'screen.request', request_id: 'r3', max_edge: 768, quality: 0.7 } });
    await tick();
    assert.deepEqual(page.encoded, [{ type: 'image/jpeg', quality: 0.5, width: 512, height: 288 }, { type: 'image/jpeg', quality: 0.7, width: 768, height: 432 }]);
    assert.deepEqual(f.requests.filter(r => r.event === 'convo.screen_snapshot').map(r => r.data.request_id), ['r1', 'r2']);
    await f.store.stop();
});

test('A request while nothing is shared, or when the frame cannot be taken, answers with an empty image', async () => {
    const { page, globals } = screenPage({ width: 0, height: 0 });
    const f = await storeFixture({ globals });
    await f.store.start();
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, turn_id: 't', type: 'screen.request', request_id: 'r1', max_edge: 768, quality: 0.7 } });
    await tick();
    await f.store.shareScreen();                                                    // shared, but the video has no size yet
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, turn_id: 't', type: 'screen.request', request_id: 'r2', max_edge: 768, quality: 0.7 } });
    await tick();
    assert.deepEqual(f.requests.filter(r => r.event === 'convo.screen_snapshot').map(r => [r.data.request_id, r.data.image]), [['r1', ''], ['r2', '']]);
    assert.equal(page.encoded.length, 0);
    await f.store.stop();
});

test('Stop screen releases the stream and tells the session; stopping the conversation releases it too', async () => {
    const { page, globals } = screenPage();
    const f = await storeFixture({ globals });
    await f.store.start();
    await f.store.toggleScreen();
    await f.store.toggleScreen();
    await tick();
    assert.equal(page.stopped, 1);
    assert.equal(f.store.screenShared, false);
    assert.deepEqual(shared(f), [true, false]);
    await f.store.shareScreen();
    await f.store.stop();
    assert.equal(page.stopped, 2);
    assert.equal(f.store.screenShared, false);
    assert.deepEqual(shared(f), [true, false, true]);                               // the conversation is ending: there is nobody left to tell
});

test('Ending the share from the browser\'s own "Stop sharing" bar stops it here and tells the session', async () => {
    const { page, globals } = screenPage();
    const f = await storeFixture({ globals });
    await f.store.start();
    await f.store.shareScreen();
    assert.equal(typeof page.listeners.ended, 'function');
    page.listeners.ended();
    await tick();
    assert.equal(f.store.screenShared, false);
    assert.equal(f.store.screenLabel, 'Share screen');
    assert.deepEqual(shared(f), [true, false]);
    assert.equal(page.stopped, 1);
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, turn_id: 't', type: 'screen.request', request_id: 'late', max_edge: 768, quality: 0.7 } });
    await tick();
    assert.equal(page.encoded.length, 0);                                           // nothing can be taken after it ended
    assert.equal(f.requests.find(r => r.event === 'convo.screen_snapshot').data.image, '');
    const earlier = page.listeners.ended;
    await f.store.shareScreen();                                                    // a new share is its own stream: the old track's end does not stop it
    earlier();
    await tick();
    assert.equal(f.store.screenShared, true);
    assert.deepEqual(shared(f), [true, false, true]);
    await f.store.stop();
});

test('The label follows the camera\'s pattern: shared, and whether the model takes images', async () => {
    const { globals } = screenPage();
    let offered = false;
    const f = await storeFixture({ globals, answer: event => (event === 'convo.screen' ? { offered } : null) });
    assert.equal(f.store.screenLabel, 'Share screen');
    await f.store.shareScreen();                                                    // before a conversation: kept here, nobody to tell yet
    assert.equal(f.store.screenLabel, 'Stop screen');
    assert.deepEqual(shared(f), []);
    await f.store.start();
    await tick();
    assert.deepEqual(shared(f), [true]);                                            // the conversation learns of it when it starts
    assert.equal(f.store.screenLabel, 'Stop screen · model does not take images');
    offered = true;
    await f.store.syncScreen();
    assert.equal(f.store.screenOffered, true);
    assert.equal(f.store.screenLabel, 'Stop screen');
    await f.store.stop();
    assert.equal(f.store.screenLabel, 'Share screen');
});

test('Closing the picker is a choice, not an error; another failure is reported; nothing is shared either way', async () => {
    const cancel = Object.assign(new Error('Permission denied'), { name: 'NotAllowedError' });
    const a = screenPage({ refuse: cancel });
    const f = await storeFixture({ globals: a.globals });
    await f.store.start();
    await f.store.shareScreen();
    assert.equal(f.store.screenShared, false);
    assert.equal(f.store.error, '');
    assert.match(f.store.note, /not started/);
    assert.deepEqual(shared(f), []);
    await f.store.stop();
    const b = screenPage({ refuse: Object.assign(new Error('Could not start video source'), { name: 'NotReadableError' }) });
    const g = await storeFixture({ globals: b.globals });
    await g.store.start();
    await g.store.shareScreen();
    assert.equal(g.store.screenShared, false);
    assert.match(g.store.error, /Could not start video source/);
    assert.deepEqual(shared(g), []);
    await g.store.stop();
});

test('Where the browser has no getDisplayMedia (phones) the button is hidden and a call shares nothing', async () => {
    const { page, globals } = screenPage({ picker: false });
    const f = await storeFixture({ globals });
    await f.store.start();
    assert.equal(f.store.canShareScreen, false);
    await f.store.shareScreen();
    assert.equal(f.store.screenShared, false);
    assert.match(f.store.error, /cannot share a screen/);
    assert.equal(page.cameraAsked, 0);
    await f.store.stop();
    const bare = await storeFixture();                                              // no navigator at all
    assert.equal(bare.store.canShareScreen, false);
});

test('The camera and the screen are separate shares: stopping one leaves the other', async () => {
    const { page, globals } = screenPage();
    const f = await storeFixture({ globals });
    await f.store.start();
    await f.store.shareCamera();
    await f.store.shareScreen();
    f.store.stopCamera();
    await tick();
    assert.deepEqual([f.store.cameraShared, f.store.screenShared, page.cameraStopped, page.stopped], [false, true, 1, 0]);
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, turn_id: 't', type: 'camera.request', request_id: 'c', max_edge: 768, quality: 0.7 } });
    f.handlers['convo.event']({ data: { session_id: 's', epoch: 0, turn_id: 't', type: 'screen.request', request_id: 's1', max_edge: 768, quality: 0.7 } });
    await tick();
    assert.equal(f.requests.find(r => r.event === 'convo.camera_snapshot').data.image, '');
    assert.equal(f.requests.find(r => r.event === 'convo.screen_snapshot').data.image, JPEG);
    await f.store.stop();
    assert.equal(page.stopped, 1);
});

test('The panel has a Share screen button next to Share camera, bound to the store and hidden where it cannot work', () => {
    const html = readFileSync(new URL('../webui/main.html', import.meta.url), 'utf8');
    const camera = html.indexOf('@click="$store.convo.toggleCamera()"'), screen = html.indexOf('@click="$store.convo.toggleScreen()"');
    assert.ok(camera > 0 && screen > camera && screen < html.indexOf('</div>', screen));
    assert.equal(html.slice(camera, screen).includes('</div>'), false);              // the same row of controls
    const button = html.slice(html.lastIndexOf('<button', screen), html.indexOf('</button>', screen));
    assert.match(button, /^<button type="button"/);
    assert.match(button, /x-show="\$store\.convo\.canShareScreen"/);
    assert.match(button, /:aria-pressed="\$store\.convo\.screenShared \? 'true' : 'false'"/);
    assert.match(button, /x-text="\$store\.convo\.screenLabel"/);
    assert.match(button, /class="material-symbols-outlined" aria-hidden="true"/);    // the icon is decoration: the label is the words
});
