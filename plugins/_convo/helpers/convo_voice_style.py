"""Conversational style rules for the voice model: pure text, no host, network or model access.

Two shared settings shape *how much* Convo says. They are separate from personality (which is inherited from the selected
Agent Zero agent plus `style`) and never touch permissions, tools or what a delegated job may do:

* `response_detail` (concise | balanced | detailed): the default length and depth of a spoken answer.
* `spoken_updates` (completion | milestones | frequent): when background work is announced aloud
  (the model-facing wording here; the spoken/not-spoken decision itself is `convo_task_updates.should_speak_update`).

Application boundaries (the contract the turn engine must keep):

* NEXT_RESPONSE: `response_detail` and `spoken_updates` are read from the *current* saved settings at the start of every
  response, so a change in either settings editor applies to the next answer of a running conversation.
* SESSION_START: the inherited persona (extracted from the host agent's prompt) and `style` are read when a conversation
  starts or is retargeted; see `Host.persona_cached` for the optional background refresh.
* An explicit per-request preference ("briefly", "in detail", "just the headline") overrides the default for that answer only
  and is never remembered.
"""
from __future__ import annotations

import json

from .convo_contract import DEFAULTS, RESPONSE_DETAIL, SPOKEN_UPDATES

NEXT_RESPONSE = ("response_detail", "spoken_updates")
SESSION_START = ("persona", "style")
PERSONA_LIMIT = 1600

DETAIL = {
    "concise": ("Default answer length: concise. Lead with the answer or headline in one or two short sentences and stop; "
                "give reasons or specifics only when asked."),
    "balanced": ("Default answer length: balanced. Answer directly in a few short sentences with the key reason or next step; "
                 "leave out secondary detail unless asked."),
    "detailed": ("Default answer length: detailed. Explain fully, with the reasoning and relevant specifics, in clear spoken "
                 "sentences; still no formatting and no reading identifiers aloud. This default replaces the general "
                 "instruction to be concise."),
}

UPDATES = {
    "completion": ("Background tasks: say nothing about a delegated task while it is queued or running. Report it when it has "
                   "finished or failed, or when it needs a permission or a decision from the user. Routine progress is shown "
                   "as text only."),
    "milestones": ("Background tasks: report a meaningful result, a blocker, or a decision the user must make, plus the final "
                   "outcome or a failure. Never narrate each step; routine progress is shown as text only."),
    "frequent": ("Background tasks: short progress updates are welcome when something noticeable changes, one brief sentence "
                 "each, still never a step-by-step narration. Always report the final outcome, a failure, a permission "
                 "request or a decision the user must make."),
}

TASK_STATE = ("A queued or running task is not finished; never describe it as completed, and say only what the task status "
              "data confirms.")
OVERRIDE = ('An explicit per-request preference ("briefly", "in detail", "just the headline") overrides these defaults for '
            "that answer only.")


def choice(settings, key, allowed):
    """The configured value, or the shipped default when it is missing or not one of the allowed choices."""
    value = settings.get(key) if isinstance(settings, dict) else None
    return value if isinstance(value, str) and value in allowed else DEFAULTS[key]


def style_settings(read, fallback):
    """Settings for the next response: `read()` (the current saved settings) or, if that fails, the session's own copy.
    Reading must never end a conversation, so any failure keeps what the session already has."""
    try:
        current = read()
    except Exception:
        return fallback
    return current if isinstance(current, dict) else fallback


def voice_rules(settings, base_rules):
    """`base_rules` unchanged, then the answer-length and task-update instructions, then the per-request override rule."""
    detail = choice(settings, "response_detail", RESPONSE_DETAIL)
    updates = choice(settings, "spoken_updates", SPOKEN_UPDATES)
    return "\n".join((base_rules, DETAIL[detail], UPDATES[updates], TASK_STATE, OVERRIDE))


def persona_data(persona, style):
    """The persona/style block exactly as the turn engine puts it after the rules: JSON-encoded data, bounded, never rules.
    Quotes, newlines and role-like text inside the persona stay inside one JSON string."""
    return "Persona and style data: " + json.dumps(
        {"persona": str(persona)[:PERSONA_LIMIT], "style": str(style)[:PERSONA_LIMIT]}, ensure_ascii=False)
