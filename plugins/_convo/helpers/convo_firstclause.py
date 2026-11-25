"""Steady speech: the first full sentence early, then the rest of the reply in as few requests as possible.

Adapted from the standalone fork's pacing (Omni-NexusAI speech-to-speech, Apache-2.0, same owner). Every request to a speech server
costs its own startup and is sampled on its own, so a reply cut into many small requests is heard with pauses between them and with
a voice that drifts. Two small parts:

* `FirstClause` decides where a phrase ends. The first phrase is the first full sentence (ending in `. ? !`; a decimal point, a time
  and a short abbreviation are not sentence ends), never a comma or a length cut: a phrase cut mid-sentence ("Could you please
  clarify what you would") is heard as a pause in the middle of a thought. Only a first sentence longer than MERGE_LIMIT characters
  is cut, at a word. After it, the complete sentences are released as they arrive.
* `SpeechQueue` runs the synthesis in one background worker, strictly in order, and merges whatever is waiting when it is free into
  one request of at most MERGE_LIMIT characters, cut only after a full sentence (a single longer sentence at a word). It is bounded,
  never runs two syntheses at once (the speech provider admits one request at a time and answers a typed SpeechBusy otherwise,
  which the speak call already waits out), and reports the first failure to the turn exactly where a direct call would have raised
  it. Cancelling the turn cancels the worker.

With `first_clause_speech` off the turn puts nothing until the reply has ended, then the whole of it (still at most MERGE_LIMIT
characters a request): one generation for any reply up to MERGE_LIMIT characters, the parity candidate that the measurement decides
on (the default stays on until then). Playback order, per-phrase receipts, the busy wait and the two-second cancel contract are properties of the
speak call and the turn, not of this module; it only changes when the call is made and with how much text."""
from __future__ import annotations

import asyncio
import collections
import re

SENTENCE_CHARS = '.?!'
CLOSERS = '"\')]”’»'
WIDE, WIDE_CLOSERS = '。！？｡؟۔।॥', '」』）】'      # sentence marks of other scripts (no space has to follow them), and what may close a quotation there
FIRST_MIN = 3             # characters in the first sentence
# Words the first request needs: a short first sentence ("Good evening, sir.") waits for the next one. Measured 2026-11-09 on the
# user's audio.cpp (convo-live-test/tts_variants_20261109.py, 3 seeds): alone it was voiced higher and brighter than the rest; merged,
# the pitch spread across a reply fell from 23 to 16 Hz and the brightness spread from 151 to 107 Hz, for ~0.3 s more to first audio.
FIRST_WORDS = 6
MERGE_LIMIT = 420         # characters in one speech request
ABBREVIATIONS = frozenset({'dr', 'mr', 'mrs', 'ms', 'st', 'vs', 'no', 'etc', 'inc', 'jr', 'sr', 'e.g', 'i.e'})
QUEUE_LIMIT = 6           # phrases waiting before `put` waits for the worker


def _sentence_end(text, index):
    """Where the sentence ends if the mark at `index` ends one (just past it and any closing quote), False when it does not, None
    when that cannot be told until more text arrives."""
    mark = text[index]
    if index + 1 < FIRST_MIN:
        return False
    end = index + 1
    while end < len(text) and text[end] in CLOSERS:
        end += 1
    following = text[end] if end < len(text) else None
    if following is not None and following in SENTENCE_CHARS:
        return False                                   # "?!" or "...": the last mark ends it
    if following is not None and not following.isspace():
        return False                                   # 3.5, 10:30, e.g, a.b
    if mark == '.' and following is None and index and text[index - 1].isdigit():
        return None                                    # "3." may be the start of "3.5"
    word = re.search(r'([A-Za-z.]+)$', text[:index])
    if mark == '.' and word and word.group(1).lower() in ABBREVIATIONS:
        return False
    return end


def _word_cut(text, limit, *, hard=True):
    """A cut at the last space before `limit`, else at the first one after it (never inside a word). Text with no space at all is
    cut at `limit` only when `hard` (otherwise None: more text may still bring one)."""
    cut = text.rfind(' ', limit // 2, limit + 1)
    if cut <= 0:
        cut = text.find(' ', limit)
    if cut > 0:
        return cut
    return limit if hard else None


def _last_sentence_end(text, limit):
    """The last full-sentence end at or before `limit` characters, or 0."""
    last = 0
    for index, mark in enumerate(text[:limit]):
        if mark in SENTENCE_CHARS:
            end = _sentence_end(text, index)
            if end and end <= limit:
                last = end
    return last


def wordless(text):
    """Whether a text holds nothing to hear (a mark, an emoji)."""
    return not any(letter.isalnum() for letter in text)


def last_sentence(text):
    """(everything before the last full sentence, that sentence), in any script: a reply's question that comes at its end. The whole text is
    one sentence when no other ends inside it; what follows the last one without a word in it belongs to it."""
    body, cut = text.rstrip(), 0
    for index, mark in enumerate(body):
        if mark in WIDE:
            end = index + 1
            while end < len(body) and body[end] in WIDE + CLOSERS + WIDE_CLOSERS:
                end += 1
        elif mark in SENTENCE_CHARS:
            end = _sentence_end(body, index)
        else:
            continue
        if end and not wordless(body[end:]):
            cut = end
    return text[:cut], text[cut:]


class FirstClause:
    """Splits a growing reply into phrases for synthesis. `take(buffer)` returns (phrase to speak, what remains) or None."""

    def __init__(self, min_words=FIRST_WORDS):
        self.first = True
        self.min_words = min_words

    def take(self, buffer):
        cut = None
        for index, mark in enumerate(buffer):
            if mark in SENTENCE_CHARS:
                end = _sentence_end(buffer, index)
                if end is None:
                    break                                  # cannot tell yet; what came before is still released
                if end:
                    cut = end
                    if self.first and len(buffer[:end].split()) >= self.min_words:
                        break                              # the first phrase: the first sentences with at least FIRST_WORDS words
        if self.first and cut is not None and len(buffer[:cut].split()) < self.min_words:
            cut = None                                     # too short alone: wait for the next sentence (or the reply's end)
        if cut is None and len(buffer) > MERGE_LIMIT:
            cut = _word_cut(buffer, MERGE_LIMIT, hard=len(buffer) > 2 * MERGE_LIMIT)
        if not cut:
            return None
        self.first = False
        return buffer[:cut], buffer[cut:]


class SpeechQueue:
    """One ordered, bounded worker for `speak(text)` calls that merges what is waiting. Create inside the running loop."""

    def __init__(self, speak, *, limit=QUEUE_LIMIT, merge=MERGE_LIMIT):
        self._speak = speak
        self._items = collections.deque()
        self._limit, self._merge = limit, merge
        self._ready = asyncio.Event()
        self._room = asyncio.Event()
        self._room.set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._worker = None
        self.error = None
        self.requests = 0                              # speak calls made (for the timing marks)

    async def put(self, text):
        self.raise_if_failed()
        if not text or not text.strip():
            return
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())
            self._worker.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        while len(self._items) >= self._limit:
            self._room.clear()
            await self._room.wait()
            self.raise_if_failed()
        self._items.append(text)
        self._idle.clear()
        self._ready.set()
        self.raise_if_failed()

    def raise_if_failed(self):
        if self.error is not None:
            raise self.error

    async def drain(self):
        """Every phrase put has been handed to the speak call and it returned (or the first failure is raised)."""
        await self._idle.wait()
        self.raise_if_failed()

    def cancel(self):
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        return self._worker

    def _next(self):
        """The next request: everything waiting, merged up to the limit and cut after a full sentence (or, failing one, a word)."""
        text = self._items.popleft()
        while self._items and len(text) + len(self._items[0]) <= self._merge:
            text += self._items.popleft()
        if len(text) > self._merge:
            cut = _last_sentence_end(text, self._merge) or _word_cut(text, self._merge)
            text, rest = text[:cut], text[cut:]
            if rest.strip():
                self._items.appendleft(rest)
        self._room.set()
        return text.strip()

    async def _run(self):
        while True:
            if not self._items:
                self._idle.set()
                self._ready.clear()
                await self._ready.wait()
                continue
            text = self._next()
            try:
                if self.error is None and text:
                    self.requests += 1
                    await self._speak(text)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Later phrases are dropped; the turn raises it where it next looks. Without this worker's frames: a caller that
                # clears a caught exception's frames must not finalise the still-running worker.
                self.error = error.with_traceback(None)
                self._items.clear()
                self._room.set()
