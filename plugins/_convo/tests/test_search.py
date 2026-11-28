"""The web search ladder (`helpers/convo_search.py`): ordinary request, then news and SearXNG's own disabled web engines together,
engine health, the request and time bounds, and the short cache. Scripted `ask` functions and a virtual clock: nothing here
touches a network, so it says nothing about which real engines answer."""
import asyncio
import datetime
import importlib
import copy
import time
import unittest

from plugins._convo.helpers.convo_search import (BUDGET_SECONDS, CACHE_SECONDS, FALLBACK_ENGINES, MAX_REQUESTS, PENALTY_SECONDS,
                                                 Blocked, Health, fallback_engines, merge, search)

REFUSALS = [['brave', 'Suspended: too many requests'], ['duckduckgo', 'CAPTCHA'], ['google', 'Suspended: access denied'],
            ['startpage', 'parsing error']]
REFUSED = ['brave', 'duckduckgo', 'google', 'startpage']
# The shape of SearXNG's /config. Only the middle four and `presearch` are full web engines that are not asked by default and
# follow the language of the search; `naver` is a full web engine that does not (a regional index), so it is never named.
CONFIG = {'engines': [
    {'name': 'google', 'enabled': True, 'categories': ['general', 'web'], 'language_support': True},
    {'name': 'bing news', 'enabled': True, 'categories': ['news'], 'language_support': True},
    {'name': 'wikipedia', 'enabled': False, 'categories': ['general'], 'language_support': True},
    {'name': 'bing', 'enabled': False, 'categories': ['general', 'web'], 'language_support': True},
    {'name': 'mojeek', 'enabled': False, 'categories': ['general', 'web'], 'language_support': True},
    {'name': 'yahoo', 'enabled': False, 'categories': ['general', 'web'], 'language_support': True},
    {'name': 'qwant', 'enabled': False, 'categories': ['general', 'web'], 'language_support': True},
    {'name': 'bing images', 'enabled': False, 'categories': ['images', 'web'], 'language_support': True},
    {'name': 'naver', 'enabled': False, 'categories': ['general', 'web'], 'language_support': False},
    {'name': 'presearch', 'enabled': False, 'categories': ['general', 'web'], 'language_support': True},
]}
IN_CONFIG = {engine['name'] for engine in CONFIG['engines']}


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def hit(url, engine, content='snippet', date=None):
    return {'title': 'Title of ' + url, 'url': url, 'content': content, 'engine': engine, 'engines': [engine], 'publishedDate': date}


def page(*results, refused=(), answers=(), infoboxes=()):
    return {'query': 'q', 'results': list(results), 'answers': list(answers), 'infoboxes': list(infoboxes),
            'unresponsive_engines': list(refused)}


def kind(params):
    return 'news' if params.get('categories') == 'news' else 'fallback' if 'engines' in params else 'general'


class Asker:
    """A scripted `ask`: answers by the kind of request (a page, an exception to raise, or an async function) and keeps every request."""

    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    async def __call__(self, params):
        self.calls.append(dict(params))
        answer = self.answers.get(kind(params), page())
        if isinstance(answer, Exception):
            raise answer
        return await answer(params) if callable(answer) else answer

    @property
    def kinds(self):
        return [kind(params) for params in self.calls]


def together(count, answers, wait=1.0):
    """An async answer that lets no request return before `count` of them have entered, and a log of who entered and left when.
    Requests sent one after another would each wait for the other and run out of time."""
    log, everyone = [], asyncio.Event()

    async def answer(params):
        log.append(('in', kind(params)))
        if len(log) >= count:
            everyone.set()
        await asyncio.wait_for(everyone.wait(), wait)
        log.append(('out', kind(params)))
        return answers[kind(params)]
    return answer, log


async def turns(count=10):
    """Lets everything that is ready run, without waiting for time to pass."""
    for _ in range(count):
        await asyncio.sleep(0)


async def never(params):
    await asyncio.Event().wait()


NEWS = page(hit('https://news.example/storm', 'bing news', date='2026-11-17T08:30:00'), hit('https://Example.org/Page/', 'bing news'))
WEB = page(hit('https://example.org/page#top', 'bing', content='the same page again'), hit('https://example.org/other', 'mojeek'))


class Ladder(unittest.IsolatedAsyncioTestCase):
    async def test_an_ordinary_search_with_results_stops_at_the_first_request(self):
        ask, health, config_reads = Asker(general=page(hit('https://example.org/hours', 'google'))), Health(), []

        async def config():
            config_reads.append(1)
            return CONFIG
        result = await search('library hours', ask, health=health, config=config)
        self.assertEqual(ask.calls, [{'q': 'library hours'}])
        self.assertEqual((result['steps'], result['asked'], result['cached']), (['general'], 1, False))
        self.assertEqual((result['answered'], result['refused']), (['google'], []))
        self.assertEqual([item['url'] for item in result['results']], ['https://example.org/hours'])
        self.assertIsInstance(result['ms'], int)
        self.assertEqual(config_reads, [], 'the config is read only when the ladder needs it')
        self.assertEqual(health.answered(), ['google'])

    async def test_a_direct_answer_alone_also_ends_the_search(self):
        ask = Asker(general=page(answers=[{'answer': '14:05 in Lisbon'}], refused=REFUSALS))
        result = await search('time in lisbon', ask, health=Health(), config=CONFIG)
        self.assertEqual((ask.kinds, result['answers'], result['refused']), (['general'], [{'answer': '14:05 in Lisbon'}], REFUSED))

    def test_sources_are_woven_one_result_from_each_in_turn(self):
        """The model is shown only the first few results: they must not all come from one source."""
        weave = importlib.import_module('plugins._convo.helpers.convo_search').weave
        self.assertEqual(weave([['w1', 'w2', 'w3'], ['n1'], [], ['f1', 'f2']]), ['w1', 'n1', 'f1', 'w2', 'f2', 'w3'])
        self.assertEqual(weave([]), [])
        source = ['a', 'b']
        weave([source])
        self.assertEqual(source, ['a', 'b'])                                    # the lists given are not emptied

    async def test_news_from_years_ago_goes_after_the_fresh_and_the_undated_news(self):
        """Live 2026-11-18: the news category's archive put a 2008 race report among the first results of a question about last weekend."""
        old = hit('https://archive.example/2008-race', 'wikinews', date='2008-10-19T00:00:00')
        recent = hit('https://news.example/sunday-race', 'bing news', date='2026-11-15T18:00:00')
        undated = hit('https://news.example/race-report', 'bing news')
        ask = Asker(general=page(refused=REFUSALS), news=page(old, undated, recent))
        result = await search('who won the race', ask, health=Health(), config={}, today=datetime.date(2026, 11, 18))
        self.assertEqual([item['url'] for item in result['results']],
                         ['https://news.example/race-report', 'https://news.example/sunday-race', 'https://archive.example/2008-race'])
        fresh_first = importlib.import_module('plugins._convo.helpers.convo_search').fresh_first
        self.assertEqual(fresh_first([{'publishedDate': 'not a date'}, {'publishedDate': None}], datetime.date(2026, 11, 18)),
                         [{'publishedDate': 'not a date'}, {'publishedDate': None}])       # what cannot be dated stays where it is

    async def test_results_that_share_too_little_with_the_search_go_to_the_end_of_the_whole_list(self):
        """Live 2026-11-18: a news archive answered "weather in Chicago today" with an unrelated interview that said "today", and
        the weave then showed it second. One common word is not enough; two of the search's words are."""
        stray = hit('https://archive.example/interview', 'wikinews', content='An interview with two musicians at a gallery event held today')
        match = hit('https://news.example/chicago', 'bing news', content='Chicago weather: a mild stretch gives way to rain')
        web = hit('https://web.example/forecast', 'bing', content='Hourly weather for Chicago')
        ask = Asker(general=page(refused=REFUSALS), news=page(stray, match), fallback=page(web))
        result = await search('weather in Chicago today', ask, health=Health(), config=CONFIG)
        self.assertEqual([item['url'] for item in result['results']],
                         ['https://web.example/forecast', 'https://news.example/chicago', 'https://archive.example/interview'])
        relevant_first = importlib.import_module('plugins._convo.helpers.convo_search').relevant_first
        self.assertEqual(relevant_first([stray, match], 'ok'), [stray, match])            # no word long enough to judge by: left alone
        self.assertEqual(relevant_first([stray, match], '東京 天気'), [stray, match])       # another writing system: left alone
        self.assertEqual(relevant_first([stray, match], 'F1 results'), [stray, match])    # nothing shares a word: the order stays

    async def test_an_info_box_alone_does_not_end_the_search(self):
        """An encyclopedia entry is not an answer to the search: when the web engines refuse, the ladder still goes on."""
        box = {'infobox': 'Lisbon', 'content': 'Capital of Portugal'}
        ask = Asker(general=page(infoboxes=[box], refused=REFUSALS), news=NEWS, fallback=WEB)
        result = await search('lisbon tram strike', ask, health=Health(), config=CONFIG)
        self.assertEqual(sorted(ask.kinds), ['fallback', 'general', 'news'])
        self.assertTrue(result['results'])
        self.assertEqual(result['infoboxes'], [box])

    async def test_refused_engines_send_news_and_the_fallback_together_and_the_answers_are_merged(self):
        answer, log = together(2, {'news': NEWS, 'fallback': WEB})
        ask, health = Asker(general=page(refused=REFUSALS), news=answer, fallback=answer), Health()
        before = copy.deepcopy((NEWS, WEB))
        result = await search('storm warning', ask, health=health, config=CONFIG)
        self.assertEqual(sorted(log[:2]), [('in', 'fallback'), ('in', 'news')], 'both were on their way before either answered')
        self.assertEqual(len(log), 4)
        self.assertEqual((result['steps'], result['asked']), (['general', 'news', 'fallback'], 3))
        self.assertEqual(ask.calls[1], {'q': 'storm warning', 'categories': 'news'})
        self.assertEqual(ask.calls[2], {'q': 'storm warning', 'engines': 'bing,mojeek,yahoo,qwant'})
        # Woven, web before news (no recent results were asked for): one from each source in turn. One result per address (case,
        # trailing slash and fragment ignored), the first kept; a dated result says its date.
        self.assertEqual([item['url'] for item in result['results']],
                         ['https://example.org/page#top', 'https://news.example/storm', 'https://example.org/other'])
        self.assertEqual([item['content'] for item in result['results']], ['the same page again', '(2026-11-17) snippet', 'snippet'])
        self.assertEqual((result['answered'], result['refused']), (['bing', 'bing news', 'mojeek'], REFUSED))
        self.assertEqual((NEWS, WEB), before, 'the responses themselves are never changed')
        self.assertTrue(all(health.refused(name) for name in REFUSED))
        self.assertEqual(set(health.answered()), {'bing', 'bing news', 'mojeek'})

    async def test_the_config_may_be_a_function_read_once_and_only_after_the_first_request_found_nothing(self):
        reads = []

        async def config():
            reads.append(1)
            return CONFIG
        ask = Asker(general=page(refused=REFUSALS), news=NEWS, fallback=WEB)
        result = await search('storm warning', ask, health=Health(), config=config)
        self.assertEqual((reads, ask.kinds, len(result['results'])), ([1], ['general', 'news', 'fallback'], 3))

    async def test_without_a_usable_config_no_fallback_is_sent_and_news_still_is(self):
        async def broken():
            raise OSError('config unreachable')
        for config in (None, {}, {'engines': []}, {'engines': 'bing'}, 'bing', broken, lambda: None):
            with self.subTest(config=config):
                ask = Asker(general=page(refused=REFUSALS), news=NEWS, fallback=WEB)
                result = await search('storm warning', ask, health=Health(), config=config)
                self.assertEqual((ask.kinds, result['steps']), (['general', 'news'], ['general', 'news']))
                self.assertEqual([item['engine'] for item in result['results']], ['bing news', 'bing news'])

    async def test_an_engine_that_refused_the_fallback_is_not_named_in_the_next_one(self):
        health = Health()
        ask = Asker(general=page(refused=REFUSALS), news=NEWS, fallback=page(hit('https://example.org/a', 'mojeek'), refused=[['bing', 'CAPTCHA']]))
        await search('first', ask, health=health, config=CONFIG)
        await search('second', ask, health=health, config=CONFIG)
        named = [params['engines'].split(',') for params in ask.calls if 'engines' in params]
        self.assertEqual(named, [['bing', 'mojeek', 'yahoo', 'qwant'], ['mojeek', 'yahoo', 'qwant', 'presearch']])
        self.assertTrue(all(set(names) <= IN_CONFIG for names in named))

    async def test_blocked_when_nothing_came_back_and_engines_refused(self):
        ask = Asker(general=page(refused=REFUSALS), news=page(refused=[['bing news', 'timeout']]), fallback=page(refused=['bing']))
        with self.assertRaises(Blocked) as raised:
            await search('storm warning', ask, health=Health(), config=CONFIG)
        self.assertEqual(raised.exception.refused, ('bing', 'bing news', 'brave', 'duckduckgo', 'google', 'startpage'))
        self.assertEqual(ask.kinds, ['general', 'news', 'fallback'])

    async def test_blocked_when_every_request_fails(self):
        for failure in (OSError('connection refused'), asyncio.TimeoutError(), ValueError('not JSON')):
            with self.subTest(failure=failure):
                ask = Asker(general=failure, news=failure, fallback=failure)
                with self.assertRaises(Blocked) as raised:
                    await search('storm warning', ask, health=Health(), config=CONFIG)
                self.assertEqual((raised.exception.refused, ask.kinds), ((), ['general', 'news', 'fallback']))

    async def test_an_answer_that_is_not_an_object_is_a_failed_request(self):
        ask = Asker(general='<html>busy</html>', news=['x'], fallback=None)
        with self.assertRaises(Blocked):
            await search('storm warning', ask, health=Health(), config=CONFIG)

    async def test_a_search_that_worked_and_found_nothing_is_an_empty_result_not_blocked(self):
        ask, health = Asker(), Health()
        result = await search('zxqv qqqz', ask, health=health, config=CONFIG)
        self.assertEqual((result['results'], result['answers'], result['infoboxes']), ([], [], []))
        self.assertEqual((result['steps'], result['asked'], result['answered'], result['refused']), (['general', 'news', 'fallback'], 3, [], []))
        # One request failing does not make it blocked either, as long as another answered normally.
        result = await search('zxqv qqqz', Asker(general=OSError('reset')), health=health, config=CONFIG)
        self.assertEqual(result['results'], [])

    async def test_a_search_never_sends_more_than_the_allowed_requests(self):
        self.assertEqual(MAX_REQUESTS, 3)
        for recency in ('', 'day'):
            for answers in ({}, {'general': page(refused=REFUSALS)}, {'general': OSError('x'), 'news': OSError('x'), 'fallback': OSError('x')}):
                ask = Asker(**answers)
                try:
                    result = await search('anything', ask, health=Health(), config=CONFIG, recency=recency)
                    self.assertEqual(result['asked'], len(ask.calls))
                except Blocked:
                    pass
                self.assertLessEqual(len(ask.calls), MAX_REQUESTS)
                self.assertEqual(len(ask.kinds), len(set(ask.kinds)), 'each kind of request is sent once at most')

    async def test_recency_sets_the_time_range_and_sends_news_with_the_first_request(self):
        answer, log = together(2, {'general': page(hit('https://example.org/a', 'google')), 'news': NEWS})
        ask = Asker(general=answer, news=answer)
        result = await search('storm warning', ask, health=Health(), config=CONFIG, recency='week')
        self.assertEqual(sorted(log[:2]), [('in', 'general'), ('in', 'news')], 'both were on their way before either answered')
        self.assertEqual(ask.calls, [{'q': 'storm warning', 'time_range': 'week'}, {'q': 'storm warning', 'categories': 'news', 'time_range': 'week'}])
        # recent results were asked for: the news comes first, then one from each source in turn
        self.assertEqual((result['steps'], [item['engine'] for item in result['results']]), (['general', 'news'], ['bing news', 'google', 'bing news']))

    async def test_recency_with_an_empty_first_request_adds_only_the_fallback(self):
        ask = Asker(general=page(refused=REFUSALS), news=NEWS, fallback=WEB)
        result = await search('storm warning', ask, health=Health(), config=CONFIG, recency='day')
        self.assertEqual((ask.kinds, result['steps']), (['general', 'news', 'fallback'], ['general', 'news', 'fallback']))
        self.assertTrue(all(params['time_range'] == 'day' for params in ask.calls))

    async def test_an_unknown_recency_is_ignored(self):
        for recency in ('fortnight', None, 7, 'DAYS'):
            ask = Asker(general=page(hit('https://example.org/a', 'google')))
            await search('storm warning', ask, health=Health(), recency=recency)
            self.assertEqual(ask.calls, [{'q': 'storm warning'}])

    async def test_an_empty_query_is_refused_before_anything_is_sent(self):
        ask = Asker()
        for query in ('', '   ', None):
            with self.assertRaises(ValueError):
                await search(query, ask, health=Health())
        self.assertEqual(ask.calls, [])


class Budget(unittest.IsolatedAsyncioTestCase):
    async def test_requests_that_never_return_end_the_search_within_the_budget(self):
        ask, started = Asker(general=never, news=never, fallback=never), time.monotonic()
        with self.assertRaises(Blocked) as raised:
            await search('storm warning', ask, health=Health(), config=CONFIG, budget=.05)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(raised.exception.refused, ())
        self.assertLessEqual(len(ask.calls), MAX_REQUESTS)

    async def test_one_request_that_never_returns_counts_as_no_answer_and_the_others_are_kept(self):
        # A clock that stands still: the wait itself is still bounded by the budget, in real time.
        ask, started = Asker(general=page(refused=REFUSALS), news=NEWS, fallback=never), time.monotonic()
        result = await search('storm warning', ask, health=Health(), config=CONFIG, budget=.05, clock=Clock())
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual((result['steps'], result['answered']), (['general', 'news', 'fallback'], ['bing news']))
        self.assertEqual(len(result['results']), 2)

    async def test_no_request_is_sent_once_the_budget_is_spent(self):
        clock = Clock()

        async def slow(params):
            clock.advance(BUDGET_SECONDS)       # the first request used all of the time
            return page(refused=REFUSALS)
        ask = Asker(general=slow, news=NEWS, fallback=WEB)
        with self.assertRaises(Blocked) as raised:
            await search('storm warning', ask, health=Health(), config=CONFIG, clock=clock)
        self.assertEqual((ask.kinds, list(raised.exception.refused)), (['general'], REFUSED))


class EngineHealth(unittest.TestCase):
    def test_a_refusal_ends_after_the_penalty(self):
        clock = Clock()
        health = Health(clock)
        health.note(page(refused=REFUSALS))
        self.assertTrue(health.refused('brave'))
        self.assertFalse(health.refused('bing'))
        clock.advance(PENALTY_SECONDS - 1)
        self.assertTrue(health.refused('brave'))
        clock.advance(1)
        self.assertFalse(health.refused('brave'))
        self.assertEqual(health.snapshot()['refused'], {})

    def test_unresponsive_engines_are_read_as_pairs_or_plain_names(self):
        health = Health(Clock())
        health.note({'unresponsive_engines': ['brave', ['google', 'Suspended'], ('qwant',), 5, [], None, ' ']})
        self.assertEqual(sorted(health.snapshot()['refused']), ['brave', 'google', 'qwant'])
        for junk in (None, 'text', [], {'unresponsive_engines': 'brave', 'results': 'none'}, {'results': [None, {'engines': 'bing'}]}):
            health.note(junk)
        self.assertEqual((sorted(health.snapshot()['refused']), health.answered()), (['brave', 'google', 'qwant'], []))

    def test_an_engine_that_answers_after_refusing_is_no_longer_refused(self):
        clock = Clock()
        health = Health(clock)
        health.note(page(refused=[['bing', 'CAPTCHA']]))
        clock.advance(5)
        health.note(page(hit('https://example.org/a', 'bing')))
        self.assertFalse(health.refused('bing'))
        self.assertEqual(health.answered(), ['bing'])

    def test_answering_engines_are_listed_most_recent_first_and_a_refusal_hides_one_for_the_penalty(self):
        clock = Clock()
        health = Health(clock)
        health.note(page(hit('https://example.org/a', 'bing')))
        clock.advance(5)
        health.note(page({'title': 't', 'url': 'https://example.org/b', 'engines': ['mojeek', 'yahoo'], 'engine': 'mojeek'}))
        self.assertEqual(health.answered(), ['mojeek', 'yahoo', 'bing'])
        clock.advance(5)
        health.note(page(refused=['mojeek']))
        self.assertEqual(health.answered(), ['yahoo', 'bing'])
        clock.advance(PENALTY_SECONDS)
        self.assertEqual(health.answered(), ['mojeek', 'yahoo', 'bing'])

    def test_the_snapshot_says_who_answered_who_is_refused_for_how_long_and_its_age(self):
        clock = Clock()
        health = Health(clock)
        self.assertEqual(health.snapshot(), {'answered': [], 'refused': {}, 'age': None})
        health.note(page(hit('https://example.org/a', 'bing'), refused=[['brave', 'Suspended'], ['google', 'access denied']]))
        clock.advance(10)
        self.assertEqual(health.snapshot(), {'answered': ['bing'], 'refused': {'brave': PENALTY_SECONDS - 10, 'google': PENALTY_SECONDS - 10}, 'age': 10})


class Fallback(unittest.TestCase):
    def test_only_disabled_full_web_engines_from_the_config_in_its_order(self):
        self.assertEqual(FALLBACK_ENGINES, 4)
        self.assertEqual(fallback_engines(CONFIG, Health()), ['bing', 'mojeek', 'yahoo', 'qwant'])
        self.assertEqual(fallback_engines(CONFIG, Health(), limit=2), ['bing', 'mojeek'])
        self.assertEqual(fallback_engines(CONFIG, Health(), limit=99), ['bing', 'mojeek', 'yahoo', 'qwant', 'presearch'])
        self.assertEqual(fallback_engines(CONFIG, Health(), limit=0), [])

    def test_engines_seen_answering_come_first_and_refused_ones_are_left_out(self):
        clock = Clock()
        health = Health(clock)
        # `ghost` and `google` answered too, but one is not in the config and the other is asked by default.
        health.note(page(hit('https://example.org/a', 'yahoo'), hit('https://example.org/b', 'ghost'), hit('https://example.org/c', 'google')))
        clock.advance(1)
        health.note(page(hit('https://example.org/d', 'presearch'), refused=[['bing', 'CAPTCHA']]))
        names = fallback_engines(CONFIG, health)
        self.assertEqual(names, ['presearch', 'yahoo', 'mojeek', 'qwant'])
        self.assertTrue(set(names) <= IN_CONFIG)
        clock.advance(PENALTY_SECONDS)
        self.assertEqual(fallback_engines(CONFIG, health), ['presearch', 'yahoo', 'bing', 'mojeek'])

    def test_a_missing_or_malformed_config_gives_no_engine(self):
        for config in (None, {}, [], 'bing', {'engines': None}, {'engines': 'bing'}, {'engines': {'bing': {}}},
                       {'engines': [None, 'bing', {'name': 5, 'enabled': False, 'categories': ['general', 'web']},
                                    {'name': 'a', 'categories': ['general', 'web']}, {'name': 'b', 'enabled': False, 'categories': 'general web'},
                                    {'name': 'c,d', 'enabled': False, 'categories': ['general', 'web']},
                                    {'enabled': False, 'categories': ['general', 'web']}]}):
            self.assertEqual(fallback_engines(config, Health()), [], config)


class Merge(unittest.TestCase):
    def test_answers_and_info_boxes_are_joined_once_each_and_results_once_per_address(self):
        first = page(hit('https://example.org/a', 'bing'), {'title': 'no address'}, 'junk',
                     answers=['42', {'answer': 'Forty  two'}], infoboxes=[{'infobox': 'Lisbon', 'id': 'https://w.example/Lisbon', 'content': 'x'}])
        second = page(hit('HTTPS://EXAMPLE.ORG/a/#part', 'mojeek'), hit('https://example.org/b', 'mojeek', date='2026-11-16'), {'title': 'also none'},
                      answers=[{'answer': 'forty two'}, '42', ''],
                      infoboxes=[{'infobox': 'Lisbon', 'id': 'https://w.example/Lisbon', 'content': 'y'}, {'infobox': 'Porto'}])
        before = copy.deepcopy((first, second))
        merged = merge([first, None, 'text', second])
        self.assertEqual(sorted(merged), ['answers', 'infoboxes', 'results'])
        self.assertEqual(merged['answers'], ['42', {'answer': 'Forty  two'}])
        self.assertEqual([box['infobox'] for box in merged['infoboxes']], ['Lisbon', 'Porto'])
        self.assertEqual([item.get('url') for item in merged['results']], ['https://example.org/a', None, 'https://example.org/b', None])
        self.assertEqual(merged['results'][2]['content'], '(2026-11-16) snippet')
        merged['results'][0]['engines'].append('changed')
        self.assertEqual((first, second), before)
        self.assertEqual(merge([]), {'answers': [], 'infoboxes': [], 'results': []})
        self.assertEqual(merge(None), {'answers': [], 'infoboxes': [], 'results': []})

    def test_only_a_real_date_is_put_in_front_and_only_once(self):
        dated = merge([page(hit('https://example.org/a', 'bing', date='2026-11-17T08:30:00'), hit('https://example.org/b', 'bing', date='yesterday'),
                            hit('https://example.org/c', 'bing', content='', date='2026-11-17'))])
        self.assertEqual([item['content'] for item in dated['results']], ['(2026-11-17) snippet', 'snippet', '(2026-11-17)'])
        self.assertEqual([item['content'] for item in merge([dated])['results']], ['(2026-11-17) snippet', 'snippet', '(2026-11-17)'])


class Cache(unittest.IsolatedAsyncioTestCase):
    async def test_an_identical_search_within_the_cache_time_sends_nothing(self):
        clock, cache, health = Clock(), {}, Health()
        ask = Asker(general=page(hit('https://example.org/hours', 'google')))
        first = await search('Library hours', ask, health=health, cache=cache, clock=clock)
        clock.advance(CACHE_SECONDS - 1)
        second = await search('  library   HOURS ', ask, health=health, cache=cache, clock=clock)
        self.assertEqual(len(ask.calls), 1)
        self.assertEqual((first['cached'], second['cached']), (False, True))
        self.assertEqual(second['results'], first['results'])
        # What a caller does to its answer does not reach the remembered one.
        second['results'].clear()
        third = await search('library hours', ask, health=health, cache=cache, clock=clock)
        self.assertEqual((len(ask.calls), third['cached'], len(third['results'])), (1, True, 1))
        # Another recency is another search.
        await search('library hours', ask, health=health, cache=cache, clock=clock, recency='day')
        self.assertEqual(ask.kinds, ['general', 'general', 'news'])

    async def test_after_the_cache_time_the_search_is_sent_again_and_old_entries_are_dropped(self):
        clock, cache, health = Clock(), {}, Health()
        ask = Asker(general=page(hit('https://example.org/hours', 'google')))
        await search('library hours', ask, health=health, cache=cache, clock=clock)
        await search('pool hours', ask, health=health, cache=cache, clock=clock)
        self.assertEqual(len(cache), 2)
        clock.advance(CACHE_SECONDS + 1)
        again = await search('library hours', ask, health=health, cache=cache, clock=clock)
        self.assertEqual((len(ask.calls), again['cached'], len(cache)), (3, False, 1))

    async def test_a_blocked_search_is_not_remembered(self):
        clock, cache, health = Clock(), {}, Health()
        ask = Asker(general=page(refused=REFUSALS), news=page(refused=['bing news']))
        with self.assertRaises(Blocked):
            await search('storm warning', ask, health=health, cache=cache, clock=clock)
        self.assertEqual(cache, {})
        ask.answers['news'] = NEWS
        result = await search('storm warning', ask, health=health, cache=cache, clock=clock)
        self.assertEqual((ask.kinds, result['cached'], len(result['results'])), (['general', 'news', 'general', 'news'], False, 2))

    async def test_two_identical_searches_at_the_same_moment_share_one_set_of_requests(self):
        clock, cache, health, release = Clock(), {}, Health(), asyncio.Event()

        async def held(params):
            await release.wait()
            return page(refused=REFUSALS)
        ask = Asker(general=held, news=NEWS, fallback=WEB)
        both = [asyncio.ensure_future(search(query, ask, health=health, config=CONFIG, cache=cache, clock=clock))
                for query in ('storm warning', 'Storm  Warning')]
        await turns()
        self.assertEqual((ask.kinds, len(cache)), (['general'], 1))
        release.set()
        first, second = await asyncio.gather(*both)
        self.assertEqual(ask.kinds, ['general', 'news', 'fallback'])
        self.assertEqual((first['cached'], second['cached']), (False, True))
        self.assertEqual(first['results'], second['results'])
        self.assertIsNot(first['results'], second['results'])

    async def test_a_cancelled_caller_does_not_take_the_shared_search_with_it(self):
        clock, cache, health, release = Clock(), {}, Health(), asyncio.Event()

        async def held(params):
            await release.wait()
            return page(hit('https://example.org/hours', 'google'))
        ask = Asker(general=held)
        first = asyncio.ensure_future(search('library hours', ask, health=health, cache=cache, clock=clock))
        await turns()
        second = asyncio.ensure_future(search('library hours', ask, health=health, cache=cache, clock=clock))
        await turns()
        first.cancel()
        await turns()
        release.set()
        result = await second
        self.assertTrue(first.cancelled())
        self.assertEqual((len(ask.calls), result['cached'], len(result['results'])), (1, True, 1))


if __name__ == '__main__':
    unittest.main()
