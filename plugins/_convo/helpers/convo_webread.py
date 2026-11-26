"""Reads one public web page for the voice agent (`web_read`): its address after redirects, its title and its readable text.

The address is chosen by a language model and the request leaves from inside the user's network, so the page must be a public
one and that is checked here, not left to the caller:

- http and https only, no user name or password in the address, a host that is an ordinary public name or a public address.
  Names without a dot and names ending in `.local`, `.lan`, `.internal`, `.home.arpa`, `.ts.net` and the like are refused
  before anything is looked up.
- The name is looked up once and EVERY address it has must be public (`public`): loopback, private, link-local, carrier-grade
  NAT (100.64/10), unique-local, multicast, unspecified, reserved and documentation ranges are refused, and an IPv6 address
  that carries an IPv4 one (mapped, NAT64, 6to4) is judged by the address inside it.
- The request then connects to an address that was checked, never to the name again, so a second, different answer to the same
  lookup cannot move the request somewhere else. The Host header still names the page's host, and an https certificate is
  verified against that name.
- A redirect is not followed by the connection: its target comes back here and goes through the same checks, at most
  MAX_REDIRECTS times.
- At most MAX_BYTES are read, everything together takes at most `budget` seconds, and only `text/html` and `text/plain` are
  read. No cookies, no credentials and nothing remembered between requests; a generic User-Agent.

Every refusal is `Refused`, a ValueError whose message is one fixed sentence that may be said to the user: no address, no
server text and no exception text is ever in it. The page's text is untrusted data for whoever reads it next.

`read` does its blocking work in a thread. `fetch(url, timeout) -> (status, headers, body, final_url)` and
`resolve(host) -> [addresses]` replace the network in tests; a replaced `fetch` is still only ever given addresses that passed
the check, and where it says it ended (`final_url`) is checked as well."""
from __future__ import annotations

import asyncio
import html.parser
import http.client
import ipaddress
import re
import socket
import ssl
import time
import urllib.parse
import zlib

MAX_BYTES = 1_500_000        # most that is read of one page
MAX_REDIRECTS = 3            # redirects followed, each checked like the first address
BUDGET_SECONDS = 8.0         # one read, lookups, redirects and the page together
LIMIT = 6000                 # characters of text returned
CONNECT_SECONDS = 3.0        # one address may take this long to connect before the next checked one is tried
ADDRESS_CHARACTERS = 2000    # a longer address is refused
TITLE_CHARACTERS = 200
CHUNK = 16384
USER_AGENT = 'Mozilla/5.0 (compatible; page-reader)'
REDIRECTS = (301, 302, 303, 307, 308)
TEXT_TYPES = ('text/html', 'text/plain')
# Names that are never a public site, whatever they resolve to.
LOCAL_SUFFIXES = ('.local', '.lan', '.internal', '.home.arpa', '.ts.net', '.localhost', '.localdomain', '.home', '.corp',
                  '.intranet', '.private', '.test', '.invalid', '.onion')
NAME = re.compile(r'[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?)*\.?')
LAST_LABEL = re.compile(r'[a-z][a-z0-9-]*')      # a public name ends in letters: `127.1` and `0x7f.0.0.1` are not names
META_CHARSET = re.compile(rb'<meta[^>]{0,200}?charset\s*=\s*["\']?\s*([a-zA-Z0-9_.:-]{2,40})', re.I)
SAFE = "/%:@!$&'()*+,;=~-._"                      # characters of a path or query that are sent as they are

NOT_WEB = 'Only http and https web addresses can be read'
NOT_PUBLIC = 'Only public web pages can be read'
NOT_FOUND = 'That web address could not be found'
TOO_MANY = 'The page redirected too many times'
NOT_TEXT = 'That address is not a readable web page'
TIMED_OUT = 'The page did not answer in time'
FAILED = 'The page could not be read'
SITE_REFUSED = 'The site refused the request'
MISSING = 'The page was not found'
INSECURE = 'The site could not be reached securely'
EMPTY = 'The page has no readable text'

# Ranges that are not the public internet and that `ipaddress` does not all flag by itself in every Python version.
_BLOCKED = {
    4: [ipaddress.ip_network(net) for net in (
        '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24',
        '192.0.2.0/24', '192.88.99.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4',
        '240.0.0.0/4')],
    6: [ipaddress.ip_network(net) for net in (
        '::/96', '64:ff9b:1::/48', '100::/64', '2001::/23', '2001:db8::/32', 'fc00::/7', 'fe80::/10', 'fec0::/10', 'ff00::/8')],
}
_NAT64 = ipaddress.ip_network('64:ff9b::/96')

# What is left out of the readable text, with everything inside it.
SKIPPED = frozenset(('script', 'style', 'noscript', 'template', 'nav', 'header', 'footer', 'aside', 'form', 'svg', 'iframe',
                     'select', 'textarea', 'button', 'dialog', 'object', 'canvas'))
VOID = frozenset(('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'))
BLOCKS = frozenset(('address', 'article', 'blockquote', 'body', 'br', 'caption', 'dd', 'div', 'dl', 'dt', 'figcaption', 'figure',
                    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'li', 'main', 'ol', 'p', 'pre', 'section', 'table', 'tr', 'ul'))
CELLS = frozenset(('td', 'th'))


class Refused(ValueError):
    """The page was not read. The message is one fixed sentence that is safe to say to the user."""


def public(address) -> bool:
    """Whether one address is on the public internet. Anything that is not plainly public is not: an address that cannot be
    read, loopback, private, link-local, carrier-grade NAT, unique-local, multicast, unspecified, reserved, documentation and
    benchmark ranges. An IPv6 address that carries an IPv4 one (`::ffff:a.b.c.d`, NAT64, 6to4) is as public as that one."""
    try:
        ip = ipaddress.ip_address(str(address).strip().strip('[]').split('%', 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        inner = ip.ipv4_mapped or ip.sixtofour or (ipaddress.ip_address(int(ip) & 0xFFFFFFFF) if ip in _NAT64 else None)
        if inner is not None:
            return public(inner)
        if ip.is_site_local or ip.teredo:
            return False
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
            or any(ip in net for net in _BLOCKED[ip.version])):
        return False
    return bool(ip.is_global)


def _resolve(host):
    """The addresses a name has, as the system's resolver gives them."""
    return [info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


def check(url, resolve=None):
    """One address a model asked for, checked before anything is sent to it. Returns `(url, host, port, addresses)`: the
    address without its fragment, the host as it goes into the Host header, the port, and the public addresses the host has
    (the ones a request may connect to). Raises `Refused` for anything else: another scheme, credentials in the address, a
    host that is not a public name, a name that cannot be found, or a name with even one address that is not public."""
    url = str(url or '').strip()
    if not url or len(url) > ADDRESS_CHARACTERS or any(letter.isspace() or ord(letter) < 32 or letter == '\\' for letter in url):
        raise Refused(NOT_WEB)
    try:
        parts = urllib.parse.urlsplit(url)
        port, host = parts.port, (parts.hostname or '').lower()
    except ValueError:
        raise Refused(NOT_WEB) from None
    scheme = parts.scheme.lower()
    if scheme not in ('http', 'https') or not host:
        raise Refused(NOT_WEB)
    if '@' in parts.netloc or parts.username is not None or parts.password is not None:
        raise Refused(NOT_WEB)
    usual = 443 if scheme == 'https' else 80
    port = port or usual
    try:
        literal = ipaddress.ip_address(host.split('%', 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if '%' in host or not public(literal):
            raise Refused(NOT_PUBLIC)
        addresses = [str(literal)]
    else:
        try:
            host = host.encode('idna').decode('ascii').lower()
        except UnicodeError:
            raise Refused(NOT_WEB) from None
        if not NAME.fullmatch(host):
            raise Refused(NOT_WEB)
        name = host.rstrip('.')
        if '.' not in name or name.endswith(LOCAL_SUFFIXES) or not LAST_LABEL.fullmatch(name.rsplit('.', 1)[-1]):
            raise Refused(NOT_PUBLIC)
        try:
            found = (resolve or _resolve)(host)
        except Refused:
            raise
        except Exception:
            raise Refused(NOT_FOUND) from None
        addresses = list(dict.fromkeys(str(address).split('%', 1)[0] for address in (found or ())))
        if not addresses:
            raise Refused(NOT_FOUND)
        if not all(public(address) for address in addresses):
            # One private address among public ones is enough: which one a connection would get is not ours to guess.
            raise Refused(NOT_PUBLIC)
    netloc = ('[%s]' % host if ':' in host else host) + ('' if port == usual else ':%d' % port)
    return urllib.parse.urlunsplit((scheme, netloc, parts.path or '/', parts.query, '')), host, port, addresses


class _Pinned(http.client.HTTPConnection):
    """One connection to an address that was checked beforehand. `http.client` would look the host up again when it connects;
    this connects to the given addresses instead (the next one when one cannot be reached) and leaves the host name where it
    belongs: in the Host header and, for https, in the certificate check (`tls.wrap_socket(..., server_hostname=host)`)."""

    def __init__(self, host, port, addresses, deadline, *, tls=None, connect=socket.create_connection, clock=time.monotonic):
        super().__init__(host, port)
        self.default_port = 443 if tls is not None else 80      # the Host header names the port only when it is not the usual one
        self._addresses, self._deadline, self._tls, self._connect, self._clock = list(addresses), deadline, tls, connect, clock
        self.pinned = None

    def left(self):
        left = self._deadline - self._clock()
        if left <= 0:
            raise socket.timeout('out of time')
        return left

    def connect(self):
        failure = None
        for address in self._addresses:
            try:
                sock = self._connect((address, self.port), min(self.left(), CONNECT_SECONDS))
            except socket.timeout:
                raise
            except OSError as error:
                failure = error
                continue
            try:
                sock.settimeout(self.left())
                if self._tls is not None:
                    sock = self._tls.wrap_socket(sock, server_hostname=self.host)
            except BaseException:
                sock.close()
                raise
            self.sock = self.pinned = sock
            return
        raise failure or OSError('no address')


_verifying = []


def _verified():
    """The TLS settings every https page is read with: the system's certificate authorities, the chain and the host name
    both checked. Made once (reading the authorities takes a moment) and shared."""
    if not _verifying:
        _verifying.append(ssl.create_default_context())
    return _verifying[0]


def _fetch(url, timeout, addresses, *, connect=socket.create_connection, tls=None, clock=time.monotonic):
    """One GET of `url`, sent to one of `addresses` (already checked) and never anywhere else: a redirect is returned, not
    followed. Returns `(status, headers, body, url)`; the body is read only for a 2xx text answer and for at most
    MAX_BYTES + 1 bytes (the extra byte tells `read` that there was more)."""
    parts = urllib.parse.urlsplit(url)
    secure = parts.scheme == 'https'
    if secure and tls is None:
        tls = _verified()
    target = urllib.parse.quote(parts.path or '/', safe=SAFE) + ('?' + urllib.parse.quote(parts.query, safe=SAFE + '?') if parts.query else '')
    connection = _Pinned(parts.hostname, parts.port or (443 if secure else 80), addresses, clock() + max(0.0, float(timeout)),
                         tls=tls if secure else None, connect=connect, clock=clock)
    try:
        connection.request('GET', target, headers={'User-Agent': USER_AGENT, 'Accept': 'text/html, text/plain;q=0.9',
                                                   'Accept-Encoding': 'identity', 'Connection': 'close'})
        response = connection.getresponse()
        headers = {name.lower(): value for name, value in response.getheaders()}
        body = bytearray()
        kind = headers.get('content-type', '').split(';', 1)[0].strip().lower()
        if 200 <= response.status < 300 and kind in TEXT_TYPES:
            while len(body) <= MAX_BYTES:
                try:
                    connection.pinned.settimeout(connection.left())
                except OSError:
                    connection.left()       # the time check itself still applies
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(body)))
                if not chunk:
                    break
                body += chunk
        return response.status, headers, bytes(body), url
    finally:
        connection.close()


class _Text(html.parser.HTMLParser):
    """The title and the readable text of an HTML page: what is inside SKIPPED elements, or inside an element marked hidden,
    is left out. Elements are tracked as a stack so that a `<nav>` that is never closed ends with the element around it
    instead of swallowing the rest of the page; an end tag that matches nothing open is ignored."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.text, self.size = [], [], 0
        self._open, self._count, self._skipping, self._in_title, self._titled = [], {}, 0, False, False

    @staticmethod
    def _hidden(attrs):
        for name, value in attrs:
            value = (value or '').lower()
            if name == 'hidden' or (name == 'aria-hidden' and value == 'true'):
                return True
            if name == 'style' and re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', value):
                return True
        return False

    def _between(self, tag):
        if not self._skipping:
            self.text.append('\n' if tag in BLOCKS else ' ' if tag in CELLS else '')

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            self._between(tag)
            return
        first_title = tag == 'title' and not self._skipping and not self._titled
        skip = tag in SKIPPED or (tag == 'title' and not first_title) or self._hidden(attrs)
        self._open.append((tag, skip))
        self._count[tag] = self._count.get(tag, 0) + 1
        self._skipping += skip
        if first_title:
            self._in_title = self._titled = True
        else:
            self._between(tag)

    def handle_endtag(self, tag):
        if not self._count.get(tag):
            return
        while self._open:
            name, skip = self._open.pop()
            self._count[name] -= 1
            self._skipping -= skip
            if name == 'title':
                self._in_title = False
            if name == tag:
                break
        self._between(tag)

    def handle_data(self, data):
        data = data.replace('\n', ' ').replace('\r', ' ')      # a line break in the source is a space; only blocks break lines
        if self._in_title:
            self.title.append(data)
        elif not self._skipping:
            self.text.append(data)
            self.size += len(data)


def _clean(text):
    """Text with its whitespace collapsed: one space between words, one line break between blocks, no empty lines, and none
    of the characters that are not shown (controls, zero-width marks)."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = ''.join(letter if letter == '\n' else ' ' if letter.isspace() else letter if letter.isprintable() else '' for letter in text)
    lines = (' '.join(line.split()) for line in text.split('\n'))
    return '\n'.join(line for line in lines if line)


def readable(markup, limit=LIMIT, clock=None, deadline=None):
    """`(title, text, cut)` of an HTML page, the text not yet cut to `limit`. Parsing stops (`cut`) once there is plainly more
    text than `limit` asks for, or when `deadline` (on `clock`) has passed: a page built to be slow to parse costs its own
    budget, not more."""
    parser, enough, cut = _Text(), max(1, int(limit)) * 2 + 2000, False
    for start in range(0, len(markup), CHUNK):
        if parser.size > enough or (deadline is not None and clock() > deadline):
            cut = True
            break
        parser.feed(markup[start:start + CHUNK])
    else:
        parser.close()
    return _clean(''.join(parser.title)).replace('\n', ' ')[:TITLE_CHARACTERS], _clean(''.join(parser.text)), cut


def _inflate(body):
    """A gzip or deflate body, inflated to at most MAX_BYTES. `(bytes, whether there was more)`."""
    for bits in (47, -15):       # gzip or zlib with a header, then raw deflate
        try:
            inflater = zlib.decompressobj(bits)
            data = inflater.decompress(body, MAX_BYTES)
            return data, bool(inflater.unconsumed_tail)
        except zlib.error:
            continue
    raise Refused(FAILED)


def _decode(body, content_type):
    """The body as text: the charset the server names, else the one the page names in its first bytes, else UTF-8."""
    named = re.search(r'charset\s*=\s*["\']?\s*([a-zA-Z0-9_.:-]{2,40})', content_type or '', re.I)
    sniffed = None if named else META_CHARSET.search(body[:4096])
    charset = named.group(1) if named else sniffed.group(1).decode('ascii', 'replace') if sniffed else 'utf-8'
    if body.startswith(b'\xef\xbb\xbf'):
        charset = 'utf-8-sig'
    try:
        return body.decode(charset, 'replace')
    except (LookupError, ValueError):
        return body.decode('utf-8', 'replace')


def _page(status, headers, body, url, limit, clock, deadline):
    """The result for one final answer, or the refusal that fits it."""
    if status in (401, 403, 407, 429, 451):
        raise Refused(SITE_REFUSED)
    if status in (404, 410):
        raise Refused(MISSING)
    if not 200 <= status < 300:
        raise Refused(FAILED)
    content_type = headers.get('content-type', '')
    kind = content_type.split(';', 1)[0].strip().lower()
    if kind not in TEXT_TYPES:
        raise Refused(NOT_TEXT)
    body = bytes(body or b'')
    truncated, body = len(body) > MAX_BYTES, body[:MAX_BYTES]
    encoding = headers.get('content-encoding', '').strip().lower()
    if encoding in ('gzip', 'x-gzip', 'deflate'):
        body, more = _inflate(body)
        truncated = truncated or more
    elif encoding not in ('', 'identity'):
        raise Refused(FAILED)
    text = _decode(body, content_type)
    if kind == 'text/html':
        title, text, cut = readable(text, limit, clock, deadline)
        truncated = truncated or cut
    else:
        title, text = '', _clean(text)
    if not text:
        raise Refused(EMPTY)
    if len(text) > limit:
        text, truncated = text[:limit].rstrip(), True
    return {'url': url, 'title': title, 'text': text, 'truncated': truncated}


def _read(url, limit, budget, fetch, resolve, clock=time.monotonic):
    """The whole read, blocking: check, fetch, and the same again for each redirect, inside one deadline."""
    deadline = clock() + budget
    for _ in range(MAX_REDIRECTS + 1):
        url, _host, _port, addresses = check(url, resolve)
        left = deadline - clock()
        if left <= 0:
            raise Refused(TIMED_OUT)
        try:
            status, headers, body, final = fetch(url, left) if fetch is not None else _fetch(url, left, addresses)
            status, final = int(status), str(final or url)
            headers = {str(name).lower(): str(value) for name, value in dict(headers or {}).items()}
        except Refused:
            raise
        except (TimeoutError, socket.timeout):
            raise Refused(TIMED_OUT) from None
        except ssl.SSLError:
            raise Refused(INSECURE) from None
        except Exception:
            raise Refused(FAILED) from None
        if final != url:
            # A fetch that says it ended somewhere else followed a redirect by itself: that place must be public as well.
            final = check(final, resolve)[0]
        if status in REDIRECTS:
            target = headers.get('location', '').strip()
            if not target:
                raise Refused(FAILED)
            try:
                url = urllib.parse.urljoin(final, target)
            except ValueError:
                raise Refused(NOT_WEB) from None
            continue
        return _page(status, headers, body, final, limit, clock, deadline)
    raise Refused(TOO_MANY)


async def read(url, *, limit=LIMIT, budget=BUDGET_SECONDS, fetch=None, resolve=None) -> dict:
    """Reads one public web page (see the module text). Returns `{'url', 'title', 'text', 'truncated'}`: the address the page
    was finally read from, its title, its readable text without the title (at most `limit` characters; a plain-text page has no
    title) and whether anything was left out (the size cap, the parse stopping early, or `limit`). Raises `Refused` with a fixed
    sentence for everything else. Never runs on the event loop and never takes longer than `budget` seconds to return; a thread
    still waiting on a socket at that moment ends by its own deadline."""
    try:
        limit, budget = max(1, int(limit)), max(0.0, float(budget))
    except (TypeError, ValueError):
        raise Refused(FAILED) from None
    try:
        return await asyncio.wait_for(asyncio.to_thread(_read, str(url or '').strip(), limit, budget, fetch, resolve), budget)
    except (asyncio.TimeoutError, TimeoutError):
        raise Refused(TIMED_OUT) from None
