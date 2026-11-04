export default async function () {
    const { install } = await import('/plugins/_convo/webui/convo-store.js');
    install();
    // Sound cues are optional and never block startup.
    try { (await import('/plugins/_convo/webui/sound-cues.js')).install(); } catch { /* no cues */ }
}
