"""Steering: a message sent to a working agent goes into the same run at its next step.

Agent Zero's own way (`AgentContext.communicate` sets `agent.intervention`) stops the step in progress: a reply being written is
thrown away and a tool that has just finished loses its result. Here a message that arrives during a run is kept, the step (what
the model is writing, and the tool calls it leads to) finishes with its results, and the message joins the history before the next
model call.

Agent Zero is not edited. The callers are hooks (`extensions/python`), which import this module inside a try and fall back to Agent
Zero's own behaviour when anything fails. Nothing of Agent Zero is imported at load time, so this module also loads without it.

A chat's state is `context.data['_steering']`: a key with a leading underscore is never saved with the chat, and a plain dict outlives
a reload of this module.
  lock      guards the rest; never held across a call into Agent Zero that may call back (communicate, history, log)
  items     the messages, oldest first (`_item`)
  runs      the agent loops at work in this chat (kept by the monologue hooks)
  watched   the future of the chat's own run whose end is waited for
  keep      a nudge is restarting the run: its waiting messages belong to the new one
  mode      this chat's own choice, '' = the plugin's setting
  revision  counts up on every change, for the page
Waiting and held messages are also written to `data/<chat id>.json` at once, because a message waits while a step is in progress,
which is exactly when a restart would lose it. They come back as held messages.

A message's status:
  waiting      goes in at the next step
  held         the run was stopped or failed, or Agent Zero restarted: kept by the composer until the user decides
  delivered    is in the run
  sent         started a new run, because the run ended before it could go in
  undelivered  the same, but its sender asked to handle that itself (`caller`)
  stopped      the run was stopped or failed, and its sender asked to handle that itself: nothing of it should begin again by itself
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path

logger = logging.getLogger("steering")

PLUGIN = "steering"
STATE_KEY = "_steering"         # context.data: the live state
VIEW_KEY = "_steering"          # context.output_data: what the page shows (underscore keys are not saved)
MARK = "steering_mode"          # attribute on a UserMessage: 'interrupt' = its sender wants Agent Zero's own interruption
PARENT_KEY = "parent_context_id"  # Agent Zero's own key on a sub-agent's chat (tools/call_subordinate.py)
MODES = ("automatic", "steer", "queue")
WAITING, HELD, DELIVERED, SENT, UNDELIVERED, STOPPED = "waiting", "held", "delivered", "sent", "undelivered", "stopped"
SETTLE_SECONDS = 0.6            # after a run ends: time for the queue, a nudge or another message to start the next one
LIMIT = 50                      # messages waiting or held in one chat
KEPT = 20                       # delivered messages remembered, so sub-agents still at work can be told
PREVIEW = 200                   # characters of a message shown in the strip
VIEW_CHARS, VIEW_TOTAL = 1500, 6000   # kept in view for the rest of the run: per message, in total
CONFIG_SECONDS = 5.0            # the plugin's setting is read from disk at most this often
ANCESTORS = 8

# What the working agent is told beside the message. It decides by what the message says, never by its wording.
AUTOMATIC_NOTE = (
    "The user sent this message while you were working. Nothing was stopped: your last step finished and its results stand. "
    "Decide from what the message says. If it changes, corrects or adds to the work in progress, take it into account from here on, "
    "without starting over. If it is a separate request, finish the work in progress first, then do it before you give your final "
    "answer. If it tells you to stop or to drop the work, stop and say what is done so far.")
STEER_NOTE = (
    "The user sent this message while you were working and wants it taken into account now. Nothing was stopped: your last step "
    "finished and its results stand. Go on from where you are with this message included; do not start over.")
COPY_NOTE = (
    "For information only: the user sent this to the main agent while you are working on your part. Change how you do your part only "
    "if this concerns it. Do not take on anything new from it; the main agent handles that.")
CHILD_NOTE = "The user sent this to your sub-agent%s, which finished before the message could be taken up."
KEPT_IN_VIEW = ("Messages the user sent while you were working on the current request, oldest first. They are part of the request:\n")
NOTES = {"automatic": AUTOMATIC_NOTE, "steer": STEER_NOTE}

_config = {"at": 0.0, "mode": ""}


# ── State ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
def state(context, create=True):
    """The chat's steering state, or None when there is none yet and `create` is false."""
    data = getattr(context, "data", None)
    if not isinstance(data, dict):
        return None
    found = data.get(STATE_KEY)
    if isinstance(found, dict) and "lock" in found:
        return found
    if not create:
        return None
    saved = _read(getattr(context, "id", ""))
    items = []
    for raw in saved.get("items", ()):
        item = _item(raw.get("text"), raw.get("attachments"), raw.get("system"), mode=raw.get("mode"), source=raw.get("source"),
                     item_id=raw.get("id"), logged=raw.get("logged"), caller=False, at=raw.get("at"))
        item["status"] = HELD          # whatever run it waited for is gone
        items.append(item)
    fresh = {"lock": threading.RLock(), "items": items, "runs": [], "watched": None, "keep": False,
             "mode": saved.get("mode") if saved.get("mode") in MODES else "", "revision": 0}
    return data.setdefault(STATE_KEY, fresh)


def _item(text, attachments=(), system=(), *, mode="automatic", source="", item_id="", logged=False, caller=False, at=None):
    return {"id": str(item_id or uuid.uuid4()), "text": str(text or ""), "attachments": [str(a) for a in attachments or ()],
            "system": [str(s) for s in system or ()], "mode": mode if mode in NOTES else "automatic", "source": str(source or ""),
            "at": float(at or time.time()), "logged": bool(logged), "caller": bool(caller), "status": WAITING, "copies": set()}


def _open(st):
    """The messages still on their way: waiting or held."""
    return [item for item in st["items"] if item["status"] in (WAITING, HELD)]


def _data_file(context_id):
    name = str(context_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        name = "h" + hashlib.sha256(name.encode("utf-8", "replace")).hexdigest()[:32]
    return Path(__file__).resolve().parents[1] / "data" / (name + ".json")


def _read(context_id):
    try:
        loaded = json.loads(_data_file(context_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = loaded.get("items") if isinstance(loaded, dict) else None
    return {"items": [i for i in items if isinstance(i, dict)] if isinstance(items, list) else [],
            "mode": loaded.get("mode") if isinstance(loaded, dict) else ""}


def _save(context, st):
    """Write what must survive a restart. A failure is logged and never reaches a message."""
    path = _data_file(getattr(context, "id", ""))
    items = [{key: item[key] for key in ("id", "text", "attachments", "system", "mode", "source", "at", "logged")} for item in _open(st)]
    fresh = path.with_suffix(".%s.tmp" % uuid.uuid4().hex[:8])
    try:
        if not items and not st["mode"]:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh.write_text(json.dumps({"items": items, "mode": st["mode"]}, ensure_ascii=False), encoding="utf-8")
        os.replace(fresh, path)
    except OSError:
        logger.warning("Steering could not save the waiting messages of a chat", exc_info=True)
        try:
            fresh.unlink(missing_ok=True)
        except OSError:
            pass


def view(context):
    """What the page shows for this chat."""
    st = state(context)
    if st is None:
        return {"mode": default_mode(), "own_mode": "", "revision": 0, "items": []}
    with st["lock"]:
        items = [{"id": item["id"], "text": item["text"][:PREVIEW] + ("…" if len(item["text"]) > PREVIEW else ""),
                  "attachments": [os.path.basename(a) for a in item["attachments"]], "mode": item["mode"], "status": item["status"],
                  "source": item["source"]} for item in _open(st) if item["text"] or item["attachments"]]
        return {"mode": st["mode"] or default_mode(), "own_mode": st["mode"], "revision": st["revision"], "items": items}


def publish(context, st=None):
    """After a change: save it, show it on the page."""
    st = st or state(context, create=False)
    if st is None:
        return
    with st["lock"]:
        st["revision"] += 1
        _prune(st)
        _save(context, st)
    try:
        context.set_output_data(VIEW_KEY, view(context))
        from helpers.state_monitor_integration import mark_dirty_for_context
        mark_dirty_for_context(context.id, reason="steering")
    except Exception:
        logger.debug("Steering could not update the page", exc_info=True)


def _prune(st):
    done = [item for item in st["items"] if item["status"] not in (WAITING, HELD, UNDELIVERED, STOPPED)]
    for item in done[:-KEPT] if len(done) > KEPT else ():
        st["items"].remove(item)


def clear(context, forget_mode=False):
    """The chat was reset or removed: nothing of it waits any more."""
    st = state(context)
    if st is None:
        return
    with st["lock"]:
        st["items"].clear()
        if forget_mode:
            st["mode"] = ""
    publish(context, st)


# ── The setting ────────────────────────────────────────────────────────────────────────────────────────────────────────
def default_mode():
    """The plugin's setting for messages sent while the agent works."""
    now = time.monotonic()
    if not _config["mode"] or now - _config["at"] > CONFIG_SECONDS:
        chosen = ""
        try:
            from helpers.plugins import get_plugin_config
            chosen = str((get_plugin_config(PLUGIN) or {}).get("mode") or "")
        except Exception:
            logger.debug("Steering could not read its settings", exc_info=True)
        _config.update(at=now, mode=chosen if chosen in MODES else "automatic")
    return _config["mode"]


def set_mode(context, mode):
    """This chat's own choice; '' follows the setting."""
    if mode not in ("", *MODES):
        raise ValueError("Unknown mode")
    st = state(context)
    with st["lock"]:
        st["mode"] = mode
    publish(context, st)
    return view(context)


def mode_for(context):
    st = state(context, create=False)
    return (st["mode"] if st else "") or default_mode()


# ── Which runs are at work ─────────────────────────────────────────────────────────────────────────────────────────────
def _own_alive(context):
    task = getattr(context, "task", None)
    try:
        return bool(task is not None and task.is_alive())
    except Exception:
        return False


def _begun_by(task):
    """Whose run an asyncio task is: Agent Zero begins a chat's own run as DeferredTask._run(context._process_chain, ...), and this is that
    context. None when it cannot be told."""
    try:
        return getattr(task.get_coro().cr_frame.f_locals.get("func"), "__self__", None)
    except Exception:
        return None


def live(context):
    """'own' when the chat's own run is at work, 'inside' when its agent works inside another run (a sub-agent that its superior
    waits for, a scheduled task), '' when nothing is."""
    if _own_alive(context):
        return "own"
    st = state(context, create=False)
    if st is None:
        return ""
    with st["lock"]:
        runs = list(st["runs"])
    for run in runs:
        try:
            if not run["own"] and not run["task"].done():
                return "inside"
        except Exception:
            continue
    return ""


def busy(context):
    """Whether an agent is at work in this chat, sub-agents and scheduled tasks included (Agent Zero's `is_running` misses those)."""
    return bool(live(context))


def run_began(agent, task):
    """An agent loop starts (monologue hook). `task`: the asyncio task running it."""
    context = getattr(agent, "context", None)
    st = state(context)
    if st is None or task is None:
        return
    # A run that was stopped in its first moment still gets here once, though the chat's task is over already: it is the chat's own all the same
    # (taken for an inner one, it would keep the message that follows).
    entry = {"agent": agent, "task": task, "own": _own_alive(context) or _begun_by(task) is context, "began": time.time()}
    with st["lock"]:
        over = [run for run in st["runs"] if _finished(run["task"])]
        if any(not run["own"] for run in over):
            _hold(st)                  # that loop was stopped and nothing said so: what waited for it is not this run's
        st["runs"] = [run for run in st["runs"] if not any(run is gone for gone in over)] + [entry]
        waiting = any(item["status"] == WAITING for item in st["items"])
    if not entry["own"]:
        # Agent Zero's hooks do not see a loop that is cancelled (Stop on the chat it works for), so its task is watched as well.
        try:
            task.add_done_callback(lambda _: _guarded(_inner_over, context.id, entry))
        except Exception:
            logger.debug("Steering could not watch an inner run", exc_info=True)
    if waiting:
        _watch(context, st)


def _hold(st):
    """The run was stopped or failed: what waits is kept by the composer until the user decides (its sender is told, when it asked to handle
    that itself). Under the lock."""
    for item in st["items"]:
        if item["status"] == WAITING:
            item["status"] = STOPPED if item["caller"] else HELD


def _inner_over(context_id, entry):
    """The task an inner loop ran in is done. Its own end was seen (`run_over`) unless it was stopped; called in Agent Zero's own loop, so
    nothing of Agent Zero is called here."""
    from agent import AgentContext
    context = AgentContext.get(context_id)
    st = state(context, create=False) if context is not None else None
    if st is None:
        return
    with st["lock"]:
        if not any(run is entry for run in st["runs"]):
            return
        st["runs"] = [run for run in st["runs"] if run is not entry]
    if live(context):
        return                         # another loop of this chat goes on and takes it up
    with st["lock"]:
        _hold(st)
    _later(_settle, context_id, "stopped")


def _finished(task):
    try:
        return task.done()
    except Exception:
        return True


def run_over(agent, failed=False):
    """An agent loop ended (monologue hook). What a sub-agent or a scheduled run could not take up any more is passed on; the
    chat's own run is settled when its future is done (`_watch`)."""
    context = getattr(agent, "context", None)
    st = state(context, create=False)
    if st is None:
        return
    ended = None
    with st["lock"]:
        for run in reversed(st["runs"]):
            if run["agent"] is agent:
                ended = run
                st["runs"].remove(run)
                break
        waiting = [item for item in st["items"] if item["status"] == WAITING]
    if ended is None or ended["own"] or not waiting or live(context):
        return
    parent = next(iter(_ancestors(context)), None)
    if parent is not None and live(parent):
        _pass_up(context, st, parent, failed)
        return
    _later(_settle, context.id, "failed" if failed else "finished")


def _pass_up(context, st, parent, failed=False):
    """What a sub-agent could not take up any more goes to the agent that waits for it (a sender that handles the end itself is told how it
    ended)."""
    name = str(getattr(context, "name", "") or "").strip()
    with st["lock"]:
        waiting = [item for item in st["items"] if item["status"] == WAITING and not item["caller"]]
        for item in st["items"]:
            if item["status"] == WAITING and item["caller"]:
                item["status"] = STOPPED if failed else UNDELIVERED
        for item in waiting:
            st["items"].remove(item)
    for item in waiting:
        item["system"] = [CHILD_NOTE % ((' "%s"' % name) if name else "")] + item["system"]
        item["logged"] = True            # it has its bubble in the sub-agent's chat
        _store(parent, item)
    publish(context, st)


def _ancestors(context):
    """The chats whose agents wait for this one, nearest first."""
    found = []
    try:
        from agent import AgentContext
        current = context
        while len(found) < ANCESTORS:
            parent_id = current.get_output_data(PARENT_KEY)
            parent = AgentContext.get(str(parent_id)) if parent_id else None
            if parent is None or parent is context or parent in found:
                break
            found.append(parent)
            current = parent
    except Exception:
        logger.debug("Steering could not read a chat's parents", exc_info=True)
    return found


# ── A message arrives ──────────────────────────────────────────────────────────────────────────────────────────────────
def on_communicate(data):
    """Start hook of `AgentContext.communicate(msg)`. A message that arrives while an agent works here is kept for its next step and
    the original is not run (data['result'] is set); everything else is left to Agent Zero."""
    args, kwargs = data.get("args") or (), data.get("kwargs") or {}
    context = args[0] if args else kwargs.get("self")
    message = args[1] if len(args) > 1 else kwargs.get("msg")
    if context is None or message is None or getattr(message, MARK, "") == "interrupt":
        return
    where = live(context)
    if not where:
        return
    if where == "own" and getattr(context, "paused", False) and not _tool_running(context):
        return                     # the user stopped it to say this: Agent Zero's own way puts it in right there
    logged, log_id = _bubble(context, message)
    mode = mode_for(context)
    item = _item(getattr(message, "message", ""), getattr(message, "attachments", ()), getattr(message, "system_message", ()),
                 mode=mode if mode in NOTES else "automatic", item_id=getattr(message, "id", "") or log_id, logged=logged,
                 source=getattr(message, "steering_source", ""))
    if not _store(context, item):
        return
    context.paused = False
    data["result"] = context.task if where == "own" else finished_task()


def steer(context, text, *, attachments=(), mode="", source="", item_id="", caller=False, logged=False):
    """Keep a message for the next step of the agent at work in this chat. Returns its id, or '' when no agent is at work (nothing is
    kept then). `caller`: when the run ends before it goes in, its sender handles it (`status` says 'undelivered'); otherwise it
    starts the next run."""
    if not live(context):
        return ""
    item = _item(text, attachments, mode=mode or mode_for(context), source=source, item_id=item_id, caller=caller, logged=logged)
    if not _store(context, item):
        return ""
    if live(context) == "own":
        context.paused = False
    return item["id"]


def status(context, item_id):
    """Where a message is: waiting, held, delivered, sent, undelivered, stopped or '' (unknown)."""
    st = state(context, create=False)
    if st is None:
        return ""
    with st["lock"]:
        return next((item["status"] for item in st["items"] if item["id"] == item_id), "")


def forget(context, item_id):
    """Drop a message (its sender has dealt with it, or the user removed it). Returns it, or None."""
    st = state(context, create=False)
    if st is None:
        return None
    with st["lock"]:
        item = next((i for i in st["items"] if i["id"] == item_id and i["status"] in (WAITING, HELD, UNDELIVERED, STOPPED)), None)
        if item is not None:
            st["items"].remove(item)
    if item is not None:
        publish(context, st)
    return item


def _store(context, item):
    st = state(context)
    if st is None:
        return False
    with st["lock"]:
        if len(_open(st)) >= LIMIT:
            logger.warning("Steering: too many messages are waiting in one chat; Agent Zero's own way handles this one")
            return False
        st["items"].append(item)
    publish(context, st)
    _watch(context, st)            # after it is stored: a run that ended meanwhile is settled with this message included
    return True


def _tool_running(context):
    agent = getattr(context, "streaming_agent", None) or getattr(context, "agent0", None)
    return getattr(getattr(agent, "loop_data", None), "current_tool", None) is not None


def _bubble(context, message):
    """(whether the chat already shows this message, the id of that line). Most senders log the user's line before they send; some log
    it under an id they do not pass on, some not at all."""
    text = str(getattr(message, "message", "") or "").strip()
    if not text:
        return True, ""            # only an instruction to the agent: it never gets a line
    wanted = str(getattr(message, "id", "") or "")
    try:
        recent = [item for item in list(context.log.logs)[-40:] if getattr(item, "type", "") == "user"]
    except Exception:
        return bool(wanted), wanted
    for item in reversed(recent):
        line = str(getattr(item, "id", "") or "")
        if wanted and line == wanted:
            return True, line
    newest = recent[-1] if recent else None
    if newest is not None and str(getattr(newest, "content", "") or "").strip() == text and _recent(newest):
        return True, str(getattr(newest, "id", "") or "")
    return False, ""


def _recent(item):
    try:
        return abs(time.time() - float(getattr(item, "timestamp", 0) or 0)) < 15
    except (TypeError, ValueError):
        return True


def finished_task():
    """A task that is already done, for callers that keep or await what `communicate` returns. Never the superior's own task: a
    caller that stops it would stop the superior's run."""
    from helpers.defer import DeferredTask
    task = DeferredTask()
    done: Future = Future()
    done.set_result("")
    task._future = done
    return task


# ── The next step ──────────────────────────────────────────────────────────────────────────────────────────────────────
def pending(agent):
    """Whether a message waits for this agent's next step (a wait then ends early)."""
    context = getattr(agent, "context", None)
    st = state(context, create=False)
    if st is None or agent is not getattr(context, "agent0", None):
        return False
    with st["lock"]:
        return any(item["status"] == WAITING for item in st["items"])


def deliver(agent, loop_data):
    """Put the waiting messages into this agent's run, before its next model call. Called inside the loop, after history compression
    was waited for and before the history is read."""
    context = getattr(agent, "context", None)
    st = state(context, create=False)
    if st is not None and agent is getattr(context, "agent0", None):
        with st["lock"]:
            items = [item for item in st["items"] if item["status"] == WAITING]
            for item in items:
                item["status"] = DELIVERED
        for item in items:
            try:
                _put(agent, item, NOTES[item["mode"]])
                _keep_in_view(loop_data, item)
            except Exception:
                logger.exception("Steering could not put a message into the run; it is kept by the composer")
                with st["lock"]:
                    item["status"] = HELD
        if items:
            publish(context, st)
    for item in _copies(agent):
        try:
            _put(agent, item, COPY_NOTE, copy=True)
            _keep_in_view(loop_data, item)
        except Exception:
            logger.exception("Steering could not tell a sub-agent about a message")


def _copies(agent):
    """Messages the user sent to an agent that waits for this one, after this one began. Each sub-agent is told once."""
    context = agent.context
    began = _began(agent)
    if began is None:
        return []
    sources = list(_ancestors(context))
    if agent is not getattr(context, "agent0", None):
        sources.insert(0, context)           # a sub-agent living in its superior's chat
    me = "%s:%s" % (getattr(context, "id", ""), getattr(agent, "number", ""))
    found = []
    for source in sources:
        st = state(source, create=False)
        if st is None:
            continue
        with st["lock"]:
            for item in st["items"]:
                if item["status"] in (WAITING, DELIVERED) and item["text"] and item["at"] >= began and me not in item["copies"]:
                    item["copies"].add(me)
                    found.append(item)
    return found


def _began(agent):
    st = state(agent.context, create=False)
    if st is None:
        return None
    with st["lock"]:
        return next((run["began"] for run in reversed(st["runs"]) if run["agent"] is agent), None)


def _put(agent, item, note, copy=False):
    from agent import UserMessage
    context = agent.context
    if copy:
        context.log.log(type="info", heading="Passed on from the main chat", content=item["text"], update_progress="none")
        message = UserMessage(message=item["text"], system_message=[note])
    else:
        if item["text"] and not item["logged"]:
            from helpers import message_queue
            message_queue.log_user_message(context, item["text"], item["attachments"], message_id=item["id"], source=" (steering)")
            item["logged"] = True
        message = UserMessage(message=item["text"], attachments=list(item["attachments"]), system_message=[*item["system"], note],
                              id=item["id"])
    # The message belongs to the topic at work. A topic of its own would move the run's whole working history to the older topics,
    # which are compressed first. `hist_add_user_message` starts one through this method, so it is shadowed for this one call.
    history = agent.history
    history.new_topic = _same_topic
    try:
        agent.hist_add_user_message(message, intervention=True)
    finally:
        vars(history).pop("new_topic", None)


def _same_topic():
    return None


def _keep_in_view(loop_data, item):
    """History compression keeps only the first message of a topic, so the messages of this run are shown with every request."""
    if loop_data is None or not item["text"]:
        return
    kept = loop_data.params_persistent.setdefault("_steering", [])
    kept.append(item["text"][:VIEW_CHARS])
    while len(kept) > 1 and sum(len(text) for text in kept) > VIEW_TOTAL:
        kept.pop(0)
    loop_data.extras_persistent[PLUGIN] = KEPT_IN_VIEW + "\n".join("%d. %s" % (n, text) for n, text in enumerate(kept, 1))


# ── The run ends ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _watch(context, st):
    """Be told when the chat's own run ends. A run that is already over is told at once."""
    future = getattr(getattr(context, "task", None), "_future", None)
    if future is None:
        return
    with st["lock"]:
        if st["watched"] is future:
            return
        st["watched"] = future
    context_id = context.id
    future.add_done_callback(lambda done: _run_done(context_id, done))


def _run_done(context_id, future):
    """The chat's own run is over. Called wherever the future was completed (inside Stop, too): no call into Agent Zero here."""
    try:
        outcome = "stopped" if future.cancelled() else "failed" if future.exception() is not None else "finished"
    except BaseException:
        outcome = "failed"
    if outcome != "finished":
        try:
            from agent import AgentContext
            context = AgentContext.get(context_id)
            st = state(context, create=False) if context is not None else None
            if st is not None:
                with st["lock"]:
                    if not st["keep"]:
                        _hold(st)
        except Exception:
            logger.exception("Steering could not hold the waiting messages of a stopped run")
    _later(_settle, context_id, outcome)


def _later(function, *args):
    timer = threading.Timer(SETTLE_SECONDS, _guarded, (function, *args))
    timer.daemon = True
    timer.name = "steering-settle"
    timer.start()


def _guarded(function, *args):
    try:
        function(*args)
    except Exception:
        logger.exception("Steering could not settle the messages left after a run")


def _settle(context_id, outcome):
    """A moment after a run ended: what it did not take up starts the next run (after a normal end), or is kept by the composer. When
    a new run has begun meanwhile, its first step takes it up."""
    from agent import AgentContext
    context = AgentContext.get(context_id)
    st = state(context, create=False) if context is not None else None
    if st is None:
        return
    if live(context):
        with st["lock"]:
            left = any(item["status"] == WAITING for item in st["items"])
        if left:
            _watch(context, st)
        publish(context, st)
        return
    parent = next(iter(_ancestors(context)), None)
    if parent is not None and outcome == "finished":
        # A sub-agent's chat never starts a run by itself: Agent Zero would run its superior along with it, beside the superior's own run.
        if live(parent):
            _pass_up(context, st, parent)
            return
        with st["lock"]:
            for item in st["items"]:
                if item["status"] == WAITING and item["caller"]:
                    item["status"] = UNDELIVERED           # its run ended by itself
        outcome = "failed"
    first = None
    with st["lock"]:
        for item in st["items"]:
            if item["status"] != WAITING:
                continue
            if item["caller"]:
                item["status"] = UNDELIVERED if outcome == "finished" else STOPPED
            elif outcome != "finished":
                item["status"] = HELD
            elif first is None:
                item["status"], first = SENT, item       # any others are taken up by the first step of the run it starts
    publish(context, st)
    if first is not None:
        try:
            start(context, first)
        except Exception:
            logger.exception("Steering could not start the next run with a message; it is kept by the composer")
            with st["lock"]:
                for item in st["items"]:
                    if item is first or item["status"] == WAITING:
                        item["status"] = HELD
            publish(context, st)
            return
        _watch(context, st)        # the rest wait for that run's first step


def start(context, item):
    """Start a run with a message (or, when one has begun meanwhile, let the hook keep it for its next step)."""
    from agent import AgentContext, UserMessage
    from helpers import message_queue
    AgentContext.use(context.id)
    if (item["text"] or item["attachments"]) and not item["logged"]:
        message_queue.log_user_message(context, item["text"], item["attachments"], message_id=item["id"])
        item["logged"] = True
    context.communicate(UserMessage(message=item["text"], attachments=list(item["attachments"]), system_message=list(item["system"]),
                                    id=item["id"]))


_arriving, _arriving_lock = set(), threading.Lock()      # (chat, message id) of what is being sent right now


# ── What the composer, the strip and the commands do ───────────────────────────────────────────────────────────────────
def interrupt(context, text, attachments=(), system=(), item_id="", logged=False):
    """Agent Zero's own interruption: the step in progress is dropped."""
    from agent import AgentContext, UserMessage
    from helpers import message_queue
    AgentContext.use(context.id)
    item_id = str(item_id or uuid.uuid4())
    if (text or attachments) and not logged:
        message_queue.log_user_message(context, text, list(attachments), message_id=item_id)
    message = UserMessage(message=text, attachments=list(attachments), system_message=list(system), id=item_id)
    setattr(message, MARK, "interrupt")
    if live(context) == "inside":
        # Its own task does not run it, so `communicate` would start a second run beside it.
        agent = getattr(context, "streaming_agent", None) or context.agent0
        context.paused = False
        agent.intervention = message
        return
    context.communicate(message)


def send(context, text, *, attachments=(), how="", item_id="", source="composer"):
    """A message from the composer or a command. `how`: '' (the chat's mode), automatic, steer, queue or interrupt. Returns
    {'outcome': started | steering | queued | interrupted, 'id'}."""
    text, attachments = str(text or ""), [full_path(a) for a in attachments or ()]
    if not text.strip() and not attachments:
        raise ValueError("Nothing to send")
    how = how if how in (*MODES, "interrupt") else mode_for(context)
    item_id = str(item_id or uuid.uuid4())
    with _arriving_lock:
        # Sent already (only the answer was lost on its way), or on its way through here right now.
        if (context.id, item_id) in _arriving or known(context, item_id):
            return {"outcome": "known", "id": item_id}
        _arriving.add((context.id, item_id))
    try:
        return _send(context, text, attachments, how, item_id, source)
    finally:
        with _arriving_lock:
            _arriving.discard((context.id, item_id))


def _send(context, text, attachments, how, item_id, source):
    from helpers import message_queue
    where = live(context)
    if not where:
        if message_queue.has_queue(context):
            # As Agent Zero's own composer does: what is queued goes first.
            message_queue.add(context, text, attachments, item_id)
            _dirty(context, "message_queue_add")
            return {"outcome": "queued", "id": item_id}
        start(context, _item(text, attachments, item_id=item_id))
        return {"outcome": "started", "id": item_id}
    if how == "interrupt":
        interrupt(context, text, attachments, item_id=item_id)
        return {"outcome": "interrupted", "id": item_id}
    if how == "queue" and where == "own":
        message_queue.add(context, text, attachments, item_id)
        _dirty(context, "message_queue_add")
        return {"outcome": "queued", "id": item_id}
    kept = steer(context, text, attachments=attachments, mode=how if how in NOTES else "automatic", source=source, item_id=item_id)
    if kept:
        return {"outcome": "steering", "id": kept}
    start(context, _item(text, attachments, item_id=item_id))      # the run ended in between
    return {"outcome": "started", "id": item_id}


def known(context, item_id):
    """Whether a message with this id got here: it waits, is held, queued or in a run, or stands in the chat."""
    from helpers import message_queue
    item_id = str(item_id)
    if status(context, item_id):
        return True
    try:
        if any(str((entry or {}).get("id") or "") == item_id for entry in message_queue.get_queue(context) or ()):
            return True
    except Exception:
        logger.debug("Steering could not look into the queue", exc_info=True)
    try:
        return any(str(getattr(line, "id", "") or "") == item_id for line in list(context.log.logs)[-200:] if getattr(line, "type", "") == "user")
    except Exception:
        return False


def interrupt_item(context, item_id):
    item = forget(context, item_id)
    if item is None:
        return False
    if live(context):
        interrupt(context, item["text"], item["attachments"], item["system"], item["id"], item["logged"])
    else:
        start(context, item)
    return True


def queue_item(context, item_id):
    """Into Agent Zero's own queue: after the run."""
    from helpers import message_queue
    item = forget(context, item_id)
    if item is None:
        return False
    message_queue.add(context, item["text"], item["attachments"], item["id"])
    _dirty(context, "message_queue_add")
    if not live(context):
        message_queue.send_next(context)
    return True


def send_item(context, item_id):
    """Send a held message now: into the run at work, or as the next run."""
    st = state(context, create=False)
    item = forget(context, item_id)
    if item is None or st is None:
        return False
    if live(context):
        item["status"] = WAITING
        return _store(context, item)
    start(context, item)
    return True


def take_item(context, item_id):
    """Back to the composer: {'text', 'attachments'} and no longer waiting, or None."""
    item = forget(context, item_id)
    return None if item is None else {"text": item["text"], "attachments": [os.path.basename(a) for a in item["attachments"]]}


def full_path(attachment):
    attachment = str(attachment)
    if attachment.startswith("/"):
        return attachment
    try:
        from helpers.message_queue import UPLOAD_FOLDER
    except Exception:
        UPLOAD_FOLDER = "/a0/usr/uploads"
    return "%s/%s" % (UPLOAD_FOLDER, os.path.basename(attachment))


def _dirty(context, reason):
    try:
        from helpers.state_monitor_integration import mark_dirty_for_context
        mark_dirty_for_context(context.id, reason=reason)
    except Exception:
        logger.debug("Steering could not update the page", exc_info=True)
