// Audio & Video: the microphone list (Agent Zero's own setting), the camera to share and the size of the pictures the model gets.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { PICTURE_KEY, normalizePicture, readPicture, soundStoreDefinition, writePicture } from '../webui/sound-setup.js';

const read = name => readFileSync(new URL('../webui/' + name, import.meta.url), 'utf8');
function memory(initial = {}) {
    const items = new Map(Object.entries(initial));
    return { getItem: key => (items.has(key) ? items.get(key) : null), setItem: (key, value) => { items.set(key, String(value)); }, items };
}
function rig(convo = {}, storage = memory()) {
    const fake = { active: false, cameraShared: false, settings: {}, ...convo };
    return { store: soundStoreDefinition({ convo: () => fake, setup: () => null, storage }), convo: fake, storage };
}

test('Picture settings keep only known sizes and a camera name only with a camera', () => {
    assert.deepEqual(normalizePicture({}), { camera: '', camera_label: '', camera_edge: 768, screen_edge: 768 });
    assert.deepEqual(normalizePicture({ camera: 'cam-1', camera_label: 'Desk camera', camera_edge: 384, screen_edge: '512' }),
        { camera: 'cam-1', camera_label: 'Desk camera', camera_edge: 384, screen_edge: 512 });
    for (const bad of [0, 100, 4096, 'large', null, NaN]) assert.equal(normalizePicture({ camera_edge: bad }).camera_edge, 768);
    assert.equal(normalizePicture({ camera: '', camera_label: 'Desk camera' }).camera_label, '');
    assert.equal(normalizePicture({ camera: 'c', camera_label: 'x'.repeat(200) }).camera_label.length, 80);
    assert.deepEqual(normalizePicture('nonsense'), normalizePicture({}));
});

test('Picture settings are kept per browser and survive blocked or broken storage', () => {
    const storage = memory();
    assert.equal(writePicture({ camera: 'cam-1', camera_label: 'Desk camera', screen_edge: 384 }, storage), true);
    assert.deepEqual(readPicture(storage), { camera: 'cam-1', camera_label: 'Desk camera', camera_edge: 768, screen_edge: 384 });
    assert.deepEqual(readPicture(memory({ [PICTURE_KEY]: '{not json' })), normalizePicture({}));
    const blocked = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
    assert.deepEqual(readPicture(blocked), normalizePicture({}));
    assert.equal(writePicture({ camera: 'cam-1' }, blocked), false);
});

test('Choosing a camera keeps its name, and a camera that is gone is shown by that name', async () => {
    const f = rig({ cameras: async () => [{ id: 'cam-1', label: 'Desk camera' }, { id: 'cam-2', label: '' }] });
    assert.equal(f.store.savedCameraLabel(), 'Saved camera');
    await f.store.listCameras();
    f.store.setPicture('camera', 'cam-1');
    assert.deepEqual([f.store.picture.camera, f.store.picture.camera_label], ['cam-1', 'Desk camera']);
    assert.equal(readPicture(f.storage).camera, 'cam-1');
    f.store.setPicture('camera_edge', 384); f.store.setPicture('camera_label', 'forged'); f.store.setPicture('unknown', 1);
    assert.deepEqual(f.store.picture, { camera: 'cam-1', camera_label: 'Desk camera', camera_edge: 384, screen_edge: 768 });
    f.convo.cameras = async () => [];                                                // unplugged
    await f.store.listCameras();
    assert.equal(f.store.savedCameraLabel(), 'Desk camera (not connected)');
    f.convo.cameraShared = true;
    f.store.setPicture('camera', '');
    assert.equal(f.store.message, 'The camera changes the next time you share it.');
    assert.equal(f.store.picture.camera_label, '');
});

test('The microphone list is Agent Zero’s own, and a refused choice leaves the selection alone', async () => {
    const chosen = [];
    const f = rig({ microphones: async () => ({ chosen: 'mic-1', list: [{ id: 'mic-1', label: 'Headset' }, { id: 'mic-2', label: 'Webcam' }] }),
                    chooseMicrophone: async id => { chosen.push(id); return id === 'mic-2' ? { ok: true, message: 'Used the next time you start a conversation.' } : { ok: false, message: 'That microphone is not available any more.' }; } });
    await f.store.listMicrophones();
    assert.deepEqual([f.store.microphone, f.store.microphones.length], ['mic-1', 2]);
    await f.store.chooseMicrophone('mic-9');
    assert.deepEqual([f.store.microphone, f.store.microphoneNote], ['mic-1', 'That microphone is not available any more.']);
    await f.store.chooseMicrophone('mic-2');
    assert.deepEqual([f.store.microphone, f.store.microphoneNote], ['mic-2', 'Used the next time you start a conversation.']);
    assert.deepEqual(chosen, ['mic-9', 'mic-2']);
    const bare = rig();                                                              // a page whose Convo store has no such methods
    assert.deepEqual(await bare.store.listMicrophones(), []);
    assert.equal((await bare.store.chooseMicrophone('x')).ok, false);
});

test('The panel reads the lists when it shows, and the page uses the chosen camera and sizes', () => {
    const panel = read('sound-panel.html'), store = read('convo-store.js');
    for (const list of ['listMicrophones', 'listCameras']) assert.ok(panel.includes('x-init="$store.convoSound.' + list + '()"'), list);
    assert.match(panel, /@change="\$store\.convoSound\.chooseMicrophone\(\$event\.target\.value\)"/);
    for (const name of ['camera_edge', 'screen_edge']) assert.ok(panel.includes("setPicture('" + name + "', Number($event.target.value))"), name);
    assert.match(store, /videoFrame\(cameraVideo, pictureEdge\(event\.max_edge, readPicture\(\)\.camera_edge\), event\.quality\)/);
    assert.match(store, /videoFrame\(screenVideo, pictureEdge\(event\.max_edge, readPicture\(\)\.screen_edge\), event\.quality\)/);
    assert.match(store, /deviceId: \{ exact: chosen \}/);
    assert.match(store, /error\?\.name === 'OverconstrainedError' \|\| error\?\.name === 'NotFoundError'/);   // a camera that is gone: the default
    // The microphone is the host's setting: Convo keeps none of its own.
    assert.match(store, /await mic\.selectDevice\(id\)/);
    assert.doesNotMatch(read('sound-setup.js'), /microphone['"]?\s*:\s*typeof source/);
});
