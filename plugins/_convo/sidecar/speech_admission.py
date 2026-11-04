"""Durable, single-operation ownership for external speech synthesis.

No IO occurs on import. Only a complete terminal provider response can release
admission; disconnect, timeout, process exit and OFF are not terminal evidence.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import time
import uuid


DEADLINE_SECONDS = 35
RECOVERY = ('Verify the external speech provider has no in-flight synthesis, then explicitly '
            'reconcile the Convo speech admission record. Restarting Convo or turning it OFF '
            'does not prove the provider is idle; automatic reconciliation is unavailable.')
_active = {}
_last = {}


class SpeechAdmissionError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail['reason'])
        self.status_code, self.detail = status_code, detail


class DefinitiveRejection(Exception):
    """Only raise after a complete bounded provider 4xx response was drained."""
    def __init__(self, status_code):
        if not 400 <= status_code < 500:
            raise ValueError('A definitive rejection requires a 4xx response')
        self.status_code = status_code


class UncertainOutcome(Exception):
    def __init__(self, reason='transport_failure'):
        self.reason = reason if reason in ('timeout', 'response_limit', 'provider_failure') else 'transport_failure'


def _path():
    configured = os.environ.get('CONVO_DATA_DIR', '')
    if not configured or not Path(configured).is_absolute():
        raise ValueError('An absolute Convo-owned CONVO_DATA_DIR is required')
    return Path(configured) / 'speech-admission.json'


def _read(path):
    with path.open('rb') as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError('Invalid admission record')
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('operation_id'), str):
        raise ValueError('Invalid admission record')
    uuid.UUID(data['operation_id'])
    if type(data.get('started_at')) not in (float, int) or not math.isfinite(data['started_at']):
        raise ValueError('Invalid admission time')
    return data


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def status():
    """Read-only, sanitized status; never repairs, creates or clears a record."""
    try:
        path = _path()
    except ValueError:
        return {'state': 'configuration_required', 'blocked': True,
                'reason': 'Speech requires an absolute Convo-owned CONVO_DATA_DIR',
                'recovery': 'Configure persistent Convo-owned data storage before speech dispatch.'}
    operation = _active.get(str(path))
    if operation is not None and not operation.task.done():
        return operation.details('draining' if operation.detached else 'running',
                                 'Previous speech synthesis is still running', blocked=True)
    try:
        data = _read(path)
    except FileNotFoundError:
        return {'state': 'idle', 'blocked': False, 'last_operation': _last.get(str(path))}
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return {'state': 'uncertain', 'blocked': True, 'reason': 'Speech admission record cannot be verified',
                'recovery': RECOVERY}
    return {'state': 'uncertain', 'blocked': True, 'operation_id': data['operation_id'],
            'elapsed_ms': max(0, int((time.time() - data['started_at']) * 1000)),
            'reason': 'Previous speech synthesis has no confirmed terminal outcome', 'recovery': RECOVERY}


class Operation:
    def __init__(self, path):
        self.path = path
        self.operation_id = str(uuid.uuid4())
        self.started_at, self.started = time.time(), time.monotonic()
        self.detached = False
        self.detached_event = asyncio.Event()
        self.task = None

    def detach(self):
        self.detached = True
        self.detached_event.set()

    def details(self, state, reason, *, blocked):
        result = {'state': state, 'blocked': blocked, 'operation_id': self.operation_id,
                  'elapsed_ms': max(0, int((time.monotonic() - self.started) * 1000)), 'reason': reason}
        if blocked:
            result['recovery'] = RECOVERY if state == 'uncertain' else 'Wait for the tracked provider operation to finish.'
        return result

    def _record(self):
        return {'version': 1, 'operation_id': self.operation_id, 'started_at': self.started_at}

    def claim(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(self._record(), stream)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(self.path.parent)

    def _terminal(self, state):
        # A different owner or damaged record must never be cleared implicitly.
        if _read(self.path)['operation_id'] != self.operation_id:
            raise ValueError('Speech admission ownership changed')
        self.path.unlink()
        _sync_directory(self.path.parent)
        _last[str(self.path)] = self.details(state, 'Provider operation reached a confirmed terminal response', blocked=False)

    def _uncertain(self, reason):
        # The claim is already durable. Keep it on every unproved outcome;
        # diagnostic persistence failure must never remove or replace the claim.
        _last[str(self.path)] = self.details('uncertain', reason, blocked=True)

    async def _run(self, work):
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                result = await work()
        except DefinitiveRejection as error:
            try:
                self._terminal('rejected')
            except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                self._uncertain('admission_storage_failure')
                raise SpeechAdmissionError(503, self.details('uncertain', 'Speech admission could not be reconciled', blocked=True)) from None
            raise SpeechAdmissionError(error.status_code, self.details('rejected', 'Speech provider rejected the request', blocked=False)) from None
        except asyncio.CancelledError:
            self._uncertain('worker_cancelled')
            raise
        except Exception as error:
            reason = ('timeout' if isinstance(error, TimeoutError) else
                      error.reason if isinstance(error, UncertainOutcome) else 'transport_failure')
            self._uncertain(reason)
            raise SpeechAdmissionError(504 if reason == 'timeout' else 502,
                self.details('uncertain', 'Speech outcome is unconfirmed (' + reason + ')', blocked=True)) from None
        try:
            self._terminal('completed')
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            self._uncertain('admission_storage_failure')
            raise SpeechAdmissionError(503, self.details('uncertain', 'Speech admission could not be reconciled', blocked=True)) from None
        return result

    async def wait(self, request=None):
        """Detach this caller promptly, without cancelling the owned worker."""
        watcher = None
        try:
            if request is None:
                return await asyncio.shield(self.task)

            async def disconnected():
                # FastAPI has already consumed the JSON body. This is the sole
                # remaining receive owner; no body/disconnect polling race.
                while True:
                    if (await request.receive())['type'] == 'http.disconnect':
                        return

            watcher = asyncio.create_task(disconnected())
            done, _ = await asyncio.wait((self.task, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                self.detach()
                raise SpeechAdmissionError(499, self.details('detached', 'Speech caller disconnected; provider operation remains tracked', blocked=True))
            return self.task.result()
        except asyncio.CancelledError:
            self.detach()
            raise
        finally:
            if watcher is not None:
                watcher.cancel()
                watcher.add_done_callback(_consume_exception)


def _consume_exception(task):
    if not task.cancelled():
        task.exception()


def start(work):
    """Claim persistent admission before starting exactly one independent worker."""
    loop = asyncio.get_running_loop()
    current = status()
    if current['blocked']:
        raise SpeechAdmissionError(503 if current['state'] == 'configuration_required' else 409, current)
    operation = Operation(_path())
    try:
        operation.claim()
    except FileExistsError:
        raise SpeechAdmissionError(409, status()) from None
    except OSError:
        raise SpeechAdmissionError(503, {'state': 'uncertain', 'blocked': True,
            'reason': 'Persistent speech admission is unavailable; no speech was dispatched', 'recovery': RECOVERY}) from None
    operation.task = loop.create_task(operation._run(work))
    _active[str(operation.path)] = operation

    def completed(task):
        _consume_exception(task)
        if _active.get(str(operation.path)) is operation:
            _active.pop(str(operation.path), None)

    operation.task.add_done_callback(completed)
    return operation
