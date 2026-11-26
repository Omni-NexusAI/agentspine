"""The conversation model's own choice to say nothing (`[SILENT]`) or to wait for the rest of an unfinished thought (`[WAIT]`).

In a conversation the user started, Convo answers every finished turn the classifier did not confidently rule out, so the model that
heard the audio needs a way to decline: the user asked it not to respond, there were no words meant for it (noise, someone talking to
someone else, media, its own voice), or the thought is plainly unfinished ("I need to..."). The system text offers two control
words; nobody ever hears or sees them.

`ControlFilter` reads the start of the reply (after the hidden `USER:`/`LANGUAGE:` header, which convo_user_line takes off first):
* a reply that begins with a control word (any case, optionally followed by a short reason on the same line) is that decision;
  everything after it is swallowed and the reason (bounded) is kept for the developer view;
* a reply that is only one bracketed or parenthesised meta statement ("[No response provided as per instructions.]") is silent;
* a leading bracketed stage direction before real words ("[laughs] Sure.") is dropped and the words pass;
* anything else passes untouched, starting with its first chunk once it can no longer be one of the above.

It generalises convo_listen.ListenFilter (which stays the exact `[LISTEN]` rule outside a conversation)."""
from __future__ import annotations

import re

SILENT, WAIT = 'silent', 'wait'
TOKENS = {'[silent]': SILENT, '[wait]': WAIT}
BARE = {'silent': SILENT, 'wait': WAIT}
REASON_LIMIT = 120
HOLD_LIMIT = 240          # characters held back while a leading bracketed statement is still open
CLOSERS = {'[': ']', '(': ')'}
INSTRUCTION = ('After the USER and LANGUAGE lines you may write one control word instead of an answer: [SILENT] when nothing should be said: '
               'you were asked not to respond, or there were no words meant for you (noise, someone talking to someone else, media, '
               'your own voice); [WAIT] when the user\'s thought is clearly unfinished and they are about to go on (for example they '
               'said "I need to..." and stopped). A few words of reason may follow it on the same line. Nobody hears a control word. '
               'A request to do something is never [SILENT], however short it is: do it (with a tool when it needs one) and '
               'answer; never write that something was done after a control word. '
               'A bare acknowledgement of your answer that adds nothing new (such as "okay", "got it" or "thanks") usually needs no '
               'reply: write [SILENT]. Give it at most one brief reply, and never reply to a repeated acknowledgement. '
               'Otherwise answer normally. Do not bring up audio quality yourself (when the user asks about their voice or a sound, '
               'answer from what you heard), never mention the control words, and never ask the user to repeat themselves: if their '
               'words stop mid-thought, write [WAIT].')


def _reason(text):
    line = text.strip().split('\n', 1)[0].strip().lstrip(':-—–').strip()
    return ' '.join(line.split())[:REASON_LIMIT]


class ControlFilter:
    """Feed text deltas through `feed`; at the end call `finish`. `choice` holds SILENT or WAIT once decided."""

    def __init__(self):
        self.held = ''
        self.decided = False       # the reply is ordinary text: everything passes from here on
        self.choice = None
        self.rest = ''             # what followed a control word (its reason), bounded

    def feed(self, text):
        if self.choice is not None:
            if len(self.rest) < 4 * REASON_LIMIT:
                self.rest += text or ''
            return ''
        if self.decided or not text:
            return (text or '') if self.decided else ''
        self.held += text
        return self._settle(end=False)

    def _release(self, text):
        self.held, self.decided = '', True
        return text

    def _settle(self, *, end):
        head = self.held.lstrip()
        if not head:
            return ''
        lower = head.lower()
        for token, choice in TOKENS.items():
            if lower.startswith(token):
                self.choice, self.rest, self.held = choice, head[len(token):], ''
                return ''
        opener = head[0]
        if opener not in CLOSERS:
            # A model may drop the brackets: hold the few characters that could still be a bare control word ("Silent.").
            letters = re.sub(r'[^a-z]', '', lower)
            if not end and len(head) <= 12 and letters and any(word.startswith(letters) for word in BARE):
                return ''
            return self._release(self.held)
        if any(token.startswith(lower[:len(token)]) for token in TOKENS) and not end:
            return ''                                            # may still become a control word
        close = head.find(CLOSERS[opener])
        if close < 0:
            return self._release(self.held) if len(head) > HOLD_LIMIT or end else ''
        after = head[close + 1:]
        if after.strip():
            # A stage direction before real words: a square-bracketed one is dropped; parentheses are ordinary text.
            return self._release(after.lstrip() if opener == '[' else self.held)
        return '' if not end else None                           # the whole reply so far is one bracketed statement

    def finish(self):
        """(choice, reason, text): SILENT or WAIT with its reason, else (None, '', the text still held)."""
        if self.choice is not None:
            return self.choice, _reason(self.rest), ''
        if self.decided:
            return None, '', ''
        head = self.held.strip()
        bare = BARE.get(re.sub(r'[^a-z]', '', head.lower())) if len(head) <= 12 else None
        if bare:
            return bare, '', ''
        settled = self._settle(end=True)
        if settled is None:
            self.held = ''
            return SILENT, 'a bracketed note instead of an answer', ''
        held, self.held = (settled or self.held), ''
        self.decided = True
        return None, '', held
