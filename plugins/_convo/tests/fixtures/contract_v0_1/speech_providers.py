"""Which speech controls each output backend owns, computed for the settings UI and never persisted.

A control is shown only for a backend that has it; a hidden control keeps its saved value. Reading never writes,
starts, loads, downloads or scans anything: availability comes from toggle state and configuration files, and the
native summaries are read straight from the owning plugins' configuration (never their credentials).

Control vocabulary: url, key, remote, discover, model, voice, advanced, tuning, studio, instructions, pcm, native.
The same table is mirrored in webui/defaults.js (the browser fallback before the first read); a contract test keeps
the two identical.
"""
from __future__ import annotations

CONTROLS = ('url', 'key', 'remote', 'discover', 'model', 'voice', 'advanced', 'tuning', 'studio', 'instructions', 'pcm', 'native')

# Fields a backend owns on roles.tts. Switching backends stashes these under roles.tts.saved[<backend>], so a value
# configured for one backend is neither lost nor carried into another (a Qwen clone voice is not an OpenAI voice).
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
            reason = '' if available else 'Turn on the managed runtime (Advanced) to use Convo-owned audio.'
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
