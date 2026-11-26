"""Durable, single-operation ownership for external speech synthesis.

No IO occurs on import. A complete terminal provider response releases admission
at once; a disconnect, a timeout, a process exit and OFF are not such evidence.
An operation without it is settled, never left blocking for good: the provider is
asked how the request ended where it can say (its own request id), and otherwise
the record is released after a grace period no single phrase outlives (`GRACE`,
`RECORD_STALE`). Nothing is ever sent to the provider again for it.
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
# Seconds an operation with no confirmed end keeps its lock. The provider closed the connection or answered an error: it has ended
# the request, so only a moment. Convo gave up waiting or stopped: the closed connection stops most servers; one that goes on queues
# the next request itself.
GRACE = {'provider_failure': 3.0, 'transport_failure': 3.0, 'response_limit': 3.0}
GRACE_UNKNOWN = 20.0
# A record left by a process that is gone: older than any request could still be (the deadline above plus the longest grace).
RECORD_STALE = DEADLINE_SECONDS + GRACE_UNKNOWN + 5
OUTCOME_POLL = 1.0
OUTCOME_ENDED = frozenset({'completed', 'error', 'cancelled', 'limited'})
RECOVERY = 'The lock is released by itself once the provider confirms the request ended, or after a short wait.'
_active = {}
_last = {}
_settling = {}          # path -> (operation id, monotonic time its lock is released at the latest)


class SpeechAdmissionError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail['reason'])
        self.status_code, self.detail = status_code, detail


class DefinitiveRejection(Exception):
    """Only raise after a complete bounded provider response was drained: any 4xx, or a 5xx whose body names a known terminal
    outcome (`reason`, from DEFINITE_STATES), where the provider says its generation already ended."""
    def __init__(self, status_code, reason=''):
        if not (400 <= status_code < 500 or (500 <= status_code < 600 and reason in DEFINITE_STATES | LOCAL_TERMINAL)):
            raise ValueError('A definitive rejection requires a 4xx response or a known terminal provider state')
        self.status_code, self.reason = status_code, reason


# 5xx answers that are nonetheless terminal: the speech server's own words that the generation finished before it could be streamed
# (sidecar/audiocpp/supervisor.py, a phrase too short for two native PCM pieces). The engine is idle, so holding Convo's speech lock
# on them would refuse every later reply. Anything else 5xx stays "outcome unknown".
DEFINITE_STATES = frozenset({'native-stream-not-incremental', 'generation-limit'})
# A complete 2xx answer Convo cannot use (a whole-phrase WAV that is not 16-bit or float mono): the provider finished, so it is
# terminal too, reported as a refusal.
LOCAL_TERMINAL = frozenset({'whole-phrase-format'})
GENERATION_DONE ='audio.cpp generation completed before two native PCM events'


def definite_state(body):
    """The known terminal state a complete provider error body names, or ''."""
    try:
        detail = json.loads(body or b'null')
    except (ValueError, TypeError, RecursionError):
        return ''
    detail = detail.get('detail') if isinstance(detail, dict) else None
    if isinstance(detail, dict) and detail.get('state') in DEFINITE_STATES:
        return detail['state']
    if detail == GENERATION_DONE:
        return 'native-stream-not-incremental'
    return ''


def raise_for_provider(status_code, body):
    """A complete non-2xx provider answer: DefinitiveRejection when it is terminal (any 4xx, or a known terminal 5xx), else
    UncertainOutcome. Only a real transport loss or an unrecognised 5xx keeps the speech lock."""
    if 400 <= status_code < 500:
        raise DefinitiveRejection(status_code)
    reason = definite_state(body) if 500 <= status_code < 600 else ''
    if reason:
        raise DefinitiveRejection(status_code, reason)
    raise UncertainOutcome('provider_failure')


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
                'recovery': RECOVERY, 'clears_in_ms': _clears_in(path, None)}
    return {'state': 'uncertain', 'blocked': True, 'operation_id': data['operation_id'],
            'elapsed_ms': max(0, int((time.time() - data['started_at']) * 1000)),
            'reason': 'Previous speech synthesis has no confirmed terminal outcome', 'recovery': RECOVERY,
            'clears_in_ms': _clears_in(path, data['operation_id'])}


def _clears_in(path, operation_id):
    """Milliseconds until the record at `path` is released at the latest."""
    settling = _settling.get(str(path))
    if settling and settling[0] == operation_id:
        return max(0, int((settling[1] - time.monotonic()) * 1000))
    return max(0, int((RECORD_STALE - _age(path)) * 1000))


def _age(path):
    """Seconds since the record was written (0 when it cannot be read: it then keeps blocking)."""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return 0.0


def expire():
    """Release a record that can no longer stand for a running request: one left by an earlier process, or one whose settling time
    is over. Never touches a running operation or a record still within its time. Returns whether it released one."""
    try:
        path = _path()
    except ValueError:
        return False
    operation = _active.get(str(path))
    if operation is not None and not operation.task.done():
        return False
    settling = _settling.get(str(path))
    try:
        owner = _read(path)['operation_id']
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        owner = None
    if settling and settling[0] == owner:
        if time.monotonic() < settling[1]:
            return False
    elif _age(path) < RECORD_STALE:
        return False
    return _release(path, owner)


def _release(path, owner):
    """Remove the record if it is still `owner`'s (None: an unreadable one)."""
    try:
        current = _read(path)['operation_id']
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        current = None
    if current != owner:
        return False
    try:
        path.unlink()
        _sync_directory(path.parent)
    except OSError:
        return False
    _settling.pop(str(path), None)
    last = _last.get(str(path))
    if isinstance(last, dict):
        _last[str(path)] = {**last, 'state': 'settled', 'blocked': False}
    return True


class Operation:
    def __init__(self, path):
        self.path = path
        self.operation_id = str(uuid.uuid4())
        self.started_at, self.started = time.time(), time.monotonic()
        self.detached = False
        self.detached_event = asyncio.Event()
        self.task = None
        self.outcome = None            # async () -> how the provider says this request ended ('' unknown), once it named the request

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
        # The claim is already durable. It is kept on every unproved outcome and settled in the background: by the provider's own
        # word where it has one, else after the grace period.
        grace = GRACE.get(reason, GRACE_UNKNOWN)
        _settling[str(self.path)] = (self.operation_id, time.monotonic() + grace)
        _last[str(self.path)] = {**self.details('uncertain', reason, blocked=True), 'clears_in_ms': int(grace * 1000)}
        try:
            task = asyncio.get_running_loop().create_task(self._settle())
        except RuntimeError:
            return                     # no loop any more: the next start or health check releases it (expire)
        task.add_done_callback(_consume_exception)

    async def _settle(self):
        while True:
            settling = _settling.get(str(self.path))
            if not settling or settling[0] != self.operation_id:
                return
            if self.outcome is not None:
                try:
                    if await self.outcome() in OUTCOME_ENDED:
                        _release(self.path, self.operation_id)
                        return
                except Exception:
                    pass
            left = settling[1] - time.monotonic()
            if left <= 0:
                _release(self.path, self.operation_id)
                return
            await asyncio.sleep(min(OUTCOME_POLL, left))

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
            detail = self.details('rejected', 'Speech provider rejected the request'
                                  + (' (' + error.reason + ')' if error.reason else ''), blocked=False)
            if error.reason:
                detail['provider_state'] = error.reason
            _last[str(self.path)] = detail
            raise SpeechAdmissionError(error.status_code, detail) from None
        except asyncio.CancelledError:
            self._uncertain('worker_cancelled')
            raise
        except Exception as error:
            reason = ('timeout' if isinstance(error, TimeoutError) else
                      error.reason if isinstance(error, UncertainOutcome) else 'transport_failure')
            self._uncertain(reason)
            raise SpeechAdmissionError(504 if reason == 'timeout' else 502,
                {**self.details('uncertain', 'Speech outcome is unconfirmed (' + reason + ')', blocked=True),
                 'clears_in_ms': int(GRACE.get(reason, GRACE_UNKNOWN) * 1000)}) from None
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
    expire()
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
