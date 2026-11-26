"""Voice timers ("tell me in ten minutes") kept in Convo's journal database, in a table of their own: `voice_timers`.

A timer belongs to a scope (whatever the caller uses to tell conversations apart), has an optional label and a due time, and
goes through `pending` -> `fired` (claimed by `due`, exactly once) or `cancelled`. A fired timer that nobody could be told
about stays untold until `told` marks it, so `missed` can bring it up when a voice session next starts. Times are UTC epoch
seconds; every method takes `now` so that tests need no clock.

Each timer is claimed once even when two callers ask at the same moment, from two connections or after a restart: the claim is
one UPDATE that moves every due timer from `pending` to `fired` and stamps it with the caller's own token, and SQLite lets
only one writer do that at a time. The 20-per-scope bound is kept the same way, inside the INSERT itself.

`connect()` returns a sqlite3 connection to the journal database. It is called once for each thread that uses the object and
the connection is kept for that thread (and never closed here). Give this object a connection of its own
(`lambda: sqlite3.connect(path, timeout=5)`); when `connect` hands back a connection other code also writes through, pass
that code's lock as `lock`, and a call made inside that code's open transaction then joins it instead of committing it.
Only `voice_timers` is ever created, read or changed. Standard library only.

A refusal is a ValueError with one fixed sentence that may be said to the user; a database that cannot be used raises the
same kind (`UNAVAILABLE`), never the database's own text."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid

SHORTEST = 1.0             # seconds
LONGEST = 86400.0          # 24 hours
MOST = 20                  # pending timers one scope may have
LABEL_CHARACTERS = 80
MISSED_SECONDS = 7200      # a timer that rang longer ago than this is no longer worth mentioning
KEEP_SECONDS = 7 * 86400   # finished timers are forgotten after this

BOUNDS = 'A timer can run from one second to 24 hours'
TOO_MANY = 'There are already 20 timers running'
NO_SCOPE = 'A timer needs a conversation to belong to'
UNAVAILABLE = 'Timers are not available right now'

COLUMNS = 'id,scope,label,due,created,state,fired'
SCHEMA = ("CREATE TABLE IF NOT EXISTS voice_timers(id TEXT PRIMARY KEY, scope TEXT NOT NULL, label TEXT NOT NULL, due REAL NOT NULL, "
          "created REAL NOT NULL, state TEXT NOT NULL, fired REAL, told REAL, claim TEXT)",
          "CREATE INDEX IF NOT EXISTS voice_timers_state ON voice_timers(state,due)")


def _words(text, limit=None):
    text = ' '.join(str(text or '').split())
    return text if limit is None else text[:limit].rstrip()


def _scope(scope):
    scope = _words(scope)
    if not scope:
        raise ValueError(NO_SCOPE)
    return scope


def _timer(row, now=None):
    """A row as the dictionary callers get: `fired` only once it has rung, `left` (seconds, never negative) only while pending."""
    timer = {'id': row[0], 'scope': row[1], 'label': row[2], 'due': row[3], 'created': row[4], 'state': row[5]}
    if row[6] is not None:
        timer['fired'] = row[6]
    if now is not None and timer['state'] == 'pending':
        timer['left'] = max(0.0, timer['due'] - now)
    return timer


class Timers:
    def __init__(self, connect, *, lock=None):
        self._connect, self._lock, self._local = connect, lock or threading.RLock(), threading.local()

    def _db(self):
        """This thread's connection, with the table made on first use. A table made inside someone else's open transaction
        may still be undone with it, so it is only taken as made once it was made outside one."""
        db = getattr(self._local, 'db', None)
        if db is None:
            db = self._local.db = self._connect()
        if not getattr(self._local, 'made', False):
            settled = not db.in_transaction
            for statement in SCHEMA:
                self._change(db, statement)
            self._local.made = settled
        return db

    @staticmethod
    def _change(db, statement, values=()):
        """One changing statement as a transaction of its own; how many rows it changed. The transaction takes the write
        lock when it begins (IMMEDIATE), so a second writer waits its turn for as long as the connection's timeout allows
        instead of failing half-way. Inside a transaction someone else opened on this connection the statement only takes
        part: committing or undoing that one is its owner's business."""
        own = not db.in_transaction
        try:
            if own:
                db.execute('BEGIN IMMEDIATE')
            changed = db.execute(statement, values).rowcount
            if own:
                db.execute('COMMIT')
            return changed
        except BaseException:
            if own and db.in_transaction:
                db.execute('ROLLBACK')
            raise

    def _run(self, work):
        """`work(db)` under the lock; a database failure becomes the one sentence a caller may say."""
        with self._lock:
            try:
                return work(self._db())
            except sqlite3.Error as error:
                raise ValueError(UNAVAILABLE) from error

    def add(self, scope, seconds, label='', *, now=None) -> dict:
        """A new timer due `seconds` from `now` (1 second to 24 hours; at most 20 pending per scope). Returns
        `{'id', 'scope', 'label', 'due', 'created', 'state': 'pending'}`."""
        scope = _scope(scope)
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            raise ValueError(BOUNDS) from None
        if not SHORTEST <= seconds <= LONGEST:      # also refuses a value that is not a number at all
            raise ValueError(BOUNDS)
        now = time.time() if now is None else float(now)
        timer = {'id': '', 'scope': scope, 'label': _words(label, LABEL_CHARACTERS), 'due': now + seconds, 'created': now, 'state': 'pending'}

        def work(db):
            self._change(db, "DELETE FROM voice_timers WHERE state!='pending' AND due<?", (now - KEEP_SECONDS,))
            for _ in range(5):
                timer['id'] = uuid.uuid4().hex[:8]
                try:
                    # The count and the insert are one statement, so two callers cannot both take the last place.
                    return self._change(db, "INSERT INTO voice_timers(id,scope,label,due,created,state) SELECT ?,?,?,?,?,'pending' "
                                            "WHERE (SELECT COUNT(*) FROM voice_timers WHERE scope=? AND state='pending')<?",
                                        (timer['id'], scope, timer['label'], timer['due'], now, scope, MOST))
                except sqlite3.IntegrityError:
                    continue        # the short id is taken: another one
            raise ValueError(UNAVAILABLE)
        if not self._run(work):
            raise ValueError(TOO_MANY)
        return timer

    def pending(self, scope=None, *, now=None) -> list:
        """The timers still running, soonest first: one scope's, or everyone's without a scope. Each also says how many
        seconds are `left` (0 for one that is due and not yet claimed)."""
        now = time.time() if now is None else float(now)
        where, values = ("state='pending' AND scope=?", (_scope(scope),)) if scope is not None else ("state='pending'", ())
        rows = self._run(lambda db: db.execute('SELECT %s FROM voice_timers WHERE %s ORDER BY due,created,rowid' % (COLUMNS, where), values).fetchall())
        return [_timer(row, now) for row in rows]

    def cancel(self, scope, which='') -> list:
        """Cancels pending timers of one scope and returns them (state `cancelled`); [] when nothing fits. `which` empty or
        `latest` is the one set most recently; otherwise a timer's id, else every timer whose label is `which` (letter case and
        spacing aside), else every timer whose label is part of `which` or has `which` in it. An unlabelled timer is only
        ever cancelled by its id or as the latest."""
        scope, which = _scope(scope), _words(which).casefold()

        def work(db):
            rows = db.execute('SELECT %s FROM voice_timers WHERE state=? AND scope=? ORDER BY created DESC,rowid DESC' % COLUMNS,
                              ('pending', scope)).fetchall()
            timers = [_timer(row) for row in rows]
            if which in ('', 'latest'):
                chosen = timers[:1]
            else:
                labelled = [(timer, timer['label'].casefold()) for timer in timers if timer['label']]
                chosen = ([timer for timer in timers if timer['id'].casefold() == which]
                          or [timer for timer, label in labelled if label == which]
                          or [timer for timer, label in labelled if label in which or which in label])
            # A timer that rang or was cancelled between the read and this change is left as it is, and not reported.
            return [dict(timer, state='cancelled') for timer in chosen
                    if self._change(db, "UPDATE voice_timers SET state='cancelled' WHERE id=? AND state='pending'", (timer['id'],))]
        return self._run(work)

    def due(self, *, now=None) -> list:
        """The timers whose time has come, of every scope, soonest first, each handed out exactly once: they are `fired` from
        this call on, whoever asks next, on whichever connection, before or after a restart."""
        now, claim = time.time() if now is None else float(now), uuid.uuid4().hex

        def work(db):
            if not self._change(db, "UPDATE voice_timers SET state='fired',fired=?,claim=? WHERE state='pending' AND due<=?", (now, claim, now)):
                return []
            return db.execute('SELECT %s FROM voice_timers WHERE claim=? ORDER BY due,created,rowid' % COLUMNS, (claim,)).fetchall()
        return [_timer(row) for row in self._run(work)]

    def missed(self, scope, *, now=None, window=MISSED_SECONDS) -> list:
        """One scope's timers that rang (were claimed by `due`) and that the user has not been told about, soonest first,
        leaving out those due more than `window` seconds ago. Asking changes nothing: `told` is what settles one."""
        scope, now = _scope(scope), time.time() if now is None else float(now)
        rows = self._run(lambda db: db.execute(
            "SELECT %s FROM voice_timers WHERE state='fired' AND told IS NULL AND scope=? AND due>=? ORDER BY due,created,rowid" % COLUMNS,
            (scope, now - float(window))).fetchall())
        return [_timer(row) for row in rows]

    def told(self, timer_id, *, now=None) -> bool:
        """Marks a timer that rang as told to the user. True when that changed something (False for an unknown id, a timer
        still pending or cancelled, or one already told)."""
        now = time.time() if now is None else float(now)
        return bool(self._run(lambda db: self._change(
            db, "UPDATE voice_timers SET told=? WHERE id=? AND state='fired' AND told IS NULL", (now, str(timer_id or '')))))
