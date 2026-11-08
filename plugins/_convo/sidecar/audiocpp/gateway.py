"""Private authenticated headless Studio; no standalone UI or arbitrary proxy."""
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

import supervisor
from downloads import Downloads, PRESETS

# Convo never inherits a standalone bypass or relaxed custom admission policy.
# In-memory only: leave persisted profiles and the vendored source untouched.
supervisor.gpu_guard_settings.clear()
supervisor.gpu_guard_settings.update(supervisor._default_gpu_guard_settings(), mode='enforced')

# The reused supervisor configures logging globally. Signed download redirect
# URLs and unrelated provider requests must not appear in informational logs.
logging.getLogger('httpx').setLevel(logging.WARNING)

downloads = Downloads('/opt/models')


@asynccontextmanager
async def lifespan(app):
    await supervisor.startup()
    try: yield
    finally:
        for model in list(downloads.tasks): await downloads.cancel(model)
        await supervisor.shutdown()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware('http')
async def authenticate(request: Request, call_next):
    token = os.environ.get('CONVO_AUDIO_TOKEN', '')
    if len(token) < 24 or not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
        return JSONResponse({'error': 'Convo audio authentication required'}, status_code=401)
    original = request._receive
    size = 0
    async def bounded():
        nonlocal size
        message = await original(); size += len(message.get('body', b''))
        if size > 16 * 1024**2: raise HTTPException(413, 'Request exceeds audio control limit')
        return message
    request._receive = bounded
    return await call_next(request)


@app.get('/convo/catalog')
async def catalog():
    return {'models': [{'id': key, **value, 'status': downloads.status(key)} for key, value in PRESETS.items()]}


@app.post('/convo/download')
async def download(payload: dict):
    model, action = payload.get('model'), payload.get('action')
    if model not in PRESETS: raise HTTPException(400, 'Unknown model preset')
    try:
        if action == 'inspect': return await downloads.inspect(model)
        if action == 'status': return downloads.status(model)
        if payload.get('confirmed') is not True: raise ValueError('Explicit confirmation required')
        if action == 'start': return downloads.start(model, payload.get('manifest_id'))
        if action == 'cancel': return await downloads.cancel(model)
        raise ValueError('Unknown download operation')
    except Exception:
        raise HTTPException(400, 'Download operation rejected; inspect the source manifest and current state') from None


# Expose only known Studio/synthesis paths, never the reused supervisor's
# standalone model/chat proxy, GPU-guard override, or unrelated lifecycle routes.
paths = {
    '/health', '/control/status', '/v1/models', '/v1/voices', '/v1/voices/profiles',
    '/v1/voices/profiles/{profile_id}', '/v1/voices/profiles/{profile_id}/select',
    '/v1/backend/models', '/v1/backend/models/switch', '/v1/backend/models/unload',
    '/v1/tuning/profiles', '/v1/tuning/profiles/{profile_id}', '/v1/tuning/profiles/{profile_id}/clone',
    '/v1/tuning/profiles/{profile_id}/reset', '/v1/tuning/export', '/v1/tuning/import',
    '/v1/tuning/selection', '/v1/tuning/resolve', '/v1/audio/speech', '/v1/audio/outcomes/{request_id}',
}
supervisor.app.router.routes[:] = [route for route in supervisor.app.routes if getattr(route, 'path', '') in paths]
app.mount('/', supervisor.app)
