// The voice row (synthesis-controls.html, in the sound drawer and the diagnostics Tune card): a preset (tuning profile) and overrides
// for the user's own audio.cpp server. Methods only (see compose in setup-store.js); `this` is the shared settings store and view
// state lives in this.ui.synthesis.
//
// Two things are kept apart. The row's own choice (ui.synthesis.work {profile_id, overrides}) is what you click and type; it never
// touches the Settings draft, so a later Settings save cannot quietly make it the default. A click or edit is sent to the running
// conversation (convo.tune), which resolves it with the speech server at once, outside any reply, and uses it from the next reply:
// the latest choice is queued, never dropped, and during start-up or a chat switch it waits ("applies when the conversation is
// ready") and is retried. "Save as default" checks the choice with the server, then patches roles.tts.tuning alone. The row shows
// what this conversation speaks with and the saved default separately. Nothing is ever created or selected on the server.

export const SYNTHESIS_KEYS = ['temperature', 'top_k', 'top_p', 'repetition_penalty', 'seed', 'max_reference_seconds',
                               'first_block_frames', 'steady_block_frames', 'left_context_frames', 'text_lookahead', 'phrase_flush_ms'];
// The server's bounds (helpers/convo_synthesis.py BOUNDS). The bounds a read returns win once the profiles were read.
export const SYNTHESIS_BOUNDS = { temperature: [0, 2], top_k: [1, 200], top_p: [.05, 1], repetition_penalty: [.8, 2], seed: [0, 4294967295],
    max_reference_seconds: [1, 30], first_block_frames: [1, 300], steady_block_frames: [1, 300], left_context_frames: [1, 300],
    text_lookahead: [16, 512], phrase_flush_ms: [50, 3000] };
// The start-up buffer (the fork's resolvePlaybackPrimeMs, which the server mirrors in audio.begin prime_ms): the built-in profiles use a
// fixed table, any other profile its first block (80 ms a frame), at most PRIME_LIMIT_MS.
export const BUILTIN_PRIME_MS = Object.freeze({ 'low-latency': 800, balanced: 1280, quality: 1760 });
export const PRIME_LIMIT_MS = 2000;
export const FRAME_MS = 80;
export const normaliseProfileId = value => String(value || '').trim().toLowerCase().replace(/[_\s]+/g, '-');
export function primeMsFor(profileId, firstBlockFrames) {
    const id = normaliseProfileId(profileId);
    if (id === 'low' || id === 'lowlatency') return BUILTIN_PRIME_MS['low-latency'];
    if (Object.hasOwn(BUILTIN_PRIME_MS, id)) return BUILTIN_PRIME_MS[id];
    const frames = Number(firstBlockFrames);
    return firstBlockFrames !== undefined && firstBlockFrames !== null && Number.isFinite(frames) ? Math.max(0, Math.min(PRIME_LIMIT_MS, frames * FRAME_MS)) : null;
}
// What the voice model itself defaults to (Qwen3-TTS-12Hz 1.7B and 0.6B Base, generation_config.json), shown beside the presets so
// you can see what you are tuning against. The sub-talker keeps its own defaults in every preset; neither Convo nor a preset sets it.
export const MODEL_DEFAULTS = Object.freeze({ temperature: 0.9, top_k: 50, top_p: 1.0, repetition_penalty: 1.05, max_reference_seconds: null });
export const SAMPLER_KEYS = ['temperature', 'top_k', 'top_p', 'repetition_penalty'];
// The values the voice row shows and marks when overridden. Look-ahead and flush stay stored but are not shown: the speech server
// never uses them for Convo's requests.
export const VOICE_ROW_KEYS = ['temperature', 'top_k', 'top_p', 'repetition_penalty', 'max_reference_seconds', 'first_block_frames', 'steady_block_frames', 'seed'];
export const RETRY_MS = 1000;               // "applies when the conversation is ready": how often a waiting choice is tried again
export const RETRY_LIMIT_MS = 60000;        // and for how long, before it says so
let liveTimer = null;                       // the debounced live apply (module state, never reactive)
let running = null;                         // the one apply loop in flight (a Promise: kept out of the reactive store)
export const DECODER_CONTEXT = 25;          // the decoder context's default; going below it needs the unlock
const WHOLE = new Set(['top_k', 'seed', 'first_block_frames', 'steady_block_frames', 'left_context_frames', 'text_lookahead', 'phrase_flush_ms', 'max_reference_seconds']);
const NATIVE = new Set(['first_block_frames', 'steady_block_frames', 'left_context_frames']);   // only with native PCM playback
const BLOCKS = ['left_context_frames', 'steady_block_frames', 'first_block_frames'];
const LABELS = { temperature: 'Temperature', top_k: 'Top-k', top_p: 'Top-p', repetition_penalty: 'Repetition penalty', seed: 'Seed',
    max_reference_seconds: 'Reference limit (seconds)', first_block_frames: 'First block (80 ms frames)', steady_block_frames: 'Steady block (80 ms frames)',
    left_context_frames: 'Decoder context (80 ms frames)', text_lookahead: 'Look-ahead (characters)', phrase_flush_ms: 'Flush after (ms)' };
const blank = value => value === undefined || value === null || value === '';
const number = value => (typeof value === 'string' ? Number(value) : value);
const fingerprint = (profile, overrides) => JSON.stringify([profile, overrides]);
const plain = value => JSON.parse(JSON.stringify(value ?? null));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const convoStore = () => { try { return globalThis.Alpine?.store?.('convo') || null; } catch { return null; } };
// The conversation is starting or switching chat: a choice waits for it instead of failing.
const starting = convo => !!convo && !convo.session && (convo.state === 'connecting' || !!convo.busy);
const settled = tuning => ({ profile_id: typeof tuning?.profile_id === 'string' ? tuning.profile_id : '',
                             overrides: tuning?.overrides && typeof tuning.overrides === 'object' ? { ...tuning.overrides } : {} });

// Why complete values (a preset's own with overrides) would be refused at speech time (helpers/convo_synthesis.py fit_problem), or ''.
export function fitProblem(values) {
    const whole = key => { const value = number(values?.[key]); return typeof value === 'number' && Number.isFinite(value) ? Math.trunc(value) : null; };
    const first = whole('first_block_frames'), steady = whole('steady_block_frames'), context = whole('left_context_frames');
    if (first !== null && steady !== null && steady < first) return `The first block (${first} frames) is larger than this preset’s steady block (${steady} frames).`;
    if (steady !== null && (context ?? DECODER_CONTEXT) + steady > 300) return 'The decoder context plus the steady block is more than 300 frames for this preset.';
    return '';
}
// The overrides that still fit a preset's own values (convo_synthesis.fitting): switching presets drops a block size that would clash.
export function fitting(profileValues, overrides) {
    const kept = { ...(overrides || {}) };
    for (const key of BLOCKS) if (!blank(kept[key]) && fitProblem({ ...(profileValues || {}), ...kept })) delete kept[key];
    return kept;
}

export const synthesisSetup = {
    // Only for your own audio.cpp server (the server's convo_synthesis.applies); Convo's managed runtime has its own tuning profile.
    synthesisApplies() {
        const tts = this.config?.roles?.tts;
        return !!tts && tts.provider === 'audiocpp' && (tts.connection || 'external') === 'external';
    },
    // The draft's roles.tts.tuning made whole when an older file lacks part of it, and the row's own choice, started from the saved
    // default (written once, before the controls bind to it).
    synthesisInit() {
        const tts = this.config?.roles?.tts;
        if (tts && (!tts.tuning || typeof tts.tuning !== 'object')) tts.tuning = { profile_id: '', overrides: {} };
        if (tts && (!tts.tuning.overrides || typeof tts.tuning.overrides !== 'object')) tts.tuning.overrides = {};
        if (tts && typeof tts.tuning.profile_id !== 'string') tts.tuning.profile_id = '';
        if (!this.ui.synthesis) this.ui.synthesis = {};
        if (!this.ui.synthesis.work) this.ui.synthesis.work = settled(this.synthesisDefault());
        return true;
    },
    // The saved default (what this editor last loaded from the server: the Settings draft never stands in for it).
    synthesisDefault() {
        let saved = null;
        try { saved = this.baseline ? JSON.parse(this.baseline)?.roles?.tts?.tuning : null; } catch { saved = null; }
        return settled(saved || this.config?.roles?.tts?.tuning);
    },
    // The row's own choice: what the controls bind to.
    synthesisWork() {
        if (!this.ui.synthesis?.work) this.synthesisInit();
        return this.ui.synthesis.work;
    },
    synthesisDraft() {
        const work = this.ui.synthesis?.work || this.synthesisDefault();
        return { profile_id: typeof work.profile_id === 'string' ? work.profile_id : '', overrides: work.overrides && typeof work.overrides === 'object' ? work.overrides : {} };
    },
    synthesisLabel(key) { return LABELS[key] || key; },
    synthesisBounds(key) {
        const read = this.ui.synthesis?.read?.bounds?.[key];
        return Array.isArray(read) && read.length === 2 && read.every(Number.isFinite) ? read : SYNTHESIS_BOUNDS[key];
    },
    // Block sizes and the sampler apply only with native PCM playback (without it the server pins Quality's sampler and only the
    // seed and the reference limit count): the server's word once read, the saved switch until then.
    synthesisNativePcm() {
        const read = this.ui.synthesis?.read;
        return read ? read.native_pcm === true : this.config?.roles?.tts?.capabilities?.native_pcm === true;
    },
    // The decoder context sits behind an unlock: chosen here, or (until chosen) unlocked when a value is already there.
    synthesisUnlocked() {
        const chosen = this.ui.synthesis?.unlocked;
        return typeof chosen === 'boolean' ? chosen : !blank(this.synthesisDraft().overrides.left_context_frames);
    },
    synthesisUnlock(on) {
        this.synthesisInit();
        this.ui.synthesis.unlocked = !!on;
        if (!on) delete this.synthesisWork().overrides.left_context_frames;     // locked: the default context again
        return !!on;
    },
    synthesisSetProfile(id) {
        const work = this.synthesisWork();
        work.profile_id = String(id || '');
        return work.profile_id;
    },
    // Options for the profile select: what the server listed, and a chosen or saved one it did not list, kept visible as unverified.
    synthesisProfiles() {
        const listed = (this.ui.synthesis?.read?.profiles || []).filter(item => item && typeof item.id === 'string' && item.id);
        const options = listed.map(item => ({ id: item.id, label: String(item.label || item.id) + (Number.isFinite(item.revision) ? ` · revision ${item.revision}` : '') }));
        const saved = this.synthesisDraft().profile_id;
        if (saved && !options.some(item => item.id === saved)) options.unshift({ id: saved, label: `${saved} (saved; not verified)` });
        return options;
    },
    // Every value set in the row, as numbers (blank = the preset's own value; a blank seed is a new random seed per reply).
    synthesisValues() {
        const overrides = this.synthesisDraft().overrides, result = {};
        for (const key of SYNTHESIS_KEYS) if (!blank(overrides[key])) result[key] = number(overrides[key]);
        if (!this.synthesisUnlocked()) delete result.left_context_frames;
        return result;
    },
    // What a reply would use: block sizes only with native PCM playback.
    synthesisApplied() {
        const values = this.synthesisValues();
        if (!this.synthesisNativePcm()) for (const key of NATIVE) delete values[key];
        return values;
    },
    // The browser's check, before anything is sent: the server's bounds, then the chosen preset's own block sizes. [{key, text}].
    synthesisProblems() {
        const overrides = this.synthesisDraft().overrides, problems = [];
        for (const key of SYNTHESIS_KEYS) {
            if (blank(overrides[key])) continue;
            const value = number(overrides[key]), [low, high] = this.synthesisBounds(key), label = LABELS[key];
            if (typeof value !== 'number' || !Number.isFinite(value)) problems.push({ key, text: `${label} must be a number.` });
            else if (WHOLE.has(key) && !Number.isInteger(value)) problems.push({ key, text: `${label} must be a whole number.` });
            else if (key === 'temperature' && value <= 0) problems.push({ key, text: `${label} must be above 0.` });
            else if (value < low || value > high) problems.push({ key, text: `${label} must be from ${low} to ${high}.` });
        }
        if (problems.length) return problems;
        const value = key => (blank(overrides[key]) ? null : number(overrides[key]));
        const first = value('first_block_frames'), steady = value('steady_block_frames'), context = value('left_context_frames');
        if (first !== null && steady !== null && steady < first) problems.push({ key: 'steady_block_frames', text: 'The steady block must be at least the first block.' });
        if (steady !== null && (context ?? DECODER_CONTEXT) + steady > 300) problems.push({ key: 'left_context_frames', text: 'The decoder context plus the steady block must be at most 300 frames.' });
        if (context !== null && context < DECODER_CONTEXT && !this.synthesisUnlocked()) problems.push({ key: 'left_context_frames', text: `A decoder context below ${DECODER_CONTEXT} frames needs the unlock.` });
        if (problems.length) return problems;
        // Against the chosen preset's own values, once known (a first block above its steady block is refused at speech time).
        const own = this.synthesisProfileValues();
        const clash = Object.keys(own).length ? fitProblem({ ...own, ...this.synthesisApplied() }) : '';
        if (clash) problems.push({ key: 'first_block_frames', text: clash });
        return problems;
    },
    synthesisCan(action) {
        const ui = this.ui.synthesis || {};
        if (!this.config) return false;
        if (action === 'reset') return true;
        if (action === 'load') return !ui.busy;
        if (this.synthesisProblems().length) return false;
        if (action === 'apply') { const convo = convoStore(); return !!convo?.active || starting(convo); }   // a busy row queues it
        return !ui.busy && (action === 'check' || action === 'save');
    },
    // Read the speech server's profiles, its own current selection and its bounds (an explicit request; nothing is changed there).
    async synthesisLoad() {
        this.synthesisInit();
        const ui = this.ui.synthesis;
        if (ui.busy) return false;
        Object.assign(ui, { busy: 'load', error: '', message: '' });
        try {
            const result = await this.api('synthesis_read');
            const profiles = Array.isArray(result.profiles) ? result.profiles.slice(0, 64) : [];
            ui.read = { profiles, selected: result.selected || null, saved: result.saved || null, bounds: result.bounds || {}, native_pcm: result.native_pcm === true };
            const own = result.selected ? ` Its own current selection: ${result.selected.label || result.selected.id}.` : '';
            ui.message = `${profiles.length} profile${profiles.length === 1 ? '' : 's'} on the speech server.${own}`;
            return true;
        } catch (error) { ui.error = error?.message || String(error); return false; }
        finally { ui.busy = ''; }
    },
    // What the server makes of these values (nothing is spoken, saved or applied).
    async synthesisCheck() {
        if (!this.synthesisCan('check')) return false;
        const ui = this.ui.synthesis, { profile_id } = this.synthesisDraft(), overrides = this.synthesisApplied();
        Object.assign(ui, { busy: 'check', error: '', message: '' });
        try {
            const result = await this.api('synthesis_resolve', { profile_id, overrides });
            ui.effective = { source: 'check', values: result.effective || {}, profile_id: result.profile_id || profile_id, revision: result.profile_revision ?? null, for: fingerprint(profile_id, overrides) };
            ui.message = 'The speech server resolved these values. Nothing was spoken, saved or applied.';
            return true;
        } catch (error) { ui.error = error?.message || String(error); return false; }
        finally { ui.busy = ''; }
    },
    // The running conversation uses the row's choice from its next reply; nothing is saved. Queued: resolves once the latest choice
    // was applied (true) or refused (false).
    async synthesisApply() {
        if (!this.synthesisCan('apply')) return false;
        return this.synthesisQueue(0);
    },
    // Send the row's latest choice to the conversation after `delay` ms (edits are debounced, a click is not). A choice made while
    // another is being applied waits for it and is then applied: the latest is never dropped.
    synthesisQueue(delay = 0) {
        this.synthesisInit();
        const ui = this.ui.synthesis;
        clearTimeout(liveTimer); liveTimer = null;
        const convo = convoStore();
        if (!convo?.active && !starting(convo)) {
            ui.queued = null;
            ui.message = 'No conversation is running: this choice applies to the next one only once you Save as default.';
            return Promise.resolve(false);
        }
        if (this.synthesisProblems().length) return Promise.resolve(false);
        const enqueue = () => {
            ui.queued = plain({ profile_id: this.synthesisDraft().profile_id, overrides: this.synthesisApplied() });
            if (!running) running = this.synthesisRun().finally(() => { running = null; });
            return running;
        };
        if (!delay) return enqueue();
        liveTimer = setTimeout(() => { liveTimer = null; void enqueue(); }, delay);
        return Promise.resolve(true);
    },
    // The apply loop: one convo.tune at a time, always the newest queued choice next. A refusal shows at once and the conversation
    // keeps its previous voice. While the conversation starts or switches chat, the choice waits and is retried.
    async synthesisRun() {
        const ui = this.ui.synthesis;
        let last = false, waited = 0;
        ui.applying = true;
        try {
            while (ui.queued) {
                const job = ui.queued, convo = convoStore();
                ui.queued = null;
                let result = null;
                if (convo?.session) {
                    try { result = await convo.tune(job.profile_id, job.overrides); }
                    catch (error) {
                        ui.pending = ''; last = false;
                        ui.error = `${error?.message || String(error)} This conversation keeps its previous voice.`;
                        continue;
                    }
                }
                if (!convo?.session || result?.pending) {
                    if ((starting(convo) || result?.pending) && waited < RETRY_LIMIT_MS) {
                        ui.pending = 'Applies when the conversation is ready.';
                        if (!ui.queued) ui.queued = job;                // a newer choice made meanwhile wins
                        await sleep(RETRY_MS); waited += RETRY_MS;
                        continue;
                    }
                    ui.pending = ''; last = false;
                    ui.message = convo?.session || starting(convo) ? '' : 'No conversation is running: Save as default keeps this choice for the next one.';
                    if (convo?.session || starting(convo)) ui.error = 'The conversation was not ready in time; choose the preset again.';
                    continue;
                }
                waited = 0; last = true;
                ui.pending = ''; ui.error = '';
                ui.session = { session_id: convo.session?.id || '', source: 'own', profile_id: result.profile_id ?? job.profile_id,
                               overrides: plain(result.overrides ?? job.overrides), effective: plain(result.effective || {}), error: result.error || '' };
                ui.effective = { source: 'apply', values: plain(result.effective || {}), profile_id: job.profile_id, revision: result.profile_revision ?? null, for: fingerprint(job.profile_id, job.overrides) };
                ui.message = 'Applies from the next reply in this conversation. Not saved: Save as default keeps it for new conversations.';
            }
        } finally { ui.applying = false; }
        return last;
    },
    // Ask the conversation what it speaks with (its own choice or the saved default, the values and any refresh error).
    async synthesisSessionRead() {
        const convo = convoStore(), ui = this.ui.synthesis;
        if (!convo?.session || typeof convo.tune !== 'function') return false;
        try {
            const result = await convo.tune('', {}, { read: true });
            if (!result || result.pending) return false;
            ui.session = { session_id: convo.session?.id || '', source: result.source || 'default', profile_id: result.profile_id || '',
                           overrides: plain(result.overrides || {}), effective: plain(result.effective || {}), error: result.error || '' };
            if (result.source === 'own' && !ui.queued && !ui.applying) ui.work = settled(ui.session);   // the row follows the conversation
            return true;
        } catch { return false; }
    },
    // What this conversation speaks with, in words, beside the saved default: {conversation, saved, error}.
    synthesisStatus() {
        const ui = this.ui.synthesis || {}, convo = convoStore();
        const name = id => {
            if (!id) { const own = ui.read?.selected; return own ? `the speech server’s own (${own.label || own.id})` : 'the speech server’s own'; }
            const listed = (ui.read?.profiles || []).find(item => item?.id === id);
            return String(listed?.label || id);
        };
        const describe = tuning => {
            const count = Object.keys(tuning?.overrides || {}).filter(key => !blank(tuning.overrides[key])).length;
            return name(tuning?.profile_id || '') + (count ? ` with ${count} change${count === 1 ? '' : 's'}` : '');
        };
        const saved = describe(this.synthesisDefault());
        let conversation = 'No conversation is running.';
        if (ui.pending) conversation = ui.pending;
        else if (convo?.session) {
            const mine = ui.session && ui.session.session_id === convo.session.id ? ui.session : null;
            conversation = mine && mine.source === 'own' ? `This conversation: ${describe(mine)}, from its next reply.` : `This conversation: the saved default (${saved}).`;
        } else if (starting(convo)) conversation = 'The conversation is starting: it uses the saved default.';
        const mine = ui.session && convo?.session && ui.session.session_id === convo.session.id ? ui.session : null;
        return { conversation, saved: `Saved default: ${saved}.`, error: mine?.error || '' };
    },
    // Save the row's choice as the default (just this path, compare-and-set against what this editor loaded), once the speech server
    // has resolved it: a preset it no longer has, or an override that does not fit it, is refused before it can break every reply.
    async synthesisSaveDefault() {
        if (!this.synthesisCan('save')) return false;
        const ui = this.ui.synthesis, tuning = plain({ profile_id: this.synthesisDraft().profile_id, overrides: this.synthesisValues() });
        Object.assign(ui, { busy: 'save', error: '', message: '' });
        try {
            let resolved;
            try { resolved = await this.api('synthesis_resolve', { profile_id: tuning.profile_id, overrides: this.synthesisApplied() }); }
            catch (error) { ui.error = `Not saved: ${error?.message || String(error)}`; return false; }
            const clash = fitProblem({ ...(resolved?.effective || {}), ...this.synthesisApplied() });
            if (clash) { ui.error = `Not saved: ${clash}`; return false; }
            const result = await this.patch({ 'roles.tts.tuning': tuning });
            if (!result) { ui.error = this.error || 'The default could not be saved.'; return false; }
            if ((result.conflicts || []).length) { ui.message = 'Changed elsewhere meanwhile: choose which to keep in the banner above.'; return false; }
            ui.message = 'Saved as the default for new conversations.';
            return true;
        } finally { ui.busy = ''; }
    },
    // Back to the preset's own values: every override cleared and the decoder context locked again (the preset choice stays).
    synthesisReset() {
        this.synthesisInit();
        this.ui.synthesis.work.overrides = {};
        Object.assign(this.ui.synthesis, { unlocked: false, effective: null, error: '', message: 'Reset to the profile’s own values.' });
        return true;
    },
    // ---- opening the row -----------------------------------------------------------------------------------------------------
    // Opening reads the server's profiles and resolves each listed one (read-only) so every field shows the preset's own value,
    // and asks a running conversation what it speaks with.
    async synthesisOpen() {
        this.synthesisInit();
        const ui = this.ui.synthesis;
        if (ui.opened || ui.busy) return false;
        ui.opened = true;
        if (!(await this.synthesisLoad())) { ui.opened = false; return false; }
        const resolved = {};
        for (const profile of (ui.read?.profiles || []).slice(0, 8)) {
            try { const result = await this.api('synthesis_resolve', { profile_id: profile.id, overrides: {} }); resolved[profile.id] = result.effective || {}; }
            catch { /* that profile stays without pre-filled values */ }
        }
        ui.resolved = resolved;
        await this.synthesisSessionRead();
        return true;
    },
    // The profile in effect for the row: its chosen one, else the server's own current selection.
    synthesisActiveId() { return this.synthesisDraft().profile_id || this.ui.synthesis?.read?.selected?.id || ''; },
    synthesisProfileValues(id = this.synthesisActiveId()) { return this.ui.synthesis?.resolved?.[id] || {}; },
    // A field's placeholder: the preset's own value (whole seconds for the reference), or the generic word before it is known.
    synthesisPlaceholder(key) {
        if (key === 'seed') return 'New each reply';
        const value = this.synthesisProfileValues()[key];
        if (value === undefined || value === null || value === '') return 'Profile';
        return String(key === 'max_reference_seconds' ? Math.round(Number(value)) : value);
    },
    // An override that differs from the preset's own value (the dot beside the field).
    synthesisOverridden(key) {
        const value = this.synthesisDraft().overrides[key];
        if (blank(value)) return false;
        const own = this.synthesisProfileValues()[key];
        return own === undefined || own === null || Number(own) !== number(value);
    },
    synthesisAnyOverridden() { return VOICE_ROW_KEYS.some(key => this.synthesisOverridden(key)); },
    // The segmented choice: the server's profiles with their start-up buffer.
    synthesisChoices() {
        const active = this.synthesisActiveId(), overrides = this.synthesisDraft().overrides;
        return (this.ui.synthesis?.read?.profiles || []).filter(item => item && typeof item.id === 'string' && item.id).slice(0, 8).map(item => {
            const values = this.synthesisProfileValues(item.id);
            const first = item.id === active && !blank(overrides.first_block_frames) ? number(overrides.first_block_frames) : values.first_block_frames;
            const prime = primeMsFor(item.id, first);
            return { id: item.id, label: String(item.label || item.id), active: item.id === active, primeMs: prime,
                     buffer: prime === null ? 'start-up buffer not known yet' : `${(prime / 1000).toFixed(2)} s start-up buffer` };
        });
    },
    // The voice model's own defaults beside each preset's values: rows of {key, label, model, presets: [{id, value}]}.
    synthesisDefaultsTable() {
        const presets = (this.ui.synthesis?.read?.profiles || []).filter(item => item && typeof item.id === 'string' && item.id).slice(0, 8);
        const shown = value => (value === undefined || value === null || value === '' ? '–' : String(value));
        return [...SAMPLER_KEYS, 'max_reference_seconds'].map(key => ({
            key, label: key === 'max_reference_seconds' ? 'Reference audio (s)' : LABELS[key],
            model: key === 'max_reference_seconds' ? 'whole clip' : shown(MODEL_DEFAULTS[key]),
            presets: presets.map(item => ({ id: item.id, value: shown(this.synthesisProfileValues(item.id)[key]) })) }));
    },
    synthesisPresetNames() {
        return (this.ui.synthesis?.read?.profiles || []).filter(item => item && typeof item.id === 'string' && item.id).slice(0, 8).map(item => ({ id: item.id, label: String(item.label || item.id) }));
    },
    // The last measured first-audio time (the page's timing marks: end of your speech to audible reply), or "not measured yet".
    synthesisFirstAudio() {
        try {
            const turns = convoStore()?.timingReport?.()?.turns || [];
            const last = [...turns].reverse().find(turn => Number.isFinite(turn.speech_end_to_audible_ms));
            return last ? `last first audio ${(last.speech_end_to_audible_ms / 1000).toFixed(2)} s after you stopped` : 'not measured yet';
        } catch { return 'not measured yet'; }
    },
    // A preset click: chosen for the row, overrides that no longer fit it cleared, and applied to the conversation at once (queued
    // behind one in flight). The Settings draft is not touched.
    synthesisChoose(id) {
        const work = this.synthesisWork();
        work.profile_id = String(id || '');
        const own = this.synthesisProfileValues(work.profile_id || this.ui.synthesis?.read?.selected?.id || '');
        if (Object.keys(own).length) {
            const kept = fitting(own, work.overrides);
            const dropped = Object.keys(work.overrides).filter(key => !(key in kept));
            if (dropped.length) {
                work.overrides = kept;
                this.ui.synthesis.message = `Cleared ${dropped.map(key => LABELS[key] || key).join(', ')}: it did not fit this preset.`;
            }
        }
        void this.synthesisQueue(0);
        return work.profile_id;
    },
    // Back to the preset: every override cleared, then the same live apply as any change.
    synthesisResetToProfile() { this.synthesisReset(); this.synthesisChanged(0); return true; },
    // An edit while a conversation runs is sent to it shortly after the last keystroke and applies from the next reply.
    synthesisChanged(delay = 400) {
        this.synthesisInit();
        const convo = convoStore();
        if (!convo?.active && !starting(convo)) {
            clearTimeout(liveTimer); liveTimer = null;
            this.ui.synthesis.message = 'No conversation is running: Save as default keeps these values for the next one.';
            return false;
        }
        if (this.synthesisProblems().length) return false;
        void this.synthesisQueue(delay);
        return true;
    },
    // A saved tuning, in words (the "Saved default" line).
    synthesisDescribe(profileId, overrides) {
        const count = Object.keys(overrides || {}).filter(key => !blank(overrides[key])).length;
        return (profileId || 'The speech server’s own selection') + (count ? ` · ${count} change${count === 1 ? '' : 's'}` : '');
    },
    // The values the server answered, in words; `stale` once the controls changed since.
    synthesisEffective() {
        const effective = this.ui.synthesis?.effective;
        if (!effective) return null;
        const items = Object.entries(effective.values || {}).filter(([, value]) => value === null || ['number', 'string', 'boolean'].includes(typeof value)).slice(0, 24)
            .map(([key, value]) => ({ key, label: LABELS[key] || key.replaceAll('_', ' '), value: value === null || value === '' ? 'not set' : String(value) }));
        const now = fingerprint(this.synthesisDraft().profile_id, this.synthesisApplied());
        return { source: effective.source, title: effective.source === 'apply' ? 'In effect for this conversation from its next reply' : 'What the speech server resolves for these values',
                 items, stale: effective.for !== now };
    },
};
