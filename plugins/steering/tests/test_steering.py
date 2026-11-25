"""Steering's rules against small stand-ins for Agent Zero (no Agent Zero needed). `in_container.py` runs the same rules against the real one."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]


@dataclass
class UserMessage:
    message: str
    attachments: list = field(default_factory=list)
    system_message: list = field(default_factory=list)
    id: str = ""


class Line:
    def __init__(self, type, content, id="", heading="", age=0.0):
        self.type, self.content, self.id, self.heading, self.timestamp = type, content, id, heading, time.time() - age


class Log:
    def __init__(self):
        self.logs = []

    def log(self, type, heading="", content="", kvps=None, update_progress=None, id=""):
        self.logs.append(Line(type, content, id, heading))


class Task:
    def __init__(self):
        self._future = Future()

    def is_alive(self):
        return not self._future.done()

    def kill(self):
        self._future.cancel()


class LoopTask:
    """The asyncio task an agent loop runs in."""
    def __init__(self):
        self.ended, self.callbacks = False, []

    @property
    def over(self):
        return self.ended

    @over.setter
    def over(self, value):
        self.ended = value
        for call in self.callbacks if value else ():
            call(self)

    def done(self):
        return self.ended

    def add_done_callback(self, call):
        self.callbacks.append(call)


class History:
    def __init__(self):
        self.topics = 1
        self.messages = []

    def new_topic(self):
        self.topics += 1


class LoopData:
    def __init__(self):
        self.extras_persistent, self.params_persistent, self.current_tool = {}, {}, None


class Agent:
    def __init__(self, number, context):
        self.number, self.context, self.history, self.loop_data = number, context, History(), LoopData()
        self.intervention, self.last_user_message, self.fail = None, None, False

    def hist_add_user_message(self, message, intervention=False):
        self.history.new_topic()
        if self.fail:
            raise RuntimeError("history is broken")
        self.history.messages.append((message, intervention))
        self.last_user_message = message


class AgentContext:
    chats = {}

    def __init__(self, id, parent=None):
        self.id, self.data, self.output_data, self.log, self.name = id, {}, {}, Log(), "chat " + id
        self.task, self.paused, self.streaming_agent, self.sent = None, False, None, []
        self.agent0 = Agent(1 if parent else 0, self)
        if parent:
            self.output_data["parent_context_id"] = parent.id
        AgentContext.chats[id] = self

    @staticmethod
    def get(id):
        return AgentContext.chats.get(id)

    @staticmethod
    def use(id):
        return AgentContext.chats.get(id)

    def get_output_data(self, key):
        return self.output_data.get(key)

    def set_output_data(self, key, value):
        self.output_data[key] = value

    def is_running(self):
        return bool(self.task and self.task.is_alive())

    def get_agent(self):
        return self.streaming_agent or self.agent0

    def communicate(self, msg):
        """Agent Zero's own, behind the start hook as `@extension.extensible` runs it."""
        data = {"args": (self, msg), "kwargs": {}}
        STEERING[0].on_communicate(data)
        if "result" in data:
            return data["result"]
        self.paused = False
        if self.is_running():
            self.get_agent().intervention = msg
        else:
            self.task = Task()
            self.sent.append(msg)
        return self.task


STEERING = [None]


def stand_ins():
    queue = types.ModuleType("helpers.message_queue")
    queue.UPLOAD_FOLDER = "/a0/usr/uploads"
    queue.queues = {}
    queue.has_queue = lambda context: bool(queue.queues.get(context.id))
    queue.add = lambda context, text, attachments=None, item_id=None: queue.queues.setdefault(context.id, []).append((text, list(attachments or []), item_id))
    queue.log_user_message = lambda context, message, attachments, message_id=None, source="": context.log.log("user", content=message, id=message_id)

    def send_next(context):
        items = queue.queues.get(context.id) or []
        if items:
            text, attachments, _ = items.pop(0)
            context.communicate(UserMessage(text, attachments))
        return bool(items)
    queue.send_next = send_next
    agent = types.ModuleType("agent")
    agent.AgentContext, agent.UserMessage, agent.Agent = AgentContext, UserMessage, Agent
    defer = types.ModuleType("helpers.defer")

    class DeferredTask:
        def __init__(self):
            self._future = None

        def is_alive(self):
            return self._future is not None and not self._future.done()

        def kill(self):
            self._future.cancel()
    defer.DeferredTask = DeferredTask
    monitor = types.ModuleType("helpers.state_monitor_integration")
    monitor.mark_dirty_for_context = lambda *a, **k: None
    plugins = types.ModuleType("helpers.plugins")
    plugins.config = {"mode": "automatic"}
    plugins.get_plugin_config = lambda name: dict(plugins.config)
    helpers = types.ModuleType("helpers")
    helpers.message_queue, helpers.defer, helpers.state_monitor_integration, helpers.plugins = queue, defer, monitor, plugins
    return {"agent": agent, "helpers": helpers, "helpers.message_queue": queue, "helpers.defer": defer,
            "helpers.state_monitor_integration": monitor, "helpers.plugins": plugins}


class Case(unittest.TestCase):
    def setUp(self):
        self.modules = stand_ins()
        self.before = {name: sys.modules.get(name) for name in self.modules}
        sys.modules.update(self.modules)
        spec = importlib.util.spec_from_file_location("steering_under_test", PLUGIN / "helpers" / "steering.py")
        self.s = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.s)
        STEERING[0] = self.s
        self.folder = tempfile.TemporaryDirectory()
        self.s._data_file = lambda context_id: Path(self.folder.name) / (str(context_id) + ".json")
        self.later = []
        self.s._later = lambda function, *args: self.later.append((function, args))
        AgentContext.chats = {}
        self.queue = self.modules["helpers.message_queue"]
        self.chat = AgentContext("a")

    def tearDown(self):
        self.folder.cleanup()
        for name, module in self.before.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def settle(self):
        while self.later:
            function, args = self.later.pop(0)
            function(*args)

    def running(self, context=None):
        context = context or self.chat
        context.task = Task()
        task = LoopTask()
        self.s.run_began(context.agent0, task)
        context.streaming_agent = context.agent0
        return task

    def ends(self, context=None, outcome="finished"):
        context = context or self.chat
        future = context.task._future
        self.s.run_over(context.agent0, failed=outcome == "failed")
        context.streaming_agent = None
        if outcome == "stopped":
            context.task.kill()
        elif outcome == "failed":
            future.set_exception(RuntimeError("boom"))
        else:
            future.set_result("answer")

    def texts(self, context=None):
        return [message.message for message, _ in (context or self.chat).agent0.history.messages]

    def statuses(self, context=None):
        return [(item["text"], item["status"]) for item in self.s.state(context or self.chat)["items"]]


class Arriving(Case):
    def test_nothing_is_kept_when_no_agent_works(self):
        self.chat.communicate(UserMessage("hello"))
        self.assertEqual([m.message for m in self.chat.sent], ["hello"])
        self.assertIsNone(self.s.state(self.chat, create=False))

    def test_a_message_during_a_run_is_kept_and_the_step_is_not_interrupted(self):
        self.running()
        task = self.chat.task
        returned = self.chat.communicate(UserMessage("also the tests", id="m1"))
        self.assertIs(returned, task)
        self.assertIsNone(self.chat.agent0.intervention)
        self.assertEqual(self.statuses(), [("also the tests", "waiting")])
        self.assertEqual(self.s.state(self.chat)["items"][0]["id"], "m1")

    def test_several_messages_all_stay_and_keep_their_order(self):
        self.running()
        for text in ("one", "two", "three"):
            self.chat.communicate(UserMessage(text))
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertEqual(self.texts(), ["one", "two", "three"])

    def test_an_interruption_stays_agent_zeros_own(self):
        self.running()
        self.s.interrupt(self.chat, "stop that")
        self.assertEqual(self.chat.agent0.intervention.message, "stop that")
        self.assertEqual(self.statuses(), [])

    def test_a_paused_agent_takes_the_message_where_it_stopped(self):
        self.running()
        self.chat.paused = True
        self.chat.communicate(UserMessage("not like that"))
        self.assertEqual(self.chat.agent0.intervention.message, "not like that")
        self.assertFalse(self.chat.paused)

    def test_a_tool_paused_half_way_keeps_its_result(self):
        self.running()
        self.chat.paused = True
        self.chat.agent0.loop_data.current_tool = object()
        self.chat.communicate(UserMessage("not like that"))
        self.assertIsNone(self.chat.agent0.intervention)
        self.assertFalse(self.chat.paused)
        self.assertEqual(self.statuses(), [("not like that", "waiting")])

    def test_a_message_to_a_sub_agents_chat_steers_that_sub_agent(self):
        root_task = self.running()
        child = AgentContext("c", parent=self.chat)
        self.s.run_began(child.agent0, root_task)                # the superior's task runs it; the chat has none of its own
        returned = child.communicate(UserMessage("use the other file"))
        self.assertIsNot(returned, self.chat.task)
        self.assertFalse(returned.is_alive())
        returned.kill()
        self.assertTrue(self.chat.task.is_alive())                # stopping what came back never stops the superior
        self.assertEqual(child.sent, [])                          # and no second run begins beside it
        self.s.deliver(child.agent0, child.agent0.loop_data)
        self.assertEqual(self.texts(child), ["use the other file"])

    def test_a_scheduled_run_is_steered_too(self):
        self.s.run_began(self.chat.agent0, LoopTask())
        self.chat.communicate(UserMessage("and the weather"))
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(self.statuses(), [("and the weather", "waiting")])

    def test_a_run_stopped_in_its_first_moment_does_not_keep_the_message_that_follows(self):
        async def _run(func, args, kwargs):                       # as Agent Zero's DeferredTask begins a chat's own run
            return await func(*args, **kwargs)

        async def chain(owner):
            self.s.run_began(self.chat.agent0, asyncio.current_task())
            return self.s.live(self.chat), self.s.busy(self.chat)
        self.chat.task = Task()
        self.chat.task.kill()                                     # Stop was faster than the loop's first step
        self.assertEqual(asyncio.run(_run(types.MethodType(chain, self.chat), (), {})), ("", False))
        other = AgentContext("other")
        self.assertEqual(asyncio.run(_run(types.MethodType(chain, other), (), {})), ("inside", True))        # its superior's run: at work
        self.assertEqual(asyncio.run(chain(None)), ("inside", True))                                          # a run begun in another way

    def test_a_finished_loop_is_not_at_work(self):
        task = LoopTask()
        self.s.run_began(self.chat.agent0, task)
        task.over = True
        self.assertEqual(self.s.live(self.chat), "")

    def test_too_many_waiting_messages_go_agent_zeros_own_way(self):
        self.running()
        for n in range(self.s.LIMIT):
            self.chat.communicate(UserMessage("m%d" % n))
        self.chat.communicate(UserMessage("one too many"))
        self.assertEqual(self.chat.agent0.intervention.message, "one too many")

    def test_the_line_a_sender_already_wrote_is_found_by_id_or_text(self):
        self.running()
        self.chat.log.log("user", content="by id", id="x1")
        self.chat.communicate(UserMessage("by id", id="x1"))
        self.chat.log.log("user", content="by text", id="x2")
        self.chat.communicate(UserMessage("by text"))
        self.chat.communicate(UserMessage("no line yet"))
        items = self.s.state(self.chat)["items"]
        self.assertEqual([(i["logged"], i["id"]) for i in items[:2]], [(True, "x1"), (True, "x2")])
        self.assertFalse(items[2]["logged"])
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        lines = [line.content for line in self.chat.log.logs if line.type == "user"]
        self.assertEqual(lines, ["by id", "by text", "no line yet"])

    def test_an_old_line_with_the_same_words_is_another_message(self):
        self.running()
        self.chat.log.logs.append(Line("user", "again", "old", age=120))
        self.chat.communicate(UserMessage("again"))
        self.assertFalse(self.s.state(self.chat)["items"][0]["logged"])

    def test_an_instruction_without_words_gets_no_line_in_the_chat(self):
        self.running()
        self.chat.communicate(UserMessage("", system_message=["sending failed"]))
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertEqual([line for line in self.chat.log.logs if line.type == "user"], [])
        self.assertEqual(self.chat.agent0.history.messages[0][0].system_message[0], "sending failed")


class NextStep(Case):
    def test_it_joins_the_running_topic_as_a_user_message_with_a_note(self):
        self.running()
        self.chat.communicate(UserMessage("use metric units", attachments=["/a0/usr/uploads/a.png"], id="m1"))
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        (message, intervention), = self.chat.agent0.history.messages
        self.assertTrue(intervention)
        self.assertEqual((message.message, message.attachments, message.id), ("use metric units", ["/a0/usr/uploads/a.png"], "m1"))
        self.assertEqual(message.system_message, [self.s.AUTOMATIC_NOTE])
        self.assertEqual(self.chat.agent0.history.topics, 1)
        self.chat.agent0.history.new_topic()
        self.assertEqual(self.chat.agent0.history.topics, 2)       # the history works as before afterwards
        self.assertEqual(self.statuses(), [("use metric units", "delivered")])

    def test_the_history_works_again_after_a_failure(self):
        self.running()
        self.chat.communicate(UserMessage("x"))
        self.chat.agent0.fail = True
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertNotIn("new_topic", vars(self.chat.agent0.history))
        self.assertEqual(self.statuses(), [("x", "held")])          # never lost

    def test_steer_mode_says_to_work_it_in_now(self):
        self.s.set_mode(self.chat, "steer")
        self.running()
        self.chat.communicate(UserMessage("x"))
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertEqual(self.chat.agent0.history.messages[0][0].system_message, [self.s.STEER_NOTE])

    def test_queue_mode_still_steers_what_is_sent_on_purpose(self):
        self.s.set_mode(self.chat, "queue")
        self.running()
        self.chat.communicate(UserMessage("sent now"))
        self.assertEqual(self.statuses(), [("sent now", "waiting")])

    def test_it_stays_in_view_for_the_rest_of_the_run(self):
        self.running()
        for text in ("first", "second"):
            self.chat.communicate(UserMessage(text))
        loop_data = self.chat.agent0.loop_data
        self.s.deliver(self.chat.agent0, loop_data)
        self.assertIn("1. first\n2. second", loop_data.extras_persistent["steering"])
        self.chat.communicate(UserMessage("z" * 9000))
        self.s.deliver(self.chat.agent0, loop_data)
        self.assertLess(len(loop_data.extras_persistent["steering"]), 8000)

    def test_nothing_happens_without_messages(self):
        self.running()
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertEqual(self.chat.agent0.loop_data.extras_persistent, {})

    def test_each_message_goes_in_once(self):
        self.running()
        self.chat.communicate(UserMessage("once"))
        for _ in range(3):
            self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertEqual(self.texts(), ["once"])

    def test_a_wait_ends_for_the_agent_the_message_is_for_only(self):
        root_task = self.running()
        child = AgentContext("c", parent=self.chat)
        self.s.run_began(child.agent0, root_task)
        self.chat.communicate(UserMessage("hurry"))
        self.assertTrue(self.s.pending(self.chat.agent0))
        self.assertFalse(self.s.pending(child.agent0))


class SubAgents(Case):
    def child(self, began=True):
        child = AgentContext("c", parent=self.chat)
        if began:
            self.s.run_began(child.agent0, LoopTask())
        return child

    def test_a_sub_agent_at_work_is_told_once_and_its_superior_still_gets_the_message(self):
        self.running()
        child = self.child()
        self.chat.communicate(UserMessage("also email Bob"))
        for _ in range(2):
            self.s.deliver(child.agent0, child.agent0.loop_data)
        self.assertEqual(self.texts(child), ["also email Bob"])
        self.assertEqual(child.agent0.history.messages[0][0].system_message, [self.s.COPY_NOTE])
        self.assertEqual([line.type for line in child.log.logs], ["info"])
        self.assertEqual(self.statuses(), [("also email Bob", "waiting")])
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertEqual(self.texts(), ["also email Bob"])

    def test_one_that_began_later_already_knows_from_its_superior(self):
        self.running()
        self.chat.communicate(UserMessage("earlier"))
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        time.sleep(0.02)
        child = self.child()
        self.s.deliver(child.agent0, child.agent0.loop_data)
        self.assertEqual(self.texts(child), [])

    def test_one_still_at_work_after_its_superior_took_it_up_is_told_too(self):
        self.running()
        child = self.child()
        self.chat.communicate(UserMessage("change the colour"))
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.s.deliver(child.agent0, child.agent0.loop_data)
        self.assertEqual(self.texts(child), ["change the colour"])

    def test_grandchildren_are_told_too(self):
        self.running()
        child = self.child()
        grandchild = AgentContext("g", parent=child)
        self.s.run_began(grandchild.agent0, LoopTask())
        self.chat.communicate(UserMessage("stop using tabs"))
        self.s.deliver(grandchild.agent0, grandchild.agent0.loop_data)
        self.assertEqual(self.texts(grandchild), ["stop using tabs"])

    def test_a_sub_agent_living_in_the_same_chat_is_told_and_the_main_agent_gets_the_message(self):
        self.running()
        helper = Agent(1, self.chat)
        self.s.run_began(helper, LoopTask())
        self.chat.communicate(UserMessage("shorter please"))
        self.s.deliver(helper, helper.loop_data)
        self.assertEqual([m.message for m, _ in helper.history.messages], ["shorter please"])
        self.assertEqual(helper.history.messages[0][0].system_message, [self.s.COPY_NOTE])
        self.assertEqual(self.statuses(), [("shorter please", "waiting")])

    def test_what_a_sub_agent_could_not_take_up_goes_to_its_superior(self):
        root_task = self.running()
        child = AgentContext("c", parent=self.chat)
        self.s.run_began(child.agent0, root_task)
        child.communicate(UserMessage("too late"))
        self.s.run_over(child.agent0)
        self.assertEqual(self.statuses(child), [])
        self.assertEqual(self.statuses(), [("too late", "waiting")])
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        notes = self.chat.agent0.history.messages[0][0].system_message
        self.assertIn("finished before the message could be taken up", notes[0])
        self.assertEqual(child.sent, [])


class RunEnds(Case):
    def states(self, chat):
        return [status for _, status in self.statuses(chat)]

    def test_stopping_the_main_run_holds_what_waits_in_its_sub_agents_chat(self):
        # Agent Zero's hooks do not see a cancelled loop (its wrapper catches only `Exception`), so `run_over` is not called for it.
        for seen in (True, False):                                                     # whether the end of the loop's task is told in time
            with self.subTest(seen):
                child = AgentContext("child%d" % seen, parent=self.chat)
                loop = self.running(self.chat)
                self.s.run_began(child.agent0, loop)
                child.streaming_agent = child.agent0
                child.communicate(UserMessage("only peer-reviewed sources"))
                self.assertEqual(self.states(child), ["waiting"])
                self.chat.task.kill()
                if not seen:
                    loop.callbacks.clear()
                loop.over = True
                self.settle()
                if not seen:
                    self.s.run_began(child.agent0, self.running(self.chat))
                self.assertEqual(self.states(child), ["held"])
                # The same sub-agent is called again for something else: what was held stays with the user.
                self.s.run_began(child.agent0, self.running(self.chat))
                child.streaming_agent = child.agent0
                self.s.deliver(child.agent0, child.agent0.loop_data)
                self.assertEqual(self.texts(child), [])
                self.assertEqual([item["status"] for item in self.s.view(child)["items"]], ["held"])

    def test_an_inner_loop_that_ended_by_itself_is_not_held_when_its_task_ends_later(self):
        child = AgentContext("b", parent=self.chat)
        loop = self.running(self.chat)
        self.s.run_began(child.agent0, loop)
        self.s.run_over(child.agent0)
        second = LoopTask()
        self.s.run_began(child.agent0, second)                                         # at work again, in another task
        child.streaming_agent = child.agent0
        child.communicate(UserMessage("and the opening hours"))
        loop.over = True                                                               # the first task ends only now
        self.assertEqual(self.states(child), ["waiting"])
        self.s.deliver(child.agent0, child.agent0.loop_data)
        self.assertEqual(self.texts(child), ["and the opening hours"])

    def test_a_parallel_sub_agents_chat_never_starts_a_run_by_itself(self):
        child = AgentContext("b", parent=self.chat)
        self.running(self.chat)
        self.running(child)                                                            # a task of its own, as Agent Zero's parallel tool gives it
        child.communicate(UserMessage("and the opening hours"))
        self.ends(child)                                                               # too late: it was writing its final answer
        self.settle()
        self.assertEqual((child.sent, self.states(child)), ([], []))
        self.assertEqual(self.states(self.chat), ["waiting"])                          # the agent that waits for it takes it up
        self.assertIn("finished before the message could be taken up", self.s.state(self.chat)["items"][0]["system"][0])

    def test_one_whose_superior_has_ended_too_is_held(self):
        child = AgentContext("b", parent=self.chat)
        self.running(self.chat)
        self.running(child)
        child.communicate(UserMessage("and the opening hours"))
        kept = self.s.steer(child, "asked by another plugin", caller=True)
        self.ends(child)
        self.ends(self.chat)
        self.settle()
        self.assertEqual((child.sent, self.states(child), self.s.status(child, kept)), ([], ["held", "undelivered"], "undelivered"))

    def test_a_sender_that_handles_the_end_itself_learns_that_the_run_was_stopped(self):
        for how, expected in (("stopped", "stopped"), ("failed", "stopped"), ("finished", "undelivered")):
            with self.subTest(how):
                chat = AgentContext("s-" + how)
                self.running(chat)
                kept = self.s.steer(chat, "use euros", caller=True)
                self.ends(chat, how)
                self.settle()
                self.assertEqual((self.s.status(chat, kept), chat.sent), (expected, []))
                self.assertIsNotNone(self.s.forget(chat, kept))
                self.assertEqual(self.s.status(chat, kept), "")

    def test_a_message_that_came_too_late_starts_the_next_run(self):
        self.running()
        self.chat.communicate(UserMessage("and one more thing"))
        self.ends()
        self.settle()
        self.assertEqual([m.message for m in self.chat.sent], ["and one more thing"])
        self.assertEqual(self.statuses(), [("and one more thing", "sent")])

    def test_several_too_late_start_one_run_and_the_rest_join_its_first_step(self):
        self.running()
        for text in ("one", "two", "three"):
            self.chat.communicate(UserMessage(text))
        self.ends()
        self.settle()
        self.assertEqual([m.message for m in self.chat.sent], ["one"])
        self.s.run_began(self.chat.agent0, LoopTask())
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertEqual(self.texts(), ["two", "three"])

    def test_a_message_that_lands_after_the_run_is_over_but_before_it_is_settled(self):
        self.running()
        self.chat.communicate(UserMessage("early"))
        self.ends()
        self.chat.communicate(UserMessage("right at the end"))     # nothing runs any more: an ordinary message
        self.assertEqual([m.message for m in self.chat.sent], ["right at the end"])
        self.settle()
        self.assertEqual(self.statuses(), [("early", "waiting")])  # the new run's first step takes it up
        self.s.run_began(self.chat.agent0, LoopTask())
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertEqual(self.texts(), ["early"])

    def test_one_stored_just_as_the_future_completes_is_settled(self):
        self.running()
        self.chat.task._future.set_result("answer")                # over, though nobody looked yet
        st = self.s.state(self.chat)
        self.s._store(self.chat, self.s._item("in the gap"))
        self.settle()
        self.assertEqual([m.message for m in self.chat.sent], ["in the gap"])
        self.assertIsNotNone(st["watched"])

    def test_stop_holds_the_messages_and_the_next_run_does_not_take_them(self):
        self.running()
        self.chat.communicate(UserMessage("left over"))
        self.ends(outcome="stopped")
        self.assertEqual(self.statuses(), [("left over", "held")])  # at once, not only after the delay
        self.chat.communicate(UserMessage("something else"))
        self.settle()
        self.s.run_began(self.chat.agent0, LoopTask())
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertEqual(self.texts(), [])
        self.assertEqual(self.statuses(), [("left over", "held")])
        self.assertEqual([i["status"] for i in self.s.view(self.chat)["items"]], ["held"])

    def test_a_failed_run_holds_the_messages(self):
        self.running()
        self.chat.communicate(UserMessage("left over"))
        self.ends(outcome="failed")
        self.settle()
        self.assertEqual(self.statuses(), [("left over", "held")])
        self.assertEqual(self.chat.sent, [])

    def test_a_nudge_takes_the_messages_along(self):
        self.running()
        self.chat.communicate(UserMessage("take me along"))
        self.s.state(self.chat)["keep"] = True
        self.ends(outcome="stopped")
        self.chat.communicate(UserMessage("nudge"))
        self.s.state(self.chat)["keep"] = False
        self.settle()
        self.s.run_began(self.chat.agent0, LoopTask())
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertEqual(self.texts(), ["take me along"])

    def test_a_run_started_with_a_message_that_fails_at_once_does_not_lose_the_rest(self):
        self.running()
        for text in ("one", "two"):
            self.chat.communicate(UserMessage(text))
        self.ends()
        self.settle()
        self.chat.task._future.set_exception(RuntimeError("no model"))
        self.settle()
        self.assertEqual(self.statuses(), [("one", "sent"), ("two", "held")])

    def test_a_sender_that_handles_the_end_itself_is_asked_and_no_run_begins(self):
        self.running()
        item = self.s.steer(self.chat, "change the plan", source="convo", caller=True)
        self.assertEqual(self.s.status(self.chat, item), "waiting")
        self.ends()
        self.settle()
        self.assertEqual(self.s.status(self.chat, item), "undelivered")
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(self.s.view(self.chat)["items"], [])
        self.assertEqual(self.s.forget(self.chat, item)["text"], "change the plan")
        self.assertEqual(self.s.status(self.chat, item), "")

    def test_a_removed_chat_is_forgotten(self):
        self.running()
        self.chat.communicate(UserMessage("gone"))
        self.s.clear(self.chat, forget_mode=True)
        AgentContext.chats.pop("a")
        self.ends(outcome="stopped")
        self.settle()
        self.assertEqual(self.statuses(), [])
        self.assertEqual(list(Path(self.folder.name).iterdir()), [])

    def test_a_scheduled_run_that_ends_with_a_message_waiting_starts_the_next(self):
        self.s.run_began(self.chat.agent0, LoopTask())
        self.chat.communicate(UserMessage("after that"))
        self.s.run_over(self.chat.agent0)
        self.settle()
        self.assertEqual([m.message for m in self.chat.sent], ["after that"])


class Saved(Case):
    def test_a_waiting_message_survives_a_restart_as_held(self):
        self.s.set_mode(self.chat, "steer")
        self.running()
        self.chat.communicate(UserMessage("remember me", attachments=["/a0/usr/uploads/a.png"], id="m1"))
        saved = json.loads((Path(self.folder.name) / "a.json").read_text(encoding="utf-8"))
        self.assertEqual([i["text"] for i in saved["items"]], ["remember me"])
        again = AgentContext("a")                                   # Agent Zero restarted
        view = self.s.view(again)
        self.assertEqual([(i["text"], i["status"], i["attachments"]) for i in view["items"]], [("remember me", "held", ["a.png"])])
        self.assertEqual(view["mode"], "steer")
        self.assertTrue(self.s.send_item(again, "m1"))
        self.assertEqual([m.message for m in again.sent], ["remember me"])

    def test_nothing_is_left_on_disk_when_nothing_waits(self):
        self.running()
        self.chat.communicate(UserMessage("x"))
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertEqual(list(Path(self.folder.name).iterdir()), [])

    def test_a_broken_file_is_no_message(self):
        (Path(self.folder.name) / "a.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.s.view(self.chat)["items"], [])

    def test_a_folder_that_cannot_be_written_never_stops_a_message(self):
        self.s._data_file = lambda context_id: Path(self.folder.name) / "missing\0" / "x.json"

        def refuse(*args, **kwargs):
            raise OSError("read only")
        self.s.os.replace, real = refuse, self.s.os.replace
        self.s._data_file = lambda context_id: Path(self.folder.name) / "x.json"
        try:
            self.running()
            self.chat.communicate(UserMessage("x"))
        finally:
            self.s.os.replace = real
        self.assertEqual(self.statuses(), [("x", "waiting")])

    def test_a_chat_id_never_leaves_the_folder(self):
        spec = importlib.util.spec_from_file_location("steering_paths", PLUGIN / "helpers" / "steering.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = PLUGIN / "data"
        for name in ("../../x", "a/b", "", "a\\b", "x" * 200):
            self.assertEqual(module._data_file(name).parent, data)
        self.assertEqual(module._data_file("nZJhDK0y").name, "nZJhDK0y.json")


class Composer(Case):
    def test_each_way_of_sending(self):
        self.assertEqual(self.s.send(self.chat, "start")["outcome"], "started")
        self.s.run_began(self.chat.agent0, LoopTask())
        self.assertEqual(self.s.send(self.chat, "a")["outcome"], "steering")
        self.assertEqual(self.s.send(self.chat, "b", how="queue")["outcome"], "queued")
        self.assertEqual(self.s.send(self.chat, "c", how="interrupt")["outcome"], "interrupted")
        self.assertEqual([q[0] for q in self.queue.queues["a"]], ["b"])
        self.assertEqual(self.chat.agent0.intervention.message, "c")
        self.assertEqual(self.statuses(), [("a", "waiting")])

    def test_queue_mode_queues(self):
        self.s.set_mode(self.chat, "queue")
        self.running()
        self.assertEqual(self.s.send(self.chat, "b")["outcome"], "queued")
        self.assertEqual(self.s.send(self.chat, "c", how="steer")["outcome"], "steering")

    def test_a_sender_learns_that_a_sub_agent_failed(self):
        for failed, expected in ((False, "undelivered"), (True, "stopped")):
            with self.subTest(failed):
                self.running()
                child = AgentContext("f%s" % failed, parent=self.chat)
                self.s.run_began(child.agent0, LoopTask())
                kept = self.s.steer(child, "use euros", caller=True)
                self.s.run_over(child.agent0, failed=failed)
                self.settle()
                self.assertEqual(self.s.status(child, kept), expected)

    def test_a_message_that_had_arrived_is_not_sent_again(self):
        self.running(self.chat)
        self.assertEqual(self.s.send(self.chat, "use euros", item_id="m1")["outcome"], "steering")
        self.assertEqual(self.s.send(self.chat, "use euros", item_id="m1"), {"outcome": "known", "id": "m1"})      # only the answer was lost
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertEqual(self.s.send(self.chat, "use euros", item_id="m1")["outcome"], "known")                   # in the run by now
        self.assertEqual(self.texts(), ["use euros"])
        self.chat.log.log("user", content="hello", id="m2")
        self.assertEqual(self.s.send(self.chat, "hello", item_id="m2")["outcome"], "known")                       # it had begun a run
        self.assertEqual(self.s.send(self.chat, "hello", item_id="m3")["outcome"], "steering")                    # the same words are another message

    def test_one_with_attachments_only_is_known_again_too(self):
        self.assertEqual(self.s.send(self.chat, "", attachments=["photo.png"], item_id="m1")["outcome"], "started")
        self.assertEqual(self.s.send(self.chat, "", attachments=["photo.png"], item_id="m1"), {"outcome": "known", "id": "m1"})
        self.assertEqual((len(self.chat.sent), [line.id for line in self.chat.log.logs]), (1, ["m1"]))      # and it stands in the chat

    def test_the_same_message_twice_at_once_is_one(self):
        self.running()
        real, inside = self.s.steer, []

        def slow(*args, **kwargs):
            if not inside:
                inside.append(self.s.send(self.chat, "use euros", item_id="m1")["outcome"])      # the second copy arrives while the first is on its way
            return real(*args, **kwargs)
        self.s.steer = slow
        try:
            self.assertEqual(self.s.send(self.chat, "use euros", item_id="m1")["outcome"], "steering")
        finally:
            self.s.steer = real
        self.assertEqual((inside, self.statuses()), (["known"], [("use euros", "waiting")]))

    def test_what_is_already_queued_goes_first(self):
        self.queue.queues["a"] = [("earlier", [], "q")]
        self.assertEqual(self.s.send(self.chat, "now")["outcome"], "queued")

    def test_nothing_to_send_is_refused(self):
        with self.assertRaises(ValueError):
            self.s.send(self.chat, "   ")

    def test_attachments_become_full_paths(self):
        self.running()
        self.s.send(self.chat, "look", attachments=["a.png", "/a0/usr/uploads/b.png", "../../etc/passwd"])
        self.assertEqual(self.s.state(self.chat)["items"][0]["attachments"],
                         ["/a0/usr/uploads/a.png", "/a0/usr/uploads/b.png", "/a0/usr/uploads/passwd"])

    def test_a_sub_agents_chat_has_no_queue_of_its_own_to_flush(self):
        task = self.running()
        child = AgentContext("c", parent=self.chat)
        self.s.run_began(child.agent0, task)
        self.assertEqual(self.s.send(child, "x", how="queue")["outcome"], "steering")

    def test_interrupting_a_sub_agent_never_starts_a_second_run(self):
        task = self.running()
        child = AgentContext("c", parent=self.chat)
        self.s.run_began(child.agent0, task)
        self.s.interrupt(child, "stop that")
        self.assertEqual(child.agent0.intervention.message, "stop that")
        self.assertEqual(child.sent, [])

    def test_the_strip(self):
        self.running()
        for text in ("a", "b", "c", "d"):
            self.s.send(self.chat, text, item_id=text)
        self.assertTrue(self.s.interrupt_item(self.chat, "a"))
        self.assertEqual(self.chat.agent0.intervention.message, "a")
        self.assertTrue(self.s.queue_item(self.chat, "b"))
        self.assertEqual([q[0] for q in self.queue.queues["a"]], ["b"])
        self.assertEqual(self.s.take_item(self.chat, "c"), {"text": "c", "attachments": []})
        self.assertIsNotNone(self.s.forget(self.chat, "d"))
        self.assertEqual(self.statuses(), [])
        for action in (self.s.interrupt_item, self.s.queue_item, self.s.send_item):
            self.assertFalse(action(self.chat, "gone"))
        self.assertIsNone(self.s.take_item(self.chat, "gone"))

    def test_a_delivered_message_cannot_be_taken_back(self):
        self.running()
        self.s.send(self.chat, "a", item_id="a")
        self.s.deliver(self.chat.agent0, self.chat.agent0.loop_data)
        self.assertIsNone(self.s.take_item(self.chat, "a"))

    def test_a_held_message_goes_into_the_run_at_work(self):
        self.running()
        self.s.send(self.chat, "a", item_id="a")
        self.ends(outcome="stopped")
        self.settle()
        self.running()
        self.assertTrue(self.s.send_item(self.chat, "a"))
        self.assertEqual(self.statuses(), [("a", "waiting")])

    def test_a_run_that_ends_in_between_just_starts_the_next(self):
        self.running()
        real = self.s.steer

        def too_late(*args, **kwargs):
            self.ends()
            return real(*args, **kwargs)
        self.s.steer = too_late
        self.assertEqual(self.s.send(self.chat, "x")["outcome"], "started")
        self.assertEqual([m.message for m in self.chat.sent], ["x"])

    def test_the_view_is_short_and_never_holds_whole_messages(self):
        self.running()
        self.s.send(self.chat, "y" * 5000)
        self.assertLessEqual(len(self.s.view(self.chat)["items"][0]["text"]), self.s.PREVIEW + 1)
        self.assertEqual(self.chat.output_data["_steering"]["items"][0]["status"], "waiting")

    def test_modes(self):
        self.assertEqual(self.s.view(self.chat)["mode"], "automatic")
        self.s.set_mode(self.chat, "queue")
        self.assertEqual((self.s.view(self.chat)["mode"], self.s.view(self.chat)["own_mode"]), ("queue", "queue"))
        self.s.set_mode(self.chat, "")
        self.assertEqual(self.s.view(self.chat)["own_mode"], "")
        with self.assertRaises(ValueError):
            self.s.set_mode(self.chat, "sometimes")

    def test_an_unknown_setting_is_automatic(self):
        self.modules["helpers.plugins"].config["mode"] = "whatever"
        self.s._config.update(at=0.0, mode="")
        self.assertEqual(self.s.default_mode(), "automatic")


class Threads(Case):
    def test_messages_from_many_threads_are_all_delivered_exactly_once(self):
        self.running()
        agent = self.chat.agent0
        stop = threading.Event()

        def deliver():
            while not stop.is_set():
                self.s.deliver(agent, agent.loop_data)
        loop = threading.Thread(target=deliver)
        loop.start()
        senders = [threading.Thread(target=lambda n=n: [self.chat.communicate(UserMessage("%d-%d" % (n, k))) for k in range(5)]) for n in range(8)]
        for thread in senders:
            thread.start()
        for thread in senders:
            thread.join()
        stop.set()
        loop.join()
        self.s.deliver(agent, agent.loop_data)
        self.assertEqual(sorted(self.texts()), sorted("%d-%d" % (n, k) for n in range(8) for k in range(5)))


if __name__ == "__main__":
    unittest.main()
