// The Convo conversation model as an Agent Zero model type (webui/model-type-mount.js): a section in the preset editor and a
// "Convo — provider / model" row in the preset overviews. The host calls this for every modal once its content has loaded
// (js/modals.js: callJsExtensions('modal_content_loaded', { modalPath, modal, doc })); only those modals are watched, each by an
// observer on that modal alone, released when it closes.
export default async function convoModelType(payload) {
    const { watchedModal, attach } = await import('/plugins/_convo/webui/model-type-mount.js');
    if (!watchedModal(payload?.modalPath) || !payload?.modal?.element) return;
    const { store } = await import('/plugins/_convo/webui/setup-store.js');
    attach(payload, { store, document, MutationObserver: globalThis.MutationObserver, Alpine: globalThis.Alpine });
}
