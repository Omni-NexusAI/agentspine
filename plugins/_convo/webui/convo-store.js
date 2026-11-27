import { createStore } from '/js/AlpineStore.js';
import { callJsonApi } from '/js/api.js';
import { createNamespacedClient } from '/js/websocket.js';
import { getCurrentContextId, getCurrentContext, openModal } from '/js/shortcuts.js';
import { AudioSession } from './audio.js';
import { ACTIVE_JOB, GLOBAL_SCOPE, PAGE, captureAnchor, deriveTimings, describeEvent, foldTurns, markTurn, mergeEvents, nearBottom, restoreAnchor, summarizeTimings, toBottom, wrapTab } from './view-helpers.js';
import { announce, cueFor, emitCue, orbView, syncOrb } from './orb.js';
import { bindOrbGesture, continuousDraft, dictationEndNote } from './dictation.js';
import { captureProcessing, createSoundStore, effectiveSound, isPhone, migrateLegacy, phoneAudioMode, readChoices, readPicture, soundKey, soundSource } from './sound-setup.js';
import { plainText } from './plain-text.js';
import { bindHotkeys } from './hotkeys.js';

// Non-reactive owned resources: Alpine must not proxy AudioNodes or Socket.IO.
let client = null, audio = null, generation = 0;
let candidateCapture = null, candidatePending = null, candidateSending = false;
const candidates = new Map();
let nativePromise = null, nativeBinding = null, dictationPromise = null, dictationHeld = false, dictationEnding = null, dictationWasVoice = false, lastDictationLabel = '', dictationBuffer = null, bufferSettling = null;
let historyTarget = '', historySequence = 0, refreshTimer = null;
let scroller = null, stick = true, settleQueued = false;   // the transcript element and whether the reader follows its end
let capturing = false, lastActivity = 0, noticePending = false, noteTimer = null, lastOrb = null, holdTimer = null;
let draftTurn = '', replyTurn = '';      // the turn the draft belongs to; the turn whose reply was last seen (text or audio)
let playbackFault = '';                   // the last playback failure shown (cleared once audio plays again)
let shownError = null, endedError = '', errorTimer = null;   // the error line: what is shown ({text, kind, turn, source}); why the conversation ended
let abandoned = null;                     // an utterance dropped by muting whose probes the server may still hold (dropUtterance)
let pausedTaps = 0;                       // orb taps (or hotkey presses) since the output was paused
const heardTurns = new Set();             // turns whose assistant words are in the history already: their draft gives way to it
const timings = new Map();                // turn -> monotonic marks (measurement only; no content)
const clock = () => globalThis.performance?.now?.() ?? Date.now();
const mark = (turn, name, at = clock()) => markTurn(timings, turn, name, at);
const noticeSeen = new Set();
const endpoint = '/plugins/_convo/conversation';

// ---- one page, one owner, one voice -----------------------------------------------------------------------------------------
// This page's stable id (kept for the tab's life, a reload included): the server hands a conversation whose connection dropped (a
// phone's sleep, a network change) back to the page that started it, within its grace (about 20 s), with the same microphone.
// The device label is the browser and the platform only ("Chrome on Android"), never anything personal.
// A duplicated tab copies sessionStorage, so the id alone would make two pages one owner: beside it the page keeps a "live" mark
// (`PAGE_LIVE_KEY`), set while this page uses the id and removed when it is left (`pagehide`, so a reload keeps the id). A page that
// finds its stored id still marked live is a copy of a page that is still open, and takes an id of its own.
const PAGE_KEY = 'convo.page.v1';
const PAGE_LIVE_KEY = 'convo.page.live.v1';
let pageIdentity = '';
export function pageId() {
    if (pageIdentity) return pageIdentity;
    const storage = (() => { try { return globalThis.sessionStorage || null; } catch { return null; } })();
    let stored = '', live = '';
    try { stored = storage?.getItem(PAGE_KEY) || ''; live = storage?.getItem(PAGE_LIVE_KEY) || ''; } catch { stored = ''; }
    if (!/^[A-Za-z0-9-]{8,64}$/.test(stored) || live === stored) {
        stored = (globalThis.crypto?.randomUUID?.() || String(Math.random()).slice(2) + String(Date.now())).slice(0, 64);
    }
    pageIdentity = stored;
    const mark = () => { try { storage?.setItem(PAGE_KEY, pageIdentity); storage?.setItem(PAGE_LIVE_KEY, pageIdentity); } catch { /* memory only */ } };
    mark();
    try {
        globalThis.addEventListener?.('pagehide', () => { try { storage?.removeItem(PAGE_LIVE_KEY); } catch {} });
        globalThis.addEventListener?.('pageshow', event => { if (event?.persisted) mark(); });   // back from the page cache
    } catch { /* no page events: the mark stays, a later copy of this tab still takes its own id */ }
    return pageIdentity;
}
export function deviceName(nav = globalThis.navigator) {
    let ua = '', platform = '', mobile = false;
    try { ua = String(nav?.userAgent || ''); platform = String(nav?.userAgentData?.platform || nav?.platform || ''); mobile = !!nav?.userAgentData?.mobile; } catch {}
    const browser = /Electron\//.test(ua) ? 'Agent Zero app' : /Edg\//.test(ua) ? 'Edge' : /OPR\//.test(ua) ? 'Opera' : /Firefox\//.test(ua) ? 'Firefox'
        : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'Browser';
    const system = /Android/i.test(ua) ? 'Android' : /iPhone|iPad|iPod/i.test(ua) ? 'iOS' : /Windows/i.test(ua + platform) ? 'Windows'
        : /Mac/i.test(ua + platform) ? 'macOS' : /CrOS/i.test(ua) ? 'ChromeOS' : /Linux/i.test(ua + platform) ? 'Linux' : '';
    return (browser + (system ? ' on ' + system : '') + (mobile && !/Android|iOS/.test(system) ? ' (mobile)' : '')).slice(0, 60);
}
export const REATTACH_GRACE_MS = 18000;   // the page gives up a little before the server (20 s) does
export const REATTACH_RETRY_MS = 1000;
let reattaching = null;                    // {deadline} while the page re-attaches to its conversation after a dropped connection
let audioMode = { mode: '', source: 'desktop', reason: '', labels: [] };   // the phone audio mode fixed at this conversation's start
const echoByMode = new Map();             // the learned echo path per phone audio mode ('' on a desktop), kept across conversations
const pageAudio = () => { try { return AudioSession.page?.() || null; } catch { return null; } };
// Agent Zero's own read-aloud is skipped on the page that owns a running conversation (one voice), through this in-memory hook on the
// host speech store; the saved Speech preference is never touched. An explicit "speak" button press still speaks.
const GUARD = Symbol.for('convo.speechGuard');
export function guardSpeech(host, active) {
    if (!host || typeof host.speakStream !== 'function') return null;
    if (host[GUARD]) return host[GUARD];
    const guard = { skipped: 0, explicit: 0, active };
    const stream = host.speakStream, speak = host.speak;
    host.speakStream = function (id, text, finished) {
        let skip = false;
        try { skip = guard.explicit === 0 && !!guard.active(); } catch {}
        if (skip) { guard.skipped++; return Promise.resolve(); }
        return stream.call(this, id, text, finished);
    };
    if (typeof speak === 'function') host.speak = function (text) {
        guard.explicit++;                          // speak() asks speakStream synchronously: only that call passes
        try { return speak.call(this, text); } finally { guard.explicit--; }
    };
    try { Object.defineProperty(host, GUARD, { value: guard, configurable: true }); } catch { host[GUARD] = guard; }
    return guard;
}
// Device names for the phone audio mode (enumerateDevices opens nothing; names appear once the microphone was allowed).
async function listDevices() {
    try { const media = globalThis.navigator?.mediaDevices; return media?.enumerateDevices ? await media.enumerateDevices() : []; } catch { return []; }
}
// The chosen output device went away (Convo plays to the default) or came back: said once each way.
function outputNotice({ message } = {}) {
    if (!message) return;
    store.outputMessage = String(message).slice(0, 200);
    globalThis.justToast?.(store.outputMessage, 'info');
}
// The sound drawer re-reads its device values (the conversation's real microphone and mode are known now).
function refreshDrawer() { try { sound?.refresh?.(); } catch { /* the drawer is optional */ } }
let speechGuard = null, guardRef = null;
function guardHostSpeech() {
    if (speechGuard) return speechGuard;
    speechGuard = import('/components/chat/speech/speech-store.js')
        .then(module => (guardRef = guardSpeech(module?.store, () => !!store.session)))
        .catch(() => null);
    return speechGuard;
}
let lastMeterPaint = 0;                  // the meter is written to the store at most every METER_MS (visuals only)
const METER_MS = 66;
let recording = null;                     // Record raw and sent: waiting for the next completed utterance ({resolve, timer})
let clipCapture = null;                   // the utterance being recorded as a test clip (never a turn)
// Test clips: labels for a decision-model bake-off (developer diagnostics). Saved only when the developer arms the recorder.
export const CLIP_LABELS = ['request', 'statement', 'follow_up', 'acknowledgement', 'unfinished', 'dont_respond', 'noise', 'side_talk', 'over_playback'];
export const CLIP_MAX_SECONDS = 30;
// ---- One conversation across chats ---------------------------------------------------------------------------------------------
// The conversation whose `target` is GLOBAL_SCOPE (the setting `conversation_scope` "global", or voice started with no chat
// selected: the server decides and says so in `session.target`). The chat selected in Agent Zero is then only the default place
// for work: a change of selection is sent as `convo.work` and nothing is stopped, retargeted or rebuilt. `workAsked` is the
// selection the server was last told about (null: nothing yet), so one selection is sent once, whatever the server made of it.
// `jobChats`: the chat name each `job.queued` event gave, for the task list (bounded, not reactive).
let workTimer = null, workSending = false, workAsked = null;
const jobChats = new Map();
function hostChatName(id) {
    if (!id) return '';
    try {
        const selected = getCurrentContext();
        if (selected?.id === id) return String(selected.name || selected.title || '');
        const listed = globalThis.Alpine?.store?.('chats')?.contexts?.find?.(chat => chat?.id === id);
        return String(listed?.name || '');
    } catch { return ''; }
}
const AUDIO_PAUSED = 'Audio paused — click to resume';
const NO_TRANSCRIPT = 'Whisper could not write down that turn, so its words are not remembered for later turns; the conversation is unaffected.';
// The status chip words what the orb shows (orb.js orbView decides the state and its priority; the chip only names it).
// `unavailable` (a server Convo needs does not answer) is the loading state in other words.
const WAITING_FOR_SERVER = 'Waiting for a server';
const MUTED_LABELS = { speaking: 'Muted · speaking', thinking: 'Muted · thinking' };   // what the assistant does meanwhile (orbView `over`)
const LIVE_LABELS = { ready: 'Ready · microphone off', idle: 'Voice idle · development mode', connecting: 'Starting', loading: 'Waiting for a model',
    listening: 'Listening', hearing: 'Hearing you', thinking: 'Thinking', speaking: 'Speaking', muted: 'Muted', 'dictating-latched': 'Dictating', 'dictating-ptt': 'Dictating', error: 'Needs attention' };
const CHIP_STATES = { idle: 'ready', 'dictating-latched': 'dictation', 'dictating-ptt': 'dictation' };
const ACTIVITY_STATES = new Set(['listening', 'thinking']);   // a server activity ("Searching the web…") shows while nothing more pressing does
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
            return nativeBinding = { store, mic: store, device: () => store.getSelectedDevice(), transcribe: '/plugins/_whisper_stt/transcribe' };
        }
        const [{ store }, { store: devices }] = await Promise.all([
            import('/components/chat/speech/speech-store.js'), import('/components/settings/speech/microphone-setting-store.js'),
        ]);
        return nativeBinding = { store, mic: devices, device: () => devices.getSelectedDevice(), transcribe: '/transcribe' };
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
    const envelope = await socket.request('convo.' + event, data, { timeoutMs: event === 'start' || event === 'retarget' ? 50000 : event === 'whisper' ? 25000 : 10000 });
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
// ---- the error line
// An error is shown, then goes away by itself: when things demonstrably work again, else after errorShowMs. What "works again" means
// depends on its kind: 'turn' (one turn's failure: a later turn's words or sound, or a phrase that plays), 'playback' (a phrase that
// plays), 'page' (the same `source` working later). 'ended' (the conversation stopped, was lost or was refused) keeps its words in the
// panel until dismissed or the next start; only the red of the chip and the orb goes after errorShowMs (`errorQuiet`). Every error also
// goes to the log in the diagnostics panel (`problems`).
export const ERROR_SHOW_MS = 12000;
export const PROBLEM_LIMIT = 50;
let problemCount = 0;
function logProblem(message, turn = '') {
    const text = String(message || '').slice(0, 400);
    if (!text) return;
    const at = Date.now(), last = store.problems.at(-1), id = String(turn || '').slice(0, 80);
    if (last && last.message === text) { store.problems = [...store.problems.slice(0, -1), { ...last, turn: id || last.turn, at, count: last.count + 1 }]; return; }
    store.problems = [...store.problems, { id: ++problemCount, at, message: text, turn: id, count: 1 }].slice(-PROBLEM_LIMIT);
}
// The order in which this page first met each turn: what makes one turn later than another (ids are random).
const turnOrder = new Map();
let turnCount = 0;
function orderOf(turn) {
    if (!turn) return 0;
    if (!turnOrder.has(turn)) { turnOrder.set(turn, ++turnCount); while (turnOrder.size > 128) turnOrder.delete(turnOrder.keys().next().value); }
    return turnOrder.get(turn);
}
function fail(error, { kind = 'page', turn = '', source = '' } = {}) {
    const text = error?.message || String(error);
    logProblem(text, turn);
    shownError = { text, kind, order: orderOf(turn || replyTurn), source };
    if (kind === 'ended') endedError = text;
    store.error = text; store.errorQuiet = false;
    clearTimeout(errorTimer);
    errorTimer = setTimeout(fadeError, store.errorShowMs);
    errorTimer?.unref?.();                                          // outside a browser a timer is an object: it keeps nothing alive
    updateOrb();
}
// The time is up: the red goes; the words too, unless they say why the conversation ended.
function fadeError() {
    errorTimer = null;
    if (!shownError || store.error !== shownError.text) return;
    if (shownError.kind === 'ended') { store.errorQuiet = true; updateOrb(); } else clearError();
}
function clearError() {
    clearTimeout(errorTimer); errorTimer = null; shownError = null;
    store.error = endedError; store.errorQuiet = !!endedError;      // why the conversation ended is shown again, no longer red
    updateOrb();
}
function forgetError() { endedError = ''; playbackFault = ''; clearError(); }            // dismissed, or a new start
const showing = kind => !!shownError && store.error === shownError.text && shownError.kind === kind;
// A later turn's reply: an error about an earlier turn is over.
function laterTurn(turn) { const order = orderOf(turn); if (showing('turn') && order > shownError.order) clearError(); }
// A reply's words or sound arrived: what the conversation waited for answers.
function replyWorks(turn) {
    if (store.loading || store.unavailable) { store.loading = ''; store.unavailable = ''; updateOrb(); }
    laterTurn(turn);
    if (turn) replyTurn = turn;
}
// What failed before under this name now worked.
function settled(source) { if (showing('page') && shownError.source === source) clearError(); }
const failTurn = (error, turn = '') => fail(error, { kind: 'turn', turn });
const failEnded = error => fail(error, { kind: 'ended' });
const failSource = (source, error) => fail(error, { source });
// A playback failure is shown like any other, and its words say the session goes on; it clears itself once a phrase plays again.
function playbackError(error) { playbackFault = error?.message || String(error); fail(error, { kind: 'playback' }); }
function playbackRecovered(turn = '') {
    if (playbackFault && store.error === playbackFault) clearError();
    playbackFault = '';
    laterTurn(turn);
}
// A playback acknowledgement that came too late (its reply was replaced meanwhile) is ordinary, not an outage.
const acknowledgeFailed = (error, turn) => { if (error?.kind !== 'stale' && error?.kind !== 'busy') failTurn(error, turn); };
// The output's state (audio.js watchOutput): a context the browser paused and could not resume is said once; the next click or orb
// press resumes it.
function playbackState({ paused } = {}) {
    const next = !!paused;
    if (store.audioPaused === next) return;
    store.audioPaused = next; pausedTaps = 0;
    if (next) globalThis.justToast?.(AUDIO_PAUSED, 'info');
    updateOrb();
}
// The microphone itself (audio.js watchMicrophone): a warning about a narrow-band one, or a track that ended or was muted (Convo
// would otherwise keep "listening" to nothing), shown with Restart (`restartMicrophone`); an unmute clears a mute notice.
function microphoneIssue(issue) {
    const kind = String(issue?.kind || '');
    if (kind === 'unmuted') { if (store.micIssue?.kind === 'muted') store.micIssue = null; return; }
    if (!['warning', 'ended', 'muted'].includes(kind)) return;
    store.micIssue = { kind, message: String(issue.message || '').slice(0, 300), restart: !!issue.restart };
    if (kind !== 'warning') logProblem(store.micIssue.message);
    globalThis.justToast?.(store.micIssue.message, kind === 'warning' ? 'info' : 'warning');
}
// A live `held` notice in words (developer mode only).
function heldNote(event) {
    const reason = String(event?.reason || '').replaceAll('_', ' ').slice(0, 80);
    return event?.decision === 'wait' ? 'Waiting for you to finish' + (reason ? ` (${reason})` : '') : 'Stayed silent' + (reason ? `: ${reason}` : '');
}
// Whisper failed for a turn: the audio conversation is unaffected, so this is a note, never the error state.
function transcriptUnavailable() { logProblem(NO_TRANSCRIPT); store.say(NO_TRANSCRIPT); globalThis.justToast?.(NO_TRANSCRIPT, 'info'); }

// The browser's own Whisper request (Agent Zero's /transcribe), used only while Convo's Whisper worker cannot run on the server:
// one request at a time (a turn arriving meanwhile is skipped; the model's own words stay the record), and Convo stops waiting after
// WHISPER_ABORT_MS. A request Convo stopped waiting for still counts as running until the host answers it, so two never overlap.
export const WHISPER_ABORT_MS = 15000;
let hostWhisper = null;
async function hostTranscribe(audio) {
    if (hostWhisper) return null;
    const binding = await native();
    const pending = hostWhisper = callJsonApi(binding.transcribe, { audio, mime_type: 'audio/wav' });
    pending.then(() => {}, () => {}).finally(() => { if (hostWhisper === pending) hostWhisper = null; });
    let timer;
    const late = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Whisper took too long')), WHISPER_ABORT_MS); });
    try { return await Promise.race([pending, late]); } finally { clearTimeout(timer); }
}
// `transcript.wanted`: the server asks for one replied-to turn's words, after its reply was heard (event.audio: that turn's whole,
// joined utterance, memory only). Failures are a note and a toast, never the conversation's error state.
async function wantedTranscript(event, ids, owner) {
    try {
        const result = await hostTranscribe(event.audio);
        if (!result || ids.session_id !== owner.session?.id || !result.text) return;
        await request('transcript', { ...ids, turn_id: event.turn_id, text: result.text });
        void owner.history();                               // the words are in the journal now: show them
    } catch { transcriptUnavailable(); }
}
// An utterance that was not answered, with the server's reason, for the developer view (an ignored utterance has no journal entry).
function noteNotAnswered(turn, reason) {
    const note = { turn, reason: String(reason).slice(0, 60), at: Date.now() / 1000 };
    store.notAnswered = [...store.notAnswered.filter(item => item.turn !== turn), note].slice(-20);
}

// What the server needs to judge speech over a reply: the confirmed voiced speech the observation holds (voiced_s) and, when the user
// began while the assistant's audio was heard, `overlap` and the phrase they were hearing then with how much of it had played.
const voicedOf = meta => (Number.isFinite(meta?.voiced_s) && meta.voiced_s >= 0 ? meta.voiced_s : undefined);
// The audio's pre-roll (sound kept from before the speech began, audio.js PRE_ROLL_SECONDS): the server subtracts it from "short".
const preRollField = meta => (Number.isFinite(meta?.pre_roll_s) && meta.pre_roll_s > 0 ? { pre_roll_s: meta.pre_roll_s } : {});
// An overlap probe also carries this device's "Interrupt after" (`barge_in_s`, 0.4-2.0): the server's safety yield uses it for that probe.
// The echo guard's latest verdict (state names and numbers only, never audio), sent with each piece of speech so the server's
// journal can explain afterwards why speech during a reply was let through (2026-11-25: the assistant's own voice, through a loud
// headset, was taken for the user's twice, and nothing recorded what the guard had seen).
export function guardVerdict(metric) {
    if (!metric || typeof metric !== 'object') return null;
    const number = value => (typeof value === 'number' && Number.isFinite(value) ? Math.round(value * 1000) / 1000 : undefined);
    const name = value => (typeof value === 'string' ? value.slice(0, 24) : undefined);
    return { mode: name(metric.mode), state: name(metric.acousticState), path: name(metric.candidatePath), reject: name(metric.rejectReason),
        correlation: number(metric.correlation), lag_ms: number(metric.lagMs), independent_ms: number(metric.independentMs),
        echo_ms: number(metric.echoEvidenceMs), playing: metric.playbackActive === true, native_aec: metric.nativeAec === true };
}
function overlapFields(candidate, voiced, final = false) {
    const fields = {};
    if (voiced !== undefined) fields.voiced_s = voiced;
    const guard = guardVerdict(audio?.echoMetric);
    if (guard) fields.guard = guard;
    if (candidate?.overlap) {
        fields.overlap = true; if (candidate.heard) fields.heard = { ...candidate.heard };
        if (!final) fields.barge_in_s = store.soundValues().barge_in_seconds;
    }
    return fields;
}

function beginCandidate(attempt) {
    if (attempt !== generation || !store.session || reattaching) return;   // re-attaching: speech then is not sent (the server waits)
    // A test clip armed in diagnostics takes this utterance instead: no candidate, no probe, no turn.
    if (store.clipArmed) { clipCapture = { label: store.clipArmed, attempt }; store.clipArmed = ''; store.clipNote = 'Recording the test clip…'; return; }
    setHearing(true); lastActivity = Date.now();
    // Told at once, before any audio exists (no audio and no words go with it): a finished task's result never starts over the user.
    void request('speaking', { ...identity() }).catch(() => {});
    candidateCapture = resumeAbandoned(attempt) || { turn_id: crypto.randomUUID(), sequence: 0, attempt, final: false };
    candidates.set(candidateCapture.turn_id, candidateCapture); orderOf(candidateCapture.turn_id);
    mark(candidateCapture.turn_id, 'onset', audio?.boundary?.onsetAt || clock());
    while (candidates.size > 16) candidates.delete(candidates.keys().next().value);
    // Detection is observation only. Never cancel playback here. Talking over the assistant (its audio was heard when this utterance
    // began, or within the echo tail): duck now, and probe at once and then every 0.5 s while the user keeps talking, so the server
    // decides sooner whether this is an interruption, a backchannel or speech to someone else.
    const overlap = audio?.overlapAtOnset?.();
    if (overlap?.overlap) {
        candidateCapture.overlap = true;
        if (overlap.heard?.phrase_id) candidateCapture.heard = { phrase_id: overlap.heard.phrase_id, played_s: overlap.heard.played_s };
        hold(true);
        audio.quicken?.();
    }
}

async function sendCandidate(value, attempt, final, meta = null) {
    if (clipCapture) return;                                  // a test clip is recording: no probe (its end is saved by turn)
    if (attempt !== generation || !store.session || !candidateCapture || reattaching) return;
    const candidate = candidateCapture;
    candidate.sequence++; candidate.final = final;
    if (final) { mark(candidate.turn_id, 'speech_end', audio?.boundary?.lastTurn?.voicedAt || clock()); mark(candidate.turn_id, 'final_sent'); }
    else if (candidate.sequence === 1) mark(candidate.turn_id, 'first_probe_sent');
    if (candidatePending?.final && candidatePending.candidate !== candidate) {
        if (final) { setHearing(false); candidateCapture = null; }
        hold(true);
        failTurn(new Error('A completed turn is still awaiting policy; this newer observation was withheld.'), candidate.turn_id);
        return;
    }
    candidatePending = { candidate, value, sequence: candidate.sequence, final, voiced: voicedOf(meta), lead: preRollField(meta) };
    if (final) candidate.audio = value;
    if (final) { setHearing(false); candidateCapture = null; lastActivity = Date.now(); }
    if (candidateSending) return; // Latest observation replaces an unsent probe.
    candidateSending = true;
    try {
        while (candidatePending) {
            const item = candidatePending; candidatePending = null;
            if (item.candidate.attempt !== generation || !store.session || item.candidate.dropped) continue;
            item.candidate.sentAt ||= Date.now();
            try {
                await request('candidate', { ...identity(), turn_id: item.candidate.turn_id,
                    sequence: item.sequence, final: item.final, audio: item.value, ...overlapFields(item.candidate, item.voiced, item.final), ...(item.lead || {}) });
            } catch (error) {
                if (item.candidate.attempt !== generation || !store.session || item.candidate.dropped) continue;   // ended or muted meanwhile
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
                        failTurn(new Error('Convo was still busy with an earlier turn, so this one was not answered. Please say it again.'), item.candidate.turn_id);
                    }
                    continue;                                                                 // a busy probe is simply replaced by the next one
                }
                failTurn(new Error("Convo couldn't check this speech (" + (error?.message || 'no answer') + '); the reply is turned down and nothing was interrupted.'), item.candidate.turn_id);
            }
        }
    } finally { candidateSending = false; }
}

// ---- mute
// Muting drops the utterance in progress: nothing more of it is sent, and nothing said before it is ever completed as a turn. A completed
// turn that is still waiting to be sent or decided was said before and goes on. A probe of the dropped utterance may be at the server
// already, which then keeps it open until its capture deadline: the server is told (`candidate_cancel`), and should it not know that
// event, the next utterance carries on under the same id (resumeAbandoned), so the user is never answered "busy" for having muted.
export const CAPTURE_GRACE_S = 5;                  // the server's capture deadline: the longest turn and this many seconds
export const ABANDON_ROOM_MS = 10000;              // too little of that deadline left: a new id (the server frees itself meanwhile)
function dropUtterance() {
    const dropped = candidateCapture;
    candidateCapture = null;
    if (candidatePending && !candidatePending.final) candidatePending = null;
    if (clipCapture) { clipCapture = null; store.clipNote = 'Not saved: the microphone was muted.'; }
    setHearing(false);
    if (dropped) {
        dropped.dropped = true; timings.delete(dropped.turn_id);         // kept in `candidates`: an admission on its way still moves the epoch
        if (dropped.sentAt) {
            const mine = abandoned = { turn_id: dropped.turn_id, sequence: dropped.sequence, until: dropped.sentAt + ((Number(store.settings?.max_audio_seconds) || 20) + CAPTURE_GRACE_S) * 1000 };
            request('candidate_cancel', { ...identity(), turn_id: dropped.turn_id })
                .then(result => { if (result?.cancelled && abandoned === mine) abandoned = null; }).catch(() => { /* the next utterance carries on under its id */ });
        }
    }
    if (!candidatePending && ![...candidates.values()].some(candidate => candidate.final)) hold(false, true);
}
function resumeAbandoned(attempt) {
    const held = abandoned; abandoned = null;
    if (!held || held.until - Date.now() < ABANDON_ROOM_MS) return null;
    return { turn_id: held.turn_id, sequence: held.sequence, attempt, final: false, sentAt: held.until - ((Number(store.settings?.max_audio_seconds) || 20) + CAPTURE_GRACE_S) * 1000 };
}
// The microphone follows `micMuted` (muted, and not held open by push-to-talk). `finish`: push-to-talk was let go of, so what was said
// while it was held is completed as a turn instead of dropped.
function applyMute({ finish = false } = {}) {
    const muted = store.micMuted;
    if (muted && !finish) dropUtterance();
    audio?.setMuted?.(muted, { finish });
    if (muted) {
        if (recording) { clearTimeout(recording.timer); recording.resolve(null); recording = null; }     // nothing more is heard for it
        if (candidateCapture) dropUtterance();               // too short to be a turn
        setHearing(false);
        store.inputLevel = 0; store.meter = { level: 0, withheld: false, voiced: false, state: 'muted', playback: !!store.meter.playback };
    }
    updateOrb();
}

// The finished test clip: 16 kHz mono PCM16 WAV (the turn's own upload format), at most CLIP_MAX_SECONDS, saved on the Agent Zero host
// through `clip_save`. Never sent as a turn.
async function saveClip(audioBase64, attempt) {
    const capture = clipCapture; clipCapture = null; setHearing(false);
    if (!capture || attempt !== generation) return;
    const seconds = Math.max(0, (Math.floor(audioBase64.length * 3 / 4) - 44) / 32000);
    if (seconds > CLIP_MAX_SECONDS) { store.clipNote = `Not saved: the clip was ${seconds.toFixed(1)} s, longer than ${CLIP_MAX_SECONDS} s.`; return; }
    try {
        const result = await callJsonApi(endpoint, { action: 'clip_save', label: capture.label, audio: audioBase64, device: deviceLabel() });
        if (!result?.ok) throw new Error(result?.error || 'The clip was not saved.');
        store.clipNote = `Saved a ${seconds.toFixed(1)} s “${capture.label.replaceAll('_', ' ')}” clip.`;
        void store.clipList();
    } catch (error) { store.clipNote = 'Not saved: ' + (error?.message || String(error)); }
}
// A short label for the input device (its name when the browser gives one), for clip metadata only.
function deviceLabel() {
    const device = nativeBinding?.device?.();
    return String(device?.label || (device?.deviceId ? 'microphone ' + String(device.deviceId).slice(0, 8) : 'default microphone')).slice(0, 40);
}
// The meter: raw microphone level before the guard and gate, whether input was withheld, the voicing verdict and the guard's state.
// Written to the store at most every METER_MS; a measurement in progress (Measure room) sees every reading. Never gates capture.
function meterReading(reading) {
    try { store.meterTap?.(Number(reading?.level) || 0, !!reading?.playback); } catch {}
    const now = Date.now();
    if (now - lastMeterPaint < METER_MS) return;
    lastMeterPaint = now;
    store.meter = { level: Number(reading?.level) || 0, withheld: !!reading?.withheld, voiced: !!reading?.voiced, state: String(reading?.state || ''), playback: !!reading?.playback };
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
// `continue`: the user's speech over the reply was a backchannel or a side remark. The decision is made, the reply plays on, and the
// duck lifts 400 ms after the user stops talking (audio.js releaseAfterSpeech) instead of the usual delay.
function releaseAfterSpeech() {
    clearTimeout(holdTimer); holdTimer = null;
    if (!audio?.releaseAfterSpeech?.()) audio?.setPolicyHold(false);
    if (store.policyHold) { store.policyHold = false; updateOrb(); }
}

// The orb is a pure function of store state; cues follow its transitions, so every path to a state cues the same way.
function updateOrb() {
    const view = orbView({ enabled: store.enabled, dictating: store.dictating || !!store.dictationMode, dictationMode: store.dictationMode,
        continuous: !!store.dictationMode && continuousMode(), holding: store.dictationHolding, pressing: store.pressing, capturing: store.capturing, error: store.errorQuiet ? '' : store.error, busy: store.busy, active: store.active,
        state: store.state, hearing: store.hearing, playing: !!(audio?.playing || audio?.pcm?.size || audio?.queue?.length || store.outputLevel > .02),
        devIdle: !!store.settings?.ai_link_development_mode, policyHold: store.policyHold, loading: store.loading, unavailable: store.unavailable, muted: store.micMuted });
    const id = view?.id ?? null;
    if (store.mutedOver !== (view?.over ?? '')) store.mutedOver = view?.over ?? '';
    if (id !== lastOrb) { const cue = cueFor(lastOrb, id); lastOrb = id; if (cue) emitCue(cue); }
    // The panel's status chip mirrors the orb (`live`); while it hears you it also shows the input level, in 5% steps so the
    // chip is not repainted at the metering rate.
    if (store.live !== id) store.live = id;
    const heardLevel = id === 'hearing' ? Math.round(Math.max(0, Math.min(1, Number(store.inputLevel) || 0)) * 20) / 20 : 0;
    if (store.liveLevel !== heardLevel) store.liveLevel = heardLevel;
    // A dictation transition (latched, push-to-talk, released) is also said aloud: the label changed, a screen reader will not read it.
    const spoken = view && id.startsWith('dictating-') ? view.label : '';
    if (spoken !== lastDictationLabel) { lastDictationLabel = spoken; if (spoken) announce(spoken); }
    const mic = typeof document !== 'undefined' ? document.getElementById?.('microphone-button') : null;
    if (mic) syncOrb(mic, view, id === 'speaking' ? store.outputLevel : store.inputLevel);
}

// History entries are keyed by the sequence number of the event they are placed at (a folded turn: its first event).
const entryKey = entry => entry.key ?? entry.seq;
// A draft gives way to the history once its turn's words are there: heard phrases, or in developer mode the generated reply too.
function settleDraft(events) {
    const rejected = !!store.settings?.developer_mode && !!store.settings?.show_rejected_responses;   // only then is generated text in the history
    for (const event of events) if (event.turn && (event.kind === 'assistant.delivered' || (rejected && event.kind === 'assistant.generated'))) heardTurns.add(event.turn);
    while (heardTurns.size > 64) heardTurns.delete(heardTurns.values().next().value);
    if (draftTurn && heardTurns.has(draftTurn) && store.draft) store.draft = '';
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

// ---- Camera (the camera tool) -------------------------------------------------------------------------------------------------
// "Share camera" keeps the stream in this page; the server only learns that one is shared. One frame is taken only when the model
// calls the camera tool (`camera.request`): longest edge SNAPSHOT_EDGE px, JPEG at SNAPSHOT_QUALITY (the standalone fork's values),
// sent once for that reply and never stored.
export const SNAPSHOT_EDGE = 768, SNAPSHOT_QUALITY = 0.7;
let cameraStream = null, cameraVideo = null;
export function snapshotSize(width, height, edge = SNAPSHOT_EDGE) {
    const scale = Math.min(1, edge / Math.max(1, width, height));
    return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
}
function videoFrame(video, edge = SNAPSHOT_EDGE, quality = SNAPSHOT_QUALITY) {
    if (!video || !video.videoWidth || !video.videoHeight) return '';
    const size = snapshotSize(video.videoWidth, video.videoHeight, Math.min(SNAPSHOT_EDGE, Number(edge) || SNAPSHOT_EDGE));
    const canvas = document.createElement('canvas');
    canvas.width = size.width; canvas.height = size.height;
    canvas.getContext('2d')?.drawImage(video, 0, 0, size.width, size.height);
    return canvas.toDataURL('image/jpeg', Math.min(SNAPSHOT_QUALITY, Number(quality) || SNAPSHOT_QUALITY));
}
function releaseCamera() {
    cameraStream?.getTracks?.().forEach(track => track.stop());
    if (cameraVideo) cameraVideo.srcObject = null;
    cameraStream = null; cameraVideo = null;
}
// The edge for one picture: the smaller of what the server asked for and what this browser's Audio & Video setting allows.
export function pictureEdge(asked, chosen) {
    const limit = Number(chosen) > 0 ? Number(chosen) : SNAPSHOT_EDGE;
    return Math.min(Number(asked) > 0 ? Number(asked) : SNAPSHOT_EDGE, limit);
}
async function answerCamera(event) {
    let image = '';
    try { image = videoFrame(cameraVideo, pictureEdge(event.max_edge, readPicture().camera_edge), event.quality); } catch { image = ''; }
    await request('camera_snapshot', { ...identity(), turn_id: event.turn_id, request_id: event.request_id, image }).catch(() => {});
}

// ---- Screen (the screen tool) -------------------------------------------------------------------------------------------------
// "Share screen" is the camera's twin: the browser's own picker (`getDisplayMedia`) chooses a screen, window or tab, the stream stays
// in this page and the server only learns that one is shared. One frame is taken only on the server's `screen.request` (the same
// edge and quality handling as the camera), sent once as `convo.screen_snapshot` and never stored. Sharing ends on the button, when
// the browser's own "Stop sharing" bar ends the track, and when voice stops.
let screenStream = null, screenVideo = null;
function releaseScreen() {
    screenStream?.getTracks?.().forEach(track => track.stop());
    if (screenVideo) screenVideo.srcObject = null;
    screenStream = null; screenVideo = null;
}
async function answerScreen(event) {
    let image = '';
    try { image = videoFrame(screenVideo, pictureEdge(event.max_edge, readPicture().screen_edge), event.quality); } catch { image = ''; }
    await request('screen_snapshot', { ...identity(), turn_id: event.turn_id, request_id: event.request_id, image }).catch(() => {});
}

// ---- Asking first (`action.pending`) ------------------------------------------------------------------------------------------
// Convo asks before it goes ahead with something: one card in the panel (label, summary as plain text, Run and Dismiss). A new
// question replaces the old one; the card goes when the server resolves it, after its `seconds`, and when voice stops. The timer
// is not reactive state.
const ACTION_SECONDS = 30, ACTION_MAX_SECONDS = 600;
let actionTimer = null;
function clearAction(id = '') {
    if (id && store.pendingAction?.id !== id) return;                 // an answer about an earlier question leaves the newer card
    clearTimeout(actionTimer); actionTimer = null; store.pendingAction = null;
}
function showAction(event) {
    const id = String(event.id || '');
    if (!id) return;
    clearAction();
    const seconds = Number(event.seconds);
    store.pendingAction = { id, label: String(event.label || 'Go ahead?').slice(0, 80), summary: String(event.summary || '').slice(0, 300), busy: false };
    actionTimer = setTimeout(() => clearAction(id), Math.min(ACTION_MAX_SECONDS, seconds > 0 ? seconds : ACTION_SECONDS) * 1000);
}

// ---- "Open …" (the open tool, `ui.open`) ------------------------------------------------------------------------------------
// One thing in Agent Zero's own interface, through the host's own functions, each guarded (a host may differ): a chat is selected
// with the chat list's `selectChat` (a scheduler task's chat with the task list's `selectTask`), Settings open as the header button
// opens them, the file browser as the sidebar's Files entry does. A page is only a link card in the panel (a page cannot open a
// tab without the user's tap; at most LINK_LIMIT cards): Agent Zero's own browser panel is never navigated from here, because it
// runs with the user's saved sign-ins and is theirs to steer. The answer (`convo.ui_result`) is one fixed plain sentence: never
// an address, a chat's name or an exception's text.
// The server waits three seconds for the answer: a host call may take UI_WAIT_MS of them, the rest is the round trip.
const UI_WAIT_MS = 1500, UI_START_MS = 250, LINK_LIMIT = 3, SETTINGS_MODAL = 'settings/settings.html';
let linkCount = 0;
const hostStore = name => { try { return globalThis.Alpine?.store?.(name) || null; } catch { return null; } };
// A host call's outcome within `ms`: what it returned (`value`), that it threw (`error`), or that it is still going (`waiting`: a
// host modal's promise settles only when the modal closes, so there "still going" means it opened).
function hostCall(work, ms) {
    let pending;
    try { pending = Promise.resolve(work()); } catch (error) { pending = Promise.reject(error); }
    return Promise.race([pending.then(value => ({ value }), error => ({ error })), new Promise(resolve => setTimeout(() => resolve({ waiting: true }), ms))]);
}
// An http(s) address as it may go into a link: no other scheme, no spaces or control characters, no user name in front of the host.
// Parsed without `URL` (not every context has it).
export function webAddress(value) {
    const url = String(value ?? '').trim();
    if (!url || url.length > 2000 || /[\s\u0000-\u001f\u007f]/.test(url)) return '';
    const authority = /^https?:\/\/([^\/?#]+)/i.exec(url)?.[1] || '';
    return authority && !authority.includes('@') ? url : '';
}
function addLink(url, title) {
    const host = /^https?:\/\/([^\/?#]+)/i.exec(url)?.[1] || url;
    const link = { id: 'link-' + (++linkCount), url, title: (String(title ?? '').replace(/\s+/g, ' ').trim() || host).slice(0, 80) };
    store.links = [...store.links.filter(item => item.url !== url), link].slice(-LINK_LIMIT);
}
async function openChat(id, wait) {
    if (!id) return [false, 'No chat was named.'];
    const chats = hostStore('chats'), tasks = hostStore('tasks');
    if (typeof chats?.selectChat !== 'function') return [false, 'The chat list of Agent Zero cannot be reached from this page.'];
    const listed = (list, name) => { try { return typeof list?.contains === 'function' ? !!list.contains(name) : null; } catch { return null; } };
    const chat = listed(chats, id), task = listed(tasks, id);
    if (chat === false && !task) return [false, 'That chat is not in the chat list of Agent Zero.'];
    const outcome = await hostCall(() => (task && !chat && typeof tasks.selectTask === 'function' ? tasks.selectTask(id) : chats.selectChat(id)), wait);
    if (outcome.error) return [false, 'Agent Zero could not select that chat.'];
    if (typeof chats.selected === 'string' && chats.selected !== id) return [false, 'Agent Zero did not switch to that chat.'];
    return [true, 'The chat is selected in Agent Zero.'];
}
async function openHostSettings() {
    const open = typeof globalThis.ensureModalOpen === 'function' ? globalThis.ensureModalOpen : typeof globalThis.openModal === 'function' ? globalThis.openModal : null;
    if (!open) return [false, 'The settings of Agent Zero cannot be opened from this page.'];
    const outcome = await hostCall(() => open(SETTINGS_MODAL), UI_START_MS);            // never awaited to its end: it resolves when the modal closes
    return outcome.error ? [false, 'Agent Zero could not open its settings.'] : [true, 'The settings of Agent Zero are open.'];
}
async function openHostFiles() {
    const input = hostStore('chatInput'), files = hostStore('fileBrowser');
    const open = typeof input?.browseFiles === 'function' ? () => input.browseFiles() : typeof files?.open === 'function' ? () => files.open() : null;
    if (!open) return [false, 'The file browser of Agent Zero cannot be opened from this page.'];
    const outcome = await hostCall(open, UI_START_MS);
    return outcome.error ? [false, 'Agent Zero could not open its file browser.'] : [true, 'The file browser of Agent Zero is open.'];
}
function openPage(target, title) {
    const url = webAddress(target);
    if (!url) return [false, 'Only http and https addresses can be opened.'];
    addLink(url, title);
    return [true, 'A link is shown in the Convo panel.'];
}
// ---- The voice agent's own places: a fixed list, each opened and closed the way its own button does it.
const SETTINGS_TABS = { connections: 'Connections', conversation: 'Conversation', audio: 'Audio & Video', advanced: 'Advanced' };
const SETTINGS_PATH = '/plugins/_convo/webui/settings-modal.html', STUDIO_PATH = '/plugins/_convo/webui/studio.html';
function showPanel() { try { const canvas = hostStore('rightCanvas'); if (typeof canvas?.open === 'function') void canvas.open('convo'); } catch { /* the panel is optional */ } }
async function openVoiceSettings(tab) {
    const outcome = await hostCall(() => store.openSettingsModal(), UI_START_MS);      // resolves when the modal closes: never awaited to its end
    if (outcome.error) return [false, 'The voice settings could not be opened.'];
    const setup = hostStore('convoSetup'), name = SETTINGS_TABS[tab];
    if (name && setup) setup.section = name;
    return [true, name ? `The voice settings are open on ${name}.` : 'The voice settings are open.'];
}
function openSound() {
    const sound = hostStore('convoSound');
    if (typeof sound?.show !== 'function') return [false, 'The sound controls cannot be reached from this page.'];
    showPanel();
    if (!sound.open) sound.show();
    return [true, 'The sound controls are open in the Convo panel.'];
}
async function openVoiceStudio() {
    const outcome = await hostCall(() => store.openStudio(), UI_START_MS);
    return outcome.error ? [false, 'Voice Studio could not be opened.'] : [true, 'Voice Studio is open.'];
}
async function openDiagnostics() {
    const diagnostics = hostStore('convoDiagnostics');
    if (!diagnostics?.available || typeof diagnostics.open !== 'function') return [false, 'The diagnostics are only there with developer mode on.'];
    const outcome = await hostCall(() => diagnostics.open(), UI_START_MS);
    return outcome.error ? [false, 'The diagnostics could not be opened.'] : [true, 'The diagnostics are open.'];
}
async function closeModalAt(path) {
    const modals = await import('/js/modals.js');
    if (typeof modals.closeModal !== 'function') return false;
    if (typeof modals.isModalOpen === 'function' && !modals.isModalOpen(path)) return null;       // it was not open
    await hostCall(() => modals.closeModal(path), UI_START_MS);
    return true;
}
async function closePlace(place) {
    if (place === 'sound') {
        const sound = hostStore('convoSound');
        if (typeof sound?.hide !== 'function') return [false, 'The sound controls cannot be reached from this page.'];
        sound.hide();
        return [true, 'The sound controls are closed.'];
    }
    if (place === 'diagnostics') {
        const diagnostics = hostStore('convoDiagnostics');
        if (typeof diagnostics?.closeWindow !== 'function') return [false, 'The diagnostics cannot be reached from this page.'];
        diagnostics.closeWindow({ focus: false });
        showPanel();
        return [true, 'The diagnostics are closed.'];
    }
    if (place === 'voice_settings') {
        // Unsaved edits are the user's to keep or drop: the settings stay open rather than asking in a browser dialog nobody expects.
        if (hostStore('convoSetup')?.dirty) return [false, 'The voice settings have unsaved changes, so they were left open.'];
        const closed = await closeModalAt(SETTINGS_PATH);
        return closed === false ? [false, 'The voice settings could not be closed from this page.'] : [true, closed === null ? 'The voice settings were not open.' : 'The voice settings are closed.'];
    }
    if (place === 'voice_studio') {
        const closed = await closeModalAt(STUDIO_PATH + '?v=20260911-ready');
        return closed === false ? [false, 'Voice Studio could not be closed from this page.'] : [true, closed === null ? 'Voice Studio was not open.' : 'Voice Studio is closed.'];
    }
    if (place === 'settings') {
        const closed = await closeModalAt(SETTINGS_MODAL);
        return closed === false ? [false, 'The settings of Agent Zero could not be closed from this page.'] : [true, closed === null ? 'The settings of Agent Zero were not open.' : 'The settings of Agent Zero are closed.'];
    }
    return [false, 'Convo cannot close that.'];
}
async function answerOpen(event) {
    const what = String(event.what || ''), target = typeof event.target === 'string' ? event.target.trim() : '';
    let ok = false, detail = 'That could not be opened.';
    try {
        [ok, detail] = what === 'chat' ? await openChat(target, store.uiWaitMs) : what === 'settings' ? await openHostSettings()
            : what === 'files' ? await openHostFiles() : what === 'page' ? openPage(target, event.title)
            : what === 'voice_settings' ? await openVoiceSettings(target) : what === 'sound' ? openSound()
            : what === 'voice_studio' ? await openVoiceStudio() : what === 'diagnostics' ? await openDiagnostics()
            : what === 'close' ? await closePlace(target)
            : [false, 'Convo cannot open that kind of thing.'];
    } catch { ok = false; detail = 'That could not be opened.'; }
    await request('ui_result', { ...identity(), request_id: event.request_id, ok: !!ok, detail: String(detail).slice(0, 160) }).catch(() => {});
}

export const store = createStore('convo', {
    settings: null, settingsRevision: '', session: null, state: 'off', error: '', errorQuiet: false, errorShowMs: ERROR_SHOW_MS, problems: [], loading: '', unavailable: '', activity: '',
    muted: false, talking: false, mutedOver: '',   // the user's mute; push-to-talk held while muted; what the assistant does while muted (the chip)
    mode: 'active', target: '', selected: '', runtime: null,
    events: [], jobs: [], draft: '', dictating: false, panelOpen: false, busy: false,
    dictationMode: '', dictationHolding: false, pressing: false, capturing: false,   // orb gesture: '' | 'latched' | 'ptt'; the press still down; a press heading for the latch time
    moreEarlier: false, loadingEarlier: false, unseen: 0, pinned: true,
    inputLevel: 0, outputLevel: 0, targetName: '', hearing: false, policyHold: false, holdSafetyMs: 8000, note: '',
    live: null, liveLevel: 0,                    // the orb's state id (null while Convo does not own the orb) and the input level while it hears you
    audioPaused: false, micIssue: null, notAnswered: [],         // the output could not be resumed until a click; recent not-answered decisions (developer view)
    meter: { level: 0, withheld: false, voiced: false, state: '' }, meterTap: null,   // raw input meter (sound drawer); Measure room's tap
    build: '', updateAvailable: false,           // the first build this page saw; a later status poll reporting another one offers a reload
    assistantName: '',                           // the history's assistant label (`assistant_name`), "Convo" until the server says
    clipArmed: '', clipNote: '', clips: [],      // test clips (developer diagnostics)
    cameraShared: false, cameraOffered: false, // "Share camera": a stream kept in this page; offered to the model only when it accepts images
    screenShared: false, screenOffered: false, // "Share screen": the same for a screen, window or tab chosen in the browser's own picker
    work: '', workName: '', workDebounceMs: 300, // the conversation across chats: the chat work goes to by default ('' = none) and its title
    pendingAction: null,                         // what Convo asks before going ahead ({id, label, summary, busy}): one card, Run or Dismiss
    links: [], uiWaitMs: UI_WAIT_MS,             // "Open <title>" link cards from the open tool (at most three); how long a host call may take
    busyRetryMs: [250, 500, 1000, 1500, 2000],   // how long a completed turn may wait for a free server before it is reported as not answered
    owner: null, reconnecting: false, movedTo: '', outputMessage: '',   // who runs voice (status `voice_owner`); re-attaching; taken over by; output device notes
    reattachRetryMs: REATTACH_RETRY_MS, reattachGraceMs: REATTACH_GRACE_MS,
    // Voice runs on another page or device (status `voice_owner`): who, since when and in which chat, for "Take over here".
    get ownerElsewhere() { const owner = this.owner; return !!owner && !(this.session && owner.session === this.session.id) && owner.page !== pageId(); },
    get ownerText() {
        const owner = this.owner;
        if (!owner) return '';
        let since = '';
        try { since = owner.since ? new Date(owner.since * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''; } catch {}
        const chat = owner.target === GLOBAL_SCOPE ? 'across chats' : owner.target && owner.target === this.selected ? 'in this chat' : 'in another chat';
        return `Voice is running on ${owner.device || 'another device'}${since ? ` (since ${since}, ${chat})` : ` (${chat})`}.`;
    },
    get statusLabel() {
        if (this.live == null) return this.dictating ? 'Dictating' : this.error && !this.errorQuiet ? 'Needs attention' : ({off:'Ready · microphone off',connecting:'Starting',listening:'Listening',speaking:'Speaking',considering:'Thinking',thinking:'Thinking'}[this.state] || this.state);
        if (this.live === 'muted') return MUTED_LABELS[this.mutedOver] || LIVE_LABELS.muted;
        if (this.live === 'loading' && !this.loading) return WAITING_FOR_SERVER;
        return this.activity && ACTIVITY_STATES.has(this.live) ? this.activity : LIVE_LABELS[this.live] || this.live;
    },
    // What the conversation waits for, in words: a model that loads, or a server that does not answer.
    get waiting() { return this.loading || this.unavailable; },
    // ---- the error line and its log (fail, clearError)
    dismissError() { forgetError(); },
    clearProblems() { this.problems = []; },
    noteProblem(message, turn = '') { logProblem(message, turn); },
    // ---- mute (applyMute): the microphone is off in this page, the conversation and its playback go on. Kept across chat switches and
    // a re-attach, cleared when the conversation ends. Push-to-talk opens the microphone while it is held.
    get micMuted() { return this.muted && !this.talking; },
    setMuted(value) {
        value = !!value;
        if (!this.active || this.muted === value) return false;
        this.muted = value; this.talking = false;
        applyMute();
        if (!this.panelOpen) announce(value ? 'Microphone muted.' : 'Microphone on.');     // the open panel's chip says it
        return true;
    },
    toggleMute() { return this.setMuted(!this.muted); },
    pushToTalk(value) {
        value = !!value;
        if (value ? !this.active || !this.muted || this.talking : !this.talking) return false;
        this.talking = value;
        if (this.active && this.muted) applyMute({ finish: !value });
        return true;
    },
    // ---- hotkeys (hotkeys.js): whether the action did something now; a press that did nothing is left to the page.
    hotkey(action) {
        switch (action) {
            case 'toggle_conversation': void this.toggle(); return true;
            case 'mute': return this.toggleMute();
            case 'push_to_talk': return this.pushToTalk(true);
            case 'dictate': void this.toggleDictation(); return true;
            case 'stop_reply': return this.stopReply();
            case 'open_panel': void this.openPanel(); return true;
            case 'open_settings': void this.openSettingsModal(); return true;
            case 'run_action': case 'dismiss_action':
                if (!this.pendingAction || this.pendingAction.busy) return false;
                void this.answerAction(action === 'run_action'); return true;
            default: return false;
        }
    },
    // The reply being written or spoken ends and the conversation goes on (nothing to stop: false).
    stopReply() {
        if (!this.active || this.busy || reattaching) return false;
        if (!['speaking', 'thinking', 'considering'].includes(this.state) && !audio?.playing && !audio?.pcm?.size && !audio?.queue?.length) return false;
        void this.interrupt();
        return true;
    },
    // The docked canvas where the host has one, else the panel's own modal.
    async openPanel() {
        const canvas = hostStore('rightCanvas');
        try { if (typeof canvas?.open === 'function') { await canvas.open('convo'); return; } } catch { /* the modal then */ }
        try { await this.show(); } catch (error) { failSource('panel', error); }
    },
    get chipState() {
        if (this.live == null) return this.dictating ? 'dictation' : this.error && !this.errorQuiet ? 'error' : this.state === 'off' ? 'ready' : this.state;
        return this.activity && ACTIVITY_STATES.has(this.live) ? 'activity' : CHIP_STATES[this.live] || this.live;
    },
    // The panel's chat line. A chat's own conversation: "Chat · <name>". The conversation across chats: "Working in: <name>" (an
    // untitled chat says so), or "No chat selected".
    get across() { return this.target === GLOBAL_SCOPE; },
    get targetPrefix() { return this.across ? (this.work ? 'Working in: ' : '') : this.target ? 'Chat · ' : ''; },
    get targetLabel() {
        if (this.across) return this.work ? (this.workName || 'an untitled chat') : 'No chat selected';
        return this.targetName || (this.target ? 'Selected chat' : 'No chat selected');
    },
    // The live draft as the history will read once it is heard: no Markdown (plain-text.js, the browser copy of the server's cleaner).
    get draftText() { return plainText(this.draft); },
    get canRetarget() { return !this.across && !!this.selected && this.selected !== this.target; },
    get activeJobs() { return this.jobs.filter(job => ACTIVE_JOB.has(job.status)); },
    // One "You" and one "Convo" entry per turn (view-helpers.js foldTurns); developer mode adds a secondary line and the bookkeeping.
    get visibleEvents() {
        const developer = !!this.settings?.developer_mode;
        return foldTurns(this.events, { developer, notes: developer ? this.notAnswered : [], assistantName: this.assistantName,
            diagnostics: developer && !!this.settings?.show_diagnostic_events, rejected: developer && !!this.settings?.show_rejected_responses });
    },
    // ---- sound per device (sound-setup.js): the input device the host's microphone setting selects, and the output the context plays to.
    // The input is the microphone actually in use (the live track) once a conversation runs, else the one the host's setting selects;
    // the output is the device chosen for that microphone, else where the context plays; on a phone the audio mode is part of it.
    soundInput() {
        let live = '';
        try { live = audio?.inputDeviceId?.() || ''; } catch {}
        return live || nativeBinding?.device?.()?.deviceId || '';
    },
    soundKey() {
        const input = this.soundInput(), chosen = readChoices(input).output;
        return soundKey(input, chosen || audio?.outputInfo?.().sink || 'default', audioMode.mode);
    },
    // Before the one-time move of the old `default|default` values, a key with none of its own reads those (soundSource).
    soundValues() { return effectiveSound(this.settings, soundSource(this.soundKey())); },
    // The drawer changed this microphone's choices (sound-setup.js): noise suppression, auto gain and the phone mode apply at the next
    // start; the output device at once (chooseOutput).
    deviceChoices(choices) { return choices; },
    // Where the sound goes, in words: the chosen device's name, else "System default"; whether the browser can choose at all.
    outputView() {
        const page = pageAudio(), info = page?.snapshot?.() || {}, choices = readChoices(this.soundInput());
        const named = this.outputs?.find?.(device => device.id === (info.missing ? '' : choices.output));
        const label = choices.output && !info.missing ? (named?.label || choices.output_label || 'the saved output') : 'System default';
        return { label, state: String(info.state || ''), can_choose: !!info.can_choose && !isPhone(), missing: !!info.missing, phone: isPhone(),
                 mode: audioMode.mode, mode_source: audioMode.source, mode_reason: audioMode.reason, reopened: info.reopened || 0,
                 ducks: audio?.duck?.engagements || 0, rebuffers: audio?.underruns || 0 };
    },
    outputs: [],
    async listOutputs() { const page = pageAudio(); this.outputs = page?.supportsSink?.() ? await page.outputs() : []; return this.outputs; },
    // The microphones Agent Zero's own speech setting lists, and the one it has chosen. Convo has no microphone setting of its
    // own: choosing one here changes that setting, so dictation and Convo use the same microphone.
    async microphones() {
        try {
            const mic = (await native()).mic;
            if (!(mic.devices || []).length) await mic.loadDevices?.();
            const list = (mic.devices || []).filter(device => device?.deviceId)
                .map(device => ({ id: String(device.deviceId), label: String(device.label || '').slice(0, 80) }));
            return { chosen: String(mic.getSelectedDevice?.()?.deviceId || ''), list };
        } catch { return { chosen: '', list: [] }; }
    },
    async chooseMicrophone(id) {
        try {
            const mic = (await native()).mic;
            if (typeof mic.selectDevice !== 'function' || !(mic.devices || []).some(device => device?.deviceId === id))
                return { ok: false, message: 'That microphone is not available any more.' };
            await mic.selectDevice(id);
            return { ok: true, message: this.active ? 'Used the next time you start a conversation.' : '' };
        } catch (error) { return { ok: false, message: 'The microphone was not changed: ' + (error?.message || String(error)) }; }
    },
    // The cameras this browser can name (names appear once a camera or the microphone was allowed); opens nothing.
    async cameras() {
        return (await listDevices()).filter(device => device.kind === 'videoinput' && device.deviceId)
            .map(device => ({ id: String(device.deviceId), label: String(device.label || '').slice(0, 80) }));
    },
    async chooseOutput(id) {
        const page = pageAudio();
        if (!page) return { ok: false, message: 'This browser cannot choose an output here.' };
        const result = await page.choose(String(id || ''));
        this.outputMessage = result?.message || '';
        return result;
    },
    // Applies sound values to the running capture at once. Echo protection between Adaptive and Strict changes live; to or from Off is
    // another capture path and waits for the next start (the returned note says so).
    applySound(values, key = 'all') {
        if (!audio) return '';
        try { audio.setSound?.(values); } catch {}
        if ((key === 'echo_guard' || key === 'all') && values?.echo_guard && values.echo_guard !== audio.echoGuard) {
            if (audio.echoGuard === 'off' || values.echo_guard === 'off') return 'Echo protection Off changes how the microphone is captured: it applies the next time you start a conversation.';
            audio.setEchoGuard?.(values.echo_guard);
        }
        return '';
    },
    // ---- test clips (developer diagnostics): the next completed utterance is saved as a labelled clip and not sent as a turn.
    armClip(label) {
        if (!CLIP_LABELS.includes(label)) { this.clipNote = 'Choose a label first.'; return false; }
        if (!this.active) { this.clipNote = 'Start a conversation first: the clip is the next thing you say.'; return false; }
        this.clipArmed = label; this.clipNote = `Armed: the next thing you say is saved as “${label.replaceAll('_', ' ')}” and not answered.`;
        return true;
    },
    disarmClip() { this.clipArmed = ''; this.clipNote = ''; },
    async clipList() {
        try { const result = await callJsonApi(endpoint, { action: 'clip_list' }); this.clips = Array.isArray(result?.clips) ? result.clips.slice(0, 500) : []; }
        catch (error) { this.clipNote = 'Clips could not be listed: ' + (error?.message || String(error)); }
        return this.clips;
    },
    async clipDelete(id) {
        try { const result = await callJsonApi(endpoint, { action: 'clip_delete', id: String(id || '') }); if (!result?.ok) throw new Error(result?.error || 'Not deleted.'); }
        catch (error) { this.clipNote = 'Not deleted: ' + (error?.message || String(error)); }
        return this.clipList();
    },
    eventRole(event) { return describeEvent(event, this.jobs).role; },
    eventLabel(event) { return describeEvent(event, this.jobs).label; },
    eventDetail(event) { return this.settings?.developer_mode ? String(event?.detail || '') : ''; },
    syncTargetName() { const selected = getCurrentContext(); if (selected?.id === this.target) this.targetName = selected.name || selected.title || ''; },
    // The conversation the panel shows while voice is off: the one across chats when the setting says so, else the selected chat's own.
    idleTarget() { return this.settings?.conversation_scope === 'global' ? GLOBAL_SCOPE : (getCurrentContextId() || ''); },
    // Where work goes by default in the conversation across chats. Voice off: the selected chat. Voice on: a changed selection is
    // told to the server (`convo.work`), lightly debounced and never while the conversation starts or re-attaches.
    noteWork() {
        if (!this.across) return;
        if (!this.session) { if (!this.busy) { this.work = getCurrentContextId() || ''; this.workName = hostChatName(this.work); } return; }
        if (this.busy || reattaching || this.selected === workAsked) return;
        clearTimeout(workTimer);
        workTimer = setTimeout(() => { void this.sendWork(); }, this.workDebounceMs);
    },
    async sendWork() {
        const chat = getCurrentContextId() || '', session = this.session;
        if (workSending || !session || this.busy || reattaching || !this.across || chat === workAsked) return false;
        workSending = true;
        try {
            const result = await request('work', { ...identity(), chat });
            if (this.session?.id !== session.id) return false;
            workAsked = chat;
            this.work = typeof result.work === 'string' ? result.work : chat; this.workName = String(result.name || '').slice(0, 120);
            settled('work');
            return true;
        } catch (error) {
            // `stale`/`busy` are ordinary and tried again at the next look; any other refusal is said once for this selection.
            if (error?.kind !== 'stale' && error?.kind !== 'busy' && this.session?.id === session.id) { workAsked = chat; failSource('work', error); }
            return false;
        } finally { workSending = false; }
    },
    // Which chat a task went to, for the list in the conversation across chats: the name the server gave when it was queued, else
    // the host chat list's; '' when neither knows (the name is then left out). A chat's own conversation never shows it.
    jobChat(job) { return this.across && job ? (jobChats.get(job.id) || hostChatName(job.target)) : ''; },
    async mountPanel() { this.panelOpen = true; if (!this.active) this.target = this.idleTarget(); this.syncTargetName(); await this.refresh(); await this.history(); },
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
            this.noteBuild(data.build);
            this.settings = data.settings;
            this.settingsRevision = data.settings_revision || '';
            void globalThis.Alpine?.store?.('convoSetup')?.noteRevision?.(this.settingsRevision);   // another editor saved?
            this.runtime = data.runtime || {mode: 'external'};
            this.owner = data.voice_owner && typeof data.voice_owner === 'object' ? { ...data.voice_owner } : null;
            this.selected = getCurrentContextId() || '';
            if (!this.active && !this.busy) { this.target = this.idleTarget(); this.targetName = ''; }
            this.syncTargetName();
            this.noteWork();
            settled('status');
            updateOrb();
            if (!this.enabled && (this.active || this.busy)) await this.stop();
            if (!this.enabled && orbDrafting) void this.endDictation({ cancel: true, reason: 'off', continuous: true });
        } catch (error) {
            // The request never reached the server (a phone asleep, a network change): the conversation is not ended for that; its
            // own connection re-attaches or, past the grace, stops. An answer from the server (the plugin's route gone when it was
            // turned off, any refusal) still stops at once: that stop is a privacy guarantee.
            if (error?.name === 'TypeError' && (this.active || this.busy)) return;
            // Disabled plugins can disappear from the host API registry. Losing
            // status must release capture/playback, not leave an ambient mic open.
            this.settings = null;
            const ended = this.active || this.busy;
            if (ended) await this.stop();
            if (orbDrafting) void this.endDictation({ cancel: true, reason: 'off', continuous: true });
            if (hostDrafting) stopHostDrafting(host, mic => mic.discardConvoDictation());
            if (ended) failEnded(error); else failSource('status', error);
        }
    },
    // The page remembers the first build it saw; a later poll reporting another one shows a non-blocking banner (never a reload).
    noteBuild(build) {
        if (typeof build !== 'string' || !build) return;
        if (!this.build) this.build = build;
        else if (build !== this.build && !this.updateAvailable) this.updateAvailable = true;
    },
    reloadPage() { globalThis.location?.reload?.(); },
    async show() {
        await this.refresh(); this.panelOpen = true;
        if (!this.active) this.target = this.idleTarget();
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
            settled('settings');
            await ensureModalOpen('/plugins/_convo/webui/settings-modal.html', () => this.settingsMayClose());
        } catch (error) { failSource('settings', error); }
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
            if (!preview.changed) { this.say('No legacy settings need migration.'); return; }
            if (!window.confirm('Merge legacy Enhanced Speech settings into Convo? Existing Convo values win; rollback state will be retained.')) return;
            await callJsonApi(endpoint, { action: 'migrate' });
            await this.refresh(); this.say('Migration saved. Close and reopen plugin settings before editing them.');
        } catch (error) { fail(error); }
    },
    // A short, honest message instead of a failed start: development mode keeps browser voice idle on purpose.
    say(message) { this.note = message; clearTimeout(noteTimer); noteTimer = setTimeout(() => { this.note = ''; }, 8000); },
    // The orb's tap and the hotkey. While the output is paused ("Audio paused — click to resume") the first tap resumes it instead of
    // stopping the conversation; a second tap while it is still paused stops as usual, so a dead output never traps the user.
    async toggle() {
        if (this.active && this.audioPaused && pausedTaps++ === 0) { await this.resumeAudio(); return; }
        if (this.active || this.busy) { await this.stop(); return; }
        if (this.settings?.ai_link_development_mode) { this.say('AI Link development mode keeps browser voice idle. Hold the orb to dictate, or leave the mode in Convo settings.'); return; }
        await this.start();
    },
    // `takeover`: the id of the conversation running on another page or device ("Take over here"); the server ends that one with its
    // usual per-connection shutdown and starts this one. Two pages taking over at once: one wins, the other is told who has it.
    async start({ takeover = '' } = {}) {
        if (this.busy || this.active) return;
        const attempt = ++generation; this.busy = true; this.state = 'connecting'; this.movedTo = '';
        forgetError();
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
            // The selected chat, or none: the server decides which conversation this is (`session.target`). With no chat selected,
            // or the setting "global", it is the one across chats and the selected chat is only where work goes.
            const chat = getCurrentContextId() || '';
            this.target = chat ? this.idleTarget() : GLOBAL_SCOPE;
            this.syncTargetName();
            if (this.across) { this.work = chat; this.workName = hostChatName(chat); }
            ownedClient = createNamespacedClient('/ws'); client = ownedClient;
            ownedClient.addHandlers(['plugins/_convo/realtime']);
            await ownedClient.on('convo.disabled', () => { if (client === ownedClient) void this.stop(); });
            // Another page or device took the conversation over: this page stops and never starts it again by itself.
            await ownedClient.on('convo.taken_over', envelope => {
                if (client !== ownedClient) return;
                const by = String(envelope?.data?.device || 'another device').slice(0, 60);
                void this.stop().then(() => { this.movedTo = by; this.say(`Voice moved to ${by}. Press Start here to take it back.`); });
            });
            // A dropped connection (a phone's sleep, a network change) is not the end: the page re-attaches to the same conversation.
            ownedClient.onDisconnect(() => { if (client === ownedClient) void this.reattach(ownedClient); });
            await ownedClient.on('convo.event', envelope => {
                const event = envelope.data;
                if (client === ownedClient && this.session && event?.session_id === this.session.id && event.type === 'candidate.admitted') {
                    if (event.previous_epoch !== this.session.epoch || !candidates.has(event.turn_id)) return;
                    this.session = event.new_session;
                    mark(event.turn_id, 'admitted_recv');
                    // Stopped as audio.interrupt() always did (the playback generation moves on at once); with `fade_ms` the
                    // audio already playing fades to silence over that time first.
                    audio?.interrupt(event.fade_ms); hold(false, true); this.draft = '';
                    mark(event.turn_id, 'interrupt_done');
                    return;
                }
                // What the session waits for (a model a router is loading), what the reply is doing (`activity`, e.g. "Searching
                // the web…", shown as the live status until the next state, text or error) or a note about it: shown while the
                // session is still starting too, before the page knows its id.
                if (client === ownedClient && event?.type === 'notice' && (!this.session || event.session_id === this.session.id)) {
                    // `held` (the assistant chose to stay silent, or waits for more): developer mode only, never shown otherwise.
                    if (event.kind === 'held') { if (this.settings?.developer_mode) this.say(heldNote(event)); return; }
                    if (event.kind === 'loading') { this.loading = String(event.message || '').slice(0, 200); this.unavailable = ''; }
                    // A server Convo needs does not answer: shown as waiting, never as an error, until it answers again (`ready`, or a
                    // reply's words or sound) or the conversation stops.
                    else if (event.kind === 'unavailable') {
                        this.unavailable = String(event.message || 'A server Convo needs does not answer.').slice(0, 200); this.loading = '';
                        logProblem(this.unavailable, event.turn_id);
                    } else if (event.kind === 'ready') { this.loading = ''; this.unavailable = ''; }
                    else if (event.kind === 'activity') this.activity = String(event.message || '').slice(0, 200);
                    else globalThis.justToast?.(String(event.message || ''), 'info');
                    updateOrb();
                    return;
                }
                // Whisper (memory only, once): the server wrote a turn's words down (`transcript.saved`), or, when Convo's own Whisper
                // worker cannot run here, asks this page to have Agent Zero's Whisper do it (`transcript.wanted`, sent only after the
                // reply was heard and never while the host loads or would download a model). Either may arrive after a newer epoch.
                if (client === ownedClient && this.session && event?.session_id === this.session.id && event.type === 'transcript.saved') { void this.history(); return; }
                if (client === ownedClient && this.session && event?.session_id === this.session.id && event.type === 'transcript.wanted') {
                    // The words belong to the turn's own epoch (`turn_epoch`), not the session's current one after a barge-in.
                    const ids = identity();
                    if (Number.isInteger(event.turn_epoch)) ids.epoch = event.turn_epoch;
                    if (this.settings.transcription_enabled && this.mode === 'active' && typeof event.audio === 'string') void wantedTranscript(event, ids, this);
                    return;
                }
                // Asking first and "open …" belong to the conversation, not to one reply: a barge-in since then moved the epoch on.
                if (client === ownedClient && this.session && event?.session_id === this.session.id) {
                    if (event.type === 'action.pending') { showAction(event); return; }
                    if (event.type === 'action.resolved') { if (event.id) clearAction(String(event.id)); return; }
                    if (event.type === 'ui.open') { void answerOpen(event); return; }
                    // The spoken notice of a finished task could not be spoken: it is offered again (the server bounds how often).
                    if (event.type === 'notice_retry') { if (event.job_id) noticeSeen.delete(String(event.job_id)); return; }
                }
                if (client !== ownedClient || !this.session || event?.session_id !== this.session.id || event.epoch !== this.session.epoch) return;
                if (event.type === 'candidate.state') {
                    const candidate = candidates.get(event.turn_id);
                    if (!candidate || event.sequence < candidate.sequence) return;
                    if (candidate.dropped) return;                                          // muted meanwhile: nothing is decided about it any more
                    // A completed turn's decision is final: a withheld or degraded one is simply not answered, so the
                    // assistant is no longer ducked for it (unless a newer utterance is already open). `listen` is still
                    // pending: the reply stays ducked and the hold is renewed (the safety timer still ends it).
                    const terminal = event.final && !['considering', 'listen'].includes(event.state);
                    if (event.state === 'continue') releaseAfterSpeech();   // a backchannel or side remark: the reply plays on
                    // `listen` for a finished utterance: whether it goes on with the conversation is being read; nothing is turned down for it.
                    else if (event.final && event.state === 'listen') { if (!undecided(event.turn_id)) hold(false); }
                    else hold(['considering', 'withhold', 'degraded', 'listen'].includes(event.state) && !(terminal && !undecided(event.turn_id)));
                    if (event.state === 'ignore' && event.reason) noteNotAnswered(event.turn_id, event.reason);
                    if (event.state === 'degraded') failTurn(new Error((event.reason || "Convo's classifier didn't answer") + (terminal ? ', so this turn was not answered.' : '; the reply is turned down until it does.')), event.turn_id);
                    // Whisper is not asked here: the server asks for a turn's words once its reply has been heard, and never for a turn
                    // the model declined (background noise); see `transcript.wanted` above.
                    if (terminal) candidates.delete(event.turn_id);
                    return;
                }
                if (event.type === 'camera.request') { void answerCamera(event); return; }     // the model called the camera tool
                if (event.type === 'screen.request') { void answerScreen(event); return; }     // the model called the screen tool
                if (event.type === 'state' || event.type === 'text.delta' || event.type === 'error') { this.loading = ''; this.activity = ''; }
                if (event.type === 'state') { this.state = event.state; lastActivity = Date.now(); updateOrb(); }
                if (event.type === 'error') { if (audio?.pcm?.size) audio.interrupt(); failTurn(new Error(event.message), event.turn_id); }
                if (event.type === 'text.delta' || event.type === 'audio.begin' || event.type === 'audio.phrase') replyWorks(typeof event.turn_id === 'string' ? event.turn_id : '');
                if (event.type === 'text.delta') {
                    // The draft follows one turn; once that turn's words are in the history, the history shows them instead.
                    const turn = typeof event.turn_id === 'string' ? event.turn_id : '';
                    if (!(turn && heardTurns.has(turn))) {
                        if (turn && turn !== draftTurn) { this.draft = ''; draftTurn = turn; }
                        this.draft += event.text;
                    }
                    updateOrb();
                }
                if (event.type === 'audio.begin') {
                    if (this.policyHold && !undecided('')) hold(false, true);   // a new reply is never born ducked by a decision that already ended
                    this.state = 'speaking'; updateOrb(); mark(event.turn_id, 'audio_event');
                    const acknowledge = status => {
                        if (status === 'started') { mark(event.turn_id, 'source_start'); playbackRecovered(event.turn_id); }
                        if (client === ownedClient && event.session_id === this.session?.id && event.epoch === this.session?.epoch)
                            void request('playback', { ...identity(), turn_id: event.turn_id, phrase_id: event.phrase_id, status }).catch(error => acknowledgeFailed(error, event.turn_id));
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
                        if (event.epoch === this.session?.epoch) void request('playback', { ...identity(), turn_id: event.turn_id, phrase_id: event.phrase_id, status: 'completed' }).catch(error => acknowledgeFailed(error, event.turn_id));
                    }, () => {
                        mark(event.turn_id, 'source_start'); playbackRecovered(event.turn_id);
                        if (event.epoch === this.session?.epoch) void request('playback', { ...identity(), turn_id: event.turn_id, phrase_id: event.phrase_id, status: 'started' }).catch(error => acknowledgeFailed(error, event.turn_id));
                    }, event.phrase_id);
                }
                if (event.type === 'job.queued' && event.job_id && typeof event.chat === 'string' && event.chat) {   // which chat the task went to
                    jobChats.set(String(event.job_id), event.chat.slice(0, 80));
                    while (jobChats.size > 64) jobChats.delete(jobChats.keys().next().value);
                }
                if (event.type === 'job.queued' || (event.type === 'state' && event.state === 'listening')) void this.history();
            });
            const response = await request('start', { target: chat, mode: this.mode, page: pageId(), device: deviceName(),
                ...(takeover ? { takeover: String(takeover) } : {}) }, ownedClient);
            if (attempt !== generation) { await request('stop', { session_id: response.session.id }, ownedClient).catch(() => {}); return; }
            this.session = response.session; this.owner = null; this.muted = false; this.talking = false;
            // The server says which conversation it is; a session without a target (an older server) is the chat's own, as before.
            if (typeof response.session?.target === 'string' && response.session.target && response.session.target !== this.target) {
                this.target = response.session.target; this.targetName = ''; this.syncTargetName();
            }
            // The server says which chat work goes to (it drops a selected chat that no longer exists).
            if (this.across) {
                this.work = typeof response.work === 'string' ? response.work : chat;
                this.workName = response.work ? String(response.work_name || '').slice(0, 120) || hostChatName(this.work) : hostChatName(this.work);
                workAsked = chat;
            }
            if (this.cameraShared) void this.syncCamera();
            if (this.screenShared) void this.syncScreen();
            // The phone audio mode is fixed now, for the whole conversation (changing it means reopening the microphone, which itself
            // moves the phone between its call and media streams). Device names only: nothing is opened to find them.
            const choices = readChoices(this.soundInput());
            audioMode = phoneAudioMode({ phone: isPhone(), devices: await listDevices(), override: choices.phone_mode });
            const sound = this.soundValues(), key = this.soundKey();
            ownedAudio = new AudioSession({ muted: this.micMuted, maximum: this.settings.max_audio_seconds, deviceId: binding.device()?.deviceId, echoGuard: sound.echo_guard, sound, onMeter: meterReading, onMicrophone: microphoneIssue,
                echoState: echoByMode.get(audioMode.mode) ?? null, page: pageAudio(), processing: captureProcessing(audioMode.mode, choices), onOutput: outputNotice,
                onStart: () => beginCandidate(attempt),
                onProbe: (value, meta) => { if (!this.settings.text_input_required) void sendCandidate(value, attempt, false, meta); },
                onTurn: (value, meta) => { if (attempt === generation) void this.turn(value, attempt, meta); },
                onError: playbackError, onLevel: level, onPlaybackState: playbackState, onEchoMetric: m => { try { taps.echo?.(m) } catch {} } });
            audio = ownedAudio;
            await ownedAudio.start();
            if (attempt !== generation) return;
            void guardHostSpeech();
            // The microphone actually in use may differ from the one the host setting names: its own values apply now, and the drawer
            // shows them (the key it edits is the one the conversation reads).
            if (migrateLegacy(this.soundKey()) || this.soundKey() !== key) this.applySound(this.soundValues(), 'all');
            const chosen = readChoices(this.soundInput()).output;
            if (chosen) void this.chooseOutput(chosen);
            refreshDrawer();
            this.state = 'listening'; updateOrb(); await this.history();
        } catch (error) { if (attempt === generation) { failEnded(error); await this.stop(); } }
        finally {
            if (attempt !== generation) { ownedAudio?.close(); ownedClient?.disconnect(); }
            else { this.busy = false; updateOrb(); }
        }
    },
    async stop() {
        this.stopCamera(false); this.stopScreen(false); clearAction(); this.links = [];
        clearTimeout(workTimer); workTimer = null; workAsked = null;
        generation++; clipCapture = null; this.clipArmed = ''; this.meter = { level: 0, withheld: false, voiced: false, state: '' }; this.busy = false; this.state = 'off'; this.loading = ''; this.unavailable = ''; this.activity = ''; this.audioPaused = false; this.micIssue = null; draftTurn = ''; replyTurn = ''; playbackFault = '';
        this.muted = false; this.talking = false; abandoned = null;
        setHearing(false); this.policyHold = false; clearTimeout(holdTimer); holdTimer = null;
        candidateCapture = null; candidatePending = null; candidates.clear(); reattaching = null; this.reconnecting = false;
        if (recording) { clearTimeout(recording.timer); recording.resolve(null); recording = null; }
        const current = this.session, socket = client;
        try { if (audio?.echoSnapshot?.()) echoByMode.set(audioMode.mode, audio.echoSnapshot()); } catch {}   // the room is learned once per mode
        this.session = null; client = null; audio?.close(); audio = null; this.draft = '';
        if (current && socket) await request('stop', { session_id: current.id }, socket).catch(() => {});
        socket?.disconnect();
        updateOrb();
    },
    // The connection dropped while a conversation ran. Socket.IO reconnects by itself; the page asks the server for its conversation
    // back (`attach`: the same session, this page's id) every reattachRetryMs until the grace ends. Meanwhile the microphone stays
    // open but nothing is sent, and what was playing stops (the server ended that reply). The server answering that it no longer has
    // the conversation (Convo turned off, a restart, its grace over, another device took over) stops at once, as does the grace ending.
    async reattach(socket) {
        if (client !== socket || !this.session || reattaching) return false;
        const attempt = generation, session = this.session;
        reattaching = { deadline: Date.now() + this.reattachGraceMs };
        this.reconnecting = true; audio?.interrupt?.(); hold(false, true); setHearing(false);
        candidateCapture = null; candidatePending = null; abandoned = null; this.draft = '';
        this.say('Connection lost — reconnecting to the conversation…'); updateOrb();
        while (attempt === generation && client === socket && reattaching && Date.now() < reattaching.deadline) {
            try {
                const result = await request('attach', { session_id: session.id, epoch: session.epoch, page: pageId() }, socket);
                if (attempt !== generation || client !== socket) return false;
                reattaching = null; this.reconnecting = false; this.session = result.session; this.state = 'listening';
                this.say('Reconnected: the conversation goes on.'); updateOrb(); void this.history();
                return true;
            } catch (error) {
                if (attempt !== generation || client !== socket) return false;
                if (error?.kind) {                                   // the server answered: it no longer has this conversation
                    reattaching = null; failEnded(new Error('The conversation ended while the connection was down (' + error.message + ').'));
                    await this.stop(); return false;
                }
            }
            await new Promise(resolve => setTimeout(resolve, this.reattachRetryMs));
        }
        if (attempt === generation && client === socket) {
            reattaching = null; failEnded(new Error('Voice disconnected and could not reconnect in time. Jobs continue in their target chats.'));
            await this.stop();
        }
        return false;
    },
    // "Take over here": the conversation running on another page or device ends there (its usual shutdown) and starts here.
    async takeOver() {
        const owner = this.owner;
        if (!owner?.session || this.active || this.busy) return false;
        await this.start({ takeover: owner.session });
        return this.active;
    },
    // ---- developer: Record raw and sent. Only on press, during a conversation: the next completed utterance is saved twice in Convo's
    // data folder on the Agent Zero host (`recording_save`): the microphone before echo protection and the gate (a copy of the
    // capture's first input, no second microphone) and what Convo sent for it, each with its level, the withheld or gated stretches
    // and the clipped start. The utterance itself is answered as usual. Listed and deletable; nothing else is ever stored.
    async recordRawAndSent({ timeoutMs = 60000 } = {}) {
        if (!this.settings?.developer_mode) return 'Record raw and sent is a developer tool: turn on developer mode first.';
        if (!audio || !this.session) return 'Start a conversation first: the recording is the next thing you say.';
        if (recording) return 'Already recording: say something.';
        const owned = audio;
        let started = false;
        try { started = await owned.startRawTap?.(); } catch (error) { return 'The recording could not start: ' + (error?.message || String(error)); }
        if (!started) return 'The recording could not start.';
        this.say('Recording: the next thing you say is saved raw and as sent.');
        const sent = await new Promise(resolve => { recording = { resolve, timer: setTimeout(() => resolve(null), timeoutMs) }; });
        clearTimeout(recording?.timer); recording = null;
        const raw = owned.takeRawTap?.();
        if (!sent) return 'Nothing was recorded (no utterance finished, or the conversation stopped).';
        if (!raw?.audio) return 'The microphone copy was empty, so nothing was saved.';
        try {
            const result = await callJsonApi(endpoint, { action: 'recording_save', raw: raw.audio, sent: sent.value, pre_roll_s: Number(sent.meta?.pre_roll_s) || 0,
                device: deviceName(), mode: audioMode.mode || '' });
            if (!result?.ok) throw new Error(result?.error || 'Not saved.');
            const a = result.analysis || {}, stretches = Array.isArray(a.silenced) ? a.silenced.length : 0;
            return `Saved: raw ${Number(a.raw_s || 0).toFixed(1)} s, sent ${Number(a.sent_s || 0).toFixed(1)} s` +
                (Number.isFinite(a.clipped_start_ms) ? `, clipped start ${Math.round(a.clipped_start_ms)} ms` : '') +
                `, ${stretches} withheld or gated stretch${stretches === 1 ? '' : 'es'}.`;
        } catch (error) { return 'Not saved: ' + (error?.message || String(error)); }
    },
    async recordingList() {
        try { const result = await callJsonApi(endpoint, { action: 'recording_list' }); return Array.isArray(result?.recordings) ? result.recordings.slice(0, 200) : []; }
        catch { return []; }
    },
    async recordingDelete(id) {
        try { return !!(await callJsonApi(endpoint, { action: 'recording_delete', id: String(id || '') }))?.ok; } catch { return false; }
    },
    // "Share camera": ask for the camera (the browser's own prompt), keep the stream here, tell the session.
    async shareCamera() {
        if (this.cameraShared) return;
        try {
            const media = globalThis.navigator?.mediaDevices;
            if (!media?.getUserMedia) throw new Error('This browser cannot share a camera here.');
            // The camera chosen under Audio & Video; one that is gone (unplugged, another browser profile) falls back to the default.
            const size = { width: { ideal: 1280 }, height: { ideal: 720 } }, chosen = readPicture().camera;
            let stream = null;
            if (chosen) stream = await media.getUserMedia({ video: { ...size, deviceId: { exact: chosen } }, audio: false }).catch(error => {
                if (error?.name === 'OverconstrainedError' || error?.name === 'NotFoundError') return null;
                throw error;
            });
            if (!stream) stream = await media.getUserMedia({ video: size, audio: false });
            const video = document.createElement('video');
            video.muted = true; video.playsInline = true; video.srcObject = stream;
            await video.play?.().catch(() => {});
            cameraStream = stream; cameraVideo = video; this.cameraShared = true; settled('camera');
            stream.getVideoTracks?.()[0]?.addEventListener?.('ended', () => this.stopCamera());
            await this.syncCamera();
        } catch (error) { releaseCamera(); this.cameraShared = false; failSource('camera', error); }
    },
    stopCamera(tell = true) {
        const was = this.cameraShared;
        releaseCamera(); this.cameraShared = false; this.cameraOffered = false;
        if (was && tell && this.session) void request('camera', { ...identity(), shared: false }).catch(() => {});
    },
    async toggleCamera() { if (this.cameraShared) this.stopCamera(); else await this.shareCamera(); },
    async syncCamera() {
        if (!this.session) return;
        try { this.cameraOffered = !!(await request('camera', { ...identity(), shared: this.cameraShared }))?.offered; settled('camera'); }
        catch (error) { failSource('camera', error); }
    },
    get cameraLabel() {
        if (!this.cameraShared) return 'Share camera';
        return this.cameraOffered || !this.session ? 'Stop camera' : 'Stop camera · model does not take images';
    },
    // "Share screen": the browser's own picker (it needs the press that called this), the stream kept here, the session told. The
    // button is hidden where the browser has no `getDisplayMedia` (phones). Closing the picker is a choice, not an error.
    get canShareScreen() { return typeof globalThis.navigator?.mediaDevices?.getDisplayMedia === 'function'; },
    async shareScreen() {
        if (this.screenShared) return;
        try {
            const media = globalThis.navigator?.mediaDevices;
            if (typeof media?.getDisplayMedia !== 'function') throw new Error('This browser cannot share a screen here.');
            const stream = await media.getDisplayMedia({ video: true, audio: false });
            const video = document.createElement('video');
            video.muted = true; video.playsInline = true; video.srcObject = stream;
            await video.play?.().catch(() => {});
            screenStream = stream; screenVideo = video; this.screenShared = true; settled('screen');
            // The browser's own "Stop sharing" bar ends the track: the page follows.
            stream.getVideoTracks?.()[0]?.addEventListener?.('ended', () => { if (screenStream === stream) this.stopScreen(); });
            await this.syncScreen();
        } catch (error) {
            releaseScreen(); this.screenShared = false;
            if (error?.name === 'NotAllowedError' || error?.name === 'AbortError') this.say('Nothing is shared: screen sharing was not started.');
            else failSource('screen', error);
        }
    },
    stopScreen(tell = true) {
        const was = this.screenShared;
        releaseScreen(); this.screenShared = false; this.screenOffered = false;
        if (was && tell && this.session) void request('screen', { ...identity(), shared: false }).catch(() => {});
    },
    async toggleScreen() { if (this.screenShared) this.stopScreen(); else await this.shareScreen(); },
    async syncScreen() {
        if (!this.session) return;
        try { this.screenOffered = !!(await request('screen', { ...identity(), shared: this.screenShared }))?.offered; settled('screen'); }
        catch (error) { failSource('screen', error); }
    },
    get screenLabel() {
        if (!this.screenShared) return 'Share screen';
        return this.screenOffered || !this.session ? 'Stop screen' : 'Stop screen · model does not take images';
    },
    // The pending-action card's buttons: Run (`run: true`) or Dismiss. The card goes once the server took the answer, and also
    // when the server says the question is no longer open (`stale`: answered by voice, replaced or expired meanwhile); any other
    // failed send leaves it for another press (it still expires by itself).
    async answerAction(run) {
        const action = this.pendingAction;
        if (!action || action.busy) return false;
        this.pendingAction = { ...action, busy: true };
        try {
            await request('action', { ...identity(), id: action.id, run: !!run });
            clearAction(action.id); settled('action');
            return true;
        } catch (error) {
            if (error?.kind === 'stale') { clearAction(action.id); return false; }
            if (this.pendingAction?.id === action.id) this.pendingAction = { ...action, busy: false };
            failSource('action', error);
            return false;
        }
    },
    // A link card goes once it was tapped (after the browser followed the link, hence the timer) or dismissed.
    linkTapped(id) { setTimeout(() => this.closeLink(id), 0); },
    closeLink(id) { this.links = this.links.filter(link => link.id !== id); },
    async interrupt() {
        audio?.interrupt(); this.draft = ''; abandoned = null;
        if (this.policyHold && !undecided('')) hold(false, true);
        if (!this.session) return;
        const before = this.session.id;
        try {
            const result = await request('interrupt', identity());
            if (this.session?.id === before) { this.session = result.session; this.state = 'listening'; updateOrb(); }
        } catch (error) {
            if (error?.kind === 'stale' && this.session?.id === before) return;     // the reply was over by itself: nothing left to stop
            failEnded(error); await this.stop();
        }
    },
    async turn(value, attempt, meta = null) {
        if (clipCapture) { await saveClip(value, attempt); return; }
        if (recording && attempt === generation) { const take = recording; recording = null; clearTimeout(take.timer); take.resolve({ value, meta }); }
        setHearing(false); lastActivity = Date.now();
        if (attempt !== generation || !this.session || reattaching) return;   // re-attaching: speech then is not sent (as sendCandidate)
        if (!this.settings.text_input_required) { await sendCandidate(value, attempt, true, meta); return; }
        const candidate = candidateCapture;
        candidateCapture = null; // Text-only capability remains explicit, never substitutes for direct audio.
        const turn_id = candidate?.turn_id || crypto.randomUUID(); const ids = identity();
        try {
            // A text-only conversation model needs the words: here a failed transcription loses the turn, so it is an error. Convo's
            // Whisper worker writes them down (waiting, bounded, for a model load); only where it cannot run, Agent Zero's own does.
            let result = await request('whisper', { ...ids, turn_id, audio: value });
            if (result?.by === 'browser') {
                result = await hostTranscribe(value);
                if (!result) throw new Error('Whisper is still writing down an earlier turn, so this one was not sent.');
            }
            if (result?.unavailable && !result?.text) throw new Error('Whisper could not write down that turn (' + result.unavailable + '), so it was not sent.');
            const transcript = result?.text || '';
            if (attempt !== generation || ids.epoch !== this.session?.epoch || reattaching) return;
            if (!candidate) return;
            candidate.sequence++; candidate.final = true;
            await request('candidate', { ...ids, turn_id, sequence: candidate.sequence, final: true, transcript, ...overlapFields(candidate, voicedOf(meta), true) });
        } catch (error) { failTurn(error, turn_id); }
    },
    // Advanced synthesis controls (synthesis-setup.js): a profile and overrides for this voice session, from its next reply; nothing
    // is saved. Resolves to the server's answer ({ok, effective}); a refusal throws with the server's words.
    // `{ read: true }` only reports what the conversation speaks with; `pending` in the answer means "applies when ready" (retry).
    async tune(profile_id, overrides, { read = false } = {}) {
        if (!this.session) throw new Error('Start a conversation first: these values apply to a running voice session.');
        try {
            const result = await request('tune', { ...identity(), profile_id: String(profile_id || ''), overrides: { ...(overrides || {}) }, ...(read ? { read: true } : {}) });
            if (!read) settled('tune');
            return result;
        } catch (error) { if (!read) logProblem(error?.message || String(error)); throw error; }     // the voice row shows it; the log keeps it
    },
    // The output could not be resumed by itself (the notice says "Audio paused — click to resume"); the button asks again.
    async resumeAudio() { return !!(await audio?.ensureRunning?.()); },
    // The microphone ended or was muted (micIssue): start the conversation again with a fresh microphone.
    async restartMicrophone() { if (this.busy) return; await this.stop(); await this.start(); },
    // "Play test sound" (diagnostics): a short soft tone through the conversation's own output, or a temporary one without a session.
    async playTestSound() {
        try {
            // Without a session the page's one output plays it (no second context: on a phone another one may open another stream).
            const page = audio ? null : pageAudio();
            return (audio ? await audio.testTone?.() : page ? await page.testTone() : await AudioSession.testTone?.()) || { ok: false, message: 'This browser cannot play a test sound here.' };
        } catch (error) { return { ok: false, message: 'The test sound could not be played: ' + (error?.message || String(error)) }; }
    },
    async retarget() {
        if (this.across) { if (this.session) await this.sendWork(); return; }   // across chats a selection is only where work goes
        const target = getCurrentContextId();
        if (!target || target === this.target) return;
        if (!this.session) { this.target = target; this.targetName = ''; this.syncTargetName(); await this.history(); return; }
        const attempt = ++generation;
        setHearing(false); abandoned = null;
        this.busy = true; updateOrb();
        // The page's output keeps running across the switch (no suspend between the old capture and the new one).
        let keep = null; try { keep = pageAudio()?.keep?.() || null; } catch {}
        const ownedAudio = audio; audio = null; const echoState = ownedAudio?.echoSnapshot?.() ?? null; ownedAudio?.close();   // the learned echo path outlives the recreated session
        try {
            const result = await request('retarget', { ...identity(), target });
            if (attempt !== generation) return;
            this.session = result.session; this.target = target; this.targetName = ''; this.syncTargetName(); this.draft = '';
            const binding = await native();
            if (attempt !== generation) return;
            const sound = this.soundValues();
            // The same page output and the same phone audio mode as the conversation it continues (the mode is fixed per conversation).
            const replacement = new AudioSession({ muted: this.micMuted, maximum: this.settings.max_audio_seconds, deviceId: binding.device()?.deviceId, echoGuard: sound.echo_guard, echoState, sound, onMeter: meterReading, onMicrophone: microphoneIssue,
                page: pageAudio(), processing: captureProcessing(audioMode.mode, readChoices(this.soundInput())), onOutput: outputNotice,
                onStart: () => beginCandidate(attempt),
                onProbe: (value, meta) => { if (!this.settings.text_input_required) void sendCandidate(value, attempt, false, meta); },
                onTurn: (value, meta) => { if (attempt === generation) void this.turn(value, attempt, meta); }, onError: playbackError, onLevel: level,
                onPlaybackState: playbackState, onEchoMetric: m => { try { taps.echo?.(m) } catch {} } });
            audio = replacement; await replacement.start();
            if (attempt !== generation) replacement.close();
        } catch (error) { failEnded(error); await this.stop(); }
        finally { keep?.(); if (attempt === generation) { this.busy = false; updateOrb(); } }
        await this.history();
    },
    // The newest page on first read, then only what is newer than what is already known.
    async history() {
        const target = this.target;
        if (!target) return;
        if (historyTarget !== target) {
            historyTarget = target; historySequence = 0; this.events = []; this.jobs = []; this.notAnswered = [];
            this.moreEarlier = false; this.unseen = 0; stick = true; this.pinned = true;
        }
        try {
            const first = historySequence === 0;
            const result = await callJsonApi(endpoint, first ? { action: 'history', target, tail: PAGE } : { action: 'history', target, after: historySequence });
            if (this.target !== target) return;
            const known = new Set(this.events.map(e => e.seq));
            const fresh = result.events.filter(e => !known.has(e.seq));
            const shown = fresh.length && !stick ? new Set(this.visibleEvents.map(entryKey)) : null;   // the entries before this read
            this.events = mergeEvents(this.events, fresh);
            historySequence = Math.max(historySequence, ...result.events.map(e => e.seq));
            if (first) this.moreEarlier = !!result.more;
            this.jobs = result.jobs;
            if (typeof result.assistant_name === 'string' && result.assistant_name.trim()) this.assistantName = result.assistant_name.trim().slice(0, 64);
            if (fresh.length) {
                settleDraft(fresh);
                if (stick) this.queueSettle();
                else this.unseen += this.visibleEvents.filter(entry => !shown.has(entryKey(entry))).length;   // new entries, not new journal rows
            }
            settled('history');
            if (this.active) void this.deliverNotice();
        } catch (error) { failSource('history', error); }
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
            settled('history');
            afterRender(() => restoreAnchor(element, anchor));
        } catch (error) { failSource('history', error); }
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
        // The jobs this conversation's history returned: a chat's own conversation has only that chat's; the one across chats has
        // each job under its real chat (`job.target`), whichever chat it ran in.
        const job = this.jobs.find(job => job.session === this.session.id && (this.across || job.target === this.target) && ['completed','failed','uncertain'].includes(job.status) && !noticeSeen.has(job.id));
        if (!job) return;
        noticePending = true; noticeSeen.add(job.id);               // before the request: a `notice_retry` may follow its answer at once
        try {
            const result = await request('notice', { ...identity(), job_id: job.id });
            if (result.busy) noticeSeen.delete(job.id);
        } catch (error) { failTurn(error); }
        finally { noticePending = false; }
    },
    async cancelJob(job) { try { await callJsonApi(endpoint, { action: 'cancel_job', target: job.target, job_id: job.id }); settled('job'); await this.history(); } catch (error) { failSource('job', error); } },
    async steerJob(job) {
        const text = window.prompt('Explicitly steer this running job:');
        if (text?.trim()) try { await callJsonApi(endpoint, { action: 'steer_job', target: job.target, job_id: job.id, text }); settled('job'); } catch (error) { failSource('job', error); }
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
            this.dictating = true; settled('dictation'); updateOrb();
            watchHostEnd(mic);
            mic.setManual?.(this.dictationHolding);      // the press may have been released while the recorder opened
            if (dictationWasVoice) store.say('Convo voice was paused for dictation. Click the orb to start it again.');   // said once, never queued behind the announcement
        })().catch(error => { this.dictationMode = ''; this.dictationHolding = false; dictationHeld = false; failSource('dictation', error); }).finally(() => { dictationPromise = null; });
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
        })().catch(error => failSource('dictation', error)).finally(() => { dictationEnding = null; });
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

// What playback is doing now, for diagnostics: flags, counts and the output's state and device only, never audio. `active` is false
// without a voice session. `context` is the AudioContext state; `sink` its output device ('default' for the system default).
export function audioSnapshot() {
    if (!audio) return { active: false, ducked: false, toggles: 0, playing: false, queued: 0, pcm: 0, context: '', sink: '' };
    let output = {};
    try { output = audio.outputInfo?.() || {}; } catch { /* observational only */ }
    let page = {};
    try { page = pageAudio()?.snapshot?.() || {}; } catch { /* observational only */ }
    return { active: true, ducked: !!audio.duck?.ducked, toggles: audio.duck?.toggles || 0, playing: !!audio.playing,
        queued: audio.queue?.length || 0, pcm: audio.pcm?.size || 0, context: String(output.state || ''), sink: String(output.sink || ''),
        // How often playback was ducked, the player's re-buffers, and when the page's output reopened (on Android a reopen may move the
        // sound between the call and the media stream); the phone audio mode and why it was chosen.
        ducks: audio.duck?.engagements || 0, rebuffers: audio.underruns || 0, reopened: Number(page.reopened) || 0, last_reopen: Number(page.last_reopen) || 0,
        mode: audioMode.mode, mode_source: audioMode.source, mode_reason: audioMode.reason, read_aloud_skipped: speechGuardCount() };
}
function speechGuardCount() { return guardRef?.skipped || 0; }

// The sound drawer's store (sound-setup.js): per-device sound values over the saved defaults, applied live to this store's capture.
export const sound = createSoundStore(createStore, { convo: () => store, setup: () => globalThis.Alpine?.store?.('convoSetup') });

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
    // The configurable hotkeys (and the older `hotkey`): hotkeys.js decides what a press means, the store acts (store.hotkey).
    bindHotkeys({ store, document, window });
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
