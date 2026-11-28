"""Optional real-Gradio integration checks; local ASGI only, no model services.

Run in the approved isolated Studio environment. Absence of Gradio is a skip,
not proof of UI compatibility. Every audio/profile operation is stubbed and all
outbound socket connections are refused for the duration of these tests.
"""
import importlib.util
import io
import os
import re
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urljoin, urlsplit


@unittest.skipIf(importlib.util.find_spec('gradio') is None, 'Optional Gradio dependency not installed')
class GradioIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='convo-gradio-test-')
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'GRADIO_ANALYTICS_ENABLED':'False', 'HF_HUB_OFFLINE':'1',
            'TRANSFORMERS_OFFLINE':'1', 'GRADIO_TEMP_DIR':self.tmp.name,
            'CONVO_SIDECAR_TOKEN':'isolated-test-token-not-a-real-secret',
        })
        self.env.start(); self.addCleanup(self.env.stop)
        # In-memory ASGI transports need no TCP sockets. This also denies an
        # accidental provider call if a future callback loses its stub.
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden in Studio integration tests'))
        self.network.start(); self.addCleanup(self.network.stop)
        from fastapi import FastAPI
        import httpx
        from plugins._convo.sidecar import gradio_studio
        from plugins._convo.sidecar.studio_adapter import StudioAdapter

        self.calls = []
        async def control(operation, payload=None, **kwargs):
            self.calls.append((operation, payload, kwargs))
            if operation == 'profiles':
                return {'voices':[{'id':'test-voice', 'voice':'clone:test-voice', 'name':'Test voice', 'task_type':'Base', 'language':'English'}]}
            if operation == 'models': return {'loaded_models':[], 'state':'unloaded'}
            if operation == 'catalog': return {'models':[{'id':'shared-test-model'}]}
            raise AssertionError('Unexpected model/profile operation: ' + str(operation))
        self.preview = AsyncMock(side_effect=AssertionError('Preview must be explicitly tested, never run during UI loading'))
        adapter = StudioAdapter(self.tmp.name, control=control, preview=self.preview)
        self.app = FastAPI()
        with patch.object(gradio_studio, 'StudioAdapter', return_value=adapter):
            gradio_studio.mount(self.app, self.tmp.name, self.preview)
        self.blocks = next(route.app.get_blocks() for route in self.app.routes if getattr(route, 'path', None) == '/convo-studio')
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
            base_url='https://convo.example.test', follow_redirects=False,
            headers={'Authorization':'Bearer isolated-test-token-not-a-real-secret',
                     'Host':'convo.example.test', 'X-Forwarded-Host':'convo.example.test',
                     'X-Forwarded-Proto':'https', 'X-Convo-TTS-Model':'shared-test-model',
                     'X-Convo-TTS-Tuning-Profile':'custom'})
        self.addAsyncCleanup(self.client.aclose)

    async def test_build_metadata_is_nonqueued_private_and_base_only(self):
        config = self.blocks.get_config_file()
        dependencies = config['dependencies']
        self.assertGreaterEqual(len(dependencies), 15)
        for dependency in dependencies:
            self.assertIs(dependency['queue'], False)
            self.assertEqual(dependency.get('api_visibility'), 'private')
        components = config['components']
        # Gradio 6 HTML evaluates its template strings. The host CSP deliberately
        # excludes unsafe-eval, so even a static HTML component breaks rendering.
        self.assertFalse(any(item['type'] == 'html' for item in components))
        header = next(item for item in components if item['props'].get('elem_id') == 'header')
        self.assertEqual(header['type'], 'markdown')
        self.assertIn('# Voice Studio', header['props']['value'])
        tabs = [item['props'].get('label') for item in components if item['type'] == 'tabitem']
        self.assertEqual(tabs, ['Create', 'Library', 'Playground', 'Tuning'])
        reference = next(item['props'] for item in components if item['props'].get('label', '').startswith('Maximum reference seconds'))
        self.assertEqual(reference['minimum'], 1); self.assertEqual(reference['maximum'], 30)
        for item in components:
            if item['type'] == 'audio': self.assertFalse(item['props'].get('autoplay'))
        self.assertEqual(self.calls, []); self.preview.assert_not_called()

    async def test_authenticated_page_config_and_asset_stay_on_public_origin(self):
        page = await self.client.get('/convo-studio/')
        self.assertEqual(page.status_code, 200, page.text[:500])
        self.assertIn('text/html', page.headers['content-type'])
        config_response = await self.client.get('/convo-studio/config')
        self.assertEqual(config_response.status_code, 200, config_response.text[:500])
        config = config_response.json()
        root = urlsplit(config['root'])
        self.assertEqual((root.scheme, root.netloc, root.path.rstrip('/')),
                         ('https', 'convo.example.test', '/convo-studio'))
        self.assertNotIn('isolated-test-token', config_response.text)
        self.assertNotIn('127.0.0.1', config_response.text)
        asset_links = re.findall(r'(?:src|href)=["\']([^"\']*(?:assets|static)/[^"\']+)["\']', page.text)
        self.assertTrue(asset_links, 'Real Studio document must reference its built frontend assets')
        asset_url = urljoin(str(page.url), asset_links[0])
        parsed = urlsplit(asset_url)
        self.assertEqual(parsed.netloc, 'convo.example.test')
        self.assertTrue(parsed.path.startswith('/convo-studio/'), parsed.path)
        asset = await self.client.get(asset_url)
        self.assertEqual(asset.status_code, 200)
        self.assertGreater(len(asset.content), 0)
        self.assertEqual(self.calls, []); self.preview.assert_not_called()

    async def test_every_surface_rejects_missing_authorization(self):
        for path, method in (('/convo-studio/', 'GET'), ('/convo-studio/config', 'GET'),
                             ('/convo-studio/assets/example.js', 'GET'),
                             ('/convo-studio/gradio_api/upload', 'POST'),
                             ('/convo-studio/gradio_api/run/predict', 'POST')):
            response = await self.client.request(method, path, headers={'Authorization':''})
            self.assertEqual(response.status_code, 401, path)
        self.assertEqual(self.calls, [])

    async def test_private_nonqueued_refresh_callback_executes_by_numeric_identity(self):
        index = next(index for index, fn in self.blocks.fns.items() if getattr(fn.fn, '__name__', '') == 'refresh')
        response = await self.client.post('/convo-studio/gradio_api/run/predict',
            json={'fn_index':index, 'data':[], 'session_hash':'isolated-session'})
        self.assertEqual(response.status_code, 200, response.text[:1000])
        data = response.json()['data']
        self.assertEqual(data[0]['data'][0][0], 'test-voice')
        self.assertEqual(self.calls, [('profiles', None, {})])
        self.preview.assert_not_called()

    async def test_authenticated_upload_is_private_bounded_artifact(self):
        out = io.BytesIO()
        with wave.open(out, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
            wav.writeframes(b'\0\0' * 2400)
        response = await self.client.post('/convo-studio/gradio_api/upload',
            files={'files':('reference.wav', out.getvalue(), 'audio/wav')})
        self.assertEqual(response.status_code, 200, response.text[:1000])
        uploaded = Path(response.json()[0]).resolve()
        self.assertTrue(uploaded.is_relative_to(Path(self.tmp.name).resolve()))
        self.assertEqual(uploaded.read_bytes(), out.getvalue())
        self.assertEqual(self.calls, []); self.preview.assert_not_called()


if __name__ == '__main__': unittest.main()
