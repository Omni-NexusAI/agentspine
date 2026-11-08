import { createStore } from '/js/AlpineStore.js';
import { callJsonApi } from '/js/api.js';
import { closeModal } from '/js/modals.js';
import { store as setup } from '/plugins/_convo/webui/setup-store.js';

const STUDIO_PATH = '/convo-studio/';
let epoch = 0, probe = null, watcher = null;
export const studio = createStore('convoStudio', {
    profiles: [], selectedVoice: '', error: '', message: '', busy: false,
    available: false, blocked: false, frameUrl: STUDIO_PATH,
    get selectedVoiceAvailable() { return this.profiles.some(item => item.voice === this.selectedVoice); },
    get savedVoiceUnavailable() { return !!this.selectedVoice && !this.selectedVoiceAvailable; },
    isBlocked() {
        const convo = globalThis.Alpine?.store('convo');
        return !!(convo?.session || convo?.state === 'connecting' || convo?.enabled === false);
    },
    async open() {
        this.close(); const generation = epoch; this.error = ''; this.message = '';
        await setup.load();
        if (generation !== epoch) return;
        this.selectedVoice = setup.config?.roles?.tts?.voice || '';
        this.blocked = this.isBlocked();
        watcher = setInterval(() => {
            this.blocked = this.isBlocked();
            if (this.blocked) { this.available = false; probe?.abort(); }
        }, 250);
        await this.retry();
    },
    async retry() {
        this.blocked = this.isBlocked();
        if (this.blocked) return;
        const generation = epoch; probe?.abort(); probe = new AbortController();
        try {
            const opened = await callJsonApi('/plugins/_convo/conversation', {action:'studio_open'});
            if (!opened?.ok || generation !== epoch) return;
            // studio_open already establishes the authenticated CSRF session.
            // This is a root WSGI route, not a host API: fetchApi would rewrite
            // it to /api/convo-studio/ and probe the wrong endpoint.
            const response = await fetch(STUDIO_PATH, {method:'GET', credentials:'same-origin',
                cache:'no-store', redirect:'error', signal:probe.signal});
            if (generation !== epoch || this.isBlocked()) return;
            this.available = !!response?.ok;
            this.error = this.available ? '' : 'Studio is unavailable. Check Convo runtime readiness and dependency approval.';
            if (this.available) await this.refresh();
        } catch (error) { if (generation === epoch && error.name !== 'AbortError') this.error = 'Studio connection is unavailable.'; }
    },
    async refresh() {
        if (this.busy) return;
        const generation = epoch; this.busy = true; this.error = '';
        this.selectedVoice = setup.config?.roles?.tts?.voice || '';
        try {
            const result = await callJsonApi('/plugins/_convo/conversation', {action:'studio', operation:'profiles'});
            if (generation !== epoch) return;
            if (!result?.ok) throw new Error(result?.error || 'Profile refresh failed.');
            this.profiles = (result.data?.voices || []).filter(item => (item.task_type || item.task || 'Base') === 'Base');
            this.selectedVoice = setup.config?.roles?.tts?.voice || '';
        } catch (error) {
            if (generation === epoch) {
                this.profiles = [];
                this.error = this.available
                    ? 'Studio interface is ready, but its Convo-owned audio/profile backend is not configured or reachable. External speech connections do not supply the managed Studio library.'
                    : 'Studio profiles are unavailable. Check Convo managed runtime readiness.';
            }
        }
        finally { if (generation === epoch) this.busy = false; }
    },
    async useVoice() {
        if (this.busy) return;
        if (setup.dirty) { this.error = 'Save or discard pending Convo settings before changing the shared response voice.'; return; }
        if (!this.selectedVoiceAvailable) { this.error = 'Refresh and select an available Base clone.'; return; }
        const generation = epoch; this.busy = true;
        try {
            const saved = await setup.useVoice(this.selectedVoice);
            if (generation !== epoch) return;
            if (!saved) throw new Error(setup.error || 'Voice selection was not saved.');
            this.message = 'Saved for the next Convo response. Any active response keeps its frozen voice.';
        } catch (error) { if (generation === epoch) this.error = 'Voice selection could not be saved. Reload shared Convo settings and try again.'; }
        finally { if (generation === epoch) this.busy = false; }
    },
    close() { epoch++; probe?.abort(); probe = null; clearInterval(watcher); watcher = null; this.available = false; this.busy = false; },
    stopPreview() { this.close(); },
    back() { this.close(); closeModal(); },
});
