// A card can render before Convo's settings store exists (the module script that creates it runs after the markup). Alpine
// evaluates each binding once at that moment; an expression that throws then is never re-run, so a button stays disabled or a
// result stays hidden for good (found live: "Test audio input" was permanently disabled). Card view state is therefore always
// read through optional chaining, and a :disabled that reads it is strictly boolean.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readdir, readFile } from 'node:fs/promises';

const webui = new URL('../webui/', import.meta.url);

test('Card markup reads view state only through optional chaining', async () => {
    for (const name of (await readdir(webui)).filter(file => file.endsWith('.html'))) {
        const html = await readFile(new URL(name, webui), 'utf8');
        assert.doesNotMatch(html, /\$store\.convoSetup\.ui\./, name + ': use $store.convoSetup?.ui?.<card>?.…');
        for (const [, expression] of html.matchAll(/:disabled="([^"]*\$store\.convoSetup\??\.?ui[^"]*)"/g))
            assert.match(expression, /=== true|^!/, name + ': a :disabled reading view state must be strictly boolean: ' + expression);
    }
});
