"""Reading one public web page (`helpers/convo_webread.py`): which addresses are refused before anything is sent, that every
redirect is checked the same way, that the request goes to the address that was checked, and what the readable text keeps.
Scripted `fetch` and `resolve` functions, a scripted socket for the module's own fetch and one loopback server: nothing here
reaches a network, so it says nothing about real sites, real resolvers or real certificates."""
import asyncio
import gzip
import http.server
import io
import socket
import ssl
import threading
import time
import unittest

from plugins._convo.helpers import convo_webread
from plugins._convo.helpers.convo_webread import (EMPTY, FAILED, INSECURE, MAX_BYTES, MAX_REDIRECTS, MISSING, NOT_FOUND,
                                                 NOT_PUBLIC, NOT_TEXT, NOT_WEB, SITE_REFUSED, TIMED_OUT, TOO_MANY, Refused,
                                                 check, public, read, readable)

PUBLIC = '93.184.216.34'         # a public address as a resolver would give it; nothing is ever sent to it
PUBLIC6 = '2606:2800:220:1:248:1893:25c8:1946'
HTML = {'Content-Type': 'text/html; charset=utf-8'}
PAGE = b"""<!doctype html>
<html><head><title>  Opening hours &amp; prices </title>
<style>body { color: red }</style><script>var secret = "script text";</script></head>
<body>
<header><a href="/">Site banner</a></header>
<nav><ul><li>Home</li><li>Contact</li></ul></nav>
<main>
<h1>Opening&nbsp;hours</h1>
<p>Monday   to
   Friday: 9&ndash;17 &lt;by appointment&gt;</p>
<p>Caf&eacute; closed on <b>Sunday</b>.</p>
<table><tr><td>Adults</td><td>12 &euro;</td></tr></table>
<aside>Related links</aside>
<form><input name="q"><button>Search</button></form>
<div hidden>hidden words</div><span style="display: none">styled away</span>
</main>
<footer>Copyright footer</footer>
<script>document.write("more script")</script>
</body></html>"""


def answer(body=PAGE, status=200, headers=None, final=None):
    return (status, dict(HTML if headers is None else headers), body, final)


def redirect(target, status=302):
    return (status, {'Location': target}, b'', None)


class Site:
    """A scripted `fetch`: answers by address (an answer, an exception to raise, or a function) and keeps every request."""

    def __init__(self, pages=None, default=None):
        self.pages, self.default, self.calls, self.threads = dict(pages or {}), default, [], []

    def __call__(self, url, timeout):
        self.calls.append(url)
        self.threads.append(threading.get_ident())
        found = self.pages.get(url, self.default)
        if found is None:
            raise AssertionError('unexpected request to ' + url)
        if isinstance(found, BaseException):
            raise found
        status, headers, body, final = found(url, timeout) if callable(found) else found
        return status, headers, body, final or url


class Names:
    """A scripted `resolve`: every name has the addresses given for it (a public one unless said otherwise); keeps every lookup."""

    def __init__(self, **addresses):
        self.addresses, self.calls = {name.replace('_', '.'): value for name, value in addresses.items()}, []

    def __call__(self, host):
        self.calls.append(host)
        found = self.addresses.get(host, [PUBLIC])
        if isinstance(found, BaseException):
            raise found
        return list(found)


class Reading(unittest.IsolatedAsyncioTestCase):
    async def refused(self, url, sentence, fetch=None, resolve=None, **more):
        fetch, resolve = fetch if fetch is not None else Site(), resolve if resolve is not None else Names()
        with self.assertRaises(Refused) as caught:
            await read(url, fetch=fetch, resolve=resolve, **more)
        self.assertEqual(str(caught.exception), sentence)
        self.assertIsInstance(caught.exception, ValueError, 'a refusal is a ValueError the caller can say aloud')
        return fetch, resolve


class Addresses(Reading):
    async def test_addresses_inside_the_network_are_refused_before_anything_is_looked_up_or_sent(self):
        for url in ('http://127.0.0.1/', 'http://127.0.0.1:8080/admin', 'http://127.1.2.3/', 'http://[::1]/', 'http://0.0.0.0/',   # loopback, unspecified
                    'http://10.0.0.5/', 'http://172.16.4.1/', 'http://172.31.255.254/', 'http://192.168.1.1/router',               # private
                    'http://169.254.169.254/latest/meta-data/', 'http://[fe80::1]/',                                              # link-local
                    'http://100.64.0.1/', 'http://100.100.100.100/', 'http://100.127.255.254/',                                   # carrier-grade NAT
                    'http://[fd00::1]/', 'http://[fc00::abcd]/',                                                                  # unique-local IPv6
                    'http://[::ffff:127.0.0.1]/', 'http://[::ffff:192.168.1.1]/', 'http://[::ffff:a00:1]/',                       # IPv4 inside IPv6
                    'http://[64:ff9b::7f00:1]/', 'http://[2002:c0a8:101::1]/', 'http://[::127.0.0.1]/',                           # NAT64, 6to4, IPv4-compatible
                    'http://224.0.0.1/', 'http://[ff02::1]/', 'http://255.255.255.255/', 'http://240.0.0.1/',                     # multicast, broadcast, reserved
                    'http://192.0.2.10/', 'http://198.18.0.1/', 'http://[2001:db8::1]/', 'http://[fe80::1%25eth0]/'):              # documentation, benchmark, scoped
            with self.subTest(url=url):
                fetch, resolve = await self.refused(url, NOT_PUBLIC)
                self.assertEqual((fetch.calls, resolve.calls), ([], []))

    async def test_names_that_are_never_public_are_refused_without_a_lookup(self):
        for url in ('http://localhost/', 'http://router/', 'http://nas:5000/', 'https://intranet/wiki',          # no dot
                    'http://printer.local/', 'http://nas.lan/', 'http://build.internal/', 'http://db.corp.internal/',
                    'http://box.home.arpa/', 'http://desktop.tailnet-1234.ts.net/', 'http://app.localhost/',
                    'http://LOCALHOST/', 'http://printer.LOCAL./', 'http://router./',
                    'http://2130706433/', 'http://127.1/', 'http://0x7f.0.0.1/', 'http://017700000001/'):         # numbers a resolver may read as an address
            with self.subTest(url=url):
                fetch, resolve = await self.refused(url, NOT_PUBLIC)
                self.assertEqual((fetch.calls, resolve.calls), ([], []))

    async def test_a_public_name_that_resolves_to_a_private_address_is_refused(self):
        for addresses in (['192.168.1.20'], ['127.0.0.1'], ['10.1.2.3'], ['169.254.169.254'], ['100.64.3.4'], ['fd12:3456::1'],
                          ['::ffff:10.0.0.1'], ['::1'], ['0.0.0.0'], ['fe80::1%eth0'], ['not an address']):
            with self.subTest(addresses=addresses):
                fetch, resolve = await self.refused('https://harmless.example.com/', NOT_PUBLIC, resolve=Names(harmless_example_com=addresses))
                self.assertEqual((fetch.calls, resolve.calls), ([], ['harmless.example.com']))

    async def test_one_private_address_among_public_ones_refuses_the_name(self):
        for addresses in ([PUBLIC, '192.168.1.20'], ['10.0.0.1', PUBLIC], [PUBLIC, PUBLIC6, '::1'], [PUBLIC6, 'fd00::2']):
            with self.subTest(addresses=addresses):
                fetch, _ = await self.refused('http://mixed.example.org/', NOT_PUBLIC, resolve=Names(mixed_example_org=addresses))
                self.assertEqual(fetch.calls, [])

    async def test_a_name_that_cannot_be_found_or_has_no_address(self):
        for found in (OSError('getaddrinfo failed: 11001 secret detail'), []):
            with self.subTest(found=found):
                fetch, _ = await self.refused('http://nowhere.example.net/', NOT_FOUND, resolve=Names(nowhere_example_net=found))
                self.assertEqual(fetch.calls, [])

    async def test_only_http_and_https(self):
        for url in ('ftp://example.com/file', 'file:///etc/passwd', 'gopher://example.com/', 'javascript:alert(1)', 'data:text/html,hi',
                    'about:blank', 'example.com/page', '//example.com/page', 'http://', 'https:///path', '', None,
                    'http://exa mple.com/', 'http://example.com/\npath', 'http://example.com\\@127.0.0.1/', 'http://exam%70le.com/',
                    'http://example.com:99999/', 'http://[::1/', 'http://example.com/' + 'a' * 2100):
            with self.subTest(url=url):
                fetch, resolve = await self.refused(url, NOT_WEB)
                self.assertEqual((fetch.calls, resolve.calls), ([], []))

    async def test_credentials_in_the_address_are_refused(self):
        for url in ('http://user:password@example.com/', 'https://user@example.com/', 'http://@example.com/', 'http://:pw@example.com/',
                    'http://example.com@192.168.1.1/', 'http://192.168.1.1@example.com/'):
            with self.subTest(url=url):
                fetch, resolve = await self.refused(url, NOT_WEB)
                self.assertEqual((fetch.calls, resolve.calls), ([], []))

    def test_public_tells_public_addresses_from_the_rest(self):
        for address in (PUBLIC, '8.8.8.8', '1.1.1.1', '100.63.255.255', '100.128.0.1', '172.15.0.1', '172.32.0.1', PUBLIC6,
                        '2a00:1450:4001:81b::200e', '::ffff:8.8.8.8', '64:ff9b::808:808', '2002:808:808::1'):
            self.assertTrue(public(address), address)
        for address in ('127.0.0.1', '10.255.255.255', '192.168.0.1', '169.254.1.1', '100.64.0.0', '100.127.255.255', '0.0.0.0', '::',
                        '::1', 'fc00::1', 'fdff::1', 'fe80::1', 'fec0::1', 'ff0e::1', '::ffff:192.168.0.1', '64:ff9b::a00:1',
                        '64:ff9b:1::1', '2002:7f00:1::1', '2001:0:4136:e378:8000:63bf:3fff:fdd2', '2001:db8::5', '100::1', '', 'example.com',
                        None, '1.2.3', '192.168.1.1/24'):
            self.assertFalse(public(address), address)

    def test_check_returns_what_a_request_may_use(self):
        resolve = Names(example_com=[PUBLIC, PUBLIC6, PUBLIC])
        self.assertEqual(check('HTTPS://Example.COM/a%20b?q=1#part', resolve),
                         ('https://example.com/a%20b?q=1', 'example.com', 443, [PUBLIC, PUBLIC6]))
        self.assertEqual(check('http://example.com:8080', resolve), ('http://example.com:8080/', 'example.com', 8080, [PUBLIC, PUBLIC6]))
        self.assertEqual(check('http://example.com:80/x', resolve)[0], 'http://example.com/x')
        self.assertEqual(check('http://8.8.8.8/x', resolve), ('http://8.8.8.8/x', '8.8.8.8', 80, ['8.8.8.8']))
        self.assertEqual(check('https://[%s]:8443/' % PUBLIC6, resolve),
                         ('https://[%s]:8443/' % PUBLIC6, PUBLIC6, 8443, [PUBLIC6]))
        self.assertEqual(check('http://b\u00fccher.example/', Names())[1], 'xn--bcher-kva.example', 'a name in another script is looked up in its ASCII form')


class Redirects(Reading):
    async def test_a_redirect_is_followed_and_checked_like_the_first_address(self):
        fetch = Site({'http://example.com/old': redirect('https://www.example.com/new'), 'https://www.example.com/new': answer()})
        resolve = Names()
        result = await read('http://example.com/old', fetch=fetch, resolve=resolve)
        self.assertEqual(fetch.calls, ['http://example.com/old', 'https://www.example.com/new'])
        self.assertEqual(resolve.calls, ['example.com', 'www.example.com'], 'each host is looked up, once')
        self.assertEqual((result['url'], result['title']), ('https://www.example.com/new', 'Opening hours & prices'))

    async def test_a_relative_redirect_stays_on_the_site(self):
        fetch = Site({'https://example.com/a/b': redirect('../c?x=1', 301), 'https://example.com/c?x=1': answer()})
        self.assertEqual((await read('https://example.com/a/b', fetch=fetch, resolve=Names()))['url'], 'https://example.com/c?x=1')

    async def test_a_redirect_to_an_address_inside_the_network_is_refused(self):
        for target in ('http://192.168.1.1/admin', 'http://127.0.0.1:8000/', 'http://[::1]/', 'http://169.254.169.254/latest/',
                       '//10.0.0.7/x', 'http://router/', 'http://nas.local/', 'http://[::ffff:10.0.0.1]/', 'http://100.64.0.9/'):
            with self.subTest(target=target):
                fetch, _ = await self.refused('http://example.com/go', NOT_PUBLIC, fetch=Site({'http://example.com/go': redirect(target)}))
                self.assertEqual(fetch.calls, ['http://example.com/go'], 'nothing is sent to the redirect target')

    async def test_a_redirect_to_a_name_that_resolves_inside_the_network_is_refused(self):
        fetch = Site({'http://example.com/go': redirect('http://rebind.example.org/')})
        fetch, resolve = await self.refused('http://example.com/go', NOT_PUBLIC, fetch=fetch, resolve=Names(rebind_example_org=['192.168.0.9']))
        self.assertEqual((fetch.calls, resolve.calls), (['http://example.com/go'], ['example.com', 'rebind.example.org']))

    async def test_a_redirect_to_another_scheme_or_with_credentials_is_refused(self):
        for target in ('ftp://example.com/file', 'file:///etc/passwd', 'javascript:alert(1)', 'http://user:pw@example.com/'):
            with self.subTest(target=target):
                await self.refused('http://example.com/go', NOT_WEB, fetch=Site({'http://example.com/go': redirect(target)}))

    async def test_a_fetch_that_ended_somewhere_else_has_that_place_checked_too(self):
        """An injected fetch may follow redirects by itself; where it says it ended is not taken on trust."""
        for final, sentence in (('http://192.168.1.1/', NOT_PUBLIC), ('http://printer.local/', NOT_PUBLIC), ('ftp://example.com/', NOT_WEB)):
            with self.subTest(final=final):
                await self.refused('http://example.com/', sentence, fetch=Site({'http://example.com/': answer(final=final)}))
        fetch = Site({'http://example.com/': answer(final='https://www.example.com/home#top')})
        self.assertEqual((await read('http://example.com/', fetch=fetch, resolve=Names()))['url'], 'https://www.example.com/home')

    async def test_three_redirects_are_followed_and_a_fourth_is_not(self):
        hops = {'http://example.com/%d' % number: redirect('/%d' % (number + 1)) for number in range(MAX_REDIRECTS)}
        fetch = Site({**hops, 'http://example.com/%d' % MAX_REDIRECTS: answer()})
        self.assertEqual((await read('http://example.com/0', fetch=fetch, resolve=Names()))['url'], 'http://example.com/3')
        self.assertEqual(len(fetch.calls), MAX_REDIRECTS + 1)

        more = Site({**hops, 'http://example.com/3': redirect('/4'), 'http://example.com/4': answer()})
        await self.refused('http://example.com/0', TOO_MANY, fetch=more)
        self.assertEqual(more.calls, ['http://example.com/%d' % number for number in range(4)], 'the fifth address is never asked')
        loop = Site(default=lambda url, timeout: redirect(url))
        await self.refused('http://example.com/loop', TOO_MANY, fetch=loop)
        self.assertEqual(len(loop.calls), MAX_REDIRECTS + 1)

    async def test_a_redirect_without_a_target_fails(self):
        await self.refused('http://example.com/', FAILED, fetch=Site({'http://example.com/': (302, {}, b'', None)}))


class Pages(Reading):
    async def test_a_normal_page_gives_its_title_and_readable_text(self):
        fetch = Site({'https://example.com/hours': answer()})
        result = await read('https://example.com/hours#visit', fetch=fetch, resolve=Names())
        self.assertEqual(result, {'url': 'https://example.com/hours', 'title': 'Opening hours & prices', 'truncated': False,
                                  'text': 'Opening hours\nMonday to Friday: 9\u201317 <by appointment>\nCaf\u00e9 closed on Sunday.\nAdults 12 \u20ac'})
        for gone in ('script text', 'more script', 'color: red', 'Site banner', 'Home', 'Contact', 'Related links', 'Search',
                     'Copyright footer', 'hidden words', 'styled away', '  ', '\t', '\xa0', '&'):
            self.assertNotIn(gone, result['text'], gone)
        self.assertNotEqual(threading.get_ident(), fetch.threads[0], 'the blocking work does not run on the event loop')

    async def test_text_is_cut_to_the_limit_and_says_so(self):
        body = ('<title>Long</title><p>' + 'word ' * 4000 + '</p>').encode()
        result = await read('http://example.com/', limit=100, fetch=Site({'http://example.com/': answer(body)}), resolve=Names())
        self.assertEqual((len(result['text']) <= 100, result['truncated'], result['title']), (True, True, 'Long'))
        self.assertTrue(result['text'].startswith('word word'))
        short = await read('http://example.com/', limit=100, fetch=Site({'http://example.com/': answer(b'<p>short</p>')}), resolve=Names())
        self.assertEqual((short['text'], short['truncated'], short['title']), ('short', False, ''))

    async def test_a_body_over_the_size_cap_is_cut_and_flagged(self):
        body = b'<title>Big</title><p>start of the page</p>' + b'<!-- ' + b'x' * (MAX_BYTES + 5000) + b' --><p>the very end</p>'
        result = await read('http://example.com/', fetch=Site({'http://example.com/': answer(body)}), resolve=Names())
        self.assertEqual((result['title'], result['text'], result['truncated']), ('Big', 'start of the page', True))
        whole = b'<p>start</p>' + b'<!-- ' + b'x' * (MAX_BYTES - 200) + b' --><p>the very end</p>'
        result = await read('http://example.com/', fetch=Site({'http://example.com/': answer(whole)}), resolve=Names())
        self.assertEqual((result['text'], result['truncated']), ('start\nthe very end', False), 'a page under the cap is read whole')

    async def test_only_text_pages_are_read(self):
        for kind in ('application/pdf', 'image/png', 'application/json', 'application/octet-stream', 'video/mp4', 'text/css',
                     'application/xhtml+xml', 'text/htmlx', ''):
            with self.subTest(kind=kind):
                await self.refused('http://example.com/file', NOT_TEXT, fetch=Site({'http://example.com/file': answer(b'%PDF-1.7', headers={'Content-Type': kind})}))
        await self.refused('http://example.com/file', NOT_TEXT, fetch=Site({'http://example.com/file': answer(b'<p>no type</p>', headers={})}))

    async def test_a_plain_text_page_keeps_its_lines(self):
        body = 'First  line\r\n\r\n\tSecond line \u00e9\r\nThird\u200b line'.encode('utf-8')
        fetch = Site({'http://example.com/notes.txt': answer(body, headers={'content-type': 'Text/Plain; Charset=UTF-8'})})
        result = await read('http://example.com/notes.txt', fetch=fetch, resolve=Names())
        self.assertEqual(result, {'url': 'http://example.com/notes.txt', 'title': '', 'text': 'First line\nSecond line \u00e9\nThird line', 'truncated': False})

    async def test_the_charset_comes_from_the_server_then_from_the_page(self):
        text = 'Gr\u00fc\u00dfe aus K\u00f6ln'
        cases = ((text.encode('latin-1'), {'Content-Type': 'text/html; charset=ISO-8859-1'}, '<p>%s</p>'),
                 (text.encode('cp1252'), {'Content-Type': 'text/html'}, '<meta charset="windows-1252"><p>%s</p>'),
                 (text.encode('utf-8'), {'Content-Type': 'text/html'}, '<p>%s</p>'),
                 (text.encode('utf-8'), {'Content-Type': 'text/html; charset=no-such-charset'}, '<p>%s</p>'),
                 (b'\xef\xbb\xbf' + text.encode('utf-8'), {'Content-Type': 'text/plain; charset=latin-1'}, '%s'))
        for encoded, headers, frame in cases:
            with self.subTest(headers=headers, frame=frame):
                before, after = (part.encode('ascii') for part in frame.split('%s'))
                fetch = Site({'http://example.com/': answer(before + encoded + after, headers={**headers})})
                self.assertEqual((await read('http://example.com/', fetch=fetch, resolve=Names()))['text'], text)

    async def test_a_compressed_body_is_inflated_within_the_cap(self):
        fetch = Site({'http://example.com/': answer(gzip.compress(PAGE), headers={**HTML, 'Content-Encoding': 'gzip'})})
        self.assertEqual((await read('http://example.com/', fetch=fetch, resolve=Names()))['title'], 'Opening hours & prices')
        bomb = gzip.compress(b'<p>start</p>' + b' ' * (MAX_BYTES * 3), 1)
        result = await read('http://example.com/', fetch=Site({'http://example.com/': answer(bomb, headers={**HTML, 'Content-Encoding': 'gzip'})}),
                            resolve=Names())
        self.assertEqual((result['text'], result['truncated']), ('start', True), 'a small body that inflates to a huge one is cut at the cap')
        for encoding, body in (('br', b'\x8b\x03\x80'), ('gzip', b'this is not gzip')):
            with self.subTest(encoding=encoding):
                await self.refused('http://example.com/', FAILED, fetch=Site({'http://example.com/': answer(body, headers={**HTML, 'Content-Encoding': encoding})}))

    async def test_statuses_that_are_not_a_page(self):
        for status, sentence in ((404, MISSING), (410, MISSING), (401, SITE_REFUSED), (403, SITE_REFUSED), (429, SITE_REFUSED),
                                 (500, FAILED), (503, FAILED), (304, FAILED), (100, FAILED), ('what', FAILED)):
            with self.subTest(status=status):
                await self.refused('http://example.com/', sentence, fetch=Site({'http://example.com/': answer(b'<p>Internal detail</p>', status=status)}))

    async def test_a_page_without_readable_text(self):
        for body in (b'', b'<html><head><title></title></head><body><script>render()</script></body></html>', b'<nav>Menu</nav>   '):
            with self.subTest(body=body):
                await self.refused('http://example.com/', EMPTY, fetch=Site({'http://example.com/': answer(body)}))

    async def test_a_refusal_never_carries_the_address_or_the_failure_text(self):
        failures = (OSError('connect to 93.184.216.34 failed'), RuntimeError('token=abc123'), ValueError('bad header from server'),
                    ssl.SSLError('certificate for internal.example'), ssl.SSLCertVerificationError('hostname mismatch'), KeyError('x'))
        for failure in failures:
            with self.subTest(failure=failure):
                with self.assertRaises(Refused) as caught:
                    await read('https://private-detail.example.com/secret-path', fetch=Site(default=failure), resolve=Names())
                self.assertEqual(str(caught.exception), INSECURE if isinstance(failure, ssl.SSLError) else FAILED)
                self.assertIsNone(caught.exception.__cause__)
        sentences = [getattr(convo_webread, name) for name in ('NOT_WEB', 'NOT_PUBLIC', 'NOT_FOUND', 'TOO_MANY', 'NOT_TEXT', 'TIMED_OUT',
                                                              'FAILED', 'SITE_REFUSED', 'MISSING', 'INSECURE', 'EMPTY')]
        for sentence in sentences:
            self.assertRegex(sentence, r'^[A-Z][A-Za-z ]+$', 'one short plain sentence, nothing filled in')


class Timing(Reading):
    async def test_a_fetch_that_times_out(self):
        for failure in (TimeoutError('timed out'), socket.timeout('The read operation timed out')):
            with self.subTest(failure=failure):
                await self.refused('http://example.com/slow', TIMED_OUT, fetch=Site(default=failure))

    async def test_a_page_that_never_answers_is_given_up_within_the_budget(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def hang(url, timeout):
            release.wait(5)
            return answer()
        started = time.monotonic()
        await self.refused('http://example.com/hangs', TIMED_OUT, fetch=Site(default=hang), budget=0.15)
        self.assertLess(time.monotonic() - started, 2.0)

    async def test_the_time_left_is_what_a_fetch_is_given_and_redirects_share_one_budget(self):
        given = []

        def slow(url, timeout):
            given.append(timeout)
            time.sleep(0.12)
            return redirect('/next')
        await self.refused('http://example.com/a', TIMED_OUT, fetch=Site(default=slow), budget=0.3)
        self.assertTrue(1 <= len(given) <= 3, given)
        self.assertLessEqual(given[0], 0.3 + 1e-6)          # the monotonic clock rounds
        self.assertEqual(given, sorted(given, reverse=True), 'each hop gets only what is left')

    async def test_the_event_loop_keeps_running_while_a_page_is_read(self):
        ticks, fetched = [], []

        async def tick():
            for _ in range(5):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        def slow(url, timeout):
            time.sleep(0.4)
            fetched.append(time.monotonic())
            return answer()
        result, _ = await asyncio.gather(read('http://example.com/', fetch=Site(default=slow), resolve=Names()), tick())
        self.assertEqual(result['title'], 'Opening hours & prices')
        self.assertGreaterEqual(sum(at < fetched[0] for at in ticks), 3, 'the loop ran its own work during the blocking fetch')


class Wire:
    """A scripted socket: keeps what is sent and answers with prepared bytes, the way `http.client` reads them."""

    def __init__(self, reply):
        self.reply, self.sent, self.timeouts, self.closed = reply, b'', [], False

    def sendall(self, data):
        self.sent += bytes(data)

    def makefile(self, mode, *args, **kwargs):
        return io.BytesIO(self.reply)

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        self.closed = True


class Dialer:
    """A scripted `connect`: hands out the prepared sockets (or raises the prepared failure) and keeps where it was asked to go."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, address, timeout=None, *args):
        self.calls.append(address)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Tls:
    """A scripted TLS context: keeps the name it was asked to verify and passes the socket through."""

    def __init__(self, failure=None):
        self.names, self.failure = [], failure

    def wrap_socket(self, sock, server_hostname=None):
        self.names.append(server_hostname)
        if self.failure:
            raise self.failure
        return sock


def reply(body=b'<title>Wire</title><p>from the wire</p>', status='200 OK', headers=('Content-Type: text/html',), length=True):
    lines = ['HTTP/1.1 ' + status, *headers, *(['Content-Length: %d' % len(body)] if length else []), 'Connection: close', '', '']
    return '\r\n'.join(lines).encode('ascii') + body


class OwnFetch(Reading):
    """The module's own fetch, on scripted sockets: where it connects, what it sends, and what it verifies."""

    def test_it_connects_to_the_checked_address_and_names_the_host_in_the_request(self):
        wire = Wire(reply())
        dial = Dialer(wire)
        status, headers, body, final = convo_webread._fetch('http://example.com/a%20b?q=caf%C3%A9&x=1', 5.0, [PUBLIC], connect=dial)
        self.assertEqual(dial.calls, [(PUBLIC, 80)], 'the socket goes to the address that was checked, not to the name')
        request = wire.sent.decode('ascii').split('\r\n')
        self.assertEqual(request[0], 'GET /a%20b?q=caf%C3%A9&x=1 HTTP/1.1')
        sent = {line.split(': ', 1)[0].lower(): line.split(': ', 1)[1] for line in request[1:] if line}
        self.assertEqual(sent, {'host': 'example.com', 'user-agent': convo_webread.USER_AGENT, 'accept': 'text/html, text/plain;q=0.9',
                                'accept-encoding': 'identity', 'connection': 'close'}, 'no cookie, no authorization, nothing else')
        self.assertEqual((status, headers['content-type'], body, final),
                         (200, 'text/html', b'<title>Wire</title><p>from the wire</p>', 'http://example.com/a%20b?q=caf%C3%A9&x=1'))
        self.assertTrue(wire.closed)
        self.assertTrue(wire.timeouts and all(0 < value <= 5.0 for value in wire.timeouts), 'the socket never waits past the time given')

    def test_a_port_that_is_not_the_usual_one_is_in_the_host_header_and_the_connection(self):
        wire = Wire(reply())
        dial = Dialer(wire)
        convo_webread._fetch('http://example.com:8080/', 5.0, [PUBLIC], connect=dial)
        self.assertEqual(dial.calls, [(PUBLIC, 8080)])
        self.assertIn(b'\r\nHost: example.com:8080\r\n', wire.sent)
        wire = Wire(reply())
        convo_webread._fetch('http://[%s]/' % PUBLIC6, 5.0, [PUBLIC6], connect=Dialer(wire))
        self.assertIn(('\r\nHost: [%s]\r\n' % PUBLIC6).encode(), wire.sent)

    def test_https_verifies_the_certificate_against_the_host_name_not_the_address(self):
        wire, tls = Wire(reply()), Tls()
        dial = Dialer(wire)
        status, _, body, _ = convo_webread._fetch('https://example.com/secure', 5.0, [PUBLIC], connect=dial, tls=tls)
        self.assertEqual((dial.calls, tls.names, status), ([(PUBLIC, 443)], ['example.com'], 200))
        self.assertIn(b'\r\nHost: example.com\r\n', wire.sent)
        wire, tls = Wire(reply()), Tls()
        convo_webread._fetch('https://example.com:8443/', 5.0, [PUBLIC], connect=Dialer(wire), tls=tls)
        self.assertEqual(tls.names, ['example.com'])
        self.assertIn(b'\r\nHost: example.com:8443\r\n', wire.sent)
        plain = Tls()
        convo_webread._fetch('http://example.com/', 5.0, [PUBLIC], connect=Dialer(Wire(reply())), tls=plain)
        self.assertEqual(plain.names, [], 'plain http is not wrapped')

    def test_the_default_tls_context_checks_the_chain_and_the_name(self):
        context = convo_webread._verified()
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIs(convo_webread._verified(), context, 'made once')
        used, wire = [], Wire(reply())
        original = ssl.SSLContext.wrap_socket

        def wrap(self, sock, *args, **kwargs):
            used.append((self, kwargs.get('server_hostname')))
            raise ssl.SSLError('a scripted socket cannot do a handshake')
        ssl.SSLContext.wrap_socket = wrap
        self.addCleanup(setattr, ssl.SSLContext, 'wrap_socket', original)
        with self.assertRaises(ssl.SSLError):
            convo_webread._fetch('https://example.com/', 5.0, [PUBLIC], connect=Dialer(wire))
        self.assertEqual(used, [(context, 'example.com')], 'with no context handed in, https uses the verifying one and the page host name')
        self.assertTrue(wire.closed, 'a socket whose handshake failed is closed')

    async def test_a_certificate_that_does_not_verify_refuses_the_page(self):
        original = convo_webread._fetch
        tls = Tls(ssl.SSLCertVerificationError('certificate verify failed: hostname mismatch'))
        convo_webread._fetch = lambda url, timeout, addresses: original(url, timeout, addresses, connect=Dialer(Wire(reply())), tls=tls)
        self.addCleanup(setattr, convo_webread, '_fetch', original)
        with self.assertRaises(Refused) as caught:
            await read('https://example.com/', resolve=Names())
        self.assertEqual((str(caught.exception), tls.names), (INSECURE, ['example.com']))

    def test_the_next_checked_address_is_tried_when_one_cannot_be_reached(self):
        wire = Wire(reply())
        dial = Dialer(ConnectionRefusedError('refused'), wire)
        status, _, _, _ = convo_webread._fetch('http://example.com/', 5.0, [PUBLIC6, PUBLIC], connect=dial)
        self.assertEqual((dial.calls, status), ([(PUBLIC6, 80), (PUBLIC, 80)], 200))
        dial = Dialer(ConnectionRefusedError('refused'), OSError('unreachable'))
        with self.assertRaises(OSError):
            convo_webread._fetch('http://example.com/', 5.0, [PUBLIC6, PUBLIC], connect=dial)
        self.assertEqual(dial.calls, [(PUBLIC6, 80), (PUBLIC, 80)], 'only checked addresses are ever tried')

    def test_a_redirect_is_returned_not_followed(self):
        wire = Wire(reply(b'moved', status='302 Found', headers=('Location: http://192.168.1.1/admin', 'Content-Type: text/html')))
        dial = Dialer(wire)
        status, headers, body, final = convo_webread._fetch('http://example.com/', 5.0, [PUBLIC], connect=dial)
        self.assertEqual((status, headers['location'], body, final, len(dial.calls)), (302, 'http://192.168.1.1/admin', b'', 'http://example.com/', 1))

    def test_a_body_is_read_only_for_a_text_page_and_only_up_to_the_cap(self):
        wire = Wire(reply(b'%PDF' + b'x' * 5000, headers=('Content-Type: application/pdf',)))
        self.assertEqual(convo_webread._fetch('http://example.com/', 5.0, [PUBLIC], connect=Dialer(wire))[2], b'')
        wire = Wire(reply(b'<p>' + b'y' * (MAX_BYTES + 70000), length=False))
        body = convo_webread._fetch('http://example.com/', 5.0, [PUBLIC], connect=Dialer(wire))[2]
        self.assertEqual(len(body), MAX_BYTES + 1, 'one byte past the cap tells the reader there was more')

    def test_no_time_left_means_no_connection(self):
        dial = Dialer(Wire(reply()))
        with self.assertRaises(TimeoutError):
            convo_webread._fetch('http://example.com/', 0, [PUBLIC], connect=dial)
        self.assertEqual(dial.calls, [])

    async def test_a_second_answer_to_the_same_lookup_cannot_move_the_request(self):
        """The name is looked up once; the connection uses that answer even when the resolver would now say something else."""
        answers, lookups = [[PUBLIC], ['127.0.0.1'], ['192.168.1.1']], []

        def rebinding(host):
            lookups.append(host)
            return answers[min(len(lookups), len(answers)) - 1]
        dial, original = Dialer(Wire(reply())), convo_webread._fetch
        convo_webread._fetch = lambda url, timeout, addresses: original(url, timeout, addresses, connect=dial)
        self.addCleanup(setattr, convo_webread, '_fetch', original)
        result = await read('http://rebind.example.com/', resolve=rebinding)
        self.assertEqual((lookups, dial.calls), (['rebind.example.com'], [(PUBLIC, 80)]))
        self.assertEqual((result['title'], result['text']), ('Wire', 'from the wire'))


class Loopback(Reading):
    """The module's own fetch against a real socket: a server on this machine's loopback stands in for the public address
    the request was told to use (the scripted `connect` dials the server instead). Nothing leaves the machine."""

    def setUp(self):
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, dict(self.headers)))
                if self.path == '/moved':
                    self.send_response(301)
                    self.send_header('Location', 'http://127.0.0.1:%d/secret' % self.server.server_port)
                    self.end_headers()
                    return
                body = b'<title>Local stand-in</title><p>' + b'z' * (2000000 if self.path == '/big' else 10) + b'</p>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.dialled = []
        original = convo_webread._fetch

        def connect(address, timeout=None, *args):
            self.dialled.append(address)
            return convo_webread.socket.create_connection(('127.0.0.1', self.server.server_port), timeout)
        convo_webread._fetch = lambda url, timeout, addresses: original(url, timeout, addresses, connect=connect)
        self.addCleanup(setattr, convo_webread, '_fetch', original)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    async def test_a_page_is_read_over_a_real_socket(self):
        result = await read('http://stand-in.example.com:8123/page?x=1', resolve=Names())
        self.assertEqual(result, {'url': 'http://stand-in.example.com:8123/page?x=1', 'title': 'Local stand-in', 'text': 'z' * 10, 'truncated': False})
        self.assertEqual(self.dialled, [(PUBLIC, 8123)])
        path, headers = self.seen[0]
        self.assertEqual((path, headers['Host'], headers['User-Agent']), ('/page?x=1', 'stand-in.example.com:8123', convo_webread.USER_AGENT))
        self.assertFalse({'Cookie', 'Authorization'} & set(headers))

    async def test_a_page_over_the_cap_is_cut_on_a_real_socket(self):
        result = await read('http://stand-in.example.com/big', resolve=Names(), limit=50)
        self.assertEqual((result['title'], result['text'], result['truncated']), ('Local stand-in', 'z' * 50, True))

    async def test_a_real_redirect_into_the_network_is_refused_and_never_requested(self):
        with self.assertRaises(Refused) as caught:
            await read('http://stand-in.example.com/moved', resolve=Names())
        self.assertEqual(str(caught.exception), NOT_PUBLIC)
        self.assertEqual([path for path, _ in self.seen], ['/moved'], 'the redirect target was never asked')


class Text(unittest.TestCase):
    def test_what_is_left_out_and_what_is_kept(self):
        title, text, cut = readable('<html><head><title>T</title></head><body><noscript>enable js</noscript><template>tpl</template>'
                                    '<svg><title>icon</title><text>svg words</text></svg><p>kept <i>inline</i> words</p>'
                                    '<select><option>choice</option></select><textarea>typed</textarea><iframe>frame</iframe>'
                                    '<p aria-hidden="true">decoration</p><ul><li>one<li>two</ul><p>a<br>b<hr>c</p></body></html>')
        self.assertEqual((title, text, cut), ('T', 'kept inline words\none\ntwo\na\nb\nc', False))

    def test_an_element_that_is_never_closed_does_not_swallow_the_page(self):
        _, text, _ = readable('<div><nav><a>Menu</a></div><p>after the menu</p><footer>foot<p>the end is missing')
        self.assertEqual(text, 'after the menu')
        _, text, _ = readable('</div></nav></p><p>stray end tags are ignored</p></section>')
        self.assertEqual(text, 'stray end tags are ignored')

    def test_entities_and_whitespace(self):
        title, text, _ = readable('<title>A &amp;\n  B &#8212; C</title><p>x&nbsp;&nbsp;y &lt;tag&gt; &quot;q&quot; &#x41;&copy;\u200b\x00\x07</p>\n\n\n<p>  </p><p>z</p>')
        self.assertEqual((title, text), ('A & B \u2014 C', 'x y <tag> "q" A\u00a9\nz'))

    def test_only_the_first_title_counts_and_it_is_not_repeated_in_the_text(self):
        title, text, _ = readable('<title>First</title><body><title>Second</title><p>body</p>')
        self.assertEqual((title, text), ('First', 'body'))

    def test_parsing_stops_when_there_is_enough_text_or_no_time(self):
        _, text, cut = readable('<p>' + 'word ' * 200000 + '</p><p>never reached</p>', limit=100)
        self.assertTrue(cut)
        self.assertLess(len(text), 100000)
        clock = iter([0.0, 5.0, 5.0]).__next__
        _, text, cut = readable('<p>first</p>' + ' ' * 20000 + '<p>second</p>' + ' ' * 20000 + '<p>third</p>', limit=6000, clock=clock, deadline=1.0)
        self.assertEqual((text, cut), ('first', True))

    def test_a_deeply_nested_or_malformed_page_is_still_quick(self):
        started = time.monotonic()
        readable('<div>' * 60000 + '</span>' * 60000 + '<p>x</p>', limit=6000)
        readable('<a ' * 50000 + '<!--' * 20000 + '<![' * 20000, limit=6000)
        self.assertLess(time.monotonic() - started, 5.0)


if __name__ == '__main__':
    unittest.main()
