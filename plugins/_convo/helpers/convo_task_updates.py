"""Which delegated-job events are spoken aloud. Pure: no host, network or model access.

`spoken_updates` (completion | milestones | frequent) decides how chatty Convo is about background work. Rules:

* permission, error (failed) and decision-needed events are ALWAYS spoken, at every level;
* `completion`: also the final outcome (completed);
* `milestones`: also a meaningful result (milestone) and a blocker (blocked); routine progress stays text only;
* `frequent`: also started and progress, but at most one such update per FREQUENT_MIN_GAP seconds so it never narrates every
  step (the caller reports `seconds_since_last_spoken`);
* queued/unknown events are never spoken, and a queued or running job is never described as finished.

Today Agent Zero reports a delegated job to Convo only as queued, running or ended (`completed`, `failed`, `uncertain`);
`event_kind` maps those honestly, so `milestones` and `frequent` differ from `completion` only once a source emits
milestone/blocked/progress events. "Spoken" is separate from "shown": every event stays visible in the chat and journal.
"""
from __future__ import annotations

from dataclasses import dataclass

from .convo_contract import DEFAULTS, SPOKEN_UPDATES

ALWAYS = frozenset({"permission", "failed", "decision_needed"})
BY_LEVEL = {
    "completion": ALWAYS | {"completed"},
    "milestones": ALWAYS | {"completed", "milestone", "blocked"},
    "frequent": ALWAYS | {"completed", "milestone", "blocked", "started", "progress"},
}
THROTTLED = frozenset({"started", "progress"})
KINDS = frozenset({"started", "progress", "milestone", "blocked", "decision_needed", "completed", "failed", "permission"})
FREQUENT_MIN_GAP = 20.0

# Store job status -> event kind. `queued` is deliberately not a spoken kind; `uncertain` is an error for the user.
STATUS_KIND = {"queued": "queued", "dispatching": "queued", "running": "started", "completed": "completed",
               "failed": "failed", "uncertain": "failed", "cancel_requested": "cancelled", "cancelled": "cancelled"}

STATUS_WORDS = {
    "queued": "has been queued and has not started yet",
    "dispatching": "is being handed to the chat and has not started yet",
    "running": "is still running",
    "cancel_requested": "is being cancelled and has not stopped yet",
    "cancelled": "was cancelled",
    "completed": "has finished",
    "failed": "failed",
    "uncertain": "ended without a confirmed result, so its outcome is unknown",
}


@dataclass(frozen=True)
class UpdateDecision:
    speak: bool
    reason: str


def event_kind(status):
    """The event kind of a job status reported by the store; unknown statuses map to `unknown` (never spoken)."""
    return STATUS_KIND.get(status, "unknown")


def status_words(status):
    """Plain wording that is true for the status; a queued or running job is never described as finished."""
    return STATUS_WORDS.get(status, "has an unknown status")


def notice_instruction(status):
    """The one-line instruction for a spoken job update, matched to the real status (not always 'completed')."""
    return "Give a brief update: the background task " + status_words(status) + "."


def should_speak_update(level, event):
    """`event` is a kind string or a mapping with `kind` (and optionally `seconds_since_last_spoken`)."""
    if level not in SPOKEN_UPDATES:
        level = DEFAULTS["spoken_updates"]
    gap = None
    if isinstance(event, dict):
        kind, gap = event.get("kind"), event.get("seconds_since_last_spoken")
    else:
        kind = event
    if kind not in KINDS:
        return UpdateDecision(False, "unknown_event")
    if kind in ALWAYS:
        return UpdateDecision(True, "always_spoken")
    if kind not in BY_LEVEL[level]:
        return UpdateDecision(False, "text_only_at_" + level)
    if kind in THROTTLED and isinstance(gap, (int, float)) and gap < FREQUENT_MIN_GAP:
        return UpdateDecision(False, "rate_limited")
    return UpdateDecision(True, "level_" + level)
