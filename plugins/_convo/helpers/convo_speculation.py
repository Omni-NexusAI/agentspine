"""A held parallel reply: the main model starts on a completed utterance while the classifier is still deciding.

The generation is only buffered. Nothing here shows, speaks, records or runs anything: the caller that owns the turn replays the
held items through its normal output path only after the classifier admitted the turn, and drops the whole thing otherwise. A
generation that failed, overflowed or was built from different inputs than the admitted turn needs is never promoted; the turn is
then answered by an ordinary second request, exactly as if speculation were off.

One model HTTP operation per conversation still holds: the caller starts at most one speculation, and only while no reply of its
own is in flight. Held text never enters history on its own; it is the admitted turn's settle/delivery path that does that."""
from __future__ import annotations

import asyncio

MAX_ITEMS = 4000          # buffered stream items (text deltas, proposals, markers)
MAX_CHARS = 12000         # the voice answer limit of a turn; a longer held reply is discarded, not truncated
TOOL_NAMES = frozenset({'tool.proposal'})


class Speculation:
    """One buffered answer stream. `start` is a callable returning the async iterator of answer items for `request`."""

    def __init__(self, start, request):
        self.request = request
        self.items = []
        self.chars = 0
        self.status = 'running'            # running | done | failed | overflow | cancelled
        self.closed = False                # promoted or discarded: the outcome has been decided (and marked) once
        self._wake = asyncio.Event()
        self.task = asyncio.create_task(self._run(start(request)))
        self.task.add_done_callback(lambda task: None if task.cancelled() else task.exception())

    async def _run(self, stream):
        try:
            async for item in stream:
                self.items.append(item)
                if item.get('type') == 'text.delta':
                    self.chars += len(item.get('text', ''))
                if len(self.items) > MAX_ITEMS or self.chars > MAX_CHARS:
                    self.status = 'overflow'
                    return
                self._wake.set()
                if item.get('type') == 'error':
                    self.status = 'failed'
                    return
            self.status = 'done'
        except asyncio.CancelledError:
            self.status = 'cancelled'
            raise
        except Exception:
            self.status = 'failed'
        finally:
            self._wake.set()

    @property
    def proposals(self):
        return [item for item in self.items if item.get('type') in TOOL_NAMES]

    def cancel(self):
        """Ask the generation to stop. The caller bounds how long it waits (the cancel coordinator); a provider that ignores
        cancellation is detached, and whatever it still produces lands in a buffer nobody reads."""
        if not self.task.done():
            self.task.cancel()
        return self.task

    def decide(self):
        """True the first time the outcome is decided (promoted or discarded), so each is marked exactly once."""
        first = not self.closed
        self.closed = True
        return first

    def mismatch(self, request, *, allow_tools):
        """Why this generation cannot answer the admitted turn, or '' when it can.

        The comparison is on what the model was given: the system text, the history, the audio or
        transcript and whether tools were offered. A held reply that proposed a tool is only usable when this turn may use tools."""
        if self.status in {'failed', 'overflow', 'cancelled'}:
            return 'error' if self.status == 'failed' else self.status
        mine = self.request
        if mine['system'] != request['system']:
            return 'system'
        if mine['history'] != request['history']:
            return 'history'
        if (mine.get('audio') != request.get('audio') or mine.get('transcript') != request.get('transcript')
                or mine.get('context_audio') != request.get('context_audio')):
            return 'input'
        if self.proposals and not allow_tools:
            return 'tools'
        if mine.get('allow_tools') is False and allow_tools:
            return 'tools'
        return ''

    async def stream(self):
        """Everything held so far, then whatever arrives until the generation ends. A failure after promotion is an error item."""
        index = 0
        while True:
            while index < len(self.items):
                yield self.items[index]
                index += 1
            if self.status != 'running':
                break
            self._wake.clear()
            if index < len(self.items) or self.status != 'running':
                continue
            await self._wake.wait()
        if self.status in {'failed', 'overflow'} and not any(item.get('type') == 'error' for item in self.items):
            yield {'type': 'error', 'message': 'Conversation provider failed; no fallback was used'}
