"""Explicit managed entrypoint; parent death cannot orphan the gateway."""
import ctypes
import os
import signal
import sys


def main():
    if sys.platform != 'linux':
        raise RuntimeError('Managed runtime requires a Linux host')
    descriptor, parent = map(int, sys.argv[1:])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent:
        raise RuntimeError('Cannot establish runtime ownership')
    import uvicorn
    from .app import app, preview_speech
    from importlib.util import find_spec
    served = app
    if find_spec('gradio') is not None and find_spec('fastapi') is not None:
        # Voice Studio (the optional one-time setup) is a gradio app that mounts on FastAPI: the gateway is mounted inside it.
        from fastapi import FastAPI
        from .gradio_studio import mount
        served = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        mount(served, '/tmp/convo-studio', preview_speech)
        served.mount('/', app)
        app.state.studio_enabled = True
    uvicorn.run(served, fd=descriptor, access_log=False,
                log_level='warning', timeout_graceful_shutdown=1)


if __name__ == '__main__':
    main()
