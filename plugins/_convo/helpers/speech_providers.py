"""Speech output: the providers the settings offer, the runtime backend each resolves to, and the controls and fields each owns.

Reading never writes, starts, loads, downloads or scans anything: availability comes from toggle state and configuration files,
and the native summaries are read straight from the owning plugins' configuration (never their credentials).

Two tables, both mirrored in webui/speech-catalog.js and kept identical by a contract test:
- BACKENDS: the runtime protocol (roles.tts.backend: openai | managed_audio | host_kokoro), the fields each owns and the legacy
  control vocabulary (url, key, remote, discover, model, voice, advanced, tuning, studio, instructions, pcm, native).
- PROVIDERS: what the editor offers and stores (roles.tts.provider + roles.tts.connection). The backend is derived from it by
  normalize(), on every settings write, so code that knows only backends, and a rollback, keep working. Speech output is not a
  model type: it is the voice Convo's replies are spoken with.
"""
from __future__ import annotations

import copy
import re
from urllib.parse import urlsplit

CONTROLS = ('url', 'key', 'remote', 'discover', 'model', 'voice', 'advanced', 'tuning', 'studio', 'instructions', 'pcm', 'native')

# Fields a backend owns on roles.tts. The legacy backend switch stashed these under roles.tts.saved[<backend>]; the provider editor
# reads that stash for an older configuration and never writes it.
BACKENDS = {
    'openai': {
        'label': 'External compatible speech API', 'owner': 'external', 'settings_owner': 'convo', 'voices': 'discover',
        'controls': ('url', 'key', 'remote', 'discover', 'model', 'voice', 'advanced', 'instructions', 'pcm'),
        'fields': ('url', 'model', 'voice', 'token_env', 'remote', 'instructions'),
    },
    'managed_audio': {
        'label': 'Convo-owned audio.cpp', 'owner': 'convo', 'settings_owner': 'convo', 'voices': 'profiles',
        'controls': ('discover', 'model', 'voice', 'tuning', 'studio', 'instructions', 'pcm'),
        'fields': ('model', 'voice', 'tuning_profile', 'instructions'),
    },
    'host_kokoro': {
        'label': 'Native Kokoro', 'owner': 'host', 'settings_owner': 'native', 'voices': 'native',
        'controls': ('native',),
        'fields': (),
    },
}
# Fields a stash entry may hold; the union of every backend's own fields.
STASH_FIELDS = tuple(dict.fromkeys(field for spec in BACKENDS.values() for field in spec['fields']))
STASHED_BACKENDS = tuple(name for name, spec in BACKENDS.items() if spec['fields'])

KEY_SOURCES = ('provider', 'convo')
CONNECTIONS = ('external', 'managed')
GROUPS = ('builtin', 'local', 'cloud', 'other')
# OpenAI's documented speech voices: tts-1 and tts-1-hd offer nine, gpt-4o-mini-tts thirteen. Only gpt-4o-mini-tts honours
# `instructions` ("does not work with tts-1 or tts-1-hd"); a dated snapshot of it (gpt-4o-mini-tts-<date>) is the same model.
OPENAI_VOICES = ('alloy', 'ash', 'coral', 'echo', 'fable', 'onyx', 'nova', 'sage', 'shimmer')
OPENAI_MINI_VOICES = ('alloy', 'ash', 'ballad', 'coral', 'echo', 'fable', 'nova', 'onyx', 'sage', 'shimmer', 'verse', 'marin', 'cedar')

# The speech provider catalog, grouped like Agent Zero's own providers. A value given per connection is a {connection: value} map.
#   backend      runtime protocol            connections  ways to reach it ('' = one way)
#   default_url  editable default address ('' = none documented: the user enters it)
#   host_key     Agent Zero provider whose stored key (API_KEY_<PROVIDER>) it uses; '' = Convo's own write-only key
#   models       fixed {model: voices}; {} = whatever the server lists
#   instructions delivery instructions: 'never' | 'declared' (the user marks the server as accepting them) | the models that do
#   pcm          offers the native PCM16 stream (the audio.cpp stream format)
#   verify       how Test connection reads it: native | managed | audiocpp | speech_route | models (see speech_discovery)
PROVIDERS = {
    'kokoro_native': {'label': 'Kokoro', 'group': 'builtin', 'backend': 'host_kokoro', 'connections': (), 'default_url': '', 'host_key': '',
                      'models': {}, 'instructions': 'never', 'pcm': False, 'verify': 'native'},
    'audiocpp': {'label': 'audio.cpp', 'group': 'local', 'backend': {'external': 'openai', 'managed': 'managed_audio'}, 'connections': CONNECTIONS,
                 'default_url': 'http://host.docker.internal:8890/v1', 'host_key': '', 'models': {},
                 'instructions': {'external': 'declared', 'managed': 'never'}, 'pcm': True, 'verify': {'external': 'audiocpp', 'managed': 'managed'}},
    # llama.cpp and Unsloth Studio speak the standard speech protocol here, but neither documents an OpenAI-style speech route:
    # they stay "not verified" until Test connection shows what the server answers.
    'llamacpp': {'label': 'llama.cpp', 'group': 'local', 'backend': 'openai', 'connections': (), 'default_url': 'http://host.docker.internal:8080/v1',
                 'host_key': '', 'models': {}, 'instructions': 'declared', 'pcm': False, 'verify': 'speech_route'},
    'unsloth_studio': {'label': 'Unsloth Studio', 'group': 'local', 'backend': 'openai', 'connections': (), 'default_url': '', 'host_key': '',
                       'models': {}, 'instructions': 'declared', 'pcm': False, 'verify': 'speech_route'},
    'openai_tts': {'label': 'OpenAI', 'group': 'cloud', 'backend': 'openai', 'connections': (), 'default_url': 'https://api.openai.com/v1', 'host_key': 'openai',
                   'models': {'gpt-4o-mini-tts': OPENAI_MINI_VOICES, 'tts-1': OPENAI_VOICES, 'tts-1-hd': OPENAI_VOICES},
                   'instructions': ('gpt-4o-mini-tts',), 'pcm': False, 'verify': 'models'},
    'openai_compatible': {'label': 'OpenAI-compatible speech endpoint', 'group': 'other', 'backend': 'openai', 'connections': (), 'default_url': '',
                          'host_key': '', 'models': {}, 'instructions': 'declared', 'pcm': True, 'verify': 'speech_route'},
}
# What an older configuration (no provider) means, from its backend alone.
LEGACY = {'openai': ('openai_compatible', ''), 'managed_audio': ('audiocpp', 'managed'), 'host_kokoro': ('kokoro_native', '')}
# The user's audio.cpp container answers on this port: an older openai configuration pointing there is OFFERED as audio.cpp (external
# server), and becomes it only when the user confirms. Nothing else is inferred from an address.
AUDIOCPP_PORT = 8890
AUDIOCPP_HOSTS = ('127.0.0.1', 'localhost', 'host.docker.internal', '::1')
# A Base model clones a reference voice and ignores delivery instructions, and so does a clone voice.
BASE_MODEL = re.compile(r'(^|[^a-z0-9])base([^a-z0-9]|$)')
BOOLEAN_FIELDS = ('remote', 'accepts_instructions', 'native_pcm')


def per(value, connection):
    """A catalog value that may be given per connection ({connection: value})."""
    return value.get(connection) if isinstance(value, dict) else value


def connection_fields(provider, connection=''):
    """roles.tts fields that belong to one provider (and connection): its backend's own fields, plus the declaration that the server
    accepts delivery instructions and the native PCM toggle where they apply (native_pcm lives in capabilities on the role)."""
    spec = PROVIDERS[provider]
    fields = list(BACKENDS[backend_for(provider, connection)]['fields'])
    if per(spec['instructions'], connection) == 'declared':
        fields.append('accepts_instructions')
    if per(spec['pcm'], connection):
        fields.append('native_pcm')
    return tuple(fields)


def _provider_fields(name, spec):
    """Keys a provider's provider_saved record may hold: its fields and whose key it uses; with connections also the connection
    it was left on and one record per connection (an older flat record holds one connection's fields directly)."""
    connections = spec['connections'] or ('',)
    fields = dict.fromkeys(field for connection in connections for field in connection_fields(name, connection))
    if not fields:
        return ()
    if spec['connections']:
        fields.update(dict.fromkeys(('connection',) + tuple(spec['connections'])))
    fields['key_source'] = None
    return tuple(fields)


def backend_for(provider, connection=''):
    """The runtime backend a provider (and, for audio.cpp, its connection) speaks through, or None for an unknown id."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        return None
    if isinstance(spec['backend'], dict):
        return spec['backend'].get(connection or spec['connections'][0])
    return spec['backend']


PROVIDER_FIELDS = {name: _provider_fields(name, spec) for name, spec in PROVIDERS.items()}
STASHED_PROVIDERS = tuple(name for name, fields in PROVIDER_FIELDS.items() if fields)


def effective_provider(tts):
    """(provider, connection) in effect for roles.tts: the stored provider, or for an older configuration the one its backend
    means. Never guessed from the URL."""
    tts = tts or {}
    spec = PROVIDERS.get(tts.get('provider') or '')
    if spec is not None:
        if not spec['connections']:
            return tts['provider'], ''
        return tts['provider'], tts.get('connection') if tts.get('connection') in spec['connections'] else spec['connections'][0]
    return LEGACY.get(tts.get('backend') or 'openai', LEGACY['openai'])


def choice_label(provider, connection=''):
    label = PROVIDERS[provider]['label']
    return label + {'external': ' (external server)', 'managed': ' (managed by Convo)'}.get(connection, '')


def host_key(role):
    """The Agent Zero provider id whose stored key this speech provider uses, or '' for Convo's own key."""
    spec = PROVIDERS.get((role or {}).get('provider') or '')
    return spec['host_key'] if spec else ''


def pcm_supported(provider, connection=''):
    return bool(per(PROVIDERS[provider]['pcm'], connection))


def is_base_clone(model='', voice=''):
    """A Base (voice-cloning) model or a clone voice: it copies a reference voice and ignores delivery instructions."""
    return str(voice or '').startswith('clone:') or bool(BASE_MODEL.search(str(model or '').lower()))


def instructions_supported(tts, provider=None, connection=None):
    """Whether the speech engine honours delivery instructions for this provider, connection, model and voice. For an older
    configuration (no provider) its hand-set `instruction_control` flag counts as the declaration."""
    if provider is None:
        provider, connection = effective_provider(tts)
    rule = per(PROVIDERS[provider]['instructions'], connection or '')
    model, voice = str(tts.get('model') or ''), str(tts.get('voice') or '')
    if rule == 'never':
        return False
    if rule == 'declared':
        declared = tts.get('accepts_instructions') is True or (not tts.get('provider') and (tts.get('capabilities') or {}).get('instruction_control') is True)
        return declared and not is_base_clone(model, voice)
    return any(model == name or model.startswith(name + '-') for name in rule)


def legacy_audiocpp_offer(tts):
    """The address of an older openai configuration that points at the known audio.cpp port on this machine, or ''. It is only an
    offer: the provider stays derived from the backend until the user confirms."""
    tts = tts or {}
    if tts.get('provider') or (tts.get('backend') or 'openai') != 'openai':
        return ''
    try:
        parsed = urlsplit(tts.get('url') or '')
        return tts['url'] if parsed.port == AUDIOCPP_PORT and (parsed.hostname or '').lower() in AUDIOCPP_HOSTS else ''
    except ValueError:
        return ''


def _park_slot(tts, provider, connection, create=False):
    """The provider_saved record that holds this provider's (and connection's) delivery instructions while they cannot be used."""
    path = (provider, connection) if PROVIDERS[provider]['connections'] else (provider,)
    if not isinstance(tts.get('provider_saved'), dict):
        if not create:
            return None
        tts['provider_saved'] = {}
    node = tts['provider_saved']
    for key in path:
        if not isinstance(node.get(key), dict):
            if not create:
                return None
            node[key] = {}
        node = node[key]
    return node


def _prune(tts, provider, connection):
    saved = tts.get('provider_saved')
    if not isinstance(saved, dict):
        return
    record = saved.get(provider)
    if PROVIDERS[provider]['connections'] and isinstance(record, dict) and record.get(connection) == {}:
        del record[connection]
    if record == {}:
        del saved[provider]
    if not saved:
        del tts['provider_saved']


def parked_instructions(tts, provider=None, connection=None):
    """Delivery instructions kept for this provider while its model or voice cannot use them, or ''."""
    if provider is None:
        provider, connection = effective_provider(tts)
    slot = _park_slot(tts, provider, connection or '')
    return slot.get('instructions') if slot and isinstance(slot.get('instructions'), str) else ''


def _park(tts, provider, connection, supported):
    """Delivery instructions are sent only where they are honoured (the speech gateway refuses them otherwise), yet typed text is
    never lost: unusable text waits in the provider's stash and comes back when a model that honours it is chosen."""
    if 'instructions' not in connection_fields(provider, connection):
        return
    active = tts.get('instructions') or ''
    slot = _park_slot(tts, provider, connection)
    parked = slot.get('instructions') if slot and isinstance(slot.get('instructions'), str) else ''
    if supported:
        if not active and parked:
            tts['instructions'] = parked
        if slot is not None and 'instructions' in slot:
            del slot['instructions']
            _prune(tts, provider, connection)
    elif active:
        _park_slot(tts, provider, connection, create=True)['instructions'] = active
        tts['instructions'] = ''


def normalize(config):
    """The derived speech fields, written on every settings write (setup_store.normalize) and mirrored by normalizeTts in the
    browser: for a chosen provider its connection, its runtime backend, its default address when none is set and no PCM toggle
    where it has no PCM stream; for every configuration `capabilities.instruction_control`, which is true exactly where delivery
    instructions are honoured, and the instructions themselves, which wait in the stash where they are not. An older
    configuration (no provider) keeps its backend and everything else. Returns a new configuration; raises ValueError for a
    declaration that is not true or false."""
    config = copy.deepcopy(config)
    tts = config['roles']['tts']
    if 'accepts_instructions' in tts and type(tts['accepts_instructions']) is not bool:
        raise ValueError('Whether the speech server accepts delivery instructions must be true or false')
    caps = tts.setdefault('capabilities', {})
    provider, connection = effective_provider(tts)
    if (tts.get('provider') or '') in PROVIDERS:
        spec = PROVIDERS[provider]
        if tts.get('connection', '') != connection:
            tts['connection'] = connection
        backend = backend_for(provider, connection)
        if tts.get('backend') != backend:
            tts['backend'] = backend
        if 'url' in connection_fields(provider, connection) and not tts.get('url') and spec['default_url']:
            tts['url'] = spec['default_url']
        if not pcm_supported(provider, connection) and 'native_pcm' in caps:
            del caps['native_pcm']
    supported = instructions_supported(tts, provider, connection)
    _park(tts, provider, connection, supported)
    if supported:
        if caps.get('instruction_control') is not True:
            caps['instruction_control'] = True
    elif 'instruction_control' in caps:
        del caps['instruction_control']
    return config


def readiness_rows(config):
    """Speech-output rows for the settings readiness list. Pure: never contacts a provider, reads a key or a toggle. Whether the
    connection itself was verified is the `tts_connection` row's job."""
    tts = config['roles']['tts']
    provider, connection = effective_provider(tts)
    spec = PROVIDERS[provider]
    missing, notes = [], []
    if provider == 'kokoro_native':
        notes.append('Speaks with Agent Zero’s own Kokoro voice settings.')
    elif connection == 'managed':
        notes.append('Convo’s own audio.cpp runtime with its Voice Studio voices.')
        if not (config.get('sidecar') or {}).get('managed'):
            missing.append('Convo’s managed runtime is off: turn on “Manage runtime” (audio.cpp, Managed by Convo) and save')
    else:
        if 'url' in connection_fields(provider, connection) and not tts.get('url'):
            missing.append('a server URL' + (' (Unsloth Studio has no documented default address)' if provider == 'unsloth_studio' else ''))
        if spec['group'] == 'cloud' and not tts.get('remote'):
            missing.append('your consent to send replies to ' + spec['label'] + ' to be spoken')
        model, voice = tts.get('model') or '', tts.get('voice') or ''
        if spec['models'] and model and model not in spec['models']:
            missing.append(model + ' is not one of ' + spec['label'] + '’s speech models')
        elif spec['models'] and model and voice and voice not in spec['models'][model]:
            missing.append('the voice ' + voice + ' is not offered by ' + model)
        if spec['verify'] == 'speech_route' and provider != 'openai_compatible':
            notes.append(spec['label'] + ' has no documented OpenAI-style speech route: Test connection shows what your server answers.')
    if not tts.get('provider'):
        notes.append('Derived from the saved speech backend; choose a provider in Speech output to confirm it.')
    detail = choice_label(provider, connection) + '. ' + (('Still needed: ' + '; '.join(missing) + '. ') if missing else '') + ' '.join(notes)
    rows = [{'id': 'speech_provider', 'label': 'Speech provider', 'state': 'missing' if missing else 'ready', 'detail': detail.strip()}]
    offer = legacy_audiocpp_offer(tts)
    if offer:
        rows.append({'id': 'speech_provider_offer', 'label': 'Speech provider suggestion', 'state': 'unverified',
                     'detail': offer + ' answers on the port of your audio.cpp container. Choose audio.cpp (External server) in Speech output to use it '
                               'as that provider; its address, model and voice are kept. Nothing changes until you confirm.'})
    parked = parked_instructions(tts, provider, connection)
    if parked:
        rule = per(spec['instructions'], connection)
        why = ('they work only with ' + ', '.join(rule) if isinstance(rule, (list, tuple))
               else 'this server is not marked as accepting them, or a Base clone model or clone voice is selected' if rule == 'declared'
               else choice_label(provider, connection) + ' does not use them')
        rows.append({'id': 'speech_instructions', 'label': 'Voice delivery', 'state': 'idle',
                     'detail': 'Your delivery instructions are kept but not sent: ' + why + '.'})
    return rows


def _toggle(name):
    try:
        from helpers import plugins
        return plugins.get_toggle_state(name)
    except Exception:
        return 'unknown'


def kokoro_summary():
    """The native Kokoro choices as the host holds them: voice mix, speed and device. No credentials, no writes."""
    try:
        from . import config
        cfg = config.get_effective_config()['tts']['kokoro']
    except Exception:
        return None
    weights = cfg.get('voice_weights') or {}
    total = sum(weights.values()) or 1
    voices = [{'id': voice, 'share': round(weight / total, 3)} for voice, weight in weights.items()] or [{'id': cfg.get('voice') or '', 'share': 1.0}]
    return {'voices': voices, 'speed': cfg.get('speed'), 'device': cfg.get('device'), 'remote': bool(cfg.get('remote_enabled'))}


def whisper_summary():
    try:
        from . import config
        cfg = config.get_effective_config()['stt']['whisper']
    except Exception:
        return None
    return {key: cfg.get(key) for key in ('model_size', 'device', 'language', 'message_mode')}


def describe(convo_config):
    """Descriptors for every backend plus the native summaries. Pure reads of configuration and toggle state."""
    sidecar = (convo_config or {}).get('sidecar', {})
    kokoro_on = _toggle('_kokoro_tts')
    descriptors = []
    for name, spec in BACKENDS.items():
        if name == 'host_kokoro':
            available = kokoro_on == 'enabled'
            reason = '' if available else 'Native Kokoro is off or missing in this host. Turn it on in Plugins to use it.'
        elif name == 'managed_audio':
            available = bool(sidecar.get('managed'))
            reason = '' if available else 'Convo’s managed runtime is off: turn on “Manage runtime” (audio.cpp, Managed by Convo) to use Convo-owned audio.'
        else:
            available, reason = True, ''
        descriptors.append({'id': name, 'label': spec['label'], 'kind': 'tts', 'owner': spec['owner'], 'settings_owner': spec['settings_owner'],
                            'source': 'adapter', 'voices': spec['voices'], 'controls': list(spec['controls']), 'available': available, 'reason': reason})
    stt_toggle = _toggle('_whisper_stt')
    return {'providers': descriptors,
            'native': {'kokoro': {'plugin': '_kokoro_tts', 'enabled': kokoro_on == 'enabled', 'summary': kokoro_summary()},
                       'whisper': {'plugin': '_whisper_stt', 'enabled': stt_toggle == 'enabled', 'summary': whisper_summary()}}}


def validate_saved(saved):
    """roles.tts.saved: a bounded stash of per-backend fields. Returns the cleaned stash or raises ValueError."""
    if saved is None:
        return {}
    if not isinstance(saved, dict) or set(saved) - set(STASHED_BACKENDS):
        raise ValueError('Saved speech settings must name only backends that own fields')
    cleaned = {}
    for name, values in saved.items():
        if not isinstance(values, dict) or set(values) - set(BACKENDS[name]['fields']):
            raise ValueError('Saved speech settings for a backend may only hold that backend\'s own fields')
        for field, value in values.items():
            if field == 'remote':
                if type(value) is not bool:
                    raise ValueError('Saved remote consent must be boolean')
            elif not isinstance(value, str) or len(value) > 2000:
                raise ValueError('Saved speech setting values must be bounded text')
        cleaned[name] = dict(values)
    return cleaned


def _valid_value(name, field, value):
    if field in BOOLEAN_FIELDS:
        return type(value) is bool
    if field == 'connection':
        return isinstance(value, str) and value in PROVIDERS[name]['connections']
    if field == 'key_source':
        return isinstance(value, str) and value in KEY_SOURCES
    return isinstance(value, str) and len(value) <= 2000


def validate_provider_saved(saved):
    """roles.tts.provider_saved: a bounded stash of each catalog provider's own fields. audio.cpp keeps one record per connection
    ({'external': {...}, 'managed': {...}}, plus the connection it was left on); an earlier flat audio.cpp record is still accepted.
    Returns the cleaned stash or raises ValueError. The legacy per-backend stash (`saved`) is separate and untouched."""
    if saved is None:
        return {}
    if not isinstance(saved, dict) or set(saved) - set(STASHED_PROVIDERS):
        raise ValueError('Saved speech provider settings must name only catalog providers that own fields')
    cleaned = {}
    for name, values in saved.items():
        if not isinstance(values, dict) or set(values) - set(PROVIDER_FIELDS[name]):
            raise ValueError('Saved settings for a speech provider may only hold that provider\'s own fields')
        for field, value in values.items():
            if field in PROVIDERS[name]['connections']:
                if not isinstance(value, dict) or set(value) - set(connection_fields(name, field)):
                    raise ValueError('Saved settings for ' + name + ' (' + field + ') may only hold that connection\'s own fields')
                for inner, item in value.items():
                    if not _valid_value(name, inner, item):
                        raise ValueError('Invalid saved speech provider value: ' + name + '.' + field + '.' + inner)
            elif not _valid_value(name, field, value):
                raise ValueError('Invalid saved speech provider value: ' + name + '.' + field)
        cleaned[name] = copy.deepcopy(values)
    return cleaned
