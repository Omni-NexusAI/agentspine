import io
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from plugins._convo.helpers.studio_proxy import StudioProxy, safe_path, allowed_route, bootstrap, LIMIT
from plugins._convo.helpers import convo_service, lifecycle, studio_admission

try:
    from flask import Flask
except ImportError:
    Flask = None


class PathTests(unittest.TestCase):
    def test_routes_are_fixed_and_traversal_refused(self):
        self.assertTrue(safe_path('/convo-studio/assets/index.js'))
        for path in ('/elsewhere', '/convo-studio/../x', '/convo-studio/%2e%2e/x', '/convo-studio/file=http://host/x', '/convo-studio/a%5cb', '/convo-studio/a%00'):
            self.assertFalse(safe_path(path), path)

    def test_bootstrap_escapes_script_tokens(self):
        result = bootstrap('</script><img>')
        self.assertNotIn(b'</script><img>', result)
        self.assertIn(b'X-CSRF-Token', result)

    def test_gradio_proxy_dev_queues_and_arbitrary_files_not_exposed(self):
        for path in ('/proxy=http://evil', '/gradio_api/proxy=evil', '/gradio_api/file=/etc/passwd',
                     '/gradio_api/file=/tmp/convo-studio/test.html', '/gradio_api/startup-events', '/gradio_api/vibe-code'):
            self.assertFalse(allowed_route('/convo-studio' + path, 'GET'))
        self.assertTrue(allowed_route('/convo-studio/gradio_api/file=/tmp/convo-studio/a.wav', 'GET'))
        self.assertTrue(allowed_route('/convo-studio/gradio_api/run/predict', 'POST'))


@unittest.skipIf(Flask is None, 'Flask integration dependency not installed in this interpreter')
class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask('studio-test'); self.app.secret_key = 'test-only'
        self.enabled = True
        self.conn = MagicMock(); self.response = self.conn.getresponse.return_value
        self.response.status = 200
        self.mime, self.length = 'text/html', None
        self.response.getheader.side_effect = lambda key, default=None: self.mime if key.lower() == 'content-type' else self.length if key.lower() == 'content-length' else default
        self.response.read.return_value = b'<html><head></head><body>Studio</body></html>'
        self.connect = MagicMock(return_value=self.conn)
        self.destination = MagicMock(return_value=('http://127.0.0.1:49200', 'x'*32))
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.current = convo_service.Service(root=self.temp.name, host=types.SimpleNamespace())
        self.addCleanup(self.current.store.close)
        active = patch.object(lifecycle, 'enabled', return_value=True); active.start(); self.addCleanup(active.stop)
        service = patch.object(convo_service, 'service', return_value=self.current); service.start(); self.addCleanup(service.stop)
        self.selection = MagicMock(return_value={'model': 'shared-model', 'tuning_profile': 'shared-tuning'})
        self.proxy = StudioProxy(self.app, self.app.wsgi_app, enabled=lambda:self.enabled,
            destination=self.destination, guard=convo_service.studio_request_guard,
            model=self.selection, credentials_hash=lambda:'signed-user', connection=self.connect)
        self.app.wsgi_app = self.proxy
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['authentication'] = 'signed-user'; session['csrf_token'] = 'csrf-test'

    def post(self, **kwargs):
        return self.client.post('/convo-studio/gradio_api/api/predict', data='{}', content_type='application/json', headers={'Origin':'http://localhost','X-CSRF-Token':'csrf-test', **kwargs})

    def test_every_method_and_write_rechecks_auth_csrf_origin(self):
        self.assertEqual(self.client.get('/convo-studio/').status_code, 200)
        self.assertEqual(self.client.delete('/convo-studio/').status_code, 405)
        self.assertEqual(self.post(**{'X-CSRF-Token':''}).status_code, 403)
        self.assertEqual(self.post(Origin='http://foreign').status_code, 403)
        with self.client.session_transaction() as session: session.pop('authentication')
        self.assertEqual(self.client.get('/convo-studio/').status_code, 401)

    def test_fixed_destination_no_cookie_forwarding_no_redirects(self):
        response = self.client.get('/convo-studio/')
        self.assertIn(b'X-CSRF-Token', response.data)
        headers = self.conn.request.call_args.kwargs['headers']
        self.assertNotIn('Cookie', headers); self.assertEqual(headers['X-Convo-TTS-Model'], 'shared-model')
        self.assertEqual(headers['X-Convo-TTS-Tuning-Profile'], 'shared-tuning')
        self.assertEqual(headers['Host'], 'localhost')
        self.assertEqual(headers['X-Forwarded-Host'], 'localhost')
        self.assertEqual(headers['X-Forwarded-Proto'], 'http')
        self.selection.assert_called_once()
        self.connect.assert_called_once_with('127.0.0.1', 49200, timeout=190)
        self.response.status = 302
        self.assertEqual(self.client.get('/convo-studio/').status_code, 502)
        self.destination.return_value = ('http://other-host:8080', 'x'*32)
        self.assertEqual(self.client.get('/convo-studio/').status_code, 503)

    def test_off_before_dispatch_and_before_delivery(self):
        self.enabled = False
        self.assertEqual(self.client.get('/convo-studio/').status_code, 403)
        self.connect.assert_not_called()
        self.enabled = True
        def disable(*args): self.enabled = False; return b'<head></head>'
        self.response.read.side_effect = disable
        self.assertEqual(self.client.get('/convo-studio/').status_code, 403)

    def test_queue_and_unbounded_uploads_rejected(self):
        self.assertEqual(self.client.post('/convo-studio/gradio_api/queue/join').status_code, 403)
        self.assertEqual(self.client.post('/convo-studio/gradio_api/call/fn').status_code, 403)
        response = self.client.open('/convo-studio/gradio_api/upload', method='POST',
            headers={'Origin':'http://localhost', 'X-CSRF-Token':'csrf-test'},
            environ_overrides={'CONTENT_LENGTH':str(LIMIT+1)}, input_stream=io.BytesIO(b''))
        self.assertEqual(response.status_code, 413)

    def test_html_file_and_oversized_response_refused(self):
        self.assertEqual(self.client.get('/convo-studio/gradio_api/file=/tmp/untrusted.html').status_code, 403)
        self.response.read.return_value = b'x' * (LIMIT+1)
        self.assertEqual(self.client.get('/convo-studio/').status_code, 502)

    def test_transport_timeout_persists_uncertainty_and_blocks_next_dispatch(self):
        self.conn.getresponse.side_effect = TimeoutError('test-only')
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertIn(b'reconcile', response.data)
        self.assertTrue(self.current.studio_admission_status()['blocked'])
        self.assertFalse(self.current.studio_busy)
        self.conn.getresponse.side_effect = None
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.conn.request.call_count, 1)
        self.current.deactivate()
        self.assertTrue(self.current.studio_admission_status()['blocked'])

    def test_local_validation_never_marks_dispatch(self):
        self.destination.return_value = ('http://foreign.invalid:8080', 'x'*32)
        self.assertEqual(self.post().status_code, 503)
        self.conn.request.assert_not_called()
        self.assertFalse(self.current.studio_admission_path().exists())

    def test_invalid_local_header_never_marks_dispatch(self):
        self.selection.return_value = {'model': 'valid', 'tuning_profile': 'invalid\r\nheader'}
        self.assertEqual(self.post().status_code, 503)
        self.conn.request.assert_not_called()
        self.assertFalse(self.current.studio_admission_path().exists())

    def test_complete_success_and_rejection_acknowledge_but_truncation_does_not(self):
        self.mime = 'application/json'; self.response.read.return_value = b'{}'; self.length = '2'
        self.assertEqual(self.post().status_code, 200)
        self.assertFalse(self.current.studio_admission_status()['blocked'])
        self.response.status = 409
        self.assertEqual(self.post().status_code, 409)
        self.assertFalse(self.current.studio_admission_status()['blocked'])
        self.response.status = 200; self.length = '100'
        self.assertEqual(self.post().status_code, 503)
        self.assertTrue(self.current.studio_admission_status()['blocked'])

    def test_opaque_server_error_does_not_claim_downstream_completion(self):
        self.response.status = 500; self.response.read.return_value = b'{}'; self.length = '2'
        self.assertEqual(self.post().status_code, 503)
        self.assertTrue(self.current.studio_admission_status()['blocked'])


if __name__ == '__main__': unittest.main()
