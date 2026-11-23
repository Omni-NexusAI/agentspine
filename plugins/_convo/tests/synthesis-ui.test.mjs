// The voice row (synthesis-setup.js + synthesis-controls.html, in the sound drawer and the diagnostics Tune section), on the real
// settings store over a scripted server: shown only for your own audio.cpp server, checked against the server's bounds and the chosen
// preset in the browser, profiles read only on request, a click applied to the running conversation through convo.tune (queued, never
// dropped, waiting while the conversation starts) without touching the Settings draft, "Save as default" checked with the server and
// saved as one patch of roles.tts.tuning, Reset back to the preset's values. Nothing here creates or selects a profile on the server.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { loadModule, hostStubs } from './vm-loader.mjs';
import { defaults, getPath, setPath } from '../webui/defaults.js';
import { DECODER_CONTEXT, MODEL_DEFAULTS, SYNTHESIS_BOUNDS, SYNTHESIS_KEYS, fitting, fitProblem } from '../webui/synthesis-setup.js';

const plain = value => JSON.parse(JSON.stringify(value));
const read = name => readFile(new URL('../webui/' + name, import.meta.url), 'utf8');
const PRESETS = { quality: { temperature: .8, top_k: 50, top_p: .95, repetition_penalty: 1.05, max_reference_seconds: 30, first_block_frames: 4, steady_block_frames: 8 },
                  fast: { temperature: 1, top_k: 40, top_p: .9, repetition_penalty: 1.05, max_reference_seconds: 12, first_block_frames: 2, steady_block_frames: 4 } };

async function rig({ tts = { provider: 'audiocpp', connection: 'external' }, tuning = { profile_id: '', overrides: {} }, active = true, answers = {}, tune } = {}) {
    const config = plain(defaults);
    Object.assign(config.roles.tts, tts, { tuning: plain(tuning) });
    const server = { config, revision: 'r1', calls: [], tunes: [] };
    const answer = extra => ({ ok: true, config: plain(server.config), revision: server.revision, readiness: [], credentials: {}, decision_backends: [], providers: [], native: {}, ...extra });
    const api = async (_url, payload) => {
        server.calls.push(plain(payload));
        if (answers[payload.action]) return answers[payload.action](payload);
        if (payload.action === 'synthesis_read') return { ok: true, profiles: [{ id: 'quality', label: 'Quality', revision: 3 }, { id: 'fast', label: 'Fast', revision: 1 }],
            selected: { id: 'fast', label: 'Fast' }, saved: plain(server.config.roles.tts.tuning), bounds: { ...SYNTHESIS_BOUNDS, top_k: [1, 100] }, native_pcm: true };
        if (payload.action === 'synthesis_resolve') return { ok: true, effective: { ...(PRESETS[payload.profile_id || 'fast'] || {}), ...payload.overrides }, profile_id: payload.profile_id || 'fast', profile_revision: 3 };
        if (payload.action === 'setup_patch') {
            const conflicts = [];
            for (const change of payload.changes) {
                if (JSON.stringify(getPath(server.config, change.path)) === JSON.stringify(change.base)) setPath(server.config, change.path, change.value);
                else conflicts.push({ path: change.path, mine: change.value, theirs: getPath(server.config, change.path) });
            }
            if (conflicts.length < payload.changes.length) server.revision = 'r2';
            return answer({ conflicts });
        }
        return answer();
    };
    const convo = { session: active ? { id: 's1', epoch: 1 } : null, state: active ? 'listening' : 'off', busy: false,
        get active() { return !!this.session; },
        async tune(profile_id, overrides, options = {}) {
            if (options.read) return { ok: true, source: 'default', profile_id: '', overrides: {}, effective: {}, error: '' };
            server.tunes.push(plain({ profile_id, overrides }));
            if (tune) return tune(profile_id, overrides);
            return { ok: true, source: 'own', profile_id, overrides, effective: { temperature: overrides.temperature ?? .8, top_k: 40 }, error: '' };
        } };
    const fast = (fn, ms) => setTimeout(fn, Math.min(ms, 5));          // the retry wait, shortened
    const { namespace } = await loadModule('webui/setup-store.js', { bindings: hostStubs({ '/js/api.js': { callJsonApi: api, fetchApi: api } }, { reactive: true }),
        globals: { window: { confirm: () => true }, setTimeout: fast, Alpine: { store: name => (name === 'convo' ? convo : undefined) } } });
    const store = namespace.store;
    await store.load();
    store.synthesisInit();
    const overrides = () => store.ui.synthesis.work.overrides;
    return { store, server, convo, overrides, actions: () => server.calls.map(call => call.action) };
}

test('Shown only for your own audio.cpp server: Convo\'s managed runtime, Kokoro and other providers have no such controls', async () => {
    for (const [tts, expected] of [[{ provider: 'audiocpp', connection: 'external' }, true], [{ provider: 'audiocpp', connection: '' }, true],
                                   [{ provider: 'audiocpp', connection: 'managed' }, false], [{ provider: 'kokoro_native' }, false], [{ provider: 'openai' }, false]]) {
        const f = await rig({ tts });
        assert.equal(f.store.synthesisApplies(), expected, JSON.stringify(tts));
    }
    const f = await rig();
    assert.deepEqual(f.actions(), ['setup_read']);                                // rendering the card contacts nothing
    assert.deepEqual(plain(f.store.config.roles.tts.tuning), { profile_id: '', overrides: {} });
    assert.deepEqual(plain(defaults.roles.tts.tuning), { profile_id: '', overrides: {} });
    assert.deepEqual([defaults.answer_tokens, defaults.barge_in_seconds], [1024, 1]);
});

test('The browser checks every value against the server\'s bounds and the chosen preset before anything is sent', async () => {
    const f = await rig();
    const work = f.store.ui.synthesis.work;
    const problems = () => plain(f.store.synthesisProblems().map(item => item.text));
    assert.deepEqual(problems(), []);
    Object.assign(f.overrides(), { temperature: 0, top_k: 12.5, seed: -1, top_p: .9, max_reference_seconds: 31 });
    assert.deepEqual(problems(), ['Temperature must be above 0.', 'Top-k must be a whole number.', 'Seed must be from 0 to 4294967295.',
                                  'Reference limit (seconds) must be from 1 to 30.']);
    assert.equal(f.store.synthesisCan('apply'), false);
    assert.equal(f.store.synthesisCan('save'), false);
    assert.equal(f.store.synthesisCan('reset'), true);
    work.overrides = { temperature: .7, top_k: 50, seed: 4294967295, top_p: '', repetition_penalty: '1.1' };   // '' = the preset's own
    assert.deepEqual(problems(), []);
    assert.deepEqual(plain(f.store.synthesisValues()), { temperature: .7, top_k: 50, repetition_penalty: 1.1, seed: 4294967295 });
    work.overrides = { first_block_frames: 8, steady_block_frames: 4 };
    assert.deepEqual(problems(), ['The steady block must be at least the first block.']);
    f.store.synthesisUnlock(true);
    work.overrides = { steady_block_frames: 290, left_context_frames: 20 };
    assert.deepEqual(problems(), ['The decoder context plus the steady block must be at most 300 frames.']);
    work.overrides = { steady_block_frames: 280 };                                 // the default context of 25 counts too
    assert.deepEqual(problems(), ['The decoder context plus the steady block must be at most 300 frames.']);
    f.store.ui.synthesis.unlocked = false;
    work.overrides = { left_context_frames: DECODER_CONTEXT - 1 };
    assert.deepEqual(problems(), [`A decoder context below ${DECODER_CONTEXT} frames needs the unlock.`]);
    work.overrides = { top_k: 150 };
    await f.store.synthesisLoad();                                                 // the server's own bounds win once read
    assert.deepEqual(problems(), ['Top-k must be from 1 to 100.']);
    // Against the chosen preset's own values: a first block above Fast's steady block of 4 would be refused at speech time.
    f.store.ui.synthesis.resolved = { fast: PRESETS.fast, quality: PRESETS.quality };
    work.overrides = { first_block_frames: 6 };
    assert.match(problems()[0], /first block \(6 frames\) is larger than this preset’s steady block \(4 frames\)/);
    work.profile_id = 'quality';
    assert.deepEqual(problems(), []);
    assert.deepEqual(SYNTHESIS_KEYS.filter(key => !SYNTHESIS_BOUNDS[key]), []);
    assert.equal(fitProblem({ first_block_frames: 2, steady_block_frames: 4 }), '');
    assert.deepEqual(plain(fitting(PRESETS.fast, { first_block_frames: 6, temperature: .7 })), { temperature: .7 });
});

test('Profiles are read only on request; the saved choice stays visible when the server does not list it', async () => {
    const f = await rig({ tuning: { profile_id: 'retired', overrides: {} } });
    assert.deepEqual(plain(f.store.synthesisProfiles()), [{ id: 'retired', label: 'retired (saved; not verified)' }]);
    assert.equal(await f.store.synthesisLoad(), true);
    assert.deepEqual(f.actions(), ['setup_read', 'synthesis_read']);
    assert.deepEqual(plain(f.store.synthesisProfiles()), [{ id: 'retired', label: 'retired (saved; not verified)' }, { id: 'quality', label: 'Quality · revision 3' }, { id: 'fast', label: 'Fast · revision 1' }]);
    assert.match(f.store.ui.synthesis.message, /2 profiles on the speech server\. Its own current selection: Fast\./);
    f.store.synthesisSetProfile('quality');
    assert.equal(f.store.ui.synthesis.work.profile_id, 'quality');
    assert.equal(f.store.config.roles.tts.tuning.profile_id, 'retired');            // the saved default is untouched
    const failing = await rig({ answers: { synthesis_read: () => ({ ok: false, error: 'The speech server did not list its tuning profiles (HTTP 404)' }) } });
    assert.equal(await failing.store.synthesisLoad(), false);
    assert.match(failing.store.ui.synthesis.error, /did not list its tuning profiles/);
});

test('Block sizes and the sampler apply only with native PCM, and the decoder context only once unlocked; locking returns it to 25', async () => {
    const f = await rig({ tuning: { profile_id: '', overrides: { first_block_frames: 4, steady_block_frames: 12, left_context_frames: 20 } } });
    assert.equal(f.store.synthesisNativePcm(), false);                             // the saved switch, until the server says
    assert.equal(f.store.synthesisUnlocked(), true);                               // a saved context shows unlocked
    assert.deepEqual(plain(f.store.synthesisApplied()), {});                       // without native PCM no block size is sent
    assert.deepEqual(plain(f.store.synthesisValues()), { first_block_frames: 4, steady_block_frames: 12, left_context_frames: 20 });   // but kept for later
    await f.store.synthesisLoad();
    assert.equal(f.store.synthesisNativePcm(), true);
    assert.deepEqual(plain(f.store.synthesisApplied()), { first_block_frames: 4, steady_block_frames: 12, left_context_frames: 20 });
    f.store.synthesisUnlock(false);
    assert.equal('left_context_frames' in f.overrides(), false);
    assert.deepEqual(plain(f.store.synthesisApplied()), { first_block_frames: 4, steady_block_frames: 12 });
});

test('A preset click applies to this conversation at once, does not edit the Settings draft, and the row shows conversation and default apart', async () => {
    const f = await rig({ tuning: { profile_id: 'quality', overrides: {} } });
    await f.store.synthesisOpen();
    assert.match(f.store.synthesisStatus().conversation, /This conversation: the saved default \(Quality\)/);
    assert.equal(f.store.synthesisStatus().saved, 'Saved default: Quality.');
    f.store.synthesisChoose('fast');
    assert.equal(f.store.dirty, false);                                            // the Settings draft is not edited
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.deepEqual(f.server.tunes, [{ profile_id: 'fast', overrides: {} }]);
    assert.match(f.store.synthesisStatus().conversation, /This conversation: Fast, from its next reply\./);
    assert.equal(f.store.synthesisStatus().saved, 'Saved default: Quality.');      // still the default for new conversations
    assert.equal(f.store.synthesisEffective().title, 'In effect for this conversation from its next reply');
    f.convo.session = { id: 's2', epoch: 1 };                                      // a stop and start: the saved default again
    assert.match(f.store.synthesisStatus().conversation, /the saved default \(Quality\)/);
    assert.equal(f.actions().includes('setup_patch'), false);                      // nothing saved
});

test('A click while another is being applied is queued and applied after it; the latest choice wins', async () => {
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    const f = await rig({ tune: async (profile_id, overrides) => { if (profile_id === 'quality') await gate; return { ok: true, source: 'own', profile_id, overrides, effective: {} }; } });
    await f.store.synthesisLoad();
    const first = f.store.synthesisApply();                                         // the server's own selection
    f.store.synthesisChoose('quality');                                            // busy: queued
    await new Promise(resolve => setTimeout(resolve, 10));
    f.store.synthesisChoose('fast');                                               // a quick second click while Quality is in flight
    f.store.ui.synthesis.work.profile_id = 'fast';
    release();
    assert.equal(await first, true);
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.deepEqual(f.server.tunes.map(item => item.profile_id), ['', 'quality', 'fast']);   // nothing dropped, the latest last
    assert.equal(f.store.ui.synthesis.session.profile_id, 'fast');
});

test('A refusal shows at once and keeps the previous voice; during start-up a choice waits and applies when ready', async () => {
    const refused = await rig({ tune: async () => { throw new Error('The speech server refused these synthesis values (HTTP 503)'); } });
    assert.equal(await refused.store.synthesisApply(), false);
    assert.match(refused.store.ui.synthesis.error, /HTTP 503\) This conversation keeps its previous voice\./);
    let answers = 0;
    const waiting = await rig({ tune: async (profile_id, overrides) => (++answers < 3 ? { ok: true, pending: true, message: 'Applies when the conversation is ready.' }
                                                                                          : { ok: true, source: 'own', profile_id, overrides, effective: {} }) });
    const run = waiting.store.synthesisChoose('quality');
    assert.equal(run, 'quality');
    await new Promise(resolve => setTimeout(resolve, 5));
    assert.equal(waiting.store.synthesisStatus().conversation, 'Applies when the conversation is ready.');
    await new Promise(resolve => setTimeout(resolve, 60));
    assert.equal(answers, 3);
    assert.equal(waiting.store.ui.synthesis.session.profile_id, 'quality');
    assert.equal(waiting.store.ui.synthesis.error, '');
    const starting = await rig({ active: false });
    starting.convo.state = 'connecting';                                           // starting: no session id yet
    starting.store.synthesisChoose('fast');
    await new Promise(resolve => setTimeout(resolve, 5));
    assert.equal(starting.store.synthesisStatus().conversation, 'Applies when the conversation is ready.');
    starting.convo.session = { id: 's9', epoch: 1 }; starting.convo.state = 'listening';
    await new Promise(resolve => setTimeout(resolve, 40));
    assert.deepEqual(starting.server.tunes, [{ profile_id: 'fast', overrides: {} }]);
    const idle = await rig({ active: false });
    assert.equal(idle.store.synthesisCan('apply'), false);
    assert.equal(await idle.store.synthesisApply(), false);
    assert.deepEqual(idle.server.tunes, []);
});

test('Switching presets clears overrides that no longer fit the new one', async () => {
    const f = await rig({ tuning: { profile_id: 'quality', overrides: {} } });
    await f.store.synthesisOpen();
    Object.assign(f.overrides(), { first_block_frames: 6, temperature: .7 });
    f.store.synthesisChoose('fast');                                               // Fast's steady block is 4
    assert.deepEqual(plain(f.overrides()), { temperature: .7 });
    assert.match(f.store.ui.synthesis.message, /Cleared First block/);
});

test('Check asks the server to resolve the values and shows them; nothing is spoken, saved or applied', async () => {
    const f = await rig({ tuning: { profile_id: '', overrides: { temperature: .9 } } });
    assert.equal(await f.store.synthesisCheck(), true);
    assert.deepEqual(f.server.calls.at(-1), { action: 'synthesis_resolve', profile_id: '', overrides: { temperature: .9 } });
    const effective = f.store.synthesisEffective();
    assert.equal(effective.title, 'What the speech server resolves for these values');
    assert.ok(effective.items.some(item => item.label === 'Temperature' && item.value === '0.9'));
    assert.deepEqual(f.server.tunes, []);
    const refused = await rig({ answers: { synthesis_resolve: () => ({ ok: false, error: 'The speech server refused these synthesis values (HTTP 422)' }) } });
    assert.equal(await refused.store.synthesisCheck(), false);
    assert.match(refused.store.ui.synthesis.error, /refused these synthesis values/);
});

test('Save as default checks with the server, then patches roles.tts.tuning alone; a preset the server refuses is not saved', async () => {
    const f = await rig({ tuning: { profile_id: 'fast', overrides: { top_k: 50 } } });
    f.store.config.style = 'another unsaved edit';
    f.store.synthesisSetProfile('quality');
    Object.assign(f.overrides(), { temperature: .75, top_p: '', left_context_frames: 30 });
    f.store.ui.synthesis.unlocked = false;
    assert.equal(await f.store.synthesisSaveDefault(), true);
    assert.deepEqual(f.actions().slice(-2), ['synthesis_resolve', 'setup_patch']);   // checked first
    const patch = f.server.calls.find(call => call.action === 'setup_patch');
    assert.deepEqual(patch.changes, [{ path: 'roles.tts.tuning', base: { profile_id: 'fast', overrides: { top_k: 50 } }, value: { profile_id: 'quality', overrides: { temperature: .75, top_k: 50 } } }]);
    assert.deepEqual(plain(f.server.config.roles.tts.tuning), { profile_id: 'quality', overrides: { temperature: .75, top_k: 50 } });
    assert.equal(f.store.config.style, 'another unsaved edit');                     // other edits stay as they were
    assert.match(f.store.ui.synthesis.message, /Saved as the default/);
    assert.equal(f.store.synthesisStatus().saved, 'Saved default: quality with 2 changes.');
    const deleted = await rig({ answers: { synthesis_resolve: () => ({ ok: false, error: 'The speech server refused these synthesis values (HTTP 404): unknown profile' }) } });
    deleted.store.synthesisSetProfile('gone');
    assert.equal(await deleted.store.synthesisSaveDefault(), false);
    assert.match(deleted.store.ui.synthesis.error, /^Not saved: .*HTTP 404/);
    assert.equal(deleted.actions().includes('setup_patch'), false);
    const clash = await rig({ answers: { synthesis_resolve: () => ({ ok: true, effective: { first_block_frames: 2, steady_block_frames: 4 }, profile_id: 'fast', profile_revision: 1 }) } });
    await clash.store.synthesisLoad();
    clash.overrides().first_block_frames = 6;
    assert.equal(await clash.store.synthesisSaveDefault(), false);
    assert.match(clash.store.ui.synthesis.error, /Not saved: The first block \(6 frames\)/);
    const elsewhere = await rig();
    elsewhere.server.config.roles.tts.tuning = { profile_id: 'quality', overrides: {} };   // changed in another editor meanwhile
    elsewhere.overrides().temperature = .7;
    assert.equal(await elsewhere.store.synthesisSaveDefault(), false);
    assert.match(elsewhere.store.ui.synthesis.message, /Changed elsewhere meanwhile/);
});

test('Reset clears every override and locks the context again, keeping the preset; nothing is sent', async () => {
    const f = await rig({ tuning: { profile_id: 'quality', overrides: { temperature: .6, left_context_frames: 10 } } });
    f.store.ui.synthesis.effective = { source: 'check', values: { temperature: .6 }, for: '' };
    assert.equal(f.store.synthesisReset(), true);
    assert.deepEqual(plain(f.store.ui.synthesis.work), { profile_id: 'quality', overrides: {} });
    assert.deepEqual([f.store.synthesisUnlocked(), f.store.synthesisEffective()], [false, null]);
    assert.deepEqual(f.actions(), ['setup_read']);
    assert.deepEqual(plain(f.store.config.roles.tts.tuning), { profile_id: 'quality', overrides: { temperature: .6, left_context_frames: 10 } });
});

test('The voice model\'s own defaults show beside each preset', async () => {
    const f = await rig();
    await f.store.synthesisOpen();
    const table = plain(f.store.synthesisDefaultsTable());
    assert.deepEqual(table.map(row => [row.label, row.model]), [['Temperature', '0.9'], ['Top-k', '50'], ['Top-p', '1'], ['Repetition penalty', '1.05'], ['Reference audio (s)', 'whole clip']]);
    assert.deepEqual(table[0].presets, [{ id: 'quality', value: '0.8' }, { id: 'fast', value: '1' }]);
    assert.equal(MODEL_DEFAULTS.temperature, .9);
});

test('The row sits in the diagnostics Tune section, binds its own choice, hides look-ahead and flush, and never creates or selects a profile', async () => {
    const diagnostics = await read('diagnostics.html'), html = await read('synthesis-controls.html'), mixin = await read('synthesis-setup.js');
    assert.match(diagnostics, /<x-component path="\/plugins\/_convo\/webui\/tuning-controls\.html"><\/x-component>\s*<x-component path="\/plugins\/_convo\/webui\/synthesis-controls\.html"><\/x-component>/);
    assert.match(html, /^<html><head><script type="module">import \{ store \} from '\/plugins\/_convo\/webui\/setup-store\.js';<\/script><\/head><body>/);
    assert.match(html, /<template x-if="\$store\.convoSetup\?\.config && \$store\.convoSetup\.synthesisApplies\(\)">/);
    for (const key of ['temperature', 'top_k', 'top_p', 'repetition_penalty', 'seed', 'max_reference_seconds', 'first_block_frames', 'steady_block_frames', 'left_context_frames'])
        assert.match(html, new RegExp(`x-model\\.number="\\$store\\.convoSetup\\.synthesisWork\\(\\)\\.overrides\\.${key}"`), key);
    assert.doesNotMatch(html, /x-model[^"]*="[^"]*config\.roles\.tts\.tuning/);            // the row never edits the Settings draft
    assert.doesNotMatch(html, /text_lookahead|phrase_flush_ms/);                            // the server ignores them for Convo
    const sampler = html.slice(html.indexOf('data-convo-synthesis-sampler'), html.indexOf('</template>', html.indexOf('data-convo-synthesis-sampler')));
    for (const key of ['temperature', 'top_k', 'top_p', 'repetition_penalty']) assert.ok(sampler.includes(`overrides.${key}`), key + ' only with native PCM');
    const native = html.slice(html.indexOf('data-convo-synthesis-native'), html.indexOf('</template>', html.indexOf('data-convo-synthesis-native')));
    for (const key of ['first_block_frames', 'steady_block_frames', 'left_context_frames']) assert.ok(native.includes(`overrides.${key}`), key + ' only with native PCM');
    assert.match(native, /<label x-show="\$store\.convoSetup\.synthesisUnlocked\(\)">Decoder context/);
    for (const label of ['Apply to this conversation', 'Save as default', 'Reset to profile', 'Read profiles from the speech server'])
        assert.match(html, new RegExp(`>${label}<|'${label}'`), label);
    assert.doesNotMatch(html + mixin, /synthesis_(create|select|save|delete)|tuning_select|profile_create|\/tuning\/(select|profiles\/)/);
    assert.deepEqual([...new Set([...mixin.matchAll(/this\.api\('([a-z_]+)'/g)].map(match => match[1]))].sort(), ['synthesis_read', 'synthesis_resolve']);
    assert.match(mixin, /this\.patch\(\{ 'roles\.tts\.tuning': tuning \}\)/);
    assert.match(mixin, /convo\.tune\(job\.profile_id, job\.overrides\)/);
    // Bindings that can run before the store exists are null-safe.
    for (const [, expression] of html.matchAll(/x-(?:show|text)="(\$store\.convoSetup\?\.ui[^"]*)"/g)) assert.match(expression, /\?\.ui\?\.synthesis\?\./);
});

// The Python coverage test (tests/test_settings_coverage.py) counts a setting only through a real binding in a file reachable from
// the settings entry points or the diagnostics panel; this mirrors it for the settings this batch adds.
test('Every setting this batch adds has a real binding where the settings checks look', async () => {
    const COMPONENT = /<x-component\s+path="\/plugins\/_convo\/webui\/([\w.-]+\.html)"/g;
    const BINDING = /\s(?:x-model(?:\.[\w.]+)?|:value|:checked|x-bind:value|x-bind:checked|@change|@input|x-on:change|x-on:input)="([^"]*)"/g;
    const seen = new Set(), expressions = [];
    const visit = async name => {
        if (seen.has(name)) return;
        seen.add(name);
        const text = await read(name);
        for (const [, expression] of text.matchAll(BINDING)) expressions.push(expression.replace(/\s+/g, ''));
        for (const [, child] of text.matchAll(COMPONENT)) await visit(child);
    };
    for (const entry of ['config.html', 'settings-modal.html', 'diagnostics.html']) await visit(entry);
    for (const path of ['roles.tts.tuning.profile_id', 'roles.tts.tuning.overrides', 'barge_in_seconds', 'answer_tokens'])
        assert.ok(expressions.some(expression => new RegExp(`config\\.${path.replaceAll('.', '\\.')}(?!\\w)`).test(expression)), path);
    const tuning = await read('tuning-controls.html');
    assert.match(tuning, /<label>Interrupt after this much of your speech \(seconds\)<input type="number" min="0\.4" max="2" step="0\.1" x-model\.number="\$store\.convoSetup\.config\.barge_in_seconds"><\/label>/);
    assert.match(tuning, /<label>Longest spoken answer \(tokens\)<input type="number" min="256" max="4096" step="64" x-model\.number="\$store\.convoSetup\.config\.answer_tokens"><\/label>/);
});
