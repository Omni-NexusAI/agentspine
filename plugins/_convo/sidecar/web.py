"""The small part of FastAPI the gateway uses, on Starlette alone, so the gateway runs on Agent Zero's own Python (which has
Starlette and uvicorn but not FastAPI) with nothing to install.

Same shapes as FastAPI where the gateway relies on them: `@app.get/post(path, dependencies=[Depends(fn)])` handlers that take
the JSON body as `payload` and optionally the `request`; a dict answer becomes JSON; `HTTPException(status, detail)` answers
`{"detail": detail}`; `@app.exception_handler(type)`; `@app.middleware("http")`; `app.state`. A body that is not a JSON object is
refused with 422 before the handler runs. Routes and middleware are collected at import and the Starlette app is built on the
first request. Optional Voice Studio (gradio, which brings FastAPI with it) mounts this app inside a FastAPI app instead.
"""
from __future__ import annotations

import inspect
import json

from starlette.applications import Starlette
from starlette.datastructures import State
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

__all__ = ['App', 'Depends', 'HTTPException', 'JSONResponse', 'Request', 'Response', 'StreamingResponse']


class HTTPException(Exception):
    def __init__(self, status_code, detail=''):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def Depends(dependency):
    """A dependency is an async function taking the request; it refuses by raising HTTPException."""
    return dependency


async def _http_error(request, error):
    return JSONResponse({'detail': error.detail}, status_code=error.status_code)


class App:
    def __init__(self):
        self.state = State()
        self._routes, self._middleware, self._handlers, self._built = [], [], {HTTPException: _http_error}, None

    def _route(self, method, path, dependencies):
        def register(handler):
            wanted = inspect.signature(handler).parameters

            async def endpoint(request):
                for dependency in dependencies:
                    await dependency(request)
                arguments = {}
                if 'payload' in wanted:
                    try:
                        payload = json.loads(await request.body() or b'null')
                    except ValueError:
                        payload = None
                    if not isinstance(payload, dict):
                        return JSONResponse({'detail': 'A JSON object body is required'}, status_code=422)
                    arguments['payload'] = payload
                if 'request' in wanted:
                    arguments['request'] = request
                result = await handler(**arguments)
                return result if isinstance(result, Response) else JSONResponse(result)

            self._routes.append(Route(path, endpoint, methods=[method]))
            return handler
        return register

    def get(self, path, dependencies=()):
        return self._route('GET', path, dependencies)

    def post(self, path, dependencies=()):
        return self._route('POST', path, dependencies)

    def exception_handler(self, kind):
        def register(handler):
            self._handlers[kind] = handler
            return handler
        return register

    def middleware(self, kind):
        if kind != 'http':
            raise ValueError('Only http middleware is supported')
        def register(dispatch):
            self._middleware.append(Middleware(BaseHTTPMiddleware, dispatch=dispatch))
            return dispatch
        return register

    def starlette(self):
        if self._built is None:
            self._built = Starlette(routes=self._routes, middleware=self._middleware, exception_handlers=dict(self._handlers))
            self._built.state = self.state
        return self._built

    async def __call__(self, scope, receive, send):
        await self.starlette()(scope, receive, send)
