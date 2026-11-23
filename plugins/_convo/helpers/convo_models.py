"""A model on a router that loads models on demand (llama.cpp router mode and compatible routers: GET /models lists each model with
`status.value` loaded | loading | unloaded, POST /models/load {"model"} loads one), made ready before Convo needs it.

A router loads a model when a request names it, so nothing would ever be "missing"; but the request then waits silently for the
load, longer than a turn allows. So a voice session first asks each of its servers whether the chosen model is loaded and, when it
is not, asks the router to load it and waits, telling the user what it is waiting for. A server that does not report a status (an
ordinary OpenAI-compatible server, a cloud provider) is not waited for. Nothing is unloaded, and no other model is touched: a
router that holds one model at a time decides by itself which one it swaps out.
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlsplit

from . import convo_net

LOAD_WAIT = 180.0      # seconds a session start waits for a model to load
POLL = 1.0
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


async def state(url, headers, model):
    """The model's state on the server at `url` (an API base), or '' when unknown. Never raises."""
    try:
        code, listing = await _get(url.rstrip('/') + '/models', headers)
    except Exception:
        return ''
    return model_state(listing, model) if code == 200 else ''


async def ensure_loaded(label, url, headers, model, notify, wait=LOAD_WAIT, sleep=asyncio.sleep, clock=time.monotonic):
    """Make `model` ready on the router at `url`, telling `notify(message)` what it waits for. Returns the seconds waited (0 when
    nothing had to load). Raises ValueError, in Convo's words, when the router does not finish loading in time."""
    if not model or not url:
        return 0.0
    current = await state(url, headers, model)
    if current in ('', 'loaded'):
        return 0.0
    started = clock()
    where = _where(url)
    await notify('Loading ' + model + ' for the ' + label + ' on ' + where + '…')
    if current == 'unloaded':
        try:
            async with convo_net.client(15) as client:
                await client.post(_root(url) + '/models/load', json={'model': model}, headers=headers)
        except Exception:
            pass                            # a router that loads on first use only: the waiting below still tells
    said = started
    while clock() - started < wait:
        await sleep(POLL)
        current = await state(url, headers, model)
        if current in ('', 'loaded'):
            return clock() - started
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
