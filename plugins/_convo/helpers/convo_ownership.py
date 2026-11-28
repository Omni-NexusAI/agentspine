"""Who runs voice. One voice conversation runs at a time across every page and device; this records which page started it, a device
label (browser and platform only, never anything personal) and since when, so every other page can say where voice is running and
offer "Take over here", and so a page whose connection dropped (a phone's sleep, a network change) can re-attach to it within
GRACE_SECONDS.

Storage is additive and rollback-safe: a new table, `session_devices`, beside the journal. The `sessions` table is unchanged; a
re-attach moves a session to its new connection by rewriting the `owner` it already stores. An older Convo never reads the new table
and reads the database as before."""
from __future__ import annotations

import re
import time

GRACE_SECONDS = 20.0
PAGE = re.compile(r'^[A-Za-z0-9-]{8,64}$')
_TABLE = ("CREATE TABLE IF NOT EXISTS session_devices(session TEXT PRIMARY KEY, page TEXT NOT NULL, device TEXT NOT NULL, "
          "started REAL NOT NULL)")


def page_id(value):
    """The page's stable id as sent, or '' when absent or malformed (then the conversation ends with its connection, as before)."""
    value = str(value or '')
    return value if PAGE.match(value) else ''


def device_label(value):
    """A short printable label ("Chrome on Android"); anything else is dropped."""
    text = ' '.join(''.join(ch for ch in str(value or '') if ch.isprintable()).split())
    return text[:60] or 'another device'


def ensure(store):
    with store.transaction():
        store.db.execute(_TABLE)


def record(store, session, page, device):
    """Remember who started `session` (one row per session; never journaled)."""
    ensure(store)
    with store.transaction():
        store.db.execute("INSERT OR REPLACE INTO session_devices(session,page,device,started) VALUES(?,?,?,?)",
                         (session, page_id(page), device_label(device), time.time()))


def reassign(store, session, old_owner, new_owner):
    """Move an active session from its dropped connection to the page's new one. False when it is no longer that connection's."""
    with store.transaction():
        cursor = store.db.execute("UPDATE sessions SET owner=? WHERE id=? AND owner=? AND active=1", (new_owner, session, old_owner))
        changed = getattr(store, '_sessions_changed', None)     # the store's in-memory copy of session rows, when it keeps one
        if callable(changed):
            changed()
        return cursor.rowcount == 1


def owner(store):
    """The running conversation, {session, target, page, device, since}, or None. `since` is None for one started by an older Convo."""
    ensure(store)
    with store.lock:
        row = store.db.execute("SELECT s.id AS id, s.target AS target, d.page AS page, d.device AS device, d.started AS started "
                               "FROM sessions s LEFT JOIN session_devices d ON d.session = s.id WHERE s.active=1 LIMIT 1").fetchone()
    if not row:
        return None
    return {'session': row['id'], 'target': row['target'], 'page': row['page'] or '', 'device': row['device'] or 'another device',
            'since': round(row['started'], 3) if row['started'] else None}
