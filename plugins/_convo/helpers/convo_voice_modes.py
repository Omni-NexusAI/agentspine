"""A conversation's permission mode and reasoning level: the panel's two menus, also changed by voice.

Kept per conversation (a chat, or the global conversation) in the journal (`voice_modes`), so a reload, a lost connection, the other page and
the next voice session see the same. A conversation that never chose starts from Settings (`permission_default`, `reasoning_default`).

Permission (what it takes to run a tool that starts, changes or saves something; convo_policy.authorize reads it):
- "ask"   the built-in rule: work runs when the user's own audio carries the request, and is asked about once otherwise.
- "allow" what the model calls in a conversation the user started runs without a question, except while someone else's text (a web page,
          a chat, a memory) is in the request: then the built-in rule decides, so text from outside never starts anything by itself.
Planning (convo_planning, convo_modes) is shown in the same menu but is a mode of its own on top of either: its floor is never loosened, by
"allow" or by a remembered answer.

Remembered answers (`voice_allowed`): "always" to one of Convo's questions about a kind of work (delegating, changing a task, ...) lets that
kind run without the question from then on, in every conversation, as "allow" does for everything. Settings lists them and forgets them.

Reasoning (how much the conversation model thinks before it answers; api/realtime and the gateway turn it into request fields):
- "auto"  no thinking, so the first sound comes at once, unless the model judges that a question needs it and calls the `think` tool.
- "off", "low", "medium", "high"  every reply, as far as the model takes it (convo_reasoning says what it takes).
"""
from __future__ import annotations

import time

PERMISSIONS = ('ask', 'allow')
REASONING = ('auto', 'off', 'low', 'medium', 'high')
# The kinds of work a "yes, and don't ask again" may be remembered for. Never a change of mode (planning keeps its own rule) and never the
# hand-over of a plan (it starts what was planned).
REMEMBERABLE = ('delegate_task', 'steer_task', 'cancel_task', 'remember', 'change_setting', 'set_timer', 'open')
SCHEMA = ("CREATE TABLE IF NOT EXISTS voice_modes(target TEXT PRIMARY KEY, permission TEXT NOT NULL, reasoning TEXT NOT NULL, time REAL NOT NULL);"
          "CREATE TABLE IF NOT EXISTS voice_allowed(tool TEXT PRIMARY KEY, time REAL NOT NULL);")


class VoiceModes:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        with store.transaction():
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    store.db.execute(statement)

    @staticmethod
    def defaults(config):
        config = config if isinstance(config, dict) else {}
        permission = config.get('permission_default')
        reasoning = config.get('reasoning_default')
        return {'permission': permission if permission in PERMISSIONS else 'ask', 'reasoning': reasoning if reasoning in REASONING else 'auto'}

    def _row(self, target):
        with self.store.lock:
            return self.store.db.execute("SELECT permission, reasoning FROM voice_modes WHERE target=?", (str(target or ''),)).fetchone()

    def get(self, target, config=None):
        """{'permission', 'reasoning', 'chosen'}: what this conversation uses. Each follows Settings until it is chosen here (kept as '' until
        then, so choosing one never fixes the other); `chosen` says whether either was."""
        found = self.defaults(config)
        row = self._row(target)
        if row is not None:
            if row[0] in PERMISSIONS:
                found['permission'] = row[0]
            if row[1] in REASONING:
                found['reasoning'] = row[1]
        return {**found, 'chosen': row is not None and (row[0] in PERMISSIONS or row[1] in REASONING)}

    def set(self, target, config=None, *, permission=None, reasoning=None):
        """Change one or both for this conversation. Returns get(). Unknown values are refused (ValueError)."""
        if permission is not None and permission not in PERMISSIONS:
            raise ValueError('Unknown permission mode')
        if reasoning is not None and reasoning not in REASONING:
            raise ValueError('Unknown reasoning level')
        with self.store.transaction():
            row = self._row(target)
            kept_permission = permission if permission is not None else (row[0] if row is not None else '')
            kept_reasoning = reasoning if reasoning is not None else (row[1] if row is not None else '')
            self.store.db.execute("INSERT INTO voice_modes(target,permission,reasoning,time) VALUES(?,?,?,?) ON CONFLICT(target) DO UPDATE "
                                  "SET permission=excluded.permission, reasoning=excluded.reasoning, time=excluded.time",
                                  (str(target or ''), kept_permission, kept_reasoning, self.clock()))
        return self.get(target, config)

    # ---- remembered answers
    @staticmethod
    def remembers(tool):
        return tool in REMEMBERABLE

    def allow(self, tool):
        if not self.remembers(tool):
            raise ValueError('That kind of action is always asked about')
        with self.store.transaction():
            self.store.db.execute("INSERT OR REPLACE INTO voice_allowed(tool,time) VALUES(?,?)", (tool, self.clock()))

    def forget(self, tool):
        with self.store.transaction():
            self.store.db.execute("DELETE FROM voice_allowed WHERE tool=?", (str(tool or ''),))

    def allowed(self):
        with self.store.lock:
            return [row[0] for row in self.store.db.execute("SELECT tool FROM voice_allowed ORDER BY time")]

    def is_allowed(self, tool):
        if not self.remembers(tool):
            return False
        with self.store.lock:
            return self.store.db.execute("SELECT 1 FROM voice_allowed WHERE tool=?", (tool,)).fetchone() is not None


def of(store):
    found = getattr(store, '_voice_modes', None)
    if found is None:
        found = store._voice_modes = VoiceModes(store)
    return found
