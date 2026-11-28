"""A short identity of the Convo build the server is running (the `status` action's `build`).

An open page keeps the scripts it loaded; after an update it would keep running the old ones until reloaded (the first live test of
the 2026-11-09 batch did exactly that). The page remembers the first build it saw and offers a reload when the server's changes. The
identity is a hash of the browser files' contents (webui/ and extensions/webui/), recomputed at most every CACHE_SECONDS."""
from __future__ import annotations

import hashlib
import os
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDERS = ('webui', os.path.join('extensions', 'webui'))
SUFFIXES = ('.js', '.mjs', '.html', '.css', '.json')
CACHE_SECONDS = 10.0
_lock = threading.Lock()
_cache = [float('-inf'), '']


def build_id(root=ROOT, now=None):
    now = time.monotonic() if now is None else now
    with _lock:
        if root == ROOT and now - _cache[0] < CACHE_SECONDS and _cache[1]:
            return _cache[1]
    digest = hashlib.sha256()
    for folder in FOLDERS:
        base = os.path.join(root, folder)
        for path, names, files in os.walk(base):
            names.sort()
            for name in sorted(files):
                if not name.endswith(SUFFIXES):
                    continue
                full = os.path.join(path, name)
                try:
                    with open(full, 'rb') as handle:
                        content = handle.read()
                except OSError:
                    continue
                digest.update(os.path.relpath(full, root).replace(os.sep, '/').encode())
                digest.update(hashlib.sha256(content).digest())
    value = digest.hexdigest()[:16]
    if root == ROOT:
        with _lock:
            _cache[0], _cache[1] = now, value
    return value
