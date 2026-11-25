"""Looking at the files of a chat's project or work folder (`project_files`): list, read, search. Read-only and bounded.

Everything stays inside that one folder: a path is resolved with its links first and must still be inside. Hidden files and folders, what
usually holds keys or passwords (by name, also behind a link), and binary files are left out; in what is read or found, a line that looks
like a key or password is replaced, and so is everything between the two ends of a private key. That is a help, not a guarantee: a secret
that looks like ordinary text cannot be told from it (the settings say so). It runs in a thread (a walk stops itself after SEARCH_SECONDS),
and what is matched against a line is cut first, so no file can hold anything up. What it returns is someone else's text to the model: data,
not instructions (convo_tools.finished says so). Standard library only; nothing here writes, opens for writing, or follows a link out."""
from __future__ import annotations

import fnmatch
import os
import re
import time

READ_BYTES = 512 * 1024         # a longer file is not read by voice
SEARCH_BYTES = 1024 * 1024
SEARCH_FILES, SEARCH_DEPTH, SEARCH_SECONDS, SEARCH_HITS = 4000, 12, 3.0, 20
LIST_ENTRIES, LIST_SEEN = 80, 5000
TEXT_LIMIT = 2200               # characters of one answer (convo_tools.RESULT_LIMIT keeps 2400)
LINE_LIMIT = 200                # characters of a line that are shown, and
CHECK_LIMIT = 2000              # that are looked at for a key: no more of a line ever reaches a pattern

# Folders that are never opened, and files that are never shown: their names say that they hold keys, sign-ins or other people's data.
NOISE = frozenset(('node_modules', '__pycache__', 'venv', 'site-packages', 'bower_components'))
PLAIN_HIDDEN = frozenset(('.gitignore', '.gitattributes', '.editorconfig', '.dockerignore', '.prettierrc', '.eslintrc', '.nvmrc', '.python-version'))
_KEYS = re.compile(r'secret|passwo?r?d|passwd|credential|api[-_ ]?keys?|apikey|private[-_ ]?key|access[-_ ]?key|keystore|kubeconfig|service[-_ ]?account', re.I)
_KEY_FILES = ('*.env', '*.env.*', 'env', 'env.*', '*.pem', '*.pem.*', '*.key', '*.key.*', 'key', 'key.*', 'keys', 'keys.*', '*_key', '*_key.*', '*-key.*', '*.keys', '*.keytab',
              '*.agekey', '*.p12', '*.pfx', '*.jks', '*.kdbx', '*.ppk', '*.ovpn', '*.gpg', '*.asc', '*.crt', '*.cer', '*.der', '*.mobileprovision', '*.tfstate*', '*.tfvars*',
              'id_rsa*', 'id_dsa*', 'id_ecdsa*', 'id_ed25519*', 'known_hosts', 'authorized_keys', 'auth.json', 'oauth*.json', 'google-services.json', 'logins.json', 'login data*',
              'key?.db', 'cookies*', 'wallet*', 'vault.*', '*vault*.y*ml', 'token', 'tokens', 'token.*', 'tokens.*', '*_token.*', '*-token.*', '*.token', 'pass', 'pass.*', 'pw.*', 'pwd.*',
              'connections.env', '*htpasswd*', 'netrc', '_netrc', '*pgpass*', 'wp-config*.php', 'settings.local.json', 'local.settings.json')
LEFT_OUT = '[left out: this line looks like a key or password]'

# What a key or password looks like, by the name it stands beside or by its own shape. Every pattern is simple and gets a bounded text.
_STRONG = frozenset(('secret', 'secrets', 'password', 'passwords', 'passwd', 'passwort', 'kennwort', 'passphrase', 'pwd', 'pass', 'pw', 'credential', 'credentials', 'apikey', 'apikeys',
                     'privatekey', 'accesskey', 'secretkey', 'accountkey', 'authkey'))
_KEYED = frozenset(('api', 'access', 'private', 'secret', 'account', 'encryption', 'signing', 'auth', 'master', 'license', 'licence', 'ssh', 'storage', 'consumer', 'webhook', 'deploy'))
_WEAK = frozenset(('token', 'tokens', 'auth', 'authorization', 'bearer', 'cookie', 'session', 'sid', 'sig', 'signature', 'key', 'keys', 'otp', 'salt', 'nonce', 'cert'))
_NOTHING = frozenset(('', 'true', 'false', 'none', 'null', 'nil', 'yes', 'no', 'on', 'off', 'required', 'optional', 'str', 'string', 'text', 'int', 'bool', 'undefined', 'empty', 'todo', 'tbd',
                      'changeme', 'example', 'placeholder', 'here', 'ok', 'failed', 'passed'))
_PATH = re.compile(r'[A-Za-z_]{1,60}(?:\.[A-Za-z_]{1,60}){1,8}')
_SPLIT = re.compile(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+')
_RUN = re.compile(r'[A-Za-z0-9_.+/~-]{16,}')
_HEADER = re.compile(r'\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}', re.I)
_IN_ADDRESS = re.compile(r'://[^/\s:@]{1,100}:[^/\s@]{1,200}@')
_ADDRESS = re.compile(r'hooks\.slack\.com/services/|discord(?:app)?\.com/api/webhooks/|[?&](?:sig|token|key|signature|access_token|api_?key)=[^&\s]{8,}', re.I)
_TAG = re.compile(r'<\s{0,4}([A-Za-z][A-Za-z0-9_.-]{0,60})[^<>]{0,200}>\s{0,8}([^<\s][^<]{2,200})')
_PREFIXES = ('sk-', 'sk_live_', 'sk_test_', 'rk_live_', 'rk_test_', 'whsec_', 'ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_', 'github_pat_', 'glpat-', 'hf_', 'xoxa-', 'xoxb-', 'xoxp-', 'xoxr-',
             'xoxs-', 'xapp-', 'AKIA', 'ASIA', 'AIza', 'ya29.', 'SG.', 'npm_', 'pypi-', 'shpat_', 'dop_v1_', 'eyJ', 'AGE-SECRET-KEY-', 'gsk_', 'nvapi-', 'r8_', 'tvly-', 'lin_api_', 'ntn_', 'secret_',
             'MII', 'MIGH', 'MHcC', 'MC4C', 'b3BlbnNzaC1rZXk')          # (how the text of a private key begins)
_BEGIN = re.compile(r'-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY|^PuTTY-User-Key-File|^Private-Lines:', re.I)
_BODY, _BODY_HEAD = re.compile(r'[A-Za-z0-9+/=]{8,}'), re.compile(r'(?:Proc-Type|DEK-Info|Comment|Version|Encryption|Public-Lines|Private-Lines|Private-MAC|PuTTY-User-Key-File)[-0-9A-Za-z]{0,20}:', re.I)
# A name and its value without a ':' or '=' between them.
_OPTION = re.compile(r'(?:^|[\s"\'`])--?([A-Za-z][A-Za-z-]{1,40})[ =]{1,4}(\S{1,200})')
_ENV = re.compile(r'^\s{0,20}(?:ENV|ARG|SET|SETX|SETENV|EXPORT)\s{1,8}([A-Za-z_][A-Za-z0-9_]{0,60})\s{1,8}(\S.{0,200})$', re.I)
_PAIR = re.compile(r'["\']([A-Za-z][A-Za-z0-9_. -]{1,60})["\']\s{0,8}(?:,|=>|\]\s{0,4}=)\s{0,8}["\']([^"\']{1,200})["\']')
_CALL = re.compile(r'([A-Za-z][A-Za-z0-9_]{2,60})\(\s{0,8}["\']([^"\']{3,200})["\']\s{0,4}\)')
_USER = re.compile(r'(?:^|\s)(?:-u|--user)[ =]{1,4}["\']?[^\s:"\']{1,80}:[^\s"\']{3,}|(?:^|\s)(?:sshpass\s+-p|mysql\b.{0,80}\s-p|docker login\b.{0,80}\s-p)\s{0,2}[^\s-]\S{3,}', re.I)
_SQL = re.compile(r'\b(?:identified\s{1,4}(?:with\s{1,4}\S{1,60}\s{1,4})?by|password)\s{1,4}["\'][^"\']{3,}["\']', re.I)
_LOGIN = re.compile(r'\blogin\s{1,4}\S{1,80}\s{1,4}password\s{1,4}\S{3,}|:\$(?:apr1|2[aby]|5|6|argon2\w{0,3})\$', re.I)
_PIN = re.compile(r'\bpin(?:\s{1,4}code)?\s{0,4}(?:is|ist|:|=)\s{0,4}\d{4,8}\b', re.I)
_ALONE = re.compile(r'^\s{0,20}([A-Z][A-Z0-9_]{3,60})\s{1,8}(\S{6,200})\s{0,20}$')
_IS = re.compile(r'\b(?:password|passphrase|passwort)\s{1,4}(?:is|ist|es|est|:)\s{1,4}(\S{4,})', re.I)


def _kinds(text):
    """(upper, lower, digits) among the characters of `text`."""
    return sum(c.isupper() for c in text), sum(c.islower() for c in text), sum(c.isdigit() for c in text)


def _looks_made(value):
    """Whether a value looks made rather than written: long enough, and letters with digits, or upper and lower case mixed inside."""
    if '(' in value or _PATH.fullmatch(value):
        return False                                    # a call, or a name in a program
    upper, lower, digits = _kinds(value)
    inner = value[1:]
    return len(value) >= 8 and ((digits and (upper or lower)) or (any(c.isupper() for c in inner) and lower >= 2 and not value.isidentifier()))


def _names_a_secret(name, value):
    """Whether `name` (what stands before a ':' or '=') says that `value` is a key or password."""
    name, value = name.rstrip(' \t"\'])'), value.strip(' \t"\',;')
    words = [word.lower() for word in _SPLIT.findall(name[-60:])][-4:]
    plain = value.lower().rstrip('.')
    first = value.split()[0].strip('"\',;') if value.split() else ''
    if not words or plain in _NOTHING or not any(c.isalnum() for c in value):
        return False
    if first.isdigit() and len(first) < (6 if words[-1] in ('pass', 'pw') or not any(word in _STRONG for word in words[-3:]) else 4):
        return False                                    # (a count; a short number is a password only where it says so)
    if any(word in _STRONG for word in words[-3:]) or (words[-1] in ('key', 'keys') and len(words) > 1 and words[-2] in _KEYED):
        return len(value) >= 4
    return words[-1] in _WEAK and _looks_made(first)


def _is_key(run):
    """Whether one unbroken run of characters has the shape of a key, whatever stands beside it."""
    run = run.strip('.~-')
    upper, lower, digits = _kinds(run)
    if run.startswith(_PREFIXES) and len(run) >= 16 and (digits or (upper and lower) or any(len(part) >= 16 for part in re.split(r'[_.-]', run))):
        return True
    for part in re.split(r'[./:~]', run):
        upper, lower, digits = _kinds(part)
        if len(part) >= 24 and upper >= 2 and lower >= 2 and digits:
            pieces = _SPLIT.findall(part)               # a name in a program is made of words, a key is not
            if sum(map(len, pieces)) < (4 if digits >= 3 else 2.8) * len(pieces):
                return True
        bare = part[2:] if part.startswith('0x') else part
        if len(bare) >= 32 and digits >= 4 and all(c in '0123456789abcdefABCDEF' for c in bare) and digits < len(bare):
            return True
    return False


def looks_like_a_key(line):
    """Whether a line looks as if it held a key or a password."""
    line = line[:CHECK_LIMIT]
    if any(pattern.search(line) for pattern in (_HEADER, _IN_ADDRESS, _ADDRESS, _USER, _SQL, _LOGIN)):
        return True
    begins = _BEGIN.search(line)
    if begins and _BODY.search(line[begins.end():].replace('PRIVATE KEY', '').replace('-', '').replace('\\n', ' ').lstrip()):
        return True                     # the whole key on one line; only to mention how one begins is no key
    for pattern in (_OPTION, _ENV, _PAIR, _CALL):
        if any(_names_a_secret(name, value) for name, value in pattern.findall(line)):
            return True
    if any(_looks_made(value.strip('"\'.,;')) for value in _IS.findall(line)) or _PIN.search(line):
        return True
    alone = _ALONE.match(line)
    if alone and _names_a_secret(alone.group(1), alone.group(2)) and _looks_made(alone.group(2)):
        return True
    for at, sign in enumerate(line):
        # (not '::', '==', '=>', '!=', ':=' twice, '://')
        if sign in ':=' and at and line[at - 1] not in ':=!<>' and line[at + 1:at + 3] != '//' and line[at + 1:at + 2] not in (':',):
            if _names_a_secret(line[max(0, at - 70):at], line[at + 1:at + 260].lstrip('=>')):
                return True
    if any(_names_a_secret(name, value) for name, value in _TAG.findall(line)):
        return True
    return any(_is_key(run) for run in _RUN.findall(line))


def _in_blocks(lines):
    """The numbers (from 0) of the lines that belong to a private key written out over several lines, its two ends included: from where it
    begins for as long as the lines are key text. A sentence that only mentions how a key begins starts nothing."""
    found, begun = set(), None
    for number, line in enumerate(lines):
        if begun is None:
            if 'PRIVATE KEY' in line.upper()[:CHECK_LIMIT] or line.startswith(('PuTTY-User-Key-File', 'Private-Lines:')):
                begins = _BEGIN.search(line[:CHECK_LIMIT])
                if begins and '-----END' not in line[begins.end():]:
                    begun = number
            continue
        bare = line[:400].replace('\\n', '').replace('\\r', '').strip().strip('"\'`,+;>|#*/ ')
        end = '-----END' in line
        if end or not bare or _BODY.fullmatch(bare) or (_BODY_HEAD.match(bare) and number - begun < 12):
            if bare:
                found.update(range(begun, number + 1))
            if end:
                begun = None
            continue
        begun = None
        if _BEGIN.search(line[:CHECK_LIMIT]) and '-----END' not in line:
            begun = number
    return found

OUTSIDE = 'that is outside the folder that can be looked at'
NOT_THERE = 'there is no such file or folder'
NOT_SHOWN = 'that file may hold keys or passwords, so it is not shown'
NOT_TEXT = 'that is not a text file'
TOO_LONG = 'that file is too long to read here'
NO_FOLDER = 'this chat has no project or work folder to look at'


def hidden(name):
    """A file or folder that is never shown, whatever it holds."""
    lower = name.lower()
    return (name.startswith('.') and lower not in PLAIN_HIDDEN) or bool(_KEYS.search(name)) or any(fnmatch.fnmatch(lower, pattern) for pattern in _KEY_FILES)


def _clean(line, inside=False):
    line = line[:CHECK_LIMIT].rstrip('\r\n').expandtabs(4)
    return LEFT_OUT if inside or looks_like_a_key(line) else line if len(line) <= LINE_LIMIT else line[:LINE_LIMIT - 1] + '…'


def _inside(root, path):
    """The real path of `path` inside `root` (both with their links resolved), and its parts below the root. Raises ValueError."""
    real_root = os.path.realpath(root)
    text = str(path or '').strip().replace('\\', '/').strip()
    if text.startswith('/') or re.match(r'^[A-Za-z]:', text) or '\x00' in text:
        # An absolute path is taken when it is inside the folder, else it is outside.
        real = os.path.realpath(text) if '\x00' not in text else ''
    else:
        real = os.path.realpath(os.path.join(real_root, text))
    try:
        if not real or os.path.commonpath([real_root, real]) != real_root:
            raise ValueError(OUTSIDE)
        relative = os.path.relpath(real, real_root)
    except ValueError:
        raise ValueError(OUTSIDE) from None                     # another drive, a device
    return real, [] if relative == '.' else relative.replace('\\', '/').split('/')


def _allowed(parts):
    for index, part in enumerate(parts):
        if hidden(part) or (part in NOISE and index < len(parts)):
            raise ValueError(NOT_SHOWN if hidden(part) else NOT_THERE)


def _text(path, limit):
    """A text file's text, or ValueError."""
    size = os.path.getsize(path)
    if size > limit:
        raise ValueError(TOO_LONG)
    with open(path, 'rb') as handle:
        data = handle.read(limit + 1)
    if b'\x00' in data[:8192]:
        raise ValueError(NOT_TEXT)
    return data.decode('utf-8', errors='replace')


def _size(count):
    return '%d bytes' % count if count < 1024 else '%.0f KB' % (count / 1024) if count < 1024 * 1024 else '%.1f MB' % (count / 1024 / 1024)


def _fit(head, rows, tail):
    """As many rows as fit under the limit (what it begins and ends with is always said), and how many were left away."""
    room, kept = TEXT_LIMIT - len(head) - sum(len(line) + 1 for line in tail) - 40, []
    for row in rows:
        room -= len(row) + 1
        if room < 0:
            break
        kept.append(row)
    return kept, len(rows) - len(kept)


def listing(root, path=''):
    real, parts = _inside(root, path)
    _allowed(parts)
    if not os.path.isdir(real):
        raise ValueError(NOT_THERE)
    folders, files, left, seen = [], [], 0, 0
    with os.scandir(real) as entries:
        for entry in entries:
            seen += 1
            if seen > LIST_SEEN:
                break
            if hidden(entry.name) or entry.name in NOISE:
                left += 1
                continue
            try:
                folder = entry.is_dir()
                if folder or entry.is_symlink():          # a link that leads out of the folder, or to something private, is not listed
                    _allowed(_inside(root, os.path.join(real, entry.name))[1])
                (folders if folder else files).append((entry.name, 0 if folder else entry.stat().st_size))
            except (OSError, ValueError):
                left += 1
    rows = [name[:120] + '/' for name, _ in sorted(folders)] + ['%s (%s)' % (name[:120], _size(size)) for name, size in sorted(files)]
    head = 'In ' + ('/'.join(parts) or 'the top folder') + ':'
    tail = ['(%d hidden or private entries are left out)' % left] if left else []
    shown, _ = _fit(head, rows[:LIST_ENTRIES], tail)
    if len(rows) > len(shown) or seen > LIST_SEEN:
        tail.insert(0, '(and %s%d more)' % ('at least ' if seen > LIST_SEEN else '', len(rows) - len(shown)))
    return '\n'.join([head] + (shown or ['(nothing to show)']) + tail)


def reading(root, path, from_line=1):
    real, parts = _inside(root, path)
    _allowed(parts)
    if not os.path.isfile(real):
        raise ValueError(NOT_THERE)
    lines = _text(real, READ_BYTES).split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    first = max(1, min(int(from_line or 1), max(1, len(lines))))
    out, size, number, blocks = [], 0, first - 1, _in_blocks(lines)        # (a key may have begun before the part that is read)
    for number in range(first, len(lines) + 1):
        row = '%d: %s' % (number, _clean(lines[number - 1], number - 1 in blocks))
        if out and size + len(row) + 1 > TEXT_LIMIT - 160:
            number -= 1
            break
        out.append(row)
        size += len(row) + 1
    head = '%s, lines %d to %d of %d:' % ('/'.join(parts), first, number, len(lines)) if lines else '%s is empty.' % '/'.join(parts)
    tail = ['(There is more: read on with from_line %d.)' % (number + 1)] if lines and number < len(lines) else []
    return '\n'.join([head] + out + tail)


def searching(root, query, path='', clock=time.monotonic):
    query = ' '.join(str(query or '').split())
    if len(query) < 2:
        raise ValueError('a search needs at least two characters')
    real, parts = _inside(root, path)
    _allowed(parts)
    if not os.path.isdir(real):
        raise ValueError(NOT_THERE)
    wanted, hits, looked, until, more = query.casefold(), [], 0, clock() + SEARCH_SECONDS, False
    real_root = os.path.realpath(root)
    for folder, folders, names in os.walk(real, followlinks=False):
        depth = 0 if folder == real else os.path.relpath(folder, real).count(os.sep) + 1
        folders[:] = sorted(name for name in folders if not hidden(name) and name not in NOISE and depth < SEARCH_DEPTH)
        try:
            _allowed(_inside(root, folder)[1])                  # (a folder may be reached through a link, or a junction on Windows)
        except ValueError:
            folders[:] = []
            continue
        for name in sorted(names):
            if hidden(name):
                continue
            if looked >= SEARCH_FILES or clock() > until or len(hits) >= SEARCH_HITS:
                more = True
                break
            looked += 1
            full, blocks = os.path.join(folder, name), None
            try:
                if os.path.islink(full):
                    _allowed(_inside(root, full)[1])            # what it leads to decides, not what it is called
                if not os.path.isfile(full):
                    continue
                lines = _text(full, SEARCH_BYTES).split('\n')
            except (OSError, ValueError):
                continue
            for number, line in enumerate(lines, 1):
                if wanted in ' '.join(line.split()).casefold():
                    blocks = _in_blocks(lines) if blocks is None else blocks
                    hits.append('%s:%d: %s' % (os.path.relpath(full, real_root).replace('\\', '/'), number, _clean(line, number - 1 in blocks).strip()[:160]))
                    if len(hits) >= SEARCH_HITS:
                        break
        if more:
            break
    if not hits:
        return '"%s" was not found%s.' % (query[:100], ' in the part that could be searched in time' if more else '')
    head = '"%s" occurs here:' % query[:100]
    shown, cut = _fit(head, hits, ['(There may be more.)'])
    return '\n'.join([head] + shown + (['(There may be more.)'] if more or cut or len(hits) >= SEARCH_HITS else []))


def look(root, action='', path='', query='', from_line=1):
    """What `project_files` answers. Raises ValueError with one fixed sentence that may be said to the user."""
    if not root or not os.path.isdir(root):
        raise ValueError(NO_FOLDER)
    try:
        if not action:
            # What was asked for follows from what was given: words to look for, a file, or a folder.
            real, _ = _inside(root, path)
            action = 'search' if str(query or '').strip() else 'read' if str(path or '').strip() and os.path.isfile(real) else 'list'
        if action == 'search':
            return searching(root, query, path)
        if action == 'read':
            return reading(root, path, from_line)
        return listing(root, path)
    except OSError:
        raise ValueError('that could not be read') from None


def too_broad(root, private):
    """Whether the folder holds Agent Zero's own data (its settings and keys): the whole disk, or a folder above `private`."""
    root, private = os.path.realpath(root), os.path.realpath(private)
    try:
        return os.path.commonpath([root, private]) == root
    except ValueError:
        return False
