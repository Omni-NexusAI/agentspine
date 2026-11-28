"""Who writes a conversation's summary chapters, and whether it may (AGENTS.md, memory: "Who writes chapters, and privacy").

By default the conversation model writes them, from the conversation's own cached start (api/realtime._summarize): nothing leaves
for a model that does not already hear the conversation. `summary_writer: "utility"` asks Agent Zero's utility model instead. It
may write them only when it runs on the user's own machines (`convo_contract.local_host`: this machine, the LAN, Tailscale) or
with the user's explicit consent (`utility_summary_consent`, default off): a utility model in the cloud would receive the
conversation. Refused means refused: no other model writes them instead, and the turns stay word for word (the request still
fits: the oldest turns are left out of that one request, never refused).
"""
from __future__ import annotations

from urllib.parse import urlsplit

from .convo_contract import local_host

WRITERS = ("conversation", "utility")


def utility_local(model):
    """Whether Agent Zero's utility model runs on the user's own machines: where its requests really go (`base`, resolved by
    Host.utility_model / convo_host.resolve_base from its kwargs, its api_base, the provider's kwargs and the provider's documented
    default) under the one locality rule. A base that is not resolved counts as NOT local."""
    model = model if isinstance(model, dict) else {}
    base = str(model.get("base") if "base" in model else model.get("api_base") or "").strip()
    if not base:
        return False
    try:
        host = urlsplit(base if "://" in base else "http://" + base).hostname or ""
    except ValueError:
        return False
    return bool(host) and local_host(host)


def _label(model):
    name = str(model.get("name") or "").strip()
    provider = str(model.get("provider") or "").strip()
    return (provider + " / " + name) if provider and name else (name or provider or "not set")


def choose(config, utility=None):
    """{writer, allowed, model, local, reason, detail}: who writes this conversation's summaries. `utility`: Agent Zero's utility
    model as Host.utility_model returns it (only read when the utility model is asked for)."""
    writer = config.get("summary_writer") if config.get("summary_writer") in WRITERS else "conversation"
    if writer == "conversation":
        role = (config.get("roles") or {}).get("conversation") or {}
        model = str(role.get("model") or "").strip() or "not set"
        return {"writer": "conversation", "allowed": True, "model": model, "local": None, "reason": "",
                "detail": "The conversation model (%s) writes the summaries, reusing the conversation's own cached start; "
                          "nothing is sent to another model." % model}
    utility = utility if isinstance(utility, dict) else {}
    label = _label(utility)
    if not (utility.get("name") or utility.get("provider")):
        return {"writer": "utility", "allowed": False, "model": label, "local": False, "reason": "utility_unknown",
                "detail": "Agent Zero's utility model could not be read, so no summaries are written; the conversation is kept word "
                          "for word. Choose the conversation model, or check Agent Zero's utility model."}
    local = utility_local(utility)
    if local:
        return {"writer": "utility", "allowed": True, "model": label, "local": True, "reason": "",
                "detail": "Agent Zero's utility model (%s) writes the summaries; it runs on your own machines." % label}
    if config.get("utility_summary_consent") is True:
        return {"writer": "utility", "allowed": True, "model": label, "local": False, "reason": "",
                "detail": "Agent Zero's utility model (%s) writes the summaries; it is outside your network and receives the "
                          "conversation, as you allowed." % label}
    return {"writer": "utility", "allowed": False, "model": label, "local": False, "reason": "utility_not_consented",
            "detail": "Agent Zero's utility model (%s) is outside your network: it would receive the conversation. No summaries are "
                      "written until you allow it or choose the conversation model; the conversation is kept word for word." % label}


def readiness_rows(config, utility=None):
    """The Conversation card's "Summaries" row: who writes them (reads settings and Agent Zero's model choice; contacts nothing)."""
    if config.get("summary_writer") == "utility" and utility is None:
        try:
            from .convo_host import Host
            utility = Host().utility_model()
        except Exception:
            utility = {}
    found = choose(config, utility)
    return [{"id": "summary_writer", "label": "Summaries", "state": "ready" if found["allowed"] else "warning",
             "detail": found["detail"]}]
