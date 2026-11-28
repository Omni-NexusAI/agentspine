"""Who writes the plan while the two of them plan (convo_modes), and whether it may: the same privacy rule as the summaries (convo_summarizer).

Writing a plan well wants a strong model, and the conversation model is chosen for speed and for hearing audio; on the user's own model
server it also serves one reply at a time, so a plan written there must wait for quiet moments and the final write-up would hold the
conversation. Agent Zero's main chat model is usually the stronger one, and it runs beside the conversation on its own server.

`plan_writer`:
- "auto" (default): the main chat model when it may receive the planning conversation (it runs on the user's own machines, or
  `main_plan_consent` allows it) and is not the very model the conversation already uses; otherwise the conversation model.
- "main": the main chat model under the same privacy rule; when it may not, the conversation model writes, and the card says why.
- "conversation": always the conversation model, from the conversation's own cached start.

Convo cannot tell how capable a model is; it can tell where a model runs and whether it is the same model. A strong conversation model is
chosen by the user ("conversation"). Nothing here contacts a model.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from .convo_summarizer import utility_local

WRITERS = ('auto', 'main', 'conversation')


def _label(model):
    name, provider = str(model.get('name') or '').strip(), str(model.get('provider') or '').strip()
    return (provider + ' / ' + name) if provider and name else (name or provider or 'not set')


def _same(main, role):
    """Whether the main chat model is the conversation model itself (the same model name at the same address)."""
    def where(url):
        try:
            parts = urlsplit(url if '://' in url else 'http://' + url)
            return (parts.hostname or '', parts.port)
        except ValueError:
            return ('', None)
    name = str(main.get('name') or '').strip().casefold()
    base = str(main.get('base') or main.get('api_base') or '').strip()
    return bool(name) and name == str(role.get('model') or '').strip().casefold() and bool(base) and where(base) == where(str(role.get('url') or ''))


def choose(config, main=None):
    """{writer: 'main' | 'conversation', model, local, reason, detail}: who writes this conversation's plan. `main`: Agent Zero's main chat
    model as Host.main_model returns it ({provider, name, api_base, base}), or {} / None when it could not be read."""
    config = config if isinstance(config, dict) else {}
    wanted = config.get('plan_writer') if config.get('plan_writer') in WRITERS else 'auto'
    role = (config.get('roles') or {}).get('conversation') or {}
    conversation = {'writer': 'conversation', 'model': str(role.get('model') or '').strip() or 'not set', 'local': None, 'reason': ''}
    if wanted == 'conversation':
        return {**conversation, 'detail': 'The conversation model writes the plan, between replies.'}
    main = main if isinstance(main, dict) else {}
    label = _label(main)
    if not (main.get('name') or main.get('provider')):
        return {**conversation, 'reason': 'main_unknown',
                'detail': "Agent Zero's main chat model could not be read, so the conversation model writes the plan, between replies."}
    if _same(main, role):
        return {**conversation, 'reason': 'same_model',
                'detail': 'The main chat model is the conversation model itself, so it writes the plan from the conversation it already holds.'}
    local = utility_local(main)
    if local or config.get('main_plan_consent') is True:
        return {'writer': 'main', 'model': label, 'local': local, 'reason': '',
                'detail': "Agent Zero's main chat model (%s) writes the plan beside the conversation%s." % (
                    label, '' if local else '; it is outside your network and receives the planning conversation, as you allowed')}
    return {**conversation, 'reason': 'main_not_consented',
            'detail': ("Agent Zero's main chat model (%s) is outside your network: it would receive the planning conversation. The "
                       "conversation model writes the plan until you allow that." % label)}


def readiness_rows(config, main=None):
    """The Conversation card's "Plans" row: who writes them (reads settings and Agent Zero's model choice; contacts nothing)."""
    if main is None:
        try:
            from .convo_host import Host
            main = Host().main_model()
        except Exception:
            main = {}
    found = choose(config, main)
    asked = config.get('plan_writer') == 'main' and found['writer'] != 'main'
    return [{'id': 'plan_writer', 'label': 'Plans', 'state': 'warning' if asked else 'ready', 'detail': found['detail']}]
