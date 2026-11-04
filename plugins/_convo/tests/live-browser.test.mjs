// The pure parts of the live-check tooling. The live checks themselves (live-ui-smoke.mjs) need a browser and an isolated host.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { spawn } from 'node:child_process';
import { assertLoopback, contrast, launch, stopTree } from './live-browser.mjs';

test('A browser that handed over to another process is still stopped, by its private profile folder', { skip: process.platform !== 'win32' }, async () => {
    const profile = `convo-headless-test-${process.pid}-${Date.now()}`;
    // stands in for the real browser process: not the pid we "spawned", but started with the run's private profile in its arguments
    const survivor = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 60000)', '--', `--user-data-dir=${profile}`], { stdio: 'ignore', windowsHide: true });
    await new Promise(resolve => setTimeout(resolve, 600));
    const alive = pid => { try { process.kill(pid, 0); return true; } catch { return false; } };
    assert.equal(alive(survivor.pid), true);
    stopTree({ pid: 0, kill() {} }, profile);
    for (let i = 0; i < 30 && alive(survivor.pid); i++) await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(alive(survivor.pid), false, 'the process started with this profile should be gone');
});

test('A port that already answers as a DevTools endpoint is refused, never attached to', async () => {
    // A leftover test browser would answer in place of the one this script starts, with a stale profile and a consumed scripted
    // microphone; that once made a healthy page look broken ("Permission denied").
    const server = createServer((request, response) => { response.writeHead(200, { 'Content-Type': 'application/json' }); response.end('{"Browser":"stale"}'); });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    try {
        await assert.rejects(() => launch({ port: server.address().port }), /refusing to attach to a browser this script did not start/);
    } finally { await new Promise(resolve => server.close(resolve)); }
});

test('Live checks open only a loopback test host', () => {
    for (const ok of ['http://127.0.0.1:5028/', 'http://localhost:5028', 'http://[::1]:8080/x']) assert.doesNotThrow(() => assertLoopback(ok), ok);
    for (const bad of ['https://example.com/', 'http://192.168.1.10:5027/', 'http://10.0.0.5/', 'http://0.0.0.0:5027/', 'file:///C:/x.html', 'http://127.0.0.1.evil.test/', 'http://localhost.evil.test:80/'])
        assert.throws(() => assertLoopback(bad), /loopback/, bad);
});

test('The contrast ratio is the WCAG one', () => {
    assert.equal(Math.round(contrast('rgb(0, 0, 0)', 'rgb(255, 255, 255)') * 10) / 10, 21);
    assert.equal(contrast('rgb(255, 255, 255)', 'rgb(255, 255, 255)'), 1);
    assert.ok(contrast('rgb(255, 255, 255)', 'rgb(43, 90, 185)') > 4.5);               // the dark-mode primary button
    assert.ok(contrast('rgb(255, 255, 255)', 'rgb(37, 99, 235)') > 4.5);               // the light-mode primary button
    assert.ok(contrast('rgb(51, 51, 51)', 'rgb(43, 90, 185)') < 3);                    // the old dark-on-blue combination
    assert.equal(contrast('rgba(10, 20, 30, 0.5)', 'rgb(0, 0, 0)') > 1, true);         // alpha is ignored, not misparsed
});
