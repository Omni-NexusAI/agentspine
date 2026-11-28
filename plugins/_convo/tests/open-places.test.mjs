import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const read = name => readFile(new URL(name, import.meta.url), 'utf8');

test('The page opens and closes each of the voice agent\u2019s own places the way its own button does', async () => {
    const page = await read('../webui/convo-store.js');
    const places = page.slice(page.indexOf("// ---- The voice agent's own places"), page.indexOf('export const store = createStore'));
    for (const piece of ["what === 'voice_settings' ? await openVoiceSettings(target)", "what === 'sound' ? openSound()", "what === 'voice_studio' ? await openVoiceStudio()",
                         "what === 'diagnostics' ? await openDiagnostics()", "what === 'close' ? await closePlace(target)"])
        assert.ok(places.includes(piece), piece);
    assert.ok(places.includes("const SETTINGS_TABS = { connections: 'Connections', conversation: 'Conversation', audio: 'Audio & Video', advanced: 'Advanced' };"));
    // Unsaved settings are never dropped by a spoken "close": they stay open and the answer says so.
    assert.ok(places.includes("if (hostStore('convoSetup')?.dirty) return [false, 'The voice settings have unsaved changes, so they were left open.'];"));
    // Diagnostics only exist in developer mode, and the answer says that instead of failing silently.
    assert.ok(places.includes("return [false, 'The diagnostics are only there with developer mode on.'];"));
    for (const place of ['sound', 'diagnostics', 'voice_settings', 'voice_studio', 'settings']) assert.ok(places.includes(`if (place === '${place}')`), place);
    assert.doesNotMatch(places, /window\.confirm|eval\(|innerHTML/);
});

test('The server and the page name the same places and tabs', async () => {
    const server = await read('../helpers/convo_voice_tools.py'), catalog = await read('../helpers/convo_tools.py');
    assert.ok(server.includes("SETTINGS_TABS = ('connections', 'conversation', 'audio', 'advanced')"));
    assert.ok(server.includes("CLOSABLE = ('voice_settings', 'sound', 'voice_studio', 'diagnostics', 'settings')"));
    assert.ok(catalog.includes("('chat', 'settings', 'files', 'page', 'voice_settings', 'sound', 'voice_studio',"));
});
