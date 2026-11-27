import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { CARD_PATHS, THRESHOLD, conversationSetup, thresholdInEffect } from '../webui/conversation-setup.js';

const read = name => readFile(new URL('../webui/' + name, import.meta.url), 'utf8');

test('The compaction threshold is one of the conversation card\u2019s settings, bounded like the server bounds it', async () => {
    assert.deepEqual(THRESHOLD, { min: 0.3, max: 0.9, fallback: 0.7 });
    const server = await readFile(new URL('../helpers/convo_memory.py', import.meta.url), 'utf8');
    assert.ok(server.includes('TRIGGER_MIN, TRIGGER_MAX, TRIGGER_DEFAULT = 0.3, 0.9, 0.7'));               // the same numbers on both sides
    assert.equal(CARD_PATHS['compaction.trigger'], 'Summarise older conversation at');
    for (const [value, expected] of [[0.7, 0.7], [0.3, 0.3], [0.05, 0.3], [4, 0.9], ['0.5', 0.5], [null, 0.7], [undefined, 0.7], ['x', 0.7], [NaN, 0.7], [0, 0.7], [-2, 0.7], [true, 0.7]])
        assert.equal(thresholdInEffect(value), expected, String(value));
    const text = config => conversationSetup.conversationThresholdText.call({ config });
    assert.deepEqual([text({ compaction: { trigger: 0.55 } }), text({ compaction: {} }), text(null)], ['55% full', '70% full', '70% full']);
});

test('The card offers it as a range, like Agent Zero\u2019s history space, and marks the change for autosave', async () => {
    const card = await read('conversation-model-field.html');
    const field = card.slice(card.indexOf("x-id=\"['convo-compact']\""), card.indexOf("x-id=\"['convo-audio']\""));
    assert.ok(field.includes('Summarise older conversation at'));
    assert.ok(field.includes('type="range" min="0.3" max="0.9" step="0.05"'));
    assert.ok(field.includes('x-model.number="$store.convoSetup.config.compaction.trigger"'));
    assert.ok(field.includes("@change=\"$store.convoSetup.conversationEdited('compaction.trigger', autosave)\""));
    assert.ok(field.includes('class="range-value" x-text="$store.convoSetup.conversationThresholdText()"'));
    assert.ok(card.indexOf("x-id=\"['convo-compact']\"") > card.indexOf("x-id=\"['convo-context']\""));   // right under the context window
});
