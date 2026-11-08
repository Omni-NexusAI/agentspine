"""Durable fail-closed marker for synchronous Studio mutation acknowledgments.

No read starts a runtime or creates a directory. Only the request that recorded
an operation may clear it after receiving its complete successful response.
There is deliberately no user-supplied or restart-based reset shortcut.
"""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from .setup_store import atomic_write


FILENAME = 'studio-admission.json'
RECOVERY_MESSAGE = (
    'A Studio operation has no confirmed completion. Convo start and Studio writes are blocked. '
    'Keep Convo stopped, check the Convo-owned audio backend for outstanding work, and reconcile '
    'that operation before retrying. Native OFF and status reads remain available. '
    'Restarting the interface or gateway does not prove the audio backend is idle; '
    'backend-aware reconciliation is not yet available in this build.'
)


def status(path):
    path = Path(path)
    if not path.exists():
        return {'state': 'idle', 'blocked': False, 'message': ''}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(value, dict) and value.get('state') == 'idle':
            return {'state': 'idle', 'blocked': False, 'message': ''}
        identity = value.get('operation_id', '') if isinstance(value, dict) else ''
        identity = str(uuid.UUID(identity))
    except (OSError, ValueError, TypeError, AttributeError):
        identity = ''
    return {'state': 'uncertain', 'blocked': True, 'message': RECOVERY_MESSAGE,
            'operation_id': identity, 'reconciliation_available': False}


def dispatched(path):
    identity = str(uuid.uuid4())
    # Commit before issuing network I/O: even abrupt host shutdown must retain
    # uncertainty. A write failure prevents dispatch, not just acknowledgment.
    atomic_write(path, {'state': 'uncertain', 'operation_id': identity, 'started_at': time.time()})
    return identity


def completed(path, identity):
    current = status(path)
    if not identity or current.get('operation_id') != identity:
        raise ValueError(RECOVERY_MESSAGE)
    atomic_write(path, {'state': 'idle', 'completed_operation_id': identity})
