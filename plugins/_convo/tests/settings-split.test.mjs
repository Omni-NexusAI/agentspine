// The settings interface is one shell plus one x-component per card. These checks hold for the whole tree, whichever card
// changes: the composition the host performs, the shell's structure, and the Developer row.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { COMPONENT, ENTRY_POINTS, readWebui, settingsFiles, settingsMarkup } from './settings-markup.mjs';

const fixture = name => readFile(new URL('./fixtures/' + name, import.meta.url), 'utf8');
const CARDS = ['settings-banner.html', 'conversation-model-field.html', 'classifier-model-field.html', 'speech-output-field.html',
               'speech-voice-field.html', 'dictation-field.html'];   // tuning-controls.html is embedded by the diagnostics panel

test('Both entry points embed the one shell, and the shell composes every card exactly once', async () => {
    for (const entry of ENTRY_POINTS) assert.match(await readWebui(entry), /<x-component path="\/plugins\/_convo\/webui\/settings-controls\.html">/, entry);
    const files = await settingsFiles();
    assert.deepEqual(files, ['settings-controls.html', ...CARDS]);
    const shell = await readWebui('settings-controls.html');
    for (const card of CARDS) assert.equal(shell.split(`path="/plugins/_convo/webui/${card}"`).length - 1, 1, card);
    for (const card of CARDS) assert.equal([...(await readWebui(card)).matchAll(COMPONENT)].length, 0, card + ' nests no further component');
});

test('The host composes nested components through the same observer at every level (verbatim v2.11 fixtures)', async () => {
    // Plugin Config modal: the body is x-html of an <x-component> for the plugin's config.html.
    const modal = await fixture('host-plugin-settings.html'), store = await fixture('host-plugin-settings-store.js');
    assert.match(modal, /class="plugin-settings-body"\s+x-html="context\.settingsComponentHtml"/);
    assert.match(store, /return `<x-component path="\/plugins\/\$\{this\.pluginName\}\/webui\/config\.html"><\/x-component>`;/);
    // components.js: every element added anywhere under <body> is imported if it is an x-component, else searched for them;
    // a component's body nodes are appended only after its module scripts loaded, so nested components load the same way.
    const components = await fixture('host-components.js');
    assert.match(components, /observer\.observe\(document\.body, \{ childList: true, subtree: true \}\)/);
    assert.match(components, /if \(node\.matches\?\.\("x-component"\)\) \{\s*importComponent\(node\.getAttribute\("path"\), node\);\s*\} else if \(node\.querySelectorAll\) \{\s*loadComponents\(\[node\]\);/);
    assert.match(components, /await Promise\.all\(loadPromises\);\s*for \(const deferred of deferredNodes\) \{\s*targetElement\.appendChild\(deferred\);/);
    // Our cards therefore sit where the observer sees them: directly in the shell's x-if clone, never inside x-html.
    const shell = await readWebui('settings-controls.html');
    const body = shell.slice(shell.indexOf('<template x-if="$store.convoSetup?.config">'), shell.indexOf('</template></div>'));
    for (const card of CARDS) assert.ok(body.includes(`<x-component path="/plugins/_convo/webui/${card}">`), card);
    assert.doesNotMatch(await settingsMarkup(), /x-html/);
});

test('A card lays out exactly as before: its wrapper takes no box, and the sections are still the shell\'s direct stacks', async () => {
    const shell = await readWebui('settings-controls.html');
    assert.match(shell, /\.convo-settings x-component\{display:contents\}/);
    for (const section of ['Connections', 'Conversation', 'Voice & Audio'])
        assert.match(shell, new RegExp(`\\n  <div x-show="\\$store\\.convoSetup\\.section === '${section}'" class="convo-stack">`), section);
    assert.match(shell, /<section class="convo-settings"[^>]*>[\s\S]*<footer class="convo-actions">[\s\S]*<\/footer>\n<\/section><\/template>/);   // the footer the modals hide
});

test('The Developer row sits outside the sections, binds both modes and opens diagnostics only when that store exists', async () => {
    const shell = await readWebui('settings-controls.html');
    const row = shell.slice(shell.indexOf('<section class="convo-developer"'), shell.indexOf('</section>', shell.indexOf('<section class="convo-developer"')));
    assert.ok(shell.indexOf('<section class="convo-developer"') > shell.indexOf("section === 'Voice & Audio'"));
    assert.doesNotMatch(shell, /section === 'Advanced'|tuning-controls\.html/);         // the tunables moved to the diagnostics panel
    assert.doesNotMatch(row.split('\n')[0], /convo-stack/);                               // the live smoke counts each tab's one visible stack
    assert.match(row, /<input type="checkbox" x-model="\$store\.convoSetup\.config\.developer_mode">Developer diagnostics</);
    assert.match(row, /<input type="checkbox" x-model="\$store\.convoSetup\.config\.ai_link_development_mode">AI Link development mode</);
    assert.match(row, /<button type="button" class="button" x-show="\$store\.convoDiagnostics\?\.available" @click="\$store\.convoDiagnostics\?\.open\(\)">Open diagnostics<\/button>/);
    const tuning = await readWebui('tuning-controls.html');
    assert.doesNotMatch(tuning, /developer_mode|ai_link_development_mode/);              // moved, not duplicated
});

test('One stored value, two labelled places: the context window', async () => {
    const html = await settingsMarkup();
    assert.equal([...html.matchAll(/x-model\.number="\$store\.convoSetup\.config\.compaction\.context_tokens"/g)].length, 1);
    assert.match(html, /Context window \(tokens\)/);
    assert.doesNotMatch(html, /localStorage|sessionStorage/);
});

// The three Connections cards read alike (the user's request): the same host field structure and classes, the same note and
// advanced toggle, and text colours, sizes and spacing that come from the host's field classes, not from a card of their own.
const CONNECTIONS = ['conversation-model-field.html', 'classifier-model-field.html', 'speech-output-field.html'];
const styles = html => [...html.matchAll(/<style>([\s\S]*?)<\/style>/g)].map(match => match[1]).join('\n');
test('The Connections cards share one field structure and style source, with no colour or text-size overrides of their own', async () => {
    const shell = await readWebui('settings-controls.html');
    const order = CONNECTIONS.map(card => shell.indexOf(`path="/plugins/_convo/webui/${card}"`));
    assert.ok(order.every((index, i) => index > 0 && (i === 0 || index > order[i - 1])), 'composed in this order, the style source first');
    for (const card of [...CONNECTIONS, 'speech-voice-field.html']) {
        const html = await readWebui(card), css = styles(html);
        if (card !== 'speech-voice-field.html') {
            assert.match(html, /<fieldset class="convo-card convo-model-card[ "]/, card);
            assert.match(html, /<legend x-text="\$store\.convoSetup\.label\(role\)"><\/legend>/, card);
            assert.match(html, /<p class="convo-model-note"><x-icon name="[\w_]+" aria-hidden="true"><\/x-icon><span>/, card);
            assert.match(html, /<div class="convo-model-fields"/, card);
            assert.match(html, /<button type="button" class="advanced-toggle" :aria-expanded="advOpen \? 'true' : 'false'"/, card);
            for (const name of ['field', 'field-label', 'field-title', 'field-description', 'field-control', 'toggle', 'toggler'])
                assert.match(html, new RegExp(`class="[^"]*\\b${name}\\b`), `${card}: ${name}`);
        }
        assert.doesNotMatch(html, /class="[^"]*\bconvo-hint\b|<span class="field-title"|<summary>Advanced|class="convo-check"/, card);   // hints are host field descriptions; switches are host toggles
        assert.doesNotMatch(html, /\sstyle="/, card);                                             // no inline styling
        assert.doesNotMatch(css, /#[0-9a-f]{3,8}\b/i, card);                                       // colours are host tokens
        assert.doesNotMatch(css, /(?:^|[;{\s])(?:color|border(?:-[a-z]+)?-color|background(?:-color)?)\s*:\s*(?!var\(|transparent|none|inherit)/, card);
        if (card !== 'conversation-model-field.html')                                              // the style source alone may restyle the host's field classes
            assert.doesNotMatch(css, /\.field(?:-title|-description|-label|-control)?\b[^{]*\{/, card);
    }
});

test('Every card can render on its own: it loads the shared settings store itself', async () => {
    for (const card of CARDS) assert.match(await readWebui(card), /^<html><head><script type="module">import \{ store \} from '\/plugins\/_convo\/webui\/setup-store\.js';<\/script><\/head><body>/, card);
});
