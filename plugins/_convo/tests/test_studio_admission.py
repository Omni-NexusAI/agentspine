import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from plugins._convo.helpers import convo_service, lifecycle, studio_admission


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.current = convo_service.Service(root=self.temp.name, host=types.SimpleNamespace())
        self.addCleanup(self.current.store.close)
        self.path = self.current.studio_admission_path()
        self.enabled = patch.object(lifecycle, 'enabled', return_value=True)
        self.enabled.start(); self.addCleanup(self.enabled.stop)
        self.instance = patch.object(convo_service, 'service', return_value=self.current)
        self.instance.start(); self.addCleanup(self.instance.stop)

    def test_preflight_failure_and_reads_do_not_record_dispatch(self):
        self.assertFalse(studio_admission.status(self.path)['blocked'])
        self.assertFalse(self.path.exists())
        with self.assertRaises(ValueError):
            with convo_service.studio_request_guard('POST', '/studio'):
                raise ValueError('local validation failed')
        self.assertFalse(self.path.exists())
        with convo_service.studio_request_guard('GET', '/studio') as lease:
            lease.dispatched(); lease.completed()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.current.studio_busy)

    def test_success_acknowledges_only_the_dispatched_operation(self):
        with convo_service.studio_request_guard('POST', '/studio') as lease:
            lease.dispatched()
            self.assertTrue(studio_admission.status(self.path)['blocked'])
            with self.assertRaises(ValueError): self.current.start_session('owner', 'chat')
            with self.assertRaises(ValueError): studio_admission.completed(self.path, 'wrong-operation')
            self.assertTrue(studio_admission.status(self.path)['blocked'])
            lease.completed()
        self.assertFalse(studio_admission.status(self.path)['blocked'])
        self.current.start_session('owner', 'chat')

    async def test_direct_transport_timeout_blocks_writes_but_not_status_or_stop(self):
        sidecar = types.SimpleNamespace(studio=AsyncMock(side_effect=httpx.ReadTimeout('test-only')))
        with self.assertRaises(httpx.ReadTimeout):
            await convo_service.studio_operation(sidecar, {'operation': 'load'}, 'POST')
        self.assertFalse(self.current.studio_busy)
        self.assertTrue(studio_admission.status(self.path)['blocked'])
        with self.assertRaisesRegex(ValueError, 'reconcile'):
            await convo_service.studio_operation(sidecar, {'operation': 'load'}, 'POST')
        self.assertEqual(sidecar.studio.await_count, 1)
        with convo_service.studio_request_guard('GET', '/studio'):
            pass
        with convo_service.studio_request_guard('POST', '/runtime/stop', allow_uncertain=True):
            pass
        self.assertTrue(studio_admission.status(self.path)['blocked'])

    async def test_direct_success_and_definitive_rejection_release_marker(self):
        sidecar = types.SimpleNamespace(studio=AsyncMock(return_value={'done': True}))
        self.assertEqual(await convo_service.studio_operation(sidecar, {}, 'POST'), {'done': True})
        self.assertFalse(studio_admission.status(self.path)['blocked'])
        request = httpx.Request('POST', 'http://test.invalid/studio')
        response = httpx.Response(409, request=request, json={'error': 'rejected before work'})
        sidecar.studio.side_effect = httpx.HTTPStatusError('test-only', request=request, response=response)
        with self.assertRaises(httpx.HTTPStatusError):
            await convo_service.studio_operation(sidecar, {}, 'POST')
        self.assertFalse(studio_admission.status(self.path)['blocked'])

    async def test_cancellation_retains_uncertainty(self):
        sidecar = types.SimpleNamespace(studio=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await convo_service.studio_operation(sidecar, {}, 'POST')
        self.assertTrue(studio_admission.status(self.path)['blocked'])

    async def test_invalid_local_json_never_marks_or_submits(self):
        sidecar = types.SimpleNamespace(studio=AsyncMock())
        with self.assertRaises(ValueError):
            await convo_service.studio_operation(sidecar, {'value': float('nan')}, 'POST')
        self.assertFalse(self.path.exists())
        sidecar.studio.assert_not_called()

    async def test_opaque_direct_server_failure_retains_uncertainty(self):
        request = httpx.Request('POST', 'http://test.invalid/studio')
        response = httpx.Response(504, request=request, json={'error': 'upstream timeout'})
        error = httpx.HTTPStatusError('test-only', request=request, response=response)
        sidecar = types.SimpleNamespace(studio=AsyncMock(side_effect=error))
        with self.assertRaises(httpx.HTTPStatusError):
            await convo_service.studio_operation(sidecar, {}, 'POST')
        self.assertTrue(studio_admission.status(self.path)['blocked'])

    def test_off_module_refresh_and_new_service_preserve_uncertainty(self):
        with convo_service.studio_request_guard('POST', '/studio') as lease:
            lease.dispatched()
        self.current.deactivate()
        self.assertTrue(studio_admission.status(self.path)['blocked'])
        # Exercise the actual retained-service class-upgrade path in a fresh
        # module namespace, without loading the host or starting its watcher.
        name = 'plugins._convo.helpers._admission_reload_test'
        spec = importlib.util.spec_from_file_location(name, Path(convo_service.__file__))
        refreshed = importlib.util.module_from_spec(spec); spec.loader.exec_module(refreshed)
        runtime = types.ModuleType('helpers.runtime'); runtime._convo_service = self.current
        host = types.ModuleType('helpers'); host.runtime = runtime
        control = types.SimpleNamespace(lock=threading.RLock(), blocked=False, disabling=False, register=Mock())
        with patch.dict(sys.modules, {'helpers': host, 'helpers.runtime': runtime}), patch.object(lifecycle, 'state', return_value=control):
            retained = refreshed.service()
        self.assertIs(retained, self.current)
        with self.assertRaisesRegex(ValueError, 'reconcile'): retained.start_session('owner', 'chat')
        restarted = convo_service.Service(root=self.temp.name, host=types.SimpleNamespace())
        try:
            with self.assertRaisesRegex(ValueError, 'reconcile'): restarted.start_session('owner', 'chat')
        finally:
            restarted.store.close()

    def test_corrupt_marker_fails_closed(self):
        self.path.write_text('{broken', encoding='utf-8')
        status = studio_admission.status(self.path)
        self.assertTrue(status['blocked'])
        self.assertFalse(status['reconciliation_available'])


if __name__ == '__main__': unittest.main()
