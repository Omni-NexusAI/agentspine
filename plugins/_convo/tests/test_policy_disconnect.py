"""The legacy HTTP classifier route stops its upstream call when the caller goes away (real ASGI disconnect, mock provider)."""
import asyncio
import json
import os
import time
import unittest
from unittest.mock import patch

import httpx

from plugins._convo.sidecar import app

TOKEN = 'test-only-never-real-policy-token'
ROLE = {'url': 'http://localhost:8080/v1', 'model': 'classifier', 'capabilities': {'audio_input': False}}
PAYLOAD = {'role': ROLE, 'transcript': 'private test words', 'context': {'mode': 'active'}}
ANSWER = {'choices': [{'message': {'content': json.dumps({'speech': 'speak', 'action': 'none', 'confidence': .95, 'explicit_request': True})}}]}


class PolicyDisconnect(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'CONVO_SIDECAR_TOKEN': TOKEN})
        environment.start()
        self.addCleanup(environment.stop)
        self.upstream = {'started': asyncio.Event(), 'cancelled': False, 'finished': False, 'release': asyncio.Event(), 'calls': 0}
        self.original_client = httpx.AsyncClient
        clients = patch.object(httpx, 'AsyncClient', side_effect=lambda **kwargs: self.original_client(transport=httpx.MockTransport(self.provider), **kwargs))
        clients.start()
        self.addCleanup(clients.stop)

    async def provider(self, request):
        self.upstream['calls'] += 1
        self.upstream['started'].set()
        try:
            await self.upstream['release'].wait()
            self.upstream['finished'] = True
            return httpx.Response(200, json=ANSWER)
        except asyncio.CancelledError:
            self.upstream['cancelled'] = True
            raise

    def asgi_request(self, payload=PAYLOAD):
        incoming, outgoing = asyncio.Queue(), []
        body = json.dumps(payload).encode()
        incoming.put_nowait({'type': 'http.request', 'body': body, 'more_body': False})
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}, 'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
                 'path': '/policy', 'raw_path': b'/policy', 'query_string': b'', 'root_path': '',
                 'headers': [(b'host', b'sidecar'), (b'content-type', b'application/json'), (b'authorization', ('Bearer ' + TOKEN).encode()),
                             (b'content-length', str(len(body)).encode())],
                 'server': ('sidecar', 80), 'client': ('127.0.0.1', 12345)}

        async def send(message):
            outgoing.append(message)
        return asyncio.create_task(app.app(scope, incoming.get, send)), incoming, outgoing

    async def test_a_caller_that_leaves_cancels_the_classifier_request_at_once(self):
        caller, incoming, outgoing = self.asgi_request()
        await asyncio.wait_for(self.upstream['started'].wait(), 2)
        started = time.monotonic()
        incoming.put_nowait({'type': 'http.disconnect'})
        await asyncio.wait_for(caller, 2)
        self.assertLess(time.monotonic() - started, 1)
        for _ in range(100):                                                         # the upstream call is torn down, not left running
            if self.upstream['cancelled']:
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.upstream['cancelled'])
        self.assertFalse(self.upstream['finished'])
        self.assertEqual(self.upstream['calls'], 1)

    async def test_a_caller_that_stays_gets_the_decision_unchanged(self):
        caller, incoming, outgoing = self.asgi_request()
        await asyncio.wait_for(self.upstream['started'].wait(), 2)
        self.upstream['release'].set()
        await asyncio.wait_for(caller, 2)
        body = json.loads(b''.join(item.get('body', b'') for item in outgoing if item['type'] == 'http.response.body'))
        self.assertEqual((body['speech'], body['explicit_request']), ('speak', True))
        self.assertFalse(self.upstream['cancelled'])

    async def test_cancelling_the_handler_itself_cancels_the_upstream_call(self):
        caller, incoming, outgoing = self.asgi_request()
        await asyncio.wait_for(self.upstream['started'].wait(), 2)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        for _ in range(100):
            if self.upstream['cancelled']:
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.upstream['cancelled'])


if __name__ == '__main__':
    unittest.main()
