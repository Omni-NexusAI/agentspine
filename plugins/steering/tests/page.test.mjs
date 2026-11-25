// The page part of steering against stand-ins for Agent Zero's stores. Run: node --experimental-vm-modules --test plugins/steering/tests/page.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';

const root = new URL('..', import.meta.url);

async function page({ running = true, items = [], mode = 'automatic', answer, uploadOk = true, parent = null, composer = '' } = {}) {
    const calls = [], uploads = [], toasts = [];
    const chat = { id: 'c', running, ...(parent ? { parent_context_id: parent.id } : {}), _steering: { mode, own_mode: '', revision: 3, items } };
    const chats = { selected: 'c', selectedContext: chat, contexts: [chat, ...(parent ? [parent] : [])] };
    const input = { message: composer, resets: 0, sent: 0, sendButtonTitle: 'Add to queue', reset() { this.message = ''; this.resets++; files.attachments = []; }, adjustTextareaHeight() {}, focus() {}, async sendMessage() { this.sent++; } };
    const files = { attachments: [], addAttachment(file) { this.attachments.push(file); } };
    const bindings = {
        '/js/AlpineStore.js': { createStore: (_name, state) => state },
        '/js/api.js': {
            callJsonApi: async (endpoint, data) => { calls.push({ endpoint, ...data }); if (answer) return answer(data); return { ok: true, outcome: 'steering', steering: { mode, own_mode: '', revision: 4, items: [] } }; },
            fetchApi: async (endpoint, options) => { uploads.push({ endpoint, files: options.body.getAll('file') }); return { ok: uploadOk, json: async () => ({ filenames: options.body.getAll('file').map(f => f.name || 'x.png') }) }; },
        },
        '/components/sidebar/chats/chats-store.js': { store: chats },
        '/components/chat/input/input-store.js': { store: input },
        '/components/chat/attachments/attachmentsStore.js': { store: files },
        '/components/notifications/notification-store.js': { toastFrontendError: text => toasts.push(['error', text]), toastFrontendInfo: text => toasts.push(['info', text]) },
    };
    const context = vm.createContext({ console: { ...console, error() {} }, crypto: globalThis.crypto, FormData, queueMicrotask });
    const module = new vm.SourceTextModule(await readFile(new URL('webui/steering-store.js', root), 'utf8'), { context, identifier: 'steering-store.js' });
    await module.link(async specifier => {
        assert.ok(specifier in bindings, `unexpected import ${specifier}`);
        const exports = bindings[specifier];
        const stub = new vm.SyntheticModule(Object.keys(exports), function () { for (const [key, value] of Object.entries(exports)) this.setExport(key, value); }, { context });
        return stub;
    });
    await module.evaluate();
    return { store: module.namespace.store, calls, uploads, toasts, chats, chat, input, files };
}
const plain = value => JSON.parse(JSON.stringify(value));       // what a store returns comes from another realm
const ctx = (message, extra = {}) => ({ message, attachments: [], context: 'c', cancel: false, ...extra });
const file = name => new File(['x'], name, { type: 'image/png' });

test('while the agent works, a message goes to the server and the composer is empty', async () => {
    const p = await page({ composer: 'also the tests' });
    const sendCtx = ctx('also the tests');
    await p.store.beforeSend(sendCtx);
    assert.equal(sendCtx.cancel, true);
    assert.equal(p.calls.length, 1);
    const { id, ...call } = p.calls[0];
    assert.deepEqual(plain(call), { endpoint: '/plugins/steering/steering', action: 'send', context: 'c', text: 'also the tests', attachments: [], how: '' });
    assert.ok(id);
    assert.equal(p.input.resets, 1);
    assert.equal(p.store.items.length, 0);
});

test('an idle chat, a queue chip, an empty message and a cancelled send are left to Agent Zero', async () => {
    for (const [options, sendCtx] of [[{ running: false }, ctx('hello')], [{ mode: 'queue' }, ctx('hello')], [{}, ctx('')], [{}, ctx('hello', { cancel: true })], [{}, ctx('hello', { context: '' })]]) {
        const p = await page(options);
        const before = sendCtx.cancel;
        await p.store.beforeSend(sendCtx);
        assert.equal(sendCtx.cancel, before);
        assert.equal(p.calls.length, 0);
        assert.equal(p.input.resets, 0);
    }
});

test('a message another part of the page sends does not wipe what the user is typing', async () => {
    const p = await page({ composer: 'my unfinished thought' });
    await p.store.beforeSend(ctx('from a button'));
    assert.equal(p.calls[0].text, 'from a button');
    assert.equal(p.input.resets, 0);
    assert.equal(p.input.message, 'my unfinished thought');
});

test('attachments are uploaded first and named to the server', async () => {
    const p = await page({ composer: 'look' });
    await p.store.beforeSend(ctx('look', { attachments: [{ name: 'a.png', file: file('a.png') }] }));
    assert.equal(p.uploads[0].endpoint, '/upload');
    assert.deepEqual([...p.calls[0].attachments], ['a.png']);
});

test('only attachments, no words', async () => {
    const p = await page();
    await p.store.beforeSend(ctx('', { attachments: [{ name: 'a.png', file: file('a.png') }] }));
    assert.equal(p.calls[0].text, '');
    assert.deepEqual([...p.calls[0].attachments], ['a.png']);
});

test('a message that could not be sent is back in the composer, with its files', async () => {
    const p = await page({ composer: 'keep me', answer: () => { throw new Error('offline'); } });
    p.files.attachments = [{ name: 'a.png', file: file('a.png') }];
    await p.store.beforeSend(ctx('keep me', { attachments: [{ name: 'a.png', file: file('a.png') }] }));
    assert.equal(p.input.message, 'keep me');
    assert.equal(p.files.attachments.length, 1);
    assert.equal(p.toasts[0][0], 'error');
    assert.equal(p.store.sending.length, 0);
});

test('an upload that fails sends nothing', async () => {
    const p = await page({ composer: 'look', uploadOk: false });
    await p.store.beforeSend(ctx('look', { attachments: [{ name: 'a.png', file: file('a.png') }] }));
    assert.equal(p.calls.length, 0);
    assert.equal(p.input.message, 'look');
});

test('a failed message never overwrites what was typed meanwhile, and is not lost either', async () => {
    let p, offline = true;
    p = await page({ composer: 'first', answer: data => { if (!offline) return { ok: true, outcome: 'steering', steering: { mode: 'automatic', revision: 5, items: [] } }; p.input.message = 'second'; throw new Error('offline'); } });
    p.files.attachments = [{ name: 'a.png', file: file('a.png') }];
    await p.store.beforeSend(ctx('first', { attachments: [{ name: 'a.png', file: file('a.png') }] }));
    assert.equal(p.input.message, 'second');
    assert.match(p.toasts[0][1], /kept above the composer/);
    assert.deepEqual(plain(p.store.items.map(item => [item.text, item.attachments, item.status, item.local])), [['first', ['a.png'], 'held', true]]);
    assert.deepEqual([p.store.heldCount, p.store.title, p.store.showChip], [1, 'Not sent', true]);
    offline = false;
    await p.store.sendNow(p.store.items[0]);                        // tried again, with its file
    assert.deepEqual([p.calls.at(-1).action, p.calls.at(-1).text, [...p.calls.at(-1).attachments], p.store.items.length], ['send', 'first', ['a.png'], 0]);
    assert.equal(p.input.message, 'second');
});

test('a message that had arrived though its answer was lost shows once, and sending it again says so', async () => {
    let p, first = true;
    p = await page({ composer: 'first', answer: data => {
        if (first) { first = false; p.input.message = 'typed on'; p.sent = data.id; throw new Error('the answer was lost'); }
        return { ok: true, outcome: 'known', steering: { mode: 'automatic', revision: 6, items: [] } };
    } });
    await p.store.beforeSend(ctx('first'));
    assert.deepEqual(plain(p.store.items.map(item => [item.text, item.local])), [['first', true]]);
    p.chat._steering = { mode: 'automatic', own_mode: '', revision: 5, items: [{ id: p.sent, text: 'first', status: 'waiting', mode: 'automatic', attachments: [] }] };
    assert.deepEqual(plain(p.store.items.map(item => [item.text, item.status, !!item.local])), [['first', 'waiting', false]]);      // the server has it
    p.chat._steering = { mode: 'automatic', own_mode: '', revision: 6, items: [] };                     // and it is in the run by now
    await p.store.sendNow(p.store.items[0]);
    assert.deepEqual([p.calls.at(-1).id, p.store.items.length], [p.sent, 0]);                           // the same id: never a second message
    assert.match(p.toasts.at(-1)[1], /had arrived/);
});

test('a message that failed after the chat was changed waits in its own chat', async () => {
    let p;
    const other = { id: 'd', running: true, _steering: { mode: 'automatic', own_mode: '', revision: 1, items: [] } };
    p = await page({ composer: 'for c', answer: () => { p.chats.selected = 'd'; p.chats.selectedContext = other; throw new Error('offline'); } });
    await p.store.beforeSend(ctx('for c'));
    assert.deepEqual([p.store.items.length, p.input.message], [0, '']);           // not shown, nor put into the composer, where it does not belong
    p.chats.selected = 'c'; p.chats.selectedContext = p.chat;
    assert.deepEqual(plain(p.store.items.map(item => item.text)), ['for c']);
    p.input.message = 'typed on';
    await p.store.backToComposer(p.store.items[0]);
    assert.deepEqual([p.input.message, p.store.items.length, p.calls.length], ['typed on\nfor c', 0, 1]);
    p.chats.selected = 'd'; p.chats.selectedContext = other;
    await p.store.deliver({ context: 'c', message: 'from a button' });             // one that never was in the composer
    p.chats.selected = 'c'; p.chats.selectedContext = p.chat;
    await p.store.remove(p.store.items[0]);
    assert.deepEqual([p.store.items.length, p.calls.filter(call => call.action === 'remove').length], [0, 0]);
});

test('it shows as sending until the server has it, and never twice', async () => {
    let seen, p;
    p = await page({ composer: 'x', answer: data => { seen = p.store.items.map(item => [item.text, item.status]); return { ok: true, outcome: 'steering', steering: { mode: 'automatic', revision: 5, items: [{ id: data.id, text: 'x', status: 'waiting', mode: 'automatic', attachments: [] }] } }; } });
    await p.store.beforeSend(ctx('x'));
    assert.deepEqual(plain(seen), [['x', 'sending']]);
    assert.deepEqual(plain(p.store.items.map(item => [item.text, item.status])), [['x', 'waiting']]);
});

test('a newer snapshot wins over what was read, an older one does not', async () => {
    const p = await page();
    p.store.keep({ mode: 'steer', revision: 9, items: [] });
    assert.equal(p.store.mode, 'steer');
    p.chat._steering = { mode: 'queue', revision: 10, items: [] };
    assert.equal(p.store.mode, 'queue');
});

test('a sub-agent\'s chat counts as at work while its superior runs, and the server decides', async () => {
    const p = await page({ running: false, parent: { id: 'root', running: true } });
    assert.equal(p.store.atWork(), true);
    const sendCtx = ctx('use the other file');
    await p.store.beforeSend(sendCtx);
    assert.equal(sendCtx.cancel, true);
    const q = await page({ running: false, parent: { id: 'root', running: true }, mode: 'queue' });
    const other = ctx('x');
    await q.store.beforeSend(other);
    assert.equal(other.cancel, true);        // such a chat has no queue that would ever be sent
});

const key = (extra = {}) => ({ key: 'Enter', target: { closest: selector => selector === '#chat-input' ? {} : null }, prevented: 0, stopped: 0, preventDefault() { this.prevented++; }, stopImmediatePropagation() { this.stopped++; }, ...extra });

test('Ctrl+Enter steers and Alt+Enter queues, whatever the chip says', async () => {
    const p = await page({ composer: 'x', mode: 'queue' });
    const steer = key({ ctrlKey: true });
    assert.equal(p.store.onKey(steer), true);
    assert.deepEqual([steer.prevented, steer.stopped, p.input.sent, p.store.nextHow], [1, 1, 1, 'steer']);
    await p.store.beforeSend(ctx('x'));
    assert.equal(p.calls[0].how, 'steer');
    const later = key({ altKey: true });
    p.input.message = 'y';
    assert.equal(p.store.onKey(later), true);
    const sendCtx = ctx('y');
    await p.store.beforeSend(sendCtx);
    assert.equal(sendCtx.cancel, false);     // Agent Zero's own queue
    assert.equal(p.store.nextHow, '');       // used once
});

test('other keys, other places, idle chats and empty composers are left alone', async () => {
    const p = await page({ composer: 'x' });
    for (const event of [key(), key({ ctrlKey: true, altKey: true }), key({ ctrlKey: true, shiftKey: true }), key({ ctrlKey: true, isComposing: true }), key({ key: 'a', ctrlKey: true }), key({ ctrlKey: true, target: { closest: () => null } })])
        assert.equal(p.store.onKey(event), false);
    assert.equal((await page({ composer: 'x', running: false })).store.onKey(key({ ctrlKey: true })), false);
    assert.equal((await page({ composer: ' ' })).store.onKey(key({ ctrlKey: true })), false);
});

test('the strip\'s buttons', async () => {
    const item = { id: 'm', text: 'x', status: 'waiting', mode: 'automatic', attachments: [] };
    const p = await page({ items: [item] });
    await p.store.interruptNow(item); await p.store.afterTheTask(item); await p.store.sendNow(item); await p.store.remove(item);
    assert.deepEqual(p.calls.map(call => [call.action, call.id]), [['interrupt', 'm'], ['queue', 'm'], ['release', 'm'], ['remove', 'm']]);
});

test('a message that went in meanwhile says so', async () => {
    const p = await page({ answer: () => ({ ok: false, steering: { mode: 'automatic', revision: 8, items: [] } }) });
    await p.store.remove({ id: 'm' });
    assert.deepEqual(p.toasts, [['info', 'That message is already in the run.']]);
});

test('back to the composer keeps what is there and names files that must be added again', async () => {
    const p = await page({ composer: 'typed already', answer: () => ({ ok: true, item: { text: 'the waiting one', attachments: ['a.png'] }, steering: { mode: 'automatic', revision: 8, items: [] } }) });
    await p.store.backToComposer({ id: 'm' });
    assert.equal(p.input.message, 'typed already\nthe waiting one');
    assert.match(p.toasts[0][1], /a\.png/);
});

test('the chip saves this chat\'s choice and goes back when that fails', async () => {
    const p = await page({ answer: data => ({ ok: true, steering: { mode: data.mode, own_mode: data.mode, revision: 5, items: [] } }) });
    await p.store.setMode('queue');
    assert.deepEqual([p.calls[0].action, p.calls[0].mode, p.store.mode], ['mode', 'queue', 'queue']);
    const q = await page({ answer: () => { throw new Error('offline'); } });
    await q.store.setMode('queue');
    assert.equal(q.store.mode, 'automatic');
    assert.equal(q.toasts[0][0], 'error');
    await q.store.setMode('sometimes');
    assert.equal(q.calls.length, 1);
});

test('words: the title, the state and the send button', async () => {
    const held = { id: 'h', text: 'x', status: 'held', mode: 'automatic', attachments: [] }, waiting = { id: 'w', text: 'y', status: 'waiting', mode: 'steer', attachments: [] };
    const p = await page({ items: [waiting] });
    assert.equal(p.store.title, 'Goes in at the next step');
    assert.equal(p.store.itemState(waiting), 'steers');
    assert.equal(p.store.buttonTitle('Add to queue'), 'Send: goes in at the next step');
    assert.equal(p.store.buttonTitle('Stop agent'), 'Stop agent');
    const q = await page({ items: [held], running: false });
    assert.equal(q.store.title, 'Not sent: the run ended first');
    assert.equal(q.store.showChip, true);
    assert.equal((await page({ mode: 'queue' })).store.buttonTitle('Add to queue'), 'Add to queue');
    assert.equal((await page({ running: false })).store.showChip, false);
    assert.equal(p.store.itemText({ text: '', attachments: ['a.png'] }), 'a.png');
});

test('a chat is read once, so messages kept over a restart show', async () => {
    const p = await page({ running: false, answer: () => ({ ok: true, steering: { mode: 'automatic', own_mode: '', revision: 0, items: [{ id: 'h', text: 'kept', status: 'held', mode: 'automatic', attachments: [] }] } }) });
    delete p.chat._steering;
    await p.store.refresh();
    assert.deepEqual(plain(p.store.items.map(item => item.text)), ['kept']);
});

test('the markup uses only what the store offers, and every button has a name', async () => {
    const strip = await readFile(new URL('extensions/webui/chat-input-start/steering-strip.html', root), 'utf8');
    const chip = await readFile(new URL('extensions/webui/chat-input-bottom-actions-end/steering-chip.html', root), 'utf8');
    const { store } = await page();
    for (const [, name] of (strip + chip).matchAll(/\$store\.steering\??\.([A-Za-z]+)/g)) assert.ok(name in store, name);
    for (const button of strip.match(/<button[^>]*>/g)) assert.match(button, /aria-label="/);
    assert.match(chip, /<select aria-label="/);
});
