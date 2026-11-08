"""Explicit, bounded setup actions; no inference, discovery scans or lifecycle on reads."""
from __future__ import annotations

import copy
import json
import os
import re
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import convo_net
from . import setup_store as store
from . import speech_discovery
from .convo_contract import (DEFAULTS, ContractError, credential_reference_label, credential_reference_problems, credentials, endpoint,
                             merge, refuse_new_credential_references, validate_settings)

ROLES = ('conversation', 'classifier', 'tts')


def _secrets():
    from helpers.secrets import SecretsManager
    path = store.config_path().parent / 'data' / 'connections.env'
    return SecretsManager.get_instance(str(path))


def _key_name(role):
    return 'CONVO_' + role.upper() + '_API_KEY'


def _host_key(provider):
    """Agent Zero's own stored key for a provider (API_KEY_<PROVIDER> or its aliases), or ''. The host answers the literal
    'None' when no key is set; that, like an empty value or the 'NA' placeholder Agent Zero also uses, means unset."""
    try:
        import models
        value = models.get_api_key(provider)
    except Exception:
        return ''
    return value if isinstance(value, str) and value.strip() not in ('', 'None', 'NA') else ''


def _key_address(name, config):
    """The address a role's own Convo key is bound to: its endpoint URL; for the classifier the address its requests go to
    (the decision server root while Convo's own decision classifier is in use)."""
    if name == 'classifier':
        from .convo_classifier_catalog import key_address
        return key_address(config)
    return config['roles'][name].get('url', '').rstrip('/')


def _role_key(name, role, address, saved):
    """The key one role sends to `address`, or '' (first match wins): nothing beside an explicit environment reference (read by
    `credentials`, authoritative even when empty); Agent Zero's own key for the role's provider (key_source 'provider' and a
    provider whose key Agent Zero holds); Convo's own write-only key, only for the exact address it was saved for."""
    from . import convo_classifier_catalog, convo_model_catalog, speech_providers
    providers = {'conversation': convo_model_catalog.host_key, 'classifier': convo_classifier_catalog.host_key, 'tts': speech_providers.host_key}
    if role.get('token_env'):
        return ''
    if role.get('key_source', 'provider') == 'provider' and role.get('provider'):
        host = providers[name](role)
        key = _host_key(host) if host else ''
        if key:
            return key
    key = saved.get(_key_name(name), '')
    bound = saved.get(_key_name(name) + '_URL', '')
    return key if key and bound == address and (address or name != 'classifier') else ''


def attach_keys(config):
    """Private host-to-gateway settings only. Never use for a browser response.

    Key sources, first match wins: an explicit environment reference (authoritative, even when empty); Agent Zero's own key
    for the role's provider (key_source 'provider' and a provider whose key Agent Zero holds); Convo's own write-only key,
    bound to the exact address it was saved for (the endpoint URL; for the classifier, `_key_address`)."""
    result = copy.deepcopy(config)
    saved = _secrets().load_secrets()
    for name in ROLES:
        role = result['roles'][name]
        role.pop('_convo_key', None)
        key = _role_key(name, role, _key_address(name, config), saved)
        if key:
            role['_convo_key'] = key
    return result


def classifier_bearer(config):
    """The Authorization header value ('Bearer …') for Convo's own decision classifier, or '' when no key resolves or Convo's own
    decision classifier is not in use. Server side only: the decision plugin calls it in-process for each decision call; it is
    never part of a status, readiness or browser response. Key sources as `attach_keys`, bound to the decision server root."""
    from .convo_classifier_catalog import route
    if route(config) != 'decision':
        return ''
    return credentials(attach_keys(config)['roles']['classifier']).get('Authorization', '')


def classifier_headers(config, role, address, draft_key=None):
    """The Authorization header Check server sends to `address` for the card's provider choice `role` over the saved classifier
    settings (`config`): the saved environment reference, a key typed in the card but not saved yet (this request only), Agent
    Zero's key for that provider, or Convo's key saved for exactly that address. Server side only; never returned."""
    merged = {**config['roles']['classifier'], **role}
    merged.pop('_convo_key', None)
    key = _role_key('classifier', merged, address, _secrets().load_secrets())
    _draft_key(merged, draft_key)
    return credentials({**merged, '_convo_key': merged.get('_convo_key') or key} if (key or merged.get('_convo_key')) else merged)


def _verification_path():
    return store.config_path().parent / 'data' / 'connections.json'


def _fingerprint(role):
    return store.revision(role)


def _read_config():
    from helpers import plugins
    with store.lock():
        raw = plugins.get_plugin_config('_convo') or {}
        config = validate_settings(raw.get('convo', {}))
        revision = store.settings_revision(store.read(store.config_path()))
    return config, revision


def incremental_input_row(config, backends=None):
    """Audio goes to the model as one COMPLETE clip per turn (`completed_clip`): accepted, not streamed. Shown whatever model
    is selected. A declared `incremental_input` capability is not read by the turn engine, so a declaration alone changes the
    wording to unverified, never to supported (that would need an adapter proving it extends model state while the user speaks)."""
    if backends is None:
        from .convo_system_one import registered_backends
        backends = registered_backends()
    declared = bool((config['roles']['conversation'].get('capabilities') or {}).get('incremental_input')) or any(
        (backend.get('capabilities') or {}).get('incremental_input') for backend in backends)
    row = {'id': 'incremental_input', 'label': 'Incremental audio input', 'mode': 'completed_clip'}
    if declared:
        return {**row, 'state': 'unverified', 'detail': 'An adapter declares incremental input, but Convo still sends completed turns only and does not use or verify that declaration. Audio input is not incremental or duplex input.'}
    return {**row, 'state': 'unsupported', 'detail': 'Completed turns only; audio input is not incremental or duplex input. Your speech is sent as one complete clip when you finish.'}


def _hook_rows(hook, config, failure_id, label):
    """Readiness rows from a catalog's hook (pure, given the settings without keys). A failing hook is one 'unavailable' row,
    never a failed settings read."""
    try:
        rows = hook(config)
        return [row for row in rows if isinstance(row, dict) and row.get('id')] if isinstance(rows, list) else []
    except Exception:
        return [{'id': failure_id, 'label': label, 'state': 'unavailable', 'detail': 'This readiness check could not run; the settings are unaffected.'}]


def read():
    from helpers import plugins
    from . import managed_runtime, studio_admission
    config, revision = _read_config()
    native_on = plugins.get_toggle_state('_convo') != 'disabled'
    runtime = managed_runtime.instance().public() if config['sidecar']['managed'] else {'state': 'unverified'}
    checks = [
        {'id': 'plugin', 'label': 'Plugin enabled', 'state': 'ready' if native_on else 'missing', 'detail': 'Native compatibility remains independent of configured conversation models.'},
        {'id': 'voice_mode', 'label': 'Browser voice activation', 'state': 'idle' if config['ai_link_development_mode'] else ('ready' if config['enabled'] else 'missing'), 'detail': 'AI Link development mode keeps browser voice idle; native speech remains available.' if config['ai_link_development_mode'] else 'Conversation activation is explicit; no microphone starts on a settings read.'},
        {'id': 'runtime', 'label': 'Conversation runtime', 'state': 'ready' if runtime.get('state') == 'ready' else 'unavailable', 'detail': runtime.get('message') or runtime.get('label') or runtime.get('state', 'External runtime not checked')},
        {'id': 'studio', 'label': 'Voice Studio interface', 'state': 'ready' if runtime.get('studio_available') else 'unavailable', 'detail': 'Studio dependencies must be explicitly installed. This does not verify the separate audio backend.'},
    ]
    from .convo_system_one import readiness as system_one_readiness
    decision_ready = system_one_readiness(config)
    from .convo_contract import input_modality_conflict
    conflict = input_modality_conflict(config, decision_ready if config['system_one']['enabled'] else None)
    checks.append({'id': 'input_modality', 'label': 'Conversation and classifier input', 'state': 'missing' if conflict else 'ready',
                   'detail': conflict or 'The conversation model and the turn-policy classifier work on the same kind of input.'})
    checks.append(incremental_input_row(config))
    checks.append({'id': 'system_one', 'label': 'System One decision consumer',
                   'state': decision_ready['state'] if config['system_one']['enabled'] else 'idle',
                   'detail': decision_ready['detail'] if config['system_one']['enabled'] else 'Disabled; existing classifier remains selected. No model is loaded by this setting.'})
    from .convo_policy import GateProfile, gate_report
    typed_gate = bool(config['system_one']['enabled'])
    gate = gate_report(GateProfile.from_settings(config['policy_gate']), typed=typed_gate)
    if typed_gate and not decision_ready['ready']:
        gate = {'state': 'unavailable', 'detail': 'The selected decision backend is not ready, so the calibrated gate has no probabilities to act on yet.'}
    checks.append({'id': 'speech_gate', 'label': 'Speech gate', **gate})
    admission = studio_admission.status(store.config_path().parent / 'data' / studio_admission.FILENAME)
    checks.append({'id': 'studio_admission', 'label': 'Studio operation safety',
                   'state': 'unavailable' if admission['blocked'] else 'ready',
                   'detail': admission['message'] or 'No unacknowledged Studio mutation is recorded.'})
    saved_checks = store.read(_verification_path())
    private = attach_keys(config)
    key_states = {}
    for path, problem in credential_reference_problems(config).items():
        # Reported, never a failed read: the settings must open so the field can be cleared.
        checks.append({'id': path.replace('.', '_'), 'label': credential_reference_label(path) + ' credential reference', 'state': 'missing',
                       'detail': problem + ' Clear “Credential environment reference” (Advanced) and save.', 'path': path})
    for name in ROLES:
        role = config['roles'][name]
        try:
            key_states[name] = bool(credentials(private['roles'][name]))
        except ContractError:
            key_states[name] = False
        if name == 'classifier' and config['system_one']['enabled']:
            continue  # Its saved connection remains intact but is not selected.
        native = name == 'tts' and role.get('backend') == 'host_kokoro'
        configured = native or bool(role.get('url') and role.get('model') and (name != 'tts' or role.get('voice')))
        previous = saved_checks.get(name, {})
        fresh = previous.get('fingerprint') == _fingerprint(private['roles'][name]) and time.time() - previous.get('checked_at', 0) < 300
        checks += [
            {'id': name + '_configured', 'label': name.title() + ' configured', 'state': 'ready' if configured else 'missing', 'detail': 'Uses native Kokoro settings.' if native else 'Select a model and endpoint' + (' and voice.' if name == 'tts' else '.')},
            {'id': name + '_connection', 'label': name.title() + ' connection', 'state': previous.get('state', 'unverified') if fresh else 'unverified', 'detail': previous.get('message', '') if fresh else 'Run Check connection. Listing a model does not verify audio or duplex support.'},
        ]
    # Internal credential fields are never persisted in config or returned here.
    from .convo_system_one import registered_backends
    # The managed child shares this owned data path; record presence never
    # authorizes deletion or proves a provider operation has stopped.
    speech_record = store.config_path().parent / 'data' / 'speech-admission.json'
    speech_blocked = speech_record.exists() if config['sidecar']['managed'] else None
    checks.append({'id': 'speech_admission', 'label': 'Speech operation safety',
                   'state': 'unavailable' if speech_blocked else ('ready' if speech_blocked is False else 'unverified'),
                   'detail': 'An external speech request remains recorded. Verify provider idleness before explicit reconciliation; OFF/restart cannot clear it.' if speech_blocked else 'Read-only managed admission check; no provider was contacted.' if speech_blocked is False else 'External gateway admission is checked at voice startup.'})
    from .convo_model_catalog import readiness_rows as conversation_rows
    from .speech_providers import readiness_rows as speech_rows
    from .convo_system_one import decider_rows
    from . import convo_classifier_catalog
    checks += _hook_rows(conversation_rows, config, 'conversation_provider', 'Conversation provider')
    checks += _hook_rows(speech_rows, config, 'speech_provider', 'Speech provider')
    checks += _hook_rows(convo_classifier_catalog.readiness_rows, config, 'classifier_provider', 'Classifier provider')
    checks += _hook_rows(decider_rows, config, 'decider', 'Classifier')
    from .speech_providers import describe
    from .convo_system_one import decider_reference
    speech = describe(config)
    return {'ok': True, 'config': config, 'revision': revision, 'credentials': key_states,
            'decision_backends': registered_backends(), 'providers': speech['providers'], 'native': speech['native'], 'decider_reference': decider_reference(config),
            'classifier_catalog': convo_classifier_catalog.catalog(), 'readiness': checks, 'runtime': runtime, 'studio_admission': admission,
            'network': convo_net.network(), 'certificates': _certificates(config), 'key_bound': _key_bound(config)}


def save(config, revision, keys=None):
    if not isinstance(config, dict) or not isinstance(revision, str):
        raise ValueError('Configuration and revision are required')
    store.reject_credentials(config)
    keys = keys or {}
    if not isinstance(keys, dict) or set(keys) - set(ROLES):
        raise ValueError('Unknown credential role')
    for value in keys.values():
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid API key')
    with store.lock():
        current = store.read(store.config_path())
        if store.settings_revision(current) != revision:
            raise store.Conflict('Settings changed in another view. Reload before applying your changes.')
        normalized = store.normalize(merge(current.get('convo', {}), config))
        refuse_new_credential_references(current.get('convo', {}), normalized)
        secret_manager = _secrets()
        old_secrets = secret_manager.read_secrets_raw()
        if keys:
            values = dict(secret_manager.load_secrets())
            for name, value in keys.items():
                # An explicit empty value clears the plugin-owned key only.
                values[_key_name(name)] = value
                values[_key_name(name) + '_URL'] = _key_address(name, normalized)
            _save_secrets(secret_manager, values)
        try:
            store.transaction(store.config_path(), lambda latest: merge(latest, {
                'convo': normalized,
                '_convo_write_epoch': int(latest.get('_convo_write_epoch', 0)) + 1,
            }), revision)
        except BaseException:
            if keys:
                secret_manager.save_secrets(old_secrets)
            raise
    return read()


_SEGMENT = re.compile(r'[a-z][a-z0-9_]{0,63}')
_MISSING = object()


def _parts(path):
    """A setting path a shared editor may patch: dotted lowercase segments under a known top-level setting, and under
    `roles` a known role."""
    parts = path.split('.') if isinstance(path, str) and 0 < len(path) <= 256 else []
    if not parts or len(parts) > 8 or any(not _SEGMENT.fullmatch(part) for part in parts) or parts[0] not in DEFAULTS \
            or (parts[0] == 'roles' and (len(parts) < 2 or parts[1] not in ROLES)):
        raise ValueError('Unknown or invalid setting path: ' + str(path)[:80])
    return parts


def _lookup(node, parts):
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _assign(node, parts, value):
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    if value is _MISSING:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = copy.deepcopy(value)


def _same(a, b):
    """JSON equality that keeps true and 1 apart but treats 1 and 1.0 alike (a browser sends 1 for a stored 1.0)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[key], b[key]) for key in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def patch(changes):
    """Write a few settings now and leave the rest as saved (the preset editor's autosave).

    Each change is {path, base?, value?}. It applies only where the saved value still equals `base`, the value the editor last
    loaded (no `base`: the path was absent then); no `value` removes the path. Anything else comes back as a conflict, untouched.
    The result is validated like a full save, credentials are refused, AI Link development mode keeps its voice-idle admission,
    and the shared write epoch advances so every open editor notices. Nothing is written when nothing applied."""
    if not isinstance(changes, list) or not 1 <= len(changes) <= 64:
        raise ValueError('Send between 1 and 64 setting changes')
    parsed = []
    for change in changes:
        if not isinstance(change, dict) or 'path' not in change or set(change) - {'path', 'base', 'value'}:
            raise ValueError('Each setting change needs a path and may carry only base and value')
        parts = _parts(change['path'])
        probe = {}
        _assign(probe, parts, change.get('value'))            # a credential named by the path, or anywhere inside the value
        store.reject_credentials(probe)
        parsed.append((change['path'], parts, change.get('base', _MISSING), change.get('value', _MISSING)))
    paths = [item[0] for item in parsed]
    if any(i != j and (a == b or b.startswith(a + '.')) for i, a in enumerate(paths) for j, b in enumerate(paths)):
        raise ValueError('Setting changes must not repeat or contain one another')
    applied, conflicts = [], []
    with store.lock():
        current = store.read(store.config_path())
        config = validate_settings(current.get('convo', {}))
        before = copy.deepcopy(config)
        for path, parts, base, value in parsed:
            now = _lookup(config, parts)
            if (now is _MISSING and base is _MISSING) or (now is not _MISSING and base is not _MISSING and _same(now, base)):
                _assign(config, parts, value)
                applied.append(path)
            else:
                conflicts.append({'path': path, 'mine': None if value is _MISSING else value, 'theirs': None if now is _MISSING else now})
        if applied:
            normalized = store.normalize(config)
            refuse_new_credential_references(before, normalized)
            # The validated node already carries every stored field, so it replaces the old one (a removal must not survive a merge).
            store.transaction(store.config_path(), lambda latest: {**latest, 'convo': normalized,
                              '_convo_write_epoch': int(latest.get('_convo_write_epoch', 0)) + 1}, store.settings_revision(current))
    return {**read(), 'conflicts': conflicts, 'applied': applied}


class Answered(ValueError):
    """The endpoint answered with a status other than 200."""
    def __init__(self, status):
        super().__init__('Endpoint answered HTTP ' + str(status))
        self.status = status


def _connection_problem(error, url):
    """What a failed models check tells the user, from Convo's own words only (no server text is echoed)."""
    if isinstance(error, ContractError):
        return str(error) + ' Nothing was contacted'
    if isinstance(error, Answered):
        if error.status in (401, 403):
            return 'The server refused the key (HTTP ' + str(error.status) + '). Check the API key or the credential environment reference. Nothing else was tried'
        if error.status == 404:
            return 'No models list at ' + url + '/models (HTTP 404). Check that the API base URL ends with the server’s /v1. Nothing else was tried'
        return 'The server answered HTTP ' + str(error.status) + ' to ' + url + '/models. Nothing else was tried'
    if isinstance(error, httpx.HTTPError):
        return convo_net.describe(error, url)[1] + ' Nothing else was tried'
    return 'The answer could not be read as a models list. Nothing else was tried'


def _draft_key(role, key):
    """A key typed in the card but not saved yet, used for this one request only: never stored, logged or returned. An explicit
    credential environment reference still wins."""
    if key is None or key == '':
        return
    if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 32 for c in key):
        raise ValueError('Invalid API key')
    if not role.get('token_env'):
        role['_convo_key'] = key


def _close_match(model, models):
    """The listed name that differs from `model` only in letter case, or ''."""
    wanted = (model or '').lower()
    return next((item['id'] for item in models if wanted and item['id'] != model and item['id'].lower() == wanted), '')


def _defaults(name, role):
    """The provider's own default address(es): where a refused connection on this computer is looked for instead."""
    from . import convo_classifier_catalog, convo_model_catalog, speech_providers
    if name == 'tts':
        provider, _ = speech_providers.effective_provider(role)
        return tuple(url for url in [speech_providers.PROVIDERS.get(provider, {}).get('default_url', '')] if url)
    catalog = convo_classifier_catalog.PROVIDERS if name == 'classifier' else convo_model_catalog.PROVIDERS
    base = (catalog.get(role.get('provider') or '') or {}).get('base', '')
    extra = (convo_classifier_catalog.DEFAULT_DECIDER_URL,) if name == 'classifier' else ()
    return tuple(url for url in (base,) + extra if url)


async def _connection_extras(result, name, role, url, error):
    """What the card shows beside a result: the certificate remembered for this address, a changed one, and after a refused
    connection the address that answers instead (only the provider's default port on this computer, or this computer's private
    address for a LAN or Tailscale address)."""
    if not url:
        return
    pin = convo_net.pin_for(url)
    result['certificate'] = convo_net.public_pin(pin)
    if error is None or not isinstance(error, httpx.HTTPError):
        return
    code = convo_net.failure_code(error)
    if code == 'certificate_changed':
        result['certificate_changed'] = {'url': url, 'remembered': convo_net.public_pin(pin)}
    elif code == 'refused':
        hint = await convo_net.suggestion(url, _defaults(name, role))
        if hint:
            result['suggestion'] = hint


async def _get(url, headers):
    async with convo_net.client(8) as client:
        async with client.stream('GET', url, headers=headers) as response:
            if response.status_code != 200:
                raise Answered(response.status_code)
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 2 * 1024**2:
                    raise ValueError('Discovery response exceeds the size limit')
                chunks.append(chunk)
            return json.loads(b''.join(chunks))


def _options(value, fields):
    if isinstance(value, dict):
        # The first non-empty list (an empty `data` beside a full `models` list is not the answer).
        present = [value[field] for field in fields if isinstance(value.get(field), (list, dict))]
        value = next((item for item in present if item), present[0] if present else value)
    if isinstance(value, dict):
        value = [dict(v, id=k) if isinstance(v, dict) else {'id': k} for k, v in value.items()]
    if not isinstance(value, list):
        return []
    result, seen = [], set()
    for item in value[:1000]:
        if isinstance(item, str):
            item = {'id': item}
        if not isinstance(item, dict):
            continue
        identity = item.get('id') or item.get('model_id') or item.get('profile_id') or item.get('voice_id') or item.get('model')
        label = item.get('label') or item.get('name') or identity
        if not isinstance(identity, str) or not 0 < len(identity) <= 2000 or identity in seen:
            continue
        result.append({'id': identity, 'label': str(label)[:2000]})
        seen.add(identity)
    return result


async def discover(name, provider=None, check=False, draft_key=None):
    if name not in ROLES:
        raise ValueError('Unknown model role')
    config, _ = _read_config()
    if provider is not None:
        store.reject_credentials(provider)
        if not isinstance(provider, dict):
            raise ValueError('Provider must be an object')
        config['roles'][name] = merge(config['roles'][name], provider)
        config = store.normalize(config)
    role = attach_keys(config)['roles'][name]
    _draft_key(role, draft_key)
    if name == 'classifier' and role.get('provider'):
        # Model discovery asks the provider's OpenAI-style base (/v1/models), a decision model's server included.
        from .convo_classifier_catalog import resolve_base
        role['url'] = resolve_base(role)
    if name == 'tts':
        result = await speech_discovery.discover(role, _get, _options)
        try:
            await _connection_extras(result, name, role, endpoint(role) if role.get('url') else '', result.pop('_failure', None))
        except ContractError:
            pass
    else:
        result = {'ok': True, 'models': [], 'voices': [], 'tuning': [], 'warnings': []}
        verification = {'state': 'unavailable', 'message': 'No connection checked.'}
        url, failure = '', None
        try:
            url, headers = endpoint(role), credentials(role)
            data = await _get(url + '/models', headers)
            result['models'] = _options(data, ('data', 'models', 'available_models'))
            model = role.get('model') or ''
            found = model in {m['id'] for m in result['models']}
            close = '' if found else _close_match(model, result['models'])
            if close:
                result['close_match'] = close
            verification = {'state': 'ready' if found else 'unverified', 'message': 'Endpoint responded; selected model listed. Audio and duplex capabilities still require testing.' if found
                            else ('Endpoint responded; it lists “' + close + '”, not “' + model + '”: model names must match exactly.' if close
                                  else 'Endpoint responded; saved model is not listed. Its selection was preserved.')}
        except (ValueError, httpx.HTTPError, KeyError) as error:
            failure = error
            verification = {'state': 'unavailable', 'message': _connection_problem(error, url) + '; no fallback was used.'}
        result['verification'] = verification
        await _connection_extras(result, name, role, url, failure)
    if check:
        # Store evidence only for the exact provider tested; never change selection.
        evidence = {**result['verification'], 'fingerprint': _fingerprint(role), 'checked_at': time.time()}
        store.transaction(_verification_path(), lambda previous: merge(previous, {name: evidence}))
    return result


def _save_secrets(manager, values):
    secret_path = store.config_path().parent / 'data' / 'connections.env'
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    manager.save_secrets('\n'.join(k + '=' + json.dumps(v) for k, v in values.items()) + '\n')
    os.chmod(secret_path, 0o600)


def key_rebind(name, provider=None):
    """"Use it for this address": Convo's saved key for a card follows the card's current address (`provider` = the card's
    draft, as Test sends it). Only the binding moves, on this explicit request; the key is never read back or returned."""
    if name not in ROLES:
        raise ValueError('Unknown model role')
    config, _ = _read_config()
    if provider is not None:
        store.reject_credentials(provider)
        if not isinstance(provider, dict):
            raise ValueError('Provider must be an object')
        config['roles'][name] = merge(config['roles'][name], provider)
        config = store.normalize(config)
    address = _key_address(name, config)
    if not address:
        raise ValueError('Enter the address first.')
    with store.lock():
        manager = _secrets()
        values = dict(manager.load_secrets())
        if not values.get(_key_name(name)):
            raise ValueError('Convo has no saved key for this card.')
        values[_key_name(name) + '_URL'] = address
        _save_secrets(manager, values)
    return read()


def _https_address(url):
    if not isinstance(url, str) or len(url) > 2000 or not url.lower().startswith('https://'):
        raise ValueError('An https address is required.')
    return url


def tls_accept(url):
    """Remember the certificate the server at `url` presents now (after it changed, or before first use). Own network only."""
    try:
        return {'ok': True, 'certificate': convo_net.remember(_https_address(url))}
    except OSError:
        raise ValueError('Could not read the certificate at ' + convo_net.origin(url) + '; nothing was remembered.') from None


def tls_forget(url):
    convo_net.forget(_https_address(url))
    return {'ok': True, 'certificate': None}


def _key_bound(config):
    """Per card with a saved Convo key: the address that key is bound to (the card offers "Use it for this address" when its
    address is now another). Addresses only; never a key."""
    try:
        saved = _secrets().load_secrets()
    except Exception:
        return {}
    return {name: saved[_key_name(name) + '_URL'] for name in ROLES if saved.get(_key_name(name)) and saved.get(_key_name(name) + '_URL')}


def _certificates(config):
    """The certificate remembered for each card's address (fingerprint, names, expiry; never the certificate itself)."""
    from .convo_classifier_catalog import resolve_base
    result = {}
    for name in ROLES:
        role = config['roles'][name]
        url = resolve_base(role) if name == 'classifier' and role.get('provider') else role.get('url', '')
        result[name] = convo_net.public_pin(convo_net.pin_for(url)) if url else None
    return result


def _import_values(document):
    if not isinstance(document, dict) or len(json.dumps(document)) > 1024**2:
        raise ValueError('Import a bounded JSON settings document')
    allowed = ('url', 'model', 'voice', 'backend')
    values = {}
    warnings = ['Keys are never imported. Enter them using the masked connection controls. History, persona and routing policies are not imported.']
    source = document.get('convo', document)
    roles = source.get('roles', {}) if isinstance(source, dict) else {}
    if isinstance(roles, dict):
        for name in ROLES:
            role = roles.get(name, {})
            if isinstance(role, dict):
                for field in allowed:
                    if field in role:
                        values['roles.' + name + '.' + field] = role[field]
    # Explicit mappings for the standalone realtime browser/server export.
    aliases = {
        'gemma_audio_base_url': 'roles.conversation.url', 'model_name': 'roles.conversation.model',
        'qwen3_tts_api_base_url': 'roles.tts.url', 'qwen3_tts_api_model': 'roles.tts.model', 'qwen3_tts_api_voice': 'roles.tts.voice',
        'gemma_url': 'roles.conversation.url', 'gemma_model': 'roles.conversation.model',
        'llm_base_url': 'roles.conversation.url', 'llm_model': 'roles.conversation.model',
        'tts_base_url': 'roles.tts.url', 'tts_model': 'roles.tts.model', 'tts_voice': 'roles.tts.voice',
        'classifier_url': 'roles.classifier.url', 'classifier_model': 'roles.classifier.model',
    }
    for field, path in aliases.items():
        if field in source:
            values[path] = source[field]
    for path, value in list(values.items()):
        if not isinstance(value, str) or len(value) > 2000:
            warnings.append('Ignored unsupported value for ' + path)
            del values[path]
    # A stored provider re-derives these on save (speech backend from the speech provider, conversation URL from provider and API
    # base), which would silently undo the import: an imported backend or URL selects the generic endpoint instead.
    if 'roles.tts.backend' in values:
        values['roles.tts.provider'] = ''
    if 'roles.conversation.url' in values:
        values['roles.conversation.provider'] = ''
    if 'roles.classifier.url' in values:
        values['roles.classifier.provider'] = ''
    return values, warnings


def preview(document):
    config, revision = _read_config()
    values, warnings = _import_values(document)
    changes = []
    for path, value in values.items():
        _, name, field = path.split('.')
        before = config['roles'][name].get(field, '')
        warning = 'Existing value will only be replaced if selected.' if before and before != value else ''
        if field == 'url':
            parsed = urlsplit(value)
            if parsed.hostname in {'localhost', '127.0.0.1', '::1'}:
                value = urlunsplit((parsed.scheme, 'host.docker.internal' + (':' + str(parsed.port) if parsed.port else ''), parsed.path, parsed.query, parsed.fragment))
                warning += ' Standalone loopback translated to the Docker host address.'
            try:
                endpoint({'url': value, 'remote': config['roles'][name].get('remote', False)})
            except ValueError:
                warnings.append('Unsupported address or missing remote consent: ' + path)
                continue
        if before != value:
            changes.append({'path': path, 'before': before, 'after': value, 'warning': warning.strip()})
    if not changes:
        warnings.append('No supported setting changes found. Profile ZIP exports are imported in Voice Studio.')
    return {'ok': True, 'changes': changes, 'warnings': warnings, 'revision': revision}


def apply(document, paths, revision):
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise ValueError('Select the import changes to apply')
    candidate = preview(document)
    if candidate['revision'] != revision:
        raise store.Conflict('Settings changed since import preview; preview again.')
    available = {c['path']: c['after'] for c in candidate['changes']}
    if set(paths) - set(available):
        raise ValueError('Import selection was not present in the preview')
    config, current_revision = _read_config()
    if current_revision != revision:
        raise store.Conflict('Settings changed since import preview; preview again.')
    for path in paths:
        _, name, field = path.split('.')
        config['roles'][name][field] = available[path]
    return save(config, revision)
