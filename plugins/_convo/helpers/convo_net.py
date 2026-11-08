"""How Convo reaches a model, classifier or speech server from wherever Agent Zero runs, and through a private certificate.

Two rules, for every role connection (the conversation model, the classifier and its decision server, speech output). They are
never applied to Convo's own loopback services (the managed gateway, Studio and the managed runtime keep their own clients).

* This computer. Inside Docker, `localhost`, `127.x` and `::1` are the container itself. When Agent Zero runs in Docker
  (`/.dockerenv`) and `host.docker.internal` resolves, a role address on loopback is CONNECTED through `host.docker.internal`;
  the address as typed stays the identity (Host header, TLS server name, saved settings, key binding, evidence). The rule reads
  only the address's form and where Agent Zero runs at that moment: nothing per machine is learned or stored. An address typed
  as `host.docker.internal`, a LAN or Tailscale address, or any other name is used exactly as typed.
* Private certificates. An https server on your own network (`convo_contract.local_host`) whose certificate the built-in bundle
  does not trust is remembered the first time Convo connects to it (`data/tls-pins.json`, per origin as typed; a certificate is
  public, nothing secret is stored) and from then on only that exact certificate is accepted: it is the only trust anchor and no
  name is checked, so every address of that server works. A different certificate later is refused (`CertificateChanged`)
  until the user accepts it. A public server needs a certificate the bundle trusts; nothing public is ever remembered.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .convo_contract import local_host

DOCKER_HOST = 'host.docker.internal'
LOOPBACK_NAMES = {'localhost', 'localhost.localdomain', 'ip6-localhost', 'ip6-loopback'}
PINS_FILE = 'tls-pins.json'
PINS_LIMIT = 256 * 1024
PEM_LIMIT = 16 * 1024
_alias = {'value': None, 'at': 0.0}
_pins_lock = threading.Lock()


# ---- This computer.
def in_docker():
    return os.path.exists('/.dockerenv')


def docker_alias():
    """`host.docker.internal` when Agent Zero runs in Docker and that name resolves, else ''. Looked up at most once a minute."""
    if not in_docker():
        return ''
    now = time.monotonic()
    if _alias['value'] is None or now - _alias['at'] > 60:
        try:
            socket.getaddrinfo(DOCKER_HOST, None)
            _alias['value'] = DOCKER_HOST
        except OSError:
            _alias['value'] = ''
        _alias['at'] = now
    return _alias['value']


def is_loopback(host):
    host = (host or '').strip('[]').lower().rstrip('.')
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def this_computer(host):
    """Whether `host` means the computer Agent Zero runs on, as typed: loopback, or the Docker host's own name."""
    return is_loopback(host) or (host or '').lower().rstrip('.') == DOCKER_HOST


def connect_host(host):
    """Where a connection to `host` really goes: the Docker host for a loopback address inside Docker, else `host` itself."""
    return (docker_alias() or host) if is_loopback(host) else host


def network():
    """What the settings read tells the browser: whether loopback addresses are translated here, and to what."""
    alias = docker_alias()
    return {'in_docker': in_docker(), 'this_computer': alias, 'translated': bool(alias)}


# ---- Remembered certificates.
def origin(url):
    """`scheme://host:port` of an address, lowercased, the default port written out (the key a certificate is remembered under)."""
    parts = urlsplit(url)
    scheme, host = (parts.scheme or '').lower(), (parts.hostname or '').lower()
    port = parts.port or (443 if scheme == 'https' else 80)
    return scheme + '://' + ('[' + host + ']' if ':' in host else host) + ':' + str(port)


def pins_path():
    configured = os.environ.get('CONVO_DATA_DIR', '')
    root = Path(configured) if configured and os.path.isabs(configured) else Path(__file__).resolve().parent.parent / 'data'
    return root / PINS_FILE


def load_pins():
    try:
        path = pins_path()
        if path.stat().st_size > PINS_LIMIT:
            return {}
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, dict) and isinstance(value.get('pem'), str)} if isinstance(data, dict) else {}


def _update_pins(change):
    with _pins_lock:
        pins = load_pins()
        change(pins)
        path = pins_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
        temporary.write_text(json.dumps(pins, indent=1, sort_keys=True), encoding='utf-8')
        os.replace(temporary, path)


def pin_for(url):
    return load_pins().get(origin(url)) if (urlsplit(url).scheme or '').lower() == 'https' else None


def public_pin(entry):
    """A remembered certificate as the browser may see it (no PEM)."""
    return {key: entry.get(key) for key in ('sha256', 'names', 'subject', 'expires', 'remembered_at')} if entry else None


def certificate_info(der):
    """Fingerprint, names, subject and expiry of a DER certificate. Names need `cryptography` (in Agent Zero's environment);
    without it only the fingerprint is known."""
    digest = hashlib.sha256(der).hexdigest().upper()
    info = {'sha256': ':'.join(digest[i:i + 2] for i in range(0, len(digest), 2)), 'names': [], 'subject': '', 'expires': ''}
    try:
        from cryptography import x509
        certificate = x509.load_der_x509_certificate(der)
        try:
            alternatives = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            info['names'] = [str(name) for name in alternatives.get_values_for_type(x509.DNSName)] + \
                            [str(address) for address in alternatives.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:
            pass
        common = certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        info['subject'] = str(common[0].value)[:200] if common else ''
        expiry = getattr(certificate, 'not_valid_after_utc', None) or certificate.not_valid_after
        info['expires'] = expiry.strftime('%Y-%m-%d')
    except Exception:
        pass
    info['names'] = info['names'][:32]
    return info


def peek_certificate(url, timeout=5.0):
    """The certificate the server at `url` presents (a TLS handshake that trusts nothing and sends nothing): (pem, info)."""
    parts = urlsplit(url)
    host, port = parts.hostname or '', parts.port or 443
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((connect_host(host), port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as connection:
            der = connection.getpeercert(binary_form=True)
    if not der:
        raise ssl.SSLError('The server presented no certificate')
    return ssl.DER_cert_to_PEM_cert(der), certificate_info(der)


def may_remember(url):
    parts = urlsplit(url)
    return (parts.scheme or '').lower() == 'https' and local_host(parts.hostname or '')


def remember(url, timeout=5.0):
    """Remember the certificate `url`'s server presents now (own network, https only). Returns the public entry."""
    if not may_remember(url):
        raise ValueError('Only an https server on this computer or your network can have its certificate remembered.')
    pem, info = peek_certificate(url, timeout)
    if len(pem) > PEM_LIMIT:
        raise ValueError('The certificate is too large to remember.')
    entry = {**info, 'pem': pem, 'remembered_at': time.strftime('%Y-%m-%d')}
    _update_pins(lambda pins: pins.__setitem__(origin(url), entry))
    return public_pin(entry)


def forget(url):
    _update_pins(lambda pins: pins.pop(origin(url), None))


def pinned_context(entry):
    """Trust exactly the remembered certificate (as the only anchor, a leaf included) and check no name."""
    context = ssl.create_default_context(cadata=entry['pem'])
    context.check_hostname = False
    context.verify_flags |= getattr(ssl, 'VERIFY_X509_PARTIAL_CHAIN', 0)
    return context


# ---- Failures, in Convo's own words.
class CertificateChanged(httpx.ConnectError):
    def __init__(self, url, remembered, current, request=None):
        self.url, self.remembered, self.current = url, remembered, current
        super().__init__('The certificate at ' + origin(url) + ' changed since Convo remembered it', request=request)


class CertificateUntrusted(httpx.ConnectError):
    def __init__(self, url, request=None):
        self.url = url
        super().__init__('The certificate at ' + origin(url) + ' is not trusted', request=request)


def _chain(error):
    seen = []
    while error is not None and error not in seen and len(seen) < 12:
        seen.append(error)
        error = error.__cause__ or error.__context__
    return seen


def verify_failure(error):
    return any(isinstance(item, ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in str(item) for item in _chain(error)) \
        and not any(isinstance(item, (CertificateChanged, CertificateUntrusted)) for item in _chain(error))


def failure_code(error):
    """refused | unknown_host | timeout | certificate_changed | certificate_untrusted | unreachable"""
    chain = _chain(error)
    if any(isinstance(item, CertificateChanged) for item in chain):
        return 'certificate_changed'
    if any(isinstance(item, CertificateUntrusted) for item in chain) or verify_failure(error):
        return 'certificate_untrusted'
    if any(isinstance(item, (httpx.TimeoutException, socket.timeout, TimeoutError)) for item in chain):
        return 'timeout'
    if any(isinstance(item, socket.gaierror) for item in chain) or any(
            text in str(item) for item in chain for text in ('Name or service not known', 'No address associated', 'nodename nor servname', 'getaddrinfo failed', 'Temporary failure in name resolution')):
        return 'unknown_host'
    if any(isinstance(item, ConnectionRefusedError) or getattr(item, 'errno', None) in (errno.ECONNREFUSED, 10061) for item in chain) \
            or any('Connection refused' in str(item) or 'actively refused' in str(item) for item in chain):
        return 'refused'
    return 'unreachable'


def _where(url):
    parts = urlsplit(url)
    host = parts.hostname or url
    return (('[' + host + ']') if ':' in host else host) + (':' + str(parts.port) if parts.port else '')


def describe(error, url, seconds=8):
    """(code, message) for a connection that failed before any HTTP answer. Never quotes the server or the error text."""
    code, host, where = failure_code(error), urlsplit(url).hostname or '', _where(url)
    if code == 'refused':
        if this_computer(host):
            return code, ('Nothing is listening at ' + where + '. That is the computer running Agent Zero: if the server is on another '
                          'computer, use that computer’s LAN or Tailscale address; if it runs in Docker, use the port Docker publishes '
                          '(the number left of “->” in docker ps, so 8890->8080 means 8890).')
        return code, 'Nothing is listening at ' + where + '. Check that the server is running and listens on its network address, not only 127.0.0.1.'
    if code == 'unknown_host':
        return code, ('The name ' + host + ' could not be found from Agent Zero’s container. Use that computer’s IP address, or check the '
                      'name (a Tailscale name needs MagicDNS).')
    if code == 'timeout':
        return code, 'No answer from ' + where + ' within ' + str(seconds) + ' s: a firewall may be dropping the connection, or the server is busy (a model still loading).'
    if code == 'certificate_changed':
        changed = next(item for item in _chain(error) if isinstance(item, CertificateChanged))
        was, now = (changed.remembered or {}).get('sha256', ''), (changed.current or {}).get('sha256', '')
        return code, ('The certificate at ' + where + ' changed since Convo remembered it (was ' + was[:23] + '…' + (', now ' + now[:23] + '…' if now else '')
                      + '). If you replaced it, accept the new certificate; otherwise check what answers at that address.')
    if code == 'certificate_untrusted':
        if may_remember(url):
            return code, 'The certificate at ' + where + ' could not be read, so it was not remembered.'
        return code, (host + '’s certificate is not from a recognized issuer. A public server needs one; Convo remembers private '
                      'certificates only for servers on this computer or your network.')
    return code, 'Could not connect to ' + where + '. Check that the server is running and that Agent Zero’s container can reach it (LAN or Tailscale).'


async def reachable(host, port, timeout=1.5):
    """Whether something accepts a TCP connection at host:port (loopback goes to this computer as above). Sends nothing."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(connect_host(host), port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


def _with(url, host=None, port=None):
    parts = urlsplit(url)
    host = host or parts.hostname or ''
    netloc = ('[' + host + ']' if ':' in host else host) + (':' + str(port or parts.port) if (port or parts.port) else '')
    return urlunsplit((parts.scheme, netloc, parts.path, '', ''))


async def suggestion(url, defaults=()):
    """After a refused connection: the address that answers instead, or None. Only two places are tried, each with one TCP
    connection and nothing sent: the provider's own default port(s) on the same host (a container's internal port typed instead
    of the one Docker publishes), and, for a LAN or Tailscale address, this computer's private address on the same port (a server
    that listens only on 127.0.0.1 here)."""
    parts = urlsplit(url)
    host, port = parts.hostname or '', parts.port or (443 if parts.scheme == 'https' else 80)
    if this_computer(host):
        for default in defaults:
            other = urlsplit(default).port
            if other and other != port and await reachable(host, other):
                return {'url': _with(url, port=other), 'label': 'Use port ' + str(other),
                        'message': 'Something answers on port ' + str(other) + ' of this computer, the usual port for this server (the one Docker publishes).'}
        return None
    if local_host(host) and docker_alias() and await reachable(DOCKER_HOST, port):
        return {'url': _with(url, host='127.0.0.1'), 'label': 'Use 127.0.0.1:' + str(port),
                'message': 'This computer answers privately on port ' + str(port) + ': if ' + host + ' is this computer, its server listens only '
                           'on 127.0.0.1 here, and 127.0.0.1 is its address.'}
    return None


# ---- The transport every role connection uses.
def _route(request):
    host = request.url.host
    target = connect_host(host)
    if target != host:
        if request.url.scheme == 'https':
            request.extensions = {**request.extensions, 'sni_hostname': host}
        request.url = request.url.copy_with(host=target)       # the Host header keeps the address as typed


class RoleTransport(httpx.AsyncBaseTransport):
    """httpx transport for a role connection: loopback is connected to this computer inside Docker, and an https server on your
    own network is accepted by the certificate remembered for it (remembered on first contact). `inner` replaces the network
    (tests); `options` are httpx.AsyncHTTPTransport options."""

    def __init__(self, inner=None, **options):
        self.inner, self.options, self.pools = inner, options, {}

    def _pool(self, url):
        if self.inner is not None:
            return self.inner
        entry = pin_for(url)
        key = entry['sha256'] if entry else ''
        if key not in self.pools:
            self.pools[key] = httpx.AsyncHTTPTransport(verify=pinned_context(entry) if entry else True, **self.options)
        return self.pools[key]

    async def handle_async_request(self, request):
        typed = str(request.url)
        _route(request)
        try:
            return await self._pool(typed).handle_async_request(request)
        except httpx.ConnectError as error:
            if not verify_failure(error):
                raise
            entry = pin_for(typed)
            if entry is not None:
                try:
                    _, current = await asyncio.to_thread(peek_certificate, typed)
                except OSError:
                    current = None
                raise CertificateChanged(typed, public_pin(entry), current, request=request) from error
            if not may_remember(typed):
                raise CertificateUntrusted(typed, request=request) from error
            try:
                await asyncio.to_thread(remember, typed)
            except (OSError, ValueError) as failure:
                raise CertificateUntrusted(typed, request=request) from failure
            return await self._pool(typed).handle_async_request(request)

    async def aclose(self):
        for pool in self.pools.values():
            await pool.aclose()
        if self.inner is not None:
            await self.inner.aclose()


def client(timeout, transport=None, **options):
    """An httpx.AsyncClient for one role connection: no redirects, no environment proxies, Convo's routing and certificates."""
    return httpx.AsyncClient(transport=RoleTransport(transport), timeout=timeout, follow_redirects=False, trust_env=False, **options)


def for_urllib(url):
    """The same rules for a urllib request (the decision plugin): (address to open, extra headers, SSL context or None)."""
    parts = urlsplit(url)
    host, headers, context = parts.hostname or '', {}, None
    target = connect_host(host)
    entry = pin_for(url)
    if target != host:
        headers['Host'] = parts.netloc
        url = urlunsplit((parts.scheme, target + (':' + str(parts.port) if parts.port else ''), parts.path, parts.query, parts.fragment))
    if entry is not None:
        context = pinned_context(entry)
    elif target != host and parts.scheme == 'https':
        context = ssl.create_default_context()
        context.check_hostname = False          # the name is the typed one; a remembered certificate is checked instead
    return url, headers, context
