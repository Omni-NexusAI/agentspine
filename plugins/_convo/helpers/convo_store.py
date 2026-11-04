"""Durable event journal and idempotent jobs. No raw audio is persisted."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, owner TEXT NOT NULL, target TEXT NOT NULL, epoch INTEGER NOT NULL, active INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, target TEXT NOT NULL, epoch INTEGER NOT NULL, turn TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, time REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS target_events ON events(target,seq);
        CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY, session TEXT NOT NULL, target TEXT NOT NULL, epoch INTEGER NOT NULL, content TEXT NOT NULL, settled INTEGER NOT NULL, revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS memories(target TEXT PRIMARY KEY, summary TEXT NOT NULL, covered TEXT NOT NULL, revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, session TEXT NOT NULL, target TEXT NOT NULL, turn TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notices(job TEXT PRIMARY KEY, session TEXT NOT NULL, turn TEXT NOT NULL);
        """)
        with self.transaction():
            # A process restart cannot determine whether an external side effect happened.
            self.db.execute("UPDATE jobs SET status='uncertain' WHERE status IN ('dispatching','running','cancel_requested')")
            for row in self.db.execute("SELECT * FROM sessions WHERE active=1").fetchall():
                self._finish_pending(dict(row), "restart")
            self.db.execute("UPDATE sessions SET active=0,epoch=epoch+1 WHERE active=1")

    @contextmanager
    def transaction(self):
        with self.lock, self.db:
            yield

    def close(self):
        with self.lock:
            self.db.close()

    def session(self, sid, owner=None, active=True):
        with self.lock:
            row = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row or (owner is not None and row["owner"] != owner) or (active and not row["active"]):
                raise ValueError("Unknown or inactive voice session")
            return dict(row)

    def start(self, owner, target):
        with self.transaction():
            if self.db.execute("SELECT 1 FROM sessions WHERE active=1").fetchone():
                raise ValueError("Another voice session is active; stop it before starting a new one")
            sid = uuid.uuid4().hex
            self.db.execute("INSERT INTO sessions(id,owner,target,epoch,active) VALUES(?,?,?,0,1)", (sid, owner, target))
        return self.session(sid, owner)

    def interrupt(self, sid, owner):
        with self.transaction():
            self._finish_pending(self.session(sid, owner), "interrupted")
            self.db.execute("UPDATE sessions SET epoch=epoch+1 WHERE id=?", (sid,))
        return self.session(sid, owner)

    def stop(self, sid, owner):
        with self.transaction():
            self._finish_pending(self.session(sid, owner), "stopped")
            self.db.execute("UPDATE sessions SET active=0,epoch=epoch+1 WHERE id=?", (sid,))

    def retarget(self, sid, owner, target):
        with self.transaction():
            old = self.session(sid, owner)
            self._finish_pending(old, "retargeted")
            self._event(old, "", "segment.closed", {"next_target": target})
            self.db.execute("UPDATE sessions SET target=?,epoch=epoch+1,generation=generation+1 WHERE id=?", (target, sid))
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

    def supersede(self, sid, owner, epoch, turn, by):
        """A newer fragment took over this turn's reply before it was heard: the turn leaves model history and its queued work
        is cancelled. Work that already started is left alone (it needs its own cancel). Returns the cancelled job IDs."""
        with self.transaction():
            session = self.session(sid, owner)
            row = self.db.execute("SELECT * FROM turns WHERE id=? AND session=?", (turn, sid)).fetchone()
            if session["epoch"] != epoch or not row:
                return []
            content = json.loads(row["content"])
            content["superseded"] = by
            self.db.execute("UPDATE turns SET content=?,settled=1,revision=revision+1 WHERE id=?", (encoded(content), turn))
            self._event(session, turn, "turn.superseded", {"by": by})
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
            content["assistant"] = (content.get("assistant", "") + " " + text).strip()
            self.db.execute("UPDATE turns SET content=?,revision=revision+1 WHERE id=?", (encoded(content), turn))
            memory = self.memory(row["target"])
            if turn in memory["covered"]:
                self.db.execute("DELETE FROM memories WHERE target=?", (row["target"],))
            self._event(session, turn, "assistant.delivered", {"text": text, "phrase_id": phrase_id})
            return True

    def transcript(self, sid, owner, turn, text, epoch=None):
        with self.transaction():
            self.session(sid, owner, active=False)
            row = self.db.execute("SELECT * FROM turns WHERE id=? AND session=?", (turn, sid)).fetchone()
            if not row or (epoch is not None and row["epoch"] != epoch):
                return False
            content = json.loads(row["content"])
            if content.get("transcript") == text:
                return True
            content["transcript"] = text
            self.db.execute("UPDATE turns SET content=?,revision=revision+1 WHERE id=?", (encoded(content), turn))
            original = {"id": sid, "target": row["target"], "epoch": row["epoch"]}
            self._event(original, turn, "user.transcript", {"text": text})
            # Invalidate any summary containing the now-corrected turn.
            memory = self.memory(row["target"])
            if turn in memory["covered"]:
                self.db.execute("DELETE FROM memories WHERE target=?", (row["target"],))
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

    def context(self, target):
        with self.lock:
            memory = self.memory(target)
            rows = self.db.execute("SELECT * FROM turns WHERE target=? ORDER BY rowid", (target,)).fetchall()
            turns = [{**dict(row), "content": json.loads(row["content"])} for row in rows if row["id"] not in memory["covered"]]
            return {"memory": memory, "turns": turns}

    def snapshot(self, sid, owner, recent=6):
        with self.lock:
            session = self.session(sid, owner)
            context = self.context(session["target"])
            turns = context["turns"]
            prefix = turns[:-max(6, recent)]
            if not prefix or any(not t["settled"] for t in prefix):
                return None
            return {"session": sid, "owner": owner, "target": session["target"], "generation": session["generation"], "memory": context["memory"], "turns": prefix, "fingerprint": digest(prefix)}

    def splice(self, snapshot, summary):
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
            covered = snapshot["memory"]["covered"] + [t["id"] for t in prefix]
            self.db.execute("INSERT OR REPLACE INTO memories VALUES(?,?,?,?)", (session["target"], summary.strip(), encoded(covered), context["memory"]["revision"] + 1))
            self._event(session, "", "context.compacted", {"exchanges": len(prefix)})
            return True

    def submit(self, sid, owner, epoch, turn, request_key, prompt):
        with self.transaction():
            session = self.session(sid, owner)
            if epoch != session["epoch"]:
                raise ValueError("Stale job proposal")
            key = sid + ":" + request_key
            row = self.db.execute("SELECT * FROM jobs WHERE request_key=?", (key,)).fetchone()
            if row:
                if row["prompt"] != prompt or row["target"] != session["target"] or row["turn"] != turn:
                    raise ValueError("Idempotency key reused for another job")
                return dict(row)
            job = uuid.uuid4().hex
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,'queued','')", (job, key, sid, session["target"], turn, prompt))
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

    def claim_notice(self, sid, owner, epoch, job_id):
        with self.transaction():
            session = self.session(sid, owner)
            job = self.job(job_id)
            if session['epoch'] != epoch or job['target'] != session['target'] or job['session'] != sid or job['status'] not in {'completed','failed','uncertain'}:
                raise ValueError('Job is not eligible for this conversation notice')
            turn = uuid.uuid4().hex
            cursor = self.db.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?)', (job_id, sid, turn))
            if not cursor.rowcount: return None
            content = {'note': 'Completion update for previously authorized work', 'jobs': [job_id]}
            self.db.execute('INSERT INTO turns VALUES(?,?,?,?,?,0,0)', (turn, sid, session['target'], epoch, encoded(content)))
            self._event(session, turn, 'job.notice_claimed', {'job_id': job_id})
            return {**job, 'notice_turn': turn}

    def jobs(self, target):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE target=? ORDER BY rowid", (target,))]

    def transition(self, job_id, expected, status, result=""):
        allowed = {"queued": {"dispatching", "cancelled"}, "dispatching": {"running", "queued", "uncertain", "failed"}, "running": {"completed", "failed", "cancel_requested", "uncertain"}, "cancel_requested": {"cancelled", "completed", "uncertain"}}
        if status not in allowed.get(expected, set()):
            raise ValueError("Invalid job transition")
        with self.transaction():
            cursor = self.db.execute("UPDATE jobs SET status=?,result=? WHERE id=? AND status=?", (status, result, job_id, expected))
            if cursor.rowcount:
                job = self.job(job_id)
                session = self.session(job["session"], active=False)
                session["target"] = job["target"]
                self._event(session, job["turn"], "job." + status, {"job_id": job_id, "result": result})
            return bool(cursor.rowcount)
