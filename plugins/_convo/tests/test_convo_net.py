"""How every role connection reaches its server (helpers/convo_net.py): a loopback address is connected to this computer when
Agent Zero runs in Docker, keeping the typed address as its identity; an https server on your own network is accepted by the
certificate remembered on first contact and refused when that certificate changes; a public server is never remembered; and a
connection that failed is described in Convo's own words, with the address that answers instead when there is one."""
import asyncio
import datetime
import http.server
import ipaddress
import json
import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from plugins._convo.helpers import convo_net as net

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:                                            # the certificate tests need it to make throwaway certificates
    x509 = None


def certificate(directory, name):
    """A throwaway self-signed certificate for localhost and 127.0.0.1 (made for this run only): (certificate path, key path)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'Convo test ' + name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = Path(directory) / (name + '.pem'), Path(directory) / (name + '.key')
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_path, key_path


class Models(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'data': [{'id': 'd1-omni-600m'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def https_server(cert_path, key_path):
    """A local https server answering GET with a model list: (server, port). Stopped by the caller."""
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Models)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def caused(error, cause):
    error.__cause__ = cause
    return error


class ThisComputer(unittest.TestCase):
    def setUp(self):
        net._alias.update(value=None, at=0.0)
        self.addCleanup(lambda: net._alias.update(value=None, at=0.0))

    def test_loopback_goes_to_the_docker_host_only_inside_docker_and_only_when_that_name_resolves(self):
        with patch.object(net, 'in_docker', return_value=True), patch.object(net.socket, 'getaddrinfo', return_value=[()]):
            for host in ('localhost', '127.0.0.1', '127.0.1.1', '::1', '[::1]', 'LOCALHOST'):
                self.assertEqual(net.connect_host(host), 'host.docker.internal', host)
            for host in ('host.docker.internal', '192.168.0.33', '100.75.98.97', 'desktop-i96ld4g', 'example.com'):
                self.assertEqual(net.connect_host(host), host, host)     # every other address is used as typed
            self.assertEqual(net.network(), {'in_docker': True, 'this_computer': 'host.docker.internal', 'translated': True})
        net._alias.update(value=None, at=0.0)
        with patch.object(net, 'in_docker', return_value=True), patch.object(net.socket, 'getaddrinfo', side_effect=socket.gaierror(-2, 'no')):
            self.assertEqual(net.connect_host('127.0.0.1'), '127.0.0.1')                 # Linux Docker without the name: untouched
            self.assertFalse(net.network()['translated'])
        with patch.object(net, 'in_docker', return_value=False):
            self.assertEqual(net.connect_host('127.0.0.1'), '127.0.0.1')                 # a native install needs nothing
            self.assertEqual(net.network(), {'in_docker': False, 'this_computer': '', 'translated': False})

    def test_the_transport_changes_only_where_it_connects(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={'data': []})
        async def get(url):
            async with net.client(8, httpx.MockTransport(handler)) as client:
                return (await client.get(url)).status_code
        with patch.object(net, 'in_docker', return_value=True), patch.object(net.socket, 'getaddrinfo', return_value=[()]):
            self.assertEqual(asyncio.run(get('https://127.0.0.1:18201/v1/models')), 200)
            self.assertEqual(asyncio.run(get('http://192.168.0.33:8890/v1/models')), 200)
        routed, direct = seen
        self.assertEqual((routed.url.host, routed.url.port, routed.url.path), ('host.docker.internal', 18201, '/v1/models'))
        self.assertEqual(routed.headers['Host'], '127.0.0.1:18201')                     # the typed address stays the identity
        self.assertEqual(routed.extensions.get('sni_hostname'), '127.0.0.1')            # and the certificate is checked against it
        self.assertEqual((direct.url.host, direct.headers['Host']), ('192.168.0.33', '192.168.0.33:8890'))
        self.assertNotIn('sni_hostname', direct.extensions)

    def test_urllib_requests_follow_the_same_rule(self):
        with patch.object(net, 'in_docker', return_value=True), patch.object(net.socket, 'getaddrinfo', return_value=[()]), \
                patch.object(net, 'load_pins', return_value={}):
            self.assertEqual(net.for_urllib('http://localhost:9931/v1/systemone')[:2], ('http://host.docker.internal:9931/v1/systemone', {'Host': 'localhost:9931'}))
            self.assertEqual(net.for_urllib('http://192.168.0.33:9931/v1/systemone'), ('http://192.168.0.33:9931/v1/systemone', {}, None))

    def test_convos_own_services_never_use_it(self):
        source = (Path(net.__file__).resolve().parent / 'convo_transport.py').read_text(encoding='utf-8')
        self.assertNotIn('convo_net', source)              # the managed gateway and Studio keep their own direct clients


@unittest.skipIf(x509 is None, 'cryptography is needed to make throwaway certificates')
class Certificates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        pins = Path(self.temp.name) / 'data' / net.PINS_FILE
        patcher = patch.object(net, 'pins_path', return_value=pins)
        patcher.start(); self.addCleanup(patcher.stop)
        self.first = certificate(self.temp.name, 'first')
        self.second = certificate(self.temp.name, 'second')

    def serve(self, which):
        server, port = https_server(*which)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return port

    async def get(self, url):
        async with net.client(8) as client:
            return (await client.get(url)).json()

    def test_a_private_certificate_is_remembered_on_first_contact_then_required(self):
        port = self.serve(self.first)
        url = 'https://127.0.0.1:' + str(port) + '/v1/models'
        self.assertEqual(asyncio.run(self.get(url)), {'data': [{'id': 'd1-omni-600m'}]})         # no button: it just works
        entry = net.pin_for(url)
        self.assertEqual(entry['subject'], 'Convo test first')
        self.assertIn('127.0.0.1', entry['names'])
        self.assertEqual(len(entry['sha256']), 95)
        self.assertNotIn('pem', net.public_pin(entry))
        self.assertEqual(asyncio.run(self.get('https://localhost:' + str(port) + '/v1/models'))['data'][0]['id'], 'd1-omni-600m')
        self.assertEqual(asyncio.run(self.get(url))['data'][0]['id'], 'd1-omni-600m')            # the remembered one is accepted

    def test_a_changed_certificate_is_refused_until_it_is_accepted(self):
        port = self.serve(self.first)
        url = 'https://127.0.0.1:' + str(port) + '/v1/models'
        asyncio.run(self.get(url))
        other = self.serve(self.second)
        moved = 'https://127.0.0.1:' + str(other) + '/v1/models'
        net._update_pins(lambda pins: pins.__setitem__(net.origin(moved), net.load_pins()[net.origin(url)]))   # the same server, new certificate
        with self.assertRaises(net.CertificateChanged) as raised:
            asyncio.run(self.get(moved))
        code, message = net.describe(raised.exception, moved)
        self.assertEqual(code, 'certificate_changed')
        self.assertIn('changed since Convo remembered it', message)
        self.assertEqual(raised.exception.current['subject'], 'Convo test second')
        net.remember(moved)                                                              # "Accept the new certificate"
        self.assertEqual(asyncio.run(self.get(moved))['data'][0]['id'], 'd1-omni-600m')
        net.forget(moved)
        self.assertIsNone(net.pin_for(moved))

    def test_only_https_on_your_own_network_is_ever_remembered(self):
        for url in ('https://127.0.0.1:1/v1', 'https://192.168.0.33:18201/v1', 'https://100.75.98.97:18201/v1', 'https://desktop:1/v1'):
            self.assertTrue(net.may_remember(url), url)
        for url in ('https://example.com/v1', 'https://8.8.8.8/v1', 'http://127.0.0.1:1/v1'):
            self.assertFalse(net.may_remember(url), url)
            with self.assertRaises(ValueError):
                net.remember(url)
        self.assertIn('not from a recognized issuer', net.describe(net.CertificateUntrusted('https://example.com/v1'), 'https://example.com/v1')[1])


class Failures(unittest.TestCase):
    def test_each_failure_is_said_in_convos_words(self):
        refused = caused(httpx.ConnectError('x'), ConnectionRefusedError(111, 'Connection refused'))
        code, here = net.describe(refused, 'http://127.0.0.1:8080/v1')
        self.assertEqual(code, 'refused')
        self.assertIn('Nothing is listening at 127.0.0.1:8080', here)
        self.assertIn('8890->8080 means 8890', here)                                    # this computer: the Docker port hint
        self.assertIn('not only 127.0.0.1', net.describe(refused, 'http://192.168.0.50:8098/v1')[1])
        unknown = caused(httpx.ConnectError('x'), socket.gaierror(-2, 'Name or service not known'))
        self.assertEqual(net.describe(unknown, 'http://desktop:8098/v1')[0], 'unknown_host')
        self.assertIn('within 8 s', net.describe(httpx.ReadTimeout('slow'), 'http://10.0.0.2:1/v1')[1])
        verify = caused(httpx.ConnectError('x'), ssl.SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate'))
        self.assertEqual(net.failure_code(verify), 'certificate_untrusted')
        self.assertEqual(net.failure_code(httpx.ConnectError('odd')), 'unreachable')

    def test_the_address_that_answers_instead_is_found_only_where_it_should_be(self):
        listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(1)
        self.addCleanup(listener.close)
        open_port = listener.getsockname()[1]
        found = asyncio.run(net.suggestion('http://127.0.0.1:1/v1', ('http://host.docker.internal:' + str(open_port) + '/v1',)))
        self.assertEqual((found['url'], found['label']), ('http://127.0.0.1:' + str(open_port) + '/v1', 'Use port ' + str(open_port)))
        self.assertIsNone(asyncio.run(net.suggestion('http://127.0.0.1:1/v1', ())))
        with patch.object(net, 'docker_alias', return_value='host.docker.internal'), patch.object(net, 'reachable', new=AsyncMock(return_value=True)):
            private = asyncio.run(net.suggestion('http://192.168.0.33:8890/v1'))
        self.assertEqual(private['url'], 'http://127.0.0.1:8890/v1')                    # a server listening only on 127.0.0.1 here
        self.assertIsNone(asyncio.run(net.suggestion('http://203.0.113.9:8890/v1')))     # nothing is looked for elsewhere


if __name__ == '__main__':
    unittest.main()
