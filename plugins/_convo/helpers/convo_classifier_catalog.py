"""The classifier: which providers can decide whether speech is meant for Convo, where each is reached, and Check server.

The classifier is one Convo setting (`roles.classifier`). It is either a decision model, which answers the System One protocol
(POST <server root>/v1/systemone, reached through the decision plugin convo_d1_decider), or a chat model, which answers the
legacy JSON verdict (the gateway's /policy route on an OpenAI-compatible chat base). The card stores `provider`, `kind`
(auto|decision|chat, auto = the provider's first kind), `api_base`, `key_source` and `context_window`, plus the toggle "Use
System One's decider" (`decider.source == 'system_one'`). `normalize` derives from them, on every settings write, exactly the
keys the runtime already reads (`system_one`, `decider.*`, `roles.classifier.url`). This module is the only copy of the
catalog: the browser receives it in setup_read (`classifier_catalog`) and keeps none of its own. Everything here is pure except
`check`, which runs only on the user's Check server click and never returns a key.
"""
from __future__ import annotations

import copy
import ipaddress
import json
from collections.abc import Mapping
from urllib.parse import urlsplit

from . import convo_model_catalog
from .convo_contract import CLASSIFIER_KINDS, DECIDER_HOSTS, PROVIDER_ID, AddressRefused, classifier_url, local_host

BACKEND = 'd1_llama_cpp'                                   # the decision plugin's backend: a decision classifier selects it
DEFAULT_DECIDER_URL = 'http://host.docker.internal:9931'   # the decision plugin's default server root (`decider.url` '' = this)
ROUTE = '/v1/systemone'
UNVERIFIED = 'Check server confirms whether it answers the System One decision route'
CHAT_NOTE = ('Answers the JSON verdict through its OpenAI-compatible chat route. For an audio conversation the model must accept '
             'audio; that is not verified until a test.')


def _local(label, base, host_key, verified=False, note=UNVERIFIED):
    return {'label': label, 'group': 'decision_local', 'kinds': ['decision', 'chat'], 'base': base, 'host_key': host_key,
            'verified': verified, 'text_only': False, 'note': note}


def _chat(name):
    spec = convo_model_catalog.PROVIDERS[name]           # the same bases as the conversation model
    return {'label': spec['label'], 'group': 'chat', 'kinds': ['chat'], 'base': spec['base'], 'host_key': name,
            'verified': False, 'text_only': False, 'note': CHAT_NOTE}


# `base`: the OpenAI-style base including /v1 ('' = an API base is required; Jev's is its server root). `host_key`: the Agent
# Zero provider whose stored key it uses ('' = Convo's own key or the environment reference).
PROVIDERS = {
    'llama_cpp': _local('llama.cpp', 'http://host.docker.internal:8080/v1', 'llama_cpp', True, 'llama-server serves d1 models on /v1/systemone'),
    'lm_studio': _local('LM Studio', 'http://host.docker.internal:1234/v1', 'lm_studio'),
    'unsloth_studio': _local('Unsloth Studio', '', ''),
    'ollama': _local('Ollama', 'http://host.docker.internal:11434/v1', 'ollama'),
    'vllm': _local('vLLM', 'http://host.docker.internal:8000/v1', 'vllm'),
    'omlx': _local('oMLX', 'http://host.docker.internal:8000/v1', 'omlx'),
    'jev': {'label': 'TypeSafe Jev', 'group': 'decision_cloud', 'kinds': ['decision'], 'base': 'https://api.typesafe.ai', 'host_key': '',
            'verified': False, 'text_only': True,
            'note': 'System One protocol at its server root (/v1/systemone). Text only: unusable for an audio conversation.'},
    'openai': _chat('openai'),
    'openrouter': _chat('openrouter'),
    'google': _chat('google'),
    'other': {'label': 'Other', 'group': 'other', 'kinds': ['decision', 'chat'], 'base': '', 'host_key': '', 'verified': False,
              'text_only': False, 'note': 'Any server that answers the System One decision route or an OpenAI-compatible chat '
                                          'route: enter its API base.'},
}
# Agent Zero providers that cannot classify Convo's audio, with the conversation catalog's reason.
DISABLED = {name: reason for name, reason in convo_model_catalog.DISABLED.items() if name not in PROVIDERS}
UNKNOWN_COMPATIBLE = ('A provider Convo does not know, used as a chat classifier through its OpenAI-compatible API base. Audio '
                      'support is not verified until Check server or a test.')
UNKNOWN = 'Unknown provider: Convo cannot use it as the classifier.'


def catalog():
    """The catalog the browser shows (setup_read `classifier_catalog`). Data only: no address beyond the documented defaults."""
    return copy.deepcopy({'providers': PROVIDERS, 'disabled': DISABLED, 'unknown_compatible': UNKNOWN_COMPATIBLE, 'unknown': UNKNOWN})


def _known_id(provider):
    return isinstance(provider, str) and bool(PROVIDER_ID.fullmatch(provider))


def status(provider):
    """(selectable, reason) for a provider id. '' (the legacy endpoint exactly as entered) and catalog providers are selectable;
    the conversation catalog's disabled providers are not; any other id is an unknown provider the browser offered as
    OpenAI-compatible (it decides that from the host's provider details), usable as a chat classifier with an API base."""
    if not provider or provider in PROVIDERS:
        return True, ''
    if provider in DISABLED or not _known_id(provider):
        return False, DISABLED.get(provider, UNKNOWN)
    return True, UNKNOWN_COMPATIBLE


def kinds(provider):
    """The classifier kinds a provider offers, in catalog order: [] for '' (not derived), a disabled or an invalid id."""
    if provider in PROVIDERS:
        return list(PROVIDERS[provider]['kinds'])
    return ['chat'] if provider and status(provider)[0] else []


def effective_kind(role):
    """'decision' or 'chat' for the role's provider and `kind` (auto = the provider's first kind), or '' when the provider offers
    no such kind (nothing is then derived: no address is invented)."""
    role = role or {}
    offered = kinds(role.get('provider') or '')
    kind = role.get('kind') or 'auto'
    if kind == 'auto':
        return offered[0] if offered else ''
    return kind if kind in offered else ''


def resolve_base(role):
    """The OpenAI-style base the classifier's provider is reached at: the API base override (trailing '/' removed) or the
    catalog base; '' when neither is known or the provider cannot be used. Provider '' is the legacy endpoint as entered."""
    role = role or {}
    provider = role.get('provider') or ''
    if not provider:
        return (role.get('url') or '').rstrip('/')
    if not status(provider)[0]:
        return ''
    override = (role.get('api_base') or '').strip().rstrip('/')
    base = PROVIDERS[provider]['base'] if provider in PROVIDERS else ''
    if override and base.endswith('/v1') and _path(override) in ('', '/'):
        override += '/v1'          # a bare server address, as Agent Zero lists local servers, gets the provider's OpenAI-style /v1
    return override or base


def _path(url):
    try:
        return urlsplit(url).path
    except ValueError:
        return None


def server_root(base):
    """A base as the server root the decision route is appended to: a trailing /v1 (or the route itself) removed."""
    root = (base or '').rstrip('/')
    for suffix in (ROUTE, '/v1'):
        if root.endswith(suffix):
            root = root[:-len(suffix)].rstrip('/')
    return root


def host_key(role):
    """The Agent Zero provider id whose stored key the classifier uses, or '' for Convo's own key. An unknown (offered as
    OpenAI-compatible) provider uses Agent Zero's key for that provider id."""
    provider = (role or {}).get('provider') or ''
    if provider in PROVIDERS:
        return PROVIDERS[provider]['host_key']
    return provider if provider and status(provider)[0] else ''


def _rollback_safe(url):
    """The previous version's rule for `decider.url` (plain http on this machine, no path): only such an address is mirrored
    there, so a rollback still loads the settings. Any other decision server is derived from roles.classifier at call time."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        try:
            local = host in DECIDER_HOSTS or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        parsed.port
        return parsed.scheme == 'http' and local and not (parsed.username or parsed.password or parsed.query or parsed.fragment) \
            and parsed.path in ('', '/')
    except (ValueError, TypeError, AttributeError):
        return False


def normalize(config):
    """Derive the runtime keys from the classifier card before validation, on every settings write (pure; raises only
    AddressRefused for a public decision server without consent).

    Toggle on (`decider.source` 'system_one'): System One's decider through the decision plugin (`system_one` = d1_llama_cpp).
    Toggle off with a provider: a decision model selects the plugin (`decider.source` 'convo', `decider.model`, and `decider.url`
    = the server root, written only where the previous version accepts it, otherwise '' with the root derived at call time by
    `decision_root`); a chat model switches the plugin off and writes `roles.classifier.url`; a provider or kind Convo cannot
    use switches it off with an empty URL. No provider and the toggle off: nothing is derived (the legacy behaviour)."""
    if not isinstance(config, Mapping):
        return config
    role, decider = _role(config), config.get('decider')
    if not isinstance(role, dict) or not isinstance(decider, dict):
        return config
    if decider.get('source') == 'system_one':
        config['system_one'] = {'enabled': True, 'backend': BACKEND}
        return config
    if not role.get('provider'):
        return config
    kind, base = effective_kind(role), resolve_base(role)
    if kind == 'decision' and base:
        root = server_root(base)
        classifier_url(root, role.get('remote', False))    # a public decision server needs the classifier's consent and HTTPS
        decider.update(source='convo', url=root if _rollback_safe(root) else '', model=role.get('model') or '')
        config['system_one'] = {'enabled': True, 'backend': BACKEND}
    else:
        selected = config.get('system_one') if isinstance(config.get('system_one'), dict) else {}
        config['system_one'] = {**selected, 'enabled': False}
        url = base if kind == 'chat' else ''
        if role.get('url') != url:
            role['url'] = url
    return config


def migration_view(config):
    """What the card shows for settings saved before it existed (read-time only; nothing is written): a separate decider
    (`decider.source` 'convo' or '', System One on with d1_llama_cpp) as llama.cpp, a decision model at its address + /v1; a
    legacy classifier endpoint as provider '' and a chat model. Settings that already name a provider are shown as they are."""
    role, decider = config['roles']['classifier'], config.get('decider') or {}
    selected = config.get('system_one') or {}
    view = {'provider': role.get('provider') or '', 'kind': role.get('kind') or 'auto', 'api_base': role.get('api_base') or '',
            'model': role.get('model') or ''}
    if view['provider'] or decider.get('source') == 'system_one':
        return view
    if decider.get('source') in ('convo', '') and selected.get('enabled') is True and selected.get('backend') == BACKEND:
        return {'provider': 'llama_cpp', 'kind': 'decision', 'api_base': (decider.get('url') or DEFAULT_DECIDER_URL).rstrip('/') + '/v1',
                'model': decider.get('model') or ''}
    return {**view, 'kind': 'chat'}


def route(config):
    """Which classifier the runtime uses: 'system_one' (System One's decider, through the decision plugin), 'decision' (Convo's
    own decision classifier, through the plugin), 'plugin' (the plugin's own address: no choice made here), 'backend' (another
    registered decision backend) or 'chat' (the JSON verdict at roles.classifier.url)."""
    selected = config.get('system_one') if isinstance(config.get('system_one'), Mapping) else {}
    if selected.get('enabled') is not True:
        return 'chat'
    if selected.get('backend') != BACKEND:
        return 'backend'
    source = (config.get('decider') or {}).get('source')
    return {'system_one': 'system_one', 'convo': 'decision'}.get(source, 'plugin')


def decision_root(config):
    """Where Convo's own decision classifier is reached (server side only): {'url', 'path'}. With a provider: its server root
    for a decision model ('' when it has no address or is not a decision model: refused, never the default), and a path prefix
    may be part of it (`path` True). Without one: `decider.url`, or the decision plugin's default, with no path."""
    role = _role(config)
    if role.get('provider'):
        base = resolve_base(role) if effective_kind(role) == 'decision' else ''
        return {'url': server_root(base) if base else '', 'path': True}
    return {'url': ((config.get('decider') or {}).get('url') or DEFAULT_DECIDER_URL).rstrip('/'), 'path': False}


def _role(config):
    roles = config.get('roles') if isinstance(config.get('roles'), Mapping) else {}
    return roles.get('classifier') if isinstance(roles.get('classifier'), Mapping) else {}


def key_address(config):
    """The address Convo's own classifier key is bound to and sent to: the decision server root while Convo's own decision
    classifier is in use, otherwise the chat endpoint `roles.classifier.url` (as before)."""
    if route(config) == 'decision':
        return decision_root(config)['url']
    return (_role(config).get('url') or '').rstrip('/')


def _host(url):
    try:
        return urlsplit(url).netloc or url
    except ValueError:
        return url


def readiness_rows(config):
    """Classifier-provider rows for the settings readiness list (`classifier_*`). Pure: never contacts a server or reads a key.
    Empty for the legacy endpoint (provider '') and while System One's decider is used (the decider rows describe those)."""
    role = config['roles']['classifier']
    provider = role.get('provider') or ''
    if not provider or (config.get('decider') or {}).get('source') == 'system_one':
        return []
    row = {'id': 'classifier_provider', 'label': 'Classifier provider'}
    selectable, reason = status(provider)
    if not selectable:
        return [{**row, 'state': 'missing', 'detail': provider + ' cannot be the classifier: ' + reason}]
    spec = PROVIDERS.get(provider, {'label': provider, 'verified': False, 'text_only': False})
    kind = effective_kind(role)
    if not kind:
        return [{**row, 'state': 'missing', 'detail': spec['label'] + ' does not offer a ' + (role.get('kind') or 'auto')
                 + ' classifier. Choose Auto or another classifier type.'}]
    base = resolve_base(role)
    if not base:
        return [{**row, 'state': 'missing', 'detail': spec['label'] + ' needs an API base, for example http://host.docker.internal:8080/v1.'}]
    if role.get('token_env'):
        key = 'the environment reference ' + role['token_env'] + ' (nothing else, even when it is empty)'
    elif host_key(role) and role.get('key_source', 'provider') == 'provider':
        key = "Agent Zero's " + spec['label'] + " key, or Convo's own key saved for this address when Agent Zero has none"
    else:
        key = "Convo's own key saved for this address, if any"
    if kind == 'decision':
        target = server_root(base)
        rows = [{**row, 'kind': 'decision', 'state': 'ready' if spec['verified'] else 'unverified',
                 'detail': spec['label'] + ': a decision model (System One protocol) at ' + _host(target) + ', with ' + key + '. '
                 + ('' if spec['verified'] else UNVERIFIED + '; ') + 'each turn shows whether it answers.'}]
        if spec['text_only'] and config['roles']['conversation']['capabilities'].get('audio_input'):
            rows.append({'id': 'classifier_text_only', 'label': 'Classifier input', 'state': 'unsupported',
                         'detail': spec['label'] + ' decides from text only, so an audio conversation cannot use it. Choose another '
                         'classifier, or turn off the conversation model’s audio input for a transcript-only conversation.'})
    else:
        rows = [{**row, 'kind': 'chat', 'state': 'ready', 'detail': spec['label'] + ': a chat model answers the JSON verdict at '
                 + base + ', with ' + key + '. For an audio conversation the model must accept audio.'}]
    if host_key(role) and role.get('key_source', 'provider') == 'provider' and not role.get('token_env') \
            and (role.get('api_base') or '').strip() and provider in PROVIDERS and base != PROVIDERS[provider]['base']:
        rows.append({'id': 'classifier_key_route', 'label': 'Agent Zero key to a custom address', 'state': 'warning',
                     'detail': "Agent Zero's " + spec['label'] + ' key (when it holds one) is sent to ' + _host(base) + ', the API base '
                     'that replaces ' + spec['label'] + "'s own address. Choose Convo's own key as the key source if that address "
                     'should not receive it.'})
    return rows


# ---- Check server: on the user's click only. Lists the models, and for a decision model sends ONE tiny text decision.
CHECK_TIMEOUT = 4.0                 # seconds per request
LOAD_TIMEOUT = 90.0                 # the one decision, when a router lists the model as not loaded yet (it loads on that request)
CHECK_LIMIT = 256 * 1024            # bytes read from any answer
PROBE = {'state': 'Connection check from Convo. The text is: "hello".',
         'questions': {'check': {'type': 'choice', 'instructions': 'Is this a connection check?',
                                 'criteria': {'yes': 'Yes, a connection check', 'no': 'No'}}}}
_checking = False


def _refusal(code, message, host=''):
    """A refusal before anything was sent (or a failure to prepare): typed, with the card's check fields."""
    return {'ok': False, 'code': code, 'error': message, 'state': 'refused', 'message': message, 'models': [], 'host': host}


def model_ids(listing):
    """Model names from a /models answer (OpenAI `data` or llama.cpp `models`), bounded."""
    found = []
    for field in ('data', 'models'):
        items = listing.get(field) if isinstance(listing, Mapping) else None
        for item in items if isinstance(items, list) else ():
            name = item.get('id') or item.get('model') or item.get('name') if isinstance(item, Mapping) else None
            if isinstance(name, str) and 0 < len(name) <= 200 and name not in found:
                found.append(name)
    return found[:500]


def _loaded(listing, model):
    """False when a router's model list says `model` is not loaded yet (llama.cpp router mode: `status.value`), else True."""
    for item in (listing.get('data') or []) if isinstance(listing, Mapping) and isinstance(listing.get('data'), list) else ():
        if isinstance(item, Mapping) and item.get('id') == model and isinstance(item.get('status'), Mapping):
            return item['status'].get('value') in (None, 'loaded')
    return True


async def _send(client, method, url, headers, body=None, failures=None, timeout=None):
    """One bounded request: (status, parsed JSON or None), or (None, None) when nothing answered (the failure is added to
    `failures`). Redirects are not followed."""
    import httpx
    try:
        async with client.stream(method, url, headers=headers, json=body, **({'timeout': timeout} if timeout else {})) as response:
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > CHECK_LIMIT:
                    return response.status_code, None
                chunks.append(chunk)
    except httpx.HTTPError as failure:
        if failures is not None:
            failures.append(failure)
        return None, None
    try:
        return response.status_code, json.loads(b''.join(chunks) or b'null')
    except ValueError:
        return response.status_code, None


def _decided(data):
    answer = ((data or {}).get('answers') or {}).get('check') if isinstance(data, Mapping) and isinstance(data.get('answers'), Mapping) else None
    return isinstance(answer, Mapping) and answer.get('choice') in PROBE['questions']['check']['criteria']


async def check(request, *, transport=None):
    """Check server for the card's provider choice ({provider, api_base, model, kind}), on explicit request.

    GET <base>/models for a provider with an OpenAI-style base and, for a decision model, GET <root>/health and <root>/v1/models,
    then ONE tiny text decision POST <root>/v1/systemone (no audio). A public address without the classifier's saved remote
    consent, an unusable provider or a missing address is refused before anything is sent. The key is attached here (as for a
    turn) and never returned; a server's own error text is never echoed. Answer: {state: decision|chat|unreachable|refused|
    unverified, message, models, host}. `transport` is for tests."""
    global _checking
    if not isinstance(request, Mapping):
        return _refusal('invalid', 'Invalid classifier check request.')
    fields = {name: request.get(name) or '' for name in ('provider', 'api_base', 'model')}
    kind = request.get('kind') or 'auto'
    if any(not isinstance(value, str) or len(value) > 2000 for value in fields.values()) or kind not in CLASSIFIER_KINDS:
        return _refusal('invalid', 'Invalid classifier check request.')
    provider = fields['provider']
    if not provider:
        return _refusal('invalid', 'Choose a provider first: Check server asks that provider’s server.')
    selectable, reason = status(provider)
    if not selectable:
        return _refusal('not_usable', reason)
    role = {**fields, 'kind': kind}
    label = PROVIDERS.get(provider, {}).get('label', provider)
    effective = effective_kind(role)
    if not effective:
        return _refusal('invalid', label + ' does not offer a ' + kind + ' classifier.')
    base = resolve_base(role)
    if not base:
        return _refusal('not_configured', label + ' needs an API base: enter its address first.')
    offered = kinds(provider)
    decision = 'decision' in offered and kind != 'chat'
    root = server_root(base)
    host = _host(root if decision else base)
    if _checking:
        return _refusal('busy', 'A server check is already running.', host)
    try:
        from . import convo_setup
        config, _ = convo_setup._read_config()
        consent = config['roles']['classifier'].get('remote') is True
    except Exception:
        return _refusal('unavailable', "Convo's settings could not be read; nothing was sent.", host)
    try:
        classifier_url(base, consent)
        classifier_url(root, consent)
    except AddressRefused as refused:
        return _refusal(refused.code, str(refused), host)
    _checking = True
    try:
        try:
            headers = convo_setup.classifier_headers(config, role, root if decision else base, request.get('draft_key'))
        except ValueError as error:
            return _refusal('invalid', str(error), host)
        except Exception:
            return _refusal('unavailable', 'The key for this server could not be prepared; nothing was sent.', host)
        urls = ([base + '/models'] if 'chat' in offered else []) + ([root + '/health', root + '/v1/models'] if decision else [])
        import time
        from . import convo_net
        answers, failures, model = {}, [], fields['model'].strip()
        async with convo_net.client(CHECK_TIMEOUT, transport) as client:
            for url in dict.fromkeys(urls):
                answers[url] = await _send(client, 'GET', url, headers, failures=failures)
            health = answers.get(root + '/health', (None, None))[0]
            answered = any(status_code is not None for status_code, _ in answers.values())
            # A router loads a model on its first request: that one decision may wait for the load (LOAD_TIMEOUT).
            loading = decision and bool(model) and not all(_loaded(data, model) for code, data in answers.values() if code == 200)
            started = time.monotonic()
            posted = await _send(client, 'POST', root + ROUTE, headers, {**PROBE, 'model': model} if model else PROBE, failures,
                                 LOAD_TIMEOUT if loading else None) if decision and answered and health != 503 else (None, None)
            waited = time.monotonic() - started
        result = {'ok': True, **_verdict(answers, posted, health, model, label, host, decision,
                                         PROVIDERS.get(provider, {}).get('text_only', False))}
        if result['state'] == 'unreachable' and failures:
            result['message'] = convo_net.describe(failures[0], root if decision else base, int(CHECK_TIMEOUT))[1] + ' Nothing else was tried.'
        if loading and posted[0] == 200:
            result['message'] += ' The router loaded ' + model + ' for this check (' + str(round(waited)) + ' s): keep it loaded for '                                  'conversations (for llama.cpp, load-on-startup), since each turn allows the classifier about 1.5 s.'
        await convo_setup._connection_extras(result, 'classifier', role, root if decision else base, failures[0] if failures else None)
        return result
    finally:
        _checking = False


def _verdict(answers, posted, health, model, label, host, decision, text_only):
    """What the answers mean, in words. Nothing the server said is quoted except model names."""
    models = []
    for status_code, data in answers.values():
        if status_code == 200:
            models += [name for name in model_ids(data) if name not in models]
    statuses = [status_code for status_code, _ in list(answers.values()) + [posted] if status_code is not None]
    listing = (', '.join(models[:5])) if models else ''
    named = ' It lists ' + listing + ', not “' + model + '”; the name is sent as it is.' if model and models and model not in models else ''
    if not statuses:
        return {'state': 'unreachable', 'models': [], 'host': host,
                'message': 'No server answered at ' + host + '. Start it and check again; nothing else was tried.'}
    if any(code in (401, 403) for code in statuses):
        return {'state': 'refused', 'models': models, 'host': host,
                'message': host + ' refused the key (HTTP ' + str(next(c for c in statuses if c in (401, 403))) + '). Check the key Convo uses for ' + label + '.'}
    if decision and posted[0] == 200 and _decided(posted[1]):
        return {'state': 'decision', 'models': models, 'host': host,
                'message': host + ' answers the System One decision route (one short text decision; no audio was sent)'
                + ('. It lists ' + listing if listing else '') + '.' + named
                + (' ' + label + ' decides from text only: an audio conversation cannot use it.' if text_only else '')}
    if health == 503:
        return {'state': 'unverified', 'models': models, 'host': host,
                'message': host + ' answered: it is still loading its model. Check again in a moment.'}
    if models or any(code == 200 for code, _ in answers.values()):
        why = ' (its answer was not a decision)' if posted[0] == 200 else ' (HTTP ' + str(posted[0]) + ')' if posted[0] else ''
        note = (' It did not answer the System One decision route' + why + ': choose Chat model as the classifier type, or '
                'llama.cpp for a decision model.') if decision else ''
        return {'state': 'chat' if not named else 'unverified', 'models': models, 'host': host,
                'message': host + ' answers OpenAI-compatible requests' + (' and lists ' + listing if listing else '') + '.' + named + note}
    return {'state': 'unverified', 'models': models, 'host': host,
            'message': host + ' answered HTTP ' + str(statuses[0]) + ', but neither ' + ('the decision route nor ' if decision else '')
            + 'an OpenAI model list.'}
