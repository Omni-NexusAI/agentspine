// The browser copy of the Markdown cleaner (webui/plain-text.js, the live draft) against the table the server's copy uses
// (tests/fixtures/plain-text-cases.json; test_plain_text.py reads it too).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { plainText } from '../webui/plain-text.js';

const { cases } = JSON.parse(await readFile(new URL('./fixtures/plain-text-cases.json', import.meta.url), 'utf8'));

test('Every case in the shared table gives exactly what the server gives', () => {
    for (const item of cases) assert.equal(plainText(item.in, { lineStart: item.line_start ?? true }), item.out, item.name);
});

test('A draft still arriving shows no markup that is already complete', () => {
    assert.equal(plainText('**Saturn** has'), 'Saturn has');
    assert.equal(plainText('**Saturn'), 'Saturn');                      // a marker whose partner has not arrived yet is not shown either
    assert.equal(plainText(undefined), '');
});
