from __future__ import annotations

from typing import Any

HOW = {"steer": "steer", "later": "queue", "interrupt": "interrupt"}
SAID = {"steering": "Goes in at the next step.", "queued": "Queued until the run is done.", "interrupted": "Interrupted; the message is in.",
        "started": "Nothing was running, so the message was sent."}


def run(payload: dict[str, Any]) -> dict[str, Any]:
    from agent import AgentContext
    from usr.plugins.steering.helpers import steering

    invocation = payload.get("invocation") or {}
    name = str(invocation.get("command_name") or (payload.get("command") or {}).get("name") or "").strip().lower()
    text = str(invocation.get("raw_arguments") or "").strip()
    context = AgentContext.get(str((payload.get("context") or {}).get("context_id") or "").strip())
    if context is None:
        return _toast("Open a chat first.", "error")
    if not text:
        return _toast("Usage: /%s <message>" % (name or "steer"), "error")
    result = steering.send(context, text, how=HOW.get(name, "steer"), source="command")
    return _toast(SAID.get(result["outcome"], "Sent."))


def _toast(message: str, level: str = "success") -> dict[str, Any]:
    return {"text": "", "effects": [{"type": "toast", "message": message, "level": level}]}
