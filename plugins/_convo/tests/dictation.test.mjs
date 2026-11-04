import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { CLICK_WINDOW_MS, LATCH_MS, PRE_OPEN_MS, PUSH_TO_TALK_MS, WATCHDOG_MS, continuousDraft, createGesture, dictationEndNote, orbKey } from '../webui/dictation.js';
import { ORB_STATES, announce, cueFor, orbView, resetAnnouncer } from '../webui/orb.js';

// ---- the gesture machine, on a virtual clock -------------------------------------------------------------------------
function rig(options = {}) {
    let now = 0, next = 1;
    const timers = new Map(), effects = [];
    const clock = {
        get now() { return now; },
        get pending() { return timers.size; },
        set: (fn, ms) => { const id = next++; timers.set(id, { at: now + ms, fn }); return id; },
        clear: id => timers.delete(id),
        advance(ms) {
            const target = now + ms;
            for (;;) {
                const due = [...timers].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
                if (!due) break;
                timers.delete(due[0]); now = due[1].at; due[1].fn();
            }
            now = target;
        },
    };
    const gesture = createGesture({ onEffect: effect => effects.push(effect), setTimer: clock.set, clearTimer: clock.clear, ...options });
    const all = () => effects.map(effect => effect.type);
    const types = () => all().filter(type => type !== 'preopen' && type !== 'discard');      // the buffer effects have their own tests below
    return { gesture, clock, effects, types, all, clear: () => { effects.length = 0; } };
}

test('The provisional timings are exported constants, ordered, and validated', () => {
    assert.equal(LATCH_MS, 2000);
    assert.equal(PUSH_TO_TALK_MS, 4000);
    assert.ok(LATCH_MS < PUSH_TO_TALK_MS);
    assert.equal(PRE_OPEN_MS, 300);
    assert.ok(PRE_OPEN_MS < LATCH_MS);
    assert.throws(() => createGesture({ latchMs: 3000, pushToTalkMs: 3000 }), RangeError);
    assert.throws(() => createGesture({ latchMs: 0 }), RangeError);
    assert.throws(() => createGesture({ preOpenMs: 2000, latchMs: 2000 }), RangeError);
    const custom = rig({ latchMs: 500, pushToTalkMs: 900 });           // changing the constants is the only change needed
    custom.gesture.press(); custom.clock.advance(500);
    assert.deepEqual(custom.types(), ['press', 'arm']);
    custom.clock.advance(400);
    assert.deepEqual(custom.types(), ['press', 'arm', 'push-to-talk']);
});

test('A short press is a tap, once, and the click that follows is swallowed once', () => {
    const { gesture, clock, types } = rig();
    gesture.press(); clock.advance(300);
    assert.equal(gesture.phase, 'pressed');
    gesture.release();
    assert.deepEqual(types(), ['press', 'tap']);
    assert.equal(gesture.phase, 'idle');
    assert.equal(gesture.consumeClick(), true);                        // the browser's click after the release does not toggle Convo again
    assert.equal(gesture.consumeClick(), false);
    assert.equal(clock.pending, 0);                                    // nothing left running
});

test('Release one millisecond before the latch time is a tap; at the latch time it is latched dictation', () => {
    const early = rig();
    early.gesture.press(); early.clock.advance(LATCH_MS - 1); early.gesture.release();
    assert.deepEqual(early.types(), ['press', 'tap']);
    const exact = rig();
    exact.gesture.press(); exact.clock.advance(LATCH_MS);
    assert.deepEqual(exact.types(), ['press', 'arm']);
    assert.equal(exact.gesture.phase, 'armed');
    exact.gesture.release();
    assert.deepEqual(exact.types(), ['press', 'arm', 'latch']);
});

test('Holding past the latch time and releasing before push-to-talk leaves dictation latched after release', () => {
    const { gesture, clock, types } = rig();
    gesture.press(); clock.advance(LATCH_MS + 800);
    gesture.release();
    assert.deepEqual(types(), ['press', 'arm', 'latch']);
    assert.equal(gesture.phase, 'latched');
    assert.equal(gesture.dictating, true);
    assert.equal(gesture.holding, false);
    assert.equal(gesture.consumeClick(), true);                        // the release-click does not toggle Convo
    clock.advance(60_000);
    assert.deepEqual(types(), ['press', 'arm', 'latch']);              // and nothing ends it by itself
    assert.equal(clock.pending, 0);
});

test('Holding past the push-to-talk time listens only while held; release finishes and delivers once', () => {
    const { gesture, clock, effects, types } = rig();
    gesture.press(); clock.advance(PUSH_TO_TALK_MS);
    assert.deepEqual(types(), ['press', 'arm', 'push-to-talk']);
    assert.equal(gesture.phase, 'ptt');
    clock.advance(9000);                                               // a very long hold stays push-to-talk
    gesture.release();
    assert.deepEqual(types(), ['press', 'arm', 'push-to-talk', 'finish']);
    assert.deepEqual(effects.at(-1), { type: 'finish', reason: 'release' });
    assert.equal(gesture.phase, 'idle');
    assert.equal(gesture.consumeClick(), true);
    assert.equal(gesture.release(), false);                            // a second release does nothing
    assert.equal(types().filter(type => type === 'finish').length, 1);
});

test('A second press while dictation is latched stops it and never starts a second capture', () => {
    const { gesture, clock, effects, types, clear } = rig();
    gesture.press(); clock.advance(LATCH_MS); gesture.release(); gesture.consumeClick(); clear();
    assert.equal(gesture.phase, 'latched');
    gesture.press('pointer');
    assert.deepEqual(effects, [{ type: 'finish', reason: 'press' }]);   // finished at once: no arm, no tap
    assert.equal(gesture.dictating, false);
    clock.advance(10_000);                                             // holding this press down does not start anything
    assert.deepEqual(types(), ['finish']);
    gesture.release();
    assert.deepEqual(types(), ['finish']);                             // and its release is not a tap
    assert.equal(gesture.consumeClick(), true);                        // nor is its click
    assert.equal(gesture.phase, 'idle');
    clear(); gesture.press(); clock.advance(100); gesture.release();    // the next press is a normal gesture again
    assert.deepEqual(types(), ['press', 'tap']);
});

test('A second press while the first is still held starts nothing: no duplicate capture', () => {
    const { gesture, clock, types } = rig();
    gesture.press('a'); clock.advance(LATCH_MS);
    assert.equal(gesture.press('b'), false);                           // a second finger or key during dictation
    clock.advance(PUSH_TO_TALK_MS);
    assert.equal(types().filter(type => type === 'arm').length, 1);
    gesture.release('b');                                              // its release is not the holder's
    assert.equal(gesture.phase, 'ptt');
    gesture.release('a');
    assert.deepEqual(types(), ['press', 'arm', 'push-to-talk', 'finish']);
});

test('A second finger during the decision window makes it ambiguous: neither a tap nor dictation', () => {
    const { gesture, clock, types, effects } = rig();
    gesture.press('a'); clock.advance(500);
    gesture.press('b');
    assert.deepEqual(effects.at(-1), { type: 'abort', reason: 'multi-touch' });
    clock.advance(LATCH_MS * 3);
    assert.equal(types().includes('arm'), false);                      // the timers died with the gesture
    gesture.release('b'); gesture.release('a');
    assert.equal(types().includes('tap'), false);
    assert.equal(gesture.phase, 'idle');
    assert.equal(gesture.consumeClick(), true);                        // the click after the real release is still swallowed
    assert.equal(rigNonPrimary(), true);
});

function rigNonPrimary() {
    const { gesture, types } = rig();
    assert.equal(gesture.press('touch-2', { primary: false }), false); // a non-primary pointer never starts a gesture
    return types().length === 0 && gesture.phase === 'idle';
}

test('pointercancel, blur and a hidden page never tap and never send', () => {
    for (const reason of ['pointercancel', 'blur', 'hidden']) {
        // 1. before the decision
        const before = rig();
        before.gesture.press(); before.clock.advance(800); before.gesture.cancel(reason);
        assert.deepEqual(before.types(), ['press', 'abort'], reason);
        before.clock.advance(PUSH_TO_TALK_MS * 2);
        assert.equal(before.types().includes('arm'), false, reason);
        // 2. after the latch time, still held: dictation that this press started is discarded
        const armed = rig();
        armed.gesture.press(); armed.clock.advance(LATCH_MS + 10); armed.gesture.cancel(reason);
        assert.deepEqual(armed.types(), ['press', 'arm', 'cancel'], reason);
        assert.equal(armed.effects.at(-1).reason, reason);
        // 3. after the push-to-talk time
        const ptt = rig();
        ptt.gesture.press(); ptt.clock.advance(PUSH_TO_TALK_MS + 10); ptt.gesture.cancel(reason);
        assert.deepEqual(ptt.types(), ['press', 'arm', 'push-to-talk', 'cancel'], reason);
        assert.equal(ptt.types().includes('finish'), false, reason);
        assert.equal(ptt.clock.pending <= 1, true, reason);                     // at most the watchdog
        if (reason === 'pointercancel') {
            assert.equal(ptt.gesture.phase, 'idle');
            assert.equal(ptt.gesture.consumeClick(), false);                    // the browser sends no click after a pointercancel
        } else {
            // the finger / key may still come up over the page: that release is swallowed, never a tap or a send
            assert.equal(ptt.gesture.phase, 'void', reason);
            ptt.gesture.release();
            assert.equal(ptt.gesture.phase, 'idle', reason);
            assert.equal(ptt.gesture.consumeClick(), true, reason);
            assert.equal(ptt.types().includes('tap') || ptt.types().includes('finish'), false, reason);
        }
    }
    const latched = rig();
    latched.gesture.press(); latched.clock.advance(LATCH_MS); latched.gesture.release();
    assert.equal(latched.gesture.cancel('blur'), false);               // hands-free dictation survives losing focus or a hidden tab
    assert.equal(latched.gesture.phase, 'latched');
});

test('Escape abandons a press, a hold or latched dictation without ever sending', () => {
    const pressing = rig();
    pressing.gesture.press(); pressing.clock.advance(400); pressing.gesture.escape();
    pressing.gesture.release();
    assert.deepEqual(pressing.types(), ['press', 'abort']);            // the release after Escape is not a tap
    assert.equal(pressing.gesture.consumeClick(), true);

    const holding = rig();
    holding.gesture.press(); holding.clock.advance(LATCH_MS + 1); holding.gesture.escape();
    holding.clock.advance(PUSH_TO_TALK_MS);
    assert.deepEqual(holding.types(), ['press', 'arm', 'cancel']);     // no push-to-talk after Escape, even though the finger is down
    holding.gesture.release();
    assert.deepEqual(holding.types(), ['press', 'arm', 'cancel']);
    assert.equal(holding.gesture.phase, 'idle');

    const latched = rig();
    latched.gesture.press(); latched.clock.advance(LATCH_MS); latched.gesture.release();
    latched.gesture.escape();
    assert.deepEqual(latched.types(), ['press', 'arm', 'latch', 'cancel']);
    assert.equal(latched.gesture.phase, 'idle');
    assert.equal(latched.gesture.escape(), false);                     // nothing to cancel: Escape belongs to the page again
});

test('Releasing outside the orb before the latch time is not a tap', () => {
    const { gesture, clock, types, effects } = rig();
    gesture.press(); clock.advance(200); gesture.release('pointer', { inside: false });
    assert.deepEqual(types(), ['press', 'abort']);
    assert.equal(effects.at(-1).reason, 'released-outside');
    assert.equal(gesture.consumeClick(), false);
    const held = rig();                                                // after the latch time, release anywhere still ends the hold
    held.gesture.press(); held.clock.advance(PUSH_TO_TALK_MS); held.gesture.release('pointer', { inside: false });
    assert.equal(held.types().at(-1), 'finish');
});

test('Space and Enter are the same gesture with the same timings; other keys are not', () => {
    assert.equal(orbKey({ code: 'Space' }), 'key:Space');
    assert.equal(orbKey({ code: 'Enter' }), 'key:Enter');
    assert.equal(orbKey({ key: ' ' }), 'key:Space');
    assert.equal(orbKey({ code: 'KeyA', key: 'a' }), '');
    assert.equal(orbKey(null), '');
    const { gesture, clock, types } = rig();
    gesture.press('key:Space'); clock.advance(LATCH_MS); gesture.release('key:Space');
    assert.deepEqual(types(), ['press', 'arm', 'latch']);
    gesture.press('key:Enter');
    assert.equal(types().at(-1), 'finish');
    assert.equal(gesture.release('key:Space'), false);                 // the wrong key's release is ignored
    assert.equal(gesture.release('key:Enter'), true);
});

test('A click with no handled press (screen reader, voice control) acts as a tap, or finishes latched dictation', () => {
    const { gesture, clock, types } = rig();
    assert.equal(gesture.consumeClick(), false);
    assert.equal(gesture.activate(), true);
    assert.deepEqual(types(), ['tap']);
    gesture.press(); clock.advance(LATCH_MS); gesture.release();
    assert.equal(gesture.activate(), true);
    assert.equal(types().at(-1), 'finish');
    gesture.press(); assert.equal(gesture.activate(), false);          // mid-press a synthetic click adds nothing
});

test('The swallowed click expires, so a later assistive click is a real tap', () => {
    const { gesture, clock } = rig();
    gesture.press(); clock.advance(50); gesture.release();
    assert.equal(gesture.expectingClick, true);
    clock.advance(CLICK_WINDOW_MS + 1);
    assert.equal(gesture.expectingClick, false);
    assert.equal(gesture.consumeClick(), false);
});

test('The store is the truth: dictation that ended or started elsewhere re-syncs the gesture', () => {
    const latched = rig();
    latched.gesture.press(); latched.clock.advance(LATCH_MS); latched.gesture.release();
    latched.gesture.sync(false);                                       // an error ended it
    assert.equal(latched.gesture.phase, 'idle');
    assert.equal(latched.gesture.dictating, false);

    const elsewhere = rig();
    elsewhere.gesture.sync(true);                                      // the Dictate button started it
    assert.equal(elsewhere.gesture.phase, 'latched');
    elsewhere.gesture.press();
    assert.equal(elsewhere.types().at(-1), 'finish');

    const failing = rig();
    failing.gesture.press(); failing.clock.advance(LATCH_MS);
    failing.gesture.sync(false);                                       // the recorder failed to start while still held
    failing.clock.advance(PUSH_TO_TALK_MS);
    failing.gesture.release();
    assert.equal(failing.types().includes('finish'), false);           // nothing is stopped or sent for a capture that never existed
    assert.equal(failing.types().includes('latch'), false);
    assert.equal(failing.gesture.phase, 'idle');
});

test('An abandoned press cannot wedge the gesture', () => {
    const { gesture, clock } = rig();
    gesture.press('a'); clock.advance(100); gesture.press('b');        // ambiguous, and the releases are never seen
    assert.equal(gesture.phase, 'void');
    clock.advance(WATCHDOG_MS + 1);
    assert.equal(gesture.phase, 'idle');
    assert.equal(gesture.press(), true);
    gesture.reset();
    gesture.press('a'); clock.advance(100); gesture.press('b');         // and the same pointer pressing again frees it at once
    assert.equal(gesture.phase, 'void');
    assert.equal(gesture.press('a'), true);
    assert.equal(gesture.phase, 'pressed');
    gesture.reset();
    assert.deepEqual([gesture.phase, clock.pending], ['idle', 0]);
});

// ---- orb vocabulary (the store and recorder behaviour is in dictation-store.test.mjs) ---------------------------------------------

test('Dictation states have orb ids, labels and cues that tell latched from push-to-talk without colour', () => {
    assert.ok(ORB_STATES.includes('dictating-latched') && ORB_STATES.includes('dictating-ptt'));
    const latchedHeld = orbView({ enabled: true, dictating: true, dictationMode: 'latched', holding: true });
    const latchedFree = orbView({ enabled: true, dictating: true, dictationMode: 'latched', holding: false });
    const ptt = orbView({ enabled: true, dictating: true, dictationMode: 'ptt', holding: true });
    assert.deepEqual([latchedHeld.id, latchedFree.id, ptt.id], ['dictating-latched', 'dictating-latched', 'dictating-ptt']);
    assert.match(latchedHeld.label, /^Hands-free dictation\. Release to keep listening hands-free/);
    assert.match(latchedFree.label, /^Hands-free dictation\. Listening for speech; click to stop, Escape cancels\.$/);
    assert.match(ptt.label, /^Push-to-talk\. Listening only while held/);
    assert.notEqual(latchedHeld.label, latchedFree.label);             // the label says what a release does, as it changes
    assert.equal(cueFor('ready', 'dictating-latched'), 'dictation-start');
    assert.equal(cueFor('dictating-latched', 'dictating-ptt'), 'dictation-ptt');
    assert.equal(cueFor('dictating-ptt', 'ready'), 'dictation-end');
    assert.equal(cueFor('dictating-latched', 'ready'), 'dictation-end');
    assert.equal(orbView({ enabled: true, pressing: true, active: false }).press, true);
    assert.equal(orbView({ enabled: true, pressing: true, dictating: true }).press, false);   // the ring only charges towards the first threshold
});

test('The orb stylesheet draws latched and push-to-talk with their own glyph and ring, and no animation under reduced motion', async () => {
    const css = await readFile(new URL('../extensions/webui/page-head/zz-convo-orb.html', import.meta.url), 'utf8');
    assert.match(css, /\[data-convo=dictating-latched\] \.g-pen/);
    assert.match(css, /\[data-convo=dictating-ptt\] \.g-ptt/);
    assert.match(css, /\[data-convo\]\[data-convo-press\] \.co-ring2/);
    const reduced = css.slice(css.indexOf('@media(prefers-reduced-motion:reduce)'));
    assert.match(reduced, /data-convo-press/);                         // the charging ring has a static form
    assert.match(reduced, /dictating-ptt/);
    assert.doesNotMatch(css, /\[data-convo=dictation\]\s*\{[^}]*animation:\s*co-/);
});

test('The charging ring takes exactly the latch time, so the animation cannot drift from the gesture', async () => {
    const css = await readFile(new URL('../extensions/webui/page-head/zz-convo-orb.html', import.meta.url), 'utf8');
    assert.ok(css.includes(`var(--convo-latch-ms,${LATCH_MS}ms)`), 'update the stylesheet default when LATCH_MS changes');
});

test('The live region announces a change once, hides itself from sight, and never throws without a document', () => {
    const made = [];
    const element = () => { const e = { attrs: new Map(), style: {}, textContent: '', isConnected: true, setAttribute(k, v) { this.attrs.set(k, v); }, remove() { this.isConnected = false; } }; made.push(e); return e; };
    const saved = Object.getOwnPropertyDescriptor(globalThis, 'document');
    try {
        resetAnnouncer();
        Object.defineProperty(globalThis, 'document', { configurable: true, value: { createElement: element, body: { appendChild() {} } } });
        assert.equal(announce('Latched dictation. Click to finish; Escape cancels.'), true);
        assert.equal(made.length, 1);
        assert.equal(made[0].attrs.get('role'), 'status');
        assert.equal(made[0].attrs.get('aria-live'), 'polite');
        assert.match(made[0].style.cssText, /clip:rect/);
        assert.equal(made[0].textContent, 'Latched dictation. Click to finish; Escape cancels.');
        announce('Push-to-talk.');
        assert.equal(made.length, 1);                                       // one region, reused
        assert.equal(made[0].textContent, 'Push-to-talk.');
        resetAnnouncer();
        delete globalThis.document;
        assert.equal(announce('no document'), false);
    } finally { if (saved) Object.defineProperty(globalThis, 'document', saved); else delete globalThis.document; resetAnnouncer(); }
});

test('Pre-open: a tap never opens the microphone; at 299 ms nothing, at 300 ms the buffer opens, once', () => {
    const quick = rig();
    quick.gesture.press(); quick.clock.advance(PRE_OPEN_MS - 1); quick.gesture.release();
    assert.deepEqual(quick.all(), ['press', 'tap']);                   // no preopen, hence no discard either
    const slow = rig();
    slow.gesture.press(); slow.clock.advance(PRE_OPEN_MS - 1);
    assert.deepEqual(slow.all(), ['press']);
    slow.clock.advance(1);
    assert.deepEqual(slow.all(), ['press', 'preopen']);
    slow.clock.advance(1000);
    assert.equal(slow.all().filter(type => type === 'preopen').length, 1);
});

test('Release, interruption or Escape after pre-open but before the latch time discards the buffer first', () => {
    const tap = rig();
    tap.gesture.press(); tap.clock.advance(LATCH_MS - 1); tap.gesture.release();
    assert.deepEqual(tap.all(), ['press', 'preopen', 'discard', 'tap']);       // discard before the tap's own action
    for (const [how, run] of [
        ['pointercancel', g => g.cancel('pointercancel')], ['blur', g => g.cancel('blur')], ['hidden', g => g.cancel('hidden')],
        ['escape', g => g.escape()], ['released outside', g => g.release('pointer', { inside: false })],
    ]) {
        const r = rig();
        r.gesture.press(); r.clock.advance(1200); run(r.gesture);
        assert.deepEqual(r.all().slice(0, 3), ['press', 'preopen', 'discard'], how);
        assert.equal(r.all().includes('arm'), false, how);
    }
    const multi = rig();
    multi.gesture.press('a'); multi.clock.advance(1000); multi.gesture.press('b');
    assert.deepEqual(multi.all(), ['press', 'preopen', 'discard', 'abort']);
    multi.clock.advance(PUSH_TO_TALK_MS);
    assert.equal(multi.all().includes('arm'), false);
});

test('At the latch time the buffer is KEPT: no discard, one preopen, then arm; later cancels are dictation cancels', () => {
    const r = rig();
    r.gesture.press(); r.clock.advance(LATCH_MS);
    assert.deepEqual(r.all(), ['press', 'preopen', 'arm']);
    r.gesture.release();
    assert.deepEqual(r.all(), ['press', 'preopen', 'arm', 'latch']);
    assert.equal(r.all().includes('discard'), false);
    const ptt = rig();
    ptt.gesture.press(); ptt.clock.advance(PUSH_TO_TALK_MS); ptt.gesture.cancel('blur');
    assert.deepEqual(ptt.all(), ['press', 'preopen', 'arm', 'push-to-talk', 'cancel']);
    const late = rig();                                                // one very late timer still passes through every stage in order
    late.gesture.press(); late.clock.advance(PUSH_TO_TALK_MS * 3);
    assert.deepEqual(late.all(), ['press', 'preopen', 'arm', 'push-to-talk']);
    const custom = rig({ preOpenMs: 100 });
    custom.gesture.press(); custom.clock.advance(100);
    assert.deepEqual(custom.all(), ['press', 'preopen']);
});

// ---- Draft in composer (continuous): the one rule the store and the host recorder share, and what its end says ------------------
test('Continuous drafting needs the host draft AND Convo continuous, and always yields to a host transcript handler', () => {
    assert.equal(continuousDraft({ flag: 'continuous', messageMode: 'draft', handler: null }), true);
    for (const [flag, messageMode, handler] of [['single', 'draft', null], ['continuous', 'send', null], ['continuous', 'draft', () => {}],
        [undefined, 'draft', null], ['continuous', undefined, null], ['CONTINUOUS', 'draft', null], ['continuous', 'continuous', null]])
        assert.equal(continuousDraft({ flag, messageMode, handler }), false, JSON.stringify([flag, messageMode, !!handler]));
    assert.equal(continuousDraft(), false);
});

test('The end of a continuous dictation says how many phrases were added, and never that something was sent for you', () => {
    assert.equal(dictationEndNote({ phrases: 3 }), 'Dictation finished · 3 phrases added; nothing was sent.');
    assert.equal(dictationEndNote({ phrases: 1, reason: 'finish' }), 'Dictation finished · 1 phrase added; nothing was sent.');
    assert.equal(dictationEndNote({ phrases: 0 }), 'Dictation finished · 0 phrases added; nothing was sent.');
    assert.equal(dictationEndNote({ phrases: 2, reason: 'sent' }), 'Dictation finished when you sent your message · 2 phrases added.');
    assert.equal(dictationEndNote({ phrases: 2, reason: 'cancel' }), 'Dictation cancelled · 2 phrases added; the phrase in progress was dropped. Nothing was sent.');
    assert.equal(dictationEndNote({ phrases: 0, reason: 'cancel' }), 'Dictation cancelled. Nothing was sent.');
    assert.equal(dictationEndNote({ phrases: 1, reason: 'off' }), 'Dictation stopped because Convo was turned off · 1 phrase added; nothing was sent.');
    assert.equal(dictationEndNote({ phrases: -4.5 }), 'Dictation finished · 0 phrases added; nothing was sent.');
});
