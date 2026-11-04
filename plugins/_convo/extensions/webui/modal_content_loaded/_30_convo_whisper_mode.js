// Agent Zero's Whisper settings offer "Send immediately" and "Draft in composer" under Voice Message Handling. Convo adds a third
// choice, "Draft in composer (continuous)". The host keeps only send|draft, so the choice is stored as the host's draft plus Convo's
// voice_message_draft flag (api/stt_config.py; helpers/config.py maps the host's own save route the same way). This extension only
// adds the option and shows the stored choice; saving stays with the modal's Save (Convo's legacy Whisper save forwards the value).
//
// Scope: the plugin settings modal (host modals.js: callJsExtensions('modal_content_loaded', { modalPath, modal, doc })) while it
// shows Whisper's config.html. One observer on that modal only, released when it closes or turns out to be another plugin's.
// Idempotent: the option and its hint are added once. The stored choice is shown by giving the modal's settings the value the select
// should show and moving the modal's saved snapshot the same way, so the modal does not look edited.
import { callJsonApi } from '/js/api.js';

export const SETTINGS_MODAL = 'components/plugins/plugin-settings.html';
export const WHISPER_SELECT = 'x-component[path="/plugins/_whisper_stt/webui/config.html"] select[x-model="config.message_mode"]';
export const CONTINUOUS = 'continuous';
export const OPTION_LABEL = 'Draft in composer (continuous)';
export const HINT = 'Draft in composer (continuous) keeps listening and adds each phrase you finish to the composer; nothing is sent until you send it or stop the microphone.';

export default async function convoWhisperMode(payload) {
    const element = payload?.modal?.element;
    if (!element || String(payload?.modalPath || '').replace(/^\/+/, '') !== SETTINGS_MODAL || element.__convoWhisperMode) return;
    const state = element.__convoWhisperMode = { flag: undefined, loading: null, shown: null, observer: null, closed: null };
    const Observer = globalThis.MutationObserver;
    if (Observer) {
        // Alpine renders the plugin page and Whisper's fields after this hook; a settings reload toggles x-show (style).
        state.observer = new Observer(() => decorate(element, state));
        state.observer.observe(element, { childList: true, subtree: true, attributes: true, attributeFilter: ['style'] });
    }
    state.closed = () => { if (!element.isConnected) release(element, state); };
    element.ownerDocument?.addEventListener?.('modal-closed', state.closed);
    decorate(element, state);
}

function decorate(element, state) {
    if (!element.isConnected) { release(element, state); return; }
    const context = element.__pluginSettingsContext;
    if (context?.pluginName && context.pluginName !== '_whisper_stt') { release(element, state); return; }     // another plugin's settings
    const select = element.querySelector(WHISPER_SELECT);
    if (!select) return;                                       // Whisper's page has not rendered yet
    if (state.flag === undefined) { load(element, state); return; }
    if (state.flag === null) return;                           // Convo's bridge did not answer: offer nothing it could not store
    addOption(select);
    const settings = context?.settings;
    if (!settings || state.shown === settings) return;         // once per loaded settings object (a scope change loads a new one)
    state.shown = settings;
    if (state.flag === CONTINUOUS && settings.message_mode === 'draft') showStored(context);
}

// Convo's stored flag, read once per modal; null when the bridge cannot answer (then nothing is added).
function load(element, state) {
    if (state.loading) return;
    state.loading = callJsonApi('/plugins/_convo/stt_status', {})
        .then(status => { state.flag = ['single', CONTINUOUS].includes(status?.voice_message_draft) ? status.voice_message_draft : null; })
        .catch(() => { state.flag = null; })
        .then(() => decorate(element, state));
}

function addOption(select) {
    if (select.querySelector(`option[value="${CONTINUOUS}"]`)) return;
    const doc = select.ownerDocument, option = doc.createElement('option');
    option.value = CONTINUOUS;
    option.textContent = OPTION_LABEL;
    const draft = select.querySelector('option[value="draft"]');
    if (draft) draft.after(option); else select.appendChild(option);
    const field = select.closest('.field'), description = field?.querySelector('.field-description');
    if (description && !field.querySelector('[data-convo-continuous-hint]')) {
        const hint = doc.createElement('div');
        hint.className = 'field-description';
        hint.setAttribute('data-convo-continuous-hint', '');
        hint.textContent = HINT;
        description.after(hint);
    }
}

// The settings say what the select shows and what any Save sends; the snapshot moves the same way, so nothing looks edited. Only
// when the loaded value is still the host's draft: a value already changed (by the user or the host) is left alone.
function showStored(context) {
    let snapshot;
    try { snapshot = JSON.parse(context.settingsSnapshotJson || 'null'); } catch { return; }
    if (!snapshot || typeof snapshot !== 'object' || snapshot.message_mode !== 'draft') return;
    snapshot.message_mode = CONTINUOUS;
    context.settings.message_mode = CONTINUOUS;
    context.settingsSnapshotJson = JSON.stringify(snapshot);
}

function release(element, state) {
    state.observer?.disconnect();
    state.observer = null;
    if (state.closed) element.ownerDocument?.removeEventListener?.('modal-closed', state.closed);
    state.closed = null;
}
