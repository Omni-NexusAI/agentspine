import { callJsonApi } from '/js/api.js';

// The dictation card (dictation-field.html). Methods only (see compose in setup-store.js); `this` is the shared settings store,
// transient view state goes in this.ui.dictation.
//
// Voice-message handling has three choices: Agent Zero's "Send immediately" (send) and "Draft in composer" (draft), and Convo's "Draft
// in composer (continuous)". Agent Zero stores only send|draft, so the third is its draft plus Convo's voice_message_draft. A choice
// applies at once through the Whisper bridge (api/stt_config.py: both values in one settings transaction), outside this editor's
// draft; the answer is then adopted, so this editor's own write never shows as "changed elsewhere" and other unsaved edits stay.
const MODES = ['send', 'draft', 'continuous'];
const LABELS = { send: 'Send immediately', draft: 'Draft in composer', continuous: 'Draft in composer (continuous)' };

export const dictationSetup = {
    // What the select shows, from the saved flag and Agent Zero's own handling (the native Whisper summary). A flag without the
    // host's draft does not apply, so the select shows what really happens.
    messageMode(flag, hostMode) { return hostMode === 'draft' ? (flag === 'continuous' ? 'continuous' : 'draft') : 'send'; },
    messageModeLabel(mode) { return LABELS[mode] || LABELS.send; },
    async applyMessageMode(mode, select = null) {
        const ui = this.ui.dictation;
        const shown = () => this.messageMode(this.config?.voice_message_draft, this.native?.whisper?.summary?.message_mode);
        if (!MODES.includes(mode) || ui.applying || this.busy || !this.config) { if (select) select.value = shown(); return false; }
        ui.applying = true; this.busy = true; this.error = '';      // busy also keeps the status poll from calling this write "elsewhere"
        try {
            const saved = await callJsonApi('/plugins/_convo/stt_config', { message_mode: mode });
            if (!saved?.success) throw new Error(saved?.error || 'Agent Zero could not save its voice-message handling.');
            const host = globalThis.Alpine?.store?.('whisperStt');     // the microphone follows at once, as after its own status refresh
            if (host && saved.config) host.config = { ...(host.config || {}), ...saved.config };
            this.adopt(await this.api('setup_read'));
            void globalThis.Alpine?.store?.('convo')?.refresh?.();    // the orb and the recorder read the new flag now
            this.message = `Voice messages: ${this.messageModeLabel(shown())}. Applied now.`;
            return true;
        } catch (error) { this.fail(error); return false; }
        finally { ui.applying = false; this.busy = false; if (select) select.value = shown(); }
    },
};
