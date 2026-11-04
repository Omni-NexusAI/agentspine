// The orb's press gesture: tap, latched dictation, push-to-talk. No imports. `createGesture` is pure (no DOM; timers are injected so
// tests run on a virtual clock) and only DECIDES: the store acts on the effects it emits, so nothing here opens a microphone.
// `bindOrbGesture` is the thin DOM adapter (document and window are passed in) that turns pointer, key and focus events into it.
//
//   idle --press--> pressed --release before LATCH_MS--> tap (toggle Convo; a buffer opened at PRE_OPEN_MS is discarded first)
//                      |
//                      +-- PRE_OPEN_MS --> (still pressed) the microphone opens as a buffer, owned by the one capture
//                      +-- LATCH_MS --> armed (dictation starts, still held)
//                                         |-- release before PUSH_TO_TALK_MS --> latched (dictation continues after release)
//                                         '-- PUSH_TO_TALK_MS --> ptt (listens only while held; release finishes)
//   latched --press--> swallow (that press finishes dictation; its release and click do nothing)
//
// Timings are provisional by the user's own description: change the two constants, nothing else.
export const PRE_OPEN_MS = 300;            // T0: a longer press than a tap opens the microphone as a BUFFER (nothing is delivered until T1); lower it to lose less of the first words
export const LATCH_MS = 2000;              // T1: how long a press must last before native dictation starts and stays latched
export const PUSH_TO_TALK_MS = 4000;       // T2: past this, the same press becomes push-to-talk (listening only while held)
export const CLICK_WINDOW_MS = 700;        // a click this soon after a handled release is the browser's echo of it, not a new tap
export const WATCHDOG_MS = 30000;          // an abandoned press (a release we never saw) cannot wedge the gesture (a new press by the same pointer frees it at once)

export const PHASES = ['idle', 'pressed', 'armed', 'ptt', 'latched', 'swallow', 'void'];

// Effects, in the order the store must act on them:
//   press        a press began (paint the "charging" ring)
//   preopen      PRE_OPEN_MS reached while pressed: open the one microphone as a silent buffer (never delivered unless T1 is reached)
//   discard      a press that had pre-opened ended before T1 (tap, interruption, Escape): throw the buffer away, close the stream
//   tap          release before T1: toggle Convo
//   arm          T1 reached while held: start native dictation (recorder records unconditionally while held)
//   push-to-talk T2 reached while held
//   latch        released between T1 and T2: dictation continues hands-free
//   finish       stop dictation and deliver the transcript ({reason})
//   cancel       stop dictation and discard it; never send ({reason})
//   abort        a press ended without any action ({reason}); paint the ring away
export function createGesture({ onEffect = () => {}, preOpenMs = PRE_OPEN_MS, latchMs = LATCH_MS, pushToTalkMs = PUSH_TO_TALK_MS,
    setTimer = (fn, ms) => globalThis.setTimeout(fn, ms), clearTimer = id => globalThis.clearTimeout(id) } = {}) {
    if (!(preOpenMs > 0) || !(latchMs > preOpenMs) || !(pushToTalkMs > latchMs)) throw new RangeError('The thresholds must be ordered: pre-open, then latch, then push-to-talk.');
    let phase = 'idle', holder = null, expectClick = false, preOpened = false;
    const timers = { preopen: null, latch: null, ptt: null, click: null, watchdog: null };

    const stop = name => { if (timers[name] != null) { clearTimer(timers[name]); timers[name] = null; } };
    const stopHold = () => { stop('preopen'); stop('latch'); stop('ptt'); };
    // A press that had pre-opened and ends before dictation was decided: the buffer must go, whatever else happens.
    const dropBuffer = () => { if (preOpened) { preOpened = false; emit('discard'); } };
    const emit = (type, detail = {}) => onEffect({ type, ...detail });
    const markClick = () => { expectClick = true; stop('click'); timers.click = setTimer(() => { expectClick = false; timers.click = null; }, CLICK_WINDOW_MS); };
    const go = (next, who = holder) => {
        phase = next; holder = next === 'idle' || next === 'latched' ? null : who;
        stop('watchdog');
        if (next === 'swallow' || next === 'void') timers.watchdog = setTimer(() => { timers.watchdog = null; if (phase === 'swallow' || phase === 'void') go('idle'); }, WATCHDOG_MS);
    };

    const reachPreOpen = () => {
        timers.preopen = null;
        if (phase !== 'pressed' || preOpened) return;
        preOpened = true;
        emit('preopen');
    };
    const reachLatch = () => {
        timers.latch = null;
        if (phase !== 'pressed') return;
        reachPreOpen();                                   // a late timer must still pass through every stage, in order
        preOpened = false;                                // from here the buffer is dictation: it is kept, not discarded
        phase = 'armed';
        emit('arm');
    };
    const reachPushToTalk = () => {
        timers.ptt = null;
        if (phase === 'pressed') reachLatch();            // a late timer must still pass through armed, in order
        if (phase !== 'armed') return;
        phase = 'ptt';
        emit('push-to-talk');
    };

    return {
        get phase() { return phase; },
        get holder() { return holder; },
        get holding() { return phase === 'pressed' || phase === 'armed' || phase === 'ptt'; },       // the finger / key is down
        get dictating() { return phase === 'armed' || phase === 'ptt' || phase === 'latched'; },
        get expectingClick() { return expectClick; },

        // `id` identifies the pointer or key; a second one while one is down is never a second gesture.
        press(id = 'pointer', { primary = true } = {}) {
            if (!primary && phase === 'idle') return false;
            switch (phase) {
                case 'idle':
                    go('pressed', id); preOpened = false;
                    timers.preopen = setTimer(reachPreOpen, preOpenMs);
                    timers.latch = setTimer(reachLatch, latchMs);
                    timers.ptt = setTimer(reachPushToTalk, pushToTalkMs);
                    emit('press');
                    return true;
                case 'pressed':
                    if (id !== holder) { stopHold(); dropBuffer(); go('void', holder); emit('abort', { reason: 'multi-touch' }); }   // ambiguous: not a tap, not dictation
                    return false;
                case 'latched':
                    go('swallow', id);                              // this press stops dictation; its release and click must do nothing more
                    emit('finish', { reason: 'press' });
                    return true;
                case 'swallow': case 'void':
                    if (id !== holder) return false;
                    go('idle');                                     // the same pointer pressing again: its earlier release was never seen
                    return this.press(id, { primary });
                default:
                    return false;                                   // armed / ptt: an extra finger or key changes nothing
            }
        },

        release(id = 'pointer', { inside = true } = {}) {
            if (id !== holder) return false;
            switch (phase) {
                case 'pressed':
                    stopHold(); go('idle'); dropBuffer();
                    if (inside) { markClick(); emit('tap'); } else emit('abort', { reason: 'released-outside' });
                    return true;
                case 'armed':
                    stopHold(); go('latched'); markClick();
                    emit('latch');
                    return true;
                case 'ptt':
                    stopHold(); go('idle'); markClick();
                    emit('finish', { reason: 'release' });
                    return true;
                case 'swallow': case 'void':
                    go('idle'); markClick();
                    return true;
                default:
                    return false;
            }
        },

        // The platform took the press away (pointercancel), the window lost focus or the page was hidden: this is never a tap and
        // never a send, and dictation that was running because of this press is discarded. After a pointercancel the browser sends no
        // click; after a blur or a hidden page the finger or key may still come up over the page and click, so wait for that release.
        cancel(reason = 'interrupted') {
            const after = reason === 'pointercancel' ? 'idle' : 'void';
            switch (phase) {
                case 'pressed': stopHold(); dropBuffer(); go(after, holder); emit('abort', { reason }); return true;
                case 'armed': case 'ptt': stopHold(); go(after, holder); emit('cancel', { reason }); return true;
                case 'swallow': case 'void': if (reason === 'pointercancel') go('idle'); return true;
                default: return false;                              // latched dictation carries on when the page merely loses focus
            }
        },

        // Escape: abandon whatever this press or dictation is. The finger or key may still be down, so wait for its release.
        escape() {
            switch (phase) {
                case 'pressed': stopHold(); dropBuffer(); go('void', holder); emit('abort', { reason: 'escape' }); return true;
                case 'armed': case 'ptt': stopHold(); go('void', holder); emit('cancel', { reason: 'escape' }); return true;
                case 'latched': go('idle'); emit('cancel', { reason: 'escape' }); return true;
                default: return false;
            }
        },

        // A click that did not come from a press we handled (assistive technology, voice control): the same as a tap.
        activate() {
            if (phase === 'idle') { emit('tap'); return true; }
            if (phase === 'latched') { go('idle'); emit('finish', { reason: 'activate' }); return true; }
            return false;
        },

        // The click the browser synthesises after a handled release must not toggle Convo a second time.
        consumeClick() {
            if (!expectClick) return false;
            expectClick = false; stop('click');
            return true;
        },

        // The store is the truth about whether a recorder is running. Dictation that ended on its own (an error, the plugin being
        // turned off) frees the gesture; dictation started elsewhere (the Dictate button) is simply latched.
        sync(dictating) {
            if (!dictating && phase === 'latched') go('idle');
            else if (!dictating && (phase === 'armed' || phase === 'ptt')) { stopHold(); go('void', holder); }
            else if (dictating && phase === 'idle') go('latched');
        },

        reset() { stopHold(); stop('click'); stop('watchdog'); expectClick = false; preOpened = false; phase = 'idle'; holder = null; },
    };
}

// ---- Draft in composer (continuous) ---------------------------------------------------------------------------------------------
// Agent Zero's Whisper message_mode is still `draft` (the host stores only send|draft) and Convo's own `voice_message_draft` is
// `continuous`: each finished phrase is added to the composer and listening goes on until the user sends, stops or cancels. A host
// component that asked for the transcript itself (a `finalTranscriptHandler`) always gets it instead. Shared by the store (the orb's
// words) and the host recorder (where each phrase goes), so the two never disagree.
export function continuousDraft({ flag, messageMode, handler } = {}) {
    return flag === 'continuous' && messageMode === 'draft' && !handler;
}

// What a continuous dictation says when it ends (the note and the screen-reader announcement). `reason`: 'finish' (a tap or press,
// push-to-talk released, the Dictate button, or the host stopped listening), 'sent' (the user sent the message), 'cancel' (Escape or
// an interruption: the phrase in progress was dropped, the text already added stays) or 'off' (Convo was turned off).
export function dictationEndNote({ phrases = 0, reason = 'finish' } = {}) {
    const count = Math.max(0, Math.floor(Number(phrases) || 0));
    const added = `${count} phrase${count === 1 ? '' : 's'} added`;
    if (reason === 'sent') return `Dictation finished when you sent your message · ${added}.`;
    if (reason === 'cancel') return count ? `Dictation cancelled · ${added}; the phrase in progress was dropped. Nothing was sent.` : 'Dictation cancelled. Nothing was sent.';
    if (reason === 'off') return `Dictation stopped because Convo was turned off · ${added}; nothing was sent.`;
    return `Dictation finished · ${added}; nothing was sent.`;
}

// Which keys act as the orb button: the same gesture, with key down / key up in place of press / release.
export const ORB_KEYS = new Set(['Space', 'Enter']);
export function orbKey(event) {
    const code = event?.code || (event?.key === ' ' ? 'Space' : event?.key === 'Enter' ? 'Enter' : '');
    return ORB_KEYS.has(code) ? 'key:' + code : '';
}

// ---- DOM adapter -------------------------------------------------------------------------------------------------------------
// `store` supplies: enabled, preOpenDictation(), discardDictationBuffer(), dictationBufferSettled(), dictationMode ('' when no dictation is running), toggle(), beginDictation({held}), endDictation({cancel, reason}),
// setDictationState({mode, holding}) and setPressing(flag). Everything the gesture does reaches the store through those.
export function bindOrbGesture({ store, document, window, selector = '#microphone-button' }) {
    const orbOf = event => event?.target?.closest?.(selector);
    const gesture = createGesture({ onEffect(effect) {
        switch (effect.type) {
            case 'press': store.setPressing(true); break;
            case 'preopen': void store.preOpenDictation(); break;
            case 'discard': void store.discardDictationBuffer(); break;
            case 'abort': store.setPressing(false); break;
            case 'tap': {
                store.setPressing(false);
                const settled = store.dictationBufferSettled();            // the buffer's stream is closed before Convo opens its own
                if (settled) void settled.then(() => store.toggle()); else void store.toggle();
                break;
            }
            case 'arm': store.setPressing(false); void store.beginDictation({ held: true }); break;
            case 'push-to-talk': store.setDictationState({ mode: 'ptt', holding: true }); break;
            case 'latch': store.setDictationState({ mode: 'latched', holding: false }); break;
            case 'finish': store.setPressing(false); void store.endDictation({ reason: effect.reason }); break;
            case 'cancel': store.setPressing(false); void store.endDictation({ cancel: true, reason: effect.reason }); break;
        }
    } });
    const sync = () => gesture.sync(!!store.dictationMode);
    const pointer = event => 'p' + (event.pointerId ?? 0);
    const listeners = [];
    const on = (target, type, handler, capture = false) => { target.addEventListener(type, handler, capture); listeners.push([target, type, handler, capture]); };

    on(document, 'pointerdown', event => {
        if (!store.enabled || !orbOf(event) || event.button !== 0) return;
        event.preventDefault(); event.stopImmediatePropagation();       // the host's own microphone click must never also run
        sync();
        gesture.press(pointer(event), { primary: event.isPrimary !== false });
    }, true);
    on(document, 'pointerup', event => { sync(); gesture.release(pointer(event), { inside: !!orbOf(event) }); }, true);
    on(document, 'pointercancel', event => { if (pointer(event) === gesture.holder) gesture.cancel('pointercancel'); }, true);
    on(document, 'contextmenu', event => { if (store.enabled && orbOf(event)) event.preventDefault(); }, true);    // a long press is dictation, not a menu
    on(window, 'blur', () => { gesture.cancel('blur'); });
    on(document, 'visibilitychange', () => { if (document.hidden) gesture.cancel('hidden'); });
    on(document, 'click', event => {
        if (!store.enabled || !orbOf(event)) return;
        event.preventDefault(); event.stopImmediatePropagation();
        if (gesture.consumeClick()) return;                              // the browser's echo of a release we already handled
        sync(); gesture.activate();                                      // assistive technology and voice control click without a press
    }, true);
    const plain = event => !(event.ctrlKey || event.metaKey || event.altKey || event.shiftKey);    // modified keys belong to the Convo hotkey
    on(document, 'keydown', event => {
        if (!store.enabled || event.isComposing) return;
        if (event.key === 'Escape' || event.code === 'Escape') {
            sync();
            if (gesture.escape()) { event.preventDefault(); event.stopImmediatePropagation(); }
            return;
        }
        const key = orbKey(event);
        if (!key || !orbOf(event) || !plain(event)) return;
        event.preventDefault();                                          // stops the button's own click for Enter (on keydown) and Space (on keyup)
        if (event.repeat) return;
        sync(); gesture.press(key);
    }, true);
    on(document, 'keyup', event => {
        const key = orbKey(event);
        if (!key || !store.enabled) return;
        if (gesture.release(key) || orbOf(event)) event.preventDefault();
    }, true);

    return { gesture, dispose() { for (const [target, type, handler, capture] of listeners.splice(0)) target.removeEventListener?.(type, handler, capture); gesture.reset(); } };
}
