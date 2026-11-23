"""Web search that still answers when the usual engines refuse this computer.

Convo's web search asks Agent Zero's SearXNG. Its default engines often refuse a home or server address (rate limit, captcha,
access denied); the search then comes back empty with those engines listed under `unresponsive_engines`. This module is the
ladder that gets an answer anyway. It sends nothing itself: the caller passes `ask(params)`, which makes one SearXNG request and
returns its JSON, so there is no network client, no Agent Zero import and nothing to configure here.

1. The ordinary request (`q`). When it has results or a direct answer, the search ends there.
2. Only when it has neither (an info box alone is an encyclopedia entry, not an answer to the search), two requests at the same
   time: the news category (other engines, which usually still answer) and
   one request that names a few full web search engines SearXNG has loaded but does not ask by default. Their names are read
   from SearXNG's own `/config` (`fallback_engines`), never from a list in this file, so the ladder follows what is installed.
With a `recency` every request carries SearXNG's `time_range`, and the news request goes out together with the first one.
The sources are woven together (`weave`): the first results come from each of them in turn, web before news, or news first when
the search asked for recent results, so the few results the model is shown are never all from one source.

One search sends at most MAX_REQUESTS requests and all of them together take at most BUDGET_SECONDS; a request that fails, runs
out of time or does not answer with a JSON object counts as no answer. `Health` remembers, in memory only, which engines answered
and which refused, so a fallback engine that refused is not named again for PENALTY_SECONDS. An identical search within
CACHE_SECONDS is answered from the caller's `cache`, and identical searches made at the same moment share one set of requests.
`Blocked` means the search could not be done (nothing came back and no engine answered); a search that worked and found nothing
returns an empty result instead."""
from __future__ import annotations

import asyncio
import datetime
import re
import time

PENALTY_SECONDS = 900      # an engine that refused is skipped this long
CACHE_SECONDS = 90         # an identical search within this time is answered from memory
BUDGET_SECONDS = 8.0       # one search, all requests together
FALLBACK_ENGINES = 4       # engines named in the one fallback request
MAX_REQUESTS = 3           # requests one search may send
RECENCY = {'day': 'day', 'week': 'week', 'month': 'month', 'year': 'year'}   # the tool's argument -> SearXNG time_range
STALE_DAYS = 365           # a news result dated longer ago than this goes after the fresher and the undated ones
WORD = re.compile(r'[^\W_]{2,}', re.U)
PARTS = ('answers', 'infoboxes', 'results')


class Blocked(Exception):
    """Nothing came back and no engine that was asked answered (refused, rate-limited, captcha, parse failure, or the request failed)."""

    def __init__(self, refused=()):
        self.refused = tuple(sorted(set(refused)))
        super().__init__('No search engine answered' + (' (refused: %s)' % ', '.join(self.refused) if self.refused else ''))


def _items(value):
    return value if isinstance(value, (list, tuple)) else []


def _names(values):
    """Engine names as SearXNG writes them: text only, each once, in order."""
    return list(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))


def _refused_in(response):
    """Engines a response lists as unresponsive, written as `[name, reason]` pairs or as plain names."""
    return _names(item[0] if isinstance(item, (list, tuple)) and item else item for item in _items(response.get('unresponsive_engines')))


def _answered_in(response):
    """Engines that have a result in a response."""
    return _names(name for item in _items(response.get('results')) if isinstance(item, dict)
                  for name in (*_items(item.get('engines')), item.get('engine')))


def _copy(value):
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    return [_copy(item) for item in value] if isinstance(value, (list, tuple)) else value


def _mark(item):
    """What makes two direct answers or two info boxes the same one: the answer's text, the box's address or title."""
    if isinstance(item, dict):
        item = item.get('answer') or item.get('id') or item.get('infobox') or item
    return ' '.join(str(item).split()).casefold()


class Health:
    """In-memory score of which engines answer; never persisted."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._answered = {}      # engine -> when it last answered
        self._refused = {}       # engine -> when it last refused, until it answers again or the penalty is over
        self._noted = None

    def note(self, response) -> None:
        """Reads one response: a listed unresponsive engine refused, an engine with a result answered (and is forgiven)."""
        if not isinstance(response, dict):
            return
        self._noted = now = self.clock()
        for name in _refused_in(response):
            self._refused[name] = now
        for name in _answered_in(response):
            self._answered[name] = now
            self._refused.pop(name, None)

    def refused(self, name) -> bool:
        at = self._refused.get(name)
        if at is not None and self.clock() - at >= PENALTY_SECONDS:
            del self._refused[name]
            return False
        return at is not None

    def answered(self) -> list:
        """Engines seen answering, most recent first; one that refused since is left out until its penalty is over."""
        return [name for name in sorted(self._answered, key=self._answered.get, reverse=True) if not self.refused(name)]

    def snapshot(self) -> dict:
        now = self.clock()
        left = {name: round(PENALTY_SECONDS - (now - at), 1) for name, at in sorted(self._refused.items()) if self.refused(name)}
        return {'answered': self.answered(), 'refused': left, 'age': None if self._noted is None else round(now - self._noted, 1)}


def fallback_engines(config, health, limit=FALLBACK_ENGINES) -> list:
    """The engines to name when the default ones gave nothing, read from SearXNG's `/config`: loaded, not asked by default
    (`enabled: false`; the enabled ones were just asked), tagged both `general` and `web` (how SearXNG marks a full web search
    engine), able to follow the language of the search (`language_support`: a regional or niche index without it answered an
    English question with unrelated pages, measured 2026-11-18) and not refused at the moment. Engines seen answering come
    first, the rest in SearXNG's order. [] without a config."""
    names = []
    for engine in _items(config.get('engines') if isinstance(config, dict) else None):
        if not isinstance(engine, dict) or engine.get('enabled') is not False:
            continue
        name, categories = engine.get('name'), _items(engine.get('categories'))
        # A comma separates the names in a request, so a name that holds one cannot be asked for.
        usable = isinstance(name, str) and name.strip() and ',' not in name and name not in names
        if (usable and 'general' in categories and 'web' in categories and engine.get('language_support') is True
                and not health.refused(name)):
            names.append(name)
    proven = [name for name in health.answered() if name in names]
    return (proven + [name for name in names if name not in proven])[:max(0, int(limit))]


def merge(responses) -> dict:
    """Several responses as one, in the order given: direct answers and info boxes once each, results once per address (letter
    case, a trailing slash and a fragment do not make another address), the first one kept. A dated result says its date at the
    start of its snippet. Everything is copied; the responses stay as they are."""
    merged, seen = {part: [] for part in PARTS}, set()
    for response in responses or ():
        if not isinstance(response, dict):
            continue
        for part in ('answers', 'infoboxes'):
            for item in _items(response.get(part)):
                if item and (part, _mark(item)) not in seen:
                    seen.add((part, _mark(item)))
                    merged[part].append(_copy(item))
        for item in _items(response.get('results')):
            if not isinstance(item, dict):
                continue
            address = str(item.get('url') or '').split('#', 1)[0].strip().rstrip('/').casefold()
            if ('results', address) in seen:
                continue
            if address:
                seen.add(('results', address))
            item, date = _copy(item), str(item.get('publishedDate') or '')[:10]
            if len(date) == 10 and date[4] == date[7] == '-' and date.replace('-', '').isdigit():
                content = str(item.get('content') or '')
                if not content.startswith('(%s)' % date):
                    item['content'] = ('(%s) %s' % (date, content)).strip()
            merged['results'].append(item)
    return merged


def relevant_first(results, query) -> list:
    """Results that share too little with the search moved to the end; the order is otherwise kept. Some engines answer with
    their latest items whatever was asked, and one common word ("today", "latest") is in any of them: a result stays ahead when
    it has two of the search's words, or its only one. Only the longer words and those with a digit count (a short word is in
    every text); a search without any such word (another writing system, a single short word) is left as it is."""
    words = {word.casefold() for word in WORD.findall(str(query)) if len(word) >= 4 or any(letter.isdigit() for letter in word)}
    need = min(2, len(words))
    if not need:
        return list(results)

    def shares(item):
        text = ' '.join(str(item.get(key) or '') for key in ('title', 'content', 'url')).casefold()
        return sum(word in text for word in words) >= need
    return [item for item in results if shares(item)] + [item for item in results if not shares(item)]


def fresh_first(results, today) -> list:
    """News results with those dated more than STALE_DAYS before `today` moved to the end; the order is otherwise kept. The news
    category includes archives, and a years-old article is rarely what a search wants first. Undated results stay where they are."""
    def stale(item):
        try:
            return (today - datetime.date.fromisoformat(str(item.get('publishedDate') or '')[:10])).days > STALE_DAYS
        except (ValueError, TypeError):
            return False
    return [item for item in results if not stale(item)] + [item for item in results if stale(item)]


def weave(lists) -> list:
    """Several result lists as one, taking a result from each in turn and keeping every list's own order."""
    lists, woven = [list(items) for items in lists if items], []
    while lists:
        for items in lists:
            woven.append(items.pop(0))
        lists = [items for items in lists if items]
    return woven


async def _ladder(query, ask, health, config, recency, clock, budget, today):
    started = clock()
    deadline, steps = started + max(0.0, float(budget)), []

    async def within(call, *args):
        """`call(*args)`, awaited for no longer than what is left of the budget. None when it fails or there is no time left."""
        left = deadline - clock()
        if left <= 0:
            return None
        try:
            value = call(*args)
            return await asyncio.wait_for(value, left) if hasattr(value, '__await__') else value
        except Exception:
            return None

    async def one(step, **extra):
        if len(steps) >= MAX_REQUESTS or deadline - clock() <= 0:
            return None
        steps.append(step)
        response = await within(ask, {'q': query, **extra, **({'time_range': recency} if recency else {})})
        if not isinstance(response, dict):
            return None
        health.note(response)
        return response

    async def fallback():
        # The config is read only here, when the ordinary request found nothing, and next to the news request, not before it.
        names = fallback_engines(await within(config) if callable(config) else config, health)
        return await one('fallback', engines=','.join(names)) if names else None

    def news():
        return one('news', categories='news')

    got = dict(zip(('general', 'news'), await asyncio.gather(one('general'), *([news()] if recency else []))))
    first = merge([got['general']])
    if not (first['results'] or first['answers']):
        late = ('fallback',) if recency else ('news', 'fallback')
        got.update(zip(late, await asyncio.gather(*([] if recency else [news()]), fallback())))
    # Web before news, or news first when recent results were asked for; a source's own order is kept within the weave.
    order = ('news', 'general', 'fallback') if recency else ('general', 'fallback', 'news')
    responses = [got[step] for step in order if got.get(step) is not None]
    result = merge(responses)
    lists = [fresh_first(merge([got[step]])['results'], today) if step == 'news' else merge([got[step]])['results']
             for step in order if got.get(step) is not None]
    # Woven first, then what shares too little with the search goes to the end of the whole list, so it is shown last or not at all.
    result['results'] = merge([{'results': relevant_first(weave(lists), query)}])['results']
    answered = sorted({name for response in responses for name in _answered_in(response)})
    refused = sorted({name for response in responses for name in _refused_in(response)})
    if not any(result.values()) and not answered and (refused or not responses):
        raise Blocked(refused)
    return {**result, 'steps': steps, 'asked': len(steps), 'answered': answered, 'refused': refused,
            'ms': int((clock() - started) * 1000), 'cached': False}


def _settle(cache, key, entry, clock):
    """A search that ended is kept from that moment; one that was blocked, failed or was cancelled is forgotten."""
    flight = entry[1]
    if not flight.cancelled() and flight.exception() is None:
        entry[0] = clock()
    elif cache.get(key) is entry:
        del cache[key]


async def search(query, ask, *, health, config=None, cache=None, recency='', clock=time.monotonic, budget=BUDGET_SECONDS,
                 today=None) -> dict:
    """One web search through the ladder (see the module text). `ask(params)` sends one SearXNG request and returns its JSON;
    `config` is SearXNG's `/config` or a callable that returns it. Returns `merge`'s three lists plus `steps`, `asked`,
    `answered`, `refused`, `ms` and `cached`; raises `Blocked` when nothing came back and no engine answered."""
    query = ' '.join(str(query or '').split())
    if not query:
        raise ValueError('A search query is required')
    recency = RECENCY.get(str(recency or '').strip().lower(), '')
    today = today or datetime.date.today()
    if cache is None:
        return await _ladder(query, ask, health, config, recency, clock, budget, today)
    key, now = (query.casefold(), recency), clock()
    # An entry is [when the search ended (None while it runs), its task].
    for old in [name for name, kept in cache.items() if kept[0] is not None and now - kept[0] > CACHE_SECONDS]:
        del cache[old]
    entry = cache.get(key)
    shared = entry is not None
    if not shared:
        cache[key] = entry = [None, asyncio.ensure_future(_ladder(query, ask, health, config, recency, clock, budget, today))]
        entry[1].add_done_callback(lambda flight: _settle(cache, key, entry, clock))
    # The task belongs to the cache, not to one caller: a caller that is cancelled does not take the others' search with it.
    result = _copy(await asyncio.shield(entry[1]))
    if shared:
        result.update(cached=True, ms=int((clock() - now) * 1000))
    return result
