"""One host-owned transaction boundary for Convo and native speech bridges.

Reads never create directories or start runtime work. The lock lives outside the
reloadable plugin namespace; all plugin writers must merge while holding it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from .convo_contract import merge, validate_settings

REVISION = '_convo_revision'
_fallback_lock = threading.RLock()


class Conflict(ValueError):
    pass


def lock():
    try:
        from helpers import runtime
    except ImportError:
        return _fallback_lock
    # setdefault is one GIL-protected insertion even during parallel first reads.
    return vars(runtime).setdefault('_convo_settings_lock', threading.RLock())


def config_path(project_name='', agent_profile=''):
    from helpers import plugins
    return Path(plugins.determine_plugin_asset_path('_convo', project_name or '', agent_profile or '', plugins.CONFIG_FILE_NAME))


def read(path):
    path = Path(path)
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Stored plugin settings must be an object; repair without overwriting them')
    value.pop(REVISION, None)
    return value


def revision(value):
    value = {k: v for k, v in value.items() if k != REVISION}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def settings_revision(value):
    """What Convo's editors share: its own subtree and the shared-save epoch (so a key-only save still advances it).

    Native speech bridges and any other top-level key of the file do not move it: a Kokoro voice changed in Agent
    Zero's speech settings is not a conflict with an open Convo editor.
    """
    return revision({'convo': value.get('convo', {}), '_convo_write_epoch': value.get('_convo_write_epoch', 0)})


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def transaction(path, update, expected=None):
    with lock():
        current = read(path)
        if expected is not None and expected != settings_revision(current):
            raise Conflict('Settings changed in another view. Reload before applying your changes.')
        result = update(copy.deepcopy(current))
        if not isinstance(result, dict):
            raise ValueError('Settings transaction must return an object')
        _check_development_mode(current, result)
        result.pop(REVISION, None)
        atomic_write(path, result)
        return copy.deepcopy(result)


def _check_development_mode(current, result):
    """All settings writers require voice-idle admission for this mode change."""
    old = (current.get('convo') or {}).get('ai_link_development_mode', False)
    new = (result.get('convo') or {}).get('ai_link_development_mode', False)
    if not new or old:
        return
    try:
        from helpers import runtime
    except ImportError:
        return
    service = getattr(runtime, '_convo_service', None)
    if service is None:
        return
    with service.connection_lock:
        if service.connections or getattr(service, 'retiring_connections', {}):
            raise ValueError('End the active Convo voice session before enabling AI Link development mode.')
        with service.store.lock:
            if service.store.db.execute('SELECT 1 FROM sessions WHERE active=1 LIMIT 1').fetchone():
                raise ValueError('End the active Convo voice session before enabling AI Link development mode.')


def native_read(value, project_name='', agent_profile=''):
    result = copy.deepcopy(value or {})
    with lock():
        current = read(config_path(project_name, agent_profile))
        result = merge(result, current)
        result[REVISION] = settings_revision(current)
    return result


def native_save(value, project_name='', agent_profile=''):
    if not isinstance(value, dict):
        raise ValueError('Plugin settings must be an object')
    expected = value.get(REVISION)
    if not isinstance(expected, str):
        raise Conflict('Reload Convo settings before saving; this editor has no revision.')
    changes = copy.deepcopy(value)
    changes.pop(REVISION, None)
    # Internal credentials can never be submitted through the ordinary editor.
    reject_credentials(changes.get('convo', {}))
    # Only Convo's own node is written. A host editor's copy of the native speech aggregates (tts/stt) may be stale and
    # must never revert what the native Kokoro or Whisper settings saved since it was read.
    changes = {'convo': normalize(changes.get('convo', {}))}
    return transaction(config_path(project_name, agent_profile), lambda current: merge(current, changes), expected)


def normalize(value):
    """Validated settings with their derived fields written. The conversation, classifier and speech catalogs derive the
    runtime protocol older code reads (the endpoints, the decision backend, the speech backend) from what was chosen; the
    result is validated again. Every settings writer stores this, never an unnormalized copy."""
    from . import convo_classifier_catalog, convo_model_catalog, speech_providers
    return validate_settings(speech_providers.normalize(convo_classifier_catalog.normalize(
        convo_model_catalog.normalize(validate_settings(value)))))


def reject_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {'api_key', 'apikey', 'authorization', 'token', '_convo_key'}:
                raise ValueError('Use the separate masked API-key control')
            reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_credentials(item)
