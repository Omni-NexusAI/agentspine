"""Which conversation providers Convo can send audio to, where each one is reached, and the two explicit provider actions.

The conversation model is one Convo setting for every preset (like System One's Decider). It is stored in Convo's own
settings because Agent Zero's model slots are fixed and drop unknown roles and keys. Its provider ids are Agent Zero's own
(conf/model_providers.yaml), so it uses Agent Zero's key for that provider (API_KEY_<PROVIDER>); Convo's own write-only key
remains for a custom endpoint ("other") and for the endpoint-only configuration (provider ''). Only providers Convo can send
audio to over an OpenAI-compatible chat route are selectable; the others are listed with the reason. The tables, `status`,
`resolve_url`, `normalize` and `readiness_rows` are pure: they never contact a provider, read a key or write settings. Only
the explicit actions do, each on the user's click: `key_copy` writes Agent Zero's empty key slot, `audio_probe` sends one
short synthetic clip. Neither ever returns a key. webui/conversation-setup.js mirrors the tables (CONVERSATION_CATALOG).
"""
from __future__ import annotations

import array
import base64
import io
import json
import math
import re
import sys
import wave
from urllib.parse import urlsplit

# Selectable providers and their OpenAI-compatible base. `host_key`: the Agent Zero provider whose stored key it uses ('' =
# Convo's own write-only key). Requests go to <base>/chat/completions with the audio as an OpenAI `input_audio` WAV part.
PROVIDERS = {
    'openai': {'label': 'OpenAI', 'base': 'https://api.openai.com/v1', 'host_key': 'openai'},
    'openrouter': {'label': 'OpenRouter', 'base': 'https://openrouter.ai/api/v1', 'host_key': 'openrouter'},
    'google': {'label': 'Google', 'base': 'https://generativelanguage.googleapis.com/v1beta/openai', 'host_key': 'google'},
    # Local servers (Agent Zero v2.11's own provider entries and default addresses): audio works when the served model takes
    # audio (e.g. Gemma 4 on llama-server with its multimodal projector). Their Agent Zero key is usually a placeholder.
    'llama_cpp': {'label': 'llama.cpp', 'base': 'http://host.docker.internal:8080/v1', 'host_key': 'llama_cpp'},
    'vllm': {'label': 'vLLM', 'base': 'http://host.docker.internal:8000/v1', 'host_key': 'vllm'},
    'other': {'label': 'Other (OpenAI-compatible)', 'base': '', 'host_key': ''},
}
# Why a known Agent Zero provider cannot be chosen: a short tag for the option and the reason shown with it.
REASONS = {
    'route': {'short': 'no compatible audio route', 'reason': 'Not reachable through the OpenAI-compatible audio route Convo uses.'},
    'text': {'short': 'no audio input', 'reason': 'Convo cannot send audio input through this provider.'},
    'format': {'short': 'different audio format', 'reason': 'Takes audio in a different format from the one Convo sends.'},
}
DISABLED_KIND = {
    'anthropic': 'route', 'azure': 'route', 'bedrock': 'route', 'github_copilot': 'route', 'mistral': 'format',
    # sign-in routes other plugins add (they authenticate a chat client, not an OpenAI-compatible audio route Convo can call)
    'codex_oauth': 'route', 'gemini_api_oauth': 'route', 'github_copilot_oauth': 'route', 'xai_grok_oauth': 'route', 'nvidia_nim': 'text',
    **{name: 'text' for name in ('a0_venice', 'cerebras', 'cometapi', 'deepseek', 'groq', 'huggingface', 'lm_studio', 'moonshot',
                                 'nebius', 'ollama', 'ollama_cloud', 'omlx', 'sambanova', 'venice', 'xai', 'zai', 'zai_coding')},
}
DISABLED = {name: REASONS[kind]['reason'] for name, kind in DISABLED_KIND.items()}
UNKNOWN_SPEC = {'short': 'unknown provider', 'reason': 'Unknown provider: Convo cannot send audio to it.'}
UNKNOWN = UNKNOWN_SPEC['reason']
# A provider id Convo does not list (for example one another plugin adds, such as a local router): the browser offers it when
# Agent Zero's details say it is OpenAI-compatible. It is used through its own API base and Agent Zero's key for that id.
# Same words as webui/conversation-setup.js UNKNOWN_COMPATIBLE.
UNKNOWN_COMPATIBLE = ('Agent Zero lists this provider as OpenAI-compatible; Convo has not verified that it accepts audio input. '
                      'Test audio input checks it.')
PROVIDER_ID = re.compile(r'[a-z0-9_]{1,64}')


def status(provider):
    """(selectable, reason) for a provider id. '' (the endpoint exactly as entered) and Convo's providers are selectable; a known
    unusable provider is not; any other valid id is an unknown provider the browser offered as OpenAI-compatible (it decides
    that from Agent Zero's provider details): selectable, with the 'audio not verified' note."""
    if not provider or provider in PROVIDERS:
        return True, ''
    if provider in DISABLED or not (isinstance(provider, str) and PROVIDER_ID.fullmatch(provider)):
        return False, DISABLED.get(provider, UNKNOWN)
    return True, UNKNOWN_COMPATIBLE


def resolve_url(role):
    """The OpenAI-compatible base a conversation role is reached at: the URL exactly as entered for the endpoint-only
    configuration (provider ''), otherwise the API base override or the provider's own base; '' when neither is known or the
    provider is one Convo cannot use."""
    role = role or {}
    provider = role.get('provider') or ''
    if not provider:
        return role.get('url', '')
    if not status(provider)[0]:
        return ''
    override = (role.get('api_base') or '').strip()
    base = PROVIDERS[provider]['base'] if provider in PROVIDERS else ''     # an unknown provider has no base of its own
    return override.rstrip('/') if override else base


def host_key(role):
    """The Agent Zero provider id whose stored key this conversation role uses, or '' for Convo's own key."""
    provider = (role or {}).get('provider') or ''
    if provider in PROVIDERS:
        return PROVIDERS[provider]['host_key']
    return provider if provider and status(provider)[0] else ''       # an unknown provider: Agent Zero's key for that id


def normalize(config):
    """Derive the runtime endpoint from the chosen provider, before validation, on every settings write: with a provider,
    `roles.conversation.url` is its API base override or its own base ('' for one Convo cannot use, so no address is
    invented); without one (''), the URL stays exactly as entered. Older code keeps reading `url`. A cloud base therefore
    needs the role's remote consent, which validation enforces as for any endpoint."""
    role = ((config or {}).get('roles') or {}).get('conversation')
    if isinstance(role, dict) and role.get('provider'):
        url = resolve_url(role)
        if role.get('url') != url:
            role['url'] = url
    return config


def _host(url):
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


def readiness_rows(config):
    """Conversation-provider rows for the settings readiness list. Pure: never contacts a provider or reads a key."""
    role = ((config or {}).get('roles') or {}).get('conversation') or {}
    provider = role.get('provider') or ''
    if not provider:
        return []                                         # the endpoint exactly as entered: the endpoint rows describe it
    row = {'id': 'conversation_provider', 'label': 'Conversation provider'}
    selectable, reason = status(provider)
    if not selectable:
        return [{**row, 'state': 'missing', 'detail': provider + ' cannot be used: ' + reason
                 + ' Choose OpenAI, OpenRouter, Google, llama.cpp, vLLM or Other (OpenAI-compatible).'}]
    spec, url = PROVIDERS.get(provider) or {'label': provider, 'base': ''}, resolve_url(role)
    if not url:
        return [{**row, 'state': 'missing', 'detail': spec['label'] + ' needs an API base, for example your llama.cpp server '
                 '(http://host.docker.internal:8080/v1).'}]
    owner = host_key(role)
    if role.get('token_env'):
        key = 'the environment reference ' + role['token_env'] + ' (nothing else, even when it is empty)'
    elif owner and role.get('key_source', 'provider') == 'provider':
        key = "Agent Zero's " + spec['label'] + " key, or Convo's own key saved for this address when Agent Zero has none"
    else:
        key = "Convo's own key saved for this address"
    rows = [{**row, 'state': 'ready' if provider in PROVIDERS else 'unverified', 'detail': spec['label'] + ': requests go to ' + url
             + ' with ' + key + '. The model must accept audio input; Test audio input checks it with one short synthetic clip.'
             + ('' if provider in PROVIDERS else ' ' + UNKNOWN_COMPATIBLE)}]
    if owner and role.get('key_source', 'provider') == 'provider' and not role.get('token_env') and url != spec['base']:
        rows.append({'id': 'conversation_key_route', 'label': 'Agent Zero key to a custom address', 'state': 'warning',
                     'detail': "Agent Zero's " + spec['label'] + ' key (when it holds one) is sent to ' + _host(url) + ', the API '
                     'base that replaces ' + spec['label'] + "'s own address. Choose Convo's own key as the key source if that "
                     'address should not receive it.'})
    return rows


def _refuse(code, message):
    return {'ok': False, 'code': code, 'error': message}


def key_copy(request):
    """Copy Convo's saved conversation key into Agent Zero's key for the saved provider, on explicit request.

    Only into an EMPTY slot, never over a key; Convo keeps its own copy; the key is never returned. It moves only to the
    address it was saved for: the provider must be the saved one (any `provider` in the request must match it) and Convo's
    key must be bound to the URL the saved settings send requests to. Every refusal is typed and writes nothing."""
    try:
        from . import convo_setup
        store = convo_setup.store
        with store.lock():
            config, _ = convo_setup._read_config()
            role = config['roles']['conversation']
            provider = role.get('provider') or ''
            requested = (request or {}).get('provider')
            if requested is not None and requested != provider:
                return _refuse('unsaved', 'Save the provider choice first: the copy goes to the saved provider only.')
            target = host_key(role)
            if not target:
                return _refuse('not_applicable', "Only OpenAI, OpenRouter and Google use Agent Zero's keys; this provider "
                                                 "keeps Convo's own key.")
            label = PROVIDERS[provider]['label']
            saved = convo_setup._secrets().load_secrets()
            name = convo_setup._key_name('conversation')
            key, bound = saved.get(name, ''), saved.get(name + '_URL', '')
            if not key:
                return _refuse('no_convo_key', 'Convo holds no saved conversation key to copy.')
            if bound != (role.get('url') or '').rstrip('/'):
                return _refuse('different_endpoint', "Convo's saved key belongs to another address, so it is not copied to "
                                                     + label + '. Enter the ' + label + " key in Agent Zero's key field.")
            if convo_setup._host_key(target):
                return _refuse('occupied', 'Agent Zero already holds a ' + label + ' key; it was not changed.')
            from helpers import dotenv
            dotenv.save_dotenv_value('API_KEY_' + target.upper(), key)
            if not convo_setup._host_key(target):
                return _refuse('unavailable', "Agent Zero's key store did not confirm the copy; nothing else was changed.")
    except Exception:
        return _refuse('unavailable', "Agent Zero's key store or Convo's settings could not be read; nothing was copied.")
    return {'ok': True, 'copied': True, 'provider': target,
            'message': "Copied Convo's key into Agent Zero's " + label + ' key. Convo keeps its own copy; nothing else changed.'}


# ---- Test audio input: one short synthetic clip, only on the user's click.
PROBE_SECONDS, PROBE_RATE = 0.8, 16000
PROBE_LIMIT = 256 * 1024
_probing = False


def _clip(seconds=PROBE_SECONDS, rate=PROBE_RATE):
    """A short 440 Hz tone as base64 mono PCM16 WAV: the same kind of audio a completed turn carries, and nothing personal."""
    frames, fade = int(seconds * rate), int(0.02 * rate)
    samples = array.array('h', (int(8000 * min(1.0, n / fade, (frames - 1 - n) / fade) * math.sin(2 * math.pi * 440 * n / rate))
                                for n in range(frames)))
    if sys.byteorder == 'big':
        samples.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    return base64.b64encode(buffer.getvalue()).decode()


def _voice_active():
    try:
        from helpers import runtime
    except ImportError:
        return False
    service = getattr(runtime, '_convo_service', None)
    if service is None:
        return False
    with service.connection_lock:
        return bool(service.connections or getattr(service, 'retiring_connections', {}))


async def _post(url, body, headers):
    """One bounded POST. Returns (status, parsed JSON or None). Raises on a transport failure."""
    import httpx
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False) as client:
        async with client.stream('POST', url, json=body, headers=headers) as response:
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > PROBE_LIMIT:
                    return response.status_code, None
                chunks.append(chunk)
    try:
        return response.status_code, json.loads(b''.join(chunks))
    except ValueError:
        return response.status_code, None


def _reply(data):
    """The model's answer, shortened, as plain text (data, never markup)."""
    try:
        content = data['choices'][0]['message'].get('content') or ''
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    if isinstance(content, list):
        content = ' '.join(part.get('text', '') for part in content if isinstance(part, dict))
    text = ' '.join(str(content).split())
    return text[:59] + '…' if len(text) > 60 else text


def verdict(status_code, data, label):
    """What one probe answer means. A provider's error text is never echoed: it can quote part of a key."""
    if status_code == 200:
        reply = _reply(data)
        if reply is None:
            return {'passed': False, 'code': 'format', 'message': 'The provider answered, but not in the OpenAI chat format Convo reads.'}
        return {'passed': True, 'code': 'passed', 'reply': reply, 'message': 'The model accepted a short synthetic audio clip and '
                'replied. This proves the route and audio input, not how well it understands speech.'}
    if status_code in (401, 403):
        message = 'The provider refused the key (HTTP %d). Check the key Convo uses for %s.' % (status_code, label)
    elif status_code == 404:
        message = 'Not found (HTTP 404): check the model name and the API base.'
    elif status_code == 429:
        message = 'Rate limited or out of quota (HTTP 429). Try again later.'
    elif 400 <= status_code < 500:
        message = ('The provider refused the audio request (HTTP %d): the model probably does not accept audio input, or the '
                   'model name is wrong.' % status_code)
    else:
        message = 'The provider reported an error (HTTP %d).' % status_code
    return {'passed': False, 'code': 'refused', 'message': message}


async def audio_probe(request):
    """Send one short synthetic clip to the SAVED conversation model, on explicit request, and report whether it was accepted.

    Keys are attached here, server-side, exactly as for a conversation; the answer never carries a key, a header or the
    provider's own error text. Refused without sending anything while a voice session is active, while another probe runs,
    or when the saved settings cannot reach a model. Any `provider` in the request must be the saved one."""
    global _probing
    from . import convo_setup
    from .convo_contract import ContractError, credentials, endpoint
    if _probing:
        return _refuse('busy', 'An audio test is already running.')
    try:
        config, _ = convo_setup._read_config()
    except Exception:
        return _refuse('unavailable', "Convo's settings could not be read; nothing was sent.")
    role = config['roles']['conversation']
    provider = role.get('provider') or ''
    requested = (request or {}).get('provider')
    if requested is not None and requested != provider:
        return _refuse('unsaved', 'Save the provider choice first: the test uses the saved settings.')
    selectable, reason = status(provider)
    if not selectable:
        return _refuse('not_usable', reason)
    if not role.get('model'):
        return _refuse('not_configured', 'Choose a model first.')
    if not (role.get('capabilities') or {}).get('audio_input'):
        return _refuse('not_configured', 'Accepts audio input is off, so Convo sends this model transcripts, not audio.')
    if _voice_active():
        return _refuse('busy', 'End the Convo voice session before testing audio input.')
    label = PROVIDERS[provider]['label'] if provider else 'this endpoint'
    _probing = True
    try:
        private = convo_setup.attach_keys(config)['roles']['conversation']
        try:
            url, headers = endpoint(private), credentials(private)
        except ContractError as exc:
            return _refuse('invalid', str(exc))
        body = {'model': role['model'], 'max_tokens': 16, 'temperature': 0, 'stream': False, 'messages': [
            {'role': 'system', 'content': 'This is an automatic audio connection check.'},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': 'The audio is a short synthetic test tone. Reply with the single word OK.'},
                {'type': 'input_audio', 'input_audio': {'data': _clip(), 'format': 'wav'}}]}]}
        try:
            status_code, data = await _post(url + '/chat/completions', body, headers)
        except Exception:
            return {'ok': True, 'passed': False, 'code': 'unreachable',
                    'message': 'Could not reach ' + _host(url) + ' (connection failed or timed out). Nothing else was tried.'}
        return {'ok': True, 'status': status_code, **verdict(status_code, data, label)}
    except Exception:
        return _refuse('unavailable', 'The audio test could not be prepared; nothing was sent.')
    finally:
        _probing = False
