import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const html = await readFile(new URL('../extensions/webui/page-head/_10_convo_legacy_ui.html', import.meta.url), 'utf8');
const script = html.replace(/^\s*<script[^>]*>/, '').replace(/<\/script>\s*$/, '');
new vm.Script(script); // Parse the entire deployed script, not just test extracts.

function fn(name, globals = {}) {
  const start = script.search(new RegExp(`    (?:async )?function ${name}\\(`));
  assert.ok(start >= 0, name);
  const end = script.indexOf('\n    }', start) + '\n    }'.length;
  return vm.runInNewContext(`(${script.slice(start, end).trim()})`, globals);
}

test('weighted and explicit equal-weight summaries never resurrect legacy secondary', () => {
  const summary = fn('voiceSummary');
  assert.equal(summary({ voice: 'af_nova,bm_george', voice_weights: {}, secondary_voice: 'am_echo' }), 'af_nova,bm_george');
  assert.equal(summary({ voice: 'stale', voice_weights: { af_nova: 3, bm_george: 7 } }), 'af_nova (30%) + bm_george (70%)');
  assert.equal(summary({ voice: 'af_nova', secondary_voice: 'bm_george' }), 'af_nova + bm_george');
});

test('native modal collects complete weights and ignores any stale injected pair', () => {
  const native = { config: { voice: 'af_nova,bm_george', voice_weights: {} } };
  const page = { querySelectorAll: () => [] };
  const modal = { querySelector: selector => selector === '.kokoro-blend-editor' ? {} :
    selector.includes('primary-voice') || selector.includes('voice-select') || selector.includes("type='range'") ? { value: 'stale' } : null };
  const collect = fn('collectKokoroModalSettings', { Alpine: { $data: () => native }, fieldPage: () => page });
  const settings = { voice_weights: { am_echo: 9 }, _convo_revision: 'keep', remote_token: '' };
  collect(settings, modal);
  assert.equal(settings.voice, 'af_nova,bm_george');
  assert.deepEqual(Object.keys(settings.voice_weights), []);
  assert.equal(settings._convo_revision, 'keep');
  assert.equal(settings.remote_token, '');
  assert.equal(settings.secondary_voice, undefined);
});

test('legacy modal clears old migrated weights; native modal skips competing persistence', async () => {
  const collect = fn('collectKokoroModalSettings', { fieldPage: () => ({ querySelectorAll: () => [] }) });
  const settings = { voice_weights: { am_echo: 9 } };
  collect(settings, { querySelector: () => null });
  assert.equal('voice_weights' in settings, false);
  const persist = fn('persistKokoroModal', { callJson: () => assert.fail('Unexpected competing save') });
  await persist({ querySelector: () => ({}) });
});

test('native catalog extension preserves existing voices/groups and is idempotent', async () => {
  const existing = { id: 'af_heart', label: 'Existing', group: 'American Female' };
  const native = { catalog: [existing], availableVoices() {}, voiceIds: () => ['zz_custom'] };
  const groups = [{ label: 'American Female' }], initialized = [];
  const select = { querySelectorAll: () => groups, append: group => groups.push(group) };
  const createElement = tag => ({ tag, attrs: {}, children: [], content: { append() {} },
    setAttribute(key, value) { this.attrs[key] = value; }, append(child) { this.children.push(child); } });
  const extend = fn('extendNativeVoiceCatalog', {
    Alpine: { $data: () => native, mutateDom: callback => callback(), initTree: group => initialized.push(group) },
    document: { createElement }, loadVoices: async () => [{ value: 'af_heart' }, { value: 'bm_george' }, { value: 'af_heart,bm_george' }],
  });
  const page = { querySelector: () => select };
  await extend(page, { settings: {} });
  await extend(page, { settings: {} });
  assert.equal(native.catalog[0], existing);
  assert.deepEqual(native.catalog.map(item => item.id), ['af_heart', 'bm_george', 'zz_custom']);
  assert.deepEqual(groups.map(group => group.label), ['American Female', 'British Male', 'Custom voice']);
  assert.equal(initialized.length, 2);
});

test('native blend editor branches away from legacy controls and retains Convo routes', () => {
  assert.match(script, /if \(nativeBlend\) await extendNativeVoiceCatalog\(page, context\);\s*else await enhancePrimaryVoiceField/);
  assert.match(script, /if \(!nativeBlend\) \{\s*const secondary = await makeVoiceSelect/);
  assert.ok(script.includes('/plugins/_convo/speech_config'));
  assert.ok(!script.includes('/plugins/_enhanced_speech/'));
});
