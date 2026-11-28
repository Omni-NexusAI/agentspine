"""What the conversation model saw and wrote, turn by turn, for the developer view ("What the model saw / wrote").

Developer mode only, memory only: the last RING turns, each with every request made for it (the first answer, a retry, a tool's
follow-up, the held early reply), the model's raw output before any filter, what each filter took out (the hidden header lines, the
control word, tool-call markup), the tool calls and end-of-answer figures, what was spoken and Whisper's text. The view (`turns`)
never carries audio: a clip is shown as its length. The audio stays in memory with the turn and leaves it only when the user
presses "Save for replay" (`save`), which writes that one turn to Convo's `data/replay/` folder (format: AGENTS.md "Saved turn").
Unlike convo_metrics (timing, never content), this holds conversation text, so only developer-mode actions read it.

`remember`/`said`/`latest` are the older single-request view (`metrics` `last_request`), kept for older panels."""
from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict

from . import convo_history

RING = 10                 # turns kept
PASSES = 8                # requests kept per turn
OUTPUT_LIMIT = 8000       # characters of raw output (and of each filter stage) kept per request
SPOKEN_LIMIT = 40         # phrases kept per turn
TOOLS_LIMIT = 8
SAVED_LIMIT = 200         # saved turns kept in data/replay before a save is refused
REPLAY_DIR = 'replay'
FORMAT = 'convo-replay/1'
LATE_SHOWN = '\n--- this reply only (sent after the history) ---\n'
_ID = re.compile(r'^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$')
ROLE_FIELDS = ('provider', 'url', 'model', 'api_base', 'capabilities', 'context_window')

_lock = threading.Lock()
_latest = None
_turns = OrderedDict()
_compactions = []
COMPACTIONS = 50          # compaction outcomes kept
_rev = 0


# ── capture (api/realtime.py) ──────────────────────────────────────────────────────────────────────────────────────────────

def _bump():
    global _rev
    _rev += 1


def _record(turn):
    """The turn's record, created (and the oldest dropped) when new. Caller holds the lock."""
    record = _turns.get(turn)
    if record is None:
        record = _turns[turn] = {'turn': turn, 'at': round(time.time(), 3), 'passes': [], 'spoken': [], 'whisper': '', 'tools': []}
        while len(_turns) > RING:
            _turns.popitem(last=False)
    return record


def begin(turn, request, *, early=False):
    """A request to the conversation model for `turn` is about to be sent: keep it (with its audio, in memory) and return the
    request's record, which `tap` fills with what the model writes. Never raises; None when nothing could be kept."""
    remember(request)
    if not turn:
        return None
    try:
        kept = dict(request)
        if isinstance(kept.get('tool_turn'), dict):
            kept['tool_turn'] = dict(kept['tool_turn'])
        with _lock:
            record = _record(str(turn)[:80])
            kind = ('early' if early else 'tool follow-up' if kept.get('tool_turn') else
                    'first' if not record['passes'] else 'again')
            entry = {'kind': kind, 'at': round(time.time(), 3), 'request': kept, 'raw': '', 'proposals': [], 'done': {},
                     'error': '', 'stages': {'header': '', 'markup': '', 'control': ''}, 'filters': None}
            if len(record['passes']) >= PASSES:
                record['passes'].pop(1 if len(record['passes']) > 1 else 0)
            record['passes'].append(entry)
            _bump()
        return entry
    except Exception:
        return None


async def tap(entry, stream):
    """The answer stream, unchanged; what arrives is kept on `entry` exactly as the gateway sent it, before any filter."""
    async for item in stream:
        if entry is not None:
            try:
                kind = item.get('type')
                with _lock:
                    if kind == 'text.delta' and len(entry['raw']) < OUTPUT_LIMIT:
                        entry['raw'] = (entry['raw'] + str(item.get('text', '')))[:OUTPUT_LIMIT]
                    elif kind == 'tool.proposal' and len(entry['proposals']) < TOOLS_LIMIT:
                        entry['proposals'].append({'name': str(item.get('name', ''))[:80],
                                                   'arguments': _bounded(item.get('arguments'), 1200)})
                    elif kind == 'answer.done':
                        entry['done'] = {key: item[key] for key in ('finish_reason', 'reasoning_chars', 'tool_calls', 'prompt_tokens',
                                                                    'cached_tokens') if key in item}
                    elif kind == 'error':
                        entry['error'] = str(item.get('message', ''))[:300]
                    _bump()
            except Exception:
                pass
        yield item


def stage(turn, name, text):
    """What came out of one filter for the turn's latest request: `header` (after the hidden USER/LANGUAGE lines), `markup`
    (after the tool-call markup guard), `control` (after the control-word filter: what is shown and spoken)."""
    if not text:
        return
    with _lock:
        record = _turns.get(turn)
        if not record or not record['passes']:
            return
        stages = record['passes'][-1]['stages']
        if name in stages and len(stages[name]) < OUTPUT_LIMIT:
            stages[name] = (stages[name] + str(text))[:OUTPUT_LIMIT]
            _bump()


def filters(turn, *, header=None, guard=None, control=None):
    """The filter objects of the turn's latest request; what they decided is read when the view is built."""
    with _lock:
        record = _turns.get(turn)
        if record and record['passes']:
            record['passes'][-1]['filters'] = (header, guard, control)


def spoken(turn, text):
    """One phrase sent to speech for this turn."""
    if not str(text or '').strip():
        return
    with _lock:
        record = _turns.get(turn)
        if record is not None and len(record['spoken']) < SPOKEN_LIMIT:
            record['spoken'].append(str(text)[:2000])
            _bump()


def whisper(turn, text):
    """Whisper's text for this turn (when it arrives; the latest wins)."""
    with _lock:
        record = _turns.get(turn)
        if record is not None:
            record['whisper'] = str(text or '')[:4000]
            _bump()


def tool(turn, name, ok, result):
    """A tool the turn ran: its name, whether it worked and the start of its result (the journal keeps tool.requested/done/failed)."""
    with _lock:
        record = _turns.get(turn)
        if record is not None and len(record['tools']) < TOOLS_LIMIT:
            record['tools'].append({'name': str(name)[:80], 'ok': bool(ok), 'result': str(result or '')[:600]})
            _bump()


MEMORY_KINDS = ('compaction', 'written', 'swap', 'warm', 'fallback')


def compaction(target, outcome):
    """A memory event for the developer view of memory. Bounded; memory only; no conversation text. `kind`:
    'compaction' (a run that changed something: a chapter written and swapped in, rewritten or condensed; or deferred: stopped by
    speech, failed), 'written' (a chapter written and kept until its swap), 'swap' (a kept chapter swapped in; `slot` when pinned),
    'warm' (the new start warmed up: `ms`, `slot`; or stopped by speech or failed), 'fallback' (slots unknown, so one slot and
    unpinned; or the chosen summary writer refused)."""
    outcome = outcome if isinstance(outcome, dict) else {}
    kind = outcome.get('kind') if outcome.get('kind') in MEMORY_KINDS else 'compaction'
    entry = {'at': round(time.time(), 3), 'target': str(target or '')[:200], 'ok': bool(outcome.get('ok')), 'kind': kind,
             'changed': bool(outcome.get('changed')), 'reason': str(outcome.get('reason') or '')[:60]}
    for key in ('slot', 'ms'):
        value = outcome.get(key)
        if type(value) is int and 0 <= value < 10_000_000:
            entry[key] = value
    with _lock:
        _compactions.append(entry)
        del _compactions[:-COMPACTIONS]
        _bump()


def compactions(target=None):
    with _lock:
        return [dict(item) for item in _compactions if target is None or item['target'] == target]


def memory_view(store, target, compaction_settings):
    """The developer view of a conversation's memory (convo_memory): the window and its budgets, each summary chapter with the turns
    it covers, when it was written and its size, the standing notes, the word-for-word part's size and the compaction runs seen in
    this process (written and swapped in, or deferred). Read-only."""
    from . import convo_memory
    budget = convo_memory.Budget(compaction_settings or {})
    chapters = []
    for chapter in store.chapters(target):
        covered = [turn for turn in chapter.get('covered') or [] if isinstance(turn, str)]
        summary = str(chapter.get('summary') or '')
        chapters.append({'seq': chapter.get('seq'), 'turns': len(covered), 'first': covered[0] if covered else '',
                         'last': covered[-1] if covered else '', 'time': chapter.get('time'), 'chars': len(summary),
                         'tokens': convo_memory.tokens(summary), 'stale': bool(chapter.get('stale')), 'summary': summary[:4000]})
    memory = store.memory(target)
    standing = store.standing(target)
    context = store.context(target)
    verbatim = convo_memory.verbatim(context['turns'])
    return {'target': target, 'window': budget.window,
            'budget': {'reserve': budget.reserve, 'verbatim_high': budget.verbatim_high, 'verbatim_low': budget.verbatim_low,
                       'chapters_max': budget.chapters_max, 'condensed': budget.condensed, 'recent': budget.recent},
            'chapters': chapters, 'chapters_tokens': sum(item['tokens'] for item in chapters),
            'summary_without_chapters': bool(memory.get('summary')) and not chapters,
            'covered': len(memory.get('covered') or []), 'standing': standing[:1500], 'standing_tokens': convo_memory.tokens(standing) if standing else 0,
            'verbatim_turns': len(context['turns']), 'verbatim_tokens': convo_memory.tokens(verbatim),
            'compactions': compactions(target)}


def revision():
    with _lock:
        return _rev


def clear(*, conversation_only=False):
    """Forget what was captured: at a session's end the conversation's turns and requests (`conversation_only`; the compaction runs,
    which hold no conversation text, stay for the memory view); on plugin OFF or when developer mode goes off, everything."""
    global _latest
    with _lock:
        if not (_latest is None and not _turns and (conversation_only or not _compactions)):
            _latest = None
            _turns.clear()
            if not conversation_only:
                _compactions.clear()
            _bump()


# ── the view (never audio) ──────────────────────────────────────────────────────────────────────────────────────────────────

def _bounded(value, limit):
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text[:limit]


def removed(before, after):
    """The parts of `before` that are not in `after` (what a filter took out), in order; '' when nothing was."""
    before, after = str(before or ''), str(after or '')
    if before == after:
        return ''
    if before.endswith(after):
        return before[:len(before) - len(after)]
    parts = []
    for op, i1, i2, _, _ in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if op in ('delete', 'replace'):
            parts.append(before[i1:i2])
    return ' … '.join(part for part in parts if part)


def _filter_facts(found):
    header, guard, control = found or (None, None, None)
    facts = {}
    try:
        if header is not None:
            facts['user_line'] = str(getattr(header, 'line', '') or '')[:600]
            facts['language'] = str(getattr(header, 'language', '') or '')[:60]
        if guard is not None:
            facts['markup'] = str(getattr(guard, 'captured', '') or '')[:1200]
        if control is not None:
            facts['control'] = getattr(control, 'choice', None) or ''
            rest = str(getattr(control, 'rest', '') or '')
            facts['control_reason'] = ' '.join(rest.strip().split('\n', 1)[0].split())[:120] if rest else ''
    except Exception:
        pass
    return facts


def request_view(request):
    """A request as text: the system text with its per-reply part marked, the messages, the tools offered, the audio length."""
    shown = convo_history.view(request)
    system = shown[0]['text']
    stable, _, late = system.partition(convo_history.LATE_MARK)
    turn_part = request.get('tool_turn') if isinstance(request.get('tool_turn'), dict) else None
    return {'system': stable + (LATE_SHOWN + late if late else ''), 'system_stable_chars': len(stable), 'system_late': late,
            'messages': shown[1:], 'audio_s': convo_history.audio_seconds(request.get('audio')) if request.get('audio') else 0.0,
            'context_clips': len(request.get('context_audio') or []),
            'tools': [str(name) for name in (request.get('tools') or [])][:12], 'tool_mode': str(request.get('tool_mode') or ''),
            'allow_tools': bool(request.get('allow_tools')), 'max_tokens': request.get('max_tokens'),
            'tool_turn': {'name': str(turn_part.get('name', '')), 'arguments': _bounded(turn_part.get('arguments'), 600),
                          'said': str(turn_part.get('said', ''))[:1200], 'result': str(turn_part.get('result', ''))[:4000],
                          'image': bool(turn_part.get('image'))} if turn_part else None}


def _pass_view(entry):
    stages, raw = entry['stages'], entry['raw']
    header, markup, control = stages['header'], stages['markup'], stages['control']
    read = entry.get('filters') is not None          # None: the reply was never read through the filters (a discarded early reply)
    return {'kind': entry['kind'], 'at': entry['at'], 'request': request_view(entry['request']), 'raw': raw,
            'proposals': list(entry['proposals']), 'done': dict(entry['done']), 'error': entry['error'], 'filtered': read,
            'removed': {'header': removed(raw, header), 'markup': removed(header, markup), 'control': removed(markup, control)} if read else {},
            'filters': _filter_facts(entry.get('filters')), 'after': control if read else None}


def turns():
    """The kept turns, newest first, as text (no audio)."""
    with _lock:
        records = list(_turns.values())
        result = []
        for record in reversed(records):
            try:
                passes = [_pass_view(entry) for entry in record['passes']]
            except Exception:
                passes = []
            first = record['passes'][0]['request'] if record['passes'] else {}
            result.append({'turn': record['turn'], 'at': record['at'], 'passes': passes, 'spoken': list(record['spoken']),
                           'whisper': record['whisper'], 'tools': list(record['tools']),
                           'audio_s': convo_history.audio_seconds(first.get('audio')) if first.get('audio') else 0.0})
        return result


# ── Save for replay (only on the user's click) ──────────────────────────────────────────────────────────────────────────────

def _replay_dir(data_dir):
    return os.path.join(str(data_dir), REPLAY_DIR)


def _saved_request(request):
    """A request as sent, for a replay runner: the system text split at LATE_MARK, the history as built (earlier turns' in-memory
    audio included), this turn's audio, the tools and options. The role keeps its address and model only, never a key; a camera
    picture is never written."""
    role = request.get('role') if isinstance(request.get('role'), dict) else {}
    stable, _, late = str(request.get('system', '')).partition(convo_history.LATE_MARK)
    turn_part = request.get('tool_turn') if isinstance(request.get('tool_turn'), dict) else None
    if turn_part is not None:
        turn_part = {key: value for key, value in turn_part.items() if key != 'image'}
        if request['tool_turn'].get('image'):
            turn_part['image_omitted'] = True
    return {'role': {key: role[key] for key in ROLE_FIELDS if key in role},
            'system_stable': stable, 'system_late': late, 'history': request.get('history') or [],
            'audio': request.get('audio'), 'audio_s': convo_history.audio_seconds(request.get('audio')) if request.get('audio') else 0.0,
            'transcript': request.get('transcript'), 'context_audio': request.get('context_audio') or [],
            'tools': request.get('tools') or [], 'tool_mode': request.get('tool_mode') or '', 'allow_tools': bool(request.get('allow_tools')),
            'max_tokens': request.get('max_tokens'), 'recall': bool(request.get('recall')), 'tool_turn': turn_part}


def save(data_dir, turn):
    """Write one kept turn (its requests with their audio, the raw outputs, the removals, what was spoken, Whisper's text) to
    data/replay/<id>.json and return its id. Raises ValueError when the turn is no longer kept or the folder is full."""
    with _lock:
        record = _turns.get(str(turn or ''))
        if record is None or not record['passes']:
            raise ValueError('That turn is no longer kept in memory (only the last %d are).' % RING)
        passes = []
        for entry in record['passes']:
            view = _pass_view(entry)
            passes.append({'kind': entry['kind'], 'at': entry['at'], 'request': _saved_request(entry['request']), 'raw': entry['raw'],
                           'tool_proposals': view['proposals'], 'done': view['done'], 'error': view['error'],
                           'removed': view['removed'], 'filters': view['filters'], 'after_filters': view['after']})
        document = {'format': FORMAT, 'saved_at': round(time.time(), 3), 'turn': record['turn'], 'captured_at': record['at'],
                    'passes': passes, 'spoken': list(record['spoken']), 'whisper': record['whisper'], 'tools': list(record['tools'])}
    folder = _replay_dir(data_dir)
    os.makedirs(folder, exist_ok=True)
    if len([name for name in os.listdir(folder) if name.endswith('.json')]) >= SAVED_LIMIT:
        raise ValueError('%d turns are already saved for replay; delete some first.' % SAVED_LIMIT)
    ident = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    document['id'] = ident
    path = os.path.join(folder, ident + '.json')
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(document, handle, ensure_ascii=False)
    os.replace(temporary, path)
    return ident


def saved(data_dir):
    """The saved turns, newest first: id, when, the turn, its audio length and how many requests it holds."""
    folder = _replay_dir(data_dir)
    try:
        names = sorted((name for name in os.listdir(folder) if name.endswith('.json') and _ID.match(name[:-5])), reverse=True)
    except OSError:
        return []
    result = []
    for name in names[:SAVED_LIMIT]:
        try:
            with open(os.path.join(folder, name), encoding='utf-8') as handle:
                document = json.load(handle)
            first = (document.get('passes') or [{}])[0].get('request') or {}
            result.append({'id': name[:-5], 'saved_at': document.get('saved_at'), 'turn': document.get('turn', ''),
                           'audio_s': first.get('audio_s', 0.0), 'passes': len(document.get('passes') or []),
                           'bytes': os.path.getsize(os.path.join(folder, name))})
        except Exception:
            result.append({'id': name[:-5], 'unreadable': True})
    return result


def delete(data_dir, ident):
    """Delete one saved turn; True when it was there."""
    ident = str(ident or '')
    if not _ID.match(ident):
        raise ValueError('Unknown saved turn')
    try:
        os.remove(os.path.join(_replay_dir(data_dir), ident + '.json'))
        return True
    except FileNotFoundError:
        return False


# ── the older single-request view (`metrics` last_request) ──────────────────────────────────────────────────────────────────

def remember(request):
    """Keep a text view of `request` (a conversation-model request as api/realtime.py builds it). Never raises."""
    global _latest
    try:
        shown = convo_history.view(request)
        system = shown[0]['text'].replace(convo_history.LATE_MARK, LATE_SHOWN)
        view = {'at': round(time.time(), 3), 'system': system, 'messages': shown[1:],
                'audio_s': convo_history.audio_seconds(request.get('audio')) if request.get('audio') else 0.0,
                'tools': bool(request.get('allow_tools'))}
    except Exception:
        return
    with _lock:
        _latest = view


def said(text):
    """What the model wrote for the request kept above, after its USER line is taken off but before the control-word and tool-markup
    filters, bounded. Memory only."""
    with _lock:
        if _latest is not None:
            _latest['output'] = (_latest.get('output', '') + str(text or ''))[:4000]


def latest():
    with _lock:
        return None if _latest is None else dict(_latest)
