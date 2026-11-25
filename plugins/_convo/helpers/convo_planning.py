"""The plan a conversation is working on (planning mode), kept in Convo's journal database in a table of its own: `plans`.

One open plan per conversation (its target: a chat's own conversation, or the one across chats). An open plan IS planning mode: it
outlives a reload, a lost connection, a restart and stopping voice, and it is the same for every page. Ended, it is `kept` (and can be
opened again), `handed` (it became a task) or `dropped` (the user discarded it). Standard library only; an older Convo ignores the table.

Two writers: Convo itself, in quiet moments (convo_modes.keep), and the user in the panel. Every change moves `revision` on, and a writer
says which revision it read: what changed meanwhile wins and Convo writes again later. Every text that was replaced stays findable (table
`plan_versions`: the newest VERSIONS, and the user's own last words however old), so no rewriting loses anything for good.

A plan is handed over once: `hand_over` makes the task and ends the plan in one step, only from an open or a kept plan. When that task is
cancelled or fails the plan was not carried out: it is a kept one again (`settle`). Earlier plans stay findable: by exactly what they are
called (`match`; the model, which understands what is meant, is told the names), and in the panel's list (`earlier`).

While a plan is open, tasks of that conversation that have not started yet wait (`held`); only those the user agreed to meanwhile
(`allow`) start. Text is the user's own plan: data to the model, never instructions."""
from __future__ import annotations

import json
import re
import time
import unicodedata
import uuid

from .convo_store import encoded

TOPIC_LIMIT = 200
TEXT_LIMIT = 12000          # characters of a plan (a final plan is handed over as one task, at most 16000)
TOLD_LIMIT = 2200           # characters of the plan every planning turn carries; the recent conversation is there word for word anyway
AGAIN_SECONDS = 30 * 86400  # a kept plan older than this is not opened again by itself
SHOWN_SECONDS = 7 * 86400   # an ended plan stays in the panel for this long
EARLIER = 6                 # earlier plans the panel and the model are told about
VERSIONS, VERSIONS_SHOWN = 20, 8   # replaced texts of one plan that are kept, and that the panel lists

CHANGED = 'The plan was changed meanwhile'
GONE = 'That plan is no longer there'
HANDED = 'That plan was already handed over'
EMPTY = 'Nothing is written down in this plan yet'

# The record's headings. Convo asks for them in English whatever language the plan is in: they are its own control words.
OWN = 'Own words'            # lines the user wrote themselves that a final text had lost: handed over word for word
SECTIONS = ('Goal', 'Decisions', 'Open questions', 'Steps', OWN, 'Set aside')
_HEADING = re.compile(r'^[\s#*_>]*(topic|goal|decisions|open questions|steps|own words|set aside)\s*[*_]*\s*:\s*[*_]*\s*(.*)$', re.I)
_FENCE = re.compile(r'^\s*```[a-z]*\s*$', re.I)


def words(text, limit=TOPIC_LIMIT):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def tidy(text):
    """(plan, topic) from what the model wrote: everything from the first heading on, each heading in its fixed spelling, empty ones
    left out. ('', '') when it holds none of the headings: that is not a plan (a refusal, a reply, a fragment) and is not kept."""
    topic, sections, current = '', {}, None
    for line in str(text or '').replace('\r', '').split('\n'):
        if _FENCE.match(line):
            continue
        heading = _HEADING.match(line)
        if heading:
            name, rest = heading.group(1).lower(), heading.group(2).strip()
            if name == 'topic':
                topic, current = topic or words(rest.strip('*_ "')), None
                continue
            current = next(section for section in SECTIONS if section.lower() == name)
            sections.setdefault(current, [])
            if rest:
                sections[current].append(rest)
        elif current is not None and line.strip():
            sections[current].append(line.strip())
    blocks = [name + ':\n' + '\n'.join(sections[name]) for name in SECTIONS if sections.get(name)]
    return '\n\n'.join(blocks)[:TEXT_LIMIT].rstrip(), topic


def told(text, limit=TOLD_LIMIT):
    """The plan as a planning turn carries it: whole when it fits; else without what was set aside, then shortened at its end."""
    text = str(text or '').strip()
    if len(text) > limit:
        text = re.sub(r'\n*Set aside:\n.*\Z', '', text, flags=re.S)
    return text if len(text) <= limit else text[:limit].rsplit('\n', 1)[0].rstrip() + '\n(The rest of the plan is in the panel.)'


# What Convo asks the conversation model to write, as the latest message of a request that begins exactly as the conversation's requests
# do (api/realtime._upkeep_request), like a summary chapter. Nothing of it is spoken.
UPKEEP = "[Instructions from the voice assistant's system, not said by the user: planning upkeep, not a reply]"
_LAYOUT = ('Use exactly these headings, in English, each on a line of its own, followed by short lines that begin with "- "; leave a heading '
           'out when there is nothing under it:\nTopic: (what is being planned, in at most eight words, on this one line)\nGoal:\n'
           'Decisions:\nOpen questions:\nSteps:\nSet aside:\n')
KEEP = (UPKEEP + '\nDo not reply to the user and do not call any tool. You and the user are planning something together. Bring the written '
        'plan up to date from the conversation above, in the conversation\'s language, in at most about 300 words (longer only when the '
        'current plan already is). ' + _LAYOUT +
        'Decisions: only what the user decided or agreed to. Open questions: what is not decided yet, proposals the user has not accepted '
        'included. Set aside: ideas the user rejected or put off. Keep what the current plan already holds unless the user changed it; '
        'they may have edited it themselves, so keep their wording and do not bring back what they removed. Never invent anything. '
        'Return only the plan.')
FINAL = (UPKEEP + '\nDo not reply to the user and do not call any tool. You and the user have planned something together. Write the final '
         'plan, in the conversation\'s language, complete and understandable without this conversation, so that another agent could carry '
         'it out: what is wanted and why, every decision with its details (names, numbers, places, dates, limits), and the steps in order. '
         'At most about 700 words. ' + _LAYOUT + 'Open questions: only what is really still open. Keep the user\'s own wording where they '
         'edited the current plan and do not bring back what they removed. Never invent anything. Return only the plan.')


def instruction(plan, final=False):
    """The latest message of the keeper's request, with the current plan as data."""
    return ((FINAL if final else KEEP) + ('\nTopic so far (data): ' + json.dumps(plan['topic'], ensure_ascii=False) if plan['topic'] else '')
            + '\nCurrent plan (data' + (', last edited by the user' if plan['by'] == 'user' else '') + '): '
            + (json.dumps(plan['text'], ensure_ascii=False) if plan['text'] else 'nothing is written down yet.'))


HAND_OVER = ('Carry out the plan below. The user worked it out in conversation with the voice assistant and has agreed to hand it over to '
             'you now. Where it leaves something open that matters, ask the user before going on.\n\n')


CHANGE = '\n\nChanged by the user when they handed it over (this wins over what is above):\n- '


def task(plan):
    """The task a plan is handed over as."""
    return HAND_OVER + (('What is planned: ' + plan['topic'] + '\n\n') if plan['topic'] else '') + plan['text'].strip()


# Scripts written without a space between two words: Thai and Lao, Burmese, Khmer, Japanese, Chinese.
_UNSPACED = re.compile(r'[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
_AROUND = ' .,;:!?…·"\'“”‘’„«»‹›()[]{}¿¡。！？、，；：「」『』（）।؟،'      # marks around a name that are no part of it


def _name(text):
    """A name as it is compared: without its case, its spacing and the marks around it."""
    return ' '.join(unicodedata.normalize('NFKC', str(text or '')).casefold().split()).strip(_AROUND)


def _words(text):
    """The words of a text, lower-cased, without punctuation, in any script (a sign that belongs to a letter stays with it)."""
    text = unicodedata.normalize('NFKC', str(text or '')).casefold()
    return ''.join(letter if unicodedata.category(letter)[0] in 'LMN' else ' ' for letter in text).split()


def _pieces(text):
    """What two texts are compared by: their words; in a script without spaces every two neighbouring characters."""
    pieces = set()
    for word in _words(text):
        pieces.update((word[at:at + 2] for at in range(len(word) - 1)) if len(word) > 2 and _UNSPACED.search(word) else (word,))
    return pieces


def named(name, said):
    """Whether every word of a name was said (in a script without spaces: whether it stands in what was said)."""
    heard, wanted = _words(said), _words(name)
    flat = ' '.join(heard)
    return bool(wanted) and all(word in heard or (_UNSPACED.search(word) is not None and word in flat) for word in wanted)


def _first(text):
    """The first thing written in a plan, to call one that has no name by."""
    return words(next((line.strip(' -*') for line in text.split('\n') if line.strip(' -*') and not _HEADING.match(line)), ''), 60)


def shrank(before, after):
    """Whether a rewritten plan lost most of what stood there: a slip of the model, or what the user wants (they threw it over). So it is
    taken only when the next writing says the same."""
    return len(before) > 400 and len(after) < len(before) * .4


def lost_lines(mine, text):
    """The lines of `mine` that are gone from a rewritten text: neither there word for word nor, mostly, in other words."""
    have, flat = _pieces(text), ' '.join(text.casefold().split())
    lines = [line.strip().lstrip('-*• ').strip() for line in mine.split('\n') if line.strip() and not _HEADING.match(line)]
    return [line for line in lines if len(_pieces(line)) > 1 and ' '.join(line.casefold().split()) not in flat
            and len(_pieces(line) & have) < .6 * len(_pieces(line))][:12]


def with_own_words(text, mine):
    """A rewritten plan with the lines it lost of what the user had just written themselves, word for word under a heading of their own (the
    next writing, or the user, may take them in or out)."""
    lost = lost_lines(mine, text)
    return tidy(text + '\n\n' + OWN + ':\n' + '\n'.join('- ' + line for line in lost))[0] if lost else text


def _plan(row):
    try:
        allowed = json.loads(row['allowed'])
    except ValueError:
        allowed = []
    return {'id': row['id'], 'target': row['target'], 'session': row['session'], 'topic': row['topic'], 'text': row['text'],
            'status': row['status'], 'revision': row['revision'], 'by': row['written_by'], 'upto': row['upto'],
            'allowed': [str(item) for item in allowed] if isinstance(allowed, list) else [], 'job': row['job'],
            'started': row['started'], 'updated': row['updated']}


class Plans:
    CHANGED, GONE = CHANGED, GONE

    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    # ── reading ──
    def _row(self, query, *values):
        with self.store.lock:
            row = self.store.db.execute('SELECT * FROM plans WHERE ' + query, values).fetchone()
        return _plan(row) if row else None

    def open(self, target):
        """The plan this conversation is working on (planning mode is on), or None."""
        return self._row("target=? AND status='open' ORDER BY started DESC LIMIT 1", str(target))

    def get(self, plan_id):
        return self._row('id=?', str(plan_id))

    def settle(self):
        """A plan whose task was cancelled, failed or ended without an answer was not carried out (as far as is known): it is kept again."""
        back = "status='handed' AND EXISTS (SELECT 1 FROM jobs WHERE jobs.id=plans.job AND jobs.status IN ('cancelled','failed','uncertain'))"
        with self.store.lock:
            if self.store.db.execute('SELECT 1 FROM plans WHERE ' + back + ' LIMIT 1').fetchone():
                with self.store.transaction():
                    self.store.db.execute("UPDATE plans SET status='kept',revision=revision+1 WHERE " + back)

    def last(self, target, within=AGAIN_SECONDS):
        """The plan this conversation kept most recently, or None."""
        self.settle()
        return self._row("target=? AND status='kept' AND updated>? ORDER BY updated DESC,rowid DESC LIMIT 1", str(target), self.clock() - within)

    def earlier(self, target, besides=''):
        """The earlier plans of this conversation with something in them, newest first (not `besides`): what can be gone on with. Those that
        were handed over are among them (status)."""
        self.settle()
        with self.store.lock:
            rows = self.store.db.execute("SELECT * FROM plans WHERE target=? AND status IN ('kept','handed') AND text<>'' AND updated>? AND id<>? "
                                         'ORDER BY updated DESC,rowid DESC LIMIT ?',
                                         (str(target), self.clock() - AGAIN_SECONDS, str(besides), EARLIER)).fetchall()
        return [_plan(row) for row in rows]

    def match(self, target, topic, besides=''):
        """The earlier plan that is called exactly `topic` (the newest of several), or None. Never one whose name is only similar: "a trip to
        Madrid" is not "a trip to Lisbon", and which of them is meant is for the model to understand."""
        return next((plan for plan in self.earlier(target, besides) if words(topic) and self.same(topic, plan['topic'])), None)

    def about(self, target, topic='', new=False, plan_id=''):
        """What `begin` would do: (how, the plan or None, the open plan it would put away or None). An open plan with nothing written in it
        is not put away: there is nothing to lose."""
        plan, topic, chosen = self.open(target), words(topic), None
        if plan_id:
            chosen = self.get(plan_id)
            if chosen is None or chosen['target'] != str(target) or chosen['status'] == 'dropped':
                raise ValueError(GONE)
        elif topic and not new and (plan is None or not self.same(topic, plan['topic'])):
            chosen = self.match(target, topic, plan['id'] if plan else '')
        elif plan is None and not new:
            chosen = self.last(target)
        if plan is not None and (chosen['id'] == plan['id'] if chosen is not None else not new):
            return 'on', plan, None
        away = plan if plan is not None and plan['text'].strip() else None
        return ('again' if chosen is not None else 'new'), chosen, away

    @staticmethod
    def same(one, other):
        """Whether two names are the same plan's: the very same words in the same order ("from Rome to Paris" is not "from Paris to Rome", nor
        "learn C" "learn C++"); case, spacing and the marks around a name do not count."""
        return _name(one) == _name(other)

    def shown(self, target):
        """What the panel shows: the open plan, else the last one while it is recent. When that one was discarded: the last one that is still
        kept, never one that was handed over before it (that one is done)."""
        self.settle()
        plan = self.open(target) or self._row("target=? AND updated>? ORDER BY updated DESC,rowid DESC LIMIT 1", str(target), self.clock() - SHOWN_SECONDS)
        return self.last(target, SHOWN_SECONDS) if plan is not None and plan['status'] == 'dropped' else plan

    def newest(self, target):
        """The row number of this conversation's newest finished turn (what a plan is up to date with)."""
        with self.store.lock:
            row = self.store.db.execute('SELECT max(rowid) FROM turns WHERE target=? AND settled=1', (str(target),)).fetchone()
        return int(row[0] or 0)

    def behind(self, plan):
        """How many finished exchanges the plan has not taken in yet (what left the model's history does not count)."""
        from .convo_history import hidden
        with self.store.lock:
            rows = self.store.db.execute('SELECT content FROM turns WHERE target=? AND settled=1 AND rowid>? ORDER BY rowid LIMIT 50',
                                         (plan['target'], plan['upto'])).fetchall()
        return sum(1 for row in rows if not hidden(json.loads(row['content'])))

    # ── tasks that wait while planning ──
    def held(self, job):
        """Whether this queued task waits: its conversation is planning and the user has not agreed to it meanwhile."""
        plan = self.open(self.store.scope_of(job))
        return plan is not None and job['id'] not in plan['allowed']

    def waiting(self, target):
        """The tasks of this conversation that wait while it is planning."""
        plan = self.open(target)
        if plan is None:
            return []
        return [job for job in self.store.jobs(target) if job['status'] == 'queued' and job['id'] not in plan['allowed']]

    def allow(self, target, job_id):
        """A task the user agreed to while planning starts although the others wait."""
        with self.store.transaction():
            plan = self.open(target)
            if plan is None or job_id in plan['allowed']:
                return False
            self.store.db.execute('UPDATE plans SET allowed=? WHERE id=?', (json.dumps((plan['allowed'] + [str(job_id)])[-64:]), plan['id']))
            return True

    # ── changing ──
    def _note(self, plan, kind, session=None, turn='', **fields):
        """Journalled with the conversation, whether or not voice is running."""
        session = session or {}
        self.store._event({'id': session.get('id') or plan['session'] or '', 'target': plan['target'], 'epoch': session.get('epoch') or 0}, turn, kind,
                          {'plan_id': plan['id'], 'topic': plan['topic'], **fields})

    def begin(self, target, topic='', *, new=False, plan_id='', session=None, turn=''):
        """Planning is on from here. Returns (plan, how): 'on' (it was already), 'again' (a kept plan is open again: the one that is named, the
        one chosen by its id, or the last one when nothing is named) or 'new'. `new` starts a separate plan. A plan that was open is kept
        (one with nothing in it is dropped)."""
        topic, target = words(topic), str(target)
        with self.store.transaction():
            now, was = self.clock(), self.open(target)
            how, plan, _ = self.about(target, topic, new, plan_id)
            if how == 'on':
                if topic and not plan['topic']:
                    self.store.db.execute('UPDATE plans SET topic=?,revision=revision+1,updated=? WHERE id=?', (topic, now, plan['id']))
                return self.get(plan['id']), how
            if was is not None:
                self._end(was, 'kept' if was['text'].strip() else 'dropped', session, turn)
            if how == 'again':
                self.store.db.execute("UPDATE plans SET status='open',allowed='[]',started=?,updated=?,revision=revision+1,session=? WHERE id=?",
                                      (now, now, (session or {}).get('id') or plan['session'], plan['id']))
                plan = self.get(plan['id'])
            else:
                made = uuid.uuid4().hex
                self.store.db.execute('INSERT INTO plans(id,target,session,topic,text,status,revision,written_by,upto,allowed,job,started,updated) '
                                      "VALUES(?,?,?,?,'','open',0,'convo',?,'[]','',?,?)",
                                      (made, target, (session or {}).get('id') or '', topic, self.newest(target), now, now))
                plan = self.get(made)
            self._note(plan, 'plan.started', session, turn, how=how)
            return plan, how

    def write(self, plan_id, text, *, revision, by, topic='', upto=None, was=None):
        """Replace the plan's text, if it is still at `revision`, or still says what the writer had read (`was`: ending it or naming it
        changes no text). What stood there is kept as a version. Returns the plan. Raises ValueError (CHANGED, GONE)."""
        text = str(text or '').replace('\r', '').strip()[:TEXT_LIMIT]
        with self.store.transaction():
            plan = self.get(plan_id)
            if plan is None or plan['status'] == 'dropped':
                raise ValueError(GONE)
            if plan['revision'] != revision and (was is None or str(was).replace('\r', '').strip() != plan['text']):
                raise ValueError(CHANGED)
            topic = words(topic)
            if by != 'user':
                topic = plan['topic'] or topic         # Convo names a plan that has no name; it never renames one
            elif not topic:
                topic = plan['topic']
            if text != plan['text'] or topic != plan['topic']:
                if text != plan['text']:
                    self._remember(plan)
                self.store.db.execute('UPDATE plans SET text=?,topic=?,written_by=?,revision=revision+1,updated=? WHERE id=?',
                                      (text, topic, by, self.clock(), plan_id))
                if by == 'user':
                    self._note(self.get(plan_id), 'plan.edited')
            if upto is not None and upto > plan['upto']:
                self.store.db.execute('UPDATE plans SET upto=? WHERE id=?', (int(upto), plan_id))
            return self.get(plan_id)

    def _remember(self, plan):
        """What is about to be replaced (inside a transaction)."""
        db = self.store.db
        newest = db.execute('SELECT text FROM plan_versions WHERE plan=? ORDER BY id DESC LIMIT 1', (plan['id'],)).fetchone()
        if not plan['text'].strip() or (newest and newest['text'] == plan['text']):
            return
        db.execute('INSERT INTO plan_versions(plan,text,written_by,time) VALUES(?,?,?,?)', (plan['id'], plan['text'], plan['by'], self.clock()))
        db.execute('DELETE FROM plan_versions WHERE plan=? AND id NOT IN (SELECT id FROM plan_versions WHERE plan=? ORDER BY id DESC LIMIT ?) '
                   "AND id<>COALESCE((SELECT max(id) FROM plan_versions WHERE plan=? AND written_by='user'),0)", (plan['id'], plan['id'], VERSIONS, plan['id']))

    def versions(self, plan):
        """What stood in this plan before, newest first: [{id, by, time}] (without a text equal to what stands there now)."""
        with self.store.lock:
            rows = self.store.db.execute('SELECT id,written_by,time FROM plan_versions WHERE plan=? AND text<>? ORDER BY id DESC LIMIT ?',
                                         (plan['id'], plan['text'], VERSIONS_SHOWN)).fetchall()
        return [{'id': row['id'], 'by': row['written_by'], 'time': row['time']} for row in rows]

    def version(self, plan_id, version):
        """The text of one of them. Raises ValueError (GONE)."""
        with self.store.lock:
            row = self.store.db.execute('SELECT text FROM plan_versions WHERE plan=? AND id=?', (str(plan_id), version)).fetchone()
        if not row:
            raise ValueError(GONE)
        return row['text']

    def _end(self, plan, status, session=None, turn='', job=''):
        self.store.db.execute('UPDATE plans SET status=?,job=?,revision=revision+1,updated=? WHERE id=?',
                              (status, job or plan['job'], self.clock(), plan['id']))
        plan = self.get(plan['id'])
        self._note(plan, 'plan.ended', session, turn, how=status, **({'job_id': job} if job else {}))
        return plan

    def end(self, plan_id, status='kept', *, session=None, turn=''):
        """Planning is over ('kept' or 'dropped'). An ended plan may still be dropped; keeping one that is over already changes nothing."""
        if status not in ('kept', 'dropped'):
            raise ValueError('Unknown end of a plan')
        with self.store.transaction():
            plan = self.get(plan_id)
            if plan is None or plan['status'] == 'dropped':
                raise ValueError(GONE)
            return self._end(plan, status, session, turn) if status == 'dropped' or plan['status'] == 'open' else plan

    @staticmethod
    def check(plan):
        """Raises ValueError, in plain words, when this plan cannot be handed over (any more)."""
        if plan is None or plan['status'] == 'dropped':
            raise ValueError(GONE)
        if plan['status'] == 'handed':
            raise ValueError(HANDED)
        if not plan['text'].strip():
            raise ValueError(EMPTY)

    def hand_over(self, plan_id, chat, *, session=None, turn='', change=''):
        """The plan becomes one task for a chat's agent and is over: one step, so it happens once. `change`: what the user changed about it
        with their yes. Returns the job. Raises ValueError when it cannot be handed over (any more)."""
        change = words(change, 500)
        self.settle()
        with self.store.transaction():
            plan = self.get(plan_id)
            self.check(plan)
            if change:
                self.store.db.execute('UPDATE plans SET text=? WHERE id=?', (plan['text'].strip() + CHANGE + change, plan['id']))
                plan = self.get(plan['id'])
            owner = (session or {}).get('id') or self._session(plan)
            job = uuid.uuid4().hex
            self.store.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,'queued','')",
                                  (job, owner + ':plan:' + job, owner, str(chat), turn, task(plan)[:16000]))
            if str(chat) != plan['target']:
                self.store.db.execute('INSERT OR REPLACE INTO job_scope VALUES(?,?)', (job, plan['target']))
            row = self.store.db.execute('SELECT content FROM turns WHERE id=? AND session=?', (str(turn), owner)).fetchone()
            if row:                                         # as for any other task: the turn it was agreed to in knows it
                content = json.loads(row['content'])
                content.setdefault('jobs', []).append(job)
                self.store.db.execute('UPDATE turns SET content=?,revision=revision+1 WHERE id=?', (encoded(content), str(turn)))
            self._note(plan, 'job.queued', {'id': owner}, turn, job_id=job)
            self._end(plan, 'handed', {'id': owner}, turn, job)
        return self.store.job(job)

    def _session(self, plan):
        """A session row the task can belong to: the plan's own, else this conversation's newest, else a closed one made for it (a job
        always belongs to a session; a plan started in the panel may have had none)."""
        for query, values in (('SELECT id FROM sessions WHERE id=?', (plan['session'],)),
                              ('SELECT id FROM sessions WHERE target=? ORDER BY rowid DESC LIMIT 1', (plan['target'],))):
            row = self.store.db.execute(query, values).fetchone()
            if row:
                return row['id']
        made = uuid.uuid4().hex
        self.store.db.execute('INSERT INTO sessions(id,owner,target,epoch,active) VALUES(?,?,?,0,0)', (made, 'panel', plan['target']))
        return made

    def view(self, target):
        """The plan for the panel, or None."""
        plan = self.shown(target)
        if plan is None:
            return None
        return {key: plan[key] for key in ('id', 'topic', 'text', 'status', 'revision', 'by', 'started', 'updated')} | {
            'on': plan['status'] == 'open', 'waiting': [job['id'] for job in self.waiting(target)] if plan['status'] == 'open' else [],
            'versions': self.versions(plan),
            'earlier': [{'id': kept['id'], 'topic': kept['topic'] or _first(kept['text']), 'updated': kept['updated'], 'handed': kept['status'] == 'handed'}
                        for kept in self.earlier(target, plan['id'])]}


def of(store):
    """The plans kept beside this journal."""
    found = getattr(store, '_plans', None)
    if found is None:
        found = Plans(store)
        try:
            store._plans = found
        except Exception:
            pass
    return found
