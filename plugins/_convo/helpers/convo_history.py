"""The conversation model's history as chat messages: one rule, used by the gateway (sidecar/app.py) and the developer view.

What the model sees decides what it says next. Earlier versions told it "[The user spoke; their words were not captured.]" for a turn
with no words, and that placeholder (with the user-line wording) primed replies about audio quality, which then filled the history and
were repeated. The rule now:
* a turn the model declined (`held`: [SILENT]/[WAIT]) or that a newer fragment took over (`superseded`) never appears;
* every assistant message follows the user's words (a Whisper transcript, the model's own USER line or the classifier's note), else
  the turn's audio while it is still in memory, for a model that takes audio;
* an exchange with neither is left out entirely, both sides: the model never sees an answer to a question it cannot see;
* an interrupted reply says so;
* consecutive user messages (a turn that got no reply) are joined, so roles always alternate (some chat templates require it).
"""
from __future__ import annotations

INTERRUPTED = " — (interrupted by the user)"
TEXT_LIMIT = 12000


def words(turn):
    return str(turn.get("transcript") or turn.get("user_line") or turn.get("note") or "")


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
        if not isinstance(turn, dict) or turn.get("held") or turn.get("superseded"):
            continue
        user = words(turn)
        clip = turn.get("user_audio") if audio_input else None
        if user:
            content = user[:TEXT_LIMIT]
        elif clip:
            content = audio_part(clip) if audio_part else "[audio]"
        else:
            continue
        uses = [entry for entry in (turn.get("tools") or [])[:convo_tools.HISTORY_LIMIT] if isinstance(entry, dict) and entry.get("name")]
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
    if request.get("context_audio"):
        current = "[%d overheard clip(s)] " % len(request["context_audio"]) + current
    add_user_view = shown[-1]["role"] == "user"
    if add_user_view:
        shown[-1] = {"role": "user", "text": shown[-1]["text"] + "\n" + current}
    else:
        shown.append({"role": "user", "text": current})
    return shown
