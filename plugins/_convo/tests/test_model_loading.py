"""A voice session made ready before it listens: a model that a router reports as not loaded is loaded (the router's own
POST /models/load) and waited for, saying what it waits for; a server that reports no status is not waited for. The gateway runs
on Agent Zero's own Python, says why a model call failed in Convo's words, and Convo's own runtime never keeps a hidden external
gateway address or consent that would block a save."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from plugins._convo.helpers import convo_models, managed_runtime, setup_store
from plugins._convo.helpers.convo_contract import validate_settings
from plugins._convo.sidecar import app as gateway
from plugins._convo.sidecar import web


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class Loading(unittest.IsolatedAsyncioTestCase):
    def test_a_router_reports_each_models_state(self):
        listing = {'data': [{'id': 'Gemma-12b', 'status': {'value': 'unloaded'}}, {'id': 'd1-omni-600m', 'status': {'value': 'loaded'}},
                            {'id': 'plain-model'}]}
        self.assertEqual([convo_models.model_state(listing, name) for name in ('Gemma-12b', 'd1-omni-600m', 'plain-model', 'absent')],
                         ['unloaded', 'loaded', '', ''])

    async def test_an_unloaded_model_is_loaded_and_waited_for_saying_so(self):
        clock, said, posted = Clock(), [], []
        states = iter(['unloaded', 'loading', 'loading', 'loading', 'loading', 'loading', 'loaded'])
        async def notify(message):
            said.append(message)
        def handler(request):
            posted.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, json={'success': True})
        real_client = convo_models.convo_net.client
        with patch.object(convo_models, 'state', new=AsyncMock(side_effect=lambda *args: next(states))), \
                patch.object(convo_models.convo_net, 'client', side_effect=lambda timeout, transport=None: real_client(timeout, httpx.MockTransport(handler))):
            waited = await convo_models.ensure_loaded('conversation model', 'http://desktop-example:8098/v1', {}, 'Gemma-12b', notify,
                                                      sleep=clock.sleep, clock=clock)
        self.assertEqual(posted, [('http://desktop-example:8098/models/load', {'model': 'Gemma-12b'})])   # the router's own route
        self.assertEqual(waited, 6.0)
        self.assertEqual(said[0], 'Loading Gemma-12b for the conversation model on desktop-example:8098…')
        self.assertTrue(any(message.endswith(' 5 s') for message in said[1:]))                            # progress while waiting

    async def test_a_loaded_model_or_a_server_without_status_is_not_waited_for(self):
        for reported in ('loaded', ''):
            notify = AsyncMock()
            with patch.object(convo_models, 'state', new=AsyncMock(return_value=reported)):
                self.assertEqual(await convo_models.ensure_loaded('classifier', 'http://h:1/v1', {}, 'm', notify), 0.0)
            notify.assert_not_called()

    async def test_a_load_that_never_finishes_is_refused_in_words(self):
        clock = Clock()
        with patch.object(convo_models, 'state', new=AsyncMock(return_value='loading')):
            with self.assertRaises(ValueError) as raised:
                await convo_models.ensure_loaded('classifier', 'https://localhost:18201/v1', {}, 'd1-omni-600m', AsyncMock(),
                                                 wait=30, sleep=clock.sleep, clock=clock)
        self.assertIn('did not finish loading on localhost:18201 within 30 s', str(raised.exception))

    def test_two_roles_on_one_router_are_noticed(self):
        self.assertTrue(convo_models.same_router('http://desktop:8098/v1', 'http://desktop:8098/v1'))
        self.assertFalse(convo_models.same_router('http://desktop:8098/v1', 'https://localhost:18201/v1'))


class Gateway(unittest.IsolatedAsyncioTestCase):
    def test_a_failed_model_call_is_said_in_convos_words(self):
        self.assertIn('refused the key (HTTP 401)', gateway.provider_status(401))
        self.assertIn('ends with /v1', gateway.provider_status(404))
        self.assertIn('HTTP 500', gateway.provider_status(500))

    async def test_the_starlette_gateway_refuses_a_body_that_is_not_an_object_and_keeps_its_errors(self):
        app = web.App()
        async def allowed(request):
            if request.headers.get('authorization') != 'Bearer ok':
                raise web.HTTPException(401, 'Convo sidecar authentication required')
        @app.post('/echo', dependencies=[web.Depends(allowed)])
        async def echo(payload: dict):
            return {'got': payload}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
            denied = await client.post('/echo', json={'a': 1})
            refused = await client.post('/echo', content=b'[1]', headers={'Authorization': 'Bearer ok'})
            fine = await client.post('/echo', json={'a': 1}, headers={'Authorization': 'Bearer ok'})
        self.assertEqual((denied.status_code, denied.json()), (401, {'detail': 'Convo sidecar authentication required'}))
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(fine.json(), {'got': {'a': 1}})

    def test_the_gateway_needs_no_fastapi(self):
        source = (Path(gateway.__file__).parent / 'app.py').read_text(encoding='utf-8')
        self.assertNotIn('from fastapi', source)                 # Agent Zero's own Python runs it (Starlette, uvicorn, httpx)


class OwnRuntime(unittest.TestCase):
    def test_agent_zeros_python_runs_the_gateway_when_convo_has_no_environment_of_its_own(self):
        with patch('importlib.util.find_spec', return_value=object()):
            self.assertEqual(managed_runtime.gateway_python(Path('/nowhere')), Path(sys.executable))
        with patch('importlib.util.find_spec', return_value=None):
            self.assertIsNone(managed_runtime.gateway_python(Path('/nowhere')))

    def test_convos_own_runtime_is_the_default_and_never_keeps_a_hidden_external_gateway(self):
        self.assertTrue(validate_settings({})['sidecar']['managed'])
        held = setup_store._own_runtime({'sidecar': {'managed': True, 'remote': True, 'url': 'https://gateway.example.com'}})
        self.assertEqual((held['sidecar']['remote'], held['sidecar']['url']), (False, 'http://convo-runtime:8090'))
        lan = setup_store._own_runtime({'sidecar': {'managed': True, 'remote': False, 'url': 'http://192.168.1.20:9940'}})
        self.assertEqual(lan['sidecar']['url'], 'http://192.168.1.20:9940')
        external = {'sidecar': {'managed': False, 'remote': True, 'url': 'https://gateway.example.com'}}
        self.assertIs(setup_store._own_runtime(external), external)                    # your own gateway is left as you set it


if __name__ == '__main__':
    unittest.main()
