import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';

const read = path => readFile(new URL(path, import.meta.url), 'utf8');
const squash = text => text.replace(/\s+/g, ' ').trim();

// The host contract, taken from real wrappers (see fixtures/host-canvas-panels.html):
// a panel owns its own visibility binding, because host CSS leaves every
// `.right-canvas-surface-panel` at opacity:0 / pointer-events:none until it
// carries `.is-active`.
function bindings(wrapper, id) {
    const text = squash(wrapper);
    return {
        base: new RegExp(`class="right-canvas-surface-panel[^"]*" data-surface-id="${id}"`).test(text),
        active: text.includes(`'is-active': $store.rightCanvas?.isSurfaceVisible('${id}')`),
        mounted: text.includes(`'is-mounted': $store.rightCanvas?.isSurfaceRendered('${id}')`),
        hidden: text.includes(`:aria-hidden="(!$store.rightCanvas?.isSurfaceVisible('${id}')).toString()"`),
        canvasMode: text.includes('mode="canvas"'),
        displayToggle: /\bx-show=/.test(text),
    };
}

test('Host reference wrappers define the binding contract this suite checks', async () => {
    const fixture = await read('./fixtures/host-canvas-panels.html');
    const [files, browser] = fixture.split('-->')[1].split(/\n(?=<div)/).map(part => part.trim()).filter(Boolean);
    for (const [wrapper, id] of [[files, 'files'], [browser, 'browser']]) {
        const found = bindings(wrapper, id);
        assert.deepEqual({ ...found, canvasMode: undefined }, { base: true, active: true, mounted: true, hidden: true, canvasMode: undefined, displayToggle: false }, id);
    }
});

test('Convo panel follows the host wrapper contract instead of toggling display', async () => {
    const wrapper = await read('../extensions/webui/right-canvas-panels/convo.html');
    assert.deepEqual(bindings(wrapper, 'convo'), { base: true, active: true, mounted: true, hidden: true, canvasMode: true, displayToggle: false });
    // x-show only toggles display, which leaves the host's opacity:0 / pointer-events:none in force.
    assert.doesNotMatch(wrapper, /x-show/);
});

test('Convo mounts its page once the surface is mounted, never from the raw map', async () => {
    const wrapper = squash(await read('../extensions/webui/right-canvas-panels/convo.html'));
    assert.match(wrapper, /<template x-if="\$store\.rightCanvas\?\.isSurfaceMounted\('convo'\)">/);
    assert.doesNotMatch(wrapper, /mountedSurfaces/);
    assert.match(wrapper, /<x-component path="\/plugins\/_convo\/webui\/main\.html" mode="canvas"><\/x-component>/);
});

test('Panel visibility drives polling because the host never calls surface.close() on canvas close', async () => {
    const wrapper = squash(await read('../extensions/webui/right-canvas-panels/convo.html'));
    assert.match(wrapper, /x-effect="\$store\.convo && \(\$store\.convo\.panelOpen = !!\$store\.rightCanvas\?\.isSurfaceVisible\('convo'\)\)"/);
});

test('Registration keeps the modal path for narrow screens and the same surface id', async () => {
    const registration = await read('../extensions/webui/right_canvas_register_surfaces/convo.js');
    assert.match(registration, /id: 'convo'/);
    assert.match(registration, /modalPath: '\/plugins\/_convo\/webui\/main\.html'/);
});

// ---- the second surface: developer diagnostics
test('The diagnostics wrapper follows the same host contract and mounts its page only once the surface is mounted', async () => {
    const wrapper = await read('../extensions/webui/right-canvas-panels/convo-diagnostics.html');
    const markup = wrapper.slice(0, wrapper.indexOf('<style>'));
    assert.deepEqual(bindings(markup, 'convo-diagnostics'), { base: true, active: true, mounted: true, hidden: true, canvasMode: true, displayToggle: false });
    const text = squash(markup);
    assert.match(text, /<template x-if="\$store\.rightCanvas\?\.isSurfaceMounted\('convo-diagnostics'\)"> <x-component path="\/plugins\/_convo\/webui\/diagnostics\.html" mode="canvas"><\/x-component> <\/template>/);
    // The host never calls surface.close() when the canvas closes: visibility, read by the wrapper, is what starts and stops polling.
    assert.match(text, /x-effect="\$store\.convoDiagnostics && \$store\.convoDiagnostics\.setDockedVisible\(!!\$store\.rightCanvas\?\.isSurfaceVisible\('convo-diagnostics'\)\)"/);
});

test('The host offers no unregister and falls back to its own registry, so Convo removes the entry itself and guards opening', async () => {
    const store = await read('./fixtures/host-right-canvas-store.js'), shell = await read('./fixtures/host-right-canvas.html');
    assert.doesNotMatch(store, /unregister|removeSurface/);
    assert.match(store, /get railSurfaces\(\) \{\s*return this\.surfaces;/);                  // the rail is the surfaces array itself
    assert.match(store, /\|\| getRegisteredSurfaces\(\)\.find\(\(surface\) => surface\.id === targetId\)/);
    assert.match(store, /if \(typeof surface\.canOpen === "function" && surface\.canOpen\(payload\) === false\) \{\s*return false;/);
    assert.match(store, /currentSurfaceCanUndock\(\) \{\s*return Boolean\(this\.currentSurface\(\)\?\.modalPath\);/);
    assert.match(shell, /<template x-for="surface in \$store\.rightCanvas\.railSurfaces" :key="surface\.id">/);
    assert.match(shell, /title="Open as window"[\s\S]*?x-show="\$store\.rightCanvas\.currentSurfaceCanUndock\(\)"/);
    const diagnostics = await read('../webui/diagnostics-store.js'), registration = await read('../extensions/webui/right_canvas_register_surfaces/convo.js');
    assert.match(registration, /diagnostics\.bindCanvas\(canvas\);/);
    const registered = squash(diagnostics.slice(diagnostics.indexOf('canvas.registerSurface('), diagnostics.indexOf('});', diagnostics.indexOf('canvas.registerSurface('))));
    assert.match(registered, /id: SURFACE, title: 'Convo diagnostics'/);
    assert.match(registered, /canOpen: payload => this\.canOpen\(payload\)/);
    assert.doesNotMatch(registered, /modalPath/);                                              // Convo's own window is the only pop-out
    assert.match(diagnostics, /host\.surfaces\.splice\(index, 1\)/);
    assert.match(diagnostics, /host\.markSurfaceUnmounted\?\.\(SURFACE\)/);
});

test('The pop-out frame is styled where the canvas loads it, above the canvas and below host modals', async () => {
    const wrapper = await read('../extensions/webui/right-canvas-panels/convo-diagnostics.html');
    const { WINDOW_Z } = await import('../webui/floating-window.js');
    assert.match(wrapper, new RegExp(`\\.convo-diag-window\\{position:fixed;z-index:${WINDOW_Z};`));
    assert.match(wrapper, /\.convo-diag-window\.is-sheet\{/);                                 // bottom sheet on narrow screens
    assert.match(wrapper, /@media \(prefers-reduced-motion:reduce\)\{\.convo-diag-window,\.convo-diag-window \*\{transition:none!important;animation:none!important\}\}/);
    assert.doesNotMatch(wrapper.slice(wrapper.indexOf('<style>')), /(^|[},])\s*\[/);         // no bare attribute selector leaks onto host elements
});

test('The panel: collapsible sections, words beside every graphic, no live region, no title, and the waterfall as a list', async () => {
    const html = await read('../webui/diagnostics.html');
    const markup = html.slice(0, html.indexOf('<style>')), css = html.slice(html.indexOf('<style>'));
    const toggles = [...markup.matchAll(/class="convo-diag-toggle" :aria-expanded="String\(!!\$store\.convoDiagnostics\.sections\.(\w+)\)" :aria-controls="uid \+ '-(\w+)'"/g)];
    assert.deepEqual(toggles.map(match => match[1]), ['live', 'turns', 'gate', 'speculation', 'rolling', 'context', 'tune']);
    for (const [, , id] of toggles) assert.match(markup, new RegExp(`:id="uid \\+ '-${id}'"`));
    assert.doesNotMatch(markup, /aria-live|role="log"/);                                      // meters and numbers are never announced
    assert.doesNotMatch(markup, /\stitle=|:title=|x-bind:title/);                             // nothing writes a title the host tooltip observer would chase
    for (const graphic of markup.matchAll(/<span class="convo-diag-(?:bar|track|ring)[^"]*"([^>]*)>/g)) assert.match(graphic[1], /aria-hidden="true"/, graphic[0]);
    assert.match(markup, /<div class="convo-diag-history" aria-hidden="true">/);
    assert.match(markup, /<ol class="convo-diag-waterfall"/);
    assert.match(markup, /class="convo-diag-issue" :data-severity="issue\.severity"><span class="material-symbols-outlined" aria-hidden="true" x-text="issue\.icon"><\/span><span x-text="issue\.text/);
    // Level meters are written by the store four times a second, straight to the DOM: no reactive binding on them.
    for (const meter of markup.matchAll(/<div class="convo-diag-tile convo-diag-meter" data-diag-level="(input|output)">([\s\S]*?)<\/div>/g)) {
        assert.doesNotMatch(meter[2], /:style|x-text/, meter[1]);
        assert.match(meter[2], /data-diag-level-text>0%<\/span>/);
    }
    assert.match(css, /@media \(prefers-reduced-motion:reduce\)\{\.convo-diag \*\{transition:none!important;animation:none!important\}\}/);
    assert.match(css, /\.light-mode \.convo-diag\{/);                                         // both host themes
    assert.doesNotMatch(css.replace(/\.convo-diag [^{]*\{/g, ''), /(^|[},])\s*\[data-severity/);   // severity colours stay inside the panel
});

test('Tune holds every Advanced tunable and the decision-backend picker, saved through the shared revision-aware draft', async () => {
    const html = await read('../webui/diagnostics.html'), tuning = await read('../webui/tuning-controls.html');
    assert.match(html, /<x-component path="\/plugins\/_convo\/webui\/settings-banner\.html"><\/x-component>/);
    assert.match(html, /<x-component path="\/plugins\/_convo\/webui\/tuning-controls\.html"><\/x-component>/);
    assert.match(html, /x-model="\$store\.convoSetup\.config\.echo_guard"/);                  // mirrored; it also stays in Voice & Audio
    assert.match(html, /@click="\$store\.convoSetup\.save\(\)"/);
    assert.match(html, /:disabled="!\$store\.convoSetup\.dirty \|\| \$store\.convoSetup\.busy"/);
    const bound = [...tuning.matchAll(/(?:x-model(?:\.number)?|:value|@input)="([^"]*)"/g)].map(match => match[1].replace(/roles\[role\]/g, 'roles.classifier'));
    const has = path => bound.some(expression => expression.includes('config.' + path) || expression.includes(path));
    for (const path of ['sidecar.managed', 'sidecar.url', 'sidecar.token_env', 'sidecar.remote', 'system_one.enabled', 'system_one.backend',
                        'policy_gate.respond_min', 'policy_gate.ambient_respond_min', 'policy_gate.interrupt_min', 'policy_gate.ignore_min', 'policy_gate.name_min',
                        'policy_gate.clarify_min', 'policy_gate.clarify_elsewhere_max', 'speculative_reply', 'main_listen_decision', 'first_clause_speech',
                        'continuation_seconds', 'recent_remarks_seconds', 'compaction.context_tokens', 'compaction.output_reserve', 'audio_token_reserve',
                        'max_audio_seconds'])
        assert.ok(has(path), path);
    // The earlier HTTP classifier is edited on the Classifier card (Connections) now; Tune does not repeat it.
    assert.doesNotMatch(tuning, /convo-legacy-classifier|roles\[role\]|roles\.classifier|setKey\(/);
    assert.match(tuning, /Settings → Connections → Classifier/);
    assert.match(tuning, /runtime\('runtime_start'\)[\s\S]*runtime\('runtime_stop'\)/);
    assert.match(tuning, /previewImport\(\$event\.target\.files\[0\]\)/);
});
