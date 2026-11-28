// Optional sound cues for activation, deactivation, dictation and errors. The clips come later: drop files into
// webui/sounds/ and name them in sounds/manifest.json. A missing clip, a blocked autoplay or a failed download is
// silence, never an error, and nothing here touches capture, playback or lifecycle. No AudioContext is created.
const BASE = '/plugins/_convo/webui/sounds/';
let manifest = null, loading = null;
const clips = new Map();

async function load() {
    if (manifest) return manifest;
    loading ??= fetch(BASE + 'manifest.json', { cache: 'no-store' }).then(response => response.ok ? response.json() : {}).catch(() => ({}))
        .then(value => (manifest = { volume: .6, cues: {}, ...(value && typeof value === 'object' ? value : {}) }));
    return loading;
}

// Plays a cue by name. Resolves false when there is nothing to play or playback was refused.
export async function play(name) {
    try {
        const { cues, volume } = await load();
        const file = cues?.[name];
        if (typeof file !== 'string' || !/^[A-Za-z0-9._-]{1,80}$/.test(file)) return false;   // null = no clip yet
        let clip = clips.get(file);
        if (!clip) { clip = new globalThis.Audio(BASE + file); clips.set(file, clip); }
        clip.volume = Math.min(1, Math.max(0, Number(volume) || .6));
        clip.currentTime = 0;
        await clip.play();
        return true;
    } catch { return false; }
}

export function install(target = globalThis) {
    if (target.__convoCues) return target.__convoCues;
    const listener = event => { void play(event?.detail?.name); };
    target.addEventListener?.('convo:cue', listener);
    return (target.__convoCues = { play, dispose() { target.removeEventListener?.('convo:cue', listener); delete target.__convoCues; } });
}

// Test hook: forget the cached manifest and clips.
export function reset() { manifest = loading = null; clips.clear(); }
