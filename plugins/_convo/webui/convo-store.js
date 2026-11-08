import { createStore } from '/js/AlpineStore.js';
import { callJsonApi } from '/js/api.js';
import { createNamespacedClient } from '/js/websocket.js';
import { getCurrentContextId, getCurrentContext, openModal } from '/js/shortcuts.js';
import { AudioSession } from './audio.js';
import { ACTIVE_JOB, HIDDEN_KINDS, PAGE, captureAnchor, deriveTimings, describeEvent, markTurn, mergeEvents, nearBottom, restoreAnchor, summarizeTimings, toBottom, wrapTab } from './view-helpers.js';
import { announce, cueFor, emitCue, orbView, syncOrb } from './orb.js';
import { bindOrbGesture, continuousDraft, dictationEndNote } from './dictation.js';

// Non-reactive owned resources: Alpine must not proxy AudioNodes or Socket.IO.
let client = null, audio = null, generation = 0;
let candidateCapture = null, candidatePending = null, candidateSending = false;
const candidates = new Map();
let nativePromise = null, nativeBinding = null, dictationPromise = null, dictationHeld = false, dictationEnding = null, dictationWasVoice = false, lastDictationLabel = '', dictationBuffer = null, bufferSettling = null;
let historyTarget = '', historySequence = 0, refreshTimer = null;
let scroller = null, stick = true, settleQueued = false;   // the transcript element and whether the reader follows its end
let capturing = false, lastActivity = 0, noticePending = false, noteTimer = null, lastOrb = null, holdTimer = null;
const timings = new Map();                // turn -> monotonic marks (measurement only; no content)
const clock = () => globalThis.performance?.now?.() ?? Date.now();
const mark = (turn, name, at = clock()) => markTurn(timings, turn, name, at);
const noticeSeen = new Set();
const endpoint = '/plugins/_convo/conversation';
const NO_CHAT = 'Select or create a chat first.';
// Diagnostics taps: optional observers a developer panel may set or clear at any time (`echo` receives each echo-guard
// metric). Observational only: a tap can never throw into the voice path, and nothing passed to one holds audio.
export const taps = {};

async function native() {
    if (!nativePromise) nativePromise = (async () => {
        const path = '/plugins/_whisper_stt/webui/whisper-stt-store.js';
        const modern = await fetch(path, { cache: 'no-store' }).then(r => r.ok).catch(() => false);
        if (modern) {
            const { store } = await import(path);
            await store.initRuntime?.();
            return nativeBinding = { store, device: () => store.getSelectedDevice(), transcribe: '/plugins/_whisper_stt/transcribe' };
        }
        const [{ store }, { store: devices }] = await Promise.all([
            import('/components/chat/speech/speech-store.js'), import('/components/settings/speech/microphone-setting-store.js'),
        ]);
        return nativeBinding = { store, device: () => devices.getSelectedDevice(), transcribe: '/transcribe' };
    })();
    return nativePromise;
}

// ---- Draft in composer (continuous) ----------------------------------------------------------------------------------------------
// The host speech store, for decisions that cannot wait for an import (a send in progress, the orb's words): the binding once known,
// else the host's own Alpine store.
function hostSpeech() { return nativeBinding?.store || globalThis.Alpine?.store?.('whisperStt') || null; }
// Whether hands-free dictation drafts continuously now: Agent Zero's message_mode is draft and Convo's voice_message_draft is
// continuous, and no host component asked for the transcript itself (see continuousDraft in dictation.js, shared with the recorder).
function continuousMode(host = hostSpeech()) {
    return continuousDraft({ flag: store.settings?.voice_message_draft, messageMode: host?.config?.message_mode, handler: host?.finalTranscriptHandler });
}
// The recorder is drafting continuously: it is in continuous mode now, or it already added phrases this dictation.
const drafting = (mic, host = hostSpeech()) => !!mic && ((mic.phrasesAdded || 0) > 0 || continuousMode(host));
// The host store reads its message_mode only when its own microphone button is clicked, so a choice saved in the Whisper settings (or
// in another tab) would reach orb dictation late. At the start of a dictation, read both values from Convo's bridge and take them in
// as the host's own status read would. Never the host's refreshStatus here: a failed read there stops the microphone.
async function syncDraftMode(host) {
    try {
        const status = await callJsonApi('/plugins/_convo/stt_status', {});
        if (host && status?.config?.message_mode) host.config = { ...(host.config || {}), ...status.config };
        if (store.settings && ['single', 'continuous'].includes(status?.voice_message_draft)) store.settings.voice_message_draft = status.voice_message_draft;
        updateOrb();
    } catch { /* the values already known stand */ }
}
// After a manual send the host empties the composer a moment later (once its model check has answered). A phrase that finishes
// transcribing in between waits for that, so it lands in the emptied composer instead of being wiped with the sent text. Bounded:
// a send that never empties it (another extension cancelled it) releases the phrase after `limit`. No composer to watch: no wait.
function composerCleared(sent, { step = 50, limit = 3000 } = {}) {
    const text = String(sent ?? '').trim(), composer = () => globalThis.Alpine?.store?.('chatInput');
    if (!text || !composer()) return null;
    return new Promise(resolve => {
        let waited = 0;
        const check = () => {
            if (String(composer()?.message ?? '').trim() !== text || waited >= limit) { resolve(); return; }
            waited += step; setTimeout(check, step);
        };
        check();
    });
}
// The host microphone's continuous drafting (Convo is not dictating through the orb): `how(mic)` finishes it (a send: the phrase being
// transcribed still lands) or discards it, then the host gets its button back.
function stopHostDrafting(host, how) {
    const mic = host?.microphoneInput;
    if (!mic || typeof mic.finishConvoDictation !== 'function') return null;
    return (async () => {
        try { await how(mic); }
        finally { if (host.microphoneInput === mic) host.microphoneInput = null; host.notifyStatusChange?.(); }
    })().catch(() => {});
}

async function request(event, data = {}, socket = client) {
    if (!socket) throw new Error('Convo is disconnected.');
    const envelope = await socket.request('convo.' + event, data, { timeoutMs: event === 'start' || event === 'retarget' ? 50000 : 10000 });
    const result = envelope.results?.find(r => r.handlerId?.includes('Realtime')) || envelope.results?.[0];
    if (!result?.ok || !result.data?.ok) {
        // `stale` (the epoch moved on) and `busy` (an earlier turn is still being decided) are ordinary, not an outage.
        // `refused` carries the reason itself: the host masks anything a handler raises as "Internal server error".
        const kind = result?.data?.stale ? 'stale' : result?.data?.busy ? 'busy' : result?.data?.refused ? 'refused' : 'rejected';
        const message = result?.error?.error || (kind === 'refused' && result.data.message) || (kind === 'stale' ? 'Stale voice operation withheld.' : kind === 'busy' ? 'Convo is still deciding on an earlier turn.' : 'Convo request was rejected. Check host diagnostics.');
        throw Object.assign(new Error(message), { kind });
    }
    return result.data;
}

function identity() { return store.session ? { session_id: store.session.id, epoch: store.session.epoch } : {}; }
function fail(error) { store.error = error?.message || String(error); updateOrb(); }

function beginCandidate(attempt) {
    if (attempt !== generation || !store.session) return;
    setHearing(true); lastActivity = Date.now();
    candidateCapture = { turn_id: crypto.randomUUID(), sequence: 0, attempt, final: false };
    candidates.set(candidateCapture.turn_id, candidateCapture);
    mark(candidateCapture.turn_id, 'onset', audio?.boundary?.onsetAt || clock());
    while (candidates.size > 16) candidates.delete(candidates.keys().next().value);
    // Detection is observation only. Never cancel playback here.
}

async function sendCandidate(value, attempt, final) {
    if (attempt !== generation || !store.session || !candidateCapture) return;
    const candidate = candidateCapture;
    candidate.sequence++; candidate.final = final;
    if (final) { mark(candidate.turn_id, 'speech_end', audio?.boundary?.lastTurn?.voicedAt || clock()); mark(candidate.turn_id, 'final_sent'); }
    else if (candidate.sequence === 1) mark(candidate.turn_id, 'first_probe_sent');
    if (candidatePending?.final && candidatePending.candidate !== candidate) {
        if (final) { setHearing(false); candidateCapture = null; }
        hold(true);
        fail(new Error('A completed turn is still awaiting policy; this newer observation was withheld.'));
        return;
    }
    candidatePending = { candidate, value, sequence: candidate.sequence, final };
    if (final) candidate.audio = value;
    if (final) { setHearing(false); candidateCapture = null; lastActivity = Date.now(); }
    if (candidateSending) return; // Latest observation replaces an unsent probe.
    candidateSending = true;
    try {
        while (candidatePending) {
            const item = candidatePending; candidatePending = null;
            if (item.candidate.attempt !== generation || !store.session) continue;
            try {
                await request('candidate', { ...identity(), turn_id: item.candidate.turn_id,
                    sequence: item.sequence, final: item.final, audio: item.value });
            } catch (error) {
                if (item.candidate.attempt !== generation || !store.session) continue;      // the session ended meanwhile
                if (error.kind === 'stale') continue;                                         // an older epoch: nothing to decide any more
                hold(true);                                                                   // speech we cannot yet decide about stays ducked
                if (error.kind === 'busy') {
                    const tries = item.tries || 0;
                    if (item.final && tries < store.busyRetryMs.length) {
                        // A completed turn is never dropped for want of a free server: it stays pending, so a newer
                        // observation is withheld instead of interleaved, and is sent again shortly.
                        item.tries = tries + 1; candidatePending = item;
                        await new Promise(resolve => setTimeout(resolve, store.busyRetryMs[tries]));
                    } else if (item.final) {
                        if (!undecided(item.candidate.turn_id)) hold(false);
                        fail(new Error('Convo was still busy with an earlier turn, so this one was not answered. Please say it again.'));
                    }
                    continue;                                                                 // a busy probe is simply replaced by the next one
                }
                fail(new Error('Turn policy unavailable; speech is withheld and playback ducked. No interruption was assumed.'));
            }
        }
    } finally { candidateSending = false; }
}

// Capture and policy-hold state also drive the orb, so they live on the store as well as in the audio session.
function setHearing(value) { capturing = value; if (store.hearing !== value) { store.hearing = value; updateOrb(); } }
// Another utterance is open or waiting to be sent: it, not the one that just finished, decides whether playback is ducked.
function undecided(turn) {
    return !!((candidateCapture && candidateCapture.turn_id !== turn) || (candidatePending && candidatePending.candidate.turn_id !== turn));
}
// A duck is a promise that a decision is coming. If none does, playback must not stay quiet for good: after the user has
// stopped speaking and nothing is waiting to be sent, the duck is released (it is renewed by every decision that arrives).
function hold(value, immediate = false) {    // `immediate`: release now, skipping the audio-side release delay (the decision already ended)
    clearTimeout(holdTimer); holdTimer = null;
    audio?.setPolicyHold(value, immediate);
    if (value) holdTimer = setTimeout(releaseStaleHold, store.holdSafetyMs);
    if (store.policyHold !== value) { store.policyHold = value; updateOrb(); }
}
function releaseStaleHold() {
    holdTimer = null;
    if (!store.policyHold) return;
    if (capturing || candidatePending || candidateSending) { holdTimer = setTimeout(releaseStaleHold, store.holdSafetyMs); return; }
    hold(false);
}

// The orb is a pure function of store state; cues follow its transitions, so every path to a state cues the same way.
function updateOrb() {
    const view = orbView({ enabled: store.enabled, dictating: store.dictating || !!store.dictationMode, dictationMode: store.dictationMode,
        continuous: !!store.dictationMode && continuousMode(), holding: store.dictationHolding, pressing: store.pressing, capturing: store.capturing, error: store.error, busy: store.busy, active: store.active,
        state: store.state, hearing: store.hearing, playing: !!(audio?.playing || audio?.pcm?.size || audio?.queue?.length || store.outputLevel > .02),
        devIdle: !!store.settings?.ai_link_development_mode, policyHold: store.policyHold });
    const id = view?.id ?? null;
    if (id !== lastOrb) { const cue = cueFor(lastOrb, id); lastOrb = id; if (cue) emitCue(cue); }
    // A dictation transition (latched, push-to-talk, released) is also said aloud: the label changed, a screen reader will not read it.
    const spoken = view && id.startsWith('dictating-') ? view.label : '';
    if (spoken !== lastDictationLabel) { lastDictationLabel = spoken; if (spoken) announce(spoken); }
    const mic = typeof document !== 'undefined' ? document.getElementById?.('microphone-button') : null;
    if (mic) syncOrb(mic, view, id === 'speaking' ? store.outputLevel : store.inputLevel);
}

// Run after Alpine has rendered the change that was just made (it flushes in microtasks). A timer, not
// requestAnimationFrame: frames are paused in a background tab or a hidden pane, and the view must still settle.
function afterRender(callback) { setTimeout(callback, 0); }

let lastLevelPaint = 0;
function level(kind, value) {
    store[kind === 'input' ? 'inputLevel' : 'outputLevel'] = value;
    if (!value || Date.now() - lastLevelPaint >= 50) { lastLevelPaint = Date.now(); updateOrb(); }
}

// What a finished dictation says out loud (the note, and the screen-reader announcement). Nothing is said for a delivered transcript:
// the host shows it in the composer or sends it, whichever its own voice-message setting says.
function dictationNote(message) { store.say(message); announce(message); }
// Hands-free dictation is the host's own: it may end it itself (draft voice-message mode stops after putting one utterance in the
// composer). The recorder reports its status; when it goes inactive while Convo still believes it is dictating, follow the host.
let watchedRecorder = null;
function watchHostEnd(mic) {
    watchedRecorder = mic;
    mic.onConvoStatus = status => {
        if (status !== 'inactive' || watchedRecorder !== mic || !store.dictating || dictationEnding) return;
        watchedRecorder = null;
        const continuous = drafting(mic);                // e.g. speech playback started: the host stops listening itself
        store.dictating = false; store.dictationMode = ''; store.dictationHolding = false; dictationHeld = false;
        updateOrb();
        if (continuous) dictationNote(dictationEndNote({ phrases: mic.phrasesAdded, reason: 'finish' }));
        else announce('Dictation finished.');
        void native().then(binding => { mic.dispose?.(); if (binding.store.microphoneInput === mic) binding.store.microphoneInput = null; });
    };
}
// `continuous`: the dictation drafted into the composer, so its end says how many phrases it added (dictationEndNote), whatever
// the reason; nothing heard at all still says so.
function dictationReport(outcome, { continuous = false, phrases = 0, reason = 'finish' } = {}) {
    if (continuous && !(phrases === 0 && (outcome === 'empty' || outcome === 'silent'))) {
        const note = dictationEndNote({ phrases, reason: outcome === 'cancelled' && reason !== 'off' ? 'cancel' : reason });
        dictationNote(outcome === 'error' ? `Transcription failed for the last phrase. ${note}` : note);
        return outcome;
    }
    if (outcome === 'cancelled') dictationNote('Dictation cancelled. Nothing was sent.');
    else if (outcome === 'empty' || outcome === 'silent') dictationNote('Nothing was heard, so nothing was sent.');
    else if (outcome === 'error') dictationNote('Transcription failed, so nothing was sent.');
    else announce('Dictation finished.');
    return outcome;
}

export const store = createStore('convo', {
    settings: null, settingsRevision: '', session: null, state: 'off', error: '', mode: 'active', target: '', selected: '', runtime: null,
    events: [], jobs: [], draft: '', dictating: false, panelOpen: false, busy: false,
    dictationMode: '', dictationHolding: false, pressing: false, capturing: false,   // orb gesture: '' | 'latched' | 'ptt'; the press still down; a press heading for the latch time
    moreEarlier: false, loadingEarlier: false, unseen: 0, pinned: true,
    inputLevel: 0, outputLevel: 0, targetName: '', hearing: false, policyHold: false, holdSafetyMs: 8000, note: '',
    busyRetryMs: [250, 500, 1000, 1500, 2000],   // how long a completed turn may wait for a free server before it is reported as not answered
    get statusLabel() { return this.dictating ? 'Dictating' : this.error ? 'Needs attention' : ({off:'Ready · microphone off',connecting:'Starting',listening:'Listening',speaking:'Speaking',considering:'Thinking',thinking:'Thinking'}[this.state] || this.state); },
    get chipState() { return this.dictating ? 'dictation' : this.error ? 'error' : this.state === 'off' ? 'ready' : this.state; },
    get targetLabel() { return this.targetName || (this.target ? 'Selected chat' : 'Select a chat to begin'); },
    get canRetarget() { return !!this.selected && this.selected !== this.target; },
    get activeJobs() { return this.jobs.filter(job => ACTIVE_JOB.has(job.status)); },
    get visibleEvents() {
        if (this.settings?.developer_mode) return this.events;
        const transcripts = new Map(this.events.filter(event => event.kind === 'user.transcript').map(event => [`${event.session}:${event.turn}`, event.payload.text]));
        return this.events.filter(event => !HIDDEN_KINDS.has(event.kind)).map(event => {
            const transcript = transcripts.get(`${event.session}:${event.turn}`);
            return event.kind === 'user.accepted' && transcript ? {...event,payload:{...event.payload,transcript}} : event;
        });
    },
    eventRole(event) { return describeEvent(event, this.jobs).role; },
    eventLabel(event) { return describeEvent(event, this.jobs).label; },
    syncTargetName() { const selected = getCurrentContext(); if (selected?.id === this.target) this.targetName = selected.name || selected.title || ''; },
    async mountPanel() { this.panelOpen = true; if (!this.active) this.target = getCurrentContextId() || ''; this.syncTargetName(); await this.refresh(); await this.history(); },
    unmountPanel() { this.panelOpen = false; },
    get enabled() { return !!this.settings?.enabled; },
    get active() { return !!this.session; },
    async refresh() {
        // Continuous drafting stops when Convo is turned off: the orb's dictation when Convo stops owning the orb, the host
        // microphone's when the plugin stops answering. Decided on the settings it started under (they are replaced below).
        const orbDrafting = this.dictating && continuousMode(), host = hostSpeech();
        const hostDrafting = !this.dictating && !!host?.microphoneInput && host.microphoneInput.status !== 'inactive' && drafting(host.microphoneInput, host);
        try {
            const data = await callJsonApi(endpoint, { action: 'status' });
            this.settings = data.settings;
            this.settingsRevision = data.settings_revision || '';
            void globalThis.Alpine?.store?.('convoSetup')?.noteRevision?.(this.settingsRevision);   // another editor saved?
            this.runtime = data.runtime || {mode: 'external'};
            this.selected = getCurrentContextId() || '';
            if (this.selected && this.error === NO_CHAT) this.error = '';          // a chat was chosen since: that complaint is no longer true
            if (!this.active && !this.busy) { this.target = getCurrentContextId() || ''; this.targetName = ''; }
            this.syncTargetName();
            updateOrb();
            if (!this.enabled && (this.active || this.busy)) await this.stop();
            if (!this.enabled && orbDrafting) void this.endDictation({ cancel: true, reason: 'off', continuous: true });
        } catch (error) {
            // Disabled plugins can disappear from the host API registry. Losing
            // status must release capture/playback, not leave an ambient mic open.
            this.settings = null;
            if (this.active || this.busy) await this.stop();
            if (orbDrafting) void this.endDictation({ cancel: true, reason: 'off', continuous: true });
            if (hostDrafting) stopHostDrafting(host, mic => mic.discardConvoDictation());
            fail(error);
        }
    },
    async show() {
        await this.refresh(); this.panelOpen = true;
        if (!this.active) this.target = getCurrentContextId() || '';
        await openModal('/plugins/_convo/webui/main.html');
        await this.history();
    },
    async openSettings() {
        const { store: settings } = await import('/components/plugins/plugin-settings-store.js');
        await settings.openConfig('_convo');
    },
    // The canvas settings live in a modal over the same shared draft the plugin Config uses; closing guards unsaved edits.
    async openSettingsModal() {
        try {
            await import('/plugins/_convo/webui/setup-store.js');
            const { ensureModalOpen } = await import('/js/modals.js');
            await ensureModalOpen('/plugins/_convo/webui/settings-modal.html', () => this.settingsMayClose());
        } catch (error) { fail(error); }
    },
    async settingsMayClose() {
        const { store: setup } = await import('/plugins/_convo/webui/setup-store.js');
        if (!setup.dirty) return true;
        if (!window.confirm('Discard unsaved Convo settings?')) return false;
        setup.discard();
        return true;
    },
    // The host gives modals no focus trap: wrap Tab inside the modal shell (header, body and footer).
    trapFocus(element) {
        const shell = element?.closest?.('.modal-inner') || element;
        if (!shell) return () => {};
        const handler = event => wrapTab(event, shell, shell.ownerDocument?.activeElement);
        shell.addEventListener('keydown', handler);
        return () => shell.removeEventListener('keydown', handler);
    },
    async openStudio() { await openModal('/plugins/_convo/webui/studio.html?v=20260911-ready'); },
    async migrateSettings() {
        try {
            const preview = await callJsonApi(endpoint, { action: 'migration_preview' });
            if (!preview.changed) { this.error = 'No legacy settings need migration.'; return; }
            if (!window.confirm('Merge legacy Enhanced Speech settings into Convo? Existing Convo values win; rollback state will be retained.')) return;
            await callJsonApi(endpoint, { action: 'migrate' });
            await this.refresh(); this.error = 'Migration saved. Close and reopen plugin settings before editing them.';
        } catch (error) { fail(error); }
    },
    // A short, honest message instead of a failed start: development mode keeps browser voice idle on purpose.
    say(message) { this.note = message; clearTimeout(noteTimer); noteTimer = setTimeout(() => { this.note = ''; }, 8000); },
    async toggle() {
        if (this.active || this.busy) { await this.stop(); return; }
        if (this.settings?.ai_link_development_mode) { this.say('AI Link development mode keeps browser voice idle. Hold the orb to dictate, or leave the mode in Convo settings.'); return; }
        await this.start();
    },
    async start() {
        if (this.busy || this.active) return;
        const attempt = ++generation; this.busy = true; this.error = ''; this.state = 'connecting';
        updateOrb();
        let ownedClient = null, ownedAudio = null;
        try {
            await this.refresh();
            if (!this.enabled) throw new Error('Enable Convo and configure its providers in plugin settings.');
            const binding = await native();
            if (attempt !== generation) return;
            await this.endDictation();
            const mic = binding.store.microphoneInput;
            if (mic) {
                if (typeof mic.dispose !== 'function') throw new Error('Native microphone cannot be safely released on this host. Reload with the Convo compatibility adapter.');
                mic.dispose(); binding.store.microphoneInput = null;
            }
            this.target = getCurrentContextId() || '';
            this.syncTargetName();
            if (!this.target) throw new Error(NO_CHAT);
            ownedClient = createNamespacedClient('/ws'); client = ownedClient;
            ownedClient.addHandlers(['plugins/_convo/realtime']);
            await ownedClient.on('convo.disabled', () => { if (client === ownedClient) void this.stop(); });
            ownedClient.onDisconnect(() => { if (client === ownedClient) { fail(new Error('Voice disconnected. Jobs continue in their target chats.')); void this.stop(); } });
            await ownedClient.on('convo.event', envelope => {
                const event = envelope.data;
                if (client === ownedClient && this.session && event?.session_id === this.session.id && event.type === 'candidate.admitted') {
                    if (event.previous_epoch !== this.session.epoch || !candidates.has(event.turn_id)) return;
                    this.session = event.new_session;
                    mark(event.turn_id, 'admitted_recv');
                    audio?.interrupt(); hold(false, true); this.draft = '';
                    mark(event.turn_id, 'interrupt_done');
                    return;
                }
                if (client !== ownedClient || !this.session || event?.session_id !== this.session.id || event.epoch !== this.session.epoch) return;
                if (event.type === 'candidate.state') {
                    const candidate = candidates.get(event.turn_id);
                    if (!candidate || event.sequence < candidate.sequence) return;
                    const held = ['considering', 'withhold', 'degraded'].includes(event.state);
                    // A completed turn's decision is final: a withheld or degraded one is simply not answered, so the
                    // assistant is no longer ducked for it (unless a newer utterance is already open).
                    const terminal = event.final && event.state !== 'considering';
                    hold(held && !(terminal && !undecided(event.turn_id)));
                    if (event.state === 'degraded') fail(new Error(terminal ? 'Turn policy unavailable; this turn was not answered.' : 'Turn policy unavailable; speech is withheld and playback ducked.'));
                    if (event.final && event.state === 'admit' && candidate.audio && this.settings.transcription_enabled && this.mode === 'active') {
                        const ids = identity();
                        void (async () => {
                            const binding = await native();
                            const result = await callJsonApi(binding.transcribe, {audio: candidate.audio, mime_type: 'audio/wav'});
                            if (ids.session_id === this.session?.id && result?.text) await request('transcript', {...ids, turn_id: event.turn_id, text: result.text});
                        })().catch(() => { this.error = 'Transcription unavailable; the audio conversation is unaffected.'; });
                    }
                    if (event.final && event.state !== 'considering') candidates.delete(event.turn_id);
                    return;
                }
                if (event.type === 'state') { this.state = event.state; lastActivity = Date.now(); updateOrb(); }
                if (event.type === 'error') { if (audio?.pcm?.size) audio.interrupt(); fail(new Error(event.message)); }
                if (event.type === 'text.delta') this.draft += event.text;
                if (event.type === 'audio.begin') {
                    if (this.policyHold && !undecided('')) hold(false, true);   // a new reply is never born ducked by a decision that already ended
                    this.state = 'speaking'; updateOrb(); mark(event.turn_id, 'audio_event');
                    const acknowledge = status => {
                        if (status === 'started') mark(event.turn_id, 'source_start');
                        if (client === ownedClient && event.session_id === this.session?.id && event.epoch === this.session?.epoch)
                            void request('playback', { ...identity(), turn_id: event.turn_id, phrase_id: event.phrase_id, status }).catch(fail);
                    };
                    audio?.pcmBegin(event, () => acknowledge('completed'), () => acknowledge('started'));
                }
                if (event.type === 'audio.chunk') audio?.pcmChunk(event);
                if (event.type === 'audio.end') audio?.pcmEnd(event);
                if (event.type === 'audio.abort') audio?.pcmAbort(event);
                if (event.type === 'audio.phrase') {
                    if (this.policyHold && !undecided('')) hold(false, true);
                    this.state = 'speaking'; updateOrb(); mark(event.turn_id, 'audio_event');
                    audio?.enqueue(event.audio, () => {
                        if (event.epoch === this.session?.epoch) void request('playback', { ...identity(), turn_id: event.turn_id, phrase_id: event.phrase_id, status: 'completed' }).catch(fail);
                    }, () => {
                        mark(event.turn_id, 'source_start');
                        if (event.epoch === this.session?.epoch) void request('playback', { ...identity(), turn_id: event.turn_id, phrase_id: event.phrase_id, status: 'started' }).catch(fail);
                    });
                }
                if (event.type === 'job.queued' || (event.type === 'state' && event.state === 'listening')) void this.history();
            });
            const response = await request('start', { target: this.target, mode: this.mode }, ownedClient);
            if (attempt !== generation) { await request('stop', { session_id: response.session.id }, ownedClient).catch(() => {}); return; }
            this.session = response.session;
            ownedAudio = new AudioSession({ maximum: this.settings.max_audio_seconds, deviceId: binding.device()?.deviceId, echoGuard: this.settings.echo_guard,
                onStart: () => beginCandidate(attempt),
                onProbe: value => { if (!this.settings.text_input_required) void sendCandidate(value, attempt, false); },
                onTurn: value => { if (attempt === generation) void this.turn(value, attempt); },
                onError: fail, onLevel: level, onEchoMetric: m => { try { taps.echo?.(m) } catch {} } });
            audio = ownedAudio;
            await ownedAudio.start();
            if (attempt !== generation) return;
            this.state = 'listening'; updateOrb(); await this.history();
        } catch (error) { if (attempt === generation) { fail(error); await this.stop(); } }
        finally {
            if (attempt !== generation) { ownedAudio?.close(); ownedClient?.disconnect(); }
            else { this.busy = false; updateOrb(); }
        }
    },
    async stop() {
        generation++; this.busy = false; this.state = 'off';
        setHearing(false); this.policyHold = false; clearTimeout(holdTimer); holdTimer = null;
        candidateCapture = null; candidatePending = null; candidates.clear();
        const current = this.session, socket = client;
        this.session = null; client = null; audio?.close(); audio = null; this.draft = '';
        if (current && socket) await request('stop', { session_id: current.id }, socket).catch(() => {});
        socket?.disconnect();
        updateOrb();
    },
    async interrupt() {
        audio?.interrupt(); this.draft = '';
        if (!this.session) return;
        const before = this.session.id;
        try {
            const result = await request('interrupt', identity());
            if (this.session?.id === before) { this.session = result.session; this.state = 'listening'; updateOrb(); }
        } catch (error) { fail(error); await this.stop(); }
    },
    async turn(value, attempt) {
        setHearing(false); lastActivity = Date.now();
        if (attempt !== generation || !this.session) return;
        if (!this.settings.text_input_required) { await sendCandidate(value, attempt, true); return; }
        const candidate = candidateCapture;
        candidateCapture = null; // Text-only capability remains explicit, never substitutes for direct audio.
        const turn_id = candidate?.turn_id || crypto.randomUUID(); const ids = identity();
        try {
            const transcription = async () => {
                const binding = await native();
                const result = await callJsonApi(binding.transcribe, { audio: value, mime_type: 'audio/wav' });
                return result?.text || '';
            };
            if (this.settings.text_input_required) {
                const transcript = await transcription();
                if (attempt !== generation || ids.epoch !== this.session?.epoch) return;
                if (!candidate) return;
                candidate.sequence++; candidate.final = true;
                await request('candidate', { ...ids, turn_id, sequence: candidate.sequence, final: true, transcript });
            } else {
                await request('turn', { ...ids, turn_id, audio: value });
                if (this.settings.transcription_enabled && this.mode === 'active') void transcription().then(text => {
                    if (ids.session_id === this.session?.id && text) return request('transcript', { ...ids, turn_id, text });
                }).catch(() => { this.error = 'Transcription unavailable; the audio conversation is unaffected.'; });
            }
        } catch (error) { fail(error); }
    },
    async retarget() {
        const target = getCurrentContextId();
        if (!target || target === this.target) return;
        if (!this.session) { this.target = target; this.targetName = ''; this.syncTargetName(); await this.history(); return; }
        const attempt = ++generation;
        setHearing(false);
        this.busy = true; updateOrb();
        const ownedAudio = audio; audio = null; const echoState = ownedAudio?.echoSnapshot?.() ?? null; ownedAudio?.close();   // the learned echo path outlives the recreated session
        try {
            const result = await request('retarget', { ...identity(), target });
            if (attempt !== generation) return;
            this.session = result.session; this.target = target; this.targetName = ''; this.syncTargetName(); this.draft = '';
            const binding = await native();
            if (attempt !== generation) return;
            const replacement = new AudioSession({ maximum: this.settings.max_audio_seconds, deviceId: binding.device()?.deviceId, echoGuard: this.settings.echo_guard, echoState,
                onStart: () => beginCandidate(attempt),
                onProbe: value => { if (!this.settings.text_input_required) void sendCandidate(value, attempt, false); },
                onTurn: value => { if (attempt === generation) void this.turn(value, attempt); }, onError: fail, onLevel: level,
                onEchoMetric: m => { try { taps.echo?.(m) } catch {} } });
            audio = replacement; await replacement.start();
            if (attempt !== generation) replacement.close();
        } catch (error) { fail(error); await this.stop(); }
        finally { if (attempt === generation) { this.busy = false; updateOrb(); } }
        await this.history();
    },
    // The newest page on first read, then only what is newer than what is already known.
    async history() {
        const target = this.target;
        if (!target) return;
        if (historyTarget !== target) {
            historyTarget = target; historySequence = 0; this.events = []; this.jobs = [];
            this.moreEarlier = false; this.unseen = 0; stick = true; this.pinned = true;
        }
        try {
            const first = historySequence === 0;
            const result = await callJsonApi(endpoint, first ? { action: 'history', target, tail: PAGE } : { action: 'history', target, after: historySequence });
            if (this.target !== target) return;
            const known = new Set(this.events.map(e => e.seq));
            const fresh = result.events.filter(e => !known.has(e.seq));
            this.events = mergeEvents(this.events, fresh);
            historySequence = Math.max(historySequence, ...result.events.map(e => e.seq));
            if (first) this.moreEarlier = !!result.more;
            this.jobs = result.jobs;
            if (fresh.length) {
                if (stick) this.queueSettle();
                else this.unseen += fresh.filter(e => this.settings?.developer_mode || !HIDDEN_KINDS.has(e.kind)).length;
            }
            if (this.active) void this.deliverNotice();
        } catch (error) { fail(error); }
    },
    // Older messages are fetched on request and added above without moving what the reader is looking at.
    async loadEarlier() {
        if (this.loadingEarlier || !this.moreEarlier || !this.events.length) return;
        const target = this.target, before = this.events[0].seq, element = scroller, anchor = captureAnchor(element);
        this.loadingEarlier = true;
        try {
            const result = await callJsonApi(endpoint, { action: 'history', target, tail: PAGE, before });
            if (this.target !== target) return;
            this.events = mergeEvents(this.events, result.events);
            this.moreEarlier = !!result.more;
            afterRender(() => restoreAnchor(element, anchor));
        } catch (error) { fail(error); }
        finally { this.loadingEarlier = false; }
    },
    bindTranscript(element) { scroller = element; stick = true; this.pinned = true; this.unseen = 0; this.queueSettle(); },
    unbindTranscript(element) { if (scroller === element) scroller = null; },
    onScroll(element) {
        stick = nearBottom(element);
        if (this.pinned !== stick) this.pinned = stick;
        if (stick) this.unseen = 0;
    },
    // Called whenever the rendered conversation may have grown: follow the end only if the reader is already there.
    queueSettle() {
        if (settleQueued) return;
        settleQueued = true;
        afterRender(() => { settleQueued = false; this.settleScroll(); });
    },
    settleScroll() { if (scroller && stick) toBottom(scroller); },
    jumpToLatest() { stick = true; this.pinned = true; this.unseen = 0; toBottom(scroller); },
    eventText(event) { return describeEvent(event, this.jobs).text; },
    // Content-free latency marks for this page (milliseconds), for measurement runs and developer diagnostics.
    timingReport() {
        return { turns: [...timings].map(([turn, record]) => ({ turn, marks: { ...record }, ...deriveTimings(record) })), summary: summarizeTimings(timings) };
    },
    async deliverNotice() {
        if (!this.session || noticePending || capturing || this.busy || this.state !== 'listening' || audio?.playing || audio?.decoding || audio?.pcm?.size || audio?.queue?.length || Date.now() - lastActivity < 4000) return;
        const job = this.jobs.find(job => job.session === this.session.id && job.target === this.target && ['completed','failed','uncertain'].includes(job.status) && !noticeSeen.has(job.id));
        if (!job) return;
        noticePending = true;
        try {
            const result = await request('notice', { ...identity(), job_id: job.id });
            if (!result.busy) noticeSeen.add(job.id);
        } catch (error) { fail(error); noticeSeen.add(job.id); }
        finally { noticePending = false; }
    },
    async cancelJob(job) { try { await callJsonApi(endpoint, { action: 'cancel_job', target: job.target, job_id: job.id }); await this.history(); } catch (error) { fail(error); } },
    async steerJob(job) {
        const text = window.prompt('Explicitly steer this running job:');
        if (text?.trim()) try { await callJsonApi(endpoint, { action: 'steer_job', target: job.target, job_id: job.id, text }); } catch (error) { fail(error); }
    },
    // Dictation. One owned capture: a live Convo voice session is closed cleanly first (and not resumed), and a second start while one
    // runs is a no-op. `held` is a press still down (orb hold): the host recorder records unconditionally until the press is released
    // (latched) or finished (push-to-talk). The transcript goes to the host exactly as its own voice button hands it over, so the
    // host's voice-message handling (leave in the composer or send) applies, with one Convo addition: "Draft in composer
    // (continuous)" (host draft + voice_message_draft continuous) adds each phrase to the composer and keeps listening.
    setPressing(value) { value = !!value; if (this.pressing !== value) { this.pressing = value; updateOrb(); } },
    setDictationState({ mode, holding } = {}) {
        if (!this.dictationMode) return;                 // dictation already ended or never started: a late gesture effect changes nothing
        if (mode) this.dictationMode = mode;
        if (holding !== undefined) this.dictationHolding = !!holding;
        updateOrb();
        if (this.dictating) void native().then(binding => binding.store.microphoneInput?.setManual?.(this.dictationHolding));
    },
    // PRE-OPEN. A press that lasts past the tap time opens the one native microphone as a silent buffer, so the words spoken while the
    // press is still deciding are part of the dictation if it goes on. Nothing is transcribed or delivered, and a release before the
    // latch time discards the buffer and closes the stream. It never runs next to a Convo voice session (that would be two captures):
    // then dictation starts fresh at the latch time and the first seconds are not captured. Words before the pre-open time are never captured.
    async preOpenDictation() {
        if (!this.enabled || this.active || this.busy || this.dictating || dictationPromise || dictationEnding || dictationBuffer) return false;
        const entry = dictationBuffer = { ok: false, discarded: false, mic: null, promise: null };
        const release = async (mic) => { const binding = await native(); if (mic) { mic.dispose?.(); if (binding.store.microphoneInput === mic) binding.store.microphoneInput = null; } };
        entry.promise = (async () => {
            const binding = await native();
            if (entry.discarded) return;
            void syncDraftMode(binding.store);           // known by the latch time, well before a first phrase is delivered
            await binding.store.initMicrophone();
            const mic = binding.store.microphoneInput;
            if (!mic) throw new Error('The microphone could not be opened.');
            if (entry.discarded || typeof mic.startManual !== 'function') { await release(mic); return; }   // a recorder without held mode cannot buffer
            await mic.startManual();
            entry.mic = mic; entry.ok = true;
            if (!entry.discarded) { this.capturing = true; updateOrb(); }
        })().catch(async error => {
            entry.failed = true;
            await release(entry.mic).catch(() => {});
            if (!entry.discarded) this.say('The microphone could not be opened early, so speech from the start of this hold is not captured.');
        });
        return entry.promise;
    },
    // A press that had pre-opened ended before dictation began: nothing recorded is kept and the stream is closed at once.
    discardDictationBuffer() {
        const entry = dictationBuffer;
        if (!entry) return bufferSettling;
        dictationBuffer = null; entry.discarded = true;
        if (this.capturing) { this.capturing = false; updateOrb(); }
        const settling = bufferSettling = (async () => {
            await entry.promise;
            if (entry.mic) {
                const binding = await native();
                if (typeof entry.mic.discardConvoDictation === 'function') entry.mic.discardConvoDictation(); else entry.mic.dispose?.();
                if (binding.store.microphoneInput === entry.mic) binding.store.microphoneInput = null;
            }
        })().catch(() => {}).finally(() => { if (bufferSettling === settling) bufferSettling = null; });
        return settling;
    },
    dictationBufferSettled() { return bufferSettling; },
    async beginDictation({ held = false } = {}) {
        if (this.dictating && !dictationPromise) return;
        dictationHeld = true;
        this.dictationMode = 'latched'; this.dictationHolding = !!held; this.pressing = false; updateOrb();    // the decision shows at once
        if (dictationPromise) return dictationPromise;
        dictationPromise = (async () => {
            await dictationEnding;                       // a recorder still flushing a previous dictation is released before a new capture opens
            const buffered = dictationBuffer; dictationBuffer = null;
            const binding = await native();
            if (!buffered) void syncDraftMode(binding.store);   // a pre-opened press synced already
            let adopted = null;
            if (buffered) {                              // the press pre-opened the microphone: the SAME recording carries on, nothing restarts
                await buffered.promise;
                this.capturing = false;
                if (buffered.ok && !buffered.discarded && binding.store.microphoneInput === buffered.mic) adopted = buffered.mic;
            }
            if (!dictationHeld) {
                const orphan = adopted || binding.store.microphoneInput;
                if (adopted) { adopted.discardConvoDictation?.(); if (!adopted.discardConvoDictation) adopted.dispose?.(); binding.store.microphoneInput = null; }
                else if (orphan && buffered) { orphan.dispose?.(); binding.store.microphoneInput = null; }
                updateOrb(); return;
            }
            let mic = adopted;
            if (!mic) {
                dictationWasVoice = this.active || this.busy;
                await this.stop();
                if (!dictationHeld) return;
                await binding.store.initMicrophone();
                mic = binding.store.microphoneInput;
                if (!dictationHeld) { mic?.dispose?.(); binding.store.microphoneInput = null; return; }
                if (!mic) throw new Error('The microphone could not be opened for dictation.');
                try {
                    if (this.dictationHolding && typeof mic.startManual === 'function') await mic.startManual(); else await mic.toggle();
                } catch (error) { mic.dispose?.(); binding.store.microphoneInput = null; throw error; }
            }
            this.dictating = true; updateOrb();
            watchHostEnd(mic);
            mic.setManual?.(this.dictationHolding);      // the press may have been released while the recorder opened
            if (dictationWasVoice) store.say('Convo voice was paused for dictation. Click the orb to start it again.');   // said once, never queued behind the announcement
        })().catch(error => { this.dictationMode = ''; this.dictationHolding = false; dictationHeld = false; fail(error); }).finally(() => { dictationPromise = null; });
        return dictationPromise;
    },
    // `reason` 'sent' (the user sent the message) or 'off' (Convo was turned off) changes only what is said at the end; `continuous`
    // overrides whether this dictation was drafting continuously (decided by the caller when the settings are already gone).
    async endDictation({ cancel = false, reason = '', continuous = null } = {}) {
        dictationHeld = false; await dictationPromise;
        const wasDictating = this.dictating;
        this.dictating = false; this.dictationMode = ''; this.dictationHolding = false; this.pressing = false;
        updateOrb();
        if (!wasDictating) return dictationEnding;       // released before the recorder opened, or already finishing: nothing more to stop
        dictationEnding = (async () => {
            const binding = await native();
            const mic = binding.store.microphoneInput;
            const wasDrafting = continuous ?? drafting(mic, binding.store);
            let outcome = null;
            if (cancel) {
                if (typeof mic?.discardConvoDictation === 'function') { mic.discardConvoDictation(); binding.store.microphoneInput = null; }
                else await mic?.toggle();
                outcome = 'cancelled';
            } else if (typeof mic?.finishConvoDictation === 'function') { outcome = await mic.finishConvoDictation(); binding.store.microphoneInput = null; }
            else { await mic?.toggle(); }
            return dictationReport(outcome, { continuous: wasDrafting, phrases: mic?.phrasesAdded || 0, reason: reason === 'sent' || reason === 'off' ? reason : 'finish' });
        })().catch(fail).finally(() => { dictationEnding = null; });
        return dictationEnding;
    },
    async toggleDictation() { if (this.dictating || dictationHeld) await this.endDictation(); else await this.beginDictation(); },
    // A message was sent from the composer (Agent Zero's send_message_before, see extensions/webui/send_message_before). Continuous
    // drafting ends there, through the orb or the host microphone alike: listening stops at once and a phrase still being transcribed
    // lands in the composer the send empties. Ordinary dictation, a press still deciding, or none, is left alone. Never waits on
    // anything and never changes or cancels the send.
    composerSent(sendCtx = {}) {
        const host = hostSpeech(), mic = host?.microphoneInput;
        if (dictationBuffer || (this.dictationMode && !this.dictating)) return false;
        if (!mic || mic.status === 'inactive' || typeof mic.finishConvoDictation !== 'function' || !drafting(mic, host)) return false;
        mic.appendGate = composerCleared(sendCtx?.message);
        if (this.dictating && watchedRecorder === mic) void this.endDictation({ reason: 'sent', continuous: true });
        else void stopHostDrafting(host, recorder => recorder.finishConvoDictation());
        return true;
    },
});

// What playback is doing now, for diagnostics: flags and counts only, never audio. `active` is false without a voice session.
export function audioSnapshot() {
    if (!audio) return { active: false, ducked: false, toggles: 0, playing: false, queued: 0, pcm: 0 };
    return { active: true, ducked: !!audio.duck?.ducked, toggles: audio.duck?.toggles || 0, playing: !!audio.playing,
        queued: audio.queue?.length || 0, pcm: audio.pcm?.size || 0 };
}

export function install() {
    if (globalThis.__convoInstalled) return;
    globalThis.__convoInstalled = true;
    // The orb's press gesture (tap / latched dictation / push-to-talk) lives in dictation.js; this only hands it the store.
    bindOrbGesture({ store, document, window });
    // Escape also ends continuous drafting on the host's own microphone (Convo not holding the orb, whose gesture has its own Escape):
    // the text already added stays, the phrase in progress is dropped. Ordinary host dictation is left to the host.
    document.addEventListener('keydown', event => {
        if (store.enabled || event.isComposing || (event.key !== 'Escape' && event.code !== 'Escape')) return;
        const host = hostSpeech(), mic = host?.microphoneInput;
        if (!mic || mic.status === 'inactive' || !drafting(mic, host)) return;
        event.preventDefault(); event.stopImmediatePropagation();
        void stopHostDrafting(host, recorder => recorder.discardConvoDictation());
    }, true);
    document.addEventListener('keydown', event => {
        if (!store.enabled || event.repeat || event.isComposing) return;
        const parts = (store.settings.hotkey || '').split('+'); const key = parts.pop();
        if (event.ctrlKey !== parts.includes('Ctrl') || event.shiftKey !== parts.includes('Shift') || event.altKey !== parts.includes('Alt') || event.metaKey !== parts.includes('Meta')) return;
        if ((key === 'Space' && event.code === 'Space') || (key && event.key.toLowerCase() === key.toLowerCase())) { event.preventDefault(); void store.toggle(); }
    });
    window.addEventListener('pagehide', () => { audio?.close(); client?.disconnect(); });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) void store.refresh(); });   // a returning tab checks for changes made elsewhere
    refreshTimer = setInterval(() => {
        if (!document.hidden || store.active || store.busy) {
            void store.refresh(); if (store.active || store.panelOpen) void store.history();
            updateOrb();
        }
    }, 2000);
    void store.refresh();
}
