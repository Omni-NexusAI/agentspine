// Sound settings per device (the sound drawer, sound-panel.html): noise gate, interrupt after, end of turn and echo protection.
//
// The saved server settings are only the defaults. What a device uses is kept in this browser under
// `convo.sound.v1:<inputDeviceId>|<outputDeviceId>` = {noise_gate_db, barge_in_seconds, end_of_turn_seconds, echo_guard}: a phone, a
// laptop and a headset each keep their own. Every storage call is wrapped (blocked storage, a private window): without it the
// defaults apply and changes last for the page. "Use as default" patches the server settings through setup_patch.
// Pure helpers first (no imports, node-testable), then the Alpine store `convoSound` built by createSoundStore.

export const SOUND_PREFIX = 'convo.sound.v1:';
export const SOUND_KEYS = ['noise_gate_db', 'barge_in_seconds', 'end_of_turn_seconds', 'echo_guard'];
export const SOUND_DEFAULTS = Object.freeze({ noise_gate_db: -50, barge_in_seconds: 1.0, end_of_turn_seconds: 0.9, echo_guard: 'adaptive' });
export const GATE_AXIS = Object.freeze([-66, -3]);              // dBFS; the leftmost position is "off" (the fork's axis)
export const BARGE_RANGE = Object.freeze([0.4, 2.0]);
export const END_RANGE = Object.freeze([0.5, 2.0]);
export const ECHO_MODES = ['adaptive', 'strict', 'off'];
export const MEASURE_SECONDS = 3;
export const MEASURE_MARGIN_DB = 8;
export const GATE_HINT = 'Raise the handle until room noise stops lighting the bar';
export const MEASURE_PLAYING = 'Convo is speaking: Measure room waits until it is quiet, so its voice is not measured as the room.';

const clamp = (value, [low, high], fallback) => { const n = Number(value); return Number.isFinite(n) ? Math.max(low, Math.min(high, n)) : fallback; };
const round = (value, step) => Math.round(value / step) / Math.round(1 / step);
export function normalizeSound(values = {}, defaults = SOUND_DEFAULTS) {
    const base = { ...SOUND_DEFAULTS, ...pickSound(defaults) };
    const source = { ...base, ...pickSound(values) };
    return {
        noise_gate_db: Math.round(clamp(source.noise_gate_db, GATE_AXIS, base.noise_gate_db)),
        barge_in_seconds: round(clamp(source.barge_in_seconds, BARGE_RANGE, base.barge_in_seconds), .1),
        end_of_turn_seconds: round(clamp(source.end_of_turn_seconds, END_RANGE, base.end_of_turn_seconds), .1),
        echo_guard: ECHO_MODES.includes(source.echo_guard) ? source.echo_guard : (ECHO_MODES.includes(base.echo_guard) ? base.echo_guard : 'adaptive'),
    };
}
export function pickSound(values) {
    const out = {};
    for (const key of SOUND_KEYS) if (values && values[key] !== undefined && values[key] !== null && values[key] !== '') out[key] = values[key];
    return out;
}
// The saved server settings as this device's defaults.
export const soundDefaults = settings => normalizeSound(pickSound(settings || {}));
// `mode` ('media' | 'call', phones only): each phone audio mode keeps its own calibration (noise gate, interrupt, end of turn, echo).
export const soundKey = (input, output, mode = '') =>
    `${SOUND_PREFIX}${String(input || 'default').slice(0, 200)}|${String(output || 'default').slice(0, 200)}${mode === 'media' || mode === 'call' ? '#' + mode : ''}`;
const storageOf = storage => { try { return storage === undefined ? globalThis.localStorage : storage; } catch { return null; } };
// The key every page used before the microphone in use was known (`default|default`). Its values move once to the first real
// device key a conversation reads (copied, never removed, so an older Convo still finds them); the marker says it happened.
export const LEGACY_KEY = soundKey('', '');
export const MIGRATED_MARK = 'convo.sound.v1.migrated';
export function migrateLegacy(key, storage) {
    try {
        const store = storageOf(storage);
        if (!store || !key || key === LEGACY_KEY || key.startsWith(SOUND_PREFIX + 'default|')) return false;
        if (store.getItem(MIGRATED_MARK) || store.getItem(key) !== null) return false;
        const legacy = readDevice(LEGACY_KEY, store);
        if (!Object.keys(legacy).length) return false;
        store.setItem(key, JSON.stringify(legacy));
        store.setItem(MIGRATED_MARK, key);
        return true;
    } catch { return false; }
}

// Which stored values a key reads: its own, or (before the one-time move, while it has none) the old `default|default` ones, so the
// values in use do not change before a conversation has named its real microphone.
export function soundSource(key, storage) {
    try {
        const store = storageOf(storage);
        if (!store || key === LEGACY_KEY || store.getItem(key) !== null || store.getItem(MIGRATED_MARK)) return key;
        return store.getItem(LEGACY_KEY) !== null ? LEGACY_KEY : key;
    } catch { return key; }
}

// Choices for one microphone, kept beside its sound values: the output device to play to ('' = system default; remembered for this
// microphone, so the per-device sound key is the microphone and that output), the browser's noise suppression and auto gain (Call
// mode and desktops; Media mode turns them off), and the phone audio mode ('auto' | 'media' | 'call').
export const DEVICE_PREFIX = 'convo.device.v1:';
export const DEVICE_DEFAULTS = Object.freeze({ output: '', output_label: '', noise_suppression: true, auto_gain: true, phone_mode: 'auto' });
export const PHONE_MODES = ['auto', 'media', 'call'];
export const deviceKey = input => `${DEVICE_PREFIX}${String(input || 'default').slice(0, 200)}`;
export function normalizeChoices(values = {}) {
    const source = values && typeof values === 'object' && !Array.isArray(values) ? values : {};
    return {
        output: typeof source.output === 'string' ? source.output.slice(0, 200) : '',
        // The chosen output's name as the browser gave it, so the choice can be shown before (or without) the device list.
        output_label: typeof source.output === 'string' && source.output && typeof source.output_label === 'string' ? source.output_label.slice(0, 80) : '',
        noise_suppression: source.noise_suppression !== false,
        auto_gain: source.auto_gain !== false,
        phone_mode: PHONE_MODES.includes(source.phone_mode) ? source.phone_mode : 'auto',
    };
}
export function readChoices(input, storage) {
    try { const raw = storageOf(storage)?.getItem(deviceKey(input)); return normalizeChoices(raw ? JSON.parse(raw) : {}); }
    catch { return normalizeChoices({}); }
}
export function writeChoices(input, values, storage) {
    try { storageOf(storage)?.setItem(deviceKey(input), JSON.stringify(normalizeChoices(values))); return true; } catch { return false; }
}
// This device's own values (only the keys it changed), or {} when none or storage is blocked.
export function readDevice(key, storage) {
    try {
        const raw = storageOf(storage)?.getItem(key);
        const parsed = raw ? JSON.parse(raw) : null;
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? pickSound(parsed) : {};
    } catch { return {}; }
}
export function writeDevice(key, values, storage) {
    try { storageOf(storage)?.setItem(key, JSON.stringify(pickSound(values))); return true; } catch { return false; }
}
export function forgetDevice(key, storage) { try { storageOf(storage)?.removeItem(key); return true; } catch { return false; } }
// What a device uses: its own values over the saved defaults.
export const effectiveSound = (settings, key, storage) => normalizeSound(readDevice(key, storage), soundDefaults(settings));

// ---- phone audio mode --------------------------------------------------------------------------------------------------------
// An Android phone plays a page in call (communication) mode while its microphone runs with the browser's echo cancelling, and in
// media mode without it. Convo fixes the mode when a conversation starts (switching means reopening the microphone, which flips the
// route itself): with a headset Media (the louder media volume, the browser's echo cancelling, noise suppression and auto gain off);
// on the loudspeaker Call (today's behaviour, which keeps interruption by voice working). An override per device wins. Which labels
// mean a headset is a guess to be confirmed by the probe on a real phone; the decision carries what it saw (`labels`, `reason`).
const HEADSET = /head ?set|head ?phone|earbud|ear ?phone|airpods|\bbuds\b|bluetooth|\bwired\b|\busb\b|\bBT\b/i;
const BUILT_IN = /^(default|communications|speaker ?phone|speaker|earpiece|built-?in.*|phone|handset)$/i;
export function isPhone(nav = globalThis.navigator) {
    try { return !!(nav?.userAgentData?.mobile) || /Android/i.test(String(nav?.userAgent || '')); } catch { return false; }
}
export function phoneAudioMode({ phone = false, devices = [], override = 'auto' } = {}) {
    if (!phone) return { mode: '', source: 'desktop', reason: 'not a phone', labels: [] };
    const labels = devices.map(device => String(device?.label || '').slice(0, 60)).filter(Boolean).slice(0, 12);
    if (override === 'media' || override === 'call') return { mode: override, source: 'override', reason: 'chosen for this device', labels };
    const headset = labels.find(label => HEADSET.test(label) && !BUILT_IN.test(label.trim()));
    return headset ? { mode: 'media', source: 'auto', reason: 'headset: ' + headset, labels } : { mode: 'call', source: 'auto', reason: labels.length ? 'no headset named' : 'no device names (permission not granted yet)', labels };
}
// The microphone's own processing for a mode: Media turns the browser's processing off; otherwise echo cancelling stays on (Convo's
// interruption by voice needs it on a loudspeaker) and noise suppression and auto gain are this device's choices (default on).
export function captureProcessing(mode, { noise_suppression = true, auto_gain = true } = {}) {
    if (mode === 'media') return { echoCancellation: false, noiseSuppression: false, autoGainControl: false };
    return { echoCancellation: true, noiseSuppression: noise_suppression !== false, autoGainControl: auto_gain !== false };
}

// Meter geometry on the gate's dB axis: 0..1 across GATE_AXIS (the handle and the bar share it).
export const dbOfLevel = rms => (rms > 1e-10 ? 20 * Math.log10(rms) : -120);
export const axisPosition = db => Math.max(0, Math.min(1, (db - GATE_AXIS[0]) / (GATE_AXIS[1] - GATE_AXIS[0])));
export const gateLabel = db => (db <= GATE_AXIS[0] ? 'Off' : `${Math.round(db)} dB`);
// "Measure room": the quiet floor of the levels collected (their 20th percentile in dB) + MEASURE_MARGIN_DB, on the axis. null when
// too little was heard to say.
export function measuredThreshold(levels) {
    const values = (levels || []).filter(Number.isFinite).map(dbOfLevel).sort((a, b) => a - b);
    if (values.length < 10) return null;
    const floor = values[Math.floor((values.length - 1) * .2)];
    return Math.round(clamp(floor + MEASURE_MARGIN_DB, [GATE_AXIS[0] + 1, GATE_AXIS[1]], SOUND_DEFAULTS.noise_gate_db));
}

// ---- the store --------------------------------------------------------------------------------------------------------------
// What this browser shows the model, kept per browser (not per microphone): the camera to share ('' = the browser's default; its
// name is kept so the choice can be shown before the list is read) and the longest edge of a picture taken from the camera and
// from a shared screen. A smaller picture is fewer tokens and a faster look; the server's own limit still applies.
export const PICTURE_KEY = 'convo.picture.v1';
export const PICTURE_EDGES = [384, 512, 768];
export const PICTURE_DEFAULTS = Object.freeze({ camera: '', camera_label: '', camera_edge: 768, screen_edge: 768 });
export function normalizePicture(values = {}) {
    const source = values && typeof values === 'object' && !Array.isArray(values) ? values : {};
    const edge = value => (PICTURE_EDGES.includes(Number(value)) ? Number(value) : 768);
    const camera = typeof source.camera === 'string' ? source.camera.slice(0, 200) : '';
    return { camera, camera_label: camera && typeof source.camera_label === 'string' ? source.camera_label.slice(0, 80) : '',
             camera_edge: edge(source.camera_edge), screen_edge: edge(source.screen_edge) };
}
export function readPicture(storage) {
    try { const raw = storageOf(storage)?.getItem(PICTURE_KEY); return normalizePicture(raw ? JSON.parse(raw) : {}); }
    catch { return normalizePicture({}); }
}
export function writePicture(values, storage) {
    try { storageOf(storage)?.setItem(PICTURE_KEY, JSON.stringify(normalizePicture(values))); return true; } catch { return false; }
}

// createStore and the stores it talks to are passed in (sound-panel.html imports the host's AlpineStore), so this file has no
// host imports and node tests build it directly.
export function soundStoreDefinition({ convo, setup, storage, schedule = (fn, ms) => setTimeout(fn, ms), cancel = id => clearTimeout(id) }) {
    let measureTimer = null;
    return {
        open: false, values: { ...SOUND_DEFAULTS }, key: soundKey('', ''), own: {}, message: '', error: '', measuring: false, levels: [],
        choices: normalizeChoices({}), outputs: [], outputNote: '', testNote: '', recordNote: '', recordings: [], recording: false,
        picture: readPicture(storage), cameras: [], camerasListed: false, microphones: [], microphone: '', microphoneNote: '',
        get convo() { return convo(); },
        get setup() { return setup(); },
        // Re-read this device's values (the panel opening, the device changing, a conversation starting with its real microphone).
        refresh() {
            const store = this.convo;
            this.key = store?.soundKey?.() || soundKey('', '');
            if (store?.active) migrateLegacy(this.key, storage);      // only once the conversation's real microphone is known
            this.own = readDevice(soundSource(this.key, storage), storage);
            this.values = normalizeSound(this.own, soundDefaults(store?.settings));
            this.choices = readChoices(store?.soundInput?.() || '', storage);
            return this.values;
        },
        // ---- this microphone's choices: output device, noise suppression, auto gain, phone audio mode
        setChoice(name, value) {
            if (!(name in DEVICE_DEFAULTS)) return this.choices;
            const input = this.convo?.soundInput?.() || '';
            const named = name === 'output' ? { output_label: this.outputs.find(device => device.id === value)?.label || '' } : {};
            this.choices = normalizeChoices({ ...this.choices, [name]: value, ...named });
            this.error = '';
            if (!writeChoices(input, this.choices, storage)) this.message = 'This browser is not keeping settings, so this change lasts until the page is reloaded.';
            else this.message = name !== 'output' && this.convo?.active ? 'This applies the next time you start a conversation (the microphone is opened with it).' : '';
            this.convo?.deviceChoices?.(this.choices);
            // Another output is another input/output pair: its own sound values apply to the running capture at once.
            if (name === 'output') { void this.chooseOutput(this.choices.output); this.refresh(); this.convo?.applySound?.(this.values, 'all'); }
            return this.choices;
        },
        async chooseOutput(id) {
            const result = await this.convo?.chooseOutput?.(id);
            this.outputNote = result?.message || '';
            return result;
        },
        // The real output names (the browser gives names once the microphone was allowed); hidden where a browser cannot choose.
        async listOutputs() { this.outputs = (await this.convo?.listOutputs?.()) || []; this.outputsListed = true; return this.outputs; },
        outputsListed: false,
        // The saved output when the list does not hold it: by its name, and "not connected" only once the list was really read.
        savedOutputLabel() {
            const name = this.choices.output_label || 'Saved output';
            return this.outputsListed ? name + ' (not connected)' : name;
        },
        get canChooseOutput() { return !!this.convo?.outputView?.().can_choose; },
        // ---- the microphone: Agent Zero's own speech setting (one microphone for dictation and for Convo), read and changed here
        async listMicrophones() {
            const found = (await this.convo?.microphones?.()) || { chosen: '', list: [] };
            this.microphones = found.list || []; this.microphone = found.chosen || '';
            return this.microphones;
        },
        async chooseMicrophone(id) {
            const result = (await this.convo?.chooseMicrophone?.(id)) || { ok: false, message: 'The microphone could not be changed here.' };
            this.microphoneNote = result.message || '';
            if (result.ok) this.microphone = id;
            return result;
        },
        // ---- camera and screen: which camera, and how large a picture the model gets
        async listCameras() { this.cameras = (await this.convo?.cameras?.()) || []; this.camerasListed = true; return this.cameras; },
        savedCameraLabel() {
            const name = this.picture.camera_label || 'Saved camera';
            return this.camerasListed ? name + ' (not connected)' : name;
        },
        setPicture(name, value) {
            if (!(name in PICTURE_DEFAULTS) || name === 'camera_label') return this.picture;
            const named = name === 'camera' ? { camera_label: this.cameras.find(device => device.id === value)?.label || '' } : {};
            this.picture = normalizePicture({ ...this.picture, [name]: value, ...named });
            if (!writePicture(this.picture, storage)) this.message = 'This browser is not keeping settings, so this change lasts until the page is reloaded.';
            else this.message = name === 'camera' && this.convo?.cameraShared ? 'The camera changes the next time you share it.' : '';
            return this.picture;
        },
        output() { return this.convo?.outputView?.() || { label: 'System default', state: '', can_choose: false, missing: false, phone: false, mode: '' }; },
        async testSound() {
            const result = await this.convo?.playTestSound?.();
            this.testNote = result?.ok ? `Test sound played to ${this.output().label}.` : (result?.message || 'The test sound could not be played.');
            return result;
        },
        // ---- developer: Record raw and sent (only on press; saved in Convo's data folder on the Agent Zero host, listed and deletable)
        async record() {
            this.recordNote = ''; this.recording = true;
            try { this.recordNote = (await this.convo?.recordRawAndSent?.()) || ''; }
            finally { this.recording = false; }
            void this.listRecordings();
        },
        async listRecordings() { this.recordings = (await this.convo?.recordingList?.()) || []; return this.recordings; },
        async deleteRecording(id) { await this.convo?.recordingDelete?.(id); return this.listRecordings(); },
        // Opening the drawer reads the device lists (a saved device then shows by its name, not as "the chosen device").
        show() {
            this.open = true; this.message = ''; this.error = ''; this.refresh(); void this.setup?.load?.();
            for (const list of ['listOutputs', 'listMicrophones', 'listCameras']) { try { void this[list]?.()?.catch?.(() => {}); } catch { /* a list is optional */ } }
        },   // the voice row needs the settings
        hide() { this.open = false; this.stopMeasure(); },
        toggle() { if (this.open) this.hide(); else this.show(); },
        overridden(key) { return key in this.own; },
        // A change on this device: saved for this device only and applied to the running capture at once.
        set(key, value) {
            if (!SOUND_KEYS.includes(key)) return this.values;
            const next = normalizeSound({ ...this.values, [key]: value }, soundDefaults(this.convo?.settings));
            this.own = { ...this.own, [key]: next[key] };
            this.values = next;
            if (!writeDevice(this.key, this.own, storage)) this.message = 'This browser is not keeping settings, so this change lasts until the page is reloaded.';
            else this.message = '';
            const note = this.convo?.applySound?.(next, key);
            if (note) this.message = note;
            return next;
        },
        // Back to the saved defaults on this device.
        reset() {
            forgetDevice(this.key, storage); this.own = {};
            this.values = normalizeSound({}, soundDefaults(this.convo?.settings));
            this.convo?.applySound?.(this.values, 'all');
            this.message = 'This device uses the saved defaults again.';
        },
        // This device's values become the saved defaults for every device that has not changed its own.
        async useAsDefault() {
            this.error = ''; this.message = '';
            const setupStore = this.setup;
            if (!setupStore?.patch) { this.error = 'Convo settings are not loaded yet; open Settings once and try again.'; return false; }
            if (!setupStore.config) await setupStore.load?.();
            const result = await setupStore.patch(Object.fromEntries(SOUND_KEYS.map(key => [key, this.values[key]])));
            if (!result) { this.error = setupStore.error || 'The defaults could not be saved.'; return false; }
            if ((result.conflicts || []).length) { this.message = 'Changed elsewhere meanwhile: choose which to keep in Settings.'; return false; }
            this.message = 'Saved as the default for devices without their own sound settings.';
            void this.convo?.refresh?.();
            return true;
        },
        // Meter view: the raw microphone level and the handle on one axis.
        get canMeasure() { return !!this.convo?.active && !this.convo.micMuted && !this.measuring && !this.assistantPlaying; },
        // The assistant is speaking (or its echo tail): its voice would be measured as the room, so Measure room waits.
        get assistantPlaying() { return this.convo?.state === 'speaking' || !!this.convo?.meter?.playback; },
        meter() {
            const reading = this.convo?.meter || {};
            const db = dbOfLevel(Number(reading.level) || 0);
            return { db, position: axisPosition(db), handle: axisPosition(this.values.noise_gate_db), withheld: !!reading.withheld, voiced: !!reading.voiced,
                     state: String(reading.state || ''), live: !!this.convo?.active, text: this.convo?.active ? `${Math.max(-99, Math.round(db))} dB` : 'Microphone off' };
        },
        // Three seconds of quiet: the gate goes to the measured floor + 8 dB (clamped). Only while a session's microphone is live.
        measureRoom(seconds = MEASURE_SECONDS) {
            if (this.convo?.active && !this.measuring && this.assistantPlaying) { this.error = MEASURE_PLAYING; return false; }
            if (!this.canMeasure) { this.error = 'Start a conversation first: Measure room listens through its microphone.'; return false; }
            this.error = ''; this.measuring = true; this.levels = []; this.message = 'Measuring… stay quiet for three seconds.';
            // A reading taken while the assistant plays ends the measurement: its voice is not the room.
            this.convo.meterTap = (level, playback) => { if (playback) this.abortMeasure(MEASURE_PLAYING); else this.levels.push(level); };
            cancel(measureTimer);
            measureTimer = schedule(() => this.finishMeasure(), seconds * 1000);
            return true;
        },
        finishMeasure() {
            measureTimer = null;
            if (this.convo) this.convo.meterTap = null;
            const threshold = measuredThreshold(this.levels);
            this.measuring = false;
            if (threshold === null) { this.error = 'Too little was heard to measure; is the microphone live?'; return null; }
            this.set('noise_gate_db', threshold);
            this.message = `Noise gate set to ${threshold} dB (the room's floor + ${MEASURE_MARGIN_DB} dB) on this device.`;
            return threshold;
        },
        abortMeasure(error) { this.stopMeasure(); this.message = ''; this.error = error; },
        stopMeasure() { cancel(measureTimer); measureTimer = null; if (this.measuring && this.convo) this.convo.meterTap = null; this.measuring = false; },
    };
}

export function createSoundStore(createStore, options) { return createStore('convoSound', soundStoreDefinition(options)); }
