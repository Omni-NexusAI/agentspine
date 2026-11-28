"""Looking at a project's files by voice (`project_files`, convo_files): what is inside the folder and what never is, what is left out
because it may hold keys, the bounds, which conversations are offered it, and which folder it looks at."""
import importlib
import json
import os
import random
import string
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__:
    from .behavior_harness import SPEAK_PLAIN, script
    from .test_tool_rounds import Loop, call, result_of
else:
    from behavior_harness import SPEAK_PLAIN, script
    from test_tool_rounds import Loop, call, result_of

from plugins._convo.helpers import convo_files as files
from plugins._convo.helpers.convo_host import Host

tools = importlib.import_module('plugins._convo.helpers.convo_tools')


class Look(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'project'
        (self.root / 'src' / 'deep').mkdir(parents=True)
        (self.root / 'README.md').write_text('# Shed\nA garden shed for two bikes.\n', encoding='utf-8')
        (self.root / 'src' / 'app.py').write_text('\n'.join('line %d' % n for n in range(1, 401)), encoding='utf-8')
        (self.root / 'src' / 'deep' / 'notes.txt').write_text('The SHED needs   a roof.\nNothing here.\n', encoding='utf-8')
        (self.base / 'outside.txt').write_text('outside', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def refused(self, *args, **kwargs):
        with self.assertRaises(ValueError) as caught:
            files.look(str(self.root), *args, **kwargs)
        return str(caught.exception)

    def test_listing_reading_and_searching(self):
        listed = files.look(str(self.root))
        self.assertEqual(listed.splitlines()[0], 'In the top folder:')
        self.assertIn('src/', listed)
        self.assertRegex(listed, r'README\.md \(\d+ bytes\)')
        self.assertIn('app.py', files.look(str(self.root), path='src'))
        self.assertEqual(files.look(str(self.root), path='README.md'), 'README.md, lines 1 to 2 of 2:\n1: # Shed\n2: A garden shed for two bikes.')
        self.assertEqual(files.look(str(self.root), query='shed needs a roof').splitlines()[1], 'src/deep/notes.txt:1: The SHED needs   a roof.')
        self.assertIn('was not found', files.look(str(self.root), query='greenhouse'))
        self.assertIn('README.md:2', files.look(str(self.root), 'search', query='bikes'))

    def test_a_long_file_is_read_in_parts(self):
        first = files.look(str(self.root), path='src/app.py')
        self.assertLessEqual(len(first), tools.RESULT_LIMIT)
        self.assertRegex(first.splitlines()[0], r'^src/app\.py, lines 1 to \d+ of 400:$')
        last = int(first.splitlines()[-1].split('from_line ')[1].rstrip('.)'))
        second = files.look(str(self.root), path='src/app.py', from_line=last)
        self.assertEqual(second.splitlines()[1], '%d: line %d' % (last, last))
        self.assertIn('400: line 400', files.look(str(self.root), path='src/app.py', from_line=9999))

    def test_nothing_outside_the_folder_is_reached(self):
        for path in ('..', '../outside.txt', 'src/../../outside.txt', str(self.base / 'outside.txt'), '/etc/passwd', 'C:\\Windows\\win.ini', '..\\outside.txt', 'a\x00b', 'nul', 'con',
                     'Q:\\x'):
            for action in ('', 'list', 'read'):
                self.assertIn(self.refused(action, path), (files.OUTSIDE, files.NOT_THERE), (action, path))
            self.assertIn(self.refused('search', path, 'outside'), (files.OUTSIDE, files.NOT_THERE), path)
        self.assertEqual(files.look(str(self.root), path=str(self.root / 'README.md')).splitlines()[0], 'README.md, lines 1 to 2 of 2:')   # an absolute path inside

    def test_a_link_out_of_the_folder_is_not_followed(self):
        (self.base / 'private').mkdir()
        (self.base / 'private' / 'note.txt').write_text('a private shed', encoding='utf-8')
        try:
            os.symlink(self.base / 'private', self.root / 'linked', target_is_directory=True)
            os.symlink(self.base / 'outside.txt', self.root / 'linked.txt')
        except OSError:
            self.skipTest('links cannot be made here')
        self.assertNotIn('linked', files.look(str(self.root)))
        for path in ('linked', 'linked/note.txt', 'linked.txt'):
            self.assertEqual(self.refused('', path), files.OUTSIDE, path)
        self.assertNotIn('private shed', files.look(str(self.root), query='private shed').splitlines()[-1].replace('"private shed"', ''))

    def test_a_link_inside_the_folder_to_something_private_is_not_followed_either(self):
        (self.root / '.env').write_text('MY_HOST=SHIBBOLETH\n', encoding='utf-8')
        (self.root / 'secrets').mkdir()
        (self.root / 'secrets' / 'a.txt').write_text('SHIBBOLETH inside\n', encoding='utf-8')
        try:
            os.symlink(self.root / '.env', self.root / 'settings.txt')
            os.symlink(self.root / 'secrets', self.root / 'public', target_is_directory=True)
        except OSError:
            self.skipTest('links cannot be made here')
        self.not_followed()

    @unittest.skipUnless(os.name == 'nt', 'junctions are a thing of Windows')
    def test_nor_is_a_junction(self):
        import subprocess
        (self.root / 'secrets').mkdir()
        (self.root / 'secrets' / 'a.txt').write_text('SHIBBOLETH inside\n', encoding='utf-8')
        (self.base / 'private').mkdir()
        (self.base / 'private' / 'b.txt').write_text('SHIBBOLETH outside\n', encoding='utf-8')
        for name, target in (('public', self.root / 'secrets'), ('out', self.base / 'private')):
            if subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.root / name), str(target)], capture_output=True).returncode:
                self.skipTest('junctions cannot be made here')
            self.addCleanup(lambda name=name: os.path.lexists(self.root / name) and os.rmdir(self.root / name))
        try:
            self.not_followed()
            self.assertEqual(self.refused('read', 'out/b.txt'), files.OUTSIDE)
        finally:
            for name in ('public', 'out'):
                os.rmdir(self.root / name)

    def not_followed(self):
        listed = files.look(str(self.root))
        for name in ('settings.txt', 'public/', 'out/'):
            self.assertNotIn(name, listed.splitlines(), name)
        self.assertIn('was not found', files.look(str(self.root), query='SHIBBOLETH'))
        for path in ('settings.txt', 'public/a.txt'):
            if (self.root / path).exists():
                self.assertEqual(self.refused('read', path), files.NOT_SHOWN, path)
        if (self.root / 'public').exists():
            self.assertEqual(self.refused('list', 'public'), files.NOT_SHOWN)
            self.assertEqual(self.refused('search', 'public', 'inside'), files.NOT_SHOWN)

    def test_a_folder_that_is_itself_reached_through_a_link_works(self):
        try:
            os.symlink(self.root, self.base / 'by_link', target_is_directory=True)
        except OSError:
            self.skipTest('links cannot be made here')
        self.assertIn('A garden shed', files.look(str(self.base / 'by_link'), path='README.md'))
        with self.assertRaises(ValueError):
            files.look(str(self.base / 'by_link'), path='../outside.txt')

    def test_what_may_hold_keys_is_never_listed_read_or_searched(self):
        names = ('.env', '.env.local', 'prod.env', 'server.pem', 'id_rsa', 'id_ed25519.pub', 'secrets.yaml', 'my-passwords.txt', 'credentials.json', 'api_key.txt',
                 'apikeys.json', 'token.json', 'github_token.txt', 'terraform.tfvars', 'state.tfstate.backup', '.npmrc', '.netrc', 'auth.json', 'cookies.sqlite',
                 'service-account.json', 'wallet.dat', 'kubeconfig', 'connections.env', 'private_key.der', 'notes.KEY')
        for name in names:
            (self.root / name).write_text('the marker word SHIBBOLETH', encoding='utf-8')
        (self.root / '.a0proj').mkdir()
        (self.root / '.a0proj' / 'variables.env').write_text('SHIBBOLETH', encoding='utf-8')
        (self.root / '.git').mkdir()
        (self.root / '.git' / 'config').write_text('SHIBBOLETH', encoding='utf-8')
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules' / 'x.js').write_text('SHIBBOLETH', encoding='utf-8')
        listed = files.look(str(self.root))
        for name in names:
            self.assertNotIn(name, listed.splitlines(), name)
            self.assertEqual(self.refused('read', name), files.NOT_SHOWN, name)
        self.assertEqual(self.refused('read', '.a0proj/variables.env'), files.NOT_SHOWN)
        self.assertEqual(self.refused('list', '.git'), files.NOT_SHOWN)
        self.assertIn(self.refused('read', 'node_modules/x.js'), (files.NOT_SHOWN, files.NOT_THERE))
        self.assertIn('was not found', files.look(str(self.root), query='SHIBBOLETH'))
        self.assertIn('are left out', listed)
        for name in ('server.key.bak', 'key.txt', 'keys.json', 'deploy_key', 'pass.txt', 'vault.yml', 'group-vault.yaml', '_netrc', 'pgpass.conf', 'wp-config.php', 'oauth2.json',
                     'google-services.json', 'local.settings.json', 'Login Data', 'logins.json', 'key4.db', 'env.local', 'token', 'site.htpasswd'):
            self.assertTrue(files.hidden(name), name)
        for shown in ('.gitignore', 'tokenizer.py', 'environment.md', 'keyboard.txt', 'monkey.py', 'keynote.md', 'passage.txt', 'authors.md', 'hotkeys.js', 'my keyboard.txt', 'compass.md'):
            self.assertFalse(files.hidden(shown), shown)

    def test_a_line_that_looks_like_a_key_is_left_out_wherever_it_stands(self):
        keys = ('api_key = "abcd1234efgh5678"', "password: 'correct horse'".replace(' horse', 'horsebattery'), 'token=ghp_' + 'a' * 36, 'key: sk-' + 'b' * 24,
                'AKIA' + 'ABCDEFGH12345678', 'url = https://someone:hunter22@example.com/x', 'Authorization: Bearer eyJ' + 'c' * 20 + '.' + 'd' * 20,
                'SLACK=xox' + 'b-1234567890-abcdef', 'AIza' + 'e' * 35)
        (self.root / 'config.py').write_text('ordinary = 1\n' + '\n'.join(keys) + '\nordinary_again = 2\n', encoding='utf-8')
        told = files.look(str(self.root), path='config.py')
        self.assertIn('1: ordinary = 1', told)
        self.assertIn('%d: ordinary_again = 2' % (len(keys) + 2), told)
        self.assertEqual(told.count(files.LEFT_OUT), len(keys))
        found = files.look(str(self.root), query='password')
        self.assertIn(files.LEFT_OUT, found)
        self.assertNotIn('correct', found)

    # (Made up, and written with a ~ inside so that nothing here looks like a real key to whoever scans this repository.)
    KEYS = tuple(line.replace('~', '') for line in (
            "SECRET_KEY = 'django-~insecure-3k#x8w!q9zv7r2m5n0p4'", 'PRIVATE_KEY=0x' + '4c0883a69102937d' * 4, 'Authorization: Bearer ~9f8b7c6d5e4f3a2b', 'curl -H "authorization: basic ~dXNlcjpwYXNz"',
            'Account~Key=Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZg==', 'STRIPE=sk_~live_51H8xyzABCDEFGHIJ', 'github_~pat_11ABCDEFG0abcdefghijkl', 'gl~pat-ABCDEFGHIJKLMNOPQRST',
            'hf~_abcdefghijklmnopqrstuvwxyzABCDEFGH', 'https://hooks.~slack.com/services/T00/B00/xyz', 'https://disc~ord.com/api/webhooks/1/abc', 'password: hunter2', 'password = "correct horse battery"',
            'pwd=S3cr3tPwd!!', 'DB_PASS=hunter2hunter2', 'passphrase: one two three four', 'encryption_key: 0123456789abcdef', 'client_secret: abcdefghijkl', 'OPENAI_KEY=abcdef0123456789',
            'MY_OWN_KEY=abcdef0123456789', 'aws_secret_access_key = wJalrXUtnFEMI/K7MDENG', '<password>hunter2hunter2</password>', '"pass": "abcdefghijk"', "'apiKey': 'abcd-efgh'",
            'S~G.abcdefghijklmnop.qrstuvwxyzABCDEFGHIJ', 'ya~29.a0AfH6SMBxxxxxxxx', 'npm~_abcdefghijklmnopqrstuvwxyz0123456789', 'export GH_TOKEN="abc~12345def"', 'session=8f14e45f~ceea167a5a36dedd4bea2543',
            'https://x.test/cb?state=1&access_token=abc~defgh1234', 'MIIEow~IBAAKCAQEA0abcdEFGH12345678ijklMNOP', 'const token = "a1b2~c3d4e5"', 'Set-Cookie: sid=s%3AaBc~123dEf456', 'Passwort: geheim123',
            'the api key: 3f9a~8b7c6d5e', 'password=hunter2 # change it', 'deploy --password hunter2hunter2 --host x', 'deploy --token 8f3k~d92ls0', 'run --api-key=abcd~1234efgh',
            'ENV DB_PASSWORD hunter2hunter2', '  arg  API_TOKEN  abc~123def456', 'define("DB_PASSWORD", "hunter2hunter2");', 'os.environ["API_KEY"] = "abcd~1234efgh"', "'password' => 'hunter2'",
            'setPassword("hunter2hunter2")', 'curl -u admin:hunter2hunter2 https://example.com/api', 'curl --user me:hunter2hunter2 x', 'sshpass -p hunter2hunter2 ssh me@host',
            'mysql -h db -u root -phunter2hunter2', 'CREATE USER me IDENTIFIED BY "hunter2hunter2"', "ALTER ROLE me PASSWORD 'hunter2hunter2'",
            'machine api.example.com login me password hunter2hunter2', 'me:$2~y$10$abcdefghijklmnopqrstuv', 'The admin password is hunter2hunter2',
            'PASSWORD hunter2hunter2', 'wifi password: 1234', 'my pin is 4711', 'PIN: 4711',
            '"private_key": "-----BEGIN PRIVATE ~KEY-----\\nMIIEvQIBADANBg\\n-----END PRIVATE ~KEY-----\\n",'))
    ORDINARY = ('def tokenize(text):', 'tokens = 12', 'max_tokens = 1024', 'the password is asked for later', 'password:', 'password: ""', 'secret = None', 'pass: true', 'passed: 12, failed: 3',
                'A key idea: keep it simple', 'Key decisions: use wood', 'https://example.com/a:b', 'sort_key = name', 'primary key: id', 'the token: a small wooden coin', 'x == secret',
                'for key, value in items.items():', 'key = row["id"]', 'Monkey: ape', 'color: #c8aa72;', 'std::pair<int, int> token;', 'import keyword', 'session = self.store.start("browser", "a")',
                'test_the_pinned_files_match_system_ones_canonical_hashes', 'src/Components/Button2Test/Index3.tsx', 'docs/2026/11/25/MeetingNotes.md', '2026-11-25T00:33:00+00:00',
                'Bearer of good news', 'Steps: level the ground, then build the frame', 'sk-learn-contrib-examples', 'npm_config_registry_url', 'lambda: pass', 'auth: required', 'if a <= b and c >= d: pass',
                'A private key begins with `-----BEGIN PRIVATE KEY-----`.', 'the password is asked for later', 'the password is required', 'git log --oneline --all', 'pip install --upgrade pip',
                'ENV PATH /usr/bin', 'setTitle("Garden shed")', 'define("DB_HOST", "localhost");', 'self.assertEqual("a", "b")', 'curl -u me https://example.com', 'grep -p pattern file', 'python -u main.py',
                'self.session = aiohttp.ClientSession()', 'token = getToken()', 'auth = HTTPBasicAuth(user)', 'app key: Escape', 'The session key: F5', 'Authorized: yes',
                'docker run -u 1000:1000 image', 'docker run --user 1000:1000 -it image', 'rsync -u server:/data/ .', 'usage: tool --password PASSWORD', '  --token <token>', 'password: ${DB_PASSWORD}', 'PASSWORD=%PASSWORD%',
                'reset_password("user@example.com")', "ask_password('Please type it in')", '("password", "Passwort"),', 'su -u www:data', 'mysqldump mysql db -P 3306', 'secret_santa_gift_list_2026', 'SESSION 20261125T1200', 'TOKEN identifier_1',
                'password: ********', 'hf_transformers_version', 'npm_lifecycle_event_name', 'the MIIT is a ministry',
                'pass: 12 tests', 'getUserById2023Version3HandlerFactory()', 'pin: 12', 'README hunter', 'TOKEN COUNT', 'passwords are kept elsewhere')

    def test_made_up_keys_are_seen_beside_a_name_and_mostly_alone(self):
        rnd = random.Random(7)
        letters = string.ascii_letters + string.digits
        for size, alone in ((24, .9), (32, .95), (40, .97)):
            made = [''.join(rnd.choice(letters) for _ in range(size)) for _ in range(300)]
            self.assertTrue(all(files.looks_like_a_key('API_KEY=' + key) for key in made), size)
            self.assertGreaterEqual(sum(files.looks_like_a_key('the one from before: ' + key) for key in made) / len(made), alone, size)

    def test_what_is_taken_for_a_key_and_what_is_not(self):
        for line in self.KEYS:
            self.assertTrue(files.looks_like_a_key(line), line)
            self.assertTrue(files.looks_like_a_key('    ' + line + '   '), line)
        for line in self.ORDINARY:
            self.assertFalse(files.looks_like_a_key(line), line)

    def test_a_private_key_is_left_out_from_its_beginning_to_its_end(self):
        body = ['MIIE%02dabcdefghijklmnop' % n for n in range(6)]
        text = ['How to deploy', '-----BEGIN OPENSSH PRIVATE KEY-----'] + body + ['-----END OPENSSH PRIVATE KEY-----', 'and then restart']
        (self.root / 'deploy.md').write_text('\n'.join(text), encoding='utf-8')
        told = files.look(str(self.root), path='deploy.md')
        self.assertNotIn('MIIE', told)
        self.assertEqual((told.count(files.LEFT_OUT), '1: How to deploy' in told, '10: and then restart' in told), (8, True, True))
        self.assertNotIn('MIIE', files.look(str(self.root), path='deploy.md', from_line=5))        # also when it began before what is read
        self.assertNotIn('MIIE03', files.look(str(self.root), query='MIIE03').replace('"MIIE03"', ''))
        self.assertEqual(files._in_blocks(text[:5] + ['and then more words'] * 1000), {1, 2, 3, 4})      # no end: only what is key text
        self.assertEqual(files._in_blocks(['-----BEGIN CERTIFICATE-----', 'MIIEabcdefghijklmnop']), set())
        # Only to mention how a key begins hides nothing; a key on one line hides that line.
        doc = ['# Keys', 'A private key begins with `-----BEGIN PRIVATE KEY-----`.'] + ['Ordinary sentence %d.' % n for n in range(20)]
        self.assertEqual(files._in_blocks(doc), set())
        self.assertEqual(files._in_blocks(['"k": "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBg\\n-----END PRIVATE KEY-----"', 'ordinaryword', 'anotherword']), set())
        for mention in ('A key begins with -----BEGIN PRIVATE KEY-----', 'It starts with `-----BEGIN PRIVATE KEY-----`.', '-----BEGIN PRIVATE KEY----- is how one begins'):
            self.assertEqual(files._in_blocks([mention, '', 'Installation', '============', 'Run it.']), set(), mention)
        for start in ('-----BEGIN PRIVATE KEY-----', 'KEY="-----BEGIN RSA PRIVATE KEY-----\\n" \\', 'static const char *key = "-----BEGIN EC PRIVATE KEY-----\\n"', '  private_key: |-  -----BEGIN PRIVATE KEY-----',
                      '# -----BEGIN OPENSSH PRIVATE KEY-----', '-----BEGIN PGP PRIVATE KEY BLOCK-----'):
            self.assertEqual(files._in_blocks([start, 'MIIEvQIBADANBgkqhkiG', 'x y z']), {0, 1}, start)
        # A long file of key text is read through once (2026-11-25: the time grew with the square of its length).
        began = time.perf_counter()
        self.assertEqual(len(files._in_blocks(['-----BEGIN PRIVATE KEY-----'] + ['MIIEvQIBADANBgkqhkiG'] * 40000)), 40001)
        self.assertLess(time.perf_counter() - began, 3)
        (self.root / 'doc.md').write_text('\n'.join(doc), encoding='utf-8')
        self.assertNotIn(files.LEFT_OUT, files.look(str(self.root), path='doc.md'))
        one = ['{', '  "private_key": "-----BEGIN PRIVATE KEY-----\\n' + 'MIIEvQIBADANBg' * 200 + '\\n-----END PRIVATE KEY-----\\n",'] + ['  "setting_%d": "value %d",' % (n, n) for n in range(20)]
        (self.root / 'account.json').write_text('\n'.join(one), encoding='utf-8')
        told = files.look(str(self.root), path='account.json')
        self.assertNotIn('MIIE', told)
        self.assertEqual((told.count(files.LEFT_OUT), 'setting_19' in told), (1, True))
        # Written out in a program, a YAML block, with headers, and in PuTTY's own way.
        for lines in (['key = (', '  "-----BEGIN RSA PRIVATE KEY-----\\n"', '  "MIIEabcdefghijklmnop\\n" +', '  "QRSTUVWXYZabcdef\\n"', '  "-----END RSA PRIVATE KEY-----"', ')'],
                      ['key: |', '    -----BEGIN EC PRIVATE KEY-----', '    Proc-Type: 4,ENCRYPTED', '    DEK-Info: AES-128-CBC,ABCDEF0123456789', '', '    MIIEabcdefghijklmnop', '    -----END EC PRIVATE KEY-----', 'next: 1'],
                      ['PuTTY-User-Key-File-3: ssh-rsa', 'Encryption: none', 'Comment: mine', 'Public-Lines: 1', 'AAAAB3NzaC1yc2EAAAA', 'Private-Lines: 1', 'AAABAQCabcdefgh', 'Private-MAC: 0123456789abcdef']):
            (self.root / 'k.txt').write_text('\n'.join(lines), encoding='utf-8')
            told = files.look(str(self.root), path='k.txt')
            for secret in ('MIIE', 'QRSTUV', 'AAABAQ'):
                self.assertNotIn(secret, told)

    def test_no_line_can_hold_anything_up(self):
        import time
        for unit in ('eyJ-', '://a:', 'token ', 'password=', 'sk-', '-----BEGIN ', 'a' * 60 + '=', 'key: ', '<a ', '&key=', 'Bearer ', 'x:'):
            line = (unit * (512 * 1024 // len(unit) + 1))[:512 * 1024]
            (self.root / 'odd.txt').write_text(line, encoding='utf-8')
            began = time.perf_counter()
            files.look(str(self.root), path='odd.txt')
            files.look(str(self.root), query='zzz')
            self.assertLess(time.perf_counter() - began, 2.0, unit)

    def test_binary_and_long_files_are_refused_in_words(self):
        (self.root / 'image.png').write_bytes(b'\x89PNG\x00\x01')
        (self.root / 'big.txt').write_bytes(b'x' * (files.READ_BYTES + 1))
        self.assertEqual(self.refused('read', 'image.png'), files.NOT_TEXT)
        self.assertEqual(self.refused('read', 'big.txt'), files.TOO_LONG)
        self.assertEqual(self.refused('read', 'missing.txt'), files.NOT_THERE)
        self.assertEqual(self.refused('list', 'README.md'), files.NOT_THERE)
        self.assertEqual(self.refused('search', '', 'x'), 'a search needs at least two characters')
        (self.root / 'odd.txt').write_bytes(b'caf\xe9 au lait\n')
        self.assertIn('au lait', files.look(str(self.root), path='odd.txt'))
        (self.root / 'empty.txt').write_bytes(b'')
        self.assertEqual(files.look(str(self.root), path='empty.txt'), 'empty.txt is empty.')

    def test_everything_is_bounded(self):
        many = self.root / 'many'
        many.mkdir()
        for n in range(200):
            (many / ('file%03d.txt' % n)).write_text('the same shed\n' * 30, encoding='utf-8')
        listed = files.look(str(self.root), path='many')
        self.assertIn('(and 120 more)', listed)
        found = files.look(str(self.root), 'search', 'many', 'same shed')
        self.assertLessEqual(len(found.splitlines()), files.SEARCH_HITS + 2)
        self.assertIn('(There may be more.)', found)
        times, opened, real = iter([0.0] + [0.1] * 3 + [99.0] * 500), [], files._text
        with patch.object(files, 'SEARCH_HITS', 10000), patch.object(files, '_text', new=lambda path, limit: opened.append(path) or real(path, limit)):
            stopped = files.searching(str(self.root), 'same shed', 'many', clock=lambda: next(times))
        self.assertEqual(len(opened), 3)                                               # its time was up after three of two hundred files
        self.assertTrue(stopped.endswith('(There may be more.)'), stopped[-60:])        # it stops itself, and says so however long the rest is
        for answer in (listed, found, stopped, files.look(str(self.root), path='many/file000.txt')):
            self.assertLessEqual(len(answer), files.TEXT_LIMIT)
        self.assertLess(files.TEXT_LIMIT, tools.RESULT_LIMIT)
        for n in range(60):
            (many / ('a long name that takes a lot of room in the answer, number %03d, and then some more words.txt' % n)).write_text('x', encoding='utf-8')
        listed = files.look(str(self.root), path='many')
        self.assertLessEqual(len(listed), files.TEXT_LIMIT)
        self.assertRegex(listed.splitlines()[-1], r'^\(and \d+ more\)$')

    def test_a_folder_that_holds_agent_zeros_own_data_is_too_broad(self):
        private = self.base / 'a0' / 'usr'
        (private / 'projects' / 'x').mkdir(parents=True)
        for root, broad in ((self.base, True), (self.base / 'a0', True), (private, True), (private / 'projects' / 'x', False), (private / 'projects', False), (self.root, False)):
            self.assertEqual(files.too_broad(str(root), str(private)), broad, str(root))
        self.assertEqual(self.refused_root(''), files.NO_FOLDER)
        self.assertEqual(self.refused_root(str(self.base / 'missing')), files.NO_FOLDER)

    @staticmethod
    def refused_root(root):
        try:
            files.look(root)
        except ValueError as error:
            return str(error)


class WorkFolder(unittest.TestCase):
    """Which folder it is: the chat's project, else the work folder, as Agent Zero's own code tool picks it (scripted host modules)."""
    def folder(self, chat, project=None, workdir='/a0/usr/workdir'):
        context = types.SimpleNamespace(project=project) if chat else None
        agent = types.ModuleType('agent'); agent.AgentContext = types.SimpleNamespace(get=lambda chat: context)
        helpers = types.ModuleType('helpers'); helpers.__path__ = []
        base = os.path.abspath(os.sep + 'a0')
        parts = {'files': types.SimpleNamespace(fix_dev_path=lambda path: path.replace('/a0', base, 1), get_abs_path=lambda *parts: os.path.join(base, *parts)),
                 'projects': types.SimpleNamespace(get_context_project_name=lambda context: context.project, get_project_folder=lambda name: os.path.join(base, 'usr', 'projects', name)),
                 'settings': types.SimpleNamespace(get_settings=lambda: {'workdir_path': workdir})}
        for name, value in parts.items():
            setattr(helpers, name, value)
        with patch.dict(sys.modules, {'agent': agent, 'helpers': helpers, **{'helpers.' + name: value for name, value in parts.items()}}):
            return os.path.relpath(Host.work_folder(None, chat), base).replace('\\', '/')

    def test_the_chats_project_else_the_work_folder(self):
        self.assertEqual(self.folder('a', 'shed'), 'usr/projects/shed')
        self.assertEqual(self.folder('a'), 'usr/workdir')
        self.assertEqual(self.folder(''), 'usr/workdir')                               # across chats, with none selected
        for workdir in ('', '/a0', '/a0/usr', '/'):
            with self.assertRaises(ValueError) as caught:
                self.folder('a', workdir=workdir)
            self.assertEqual(str(caught.exception), files.NO_FOLDER, workdir)


class Offered(Loop):
    LOCAL = {'roles': {'conversation': {'url': 'http://127.0.0.1:8080/v1'}}}
    REMOTE = {'roles': {'conversation': {'url': 'https://models.example.com/v1', 'remote': True}}}

    def tool(self, config):
        async def scenario(h):
            await self.ask(h)
            return 'project_files' in h.answers[0]['tools']
        return self.play(scenario, config=config)

    def test_only_when_the_conversation_model_is_the_users_own_or_the_user_says_so(self):
        for choice, local, remote in (('local', True, False), ('on', True, True), ('off', False, False)):
            with self.subTest(choice):
                self.assertEqual((self.tool({**self.LOCAL, 'project_files': choice}), self.tool({**self.REMOTE, 'project_files': choice})), (local, remote))

    def test_and_everything_else_their_text_goes_through_is_the_users_own_too(self):
        for other in ({'sidecar': {'url': 'https://gateway.example.com', 'token_env': '', 'remote': True, 'managed': False}},
                      {'summary_writer': 'utility', 'utility_summary_consent': True}):
            with self.subTest(list(other)[0]):
                self.assertEqual((self.tool({**self.LOCAL, **other, 'project_files': 'local'}), self.tool({**self.LOCAL, **other, 'project_files': 'on'})), (False, True))
        self.assertTrue(self.tool({**self.LOCAL, 'summary_writer': 'utility', 'project_files': 'local'}))      # without consent only a utility model of one's own writes

    def test_what_was_looked_up_never_reaches_the_classifier(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'plan.md').write_text('CONFIDENTIAL: the merger closes in May\n', encoding='utf-8')
            seen = []

            async def here(function, *args):
                return function(*args)

            async def scenario(h):
                h.host.work_folder = lambda chat: folder
                real = type(h.sidecar).policy

                async def policy(sidecar, payload):
                    seen.append(json.dumps(payload))
                    return await real(sidecar, payload)
                with patch.object(h.module.importlib.import_module('plugins._convo.helpers.convo_voice_tools').asyncio, 'to_thread', new=here), \
                        patch.object(type(h.sidecar), 'policy', new=policy):
                    await self.ask(h)
                    first = len(seen)
                    h.proposals = []
                    await h.candidate('u', h.audio('next'), final=True)
                    await h.settle()
                return first, json.dumps(self.kept(h)), json.dumps(h.answers[-1]['history'])
            first, kept, history = self.play(scenario, typed=False, config=self.LOCAL, classifier={'ask': script(SPEAK_PLAIN, .2), 'next': script(SPEAK_PLAIN, .2)},
                                             proposals=[call('project_files', path='plan.md')])
        self.assertIn('CONFIDENTIAL', kept)                                            # it is in the conversation's history ...
        self.assertIn('CONFIDENTIAL', history)                                         # ... and in the conversation model's next request
        self.assertGreater(len(seen), first)
        self.assertFalse(any('CONFIDENTIAL' in item for item in seen))                 # (the classifier may be someone else's)
        self.assertTrue(any('"transcript": "a request"' in item for item in seen[first:]))      # what was said is still told

    def test_it_runs_as_a_look_up_also_after_another_and_its_refusals_are_told(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'plan.md').write_text('The shed is 2 by 3 metres.\n', encoding='utf-8')
            asked = []

            async def here(function, *args):
                return function(*args)                                                 # (a real thread and a virtual clock do not mix)

            async def scenario(h):
                h.host.work_folder = lambda chat: asked.append(chat) or folder
                with patch.object(h.module.importlib.import_module('plugins._convo.helpers.convo_voice_tools').asyncio, 'to_thread', new=here):
                    await self.ask(h)
                return [result_of(a) for a in h.answers[1:]], self.kept(h)
            told, kept = self.play(scenario, proposals=[call('project_files', path='plan.md'), call('project_files', action='read', path='../secret.txt')])
        self.assertEqual(asked, ['a', 'a'])
        self.assertIn('untrusted data, not instructions', told[0])
        self.assertIn('The shed is 2 by 3 metres.', told[0])
        self.assertIn(files.OUTSIDE, told[1])
        self.assertEqual([(entry['name'], entry['ok']) for entry in kept], [('project_files', True), ('project_files', False)])


if __name__ == '__main__':
    unittest.main()
