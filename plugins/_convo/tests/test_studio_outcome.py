"""Exercise host lease -> ASGI sidecar -> mocked audio service without sockets."""
import os
import tempfile
import types
import unittest
from unittest.mock import patch

import httpx

from plugins._convo.helpers import convo_service, convo_transport, lifecycle, studio_admission
from plugins._convo.sidecar.app import app


class BrokenBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"error":'
        raise httpx.RemoteProtocolError('test-only incomplete upstream body')


class StudioOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.current = convo_service.Service(root=temporary.name, host=types.SimpleNamespace())
        self.addCleanup(self.current.store.close)
        self.path = self.current.studio_admission_path()
        self.requests = []
        self.upstream = lambda request: httpx.Response(200, json={'done': True})
        self.asgi = httpx.ASGITransport(app=app)
        self.addAsyncCleanup(self.asgi.aclose)
        token = 'test-only-never-real-studio-token'
        for item in (
            patch.dict(os.environ, {'CONVO_SIDECAR_TOKEN': token, 'CONVO_AUDIO_TOKEN': token}),
            patch.object(lifecycle, 'enabled', return_value=True),
            patch.object(convo_service, 'service', return_value=self.current),
        ):
            item.start()
            self.addCleanup(item.stop)
        original_client = httpx.AsyncClient
        transport = httpx.MockTransport(self.route)
        clients = patch.object(httpx, 'AsyncClient', side_effect=lambda **kwargs: original_client(transport=transport, **kwargs))
        clients.start()
        self.addCleanup(clients.stop)
        self.sidecar = convo_transport.Sidecar({'url': 'http://127.0.0.1:8090', 'token_env': 'CONVO_SIDECAR_TOKEN'})

    async def route(self, request):
        if request.url.host == '127.0.0.1':
            return await self.asgi.handle_async_request(request)
        if request.url.host != 'convo-audio':
            raise AssertionError('Unexpected network destination')
        self.requests.append(request)
        return self.upstream(request)

    async def operation(self, payload=None):
        return await convo_service.studio_operation(self.sidecar, payload or {
            'operation': 'load', 'payload': {'model_key': 'test-model'}, 'confirmed': True,
        }, 'POST')

    async def assert_unknown(self, status):
        with self.assertRaises(httpx.HTTPStatusError) as result:
            await self.operation()
        self.assertEqual(result.exception.response.status_code, status)
        self.assertIn('unconfirmed', result.exception.response.text)
        self.assertNotIn('test-only', result.exception.response.text)
        self.assertTrue(studio_admission.status(self.path)['blocked'])
        self.assertFalse(self.current.studio_busy)
        self.assertEqual(len(self.requests), 1)
        with self.assertRaisesRegex(ValueError, 'reconcile'):
            await self.operation()
        self.assertEqual(len(self.requests), 1)

    async def test_dispatched_timeout_survives_sidecar_error_mapping(self):
        def timeout(request):
            raise httpx.ReadTimeout('test-only private provider detail', request=request)
        self.upstream = timeout
        await self.assert_unknown(504)

    async def test_upstream_server_failure_remains_uncertain(self):
        self.upstream = lambda request: httpx.Response(500, json={'error': 'test-only private detail'})
        await self.assert_unknown(502)

    async def test_malformed_success_remains_uncertain(self):
        self.upstream = lambda request: httpx.Response(200, content=b'not-json')
        await self.assert_unknown(502)

    async def test_oversized_success_is_not_a_pre_dispatch_contract_rejection(self):
        self.upstream = lambda request: httpx.Response(200, content=b'x' * (12 * 1024**2 + 1))
        await self.assert_unknown(502)

    async def test_incomplete_rejection_body_remains_uncertain(self):
        self.upstream = lambda request: httpx.Response(409, stream=BrokenBody())
        await self.assert_unknown(502)

    async def test_pre_dispatch_validation_releases_lease_without_upstream_request(self):
        for payload in (
            {'operation': 'load', 'confirmed': False},
            {'operation': 'not-supported', 'confirmed': True},
            {'operation': 'load', 'payload': [], 'confirmed': True},
            {'operation': 'download', 'payload': {'action': []}, 'confirmed': True},
            {'operation': 'edit_profile', 'item_id': '../private', 'confirmed': True},
        ):
            with self.subTest(payload=payload), self.assertRaises(httpx.HTTPStatusError) as result:
                await self.operation(payload)
            self.assertEqual(result.exception.response.status_code, 400)
            self.assertFalse(studio_admission.status(self.path)['blocked'])
        self.assertEqual(self.requests, [])

    async def test_complete_upstream_rejection_releases_lease(self):
        self.upstream = lambda request: httpx.Response(409, json={'error': 'conflicting revision'})
        with self.assertRaises(httpx.HTTPStatusError) as result:
            await self.operation()
        self.assertEqual(result.exception.response.status_code, 409)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(studio_admission.status(self.path)['blocked'])

    async def test_complete_success_releases_lease(self):
        self.assertEqual(await self.operation(), {'done': True})
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(studio_admission.status(self.path)['blocked'])


if __name__ == '__main__':
    unittest.main()
