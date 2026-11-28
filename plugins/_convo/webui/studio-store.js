import { createStore } from '/js/AlpineStore.js';
import { callJsonApi } from '/js/api.js';
import { closeModal } from '/js/modals.js';
import { store as setup } from '/plugins/_convo/webui/setup-store.js';

const STUDIO_PATH = '/convo-studio/';
let epoch = 0, probe = null, watcher = null;
export const studio = createStore('convoStudio', {
    profiles: [], selectedVoice: '', error: '', message: '', busy: false,
    available: false, blocked: false, frameUrl: STUDIO_PATH,
    // The Voice Studio of a speech server the user runs themself: its own page, in a frame of its own origin (it cannot reach
    // Agent Zero's session). Shown only when this browser can reach that address.
    mode: 'managed', external: '', externalNote: '',
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
        this.mode = setup.speechVerify?.() === 'managed' || !setup.speechVerify ? 'managed' : 'external';
        this.external = ''; this.externalNote = '';
        if (this.mode === 'external') { await this.openExternal(generation); return; }
        this.blocked = this.isBlocked();
        watcher = setInterval(() => {
            this.blocked = this.isBlocked();
            if (this.blocked) { this.available = false; probe?.abort(); }
        }, 250);
        await this.retry();
    },
    async openExternal(generation) {
        let found = setup.choices?.tts?.studio;
        if (!found?.browser_url && setup.speechBlocked?.() === '') {
            try { await setup.discover('tts'); } catch { /* said below */ }
            found = setup.choices?.tts?.studio;
        }
        if (generation !== epoch) return;
        // The voices this server lists are the ones "Use in Convo" can choose from.
        this.profiles = (setup.choices?.tts?.voices || []).map(voice => ({ id: voice.id, voice: voice.id, name: voice.label || voice.id }));
        const url = found?.browser_url || '';
        if (!url) { this.externalNote = 'Your speech server shows no Voice Studio page at its own address. Its voices are still listed above.'; return; }
        let origin = '';
        try { origin = new URL(url).origin; } catch { origin = ''; }
        if (!origin || origin === globalThis.location?.origin) { this.externalNote = 'That Voice Studio cannot be shown inside Agent Zero.'; return; }
        try {
            await fetch(url, { mode: 'no-cors', cache: 'no-store', credentials: 'omit' });     // only whether this browser reaches it
            if (generation === epoch) this.external = url;
        } catch {
            if (generation === epoch) this.externalNote = `Your speech server’s Voice Studio is at ${url}, which this browser cannot reach. Open it on the computer that runs the speech server.`;
        }
    },
    async retry() {
        if (this.mode === 'external') return this.open();
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
    close() { epoch++; probe?.abort(); probe = null; clearInterval(watcher); watcher = null; this.available = false; this.busy = false; this.external = ''; },
    stopPreview() { this.close(); },
    back() { this.close(); closeModal(); },
});
