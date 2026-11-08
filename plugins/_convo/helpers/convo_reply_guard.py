"""Keeps a model's tool-call markup out of speech and text.

Some models write a tool call as ordinary reply text (`<tool_call><function=web_search>...`) instead of making a structured one.
Nothing executes it, but a reply is spoken and shown, so it would be read aloud. This filter sits on the streamed reply: it passes
text through, withholds a trailing fragment that might still turn into markup, and drops everything from the first marker on. It
never runs, parses or authorizes anything; tools still need a structured proposal and the host's own checks."""
from __future__ import annotations

import re

MARKER = re.compile(r'<\s*/?\s*(?:tool_calls?|tool_use|function_call|function|invoke|parameter)\b|<\|[a-z_]*tool[a-z_]*\|>|\[\s*tool_calls?\s*\]', re.I)
# What a still-incomplete marker can look like at the end of a chunk (compared after lower-casing, without spaces).
PREFIXES = ('<tool_call', '</tool_call', '<tool_use', '</tool_use', '<function', '</function', '<invoke', '</invoke', '<parameter', '</parameter', '<|', '[tool_call')
HOLD = 24


class ReplyGuard:
    def __init__(self):
        self.pending = ''
        self.blocked = False

    def feed(self, text):
        """Text that is safe to show and speak now ('' while a possible marker is held back, or after one was found)."""
        if self.blocked or not text:
            return ''
        buffer = self.pending + text
        found = MARKER.search(buffer)
        if found:
            self.blocked, self.pending = True, ''
            return buffer[:found.start()]
        cut = max(buffer.rfind('<'), buffer.rfind('['))
        if cut >= 0 and len(buffer) - cut <= HOLD:
            tail = re.sub(r'\s+', '', buffer[cut:].lower())
            if any(prefix.startswith(tail) or tail.startswith(prefix) for prefix in PREFIXES):
                self.pending = buffer[cut:]
                return buffer[:cut]
        self.pending = ''
        return buffer

    def finish(self):
        """The end of the reply: a held fragment that never became a marker is ordinary text after all."""
        released, self.pending = ('' if self.blocked else self.pending), ''
        return released
