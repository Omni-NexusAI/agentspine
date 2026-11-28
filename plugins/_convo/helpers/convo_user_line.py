"""The reply's hidden header: what the user said and the language of the answer, reported by the model itself.

A direct-audio turn stores no text of what was said, so later turns would show the model its own answers with no question in front
of them ("What about Mexico?" then means nothing). The model that heard the audio is asked to begin its answer with two hidden lines
(the standalone fork's USER_TRANSCRIPT / ASSISTANT_LANGUAGE pattern):

    USER: <exactly what the user said>
    LANGUAGE: <the language of the answer>

`HeaderFilter` takes them off the stream before anything is shown or spoken. The words become the turn's user text; the language is
offered to speech (convo_language maps it). A missing or malformed header changes nothing: the answer passes through untouched.

What it accepts, like the fork's parser:
* the aliases USER_TRANSCRIPT / TRANSCRIPT and ASSISTANT_LANGUAGE / LANG, any case, with markdown emphasis or a list/quote marker;
* the words on the same line, or on the line after an empty `USER:` when that line is indented or a LANGUAGE line follows it (so an
  empty USER line followed directly by the answer never turns the answer's first sentence into "what the user said");
* deltas split anywhere, including mid-marker;
* a header written as one bracketed tag, `[LANGUAGE: English] The answer...` (2026-11-24: spoken aloud with its brackets);
* an optional leading answer label (`ASSISTANT:`, `RESPONSE:`, ...), which is removed.
Everything held is bounded (SCAN characters); past that the text is released as answer, except a line that already began with a
header marker: that line is dropped up to its newline (at most DISCARD characters) and never spoken, its start kept as the words.
"""
from __future__ import annotations

import re

PREFIX = 'USER:'
LIMIT = 1600            # a request said in several parts may take a minute (convo_continuation)
LANGUAGE_LIMIT = 40
SCAN = 1720             # characters held while the header is still undecided
DISCARD = 4000          # characters of one over-long header line dropped while waiting for its newline
HEADER_LINES = 4

USER_NAMES = ('USER_TRANSCRIPT', 'USER', 'TRANSCRIPT')
LANGUAGE_NAMES = ('ASSISTANT_LANGUAGE', 'LANGUAGE', 'LANG')
LABEL_NAMES = ('ASSISTANT_RESPONSE', 'ASSISTANT_PREAMBLE', 'ASSISTANT', 'RESPONSE')
_LEAD = r'^[\s>*_#`-]*'
# `[NAME: value]` (or in parentheses or braces), closed on the same line; what follows it on that line is the next line.
TAG = re.compile(r'^[\s>*_#`-]*[\[({]\s*(' + '|'.join(USER_NAMES + LANGUAGE_NAMES) + r')[\s*_`]*:([^\n]*?)[\])}][ \t]*', re.I)
TAG_OPEN = re.compile(r'^[\s>*_#`-]*[\[({]\s*([A-Za-z_]*)([\s*_`]*:)?')
_MARK = r'[\s*_`]*:[\s*_`]*'
MARKER = re.compile(_LEAD + r'(' + '|'.join(USER_NAMES + LANGUAGE_NAMES) + r')' + _MARK + r'(.*)$', re.I | re.S)
LABEL = re.compile(_LEAD + r'(' + '|'.join(LABEL_NAMES) + r')' + _MARK, re.I)
ECHOED = re.compile(r'\b(USER\w*|TRANSCRIPT|ASSISTANT\w*|LANG\w*)\s*:', re.I)
LANGUAGE_VALUE = re.compile(r"^[^\W\d_][\w ()'.,/-]*$", re.U)

INSTRUCTION = ('Begin your reply with two hidden lines. First "USER: " followed by exactly what the user just said, word for word as '
               'you heard it (leave it empty if you heard no words). Then "LANGUAGE: " followed by the language you answer in (for '
               'example English). Then a new line and your spoken answer. Only what follows those two lines is spoken.')
# Said with the header instruction on every turn that carries the user's audio: the model is hearing a recording, not reading text.
# Without it a model that writes the user's words down turn after turn concludes it is only given text (2026-11-18: asked about the
# user's accent, a model that had just transcribed the audio answered "I only receive your words as text").
HEARING = ('The latest message is the user\'s own microphone audio, and you hear it directly: their voice, tone, accent and pace, and '
           'any sounds around them. You may rely on what you hear, and describe it when the user asks about their voice or a sound. '
           'Earlier turns appear as text: their audio is no longer attached unless a clip is shown.')
PLACEHOLDER = re.compile(r'^[\[(<].*[\])>]$')


def validate(text):
    """The reported words, or '' when they are not usable (empty, too long, a placeholder, or header text echoed back)."""
    text = ' '.join(str(text or '').split()).strip().strip('*_`').strip().strip('"').strip()
    if not text or len(text) > LIMIT or PLACEHOLDER.match(text) or ECHOED.search(text):
        return ''
    return text


def validate_language(text):
    """The declared language as written (`Spanish`, `es`, `Português`), or '' when it is not a plausible language name."""
    text = ' '.join(str(text or '').split()).strip().strip('*_`"\'').strip().rstrip('.').strip()
    if not text or len(text) > LANGUAGE_LIMIT or not LANGUAGE_VALUE.match(text) or ECHOED.search(text):
        return ''
    return text


def _bare(text):
    return re.sub(_LEAD, '', text).upper()


def _could_be(partial, names):
    """Whether an unfinished line may still turn into one of `names` followed by a colon."""
    head = _bare(partial)
    if not head:
        return True
    if any(name.startswith(head) for name in names):
        return True
    return bool(re.match(r'^(' + '|'.join(names) + r')[\s*_`]*(:.*)?$', head, re.S))


def _kind(name):
    return 'language' if name.upper() in LANGUAGE_NAMES else 'user'


class HeaderFilter:
    """Feed the answer's text deltas through `feed`; it returns the text to pass on. Call `finish` when the answer ends.

    `line` holds the reported words and `language` the declared language once found. With `record=False` (a tool's follow-up, whose
    question is already known) the header is still removed but its words are not reported."""

    def __init__(self, record=True):
        self.record = record
        self.buffer, self.done, self.words, self.language = '', False, '', ''
        self.lines = 0
        self.pending = None          # a line after an empty USER: that is the user's words only if a LANGUAGE line follows
        self.awaiting = False        # an empty USER: was seen and its words may follow on the next line
        self.label = True            # the first answer line may still carry an ASSISTANT:/RESPONSE: label
        self.skipping = None         # the start of a header line longer than SCAN, dropped up to its newline (never spoken)
        self.skipped = 0

    @property
    def line(self):
        return self.words if self.record else ''

    # -- streaming ------------------------------------------------------------------------------------------------------------
    def feed(self, text):
        if self.done and not self.label:
            return text
        self.buffer += text or ''
        return self._advance(final=False)

    def finish(self):
        """The answer ended: what is still held (the whole answer when it was only a header, nothing)."""
        return self._advance(final=True)

    # -- internals ------------------------------------------------------------------------------------------------------------
    def _untag(self):
        """A bracketed header tag at the start of the buffer becomes an ordinary header line. False while it may still become one."""
        if self.skipping is not None or self.lines >= HEADER_LINES:
            return True
        found = TAG.match(self.buffer)
        if found:
            self.buffer = found.group(1) + ':' + found.group(2) + '\n' + self.buffer[found.end():]
            return True
        opened = TAG_OPEN.match(self.buffer)
        if not opened or '\n' in self.buffer.lstrip() or len(self.buffer) > SCAN:
            return True
        names, word = USER_NAMES + LANGUAGE_NAMES, opened.group(1).upper()
        if opened.group(2):
            return word not in names                            # `[LANGUAGE: Eng`: wait for the closing bracket
        return opened.end() < len(self.buffer) or not any(name.startswith(word) for name in names)

    def _advance(self, final):
        out = []
        while not self.done:
            if not self._untag() and not final:
                return ''.join(out)
            if self.skipping is not None:
                # An over-long header line: drop everything up to its newline (bounded); its start becomes the words.
                newline = self.buffer.find('\n')
                piece = self.buffer if newline < 0 else self.buffer[:newline]
                self.buffer = '' if newline < 0 else self.buffer[newline + 1:]
                self.skipped += len(piece)
                if newline < 0 and not final and self.skipped <= DISCARD:
                    return ''.join(out)
                out.append(self._end_skip())
                if newline < 0:
                    self.done = True                            # the bound or the answer's end: what follows is answer
                continue
            newline = self.buffer.find('\n')
            if newline < 0:
                partial = self.buffer
                if final:
                    self.buffer = ''
                    out.append(self._last_line(partial))
                    self.done = True
                    break
                if self._undecided(partial) and len(partial) + len(self.pending or '') <= SCAN:
                    return ''.join(out)
                if self.lines < HEADER_LINES and MARKER.match(partial):
                    # A header line longer than SCAN: never released as answer (it would be spoken with its marker).
                    self.skipping, self.skipped, self.buffer = partial[:SCAN], len(partial), ''
                    continue
                self.buffer = ''
                out.append(self._end_header(partial))
                break
            line, self.buffer = self.buffer[:newline], self.buffer[newline + 1:]
            out.append(self._header_line(line))
        if self.done and self.label:
            out.append(self._strip_label(final))
        elif self.done and self.buffer:
            out.append(self.buffer)
            self.buffer = ''
        return ''.join(out)

    def _undecided(self, partial):
        if not partial.strip():
            return True
        label = LABEL.match(partial)
        if label:
            return label.end() == len(partial)                  # an answer label: the answer begins with its first real character
        if self.lines >= HEADER_LINES:
            return False
        if self.pending is not None or self.awaiting:
            return _could_be(partial, LANGUAGE_NAMES + USER_NAMES) or self.awaiting
        return _could_be(partial, USER_NAMES + LANGUAGE_NAMES + LABEL_NAMES)

    def _header_line(self, line):
        """One complete line while the header is open; returns any text released as answer."""
        if not line.strip():
            return ''                                           # blank lines before or inside the header are not answer
        match = MARKER.match(line) if self.lines < HEADER_LINES else None
        if match:
            self.lines += 1
            kind, value = _kind(match.group(1)), match.group(2)
            if kind == 'language':
                if self.pending is not None:
                    self.words, self.pending = self.words or validate(self.pending), None
                self.awaiting = False
                self.language = self.language or validate_language(value)
                if self.words or self.lines >= 2:
                    self.done = True                            # LANGUAGE ends the header
                return ''
            released = self._flush_pending()
            words = validate(value)
            if words:
                self.words, self.awaiting = self.words or words, False
            else:
                self.awaiting = not self.words
            if self.language and self.words:
                self.done = True
            return released
        # Not a marker line.
        if self.awaiting:
            self.awaiting = False
            if line[:1] in ' \t' and validate(line):
                self.words = validate(line)                     # an indented line under an empty USER: is the user's words
                if self.language:
                    self.done = True
                return ''
            self.pending = line
            return ''
        if self.pending is not None:
            released = self._flush_pending() + line + '\n'
            self.done = True
            return released
        self.done = True
        return self._label_off(line) + '\n'

    def _flush_pending(self):
        if self.pending is None:
            return ''
        held, self.pending = self.pending, None
        self.done = True
        return self._label_off(held) + '\n'

    def _end_skip(self):
        """An over-long header line ended (or was cut at DISCARD): read it as that header line with its value cut to LIMIT at a word
        boundary, so the user's words are kept when they are usable and nothing of the line is spoken."""
        held, self.skipping = self.skipping, None
        match = MARKER.match(held)
        value = ' '.join(match.group(2).split())
        if len(value) > LIMIT:
            value = value[:LIMIT].rsplit(' ', 1)[0]
        return self._header_line(match.group(1) + ': ' + value)

    def _end_header(self, partial):
        """The header can no longer continue: release what is held as answer."""
        released = self._flush_pending()
        self.done = True
        return (released + partial) if released else self._label_off(partial)

    def _last_line(self, partial):
        """The answer ended with `partial` (no newline) while the header was open."""
        if partial.strip():
            match = MARKER.match(partial) if self.lines < HEADER_LINES else None
            if match:
                if _kind(match.group(1)) == 'language':
                    if self.pending is not None:
                        self.words, self.pending = self.words or validate(self.pending), None
                    self.language = self.language or validate_language(match.group(2))
                    return ''
                released = self._flush_pending()
                self.words = self.words or validate(match.group(2))
                return released
            if self.awaiting and partial[:1] in ' \t' and validate(partial):
                self.words = validate(partial)
                return ''
        released = self._flush_pending()
        return released + (self._label_off(partial) if not released else partial)

    def _label_off(self, line):
        """Remove a leading answer label from the first answer line (once)."""
        self.label = False
        match = LABEL.match(line)
        return line[match.end():] if match else line

    def _strip_label(self, final):
        """After the header: the first answer line may still begin with a label; hold just enough to tell."""
        if not self.buffer:
            if final:
                self.label = False
            return ''
        head = self.buffer.lstrip('\n')
        if not head.strip() and not final:
            return ''
        if not final and '\n' not in head and len(head) <= SCAN and _could_be(head, LABEL_NAMES):
            label = LABEL.match(head)
            if not label or label.end() == len(head):
                return ''                                       # a label, or not yet clear: wait for its first real character
        self.buffer = ''
        return self._label_off(head)


UserLine = HeaderFilter
