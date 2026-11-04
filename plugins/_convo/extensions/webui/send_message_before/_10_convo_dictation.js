// Agent Zero calls this before every message it sends (index.js sendMessage: callJsExtensions('send_message_before', sendCtx), with
// sendCtx = { message, attachments, context, cancel }). A manual send ends Convo's continuous drafting ("Draft in composer
// (continuous)"): listening stops, and a phrase still being transcribed lands in the composer the send empties. This never cancels,
// delays or changes the send; any failure here is swallowed.
export default async function convoComposerSent(sendCtx) {
    try {
        const { store } = await import('/plugins/_convo/webui/convo-store.js');
        store.composerSent?.(sendCtx);
    } catch { /* never in the way of a send */ }
}
