"""Continued and corrected thoughts: join a following fragment to the one that is still unanswered.

Memory only. Adapted from the standalone fork's reopen tracker (`speech_to_speech/pipeline/speculative_turns.py` and the reopen
limits in `VAD/vad_handler.py`, Apache-2.0, same owner): a turn whose reply has not been committed may be reopened by a following
fragment within a fixed horizon, a bounded number of times and up to a bounded combined length. Here that is one small object per
voice session:

* an utterance the classifier called `unfinished` is no longer dropped: its audio is held in RAM;
* an utterance that was admitted but whose reply has not started to be heard is remembered the same way;
* a following completed fragment that starts within the horizon is concatenated (the real gap as short silence) and classified as one
  turn, so the user's correction or extension is answered once, with everything they said.

Active listening only. The audio exists only inside this object: never journaled, never written to disk, never sent anywhere except
as the merged audio to the classifier and the conversation model. It is dropped on stop, retarget, disable, when the reply is
committed (the first audio of it was sent: normal barge-in rules apply from then on) and when its horizon has passed.

Origin: Omni-NexusAI speech-to-speech `pipeline/speculative_turns.py` (Apache-2.0)."""
from __future__ import annotations

import asyncio
import base64
import io
import wave
from dataclasses import dataclass

MAX_REVISIONS = 8
HARD_CAP_SECONDS = 30.0
MAX_SILENCE = 1.5          # a longer real pause is joined as this much silence


def parse_wav(encoded):
    """(rate, pcm bytes) of a mono PCM16 WAV already validated by the caller."""
    with wave.open(io.BytesIO(base64.b64decode(encoded))) as reader:
        return reader.getframerate(), reader.readframes(reader.getnframes())


def build_wav(rate, pcm):
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(rate)
        writer.writeframes(pcm)
    return base64.b64encode(output.getvalue()).decode()


@dataclass
class Pending:
    rate: int
    pcm: bytes
    arrived: float            # loop time the fragment's final reached the server (its speech had just ended)
    turn: str                 # the admitted turn whose unanswered reply a follower supersedes ('' for an unfinished fragment)
    revisions: int = 0

    @property
    def seconds(self):
        return len(self.pcm) / (2 * self.rate)


@dataclass
class Merge:
    audio: str                # base64 WAV of the joined utterance
    supersede: str            # admitted turn to supersede ('' when the earlier fragment was only held)
    revisions: int
    gap: float
    seconds: float            # the joined utterance
    fragment: float = 0.0     # the new fragment alone


class Continuation:
    def __init__(self, horizon, max_seconds, *, max_revisions=MAX_REVISIONS, hard_cap=HARD_CAP_SECONDS):
        self.horizon = float(horizon)
        self.limit = min(float(max_seconds), hard_cap)
        self.max_revisions = max_revisions
        self.pending = None
        self._timer = None

    @property
    def enabled(self):
        return self.horizon > 0

    def clear(self):
        self.pending = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _keep(self, pending):
        self.clear()
        self.pending = pending
        # Speech may start at the end of the horizon and still be finishing up to one full turn later.
        self._timer = asyncio.get_running_loop().call_later(self.horizon + self.limit, self.clear)

    def hold(self, encoded, arrived, *, revisions=0):
        """An utterance the classifier called unfinished: keep its audio for the fragment that follows it."""
        if not self.enabled or revisions >= self.max_revisions:
            return self.clear()
        rate, pcm = parse_wav(encoded)
        self._keep(Pending(rate, pcm, arrived, '', revisions))

    def remember(self, encoded, arrived, turn, *, revisions=0):
        """An admitted utterance whose reply has not started to be heard: a follower may take it over."""
        if not self.enabled or revisions >= self.max_revisions:
            return self.clear()
        rate, pcm = parse_wav(encoded)
        self._keep(Pending(rate, pcm, arrived, turn, revisions))

    def committed(self, turn):
        """The first audio of this turn's reply was sent: it can no longer be superseded, so its audio is dropped."""
        if self.pending is not None and self.pending.turn == turn:
            self.clear()

    def holds(self, turn):
        return self.pending is not None and self.pending.turn == turn

    def plan(self, encoded, arrived):
        """How a completed fragment would join the pending one, or None (the pending audio is dropped when it is too old to join)."""
        pending = self.pending
        if pending is None or not self.enabled:
            return None
        rate, pcm = parse_wav(encoded)
        seconds = len(pcm) / (2 * rate)
        gap = max(0.0, arrived - seconds - pending.arrived)
        if gap > self.horizon:
            self.clear()
            return None
        silence = min(gap, MAX_SILENCE)
        joined = pending.seconds + silence + seconds
        if rate != pending.rate or pending.revisions + 1 > self.max_revisions or joined > self.limit:
            return None
        gap_bytes = b'\0\0' * int(round(silence * rate))
        return Merge(build_wav(rate, pending.pcm + gap_bytes + pcm), pending.turn, pending.revisions + 1, gap, joined, seconds)
