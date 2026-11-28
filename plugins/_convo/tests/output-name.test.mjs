// The saved output device is shown by its own name (2026-11-25: an option read "The chosen device" until the list was clicked).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { normalizeChoices } from '../webui/sound-setup.js';

const read = name => readFileSync(new URL('../webui/' + name, import.meta.url), 'utf8');

test('The output choice keeps the device name, and no option is a placeholder for it', () => {
    assert.deepEqual(normalizeChoices({ output: 'abc', output_label: 'Headset (USB)' }), { output: 'abc', output_label: 'Headset (USB)', noise_suppression: true, auto_gain: true, phone_mode: 'auto' });
    assert.equal(normalizeChoices({ output: '', output_label: 'Headset' }).output_label, '');      // no name without a choice
    assert.equal(normalizeChoices({ output: 'abc', output_label: 'x'.repeat(200) }).output_label.length, 80);
    const panel = read('sound-panel.html'), store = read('convo-store.js');
    assert.doesNotMatch(panel + store, /The chosen device|the chosen output device|chosen device gone/);
    assert.match(panel, /x-init="\$store\.convoSound\.listOutputs\(\)"/);                            // read when the panel shows, not on a click
    assert.match(panel, /x-text="\$store\.convoSound\.savedOutputLabel\(\)"/);
    assert.doesNotMatch(panel, /class="convo-sound-check"|accent-color:var\(--color-text\);min-height/);   // Agent Zero's switch and slider
    assert.equal((panel.match(/<label class="toggle">/g) || []).length, 2);
});
