"""A model on a router that loads models on demand (llama.cpp router mode and compatible routers: GET /models lists each model with
`status.value` loaded | loading | unloaded, POST /models/load {"model"} loads one), made ready before Convo needs it.

A router loads a model when a request names it, so nothing would ever be "missing"; but the request then waits silently for the
load, longer than a turn allows. So a voice session first asks each of its servers whether the chosen model is loaded and, when it
is not, asks the router to load it and waits, telling the user what it is waiting for. A server that does not report a status (an
ordinary OpenAI-compatible server, a cloud provider) is not waited for. Nothing is unloaded, and no other model is touched: a
router that holds one model at a time decides by itself which one it swaps out.

Besides routers, three more kinds of server tell whether a model is loaded and take a load signal: a Voice Studio speech server
(audio.cpp: /backend/models and its switch route), Ollama (/api/ps) and LM Studio (/api/v0/models); and a llama.cpp server that
answers 503 is still loading its one model. Only servers on this machine or the user's network are looked at: a cloud provider has
nothing to load. A server nothing listens at is 'unreachable' and is named, never waited for.
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlsplit

from . import convo_net

LOAD_WAIT = 180.0      # seconds a session start waits for a model to load
POLL = 1.0
ANSWER_WAIT = 4.0      # seconds a server that does not answer is tried again before the start is refused
LIMIT = 512 * 1024


def _root(url):
    """The router's own address for its management routes (/models/load): the API base without a trailing /v1."""
    url = url.rstrip('/')
    return url[:-3] if url.endswith('/v1') else url


def _where(url):
    return urlsplit(url).netloc or url


async def _get(url, headers):
    async with convo_net.client(8) as client:
        async with client.stream('GET', url, headers=headers) as response:
            if response.status_code != 200:
                return response.status_code, None
            body = b''
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > LIMIT:
                    return response.status_code, None
    try:
        return 200, json.loads(body)
    except ValueError:
        return 200, None


def model_state(listing, model):
    """'loaded' | 'loading' | 'unloaded' for `model` as a router reports it, or '' when it reports no status for it."""
    items = listing.get('data') if isinstance(listing, dict) else None
    for item in items if isinstance(items, list) else ():
        if isinstance(item, dict) and model in (item.get('id'), item.get('model')):
            status = item.get('status')
            value = status.get('value') if isinstance(status, dict) else status if isinstance(status, str) else ''
            return value if value in ('loaded', 'loading', 'unloaded') else ''
    return ''


def _local(url):
    from .convo_contract import local_host
    return local_host(urlsplit(url).hostname or '')


def studio_state(listing, model):
    """The speech model's state on a Voice Studio server (audio.cpp's /backend/models), or '' for anything else."""
    if not isinstance(listing, dict) or not isinstance(listing.get('loaded_models'), list) or not isinstance(listing.get('available'), list):
        return ''
    if listing.get('state') in ('loading', 'warming', 'switching'):
        return 'loading'
    if model in listing['loaded_models']:
        return 'loaded'
    return 'unloaded' if model in listing['available'] else ''


async def state(url, headers, model, kind=''):
    """The model's state on the server at `url` (an API base): 'loaded', 'loading', 'unloaded', 'unreachable' (nothing answers
    there) or '' when unknown. `kind`: 'speech', or the provider's id. Never raises."""
    if not _local(url):
        return ''
    base = url.rstrip('/')
    try:
        if kind == 'speech':
            code, listing = await _get(base + '/backend/models', headers)
            found = studio_state(listing, model) if code == 200 else ''
            if found:
                return found
        code, listing = await _get(base + '/models', headers)
    except Exception as error:
        return 'unreachable' if convo_net.failure_code(error) in ('refused', 'unknown_host', 'unreachable') else ''
    if code == 503:
        return 'loading'                    # llama.cpp's server while its one model loads
    if code != 200:
        return ''
    found = model_state(listing, model)
    if found:
        return found
    try:
        if kind == 'ollama':
            code, running = await _get(_root(base) + '/api/ps', headers)
            names = [str(item.get('name') or item.get('model') or '') for item in (running or {}).get('models') or () if isinstance(item, dict)]
            if code == 200:
                return 'loaded' if any(name == model or name.split(':')[0] == model for name in names) else 'unloaded'
        if kind == 'lm_studio':
            code, listing = await _get(_root(base) + '/api/v0/models', headers)
            for item in (listing or {}).get('data') or () if code == 200 else ():
                if isinstance(item, dict) and item.get('id') == model:
                    return {'loaded': 'loaded', 'loading': 'loading', 'not-loaded': 'unloaded'}.get(item.get('state'), '')
    except Exception:
        pass
    return ''


async def load(url, headers, model, kind=''):
    """Ask the server to load `model`, with the request its kind takes. A server that loads on first use only may not know the
    route: waiting for its state still tells. Never raises."""
    base = url.rstrip('/')
    if kind == 'speech':
        target, body, seconds = base + '/backend/models/switch', {'model_key': model}, LOAD_WAIT
    elif kind == 'ollama':
        target, body, seconds = _root(base) + '/api/generate', {'model': model}, LOAD_WAIT      # no prompt: loads only
    elif kind == 'lm_studio':
        target, body, seconds = _root(base) + '/api/v1/models/load', {'model': model}, LOAD_WAIT
    else:
        target, body, seconds = _root(base) + '/models/load', {'model': model}, 15
    try:
        async with convo_net.client(seconds) as client:
            await client.post(target, json=body, headers=headers)
    except Exception:
        pass


_loads = set()          # load signals still being answered (a server may hold the request until the model is there)


async def _signal(url, headers, model, kind):
    if kind in ('speech', 'ollama', 'lm_studio'):
        task = asyncio.ensure_future(load(url, headers, model, kind))
        _loads.add(task)
        task.add_done_callback(_loads.discard)
    else:
        await load(url, headers, model, kind)


def down(label, url):
    """The sentence for a server nothing answers at."""
    return ('The ' + label + '’s server at ' + _where(url) + ' is not answering. Start it, or choose another one in the Convo settings.')


async def explain(label, url, headers, model, kind=''):
    """Why a step is taking long, as (notice kind, sentence): its model is loading (asked to load when it was not), its server does
    not answer, or it is only slow ('slow')."""
    current = await state(url, headers, model, kind)
    if current == 'unreachable':
        return 'unavailable', down(label, url)
    if current == 'unloaded':
        await _signal(url, headers, model, kind)
    if current in ('unloaded', 'loading'):
        return 'loading', 'Loading ' + model + ' for the ' + label + ' on ' + _where(url) + '…'
    return 'slow', 'Waiting for the ' + label + (' (' + model + ')' if model else '') + ' to answer…'


async def ensure_loaded(label, url, headers, model, notify, wait=LOAD_WAIT, sleep=asyncio.sleep, clock=time.monotonic, kind=''):
    """Make `model` ready on the server at `url`, telling `notify(message)` what it waits for. Returns the seconds waited (0 when
    nothing had to load). Raises ValueError, in Convo's words, when nothing answers there or the load does not finish in time."""
    if not model or not url:
        return 0.0
    started = clock()
    current = await state(url, headers, model, kind)
    while current == 'unreachable' and clock() - started < ANSWER_WAIT:
        await notify('Waiting for the ' + label + '’s server at ' + _where(url) + ' to answer…')
        await sleep(POLL)
        current = await state(url, headers, model, kind)
    if current == 'unreachable':
        raise ValueError(down(label, url))
    if current in ('', 'loaded'):
        return 0.0
    where = _where(url)
    await notify('Loading ' + model + ' for the ' + label + ' on ' + where + '…')
    if current == 'unloaded':
        await _signal(url, headers, model, kind)
    said = started
    while clock() - started < wait:
        await sleep(POLL)
        current = await state(url, headers, model, kind)
        if current in ('', 'loaded'):
            return clock() - started
        if current == 'unreachable':
            continue                        # a server may restart its engine while it loads
        if clock() - said >= 5:
            said = clock()
            await notify('Loading ' + model + ' for the ' + label + ' on ' + where + '… ' + str(int(clock() - started)) + ' s')
    raise ValueError('The ' + label + ' ' + model + ' did not finish loading on ' + where + ' within ' + str(int(wait))
                     + ' s. Check the router, then start again.')


def same_router(first, second):
    """Whether two role addresses are the same server (scheme, host and port)."""
    try:
        return bool(first and second) and convo_net.origin(first) == convo_net.origin(second)
    except ValueError:
        return False
