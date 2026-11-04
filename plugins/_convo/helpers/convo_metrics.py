"""Content-free timing marks for the voice path. Observational only: nothing here may change behaviour.

Marks hold a turn identifier, a name, a monotonic time (the running loop's clock, so virtual time in tests and the
same clock as asyncio deadlines in production), a log-wide sequence number (`seq`, so a reader can ask only for what is
newer) and a few typed scalars. Never audio, transcripts, prompts or credentials; unknown fields are dropped. Bounded in
turns, in marks per turn and in marks per read.

Within a turn, a probe (speech still going on, `final=False`) may hold at most PROBE_MARKS marks, so a long stretch of probes
cannot crowd out the completed turn's own marks; the marks that end a turn (TURN_END) are kept even in a full turn, up to a hard
ceiling of MARKS + END_MARKS; of a per-phrase mark (LATEST) only the newest copy is kept."""
from __future__ import annotations

import asyncio
import collections
import math
import threading
import time

FIELDS = {'sequence': int, 'final': bool, 'disposition': str, 'speech': str, 'action': str, 'confidence': float,
          'epoch': int, 'previous_epoch': int, 'chars': int, 'error': str, 'gate': str, 'p_respond': float, 'p_addressed': float,
          'p_backchannel': float, 'p_side': float, 'p_unfinished': float, 'p_unclear': float, 'p_explicit': float, 'p_name': float,
          'mode': str, 'floor': float, 'reason': str, 'ctx_used': int, 'ctx_window': int,
          'revisions': int, 'jobs': int, 'gap': float, 'seconds': float}
TURNS, MARKS = 200, 48
PROBE_MARKS = 24       # a turn's probe marks; the rest of MARKS stays free for the completed turn
END_MARKS = 16         # room above MARKS for the marks that end a turn
TURN_END = frozenset({'admitted', 'state.sent', 'reply.done', 'reply.error', 'reply.empty', 'reply.markup', 'model.listen',
                      'model.listen_overridden', 'turn.superseded', 'spec.promote', 'spec.discard', 'playback.completed'})
LATEST = frozenset({'playback.completed'})    # one per spoken phrase: only the newest copy is kept
REPORT_MARKS = 1000    # at most this many marks per cursor read; the reader asks again for the rest


def now():
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return time.monotonic()


def clean(fields):
    result = {}
    for key, kind in FIELDS.items():
        value = fields.get(key)
        if value is None:
            continue
        if kind is float and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            result[key] = round(float(value), 3)
        elif kind is int and type(value) is int:
            result[key] = value
        elif kind is bool and type(value) is bool:
            result[key] = value
        elif kind is str and isinstance(value, str):
            result[key] = value[:48]
    return result


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return None
    pick = lambda q: ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]
    return {'n': len(ordered), 'p50': pick(.5), 'p90': pick(.9), 'p95': pick(.95), 'max': ordered[-1]}


class TimingLog:
    def __init__(self):
        self.lock = threading.Lock()
        self.turns = collections.OrderedDict()
        self.sequence = 0      # the `seq` of the newest stored mark; it only grows, so a reader's cursor stays meaningful

    def reset(self):
        with self.lock:
            self.turns.clear()

    def mark(self, turn, name, **fields):
        stamp = now()
        turn, name = str(turn)[:80], str(name)[:40]
        with self.lock:
            record = self.turns.get(turn)
            if record is None:
                record = self.turns[turn] = {'turn': turn, 'marks': [], 'probes': 0}
                while len(self.turns) > TURNS:
                    self.turns.popitem(last=False)
            marks = record['marks']
            if name in LATEST:
                marks[:] = [mark for mark in marks if mark['name'] != name]
            probe = fields.get('final') is False
            if probe:
                keep = record['probes'] < PROBE_MARKS and len(marks) < MARKS
            else:
                keep = len(marks) < (MARKS + END_MARKS if name in TURN_END else MARKS)
            if keep:
                record['probes'] += probe
                self.sequence += 1
                marks.append({'name': name, 't': round(stamp, 4), 'seq': self.sequence, **clean(fields)})
        return stamp

    def records(self, limit=50):
        with self.lock:
            return [{'turn': r['turn'], 'marks': [dict(m) for m in r['marks']]} for r in list(self.turns.values())[-limit:]]

    def since(self, cursor, limit=REPORT_MARKS):
        """Marks stored after `cursor` (the last `seq` the reader has), oldest first, at most `limit`, grouped by turn in
        order. A cursor ahead of the log (it restarted) reads everything again and says `reset`."""
        with self.lock:
            reset = cursor > self.sequence
            after = 0 if reset else cursor
            found = sorted(((r['turn'], dict(m)) for r in self.turns.values() for m in r['marks'] if m['seq'] > after), key=lambda item: item[1]['seq'])
        turns = collections.OrderedDict()
        for turn, mark in found[:limit]:
            turns.setdefault(turn, []).append(mark)
        return {'turns': [{'turn': turn, 'marks': marks} for turn, marks in turns.items()],
                'cursor': found[:limit][-1][1]['seq'] if found else after, 'more': len(found) > limit, 'reset': reset}

    def summary(self):
        """Distributions (seconds) and counts over the retained turns."""
        classifier, to_admit, to_reply, to_text, to_audio, to_play = [], [], [], [], [], []
        counts = collections.Counter()
        with self.lock:
            records = [list(r['marks']) for r in self.turns.values()]
        for marks in records:
            first = lambda name, **match: next((m for m in marks if m['name'] == name and all(m.get(k) == v for k, v in match.items())), None)
            received = {m.get('sequence'): m['t'] for m in marks if m['name'] == 'candidate.received'}
            final = next((m['t'] for m in marks if m['name'] == 'candidate.received' and m.get('final')), None)
            started = {m.get('sequence'): m['t'] for m in marks if m['name'] == 'policy.start'}
            for m in marks:
                if m['name'] == 'policy.done' and m.get('sequence') in started:
                    classifier.append(m['t'] - started[m['sequence']])
                if m['name'] in {'policy.timeout', 'policy.error', 'policy.retry', 'policy.busy', 'speech.busy', 'spec.start', 'spec.promote', 'spec.discard',
                                 'cont.merge', 'cont.hold', 'turn.superseded', 'model.listen', 'model.listen_overridden'}:
                    counts[m['name']] += 1
                if m['name'] == 'state.sent' and m.get('disposition'):
                    counts['disposition.' + m['disposition']] += 1
            admitted = first('admitted')
            if admitted:
                base = received.get(admitted.get('sequence'))
                if base is not None:
                    to_admit.append(admitted['t'] - base)
            if final is not None:
                for name, bucket in (('reply.start', to_reply), ('answer.first_delta', to_text), ('audio.first', to_audio), ('playback.started', to_play)):
                    mark = first(name)
                    if mark and mark['t'] >= final:
                        bucket.append(mark['t'] - final)
        return {'turns': len(records), 'counts': dict(counts), 'classifier_s': percentiles(classifier), 'probe_to_admit_s': percentiles(to_admit),
                'final_to_reply_start_s': percentiles(to_reply), 'final_to_first_text_s': percentiles(to_text),
                'final_to_first_audio_sent_s': percentiles(to_audio), 'final_to_playback_started_s': percentiles(to_play)}


LOG = TimingLog()


def report(config, cursor=None):
    """Developer-mode diagnostics: distributions and the recent per-turn marks. No content is ever included.

    Without a cursor: the last 50 turns and the current cursor. With one (a whole number, the `cursor` of the previous read):
    only the marks stored since, bounded (`more` says another read has the rest)."""
    if not config.get('developer_mode'):
        raise ValueError('Timing marks are available in developer mode')
    # `now` is the marks' own clock, read while answering: the page places marks on its clock through the request's round trip.
    if cursor is None:
        with LOG.lock:
            sequence = LOG.sequence
        return {'ok': True, 'now': round(now(), 4), 'summary': LOG.summary(), 'turns': LOG.records(50), 'cursor': sequence}
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ValueError('The metrics cursor must be a whole number from 0')
    return {'ok': True, 'now': round(now(), 4), 'summary': LOG.summary(), **LOG.since(cursor)}
