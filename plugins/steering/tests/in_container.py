"""Steering against the real Agent Zero, with a scripted stand-in model. For an isolated test container only.

    /opt/venv-a0/bin/python /a0/usr/plugins/steering/tests/in_container.py [scenario ...]

Needs the container's default model preset to point at http://127.0.0.1:9990/v1 (any OpenAI-compatible provider). Nothing is sent
anywhere else. Prints one line per scenario (marked @@, among Agent Zero's own output) and exits with the number of failures."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("STEERING_STUB_PORT", "9990"))
os.chdir("/a0")
sys.path.insert(0, "/a0")


def tool(name, **args):
    return json.dumps({"thoughts": ["working"], "headline": "step", "tool_name": name, "tool_args": args})


def final(text):
    return tool("response", text=text)


def shell(command, **more):
    return tool("code_execution_tool", runtime="terminal", session=0, code=command, **more)


class Model:
    """An OpenAI-compatible model whose answers come from the running scenario."""

    def __init__(self):
        self.lock = threading.Condition()
        self.calls, self.script = [], None
        model = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, body, status=200):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._json({"object": "list", "data": [{"id": "stub-main", "object": "model"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path.endswith("/embeddings"):
                    rows = body.get("input") if isinstance(body.get("input"), list) else [body.get("input")]
                    return self._json({"object": "list", "model": "stub", "data": [{"index": i, "embedding": [0.0] * 8} for i in range(len(rows))],
                                       "usage": {"prompt_tokens": 1, "total_tokens": 1}})
                reply = model.answer(body)
                if reply.get("status", 200) != 200:
                    return self._json({"error": {"message": "stand-in refusal", "type": "server_error"}}, reply["status"])
                text, seconds = reply["text"], reply.get("seconds", 0)
                base = {"id": "c" + uuid.uuid4().hex[:8], "object": "chat.completion.chunk", "created": int(time.time()), "model": body.get("model")}
                if not body.get("stream"):
                    time.sleep(seconds)
                    return self._json({**base, "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                size = max(1, len(text) // 24)
                parts = [text[i:i + size] for i in range(0, len(text), size)]
                try:
                    for part in parts:
                        chunk = {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": part}, "finish_reason": None}]}
                        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                        self.wfile.flush()
                        time.sleep(seconds / len(parts))
                    done = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                    self.wfile.write(b"data: " + json.dumps(done).encode() + b"\n\ndata: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    with model.lock:
                        model.cut += 1
                self.close_connection = True

        self.cut = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def begin(self, script):
        with self.lock:
            self.calls, self.script, self.cut = [], script, 0

    def answer(self, body):
        if str(body.get("model") or "").endswith("stub-utility"):
            return {"text": "ok"}
        with self.lock:
            self.calls.append(body.get("messages") or [])
            number = len(self.calls)
            script = self.script
            self.lock.notify_all()
        reply = script(number, self.calls[number - 1]) if script else final("done")
        return reply if isinstance(reply, dict) else {"text": reply}

    def wait_call(self, number, seconds=40):
        with self.lock:
            ok = self.lock.wait_for(lambda: len(self.calls) >= number, seconds)
        assert ok, "the model was never called %d times (only %d)" % (number, len(self.calls))


def said(messages):
    """The request as plain text per message, newest last, without the system prompt."""
    out = []
    for message in messages:
        if message.get("role") == "system":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        out.append((message.get("role"), str(content)))
    return out


def joined(messages):
    return "\n".join(text for _, text in said(messages))


MODEL = Model()

import initialize                                                                   # noqa: E402
from helpers import dotenv, runtime                                                  # noqa: E402

WANTED, sys.argv = sys.argv[1:], [sys.argv[0], "--dockerized=true", "--host=127.0.0.1", "--port=80"]
runtime.initialize()
dotenv.load_dotenv()
from agent import AgentContext, UserMessage                                          # noqa: E402
from helpers import cache, message_queue as mq                                       # noqa: E402

TOGGLE = "/a0/usr/plugins/steering/.toggle-0"


def plugin(on):
    if on and os.path.exists(TOGGLE):
        os.remove(TOGGLE)
    if not on:
        open(TOGGLE, "w").close()
    for area in ("*(plugins)*", "*(extensions)*", "*(api)*"):
        cache.clear(area)


def steering():
    from usr.plugins.steering.helpers import steering as module
    return module


def chat(name):
    return AgentContext(config=initialize.initialize_agent(), id="t" + uuid.uuid4().hex[:10], name=name)


def begin(name, script, text="Please start."):
    MODEL.begin(script)
    context = chat(name)
    return context, context.communicate(UserMessage(text, id=str(uuid.uuid4())))


def until(check, seconds=40, what="it"):
    end = time.time() + seconds
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("waited %s s for %s" % (seconds, what))


def in_tool(context, name="code_execution_tool"):
    return until(lambda: getattr(getattr(getattr(context.get_agent(), "loop_data", None), "current_tool", None), "name", "") == name, what="tool " + name)


def order(text, *parts):
    at = -1
    for part in parts:
        found = text.find(part, at + 1)
        assert found > at, "%r is missing or out of order in the request" % part
        at = found


def idle(context, seconds=40):
    until(lambda: not context.is_running(), seconds, "the run to end")


# ── Scenarios ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def during_a_tool():
    """The tool finishes, its result is kept, the message follows it in the same run."""
    context, task = begin("tool", lambda n, m: shell("echo BEGIN-$((20+1)); sleep 4; echo RESULT-$((40+2))") if n == 1 else final("all done"))
    in_tool(context)
    time.sleep(1.5)
    context.communicate(UserMessage("Please also add a readme.", id=str(uuid.uuid4())))
    result = task.result_sync(60)
    assert len(MODEL.calls) == 2, "model calls: %d" % len(MODEL.calls)
    text = joined(MODEL.calls[1])
    order(text, "RESULT-42", "Please also add a readme.")
    assert "Nothing was stopped" in text
    assert result == "all done", result
    assert "Messages the user sent while you were working" in text, "kept in view"
    assert MODEL.cut == 0


def native_during_a_tool():
    """For comparison, with the plugin off: the result of the tool is lost."""
    plugin(False)
    try:
        context, task = begin("native", lambda n, m: shell("echo BEGIN-$((20+1)); sleep 4; echo RESULT-$((40+2))") if n == 1 else final("all done"))
        in_tool(context)
        time.sleep(1.5)
        context.communicate(UserMessage("Please also add a readme.", id=str(uuid.uuid4())))
        task.result_sync(60)
        assert "RESULT-42" not in joined(MODEL.calls[1]), "Agent Zero kept the result by itself"
    finally:
        plugin(True)


def while_writing():
    """What the model is writing is finished and its tool runs; nothing is cut."""
    context, task = begin("writing", lambda n, m: {"text": shell("echo STEP-$((1+1))"), "seconds": 4} if n == 1 else final("ok"))
    MODEL.wait_call(1)
    time.sleep(1)
    context.communicate(UserMessage("Use blue.", id=str(uuid.uuid4())))
    task.result_sync(60)
    assert MODEL.cut == 0, "the reply was cut"
    assert len(MODEL.calls) == 2
    order(joined(MODEL.calls[1]), "STEP-2", "Use blue.")


def several_messages():
    context, task = begin("several", lambda n, m: shell("sleep 3; echo X-$((3+3))") if n == 1 else final("ok"))
    in_tool(context)
    for text in ("first addition", "second addition", "third addition"):
        context.communicate(UserMessage(text, id=str(uuid.uuid4())))
    task.result_sync(60)
    order(joined(MODEL.calls[1]), "X-6", "first addition", "second addition", "third addition")
    assert len(MODEL.calls) == 2


def during_the_final_answer():
    """The answer is delivered whole; the message starts the next run."""
    def script(n, m):
        return {"text": final("the first answer"), "seconds": 3} if n == 1 else final("the second answer")
    context, task = begin("final", script)
    first = task._future
    MODEL.wait_call(1)
    time.sleep(1)
    context.communicate(UserMessage("And the weather?", id=str(uuid.uuid4())))
    assert first.result(60) == "the first answer"
    MODEL.wait_call(2)
    idle(context)
    assert context.task._future.result(5) == "the second answer"
    order(joined(MODEL.calls[1]), "the first answer", "And the weather?")
    answers = [item.content for item in context.log.logs if item.type == "response"][-2:]      # the chat's greeting comes first
    assert answers == ["the first answer", "the second answer"], answers


def ends_a_wait():
    context, task = begin("wait", lambda n, m: tool("wait", seconds=120) if n == 1 else final("ok"))
    in_tool(context, "wait")
    time.sleep(1)
    began = time.time()
    context.communicate(UserMessage("No need to wait.", id=str(uuid.uuid4())))
    task.result_sync(30)
    assert time.time() - began < 15, "the wait was not ended"
    order(joined(MODEL.calls[1]), "The wait ended after", "No need to wait.")


def a_wait_without_messages():
    context, task = begin("plain wait", lambda n, m: tool("wait", seconds=2) if n == 1 else final("ok"))
    task.result_sync(30)
    assert "The wait ended after" not in joined(MODEL.calls[1])


def child_calls():
    return [m for m in MODEL.calls if "CHILD-TASK" in joined(m) and "PARENT-TASK" not in joined(m)]


def with_a_sub_agent():
    """The sub-agent is told at its next step, finishes, its result is kept, and the superior gets the message itself."""
    def script(n, m):
        text = joined(m)
        if "PARENT-TASK" in text:
            return final("parent done") if "CHILD-RESULT" in text else tool("call_subordinate", profile="", message="CHILD-TASK do it", reset="true")
        return final("CHILD-RESULT") if "STEP-9" in text else shell("sleep 4; echo STEP-$((4+5))")
    context, task = begin("sub-agent", script, "PARENT-TASK go")
    child = until(lambda: next((c for c in AgentContext.all() if c.get_output_data("parent_context_id") == context.id), None), what="the sub-agent")
    in_tool(child)
    context.communicate(UserMessage("Make it shorter.", id=str(uuid.uuid4())))
    assert task.result_sync(90) == "parent done"
    last_child = joined(child_calls()[-1])
    order(last_child, "STEP-9", "Make it shorter.")
    assert "For information only" in last_child
    last_parent = joined([m for m in MODEL.calls if "PARENT-TASK" in joined(m)][-1])
    order(last_parent, "CHILD-RESULT", "Make it shorter.")
    assert last_parent.count("For information only") == 0


def to_a_sub_agents_chat():
    """A message typed into the sub-agent's own chat steers it and never starts a second run beside it."""
    def script(n, m):
        text = joined(m)
        if "PARENT-TASK" in text:
            return final("parent done") if "CHILD-RESULT" in text else tool("call_subordinate", profile="", message="CHILD-TASK do it", reset="true")
        return final("CHILD-RESULT") if "STEP-9" in text else shell("sleep 4; echo STEP-$((4+5))")
    context, task = begin("child chat", script, "PARENT-TASK go")
    child = until(lambda: next((c for c in AgentContext.all() if c.get_output_data("parent_context_id") == context.id), None), what="the sub-agent")
    in_tool(child)
    returned = child.communicate(UserMessage("Only the summary.", id=str(uuid.uuid4())))
    assert not returned.is_alive() and returned is not context.task
    assert task.result_sync(90) == "parent done"
    order(joined(child_calls()[-1]), "STEP-9", "Only the summary.")
    assert "For information only" not in joined(child_calls()[-1])
    assert len(child_calls()) == 2, "a second run began beside the sub-agent"


def with_parallel_sub_agents():
    def script(n, m):
        text = joined(m)
        if "PARENT-TASK" in text:
            if "CHILD-RESULT" in text:
                return final("parent done")
            return tool("parallel", tool_calls=[{"tool_name": "call_subordinate", "tool_args": {"message": "CHILD-TASK %d" % k, "reset": True}} for k in (1, 2)])
        return final("CHILD-RESULT") if "STEP-9" in text else shell("sleep 4; echo STEP-$((4+5))")
    context, task = begin("parallel", script, "PARENT-TASK go")
    until(lambda: len([c for c in AgentContext.all() if c.get_output_data("parent_context_id") == context.id]) == 2, what="two sub-agents")
    MODEL.wait_call(3)
    time.sleep(1)
    context.communicate(UserMessage("Use metric units.", id=str(uuid.uuid4())))
    assert task.result_sync(120) == "parent done"
    told = [m for m in child_calls() if "Use metric units." in joined(m)]
    assert len(told) == 2, "told %d of 2" % len(told)
    parent = joined([m for m in MODEL.calls if "PARENT-TASK" in joined(m)][-1])
    order(parent, "CHILD-RESULT", "Use metric units.")


def stopped():
    """Stop keeps the message by the composer; the next run does not take it by itself."""
    from api.stop import stop_context
    s = steering()
    context, task = begin("stop", lambda n, m: shell("sleep 20") if n == 1 else final("ok"))
    in_tool(context)
    context.communicate(UserMessage("left over", id="left1"))
    stop_context(context)
    time.sleep(1.5)
    assert [(i["text"], i["status"]) for i in s.view(context)["items"]] == [("left over", "held")], s.view(context)
    assert not context.is_running()
    MODEL.begin(lambda n, m: final("fresh"))
    again = context.communicate(UserMessage("something new", id=str(uuid.uuid4())))
    assert again.result_sync(60) == "fresh"
    assert "left over" not in joined(MODEL.calls[0])
    MODEL.begin(lambda n, m: final("after all"))
    assert s.send_item(context, "left1")
    MODEL.wait_call(1)
    idle(context)
    assert "left over" in joined(MODEL.calls[0])
    assert s.view(context)["items"] == []


def nudged():
    s = steering()
    context, task = begin("nudge", lambda n, m: shell("sleep 20") if n == 1 else final("nudged on"))
    in_tool(context)
    context.communicate(UserMessage("take me along", id="n1"))
    context.nudge()
    MODEL.wait_call(2)
    idle(context)
    assert "take me along" in joined(MODEL.calls[1]), "the nudge's run did not take the message up"
    assert s.view(context)["items"] == []


def failing():
    s = steering()
    context, task = begin("error", lambda n, m: shell("sleep 3; echo Q-$((5+2))") if n == 1 else {"status": 400})
    in_tool(context)
    context.communicate(UserMessage("keep me", id="k1"))
    idle(context, 90)
    time.sleep(1.5)
    seen = any("keep me" in joined(m) for m in MODEL.calls)
    held = [i["status"] for i in s.view(context)["items"]]
    assert seen or held == ["held"], "lost: %s" % held


def reset_and_removed():
    s = steering()
    context, task = begin("reset", lambda n, m: shell("sleep 20") if n == 1 else final("ok"))
    in_tool(context)
    context.communicate(UserMessage("gone", id="g1"))
    context.reset()
    time.sleep(1.2)
    assert s.view(context)["items"] == []
    context, task = begin("remove", lambda n, m: shell("sleep 20") if n == 1 else final("ok"))
    in_tool(context)
    context.communicate(UserMessage("gone", id="g2"))
    AgentContext.remove(context.id)
    time.sleep(1.2)
    assert AgentContext.get(context.id) is None
    assert not s._data_file(context.id).exists()


def paused():
    """A pause is the user stopping it: a half-written reply is cut, as Agent Zero does."""
    context, task = begin("pause", lambda n, m: {"text": shell("echo P-$((1+1))"), "seconds": 6} if n == 1 else final("ok"))
    MODEL.wait_call(1)
    time.sleep(1)
    context.paused = True
    time.sleep(0.6)
    context.communicate(UserMessage("Not that.", id=str(uuid.uuid4())))
    task.result_sync(60)
    assert context.paused is False
    text = joined(MODEL.calls[1])
    assert "Not that." in text and "P-2" not in text


def queue_first():
    """Agent Zero's own queue still works, and what is steered goes before it."""
    context, task = begin("queue", lambda n, m: shell("sleep 3; echo K-$((6+2))") if n == 1 else final("answer %d" % n))
    in_tool(context)
    mq.add(context, "queued for later", [])
    context.communicate(UserMessage("steered now", id=str(uuid.uuid4())))
    MODEL.wait_call(3)
    idle(context)
    order(joined(MODEL.calls[1]), "K-8", "steered now")
    assert "queued for later" not in joined(MODEL.calls[1])
    assert "queued for later" in joined(MODEL.calls[2])


def send_now_from_the_queue():
    """"Send now" in Agent Zero's queue steers instead of interrupting."""
    context, task = begin("send now", lambda n, m: shell("sleep 3; echo N-$((7+2))") if n == 1 else final("ok"))
    in_tool(context)
    item = mq.add(context, "from the queue", [])
    mq.send_message(context, mq.pop_item(context, item["id"]))
    task.result_sync(60)
    order(joined(MODEL.calls[1]), "N-9", "from the queue")
    lines = [i.content for i in context.log.logs if i.type == "user"]
    assert lines.count("from the queue") == 1, lines


def commands():
    import runpy
    run = runpy.run_path("/a0/usr/plugins/steering/commands/steering_command.py")["run"]

    def command(name, text, context):
        out = run({"invocation": {"command_name": name, "raw_arguments": text}, "context": {"context_id": context.id}})
        return out["effects"][0]["message"], out["effects"][0]["level"]
    context, task = begin("commands", lambda n, m: shell("sleep 5; echo C-$((8+2))") if n == 1 else final("answer %d" % n))
    in_tool(context)
    assert command("steer", "steer this", context)[0].startswith("Goes in")
    assert command("later", "later this", context)[0].startswith("Queued")
    assert command("steer", "", context)[1] == "error"
    MODEL.wait_call(3)
    idle(context)
    order(joined(MODEL.calls[1]), "C-10", "steer this")
    assert "wants it taken into account now" in joined(MODEL.calls[1])
    assert "later this" in joined(MODEL.calls[2])
    MODEL.begin(lambda n, m: shell("sleep 30") if n == 1 else final("cut"))
    context.communicate(UserMessage("again", id=str(uuid.uuid4())))
    in_tool(context)
    began = time.time()
    assert command("interrupt", "drop it", context)[0].startswith("Interrupted")
    idle(context)
    assert time.time() - began < 20, "the interruption did not cut the tool"
    assert "drop it" in joined(MODEL.calls[-1])


def a_broken_plugin():
    """A plugin that cannot be imported leaves sending untouched."""
    path = "/a0/usr/plugins/steering/helpers/steering.py"
    with open(path, encoding="utf-8") as f:
        whole = f.read()
    from helpers import modules
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("this is not python")
        modules.purge_namespace("usr.plugins.steering")
        context, task = begin("broken", lambda n, m: final("still works"))
        assert task.result_sync(60) == "still works"
        MODEL.begin(lambda n, m: shell("sleep 2; echo Z-$((1+1))") if n == 1 else final("native"))
        again = context.communicate(UserMessage("again", id=str(uuid.uuid4())))
        in_tool(context)
        context.communicate(UserMessage("mid-run", id=str(uuid.uuid4())))
        assert again.result_sync(60) == "native"
        assert "mid-run" in joined(MODEL.calls[-1])
    finally:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(whole)
        modules.purge_namespace("usr.plugins.steering")


def scheduled_run():
    """A run that is not the chat's own task (as the scheduler's) is steered too, not doubled."""
    from helpers.defer import DeferredTask
    MODEL.begin(lambda n, m: shell("sleep 4; echo S-$((9+2))") if n == 1 else final("scheduled done"))
    context = chat("scheduled")
    agent = context.agent0

    async def job():
        agent.hist_add_user_message(UserMessage("SCHEDULED"))
        return await agent.monologue()
    task = DeferredTask(thread_name="SchedulerTest")
    task.start_task(job)
    in_tool(context)
    context.communicate(UserMessage("plus the chart", id=str(uuid.uuid4())))
    assert task.result_sync(60) == "scheduled done"
    order(joined(MODEL.calls[1]), "S-11", "plus the chart")
    assert len(MODEL.calls) == 2


def long_history():
    """After a steer the running topic is still the running topic."""
    context, task = begin("topic", lambda n, m: shell("echo T-%d" % n) if n <= 3 else final("ok"))
    MODEL.wait_call(2)
    topics = len(context.agent0.history.topics)
    context.communicate(UserMessage("steered", id=str(uuid.uuid4())))
    task.result_sync(90)
    assert len(context.agent0.history.topics) == topics, "the steer moved the run to the older topics"
    assert "new_topic" not in vars(context.agent0.history)


SCENARIOS = [during_a_tool, native_during_a_tool, while_writing, several_messages, during_the_final_answer, ends_a_wait, a_wait_without_messages,
             with_a_sub_agent, to_a_sub_agents_chat, with_parallel_sub_agents, stopped, nudged, failing, reset_and_removed, paused, queue_first,
             send_now_from_the_queue, commands, a_broken_plugin, scheduled_run, long_history]


RESULTS = os.environ.get("STEERING_RESULTS", "/tmp/steering_results.txt")


def report(line):
    print("\n@@ " + line, flush=True)
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    wanted = WANTED
    open(RESULTS, "w").close()
    failures = 0
    for scenario in SCENARIOS:
        if wanted and scenario.__name__ not in wanted:
            continue
        began = time.time()
        try:
            plugin(True)
            scenario()
            report("PASS %-28s %5.1f s" % (scenario.__name__, time.time() - began))
        except Exception:
            failures += 1
            report("FAIL %-28s %5.1f s" % (scenario.__name__, time.time() - began))
            for line in traceback.format_exc().splitlines()[-6:]:
                report("  " + line)
        for context in list(AgentContext.all()):
            try:
                AgentContext.remove(context.id)
            except Exception:
                pass
    report("failures: %d" % failures)
    os._exit(failures)


if __name__ == "__main__":
    main()
