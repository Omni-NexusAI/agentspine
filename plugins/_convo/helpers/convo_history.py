"""The conversation model's history as chat messages: one rule, used by the gateway (sidecar/app.py) and the developer view.

What the model sees decides what it says next. Earlier versions told it "[The user spoke; their words were not captured.]" for a turn
with no words, and that placeholder (with the user-line wording) primed replies about audio quality, which then filled the history and
were repeated. The rule now:
* a turn the model declined as not for it (`held` silent) or that a newer fragment took over (`superseded`) never appears; a thought
  it waited on (`held` wait) does, with the user's words and no reply: what they say next continues it, however long they paused;
* every assistant message follows the user's words (a Whisper transcript, the model's own USER line or the classifier's note), else
  the turn's audio while it is still in memory, for a model that takes audio;
* an exchange with neither, but in which Convo used a tool and its answer was heard, stays: the dated tool use (what was looked up)
  stands in the user's place, never a placeholder (2026-11-25: a heard, searched answer was left out because the user's words were
  not captured, and a minute later the model said it had never mentioned it);
* any other exchange with neither is left out entirely, both sides: the model never sees an answer to a question it cannot see;
* an interrupted reply says so;
* consecutive user messages (a turn that got no reply) are joined, so roles always alternate (some chat templates require it).
"""
from __future__ import annotations

INTERRUPTED = " — (interrupted by the user)"
TEXT_LIMIT = 12000


def words(turn):
    return str(turn.get("transcript") or turn.get("user_line") or turn.get("note") or "")


def hidden(turn):
    """Whether a turn's content is left out of what the model sees."""
    return bool(turn.get("superseded")) or (bool(turn.get("held")) and turn.get("held") != "wait")


def _parts(content):
    return content if isinstance(content, list) else [{"type": "text", "text": content}]


# Separates the system text that stays the same on every request of a conversation from this pass's part (api/realtime.py
# _system_prompt builds it, sidecar/app.py split_system sends the second part after the history). Never spoken or shown as is.
LATE_MARK = "\n\u2063[convo:this-turn]\u2063\n"


def add_user(messages, content):
    """Append a user message, joined to a user message right before it."""
    if messages and messages[-1].get("role") == "user":
        last = messages[-1]["content"]
        if isinstance(last, str) and isinstance(content, str):
            messages[-1] = {"role": "user", "content": last + "\n" + content}
        else:
            messages[-1] = {"role": "user", "content": _parts(last) + _parts(content)}
    else:
        messages.append({"role": "user", "content": content})
    return messages


def messages(history, *, audio_input, audio_part=None, tool_messages=False):
    """Earlier turns as alternating messages (see the module rule). `audio_part(clip)` returns the content for a turn's in-memory
    audio (the gateway validates and wraps it; the developer view shows a placeholder); without it audio is shown as "[audio]".
    A turn's tool uses (`tools`, convo_tools.history_entry) come after the user's words: the assistant's call and the tool's answer
    when `tool_messages` (a request that offers tools natively), else a dated note added to the user's message."""
    from . import convo_tools
    result = []
    for turn in history or ():
        if not isinstance(turn, dict) or hidden(turn):
            continue
        user = words(turn)
        clip = turn.get("user_audio") if audio_input else None
        if user:
            content = user[:TEXT_LIMIT]
        elif clip:
            # One request may have been said in several clips (convo_continuation parts): all of them, in order.
            heard = [audio_part(one) if audio_part else "[audio]" for one in (clip if isinstance(clip, list) else [clip])]
            content = " ".join(heard) if all(isinstance(one, str) for one in heard) else [part for one in heard for part in _parts(one)]
        else:
            content = ""
        uses = [entry for entry in (turn.get("tools") or [])[:convo_tools.HISTORY_LIMIT] if isinstance(entry, dict) and entry.get("name")]
        if not content:
            if not (uses and str(turn.get("assistant") or "").strip()):
                continue
            # The look-up itself says what was asked: shown as text in either form (a call needs a user message before it).
            content, uses = "\n".join(convo_tools.history_text(entry) for entry in uses), []
        if uses and not tool_messages:
            notes = "\n".join(convo_tools.history_text(entry) for entry in uses)
            content = (content + "\n" + notes) if isinstance(content, str) else content + [{"type": "text", "text": notes}]
        add_user(result, content)
        if uses and tool_messages:
            for entry in uses:
                result.extend(convo_tools.history_messages(entry))
        assistant = str(turn.get("assistant") or "")
        if assistant:
            result.append({"role": "assistant", "content": assistant[:TEXT_LIMIT] + (INTERRUPTED if turn.get("interrupted") else "")})
    return result


def audio_seconds(clip):
    """A base64 16-bit mono WAV's length, estimated from its size (for display only)."""
    return round(len(clip or "") * 3 / 4 / 32000, 1)


def view(request):
    """What the conversation model was sent, for the developer view: roles and text, audio as a placeholder, never the audio."""
    def placeholder(clip):
        return "[audio %.1f s]" % audio_seconds(clip)
    shown = [{"role": "system", "text": str(request.get("system", ""))[:20000]}]
    for message in messages(request.get("history"), audio_input=True, audio_part=placeholder):
        content = message["content"]
        text = content if isinstance(content, str) else " ".join(part.get("text", "") for part in content)
        shown.append({"role": message["role"], "text": text[:4000]})
    current = placeholder(request["audio"]) if request.get("audio") else str(request.get("transcript") or "")[:4000]
    if request.get("earlier_audio") and request.get("audio"):
        current = " ".join(placeholder(clip) for clip in request["earlier_audio"]) + " " + current
    if request.get("context_audio"):
        current = "[%d overheard clip(s)] " % len(request["context_audio"]) + current
    add_user_view = shown[-1]["role"] == "user"
    if add_user_view:
        shown[-1] = {"role": "user", "text": shown[-1]["text"] + "\n" + current}
    else:
        shown.append({"role": "user", "text": current})
    return shown
