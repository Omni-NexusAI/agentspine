// A throwaway headless browser for checking Convo's real interface on an ISOLATED test host. Node built-ins only
// (fetch + WebSocket over the Chrome DevTools Protocol); nothing is installed.
//
// Safety, by construction:
//   * the browser is its own process with a private temporary profile and muted audio: it never touches the user's
//     browser, profile, sessions or the desktop app's Browser pane;
//   * it will only open a loopback address (localhost / 127.0.0.1), never another host;
//   * close() stops it and removes the profile. Callers must close it, also on failure.
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CANDIDATES = [
    process.env.CONVO_LIVE_BROWSER,
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
].filter(Boolean);
export const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// Only the machine's own loopback is acceptable: an isolated test host, never a remote service or someone's account.
export function assertLoopback(url) {
    const parsed = new URL(url);
    if (!['http:', 'https:'].includes(parsed.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname)) {
        throw new Error(`Refusing to open ${parsed.origin}: live checks run only against a loopback test host.`);
    }
    return parsed.href;
}

// WCAG contrast ratio of two CSS colours given as "rgb(r, g, b)" / "rgba(r, g, b, a)" strings (alpha ignored).
export function contrast(a, b) {
    const luminance = css => {
        const [r, g, bl] = css.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
        return .2126 * r + .7152 * g + .0722 * bl;
    };
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + .05) / (lo + .05);
}

// Stops a browser and everything it started. On Windows the process this script spawns can hand over to a different browser
// process and exit, so `child.kill()` (or killing its tree) leaves the real browser holding the debugging port; later runs then
// attached to a stale browser (a profile already deleted, a consumed scripted microphone) and failed in ways that looked like the
// page's fault ("Permission denied" for a microphone that was fine). So: ask the browser to close itself over the DevTools
// protocol, then stop whatever is left that was started with THIS run's private profile folder (a unique temporary path).
export function stopTree(child, profile = '') {
    if (child.pid) {
        if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
        else child.kill();
    }
    if (profile && process.platform === 'win32') {
        spawnSync('powershell', ['-NoProfile', '-Command', 'Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($env:CONVO_PROFILE) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }'],
            { stdio: 'ignore', windowsHide: true, env: { ...process.env, CONVO_PROFILE: profile } });
    }
}

async function closeBrowser(port, child, profile) {
    try {
        const info = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
        await new Promise(resolve => { const socket = new WebSocket(info.webSocketDebuggerUrl); socket.onopen = () => { socket.send(JSON.stringify({ id: 1, method: 'Browser.close' })); setTimeout(resolve, 600); }; socket.onerror = resolve; socket.onclose = resolve; });
    } catch { /* already gone */ }
    stopTree(child, profile);
}

export async function launch({ port = 9333, width = 1400, height = 900, extraArgs = [] } = {}) {
    const exe = CANDIDATES.find(path => existsSync(path));
    if (!exe) throw new Error('No Chromium-based browser found; set CONVO_LIVE_BROWSER to its executable.');
    // Never attach to a browser this script did not start: a stale one would answer the DevTools port in its place.
    const occupied = await fetch(`http://127.0.0.1:${port}/json/version`).then(response => response.ok, () => false);
    if (occupied) throw new Error(`Port ${port} already answers as a DevTools endpoint (a leftover test browser?). Stop it, or choose another port; refusing to attach to a browser this script did not start.`);
    const profile = mkdtempSync(join(tmpdir(), 'convo-headless-'));
    const child = spawn(exe, [
        '--headless=new', `--remote-debugging-port=${port}`, '--remote-debugging-address=127.0.0.1', `--user-data-dir=${profile}`,
        '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--disable-background-networking',
        '--mute-audio', `--window-size=${width},${height}`, '--force-device-scale-factor=1', ...extraArgs, 'about:blank',
    ], { stdio: 'ignore', windowsHide: true });
    let up = false;
    for (let i = 0; i < 100 && !up; i++) {
        try { up = (await fetch(`http://127.0.0.1:${port}/json/version`)).ok; } catch { await sleep(150); }
    }
    if (!up) { stopTree(child, profile); throw new Error('The headless browser did not start.'); }
    return {
        child, profile, port,
        async page(url) {
            const target = await (await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(assertLoopback(url))}`, { method: 'PUT' })).json();
            return connect(target);
        },
        async close() {
            await closeBrowser(port, child, profile);
            await sleep(500);
            try { rmSync(profile, { recursive: true, force: true }); } catch { /* a locked file is removed with the temp folder later */ }
        },
    };
}


async function connect(target) {
    const socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    let nextId = 1;
    const pending = new Map(), logs = [], listeners = [];
    socket.onmessage = event => {
        const message = JSON.parse(event.data);
        if (message.id && pending.has(message.id)) {
            const { resolve, reject } = pending.get(message.id); pending.delete(message.id);
            message.error ? reject(new Error(message.error.message)) : resolve(message.result);
        } else if (message.method) {
            if (message.method === 'Runtime.consoleAPICalled') logs.push({ type: message.params.type, text: message.params.args.map(arg => arg.value ?? arg.description ?? '').join(' ') });
            if (message.method === 'Runtime.exceptionThrown') logs.push({ type: 'exception', text: message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text });
            for (const listener of listeners) listener(message);
        }
    };
    const send = (method, params = {}) => new Promise((resolve, reject) => {
        const id = nextId++; pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
    });
    await send('Page.enable'); await send('Runtime.enable');
    const page = {
        logs, send,
        on: listener => listeners.push(listener),
        // Confirm dialogs are answered by the caller's policy (default: decline); alerts are always dismissed.
        answerDialogs(accept = () => false) {
            page.on(message => {
                if (message.method === 'Page.javascriptDialogOpening') page.send('Page.handleJavaScriptDialog', { accept: !!accept(message.params) }).catch(() => {});
            });
        },
        async eval(expression, timeout = 60000) {
            const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, timeout });
            if (result.exceptionDetails) throw new Error((result.exceptionDetails.exception?.description || result.exceptionDetails.text) + '\n--- in: ' + expression.slice(0, 200));
            return result.result.value;
        },
        async waitFor(expression, timeout = 20000, step = 150) {
            const end = Date.now() + timeout;
            while (Date.now() < end) {
                try { const value = await page.eval(expression); if (value) return value; } catch { /* the page may still be loading */ }
                await sleep(step);
            }
            throw new Error('Timed out waiting for: ' + expression.slice(0, 160));
        },
        async viewport({ width = 1400, height = 900, dark = false, reduced = false, mobile = false } = {}) {
            await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile });
            await send('Emulation.setEmulatedMedia', { features: [
                { name: 'prefers-color-scheme', value: dark ? 'dark' : 'light' },
                { name: 'prefers-reduced-motion', value: reduced ? 'reduce' : 'no-preference' }] });
            await sleep(200);
        },
        async shot(path, clip) {
            const { data } = await send('Page.captureScreenshot', { format: 'png', ...(clip ? { clip: { scale: 1, ...clip } } : {}) });
            writeFileSync(path, Buffer.from(data, 'base64'));
            return path;
        },
        async click(selector) {
            const point = await page.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e) return null; e.scrollIntoView({block:'center'}); const r = e.getBoundingClientRect(); return {x: r.x + r.width/2, y: r.y + r.height/2}; })()`);
            if (!point) throw new Error('No element: ' + selector);
            for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) await send('Input.dispatchMouseEvent', { type, x: point.x, y: point.y, button: 'left', clickCount: 1 });
            await sleep(120);
        },
        async key(key) {
            for (const type of ['keyDown', 'keyUp']) await send('Input.dispatchKeyEvent', { type, key, code: key, windowsVirtualKeyCode: key === 'Escape' ? 27 : key === 'Tab' ? 9 : 0 });
            await sleep(80);
        },
        problems: () => logs.filter(entry => ['error', 'exception', 'warning'].includes(entry.type)),
        close() { try { socket.close(); } catch { /* already closed */ } },
    };
    return page;
}
