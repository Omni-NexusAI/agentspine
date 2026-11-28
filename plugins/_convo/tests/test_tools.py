"""Direct (native, read-only) versus delegated tool execution, and honest job lifecycle states."""
import asyncio
import sys
import tempfile
import threading
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

    async def test_memory_that_is_not_loaded_is_loaded_on_first_use_off_the_voice_loop(self):
        """Agent Zero loads an agent's memory on its first use (Memory.get); voice does the same, in a background thread."""
        self.patch(memory_loaded=False)
        memory = sys.modules['plugins._memory.helpers.memory'].Memory
        threads = []

        async def get(agent):
            threads.append(threading.get_ident())
            memory.index['scope-a'] = object()
        memory.get = staticmethod(get)
        result = await Host().direct('a', 'memory_read', {'query': 'x'})
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())                  # never on the voice loop's thread
        self.assertEqual(self.calls['memory'], [('scope-a', 'x', 5, .7)])
        self.assertIn('memo: x', result)

    async def test_memory_that_cannot_be_loaded_is_reported_in_convos_words(self):
        self.patch(memory_loaded=False)
        memory = sys.modules['plugins._memory.helpers.memory'].Memory

        async def get(agent):
            raise RuntimeError('embedding model unavailable at http://internal')
        memory.get = staticmethod(get)
        with self.assertRaisesRegex(ValueError, '^Memory could not be loaded in time$'):
            await Host().direct('a', 'memory_read', {'query': 'x'})
        self.assertEqual(self.calls['memory'], [])

    async def test_web_search_results_are_shown_as_agent_zeros_search_tool_shows_them(self):
        self.patch()

        async def search(query):
            return {'results': [{'title': 'Title %d' % i, 'url': 'https://example.org/%d' % i, 'content': 'Snippet  %d\n' % i}
                                for i in range(8)]}
        sys.modules['helpers.searxng'].search = search
        host = Host()
        host.local_date = lambda: '17 November 2026'
        result = await host.direct('a', 'web_search', {'query': 'q'})
        header, _, body = result.partition('\n')
        self.assertEqual(header, "Web search results, 17 November 2026 (Agent Zero's search engine):")   # dated, so read as fresh
        blocks = body.split('\n\n')
        self.assertEqual(len(blocks), 5)                                        # the top five, title / address / snippet
        self.assertEqual(blocks[0], 'Title 0\nhttps://example.org/0\nSnippet 0')
        self.assertNotIn('{', result)                                            # never the service's raw JSON

    async def test_a_search_service_failure_is_reported_in_convos_words(self):
        self.patch()

        async def search(query):
            raise ConnectionError('connection refused by 10.0.0.5:8888')
        sys.modules['helpers.searxng'].search = search
        with self.assertRaisesRegex(ValueError, '^The search service did not answer$'):
            await Host().direct('a', 'web_search', {'query': 'q'})

    async def test_a_search_with_no_results_goes_on_to_the_news_engines_and_other_loaded_engines(self):
        """Live 2026-11-18: three searches came back empty because the default engines refused this address, while the news
        engines and an engine SearXNG had loaded but did not ask answered the same words."""
        self.patch()
        asked = []
        refused = [['google', 'Suspended: access denied'], ['brave', 'Suspended: too many requests'], ['duckduckgo', 'CAPTCHA']]

        async def ask(params):
            asked.append(dict(params))
            if params.get('categories') == 'news':
                return {'results': [{'title': 'Launch day', 'url': 'https://news.example/launch', 'content': 'It is out today.',
                                     'engine': 'bing news', 'publishedDate': '2026-11-17T09:00:00'}]}
            if params.get('engines'):
                return {'results': [{'title': 'Guide', 'url': 'https://web.example/guide', 'content': 'Everything about it.', 'engine': 'bing'}]}
            return {'results': [], 'unresponsive_engines': refused}

        async def config():
            return {'engines': [{'name': 'google', 'enabled': True, 'categories': ['general', 'web']},
                                {'name': 'bing', 'enabled': False, 'categories': ['general', 'web'], 'language_support': True},
                                {'name': 'wiby', 'enabled': False, 'categories': ['general', 'web'], 'language_support': False},
                                {'name': 'bing news', 'enabled': True, 'categories': ['news']}]}
        host = Host()
        host._search_ask, host._search_config = ask, config
        result = await host.direct('a', 'web_search', {'query': 'new game release'})
        self.assertEqual(asked[0], {'q': 'new game release'})                    # first the ordinary search, as before
        self.assertEqual(sorted((p.get('categories', ''), p.get('engines', '')) for p in asked[1:]), [('', 'bing'), ('news', '')])
        self.assertIn('Launch day', result)
        self.assertIn('(2026-11-17) It is out today.', result)                   # a dated result says its date
        self.assertIn('Guide', result)
        self.assertEqual((host.last_search['steps'][0], host.last_search['asked'], sorted(host.last_search['refused'])),
                         ('general', 3, ['brave', 'duckduckgo', 'google']))
        self.assertTrue(host.search_health().refused('google'))                  # remembered: not asked for by name for a while
        self.assertIn('bing', host.search_health().answered())

    async def test_engines_that_all_refuse_are_a_failed_search_not_an_empty_one(self):
        self.patch()

        async def ask(params):
            return {'results': [], 'unresponsive_engines': [['google', 'CAPTCHA'], ['bing news', 'Suspended']]}
        host = Host()
        host._search_ask = ask
        with self.assertRaisesRegex(ValueError, '^The search engines refused the request$'):
            await host.direct('a', 'web_search', {'query': 'q'})
        self.assertEqual(host.last_search['refused'], ['bing news', 'google'])
        from plugins._convo.helpers import convo_tools
        why = convo_tools.reason('web_search', ValueError('The search engines refused the request'))
        self.assertIn('refused', why)
        self.assertNotIn('google', why)                                          # in Convo's words; engine names stay in developer mode

    async def test_a_search_that_worked_and_found_nothing_is_still_just_empty(self):
        self.patch()

        async def ask(params):
            return {'results': [], 'unresponsive_engines': []}
        host = Host()
        host._search_ask = ask
        self.assertEqual(await host.direct('a', 'web_search', {'query': 'q'}), '')

    async def test_an_identical_search_within_moments_is_answered_from_memory(self):
        self.patch()
        host = Host()
        await host.direct('a', 'web_search', {'query': 'library hours'})
        await host.direct('a', 'web_search', {'query': 'Library  hours'})
        self.assertEqual(self.calls['search'], ['library hours'])                # one request for both
        self.assertTrue(host.last_search['cached'])

    async def test_outside_docker_the_category_and_engines_go_into_the_query_through_agent_zeros_helper(self):
        """Development mode: only Agent Zero's helper reaches SearXNG, so the ladder's choice is written in SearXNG's `!name` form."""
        self.patch()
        runtime = types.SimpleNamespace(is_dockerized=lambda: False)
        patcher = patch.dict(sys.modules, {'helpers.runtime': runtime}); patcher.start(); self.addCleanup(patcher.stop)
        host = Host()
        await host._search_ask({'q': 'storm warning', 'categories': 'news', 'time_range': 'day'})
        await host._search_ask({'q': 'storm warning', 'engines': 'bing,bing news'})
        await host._search_ask({'q': 'storm warning'})
        self.assertEqual(self.calls['search'], ['!news storm warning', '!bing !bing_news storm warning', 'storm warning'])
        self.assertIsNone(await host._search_config())

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


class HostSurface(unittest.IsolatedAsyncioTestCase):
    """What the voice tools ask of the host adapter besides a search: the search engines' last known state, the user's chats by
    name, the local clock and a notification."""
    REFUSING = {'results': [], 'unresponsive_engines': [['google', 'CAPTCHA'], ['brave', 'too many requests']]}
    ANSWERING = {'results': [{'title': 'Forecast', 'url': 'https://weather.example/', 'content': 'Sunny.', 'engine': 'bing'}]}

    def patch(self, **modules):
        base, self.calls = host_modules()
        patcher = patch.dict(sys.modules, {**base, **modules}); patcher.start(); self.addCleanup(patcher.stop)

    def host(self, *responses):
        """A host whose search service gives these responses in turn (the last one repeats)."""
        host, asked = Host(), []

        async def ask(params):
            asked.append(params)
            return responses[min(len(asked), len(responses)) - 1]

        async def config():
            return None
        host._search_ask, host._search_config = ask, config
        return host, asked

    async def test_the_search_state_is_unknown_until_a_search_ran_and_reading_it_sends_nothing(self):
        self.patch()
        host, asked = self.host(self.ANSWERING)
        self.assertEqual(host.search_state(), {'state': 'unknown', 'checked_at': '', 'answered': 0, 'refused': 0})
        self.assertEqual(asked, [])

    async def test_engines_that_answer_are_ok_and_the_time_of_the_last_search_is_kept(self):
        self.patch()
        host, asked = self.host(self.ANSWERING)
        await host.direct('a', 'web_search', {'query': 'weather'})
        state = host.search_state()
        self.assertEqual((state['state'], state['answered'], state['refused']), ('ok', 1, 0))
        self.assertRegex(state['checked_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$')
        before = len(asked)
        self.assertEqual(host.search_state(), state)                                # from memory: asking again sends nothing
        self.assertEqual(len(asked), before)

    async def test_every_engine_refusing_is_blocked_and_some_refusing_is_limited(self):
        self.patch()
        host, _ = self.host(self.REFUSING)
        with self.assertRaises(ValueError):
            await host.direct('a', 'web_search', {'query': 'weather'})
        state = host.search_state()
        self.assertEqual((state['state'], state['answered'], state['refused']), ('blocked', 0, 2))
        self.assertTrue(state['checked_at'])
        mixed, _ = self.host({**self.ANSWERING, 'unresponsive_engines': [['google', 'CAPTCHA']]})
        await mixed.direct('a', 'web_search', {'query': 'weather'})
        state = mixed.search_state()
        self.assertEqual((state['state'], state['answered'], state['refused']), ('limited', 1, 1))

    async def test_check_search_sends_one_small_search_now_and_answers_with_the_state_either_way(self):
        self.patch()
        host, asked = self.host(self.ANSWERING)
        state = await host.search_check()
        self.assertEqual((state['state'], state['answered'], state['refused']), ('ok', 1, 0))
        self.assertTrue(state['checked_at'])
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0], {'q': 'weather forecast'})                        # one fixed, harmless question; never the user's words
        await host.search_check()
        self.assertEqual(len(asked), 2)                                              # a check is never answered from the 90-second memory
        blocked, _ = self.host(self.REFUSING)
        state = await blocked.search_check()                                         # refusing engines are a state, not an error
        self.assertEqual((state['state'], state['answered'], state['refused']), ('blocked', 0, 2))

    async def test_a_chat_is_found_by_its_exact_name_or_id_else_by_a_unique_part_whatever_the_case(self):
        host = Host()
        chats = [types.SimpleNamespace(id='c1', name='Budget'), types.SimpleNamespace(id='c2', name='Budget 2026'),
                 types.SimpleNamespace(id='c3', name='Holiday plans'), types.SimpleNamespace(id='c4', name=None)]
        host.chat_contexts = lambda: chats
        for wanted, found in (('Budget', 'c1'), ('  budget  ', 'c1'), ('BUDGET 2026', 'c2'), ('2026', 'c2'), ('holiday', 'c3'), ('C3', 'c3'), ('c4', 'c4')):
            self.assertEqual((await host.find_chat(wanted))['id'], found, wanted)
        self.assertEqual(await host.find_chat('holiday'), {'id': 'c3', 'name': 'Holiday plans'})
        self.assertEqual(await host.find_chat('c4'), {'id': 'c4', 'name': 'c4'})     # a chat without a title is called by its id
        for wanted, why in (('bud', 'more than one chat matches that name'), ('recipes', 'no chat matches that name'), ('', 'no chat matches that name'),
                            (None, 'no chat matches that name'), ('   ', 'no chat matches that name')):
            with self.assertRaisesRegex(ValueError, '^' + why + '$'):
                await host.find_chat(wanted)

    async def test_the_local_clock_is_hours_and_minutes_in_the_users_timezone_and_utc_when_that_cannot_be_read(self):
        host = Host()
        self.assertEqual(host.local_clock(1795400000), '02:13')                      # no Agent Zero here: UTC (2026-11-23 02:13:20)
        localization = types.SimpleNamespace(Localization=types.SimpleNamespace(get=lambda: types.SimpleNamespace(
            get_timezone=lambda: 'Somewhere/Unknown', get_offset_minutes=lambda: 330)))
        with patch.dict(sys.modules, {'helpers.localization': localization, 'pytz': None}):
            self.assertEqual(host.local_clock(1795400000), '07:43')                  # the user's own offset, five and a half hours
            self.assertEqual(host.local_clock('1795400000.9'), '07:43')

    async def test_a_notification_is_sent_bounded_through_agent_zeros_own_notifications(self):
        sent = []
        manager = types.SimpleNamespace(send_notification=lambda *args, **kwargs: sent.append((args, kwargs)))
        notification = types.SimpleNamespace(NotificationManager=manager, NotificationPriority=types.SimpleNamespace(HIGH='high'),
                                             NotificationType=types.SimpleNamespace(INFO='info'))
        with patch.dict(sys.modules, {'helpers.notification': notification}):
            Host().notify('Timer', 'tea is due.')
            Host().notify('T' * 200, 'm' * 900)
        self.assertEqual(sent[0], (('info', 'high', 'tea is due.', 'Timer'), {'display_time': 12, 'group': 'convo-timers'}))
        self.assertEqual((len(sent[1][0][2]), len(sent[1][0][3])), (300, 80))


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
