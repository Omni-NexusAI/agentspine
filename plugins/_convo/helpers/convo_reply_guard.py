"""Keeps a model's tool-call markup out of speech and text.

Some models write a tool call as ordinary reply text (`<tool_call><function=web_search>...`) instead of making a structured one.
Nothing executes it, but a reply is spoken and shown, so it would be read aloud. This filter sits on the streamed reply: it passes
text through, withholds a trailing fragment that might still turn into markup, and drops everything from the first marker on. It
never runs or authorizes anything: what it cut is kept (`captured`, bounded) so `convo_tools.parse_text_call` can read the call
back into a structured proposal that goes through the host's own checks."""
from __future__ import annotations

import re

# Gemma 4 opens a call with `<|tool_call>` (no closing pipe), so the pipe-prefixed form is matched on its name alone.
MARKER = re.compile(r'<\s*/?\s*(?:tool_calls?|tool_use|function_call|function|invoke|parameter)\b|<\|\s*/?\s*(?:tool_calls?|tool_use|function_call)\b'
                    r'|<\|[a-z_]*tool[a-z_]*\|>|\[\s*tool_calls?\s*\]', re.I)
# What a still-incomplete marker can look like at the end of a chunk (compared after lower-casing, without spaces).
PREFIXES = ('<tool_call', '</tool_call', '<tool_use', '</tool_use', '<function', '</function', '<invoke', '</invoke', '<parameter', '</parameter', '<|', '[tool_call')
HOLD = 24
CAPTURE = 16000


class ReplyGuard:
    def __init__(self):
        self.pending = ''
        self.blocked = False
        self.captured = ''

    def feed(self, text):
        """Text that is safe to show and speak now ('' while a possible marker is held back, or after one was found)."""
        if self.blocked:
            self.captured = (self.captured + (text or ''))[:CAPTURE]
            return ''
        if not text:
            return ''
        buffer = self.pending + text
        found = MARKER.search(buffer)
        if found:
            self.blocked, self.pending, self.captured = True, '', buffer[found.start():][:CAPTURE]
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
