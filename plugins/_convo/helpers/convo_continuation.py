"""Continued and corrected thoughts: join a following fragment to the one that is still unanswered.

Memory only. Adapted from the standalone fork's reopen tracker (`speech_to_speech/pipeline/speculative_turns.py` and the reopen
limits in `VAD/vad_handler.py`, Apache-2.0, same owner): a turn whose reply has not been committed may be reopened by a following
fragment within a fixed horizon, a bounded number of times and up to a bounded combined length. Here that is one small object per
voice session:

* an utterance the classifier called `unfinished` is no longer dropped: its audio is held in RAM;
* an utterance that was admitted but whose reply has not started to be heard is remembered the same way;
* a following completed fragment is concatenated (the real gap as short silence) and classified as one turn, so the user's correction
  or extension is answered once, with everything they said. It may begin within the horizon, or (`late`: a request, not a mere remark)
  at any later time while the answer to the earlier part is still being prepared: nothing of it was heard, so it is still the same
  request, and a slow model must not cost the user what they said;
* what no longer fits one clip is kept as earlier parts (`Merge.parts`), which the model hears before the latest one. Live 2026-11-24:
  a thought of more than 20 s in several pauses, and a pause of more than 7 s before a slow answer, lost everything said before.

Active listening only. The audio exists only inside this object: never journaled, never written to disk, never sent anywhere except
as the merged audio to the classifier and the conversation model. It is dropped on stop, retarget, disable, when the reply is
committed (the first audio of it was sent: normal barge-in rules apply from then on) and when its horizon has passed with no answer
being prepared.

Origin: Omni-NexusAI speech-to-speech `pipeline/speculative_turns.py` (Apache-2.0)."""
from __future__ import annotations

import asyncio
import base64
import io
import wave
from dataclasses import dataclass

MAX_REVISIONS = 24
HARD_CAP_SECONDS = 30.0    # one joined clip
SAME_BREATH = 2.5          # a pause this short within one request, whatever its length
MAX_PARTS = 6              # earlier clips kept beside the joined one when everything said no longer fits in it
TOTAL_SECONDS = 75.0       # ...and all of one request together
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


def whole(earlier, encoded):
    """One WAV of everything a request was said in (its earlier clips and the newest), for a transcript; the newest alone when they
    cannot be put together."""
    try:
        rate, pcm = parse_wav(encoded)
        before = [parse_wav(clip) for clip in earlier]
    except Exception:
        return encoded
    if not before or any(other != rate for other, _ in before):
        return encoded
    gap = bytes(2 * int(rate * .4))
    return build_wav(rate, b''.join(clip + gap for _, clip in before) + pcm)


@dataclass
class Pending:
    rate: int
    pcm: bytes
    arrived: float            # loop time the fragment's final reached the server (its speech had just ended)
    turn: str                 # the admitted turn whose unanswered reply a follower supersedes ('' for an unfinished fragment)
    revisions: int = 0
    parts: tuple = ()         # earlier clips of the same request (base64 WAV, seconds), oldest first

    @property
    def seconds(self):
        return len(self.pcm) / (2 * self.rate)

    @property
    def total(self):
        return self.seconds + sum(seconds for _, seconds in self.parts)


@dataclass
class Merge:
    audio: str                # base64 WAV of the joined utterance (the newest part of it, when `parts` is not empty)
    supersede: str            # admitted turn to supersede ('' when the earlier fragment was only held)
    revisions: int
    gap: float
    seconds: float            # the joined utterance
    fragment: float = 0.0     # the new fragment alone
    parts: tuple = ()         # earlier clips of the same request that did not fit into `audio` (base64 WAV, seconds), oldest first
    late: bool = False        # only a request joins: said after the horizon while the answer was still being prepared, or a clip of
                              # its own that did not follow in the same breath (a room's remarks must not put an answer off for long)

    @property
    def earlier(self):
        return [clip for clip, _ in self.parts]


class Continuation:
    def __init__(self, horizon, max_seconds, *, max_revisions=MAX_REVISIONS, hard_cap=HARD_CAP_SECONDS):
        self.horizon = float(horizon)
        self.limit = min(float(max_seconds), hard_cap)
        self.max_revisions = max_revisions
        self.pending = None
        self.alive = None         # (turn) -> whether the answer to that turn is still being prepared (set by the session)
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
        self._timer = asyncio.get_running_loop().call_later(self.horizon + self.limit, self._expire)

    def _expire(self):
        pending = self.pending
        if pending is not None and pending.turn and self.alive is not None and self.alive(pending.turn):
            # Its answer is still being prepared (a slow model, a tool): it stays joinable until that answer is heard or over.
            self._timer = asyncio.get_running_loop().call_later(self.horizon, self._expire)
        else:
            self.clear()

    def hold(self, encoded, arrived, *, revisions=0, parts=()):
        """An utterance the classifier called unfinished: keep its audio for the fragment that follows it."""
        if not self.enabled or revisions >= self.max_revisions:
            return self.clear()
        rate, pcm = parse_wav(encoded)
        self._keep(Pending(rate, pcm, arrived, '', revisions, tuple(parts)))

    def remember(self, encoded, arrived, turn, *, revisions=0, parts=()):
        """An admitted utterance whose reply has not started to be heard: a follower may take it over."""
        if not self.enabled or revisions >= self.max_revisions:
            return self.clear()
        rate, pcm = parse_wav(encoded)
        self._keep(Pending(rate, pcm, arrived, turn, revisions, tuple(parts)))

    def committed(self, turn):
        """The first audio of this turn's reply was sent: it can no longer be superseded, so its audio is dropped."""
        if self.pending is not None and self.pending.turn == turn:
            self.clear()

    def holds(self, turn):
        return self.pending is not None and self.pending.turn == turn

    def plan(self, encoded, arrived, pre_roll=0.0, open=False):
        """How a completed fragment would join the pending one, or None (the pending audio is dropped when it is too old to join).
        `open`: the answer to the pending one was still being prepared when this speech began, so it joins however long ago it was.

        `pre_roll`: seconds at the fragment's start that the browser kept from before the speech began (its pre-roll). They are
        audio from the gap, not speech, so the gap and the fragment's own length (the "short command" test) are measured without them;
        the joined audio keeps them (the real sound of the gap)."""
        pending = self.pending
        if pending is None or not self.enabled:
            return None
        rate, pcm = parse_wav(encoded)
        seconds = len(pcm) / (2 * rate)
        lead = min(max(0.0, float(pre_roll or 0.0)), seconds)
        spoken = seconds - lead
        gap = max(0.0, arrived - spoken - pending.arrived)
        if gap > self.horizon and not open:
            self.clear()
            return None
        if pending.revisions + 1 > self.max_revisions or pending.total + seconds > TOTAL_SECONDS:
            return None
        silence = max(0.0, min(gap, MAX_SILENCE) - lead)          # the pre-roll already fills that much of the gap
        joined = pending.seconds + silence + seconds
        if rate != pending.rate or joined > self.limit:
            # It does not fit one clip: what was said before stays a clip of its own, heard before this one.
            parts = pending.parts + ((build_wav(pending.rate, pending.pcm), pending.seconds),)
            if len(parts) > MAX_PARTS:
                return None
            return Merge(encoded, pending.turn, pending.revisions + 1, gap, seconds, spoken, parts, gap > self.horizon or gap > SAME_BREATH)
        gap_bytes = b'\0\0' * int(round(silence * rate))
        return Merge(build_wav(rate, pending.pcm + gap_bytes + pcm), pending.turn, pending.revisions + 1, gap, joined, spoken, pending.parts,
                     gap > self.horizon)
