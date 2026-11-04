"""First audible sooner: speak the first clause early, and keep reading the model while earlier phrases are synthesised.

Two small parts, both off unless the `first_clause_speech` setting is on:

* `FirstClause` decides where a phrase ends. The first phrase is released as soon as it holds a clause (at least three characters
  ending in one of `, . ? ! ; :`) or, failing that, about forty characters cut at a word boundary. Every later phrase keeps today's
  rule (sixty characters ending in `. ! ?`, or eight hundred). A decimal point, a time such as 10:30 and a short abbreviation are
  not clause ends.
* `SpeechQueue` runs the phrase synthesis in one background worker, strictly in order, so reading the model's text and showing it
  does not stall while a phrase is being synthesised. It is bounded, it never runs two syntheses at once (the speech provider
  admits one request at a time and answers a typed SpeechBusy otherwise, which the speak call already waits out), and it
  reports the first failure to the turn exactly where a direct call would have raised it. Cancelling the turn cancels the worker.

Playback order, per-phrase receipts, the busy wait and the two-second cancel contract are properties of the speak call and the
turn, not of this module; it only changes when the call is made."""
from __future__ import annotations

import asyncio
import re

CLAUSE_CHARS = ',.?!;:'
FIRST_MIN = 3             # characters in the first clause
FIRST_LIMIT = 40          # no clause end by here: cut at a word boundary
LATER_MIN = 60            # today's rule for every later phrase
LATER_MAX = 800
ABBREVIATIONS = frozenset({'dr', 'mr', 'mrs', 'ms', 'st', 'vs', 'no', 'etc', 'inc', 'jr', 'sr', 'e.g', 'i.e'})
QUEUE_LIMIT = 6


def _is_clause_end(phrase, index):
    """Whether the punctuation mark at `index` ends a clause (None: cannot tell until more text arrives)."""
    mark = phrase[index]
    if index + 1 < FIRST_MIN:
        return False
    following = phrase[index + 1] if index + 1 < len(phrase) else None
    if mark in '?!;':
        return True
    if following is not None and not following.isspace():
        return False                                   # 3.5, 10:30, e.g, a.b
    if following is None and index and phrase[index - 1].isdigit():
        return None                                    # "3." may be the start of "3.5"
    word = re.search(r'([A-Za-z.]+)$', phrase[:index])
    if mark == '.' and word and word.group(1).lower() in ABBREVIATIONS:
        return False
    return True


class FirstClause:
    """Splits a growing reply into phrases for synthesis. `take(buffer)` returns (phrase to speak, what remains) or None."""

    def __init__(self):
        self.first = True

    def take(self, buffer):
        if not self.first:
            if len(buffer) >= LATER_MIN and re.search(r"[.!?]\s*$", buffer) or len(buffer) >= LATER_MAX:
                return buffer, ''
            return None
        for index, mark in enumerate(buffer):
            if mark in CLAUSE_CHARS:
                verdict = _is_clause_end(buffer, index)
                if verdict is None:
                    return None
                if verdict:
                    self.first = False
                    return buffer[:index + 1], buffer[index + 1:]
        if len(buffer) >= FIRST_LIMIT:
            cut = buffer.rfind(' ', FIRST_LIMIT // 2, FIRST_LIMIT + 1)
            if cut < 0:
                cut = buffer.find(' ', FIRST_LIMIT)               # one long word: end it, then cut
            if cut > 0:
                self.first = False
                return buffer[:cut], buffer[cut:]
        return None


class SpeechQueue:
    """One ordered, bounded worker for `speak(text)` calls. Create inside the running loop."""

    def __init__(self, speak, *, limit=QUEUE_LIMIT):
        self._speak = speak
        self._queue = asyncio.Queue(maxsize=limit)
        self._worker = None
        self.error = None

    async def put(self, text):
        self.raise_if_failed()
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())
            self._worker.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        await self._queue.put(text)
        self.raise_if_failed()

    def raise_if_failed(self):
        if self.error is not None:
            raise self.error

    async def drain(self):
        """Every queued phrase has been handed to the speak call and it returned (or the first failure is raised)."""
        await self._queue.join()
        self.raise_if_failed()

    def cancel(self):
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        return self._worker

    async def _run(self):
        while True:
            text = await self._queue.get()
            try:
                if self.error is None:
                    await self._speak(text)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.error = error                     # later phrases are dropped; the turn raises it where it next looks
            finally:
                self._queue.task_done()
