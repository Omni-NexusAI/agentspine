"""Reversible WSGI bridge for the fixed Convo-owned, loopback Studio gateway.

Not an ApiHandler: request bodies are bounded before parsing, and native session
authentication, explicit CSRF headers and Origin are checked on every request.
"""
from __future__ import annotations

import hmac
import http.client
import json
import re
from urllib.parse import unquote, urlsplit

from .studio_admission import RECOVERY_MESSAGE

PATH = '/convo-studio'
LIMIT = 16 * 1024**2
MIMES = {'text/html', 'text/css', 'text/javascript', 'application/javascript',
         'application/json', 'application/octet-stream', 'application/zip',
         'audio/wav', 'audio/x-wav', 'image/png', 'image/jpeg', 'image/webp',
         'image/svg+xml', 'image/x-icon', 'font/woff', 'font/woff2',
         'application/font-woff', 'application/wasm', 'text/event-stream'}


def safe_path(path):
    decoded = unquote(path)
    # Gradio file routes contain absolute artifact paths; Gradio separately
    # checks that they are registered uploads/outputs. Never accept a URL there.
    return (path.startswith(PATH + '/') and '\\' not in decoded and '\x00' not in decoded
            and '..' not in decoded.split('/') and '://' not in decoded
            and not any(ord(c) < 32 for c in decoded))


def allowed_route(path, method):
    relative = unquote(path[len(PATH):]).rstrip('/') or '/'
    if method in {'GET', 'HEAD'}:
        if relative in {'/', '/config', '/theme.css', '/manifest.json', '/favicon.ico',
                        '/gradio_api/info', '/gradio_api/user', '/gradio_api/login_check',
                        '/gradio_api/app_id', '/gradio_api/upload_progress'}:
            return True
        if relative.startswith(('/assets/', '/static/')):
            return True
        if re.fullmatch(r'/gradio_api/heartbeat/[A-Za-z0-9_-]{1,128}', relative):
            return True
        if relative.startswith('/gradio_api/file=/tmp/convo-studio/'):
            return relative.lower().endswith(('.wav', '.zip', '.json'))
        return False
    return relative == '/gradio_api/upload' or bool(re.fullmatch(r'/gradio_api/(api|run)/[A-Za-z0-9_-]{1,128}', relative))


def complete_body(response, data, method):
    length = response.getheader('Content-Length')
    if method != 'HEAD' and length is not None and (not length.isdecimal() or int(length) != len(data)):
        raise ValueError('Incomplete Studio response')


def bootstrap(token):
    value = json.dumps(token).replace('<', '\\u003c')
    return ('''<script>(()=>{const csrf=''' + value + ''';const original=window.fetch.bind(window);
window.fetch=(input,options={})=>{const url=new URL(input instanceof Request?input.url:String(input),location.href);
if(url.origin!==location.origin)return Promise.reject(new Error('Studio external requests are disabled'));
if(url.pathname.startsWith('/convo-studio/')){const headers=new Headers(input instanceof Request?input.headers:undefined);
new Headers(options.headers).forEach((v,k)=>headers.set(k,v));headers.set('X-CSRF-Token',csrf);
options={...options,headers,credentials:'same-origin'};}return original(input,options);};
const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open=function(method,url,...rest){const u=new URL(url,location.href);
if(u.origin!==location.origin)throw new Error('Studio external requests are disabled');this._convo=u.pathname.startsWith('/convo-studio/');return open.call(this,method,url,...rest);};
XMLHttpRequest.prototype.send=function(body){if(this._convo)this.setRequestHeader('X-CSRF-Token',csrf);return send.call(this,body);};})();</script>''').encode()


class StudioProxy:
    def __init__(self, app, original, *, enabled, destination, guard, model, credentials_hash, connection=http.client.HTTPConnection):
        self.app, self.original = app, original
        self.enabled, self.destination, self.guard, self.model = enabled, destination, guard, model
        self.credentials_hash, self.connection = credentials_hash, connection

    def __call__(self, environ, start_response):
        from flask import request, session
        from werkzeug.wrappers import Response
        path = environ.get('PATH_INFO', '')
        if path != PATH and not path.startswith(PATH + '/'):
            return self.original(environ, start_response)

        def result(message, status):
            return Response(message, status=status, content_type='text/plain', headers={
                'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})(environ, start_response)

        if not self.enabled(): return result('Convo is disabled', 403)
        method = environ.get('REQUEST_METHOD', '')
        if method not in {'GET', 'HEAD', 'POST'}: return result('Method not allowed', 405)
        if path == PATH:
            return Response('', status=307, headers={'Location': PATH + '/'})(environ, start_response)
        if not safe_path(path): return result('Invalid Studio path', 400)
        # Background event submission would outlive the host request admission.
        if not allowed_route(path, method):
            return result('Studio route is not exposed', 403)
        with self.app.request_context(environ):
            expected = self.credentials_hash()
            if expected and session.get('authentication') != expected:
                return result('Sign in to Agent Zero before opening Studio', 401)
            token = session.get('csrf_token')
            if not isinstance(token, str) or not token:
                return result('Open Studio from the authenticated Convo interface', 403)
            origin = request.host_url.rstrip('/')
            if request.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
                return result('Same-origin Studio requests only', 403)
            if method == 'POST':
                sent = request.headers.get('X-CSRF-Token', '')
                if request.headers.get('Origin') != origin or not hmac.compare_digest(token, sent):
                    return result('Studio Origin or CSRF check failed', 403)
                length = environ.get('CONTENT_LENGTH', '')
                if not length.isdecimal(): return result('Bounded Content-Length required', 411)
                if int(length) > LIMIT: return result('Studio upload exceeds limit', 413)
                body = environ['wsgi.input'].read(int(length))
                if len(body) != int(length): return result('Incomplete Studio upload', 400)
            else:
                body = None
            content_type = request.headers.get('Content-Type', '')
            if method == 'POST' and not (content_type.startswith('application/json') or content_type.startswith('multipart/form-data;')):
                return result('Unsupported Studio request format', 415)
            try:
                with self.guard(method, path) as lease:
                    if not self.enabled(): return result('Convo is disabled', 403)
                    url, secret = self.destination()
                    target = urlsplit(url)
                    if target.scheme != 'http' or target.hostname != '127.0.0.1' or target.path not in ('', '/') or target.query or target.fragment or target.username or not target.port or len(secret) < 24:
                        return result('Owned Studio gateway is unavailable', 503)
                    selection = self.model()
                    if isinstance(selection, str): selection = {'model': selection}
                    headers = {'Authorization': 'Bearer ' + secret, 'Accept-Encoding': 'identity',
                               'X-Convo-TTS-Model': selection.get('model', ''),
                               'X-Convo-TTS-Tuning-Profile': selection.get('tuning_profile', ''),
                               'Content-Type': content_type,
                               # Preserve the native public origin in Gradio's
                               # generated root/asset URLs, not its private port.
                               'Host': request.host, 'X-Forwarded-Host': request.host,
                               'X-Forwarded-Proto': request.scheme}
                    query = environ.get('QUERY_STRING', '')
                    if len(query) > 4096: return result('Studio query exceeds limit', 400)
                    # http.client validates these locally before transmitting.
                    # Do not persist an uncertainty marker for local rejections.
                    (path + query).encode('ascii')
                    if any(ord(c) < 32 or ord(c) == 127 for c in query):
                        return result('Invalid Studio query', 400)
                    for value in headers.values():
                        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
                            raise ValueError('Invalid Studio header value')
                        value.encode('latin-1')
                    conn = self.connection('127.0.0.1', target.port, timeout=190)
                    try:
                        lease.dispatched()
                        conn.request(method, path + ('?' + query if query else ''), body=body, headers=headers)
                        response = conn.getresponse()
                        if not 200 <= response.status < 300:
                            # A complete rejection is terminal; opaque server
                            # failures may hide an upstream operation timeout.
                            rejected = response.read(LIMIT + 1)
                            complete_body(response, rejected, method)
                            if len(rejected) <= LIMIT and 300 <= response.status < 500:
                                lease.completed()
                            if method == 'POST' and getattr(lease, 'operation_id', None):
                                return result(RECOVERY_MESSAGE, 503)
                            return result('Studio rejected the request or is not installed in the owned gateway', response.status if response.status in (400, 401, 403, 404, 409, 413, 422) else 502)
                        mime = response.getheader('Content-Type', '').split(';', 1)[0].lower()
                        if mime not in MIMES or (mime == 'text/html' and path != PATH + '/'):
                            return result('Unsupported Studio response', 502)
                        # Queue is disabled; heartbeat SSE does not execute work.
                        # Return one real bounded heartbeat so a stream cannot
                        # hold a WSGI worker forever; EventSource reconnects.
                        if mime == 'text/event-stream':
                            data = response.read1(4096)
                        else:
                            data = response.read(LIMIT + 1)
                        if len(data) > LIMIT: return result('Studio response exceeds limit', 502)
                        if mime != 'text/event-stream': complete_body(response, data, method)
                        lease.completed()
                        if not self.enabled(): return result('Convo was disabled', 403)
                        if mime == 'text/html':
                            marker = b'<head>'
                            if marker not in data: return result('Invalid Studio document', 502)
                            data = data.replace(marker, marker + bootstrap(token), 1)
                        safe_headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                            'X-Frame-Options': 'SAMEORIGIN',
                            'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; form-action 'self'; base-uri 'self'"}
                        if mime in {'application/zip', 'application/octet-stream'}:
                            safe_headers['Content-Disposition'] = 'attachment'
                        return Response(data, status=response.status, content_type=mime, headers=safe_headers)(environ, start_response)
                    finally:
                        conn.close()
            except Exception as exc:
                if str(exc) == RECOVERY_MESSAGE or ('lease' in locals() and getattr(lease, 'operation_id', None)):
                    return result(RECOVERY_MESSAGE, 503)
                return result('Studio is unavailable or busy. Stop Convo and check managed runtime readiness.', 503)


def install_on(webapp):
    """Install/restore exactly this webapp.wsgi_app attribute through lifecycle."""
    from helpers import login
    from . import convo_service, lifecycle, managed_runtime
    def destination():
        manager = managed_runtime.instance()
        with manager.lock:
            if manager.public()['state'] != 'ready': raise ValueError('Owned gateway not ready')
            return manager.url, manager.token
    def active():
        return lifecycle.enabled() and not lifecycle.state().blocked and convo_service.settings()['enabled']
    def operation():
        webapp.wsgi_app = StudioProxy(webapp, webapp.wsgi_app, enabled=active,
            destination=destination, guard=convo_service.studio_request_guard,
            model=lambda: convo_service.settings()['roles']['tts'],
            credentials_hash=login.get_credentials_hash)
    return lifecycle.state().install('studio-proxy', operation, [(webapp, 'wsgi_app')])


def attach(webapp=None):
    """Explicit activation/page-initialization hook, never a settings-read side effect."""
    from helpers import runtime
    if webapp is None:
        from flask import current_app, has_app_context
        webapp = current_app._get_current_object() if has_app_context() else getattr(runtime, '_convo_webapp', None)
    if webapp is not None:
        runtime._convo_webapp = webapp
        install_on(webapp)
