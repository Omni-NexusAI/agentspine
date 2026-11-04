// The browser copy of the connection rules (webui/connection-rules.js) against the one table the server copies use
// (tests/fixtures/connection-rules-cases.json; test_connection_rules.py and the decision plugin's tests read it too).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { credentialReferenceProblem, isLocalHost, NETWORK_HINT } from '../webui/connection-rules.js';

const cases = JSON.parse(await readFile(new URL('./fixtures/connection-rules-cases.json', import.meta.url), 'utf8'));

test('Your network (this machine, LAN, Tailscale) is local, everything else public, as on the server', () => {
    for (const host of cases.local_hosts) assert.equal(isLocalHost(host), true, host);
    for (const host of cases.public_hosts) assert.equal(isLocalHost(host), false, host);
    assert.equal(isLocalHost('[fd7a:115c:a1e0::1]'), true);                     // a URL parser's IPv6 form
});

test('A credential reference names a variable; the value is never repeated', () => {
    for (const name of cases.references_ok) assert.equal(credentialReferenceProblem(name), '', name);
    for (const [name, hint] of cases.references_bad) {
        const problem = credentialReferenceProblem(name);
        assert.match(problem, /name of an environment variable/);
        assert.ok(problem.includes(hint), name);
        assert.ok(!problem.includes(name), name);
    }
});

test('The network hint names both LAN and Tailscale', () => {
    assert.match(NETWORK_HINT, /LAN or Tailscale/);
});
