"""Direct (native, read-only) versus delegated tool execution, and honest job lifecycle states."""
import asyncio
import sys
import tempfile
import types
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

from plugins._convo.helpers.convo_host import Dispatcher, Host
from plugins._convo.helpers.convo_store import Store


def host_modules(*, memory_loaded=True, history=None):
    calls = {'search': [], 'memory': []}

    async def search(query):
        calls['search'].append(query); return {'results': [{'title': 'x ' + query}]}

    class Memory:
        index = {'scope-a': object()} if memory_loaded else {}
        def __init__(self, index, memory_subdir): self.subdir = memory_subdir
        async def search_similarity_threshold(self, query, limit, threshold):
            calls['memory'].append((self.subdir, query, limit, threshold)); return [query]
        @staticmethod
        def format_docs_plain(docs): return ['memo: ' + d for d in docs]

    agent = types.SimpleNamespace(history=types.SimpleNamespace(output=lambda: history or []))
    context = types.SimpleNamespace(agent0=agent)
    modules = {
        'agent': types.SimpleNamespace(AgentContext=types.SimpleNamespace(get=lambda target: context if target == 'a' else None), LoopData=object),
        'helpers.searxng': types.SimpleNamespace(search=search),
        'plugins._memory': types.ModuleType('plugins._memory'), 'plugins._memory.helpers': types.ModuleType('plugins._memory.helpers'),
        'plugins._memory.helpers.memory': types.SimpleNamespace(Memory=Memory, get_agent_memory_subdir=lambda agent: 'scope-a'),
    }
    return modules, calls


class DirectTools(unittest.IsolatedAsyncioTestCase):
    def patch(self, **kwargs):
        modules, self.calls = host_modules(**kwargs)
        patcher = patch.dict(sys.modules, modules); patcher.start(); self.addCleanup(patcher.stop)

    async def test_web_search_uses_the_native_searxng_integration_and_no_job(self):
        self.patch()
        result = await Host().direct('a', 'web_search', {'query': 'library hours'})
        self.assertEqual(self.calls['search'], ['library hours'])
        self.assertIn('library hours', result); self.assertLessEqual(len(result), 12000)

    async def test_memory_read_uses_the_selected_agents_loaded_scope_only(self):
        self.patch()
        result = await Host().direct('a', 'memory_read', {'query': 'favourite colour'})
        self.assertEqual(self.calls['memory'], [('scope-a', 'favourite colour', 5, .7)])
        self.assertIn('memo: favourite colour', result)

    async def test_memory_that_is_not_loaded_is_reported_not_initialised(self):
        self.patch(memory_loaded=False)
        with self.assertRaisesRegex(ValueError, 'not loaded'):
            await Host().direct('a', 'memory_read', {'query': 'x'})
        self.assertEqual(self.calls['memory'], [])

    async def test_empty_query_is_refused_before_any_native_call(self):
        self.patch()
        for name in ('web_search', 'memory_read'):
            for args in ({}, {'query': ''}, {'query': '   '}):
                with self.assertRaises(ValueError):
                    await Host().direct('a', name, args)
        self.assertEqual((self.calls['search'], self.calls['memory']), ([], []))

    async def test_camera_context_and_unknown_tools_have_no_direct_adapter(self):
        self.patch()
        for name in ('camera_context', 'delegate_task', 'shell'):
            with self.assertRaisesRegex(ValueError, 'no verified direct adapter'):
                await Host().direct('a', name, {'query': 'x'})

    async def test_unknown_chat_is_refused(self):
        self.patch()
        with self.assertRaises(ValueError):
            await Host().direct('nowhere', 'web_search', {'query': 'x'})


class RecentChat(unittest.TestCase):
    def test_is_read_only_bounded_and_tolerant(self):
        history = [{'ai': i % 2 == 1, 'content': {'user_message': 'message %d' % i} if i % 2 == 0 else ['assistant says', {'text': 'hello %d' % i}]} for i in range(40)]
        modules, _ = host_modules(history=history)
        with patch.dict(sys.modules, modules):
            chat = Host().recent_chat('a', limit=5)
            self.assertEqual(len(chat), 5)
            self.assertEqual([m['role'] for m in chat], ['assistant', 'user', 'assistant', 'user', 'assistant'])
            self.assertIn('hello 39', chat[-1]['text'])
            self.assertEqual(Host().recent_chat('missing'), [])             # an unknown chat or a failing history is just empty


class JobLifecycle(unittest.TestCase):
    class FakeHost:
        def __init__(self): self.free = True; self.futures = []; self.logs = []; self.refuse = False
        def available(self, target): return self.free
        def begin(self, job):
            if self.refuse: return None
            future = Future(); self.futures.append(future); return future
        def log(self, job, state): self.logs.append((job['status'], state))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'convo.db'); self.addCleanup(self.store.close)
        session = self.store.start('o', 'chat'); self.sid = session['id']
        self.store.accept_turn(self.sid, 'o', 0, 't', {'transcript': 'refactor the module'})
        self.job = self.store.submit(self.sid, 'o', 0, 't', 't:job', 'refactor the module')
        self.host = self.FakeHost(); self.dispatcher = Dispatcher(self.store, self.host)

    def kinds(self):
        return [e['kind'] for e in self.store.events('chat')]

    def test_a_queued_job_has_no_result_no_notice_and_no_completion_event(self):
        self.assertEqual((self.job['status'], self.job['result']), ('queued', ''))
        self.assertIn('job.queued', self.kinds()); self.assertNotIn('job.completed', self.kinds())
        with self.assertRaises(ValueError):
            self.store.claim_notice(self.sid, 'o', 0, self.job['id'])

    def test_a_busy_chat_keeps_the_job_queued(self):
        self.host.free = False
        self.dispatcher.tick()
        self.assertEqual(self.store.job(self.job['id'])['status'], 'queued')
        self.host.free, self.host.refuse = True, True              # the host declines the handoff: back to queued, never "running"
        self.dispatcher.tick()
        self.assertEqual(self.store.job(self.job['id'])['status'], 'queued')
        self.assertNotIn('job.completed', self.kinds())

    def test_running_is_not_completed_until_the_host_future_resolves(self):
        self.dispatcher.tick()
        self.assertEqual(self.store.job(self.job['id'])['status'], 'running')
        with self.assertRaises(ValueError):
            self.store.claim_notice(self.sid, 'o', 0, self.job['id'])
        self.dispatcher.tick()
        self.assertEqual(self.store.job(self.job['id'])['status'], 'running')
        self.host.futures[0].set_result('refactored 3 files')
        self.dispatcher.tick()
        done = self.store.job(self.job['id'])
        self.assertEqual((done['status'], done['result']), ('completed', 'refactored 3 files'))
        self.assertTrue(self.store.claim_notice(self.sid, 'o', 0, self.job['id']))

    def test_failure_is_failed_not_completed(self):
        self.dispatcher.tick()
        self.host.futures[0].set_exception(RuntimeError('boom'))
        self.dispatcher.tick()
        self.assertEqual(self.store.job(self.job['id'])['status'], 'failed')
        self.assertNotIn('job.completed', self.kinds())

    def test_a_restart_never_turns_in_flight_work_into_completed(self):
        self.dispatcher.tick()
        path = Path(self.temp.name) / 'convo.db'
        reopened = Store(path); self.addCleanup(reopened.close)
        self.assertEqual(reopened.job(self.job['id'])['status'], 'uncertain')

    def test_the_speech_a_job_gets_is_only_for_ended_work(self):
        from plugins._convo.helpers import convo_task_updates as updates
        for status in ('queued', 'dispatching'):
            self.assertFalse(updates.should_speak_update('frequent', updates.event_kind(status)).speak, status)
        self.assertFalse(updates.should_speak_update('milestones', updates.event_kind('running')).speak)
        for status in ('queued', 'dispatching', 'running'):
            self.assertNotIn('finished', updates.notice_instruction(status))


if __name__ == '__main__':
    unittest.main()
