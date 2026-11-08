export default async function (canvas) {
    const { store, install } = await import('/plugins/_convo/webui/convo-store.js');
    install();
    canvas.registerSurface({
        id: 'convo', title: 'Convo', icon: 'graphic_eq', order: 65,
        modalPath: '/plugins/_convo/webui/main.html',
        open: async () => { store.panelOpen = true; await store.refresh(); await store.history(); },
        close: () => { store.panelOpen = false; },
    });
    // Developer diagnostics: a second surface that the diagnostics store registers only while the saved developer_mode is
    // on and takes out of the rail again when it is turned off (diagnostics-store.js).
    const { store: diagnostics } = await import('/plugins/_convo/webui/diagnostics-store.js');
    diagnostics.bindCanvas(canvas);
}
