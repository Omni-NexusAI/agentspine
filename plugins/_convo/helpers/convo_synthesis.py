"""Advanced synthesis controls for the user's audio.cpp speech server (the controls of the standalone fork's diagnostics panel).

The server accepts, per speech request, a `tuning` object: a profile id with its revision and the profile's effective values (a
snapshot it resolved through POST /v1/tuning/resolve) plus overrides of twelve sampler and decoder keys. Convo never changes the
server's own Realtime selection or creates a profile: it reads the profiles, resolves a choice, and sends the snapshot with each
request of a reply. A blank seed becomes one random seed per reply, so every phrase of an answer shares it.

`roles.tts.tuning` = {profile_id, overrides} is the saved default; a voice session may hold a temporary one (`convo.tune`), applied
from the next reply. Bounds are the server's own; nothing outside them is ever sent.
"""
from __future__ import annotations

import json
import math
import secrets

PROFILE_ID_LIMIT = 128
PROVIDER_TAG = 'qwen3tts-audiocpp'            # the speech server's name for its tuning family (the fork sends the same)
# The values a resolved profile carries into a request's snapshot (the same set Convo's managed runtime freezes, sidecar/studio.py).
EFFECTIVE_FIELDS = frozenset({'model', 'clone_mode', 'max_reference_seconds', 'first_block_frames', 'steady_block_frames',
                              'left_context_frames', 'text_lookahead', 'phrase_flush_ms', 'temperature', 'top_k', 'top_p',
                              'repetition_penalty', 'seed'})
# key: (kind, low, high). Whole numbers for frames, top-k and seed; temperature must be above 0.
BOUNDS = {
    'temperature': ('float', 0.0, 2.0),
    'top_k': ('int', 1, 200),
    'top_p': ('float', 0.05, 1.0),
    'repetition_penalty': ('float', 0.8, 2.0),
    'seed': ('int', 0, 4294967295),
    # Whole seconds: the speech server refuses a fractional reference length (HTTP 422).
    'max_reference_seconds': ('int', 1, 30),
    'first_block_frames': ('int', 1, 300),
    'steady_block_frames': ('int', 1, 300),
    'left_context_frames': ('int', 1, 300),
    # The server's phrase queue (the fork's diagnostics: "Text look-ahead (chars)" and "Safe-clause idle flush (ms)").
    'text_lookahead': ('int', 16, 512),
    'phrase_flush_ms': ('int', 50, 3000),
}
# The browser's start-up buffer before a phrase plays when nothing else is playing (the fork's resolvePlaybackPrimeMs): fixed for
# the built-in profiles, the first block's length for any other (80 ms a frame), at most PRIME_LIMIT.
PRIME_MS = {'low-latency': 800, 'balanced': 1280, 'quality': 1760}
PRIME_LIMIT = 2000
NATIVE_ONLY = ('first_block_frames', 'steady_block_frames', 'left_context_frames')
DEFAULT_CONTEXT = 25


def bounds():
    """The bounds as the browser shows them: {key: [low, high]}."""
    return {key: [low, high] for key, (_, low, high) in BOUNDS.items()}


def validate_overrides(overrides):
    """Overrides with only known keys inside the server's bounds; raises ValueError naming the first problem."""
    if overrides in (None, ''):
        return {}
    if not isinstance(overrides, dict):
        raise ValueError('Synthesis overrides must be an object')
    result = {}
    for key, value in overrides.items():
        if key not in BOUNDS:
            raise ValueError('Unknown synthesis control: ' + str(key)[:40])
        if value is None or value == '':
            continue                                          # blank = the profile's value (a blank seed: new per reply)
        kind, low, high = BOUNDS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(key + ' must be a number')
        if kind == 'int' and value != int(value):
            raise ValueError(key + ' must be a whole number')
        value = int(value) if kind == 'int' else float(value)
        if key == 'temperature' and not 0 < value <= high:
            raise ValueError('temperature must be above 0 and at most %s' % high)
        if not low <= value <= high:
            raise ValueError('%s must be from %s to %s' % (key, low, high))
        result[key] = value
    first, steady = result.get('first_block_frames'), result.get('steady_block_frames')
    if first is not None and steady is not None and steady < first:
        raise ValueError('The steady block must be at least the first block')
    if steady is not None and result.get('left_context_frames', DEFAULT_CONTEXT) + steady > 300:
        raise ValueError('Decoder context plus the steady block must be at most 300 frames')
    return result


def fit_problem(values):
    """Why complete values (a preset's own merged with overrides) would be refused by the speech server at speech time (HTTP 422),
    or ''. The server only checks this combination when it synthesises, so a saved override that clashes with the chosen preset (a
    first block larger than the preset's steady block) would otherwise break every reply."""
    def whole(key):
        value = (values or {}).get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None
    first, steady, context = whole('first_block_frames'), whole('steady_block_frames'), whole('left_context_frames')
    if first is not None and steady is not None and steady < first:
        return 'The first block (%d frames) is larger than the steady block (%d frames)' % (first, steady)
    if steady is not None and (context if context is not None else DEFAULT_CONTEXT) + steady > 300:
        return 'The decoder context plus the steady block is more than 300 frames'
    return ''


def fitting(profile_values, overrides):
    """The overrides that still fit a preset's own values: a block-size override that would clash with them is dropped (switching
    presets clears overrides that no longer fit). Sampler values and the seed always fit."""
    kept = dict(overrides or {})
    for key in ('left_context_frames', 'steady_block_frames', 'first_block_frames'):
        if key in kept and fit_problem({**(profile_values or {}), **kept}):
            kept.pop(key)
    return kept


def validate_tuning(value):
    """{profile_id, overrides} with both checked (an absent value is the empty default)."""
    if value in (None, ''):
        return {'profile_id': '', 'overrides': {}}
    if not isinstance(value, dict) or set(value) - {'profile_id', 'overrides'}:
        raise ValueError('Synthesis tuning must be {profile_id, overrides}')
    profile = value.get('profile_id', '')
    if not isinstance(profile, str) or len(profile) > PROFILE_ID_LIMIT:
        raise ValueError('Invalid synthesis profile id')
    return {'profile_id': profile, 'overrides': validate_overrides(value.get('overrides'))}


def applies(role):
    """Synthesis controls apply to the user's own audio.cpp server only."""
    return isinstance(role, dict) and role.get('provider') == 'audiocpp' and (role.get('connection') or 'external') == 'external'


def reply_tuning(role, session_tuning=None, resolved=None, rng=secrets.randbits):
    """The `tuning` envelope every speech request of one reply carries (the fork's response snapshot): the session's choice, else
    the saved one; one seed for the whole reply in `overrides.seed` (the chosen one, else a new random one), so every phrase of an
    answer is sampled alike; and the profile the server resolved for exactly this choice, with its id, revision and complete values.
    With no profile chosen, that is the server's own Realtime selection as resolved for this reply: the reply is frozen on it rather
    than the server re-reading its selection for every phrase. When nothing was resolved the envelope stays minimal (the chosen
    profile's id, if any, plus the overrides). None when the controls do not apply (only the user's own audio.cpp server)."""
    if not applies(role):
        return None
    chosen = session_tuning if isinstance(session_tuning, dict) else (role.get('tuning') or {})
    overrides = dict(chosen.get('overrides') or {})
    if 'seed' not in overrides:
        overrides['seed'] = rng(32)
    envelope = {'provider': PROVIDER_TAG, 'scope': 'realtime', 'overrides': overrides}
    profile = chosen.get('profile_id') or ''
    if profile:
        envelope['profile_id'] = profile
    if (isinstance(resolved, dict) and resolved.get('profile_revision') is not None and isinstance(resolved.get('effective'), dict)
            and resolved['effective'] and resolved.get('profile_id') and (not profile or resolved.get('profile_id') == profile)):
        envelope['profile_id'] = resolved['profile_id']
        envelope['profile_revision'] = resolved['profile_revision']
        envelope['effective'] = {**resolved['effective'], 'seed': overrides['seed']}
    return envelope


def prime_ms(envelope):
    """The start-up buffer for a reply frozen on `envelope` (PRIME_MS), or 0 when it has no resolved profile."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get('effective'), dict):
        return 0
    profile = str(envelope.get('profile_id') or '').strip().lower().replace('_', '-').replace(' ', '-')
    profile = 'low-latency' if profile in ('low', 'lowlatency') else profile
    if profile in PRIME_MS:
        return PRIME_MS[profile]
    try:
        frames = int(envelope['effective'].get('first_block_frames') or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(PRIME_LIMIT, frames * 80))


async def _call(client, method, url, headers, body=None):
    async with client.stream(method, url, headers=headers, json=body) as response:
        data = b''
        async for chunk in response.aiter_bytes():
            data += chunk
            if len(data) > 512 * 1024:
                raise ValueError('The speech server answer is too large')
        status = response.status_code
    try:
        parsed = json.loads(data or b'null')
    except ValueError:
        parsed = None
    return status, parsed


async def read(role, headers, client_factory):
    """The server's tuning profiles and its own Realtime selection (read-only)."""
    from .convo_contract import endpoint
    async with client_factory(8) as client:
        status, data = await _call(client, 'GET', endpoint(role) + '/tuning/profiles', headers)
    if status != 200 or not isinstance(data, dict):
        raise ValueError('The speech server did not list its tuning profiles (HTTP %s)' % status)
    profiles = data.get('profiles') if isinstance(data.get('profiles'), dict) else {}
    listed = [{'id': key, 'label': str(item.get('name') or item.get('label') or key)[:200], 'revision': item.get('revision')}
              for key, item in profiles.items() if isinstance(item, dict)][:64]
    selected = None
    for holder in ((data.get('selections') or {}).get('realtime') if isinstance(data.get('selections'), dict) else None, data.get('selected')):
        if isinstance(holder, dict):
            for value in holder.values():
                if isinstance(value, str) and value:
                    selected = {'id': value, 'label': next((p['label'] for p in listed if p['id'] == value), value)}
                    break
        if selected:
            break
    return {'profiles': listed, 'selected': selected}


async def resolve(role, headers, client_factory, profile_id, overrides):
    """The server's own resolution of a profile plus overrides (nothing is synthesised): {effective, profile_id, profile_revision}."""
    from .convo_contract import endpoint
    overrides = validate_overrides(overrides)
    body = {'provider': PROVIDER_TAG, 'scope': 'realtime', 'overrides': overrides}
    if profile_id:
        body['profile_id'] = profile_id
    async with client_factory(8) as client:
        status, data = await _call(client, 'POST', endpoint(role) + '/tuning/resolve', headers, body)
    if status != 200 or not isinstance(data, dict):
        detail = data.get('detail') if isinstance(data, dict) and isinstance(data.get('detail'), str) else ''
        raise ValueError('The speech server refused these synthesis values (HTTP %s)%s' % (status, ': ' + detail[:200] if detail else ''))
    # The server answers with the profile it resolved and the overrides it accepted (as Convo's managed runtime reads it).
    profile = data.get('profile') if isinstance(data.get('profile'), dict) else {}
    accepted = data.get('temporaryOverrides') if isinstance(data.get('temporaryOverrides'), dict) else {}
    effective = {key: value for key, value in {**profile, **accepted}.items() if key in EFFECTIVE_FIELDS}
    problem = fit_problem({**effective, **overrides}) if overrides else ''
    if problem:
        raise ValueError(problem + ' for this preset: change or clear that override.')
    return {'effective': effective, 'profile_id': str(profile.get('id') or profile_id or ''), 'profile_revision': profile.get('revision')}
