"""Durable event journal and idempotent jobs. No raw audio is persisted."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager


ADDED = "\n\nAdded by the user before the task started:\n"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def script(text):
    """The writing system most letters of `text` use (LATIN, CYRILLIC, DEVANAGARI, CJK...), or '' when it has no letters."""
    counts = {}
    for ch in str(text or ''):
        if ch.isalpha():
            name = unicodedata.name(ch, '').split(' ')
            key = 'CJK' if name[0] in {'CJK', 'HIRAGANA', 'KATAKANA', 'HANGUL'} else name[0]
            counts[key] = counts.get(key, 0) + 1
    return max(counts, key=counts.get) if counts else ''


def disagree(line, whisper):
    """The model's line and Whisper's text describe the same speech too differently to trust the line: it is under half as long,
    or written in another script."""
    line, whisper = ' '.join(str(line).split()), ' '.join(str(whisper).split())
    if not line or not whisper:
        return False
    if len(line) < 0.5 * len(whisper):
        return True
    a, b = script(line), script(whisper)
    return bool(a and b and a != b)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class MarkWriter:
    """The one writer thread for writes nothing waits on: content-free timing marks (with the reasons utterances were ignored),
    kept across restarts in `timing_marks` beside the journal, bounded. Its own connection (WAL, synchronous=NORMAL); the voice loop
    only appends to a bounded queue, and a full queue drops marks instead of waiting. Turns, jobs and notices are not written here:
    their callers use the result at once, so they keep writing (and waiting) on the store's own connection."""
    QUEUE = 4000
    BATCH_SECONDS = 0.5

    def __init__(self, path, kept):
        import queue
        self.path, self.kept = path, kept
        self.queue = queue.Queue(self.QUEUE)
        self.flushed = threading.Condition()
        self.written = self.queued = 0
        self.closed = False
        self.thread = None
        if path and path != ":memory:":
            self.thread = threading.Thread(target=self._run, name="ConvoMarks", daemon=True)
            self.thread.start()

    def put(self, turn, mark):
        if self.thread is None or self.closed:
            return
        item = (str(turn)[:80], str(mark.get("name", ""))[:40], time.time(),
                encoded({k: v for k, v in mark.items() if k != "name" and isinstance(v, (int, float, str, bool))}))
        try:
            self.queue.put_nowait(item)
            with self.flushed:
                self.queued += 1
        except Exception:
            pass

    def flush(self, timeout=2.0):
        """Wait (bounded) until everything queued so far is written. For readers and tests, never the voice path."""
        if self.thread is None:
            return
        deadline = time.monotonic() + timeout
        with self.flushed:
            target = self.queued
            while self.written < target and time.monotonic() < deadline and self.thread.is_alive():
                self.flushed.wait(max(0.0, deadline - time.monotonic()))

    def close(self):
        """Never blocks on a full queue (or a writer that never started): the thread also stops once the queue is empty and closed."""
        import queue
        self.closed = True
        if self.thread is not None:
            try:
                self.queue.put_nowait(None)
            except queue.Full:
                pass
            self.thread.join(3)

    def _run(self):
        import queue
        try:
            db = sqlite3.connect(self.path, timeout=5)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("CREATE TABLE IF NOT EXISTS timing_marks(id INTEGER PRIMARY KEY AUTOINCREMENT, turn TEXT NOT NULL, name TEXT NOT NULL, at REAL NOT NULL, fields TEXT NOT NULL)")
            db.commit()
        except Exception:
            return
        stop = False
        while not stop:
            try:
                first = self.queue.get(timeout=self.BATCH_SECONDS)
            except queue.Empty:
                if self.closed:
                    break                       # closed while the queue was full: everything queued has been written
                continue
            batch = [first]
            while len(batch) < 500:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            stop = any(item is None for item in batch)
            rows = [item for item in batch if item is not None]
            try:
                with db:
                    db.executemany("INSERT INTO timing_marks(turn,name,at,fields) VALUES(?,?,?,?)", rows)
                    newest = db.execute("SELECT max(id) FROM timing_marks").fetchone()[0] or 0
                    if newest > self.kept:
                        db.execute("DELETE FROM timing_marks WHERE id<=?", (newest - self.kept,))
            except Exception:
                pass
            with self.flushed:
                self.written += len(rows)
                self.flushed.notify_all()
        try:
            db.close()
        except Exception:
            pass


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        # WAL with NORMAL: a commit no longer waits for the disk on the voice loop; the database stays durable across a process
        # restart (only an operating-system crash can lose the last commits).
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.path = str(path)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, owner TEXT NOT NULL, target TEXT NOT NULL, epoch INTEGER NOT NULL, active INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, target TEXT NOT NULL, epoch INTEGER NOT NULL, turn TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, time REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS target_events ON events(target,seq);
        CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY, session TEXT NOT NULL, target TEXT NOT NULL, epoch INTEGER NOT NULL, content TEXT NOT NULL, settled INTEGER NOT NULL, revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS memories(target TEXT PRIMARY KEY, summary TEXT NOT NULL, covered TEXT NOT NULL, revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, session TEXT NOT NULL, target TEXT NOT NULL, turn TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notices(job TEXT PRIMARY KEY, session TEXT NOT NULL, turn TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS job_scope(job TEXT PRIMARY KEY, scope TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS job_steers(id TEXT PRIMARY KEY, job TEXT NOT NULL, target TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL, time REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, target TEXT NOT NULL, session TEXT NOT NULL, topic TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL, written_by TEXT NOT NULL, upto INTEGER NOT NULL, allowed TEXT NOT NULL, own TEXT NOT NULL DEFAULT '[]', job TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS target_plans ON plans(target,status);
        CREATE TABLE IF NOT EXISTS plan_versions(id INTEGER PRIMARY KEY, plan TEXT NOT NULL, text TEXT NOT NULL, written_by TEXT NOT NULL, time REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS versions_of_plan ON plan_versions(plan);
        CREATE TABLE IF NOT EXISTS memory_chapters(target TEXT NOT NULL, seq INTEGER NOT NULL, summary TEXT NOT NULL, covered TEXT NOT NULL, last_rowid INTEGER NOT NULL, revision INTEGER NOT NULL, stale INTEGER NOT NULL DEFAULT 0, time REAL NOT NULL, PRIMARY KEY(target, seq));
        CREATE TABLE IF NOT EXISTS memory_standing(target TEXT PRIMARY KEY, text TEXT NOT NULL, time REAL NOT NULL);
        """)
        if 'own' not in {row[1] for row in self.db.execute('PRAGMA table_info(plans)')}:      # a journal of an earlier build of planning mode
            self.db.execute("ALTER TABLE plans ADD COLUMN own TEXT NOT NULL DEFAULT '[]'")
        with self.transaction():
            # A process restart cannot determine whether an external side effect happened.
            self.db.execute("UPDATE jobs SET status='uncertain' WHERE status IN ('dispatching','running','cancel_requested')")
            # A change still on its way is kept by the Steering plugin in its chat (as held); Convo no longer follows it.
            self.db.execute("UPDATE job_steers SET status='dropped' WHERE status='waiting'")
            for row in self.db.execute("SELECT * FROM sessions WHERE active=1").fetchall():
                self._finish_pending(dict(row), "restart")
            self.db.execute("UPDATE sessions SET active=0,epoch=epoch+1 WHERE active=1")

    @contextmanager
    def transaction(self):
        with self.lock:
            self._writing = getattr(self, "_writing", 0) + 1
            try:
                with self.db:
                    yield
            finally:
                self._writing -= 1

    def close(self):
        writer = getattr(self, "_writer", None)
        if writer is not None:
            writer.close()
        with self.lock:
            self.db.close()

    def session(self, sid, owner=None, active=True):
        """The session row. Read from an in-memory copy kept under the same lock as every change to it (the voice path checks it
        before each audio chunk, so it must not query the database); a change clears the copy inside its transaction, so a stop
        (plugin OFF included) is seen by the very next check, and a rolled-back change is simply read again."""
        with self.lock:
            cache = getattr(self, "_sessions", None)
            if cache is None:
                cache = self._sessions = {}
            row = cache.get(sid)
            if row is None:
                found = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
                row = dict(found) if found else None
                if row is not None and not getattr(self, "_writing", 0):
                    cache[sid] = row
                    while len(cache) > 64:
                        cache.pop(next(iter(cache)))
            if not row or (owner is not None and row["owner"] != owner) or (active and not row["active"]):
                raise ValueError("Unknown or inactive voice session")
            return dict(row)

    def _sessions_changed(self):
        cache = getattr(self, "_sessions", None)
        if cache:
            cache.clear()

    def start(self, owner, target):
        with self.transaction():
            if self.db.execute("SELECT 1 FROM sessions WHERE active=1").fetchone():
                raise ValueError("Another voice session is active; stop it before starting a new one")
            sid = uuid.uuid4().hex
            self.db.execute("INSERT INTO sessions(id,owner,target,epoch,active) VALUES(?,?,?,0,1)", (sid, owner, target))
            self._sessions_changed()
        return self.session(sid, owner)

    def interrupt(self, sid, owner):
        with self.transaction():
            self._finish_pending(self.session(sid, owner), "interrupted")
            self.db.execute("UPDATE sessions SET epoch=epoch+1 WHERE id=?", (sid,))
            self._sessions_changed()
        return self.session(sid, owner)

    def stop(self, sid, owner):
        with self.transaction():
            self._finish_pending(self.session(sid, owner), "stopped")
            self.db.execute("UPDATE sessions SET active=0,epoch=epoch+1 WHERE id=?", (sid,))
            self._sessions_changed()

    def retarget(self, sid, owner, target):
        with self.transaction():
            old = self.session(sid, owner)
            self._finish_pending(old, "retargeted")
            self._event(old, "", "segment.closed", {"next_target": target})
            self.db.execute("UPDATE sessions SET target=?,epoch=epoch+1,generation=generation+1 WHERE id=?", (target, sid))
            self._sessions_changed()
            new = self.session(sid, owner)
            self._event(new, "", "segment.opened", {"previous_target": old["target"]})
        return new

    def _finish_pending(self, session, reason):
        rows = self.db.execute("SELECT * FROM turns WHERE session=? AND settled=0", (session["id"],)).fetchall()
        for row in rows:
            content = json.loads(row["content"])
            content["interrupted"] = True
            self.db.execute("UPDATE turns SET content=?,settled=1,revision=revision+1 WHERE id=?", (encoded(content), row["id"]))
            self._event(session, row["id"], "turn.interrupted", {"reason": reason})

    def _event(self, session, turn, kind, payload):
        if any(key in payload for key in ("audio", "pcm", "token", "authorization")):
            raise ValueError("Audio and credentials are not journal payloads")
        cursor = self.db.execute("INSERT INTO events(session,target,epoch,turn,kind,payload,time) VALUES(?,?,?,?,?,?,?)", (session["id"], session["target"], session["epoch"], turn, kind, encoded(payload), time.time()))
        return cursor.lastrowid

    def append(self, sid, owner, epoch, turn, kind, payload):
        with self.transaction():
            session = self.session(sid, owner)
            if session["epoch"] != epoch:
                return False
            self._event(session, turn, kind, payload)
            return True

    def accept_turn(self, sid, owner, epoch, turn, content):
        with self.transaction():
            session = self.session(sid, owner)
            if session["epoch"] != epoch:
                return False
            old = self.db.execute("SELECT * FROM turns WHERE id=?", (turn,)).fetchone()
            if old:
                if old["session"] != sid or old["epoch"] != epoch or old["content"] != encoded(content):
                    raise ValueError("Turn ID reused with different content")
                return False
            self.db.execute("INSERT INTO turns VALUES(?,?,?,?,?,0,0)", (turn, sid, session["target"], epoch, encoded(content)))
            self._event(session, turn, "user.accepted", content)
            return True

    def first_words(self, target):
        """The first thing the user was answered about in this conversation, as captured ('' while nothing was): what a chat used
        only by voice is named after (Host.keep). A declined, unfinished or taken-over turn does not count."""
        with self.lock:
            rows = self.db.execute("SELECT content FROM turns WHERE target=? ORDER BY rowid LIMIT 40", (target,)).fetchall()
        for row in rows:
            content = json.loads(row["content"])
            text = " ".join(str(content.get("transcript") or "").split())
            if not content.get("held") and not content.get("superseded") and len(text.split()) >= 2:
                return text
        return ""

    def settle(self, sid, owner, epoch, turn, response):
        with self.transaction():
            session = self.session(sid, owner)
            row = self.db.execute("SELECT * FROM turns WHERE id=? AND session=? AND epoch=?", (turn, sid, epoch)).fetchone()
            if session["epoch"] != epoch or not row or row["settled"]:
                return False
            content = json.loads(row["content"])
            # Generated text stays in the event journal, never masquerading as heard speech.
            self.db.execute("UPDATE turns SET content=?,settled=1,revision=revision+1 WHERE id=?", (encoded(content), turn))
            self._event(session, turn, "assistant.generated", {"text": response})
            return True

    def supersede(self, sid, owner, epoch, turn, by, joined=True):
        """A newer fragment took over this turn's reply before it was heard: the turn leaves model history and its queued work
        is cancelled. Work that already started is left alone (it needs its own cancel). `joined`: the newer turn's audio holds this
        one's words too (else it replaced it, and the panel keeps showing them). Returns the cancelled job IDs."""
        with self.transaction():
            session = self.session(sid, owner)
            row = self.db.execute("SELECT * FROM turns WHERE id=? AND session=?", (turn, sid)).fetchone()
            if session["epoch"] != epoch or not row:
                return []
            content = json.loads(row["content"])
            content["superseded"] = by
            self.db.execute("UPDATE turns SET content=?,settled=1,revision=revision+1 WHERE id=?", (encoded(content), turn))
            self._event(session, turn, "turn.superseded", {"by": by} if joined else {"by": by, "joined": False})
            cancelled = []
            for job in self.db.execute("SELECT id FROM jobs WHERE session=? AND turn=? AND status='queued' ORDER BY rowid", (sid, turn)).fetchall():
                if self.db.execute("UPDATE jobs SET status='cancelled',result='' WHERE id=? AND status='queued'", (job["id"],)).rowcount:
                    self._event(session, turn, "job.cancelled", {"job_id": job["id"], "result": ""})
                    cancelled.append(job["id"])
            return cancelled
    def delivered(self, sid, owner, epoch, turn, phrase_id, text):
        with self.transaction():
            session = self.session(sid, owner)
            row = self.db.execute("SELECT * FROM turns WHERE id=? AND session=? AND epoch=?", (turn, sid, epoch)).fetchone()
            if session["epoch"] != epoch or not row:
                return False
            content = json.loads(row["content"])
            heard = content.setdefault("heard_phrases", [])
            if phrase_id in heard:
                return False
            heard.append(phrase_id)
            # One space between phrases however they were cut (a phrase may start or end with its own).
            content["assistant"] = (content.get("assistant", "").rstrip() + " " + text.strip()).strip()
            self.db.execute("UPDATE turns SET content=?,revision=revision+1 WHERE id=?", (encoded(content), turn))
            self._stale(row["target"], turn)
            self._event(session, turn, "assistant.delivered", {"text": text, "phrase_id": phrase_id})
            return True

    def transcript(self, sid, owner, turn, text, epoch=None, source="whisper"):
        """The words the user said in a turn. The conversation model's own report of what it heard (`source="model"`, kept in
        `model_line`) is the record: it is what the model understood, in whatever language was spoken. A Whisper transcript is kept
        in `whisper` and becomes the record only when the model reported nothing, or when the two disagree strongly (the model's
        line under half as long, or in another script); the turn is then flagged `heard_differently`. `transcript` always holds
        the record, so an older Convo (which reads only `transcript`) sees the same words after a rollback."""
        with self.transaction():
            self.session(sid, owner, active=False)
            row = self.db.execute("SELECT * FROM turns WHERE id=? AND session=?", (turn, sid)).fetchone()
            if not row or (epoch is not None and row["epoch"] != epoch):
                return False
            content = json.loads(row["content"])
            if source == "whisper" and content.get("held"):
                # The model judged this turn silent or unfinished (often background noise): Whisper's text for it is not kept, so
                # words nobody said can never make it look like speech in the history.
                return False
            content["whisper" if source == "whisper" else "model_line"] = text
            line, whisper = content.get("model_line"), content.get("whisper")
            if line and whisper and disagree(line, whisper):
                record, kind = whisper, "whisper"
                content["heard_differently"] = True
            elif line:
                record, kind = line, "model"
                content.pop("heard_differently", None)
            else:
                record, kind = whisper or text, source
            if content.get("transcript") == record and content.get("transcript_source", "whisper") == kind:
                self.db.execute("UPDATE turns SET content=?,revision=revision+1 WHERE id=?", (encoded(content), turn))
                return True
            text, source = record, kind
            content["transcript"] = text
            content["transcript_source"] = source
            self.db.execute("UPDATE turns SET content=?,revision=revision+1 WHERE id=?", (encoded(content), turn))
            original = {"id": sid, "target": row["target"], "epoch": row["epoch"]}
            self._event(original, turn, "user.transcript", {"text": text, "source": source})
            # Invalidate any summary containing the now-corrected turn.
            self._stale(row["target"], turn)
            return True

    def interrupted(self, sid, owner, turn, heard):
        """A reply the user cut off by speaking over it: the history keeps what they heard of it (its completed phrases, already
        recorded, plus `heard`, the words of the phrase that was playing) and marks it interrupted. Recorded as the turn is being
        revoked, so not bound to the current epoch (like `transcript`). Returns the reply as heard, or None."""
        with self.transaction():
            self.session(sid, owner, active=False)
            row = self.db.execute("SELECT * FROM turns WHERE id=? AND session=?", (turn, sid)).fetchone()
            if not row:
                return None
            content = json.loads(row["content"])
            if content.get("interrupted") or content.get("superseded"):
                return None
            heard = " ".join(str(heard or "").split())[:4000]
            if heard:
                content["assistant"] = (content.get("assistant", "").rstrip() + " " + heard).strip()
            content["interrupted"] = True
            self.db.execute("UPDATE turns SET content=?,revision=revision+1 WHERE id=?", (encoded(content), turn))
            original = {"id": sid, "target": row["target"], "epoch": row["epoch"]}
            self._event(original, turn, "turn.interrupted", {"heard": heard})
            self._stale(row["target"], turn)
            return content.get("assistant", "")

    def held(self, sid, owner, turn, decision, reason=""):
        """The conversation model chose to say nothing (`silent`, [SILENT]) or to wait for the rest of the thought (`wait`, [WAIT]).
        The turn keeps the user's words for the panel but leaves the model's history (convo_history), and the decision is journaled
        for the developer view (`turn.held`, with a short reason). Not bound to the current epoch, like `transcript`."""
        if decision not in ("silent", "wait"):
            raise ValueError("Unknown held decision")
        with self.transaction():
            self.session(sid, owner, active=False)
            row = self.db.execute("SELECT * FROM turns WHERE id=? AND session=?", (turn, sid)).fetchone()
            if not row:
                return False
            content = json.loads(row["content"])
            if content.get("held") == decision:
                return False
            content["held"] = decision
            self._drop_whisper(content)
            self.db.execute("UPDATE turns SET content=?,revision=revision+1 WHERE id=?", (encoded(content), turn))
            original = {"id": sid, "target": row["target"], "epoch": row["epoch"]}
            self._event(original, turn, "turn.held", {"decision": decision, "reason": " ".join(str(reason or "").split())[:120]})
            return True

    @staticmethod
    def _drop_whisper(content):
        """Remove Whisper's text from a turn's content; the record falls back to the model's own line, or to nothing. A text turn's
        words are its input, never Whisper's, and stay."""
        if content.get("input") == "text":
            return False
        had =content.pop("whisper", None) is not None
        if content.get("transcript_source", "whisper") == "whisper" and "transcript" in content and not content.get("model_line"):
            content.pop("transcript", None)
            content.pop("transcript_source", None)
            had = True
        elif content.get("model_line") and content.get("transcript") != content["model_line"]:
            content["transcript"], content["transcript_source"] = content["model_line"], "model"
            had = True
        content.pop("heard_differently", None)
        return had

    def forget_whisper(self, sid, owner, turn):
        """A turn that got no reply (the model said nothing for it): any Whisper text it carries is dropped, so a noise turn never
        looks like speech in the history. Not bound to the current epoch, like `transcript`."""
        with self.transaction():
            self.session(sid, owner, active=False)
            row = self.db.execute("SELECT * FROM turns WHERE id=? AND session=?", (turn, sid)).fetchone()
            if not row:
                return False
            content = json.loads(row["content"])
            if not self._drop_whisper(content):
                return False
            self.db.execute("UPDATE turns SET content=?,revision=revision+1 WHERE id=?", (encoded(content), turn))
            self._stale(row["target"], turn)
            return True

    # -- timing marks kept across restarts (content-free; convo_metrics) ---------------------------------------------------------
    MARKS_KEPT = 20000

    def keep_mark(self, turn, mark):
        """Queue one content-free timing mark (convo_metrics) for the writer thread; never waits, never raises. Kept in
        `timing_marks`, at most MARKS_KEPT rows (the oldest go first)."""
        try:
            writer = getattr(self, "_writer", None)
            if writer is None:
                with self.lock:
                    writer = getattr(self, "_writer", None)
                    if writer is None:
                        writer = self._writer = MarkWriter(self._file(), self.MARKS_KEPT)
            writer.put(turn, mark)
        except Exception:
            pass

    def _file(self):
        path = getattr(self, "path", None)
        if not path:
            with self.lock:
                row = self.db.execute("PRAGMA database_list").fetchone()
                path = self.path = (row[2] if row else "") or ":memory:"
        return path

    def marks(self, limit=500, before=None):
        """The newest kept timing marks, oldest first: [{id, turn, name, at (wall time), fields}]. No content, ever."""
        writer = getattr(self, "_writer", None)
        if writer is not None:
            writer.flush()
        with self.lock:
            try:
                sql, args = "SELECT * FROM timing_marks", []
                if before is not None:
                    sql, args = sql + " WHERE id<?", [int(before)]
                rows = self.db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, min(5000, max(1, int(limit))))).fetchall()
            except sqlite3.OperationalError:
                return []
        return [{"id": r["id"], "turn": r["turn"], "name": r["name"], "at": r["at"], "fields": json.loads(r["fields"])} for r in rows[::-1]]

    def tool_used(self, sid, owner, turn, entry):
        """One tool use in a turn (convo_tools.history_entry): kept in the turn's `tools` (at most HISTORY_LIMIT) so the model's
        history shows the dated call and its short result, and journaled as `tool.done` or `tool.failed` (name and argument; the
        journal keeps the result too). Not bound to the current epoch, like `transcript`: a result that arrives after the user
        interrupted is kept."""
        from .convo_tools import HISTORY_LIMIT
        if not isinstance(entry, dict) or not entry.get("name"):
            raise ValueError("Invalid tool use")
        with self.transaction():
            self.session(sid, owner, active=False)
            row = self.db.execute("SELECT * FROM turns WHERE id=? AND session=?", (turn, sid)).fetchone()
            if not row:
                return False
            content = json.loads(row["content"])
            uses = content.setdefault("tools", [])
            if len(uses) >= HISTORY_LIMIT:
                return False
            uses.append(entry)
            self.db.execute("UPDATE turns SET content=?,revision=revision+1 WHERE id=?", (encoded(content), turn))
            original = {"id": sid, "target": row["target"], "epoch": row["epoch"]}
            self._event(original, turn, "tool.done" if entry.get("ok") else "tool.failed",
                        {"name": entry["name"], "query": entry.get("argument", ""), "at": entry.get("at", ""), "result": entry.get("result", "")})
            self._stale(row["target"], turn)
            return True

    def playback_started(self, sid, owner, epoch, turn, phrase_id):
        # Start is not a claim that the user heard an entire phrase. Only the
        # later completion acknowledgement contributes text to working context.
        return self.append(sid, owner, epoch, turn, "assistant.playback_started", {"phrase_id": phrase_id})

    def events(self, target, after=0, limit=200):
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE target=? AND seq>? ORDER BY seq LIMIT ?", (target, after, min(500, max(1, limit)))).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def events_tail(self, target, limit=60, before=None):
        """The newest `limit` events (oldest first), optionally only those older than `before`, plus whether earlier ones remain."""
        limit = min(200, max(1, int(limit)))
        sql, args = "SELECT * FROM events WHERE target=?", [target]
        if before is not None:
            sql += " AND seq<?"
            args.append(int(before))
        with self.lock:
            rows = self.db.execute(sql + " ORDER BY seq DESC LIMIT ?", (*args, limit + 1)).fetchall()
        more, rows = len(rows) > limit, rows[:limit][::-1]
        return {"events": [{**dict(row), "payload": json.loads(row["payload"])} for row in rows], "more": more}

    def history_page(self, target, *, after=0, tail=None, before=None):
        """One read for the panel: events after a known sequence (polling), or the newest page (optionally older than one)."""
        if tail is None and before is None:
            return {"events": self.events(target, int(after)), "more": False}
        return self.events_tail(target, 60 if tail is None else tail, before)

    def memory(self, target):
        with self.lock:
            row = self.db.execute("SELECT * FROM memories WHERE target=?", (target,)).fetchone()
            return {"summary": row["summary"], "covered": json.loads(row["covered"]), "revision": row["revision"]} if row else {"summary": "", "covered": [], "revision": 0}

    # -- memory chapters (convo_memory): appended summaries of the older conversation -------------------------------------------
    # The `memories` row keeps the joined text and every covered turn, so an older Convo after a rollback still reads a valid
    # summary. Chapters are valid only while they carry the row's revision; if an older Convo changed the row in the meantime the
    # row wins (it becomes the first chapter on the next write), and a missing row is rebuilt from the chapters.

    def _chapter_rows(self, target):
        return [dict(r) for r in self.db.execute("SELECT * FROM memory_chapters WHERE target=? ORDER BY seq", (target,)).fetchall()]

    def chapters(self, target):
        with self.lock:
            rows = self._chapter_rows(target)
            if not rows:
                return []
            memory = self.memory(target)
            if memory["revision"] == 0 and not memory["covered"]:
                with self.db:
                    self._write_memory(target, rows, rows[-1]["revision"])
                memory = self.memory(target)
            if rows[-1]["revision"] != memory["revision"]:
                return []
            return [{**r, "covered": json.loads(r["covered"])} for r in rows]

    @staticmethod
    def joined(chapters):
        return "\n\n".join(c["summary"].strip() for c in chapters if c["summary"].strip())

    def _write_memory(self, target, rows, revision):
        covered = [turn for r in rows for turn in (json.loads(r["covered"]) if isinstance(r["covered"], str) else r["covered"])]
        self.db.execute("INSERT OR REPLACE INTO memories VALUES(?,?,?,?)", (target, self.joined(rows), encoded(covered), revision))

    def _stale(self, target, turn):
        """A summarised turn changed (a late transcript, a newly heard phrase, an interruption): only its chapter is marked to be
        rewritten and its old text serves until the new one is ready. An older summary without chapters is kept as it is."""
        for r in self._chapter_rows(target):
            if turn in json.loads(r["covered"]):
                self.db.execute("UPDATE memory_chapters SET stale=1 WHERE target=? AND seq=?", (target, r["seq"]))
        if getattr(self, "_recall", None):
            self.db.execute("DELETE FROM memory_recall WHERE target=? AND turn=?", (target, turn))      # re-read at the next lookup

    RECALL_LIMIT = 4000

    def _recall_index(self):
        """The full-text index for recall_conversation (SQLite FTS5: trigram for any language, else words), or None without FTS5.
        Older Convo ignores it; entries are rebuilt from the turns themselves, so it can never disagree with them for long."""
        if getattr(self, "_recall", "unset") != "unset":
            return self._recall
        self._recall = None
        for tokenizer in ("trigram", "unicode61"):
            try:
                self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_recall USING fts5(target UNINDEXED, turn UNINDEXED, text, tokenize='%s')" % tokenizer)
                self._recall = tokenizer
                break
            except sqlite3.OperationalError:
                continue
        return self._recall

    @staticmethod
    def _recall_text(content):
        user = content.get("transcript") or content.get("user_line") or content.get("note") or ""
        return ("User: " + user + "\nAssistant: " + content.get("assistant", "")).strip()

    def recall(self, target, query, limit=5):
        """What was said in the summarised part of this conversation that matches `query` (this target only), oldest first, as
        dated text for the model. Only turns the chapters cover: the rest is already in the model's view word for word."""
        words = [w for w in re.findall(r"\w+", str(query or "").lower()) if len(w) > 1 or w.isdigit()][:24]
        if not words:
            return ""
        with self.transaction():
            covered = self.memory(target)["covered"]
            if not covered:
                return ""
            rows = {r["id"]: r for r in self.db.execute(
                "SELECT id, rowid AS rid, content, (SELECT min(time) FROM events WHERE events.target=turns.target AND events.turn=turns.id) AS created "
                "FROM turns WHERE target=? AND id IN (%s)" % ",".join("?" * len(covered)),
                [target] + list(covered)).fetchall()}
            hits = []
            tokenizer = self._recall_index()
            if tokenizer:
                indexed = {r["turn"] for r in self.db.execute("SELECT turn FROM memory_recall WHERE target=?", (target,))}
                for turn in set(rows) - indexed:
                    self.db.execute("INSERT INTO memory_recall(target, turn, text) VALUES(?,?,?)",
                                    (target, turn, self._recall_text(json.loads(rows[turn]["content"]))))
                terms = [w for w in words if tokenizer != "trigram" or len(w) >= 3]
                if terms:
                    match = " OR ".join('"%s"' % w.replace('"', '') for w in terms)
                    hits = [r["turn"] for r in self.db.execute(
                        "SELECT turn FROM memory_recall WHERE memory_recall MATCH ? AND target=? ORDER BY bm25(memory_recall) LIMIT ?",
                        (match, target, int(limit) * 10))]
            # The candidates (or, with no index or only short words, every covered turn) ranked by every word asked for, short ones
            # such as a number included: a whole-word match counts twice a match inside a longer word.
            scored = []
            for order, turn in enumerate(hits or rows):
                text = self._recall_text(json.loads(rows[turn]["content"])).lower()
                tokens = re.findall(r"\w+", text)
                score = sum(2 * tokens.count(w) + (text.count(w) - tokens.count(w) if len(w) >= 3 else 0) for w in words)
                if score:
                    scored.append((-score, order, turn))
            hits = [turn for _, _, turn in sorted(scored)[:int(limit)]]
            found = sorted((rows[t] for t in hits if t in rows), key=lambda r: r["rid"])
            parts, size = [], 0
            for row in found:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["created"])) if row["created"] else ""
                part = ((when + " ") if when else "") + self._recall_text(json.loads(row["content"]))
                if size + len(part) > self.RECALL_LIMIT:
                    part = part[:max(0, self.RECALL_LIMIT - size)]
                parts.append(part)
                size += len(part)
                if size >= self.RECALL_LIMIT:
                    break
            return "\n\n".join(p for p in parts if p)

    def _watermark(self, target):
        rows = self._chapter_rows(target)
        if rows and rows[-1]["revision"] == self.memory(target)["revision"]:
            return rows[-1]["last_rowid"]
        return 0

    def turns_by_id(self, target, ids):
        with self.lock:
            wanted = set(ids)
            rows = self.db.execute("SELECT * FROM turns WHERE target=? ORDER BY rowid", (target,)).fetchall()
            return [{**dict(row), "content": json.loads(row["content"])} for row in rows if row["id"] in wanted]

    def replace_chapter(self, target, seq, expected, summary):
        """Rewrite one stale chapter (its covered turns are unchanged). False if it changed meanwhile."""
        if not isinstance(summary, str) or not summary.strip():
            return False
        with self.transaction():
            row = next((r for r in self.chapters(target) if r["seq"] == seq), None)
            if not row or row["summary"] != expected:
                return False
            revision = self.memory(target)["revision"] + 1
            self.db.execute("UPDATE memory_chapters SET summary=?,stale=0 WHERE target=? AND seq=?", (summary.strip(), target, seq))
            self.db.execute("UPDATE memory_chapters SET revision=? WHERE target=?", (revision, target))
            self._write_memory(target, self._chapter_rows(target), revision)
            return True

    def condense(self, target, seqs, expected, summary):
        """Replace the consecutive chapters `seqs` (whose joined text was `expected`) by one condensed chapter."""
        if not isinstance(summary, str) or not summary.strip() or not seqs:
            return False
        with self.transaction():
            rows = [r for r in self.chapters(target) if r["seq"] in seqs]
            if len(rows) != len(seqs) or self.joined(rows) != expected:
                return False
            covered = [turn for r in rows for turn in r["covered"]]
            for r in rows[1:]:
                self.db.execute("DELETE FROM memory_chapters WHERE target=? AND seq=?", (target, r["seq"]))
            revision = self.memory(target)["revision"] + 1
            # A correction still waiting in one of them stays waiting in the condensed chapter.
            stale = 1 if any(r["stale"] for r in rows) else 0
            self.db.execute("UPDATE memory_chapters SET summary=?,covered=?,last_rowid=?,stale=? WHERE target=? AND seq=?",
                            (summary.strip(), encoded(covered), rows[-1]["last_rowid"], stale, target, rows[0]["seq"]))
            self.db.execute("UPDATE memory_chapters SET revision=? WHERE target=?", (revision, target))
            self._write_memory(target, self._chapter_rows(target), revision)
            return True

    def context(self, target):
        """The summary and the turns it does not cover. With valid chapters only turns after the last chapter are read, so a long
        conversation does not load every turn ever said."""
        with self.lock:
            memory = self.memory(target)
            mark = self._watermark(target)
            rows = self.db.execute("SELECT * FROM turns WHERE target=? AND rowid>? ORDER BY rowid", (target, mark)).fetchall()
            covered = set(memory["covered"])
            turns = [{**dict(row), "content": json.loads(row["content"])} for row in rows if row["id"] not in covered]
            return {"memory": {**memory, "standing": self.standing(target)}, "turns": turns}

    def set_standing(self, target, expected, text):
        """Replace the standing notes (a step of their own after a chapter), if they are still `expected`. Never clears them."""
        if not isinstance(text, str) or not text.strip():
            return False
        with self.transaction():
            if self.standing(target) != expected:
                return False
            self.db.execute("INSERT OR REPLACE INTO memory_standing VALUES(?,?,?)", (target, text.strip()[:1500], time.time()))
            return True

    def settle_chapter(self, target, seq):
        """A stale chapter too long to rewrite keeps its text: it is no longer marked to be rewritten (its text is unchanged)."""
        with self.transaction():
            self.db.execute("UPDATE memory_chapters SET stale=0 WHERE target=? AND seq=?", (target, seq))

    def standing(self, target):
        """The user's standing notes for this conversation (name, the reply language they asked for, lasting preferences), kept
        in view after the chapters so they are never buried in a summary. Rewritten only when a chapter is written."""
        with self.lock:
            row = self.db.execute("SELECT text FROM memory_standing WHERE target=?", (target,)).fetchone()
            return row["text"] if row else ""

    def snapshot(self, sid, owner, recent=6, count=None):
        """The oldest uncovered turns to summarise: `count` of them (default: all but the newest `recent`, at least six kept), never
        one of the newest `recent`, and only settled ones (with `count`, an unsettled turn ends the prefix)."""
        with self.lock:
            session = self.session(sid, owner)
            context = self.context(session["target"])
            turns = context["turns"]
            if count is None:
                prefix = turns[:-max(6, recent)]
            else:
                prefix = turns[:min(int(count), max(0, len(turns) - max(1, recent)))]
                prefix = prefix[:next((n for n, t in enumerate(prefix) if not t["settled"]), len(prefix))]
            if not prefix or any(not t["settled"] for t in prefix):
                return None
            return {"session": sid, "owner": owner, "target": session["target"], "generation": session["generation"], "memory": context["memory"], "turns": prefix, "fingerprint": digest(prefix)}

    def splice(self, snapshot, summary, standing=None):
        """Append `summary` as the next chapter, covering the snapshot's turns, if nothing changed since the snapshot. `standing`,
        when given, replaces the standing notes in the same step ('NONE' or empty clears them)."""
        if not isinstance(summary, str) or not summary.strip():
            return False
        with self.transaction():
            try:
                session = self.session(snapshot["session"], snapshot["owner"])
            except ValueError:
                return False
            if session["target"] != snapshot["target"] or session["generation"] != snapshot["generation"]:
                return False
            context = self.context(session["target"])
            prefix = context["turns"][:len(snapshot["turns"])]
            if context["memory"] != snapshot["memory"] or digest(prefix) != snapshot["fingerprint"]:
                return False
            target, old = session["target"], context["memory"]
            revision = old["revision"] + 1
            rows = self._chapter_rows(target)
            if rows and rows[-1]["revision"] != old["revision"]:
                self.db.execute("DELETE FROM memory_chapters WHERE target=?", (target,))   # the row changed elsewhere: it wins
                rows = []
            seq = (rows[-1]["seq"] + 1) if rows else 0
            if not rows and old["summary"].strip():
                # An older summary (written before chapters, or by an older Convo) becomes the first chapter.
                self.db.execute("INSERT INTO memory_chapters VALUES(?,?,?,?,?,?,0,?)",
                                (target, 0, old["summary"].strip(), encoded(old["covered"]), self._last_rowid(target, old["covered"]), revision, time.time()))
                seq = 1
            ids = [t["id"] for t in prefix]
            self.db.execute("INSERT INTO memory_chapters VALUES(?,?,?,?,?,?,0,?)",
                            (target, seq, summary.strip(), encoded(ids), self._last_rowid(target, ids), revision, time.time()))
            self.db.execute("UPDATE memory_chapters SET revision=? WHERE target=?", (revision, target))
            self._write_memory(target, self._chapter_rows(target), revision)
            if isinstance(standing, str):
                text = standing.strip()
                if not text or text.upper().strip('. ') == "NONE":
                    self.db.execute("DELETE FROM memory_standing WHERE target=?", (target,))
                else:
                    self.db.execute("INSERT OR REPLACE INTO memory_standing VALUES(?,?,?)", (target, text[:1500], time.time()))
            self._event(session, "", "context.compacted", {"exchanges": len(prefix), "chapter": seq})
            return True

    def _last_rowid(self, target, ids):
        if not ids:
            return 0
        row = self.db.execute("SELECT max(rowid) FROM turns WHERE target=? AND id IN (%s)" % ",".join("?" * len(ids)), [target] + list(ids)).fetchone()
        return int(row[0] or 0)

    def submit(self, sid, owner, epoch, turn, request_key, prompt, target=None):
        """Queue one task. `target` is the chat whose agent does it (default: the session's own chat). A task for another chat than
        the conversation's own key (a conversation across chats, or a named chat) keeps the conversation it came from in
        `job_scope`, so its events, its list and its completion notice stay with that conversation; an older Convo ignores the
        table and still dispatches the job to its chat."""
        with self.transaction():
            session = self.session(sid, owner)
            if epoch != session["epoch"]:
                raise ValueError("Stale job proposal")
            target = str(target or session["target"])
            key = sid + ":" + request_key
            row = self.db.execute("SELECT * FROM jobs WHERE request_key=?", (key,)).fetchone()
            if row:
                same = row["prompt"] == prompt or row["prompt"].startswith(prompt + ADDED)      # (the user may have added to it since)
                if not same or row["target"] != target or row["turn"] != turn:
                    raise ValueError("Idempotency key reused for another job")
                return dict(row)
            job = uuid.uuid4().hex
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,'queued','')", (job, key, sid, target, turn, prompt))
            if target != session["target"]:
                self.db.execute("INSERT OR REPLACE INTO job_scope VALUES(?,?)", (job, session["target"]))
            row = self.db.execute("SELECT content FROM turns WHERE id=? AND session=?", (turn, sid)).fetchone()
            if row:
                content = json.loads(row["content"])
                content.setdefault("jobs", []).append(job)
                self.db.execute("UPDATE turns SET content=?,revision=revision+1 WHERE id=?", (encoded(content), turn))
            self._event(session, turn, "job.queued", {"job_id": job})
        return self.job(job)

    def job(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Unknown job")
            return dict(row)

    def note_turn(self, sid, owner, epoch, note, kind, payload=None):
        """A turn for something Convo says on its own (a timer that went off): the note is what the model's history shows as its
        cause, and the event journals it. Returns the new turn's id."""
        with self.transaction():
            session = self.session(sid, owner)
            if session['epoch'] != epoch:
                raise ValueError('Stale session for a notice')
            turn = uuid.uuid4().hex
            self.db.execute('INSERT INTO turns VALUES(?,?,?,?,?,0,0)', (turn, sid, session['target'], epoch, encoded({'note': str(note)[:300]})))
            self._event(session, turn, str(kind), dict(payload or {}))
            return turn

    def scope_of(self, job):
        """The conversation a job belongs to: the one that asked for it (`job_scope`), else its chat."""
        row = self.db.execute("SELECT scope FROM job_scope WHERE job=?", (job["id"],)).fetchone()
        return row["scope"] if row else job["target"]

    def claim_notice(self, sid, owner, epoch, job_id):
        with self.transaction():
            session = self.session(sid, owner)
            job = self.job(job_id)
            scope = self.scope_of(job)
            # A chat's own conversation says the completions of its own session's tasks, as before. A conversation across chats
            # outlives a voice session: a task finished after voice was stopped is said by the next session of that conversation.
            own = job['session'] == sid or scope != job['target']
            if session['epoch'] != epoch or scope != session['target'] or not own or job['status'] not in {'completed','failed','uncertain'}:
                raise ValueError('Job is not eligible for this conversation notice')
            turn = uuid.uuid4().hex
            cursor = self.db.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?)', (job_id, sid, turn))
            if not cursor.rowcount: return None
            content = {'note': 'Completion update for previously authorized work', 'jobs': [job_id]}
            self.db.execute('INSERT INTO turns VALUES(?,?,?,?,?,0,0)', (turn, sid, session['target'], epoch, encoded(content)))
            self._event(session, turn, 'job.notice_claimed', {'job_id': job_id})
            return {**job, 'notice_turn': turn}

    def release_notice(self, sid, owner, job_id, turn):
        """A notice nothing was heard of: its claim is given back so it can be offered again. Returns whether it was."""
        with self.transaction():
            self.session(sid, owner)
            return bool(self.db.execute('DELETE FROM notices WHERE job=? AND turn=?', (job_id, turn)).rowcount)

    def jobs(self, target):
        """The jobs of a conversation: those of its own chat, and those it handed to other chats (`job_scope`)."""
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM jobs WHERE (target=? AND id NOT IN (SELECT job FROM job_scope)) OR id IN (SELECT job FROM job_scope WHERE scope=?) "
                "ORDER BY rowid", (target, target))]

    # ── Changes to work that is going on (`job_steers`; an older Convo ignores the table) ──
    # status: added (the task had not started: it is in its text, so an older Convo has it too), waiting (passed to the run, not in yet), taken (in
    # the run), interrupted (without the Steering plugin: Agent Zero's own interruption), follow_up (the run had ended: it became a
    # task of its own), dropped (the run was stopped or failed, or Agent Zero restarted).
    def steered(self, session_id, turn, target, text, status, *, job="", steer_id=""):
        """Keep one change and journal it with the conversation that asked for it."""
        steer_id = str(steer_id or uuid.uuid4().hex)
        with self.transaction():
            session = self.session(session_id, active=False)
            if job:
                session["target"] = self.scope_of(self.job(job))
            self.db.execute("INSERT INTO job_steers VALUES(?,?,?,?,?,?,?,?)",
                            (steer_id, job, target, session_id, turn, text, status, time.time()))
            if status == "added":
                self.db.execute("UPDATE jobs SET prompt=prompt||? WHERE id=?", (ADDED + text, job))
            self._event(session, turn, "job.steered", {"steer_id": steer_id, "job_id": job, "chat": target, "how": status, "text": text[:400]})
        return steer_id

    def steer_status(self, steer_id, status):
        """What became of a change that was waiting. False when it was settled already."""
        with self.transaction():
            row = self.db.execute("SELECT * FROM job_steers WHERE id=?", (steer_id,)).fetchone()
            if not row or not self.db.execute("UPDATE job_steers SET status=? WHERE id=? AND status='waiting'", (status, steer_id)).rowcount:
                return False
            session = self.session(row["session"], active=False)
            if row["job"]:
                session["target"] = self.scope_of(self.job(row["job"]))
            self._event(session, row["turn"], "job.steer_" + status, {"steer_id": steer_id, "job_id": row["job"], "chat": row["target"], "text": row["text"][:400]})
            return True

    def steers(self, job_ids=(), status=None):
        """The changes of these jobs (all, when none are named), oldest first."""
        with self.lock:
            rows = [dict(row) for row in self.db.execute("SELECT * FROM job_steers ORDER BY rowid")]
        wanted = set(job_ids or ())
        return [row for row in rows if (not wanted or row["job"] in wanted) and (status is None or row["status"] == status)]

    def follow_up(self, steer_id):
        """A change the run could not take up any more becomes a task of its own in the same chat, so its answer is still said.
        Returns the job, or None when the change was settled already."""
        with self.transaction():
            row = self.db.execute("SELECT * FROM job_steers WHERE id=? AND status='waiting'", (steer_id,)).fetchone()
            if not row:
                return None
            session = self.session(row["session"], active=False)
            scope = self.scope_of(self.job(row["job"])) if row["job"] else session["target"]
            job = uuid.uuid4().hex
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,'queued','')",
                            (job, row["session"] + ":steer:" + steer_id, row["session"], row["target"], row["turn"], row["text"]))
            if row["target"] != scope:
                self.db.execute("INSERT OR REPLACE INTO job_scope VALUES(?,?)", (job, scope))
            self.db.execute("UPDATE job_steers SET status='follow_up' WHERE id=?", (steer_id,))
            session["target"] = scope
            self._event(session, row["turn"], "job.queued", {"job_id": job, "follows": row["job"], "steer_id": steer_id})
        return self.job(job)

    def transition(self, job_id, expected, status, result=""):
        allowed = {"queued": {"dispatching", "cancelled"}, "dispatching": {"running", "queued", "uncertain", "failed"}, "running": {"completed", "failed", "cancel_requested", "uncertain"}, "cancel_requested": {"cancelled", "completed", "uncertain"}}
        if status not in allowed.get(expected, set()):
            raise ValueError("Invalid job transition")
        with self.transaction():
            cursor = self.db.execute("UPDATE jobs SET status=?,result=? WHERE id=? AND status=?", (status, result, job_id, expected))
            if cursor.rowcount:
                job = self.job(job_id)
                session = self.session(job["session"], active=False)
                session["target"] = self.scope_of(job)          # journalled with the conversation that asked for the task
                self._event(session, job["turn"], "job." + status, {"job_id": job_id, "result": result})
            return bool(cursor.rowcount)
