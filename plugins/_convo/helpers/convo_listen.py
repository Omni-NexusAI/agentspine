"""The main model's own "this was not for me" decision.

When enabled, the system text tells the conversation model it may answer exactly `[LISTEN]` when the audio was not meant for it.
The harness treats a reply that is exactly that token (case-insensitive, surrounding whitespace ignored, even when it arrives split
across stream chunks) as no reply. This can only suppress output: the classifier's decision is never overridden in the other
direction, and a classifier that explicitly admitted the turn (see `strong`) is never silenced by it.

`ListenFilter` sits in front of the reply guard. It withholds leading text only while that text could still become the token, so an
ordinary reply starts flowing on its first chunk; once the reply is known not to be the token, everything held is released in order."""
from __future__ import annotations

TOKEN = '[listen]'
INSTRUCTION = ('If the audio was clearly not meant for you (people talking to each other, media, the speaker talking to themselves), '
               'answer with exactly [LISTEN] and nothing else, and no one will hear a reply.')


def strong(decision):
    """An explicit request the classifier is sure of: the listen token never suppresses it."""
    return bool(decision.explicit_request and decision.confidence >= .9)


class ListenFilter:
    def __init__(self):
        self.held = ''
        self.decided = False      # True once the reply is known to be ordinary text (nothing more is held)

    def feed(self, text):
        """Text that is safe to pass on now ('' while the start of the reply might still be the token)."""
        if self.decided or not text:
            return text if self.decided else ''
        buffer = self.held + text
        probe = buffer.strip().lower()
        if probe and not TOKEN.startswith(probe[:len(TOKEN)]) or len(probe) > len(TOKEN):
            self.held, self.decided = '', True
            return buffer
        self.held = buffer
        return ''

    def finish(self):
        """(is_listen, text): at the end of the reply, whether it was exactly the token, else the text still held."""
        held, self.held = self.held, ''
        if not self.decided and held.strip().lower() == TOKEN:
            return True, ''
        self.decided = True
        return False, held
