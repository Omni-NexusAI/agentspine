"""Registers a locally served d1-omni decision model as Convo's System One decision backend.

Development version of what System One's own provisioner will eventually do. It exists only so that Convo can use the decision
model before that exists, and it can serve nobody else: it registers one backend with Convo's registry (`register_backend`), with
a gate that admits the `convo` consumer only. It adds no Agent Zero extension on the chat, tool or model path.

The decision model runs outside this plugin (a server answering `POST /v1/systemone`, such as llama-server with d1-omni, text
and audio). Nothing here starts, stops or loads it. Where it is served is resolved for every call (`resolve_endpoint`) from Convo's
classifier choice: `decider.source = 'convo'` is Convo's own decision classifier (the server root Convo's classifier catalog
derives from its provider and API base, else `decider.url`, empty = the default), `'system_one'` uses System One's decider only
when its kind takes audio (System One's text-only kinds are refused, typed `text_only`), and `''`, or Convo switched off, is this
plugin's own config.json or the default. Every source obeys the classifier's locality rule: this machine and private networks as
they are, a public address only with Convo's classifier remote consent and over HTTPS (with Convo off there is no consent, so
public addresses are refused). A call to Convo's own decision classifier carries `Authorization: Bearer` when Convo resolves a
key for it (`convo_setup.classifier_bearer`, in-process, per call; never stored or reported here). A bad address never blocks
registration: the call is refused with a typed reason and Convo's turn is degraded. The two SDK files under helpers/core are
copies of System One's canonical files and are loaded only when their SHA-256 matches the pin below."""
from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import io
import ipaddress
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

BACKEND = 'd1_llama_cpp'
HERE = Path(__file__).resolve().parent
# The source this module was loaded from: api/provision.py reloads the module when the file on disk differs (a scoped update while
# the host keeps running), so an update never leaves the previous code serving decisions.
SOURCE_STAMP = Path(__file__).stat().st_mtime_ns
PINNED = {'decision.py': '87aaa5295647f7e91b0a02079192a633f7722e124f1f315a284cfa4fc862cb14',
          'media_decision.py': '22088754870e22f963df477c7a49ba056abff73ce81aff847e4fcaacd108f3b6'}    # System One canonical files, 2026-10-15
DEFAULT_URL = 'http://host.docker.internal:9931'
# This machine and Convo's containers by name; with loopback, private-network and tailnet addresses, one-label machine names and
# private-use name endings they are local (Convo's contract `local_host`, mirrored here so the rule also holds while Convo is off
# and never imported; the tests hold both to one table).
LOCAL_NAMES = {'host.docker.internal', 'localhost', 'convo-runtime', 'convo-audio', 'kokoro-worker'}
LOCAL_SUFFIXES = ('.local', '.lan', '.home.arpa', '.internal', '.localhost', '.ts.net')
TAILNET = ipaddress.ip_network('100.64.0.0/10')
LOCAL_LABEL = re.compile(r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?')
SOURCES = ('', 'system_one', 'convo')
LOCALITY_RULE = ("The classifier's decision server must be an http(s) address on this machine or your network (LAN or Tailscale), "
                 'with a valid port and no credentials, query or fragment')
REMOTE_RULE = ("A public decision server needs Convo's “Allow remote processing” for the classifier and HTTPS: it would receive "
               'the microphone audio')

_lock = threading.RLock()
_state = {'registration': None, 'calls': 0, 'last_ms': None, 'last_error': ''}
_resolved = {'key': None, 'value': None}       # the last resolution, and the settings fingerprint it was made from


class DeciderUnavailable(ValueError):
    """A decision call refused before anything was sent. `code`: text_only, remote, invalid_url, not_installed, not_configured,
    unknown, invalid or unavailable; `source`: the decider source it was resolved from. Nothing falls back to another server."""

    def __init__(self, code, message, source=''):
        super().__init__(message)
        self.code, self.source = code, source


def _host_prefix():
    return 'usr.plugins' if HERE.parent.parent.parent.name == 'usr' else 'plugins'


def _prefix():
    return _host_prefix() + '.' + HERE.parent.name


def local_host(host):
    """This machine, Convo's containers or your network: loopback, private (not link-local or unspecified) and tailnet
    addresses, Convo's names, a one-label machine name and the private-use endings. Judged as written; nothing is looked up."""
    host = (host or '').lower().rstrip('.')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host in LOCAL_NAMES or bool(LOCAL_LABEL.fullmatch(host)) or (host.endswith(LOCAL_SUFFIXES) and not host.startswith('.'))
    return address.is_loopback or (address.is_private and not address.is_link_local and not address.is_unspecified)         or (address.version == 4 and address in TAILNET)


def allowed_url(url, source='', remote=False, path=False):
    """`url` as a decision server base under the classifier's locality rule (Convo's `convo_contract.classifier_url`): this machine
    and private networks as they are (http or https); a public address only with Convo's classifier remote consent (`remote`) and
    over HTTPS; a valid port, no credentials, query or fragment, and no path unless `path` (a root derived from an API base). A
    public address without consent is refused `remote` (it would receive the microphone audio); anything else `invalid_url`. The
    same rule applies to every source."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or '').lower()
        parts.port                                     # a port that is not a number in range raises here
    except (ValueError, TypeError, AttributeError):
        raise DeciderUnavailable('invalid_url', LOCALITY_RULE, source) from None
    if parts.scheme not in ('http', 'https') or not host or parts.username or parts.password or parts.query or parts.fragment \
            or (not path and parts.path not in ('', '/')):
        raise DeciderUnavailable('invalid_url', LOCALITY_RULE, source)
    if not local_host(host):
        if remote is not True:
            raise DeciderUnavailable('remote', REMOTE_RULE, source)
        if parts.scheme != 'https':
            raise DeciderUnavailable('invalid_url', REMOTE_RULE, source)
    return url.rstrip('/')


def _own_config_path():
    return HERE.parent / 'config.json'


def base_url():
    """This plugin's own decision server (config.json `base_url`, else the default), accepted only when it is local: there is no
    classifier consent outside Convo, so a public address is refused."""
    url = DEFAULT_URL
    try:
        loaded = json.loads(_own_config_path().read_text(encoding='utf-8'))
        if isinstance(loaded, dict) and isinstance(loaded.get('base_url'), str):
            url = loaded['base_url']
    except (OSError, ValueError):
        pass
    return allowed_url(url)


def load_sdk():
    """The pinned media SDK, under this plugin's package so its relative import resolves to the pinned decision.py."""
    name = _prefix() + '.helpers.core.media_decision'
    if name in sys.modules:
        return sys.modules[name]
    for file, digest in PINNED.items():
        found = hashlib.sha256((HERE / 'core' / file).read_bytes()).hexdigest()
        if found != digest:
            raise RuntimeError('The pinned SDK file ' + file + ' changed (' + found[:12] + '); refusing to load it')
    importlib.import_module(_prefix() + '.helpers.core.decision')
    spec = importlib.util.spec_from_file_location(name, HERE / 'core' / 'media_decision.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _wav(audio):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(audio.channels)
        out.setsampwidth(2)
        out.setframerate(audio.sample_rate)
        out.writeframes(audio.pcm16)
    return buffer.getvalue()


# ---- where the decision server is: resolved per call, cached by the saved settings' file times

def _convo(name):
    return importlib.import_module(_host_prefix() + '._convo.helpers.' + name)


def convo_enabled():
    from helpers import plugins
    return plugins.get_toggle_state('_convo') != 'disabled'


def _system_one_config_path():
    from helpers import plugins
    return plugins.determine_plugin_asset_path('system_1', '', '', plugins.CONFIG_FILE_NAME)


def _stat(path):
    try:
        info = os.stat(path)
        return str(path), info.st_mtime_ns, info.st_size
    except OSError:
        return str(path), None, None


def _fingerprint(convo_on):
    """What a resolution depends on: whether Convo is on, and this plugin's, Convo's and System One's saved settings files (time and
    size). None when a path cannot be determined: then nothing is cached. With Convo off, Convo's modules are not imported."""
    try:
        paths = [_own_config_path()]
        if convo_on:
            paths += [_convo('setup_store').config_path(), _system_one_config_path()]
        return convo_on, tuple(_stat(path) for path in paths)
    except Exception:
        return None


def _convo_decider():
    """Convo's classifier choice, read through Convo's own settings accessor (validated): the `decider` fields, the decision server
    root Convo's classifier catalog derives (`root`, and whether a path prefix may be part of it) and the classifier's remote
    consent. No other setting, and none of the credentials that accessor attaches, stays in this module."""
    settings = _convo('convo_service').settings()
    decider = settings.get('decider') if isinstance(settings, dict) else None
    if not isinstance(decider, dict):
        return {}
    classifier = (settings.get('roles') or {}).get('classifier') or {}
    target = _convo('convo_classifier_catalog').decision_root(settings)
    return {**{key: decider.get(key) for key in ('source', 'kind', 'url', 'model')},
            'root': target['url'], 'path': target['path'], 'remote': classifier.get('remote') is True}


def _resolve(convo_on):
    """One resolution: {'source', 'base_url', 'host'}, or a refusal (code, message, source). Never raises."""
    source = ''
    try:
        if convo_on:
            decider = _convo_decider()
            source = decider.get('source') if decider.get('source') in SOURCES else ''
        if source == 'convo':
            root = decider.get('root', decider.get('url') or DEFAULT_URL)
            if not root:
                raise DeciderUnavailable('not_configured', "Convo's classifier has no decision server address", source)
            url = allowed_url(root, source, decider.get('remote') is True, decider.get('path') is True)
            model = decider.get('model') if isinstance(decider.get('model'), str) else ''
            return {'source': source, 'base_url': url, 'host': urllib.parse.urlsplit(url).netloc, 'model': model.strip()}
        elif source == 'system_one':
            consent = decider.get('remote') is True
            inherited = _convo('convo_system_one').inherited_decider(consent)
            verdict = inherited['audio']
            if verdict['state'] != 'audio':
                raise DeciderUnavailable(verdict['state'], "System One's decider cannot be used: " + verdict['label'], source)
            url = allowed_url(inherited['url'], source, consent)
        else:
            url = base_url()
        return {'source': source, 'base_url': url, 'host': urllib.parse.urlsplit(url).netloc}
    except DeciderUnavailable as error:
        return error.code, str(error), source
    except Exception as error:
        return 'unavailable', 'The decider settings could not be read (' + type(error).__name__ + ')', source


def resolve_endpoint():
    """Where the next decision call goes: {'source', 'base_url', 'host'}, or DeciderUnavailable with a typed reason. A changed
    setting (Convo's decider choice or address, System One's decider, this plugin's config.json) applies to the next call without
    registering again. With Convo off, Convo is never imported: this plugin's own address applies."""
    try:
        convo_on = convo_enabled()
    except Exception:
        convo_on = False
    key = _fingerprint(convo_on)
    with _lock:
        value = _resolved['value'] if key is not None and _resolved['key'] == key else None
    if value is None:
        value = _resolve(convo_on)        # outside the lock: it may read Convo's settings
        with _lock:
            _resolved.update(key=key, value=value)
    if isinstance(value, tuple):
        raise DeciderUnavailable(*value)
    return dict(value)


def describe():
    """The current resolution for status: source, host and base URL, or the typed refusal. Never raises."""
    try:
        return {**resolve_endpoint(), 'refused': '', 'detail': ''}
    except DeciderUnavailable as error:
        return {'source': error.source, 'base_url': '', 'host': '', 'refused': error.code, 'detail': str(error)}
    except Exception as error:
        return {'source': '', 'base_url': '', 'host': '', 'refused': 'unavailable', 'detail': type(error).__name__}


def _net():
    """Convo's connection rules (`helpers/convo_net.py`: this computer from inside Docker, remembered private certificates), or
    None without Convo: the address is then used exactly as given."""
    try:
        return _convo('convo_net')
    except Exception:
        return None


def _open(url, data, headers, timeout):
    """POST `data` to `url` under Convo's connection rules when Convo is installed. An https server on your own network whose
    certificate is not trusted yet is remembered on the first call (as Convo's own clients do), then asked once more."""
    net = _net() if convo_enabled_safely() else None
    for attempt in (0, 1):
        target, extra, context = net.for_urllib(url) if net else (url, {}, None)
        request = urllib.request.Request(target, data=data, headers={**headers, **extra}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=timeout, **({'context': context} if context is not None else {})) as response:
                return json.loads(response.read())
        except urllib.error.URLError as error:
            if attempt or not net or isinstance(error, urllib.error.HTTPError) or not net.verify_failure(error)                     or net.pin_for(url) is not None or not net.may_remember(url):
                raise
            net.remember(url)


def convo_enabled_safely():
    try:
        return convo_enabled()
    except Exception:
        return False


def classifier_bearer():
    """The Authorization value for Convo's own decision classifier ('' when none resolves), from Convo in-process for this one
    call (`convo_setup.classifier_bearer` over Convo's settings accessor). Never stored, cached, logged or reported."""
    try:
        return _convo('convo_setup').classifier_bearer(_convo('convo_service').settings()) or ''
    except Exception:
        return ''


class D1HttpModel:
    """The `system_one(state, questions, images=, audio=)` of a decision model, answered by a server's /v1/systemone (llama-server
    with d1-omni). The server is `url` when one is given, otherwise whatever `resolve` (default `resolve_endpoint`) answers at the
    moment of each call; a call to Convo's own decision classifier (source `convo`) carries `bearer()` when it is not empty."""

    def __init__(self, url=None, timeout=20.0, resolve=None, bearer=None):
        self.fixed = allowed_url(url) if url is not None else None
        self.resolve = resolve or resolve_endpoint
        self.bearer = bearer or classifier_bearer
        self.timeout = timeout

    def system_one(self, state, questions, images=None, audio=None):
        if images:
            raise ValueError('This provisioner carries text and audio only')
        try:
            where = None if self.fixed else self.resolve()
            base = self.fixed or where['base_url']
        except DeciderUnavailable as error:
            _state['last_error'] = 'refused: ' + error.code
            raise
        headers = {'Content-Type': 'application/json'}
        if where and where.get('source') == 'convo':
            authorization = self.bearer()
            if authorization:
                headers['Authorization'] = authorization
        body = {'state': state, 'questions': questions}
        if where and where.get('model'):
            body['model'] = where['model']          # a router serving several decision models answers with the one named
        if audio is not None:
            body['files'] = ['data:audio/wav;base64,' + base64.b64encode(_wav(audio)).decode()]
        started = time.perf_counter()
        try:
            result = _open(base + '/v1/systemone', json.dumps(body).encode(), headers, self.timeout)
        except urllib.error.HTTPError as error:
            _state['last_error'] = 'HTTP %s from the decision server' % error.code
            raise
        except Exception as error:
            _state['last_error'] = type(error).__name__
            raise
        with _lock:
            _state['calls'] += 1
            _state['last_ms'] = round((time.perf_counter() - started) * 1000, 1)
        return {'answers': result['answers']}


def consumer_config():
    """The gate System One's consumer client checks on every call: this plugin's own switch, and Convo as the only consumer."""
    from helpers import plugins
    return {'enabled': plugins.get_toggle_state('convo_d1_decider') != 'disabled', 'consumers': {'convo': {'enabled': True}}}


def ensure_registered(only_with_convo=False):
    """Idempotent. Registers the backend with Convo unless this plugin already did. The agent-start hook asks for `only_with_convo`:
    with Convo off the host must not so much as import Convo's modules on this plugin's account. No address is needed to register:
    each call resolves its own (a bad one fails that call, typed, never the registration)."""
    with _lock:
        if _state['registration'] is None and not (only_with_convo and not convo_enabled()):
            sdk = load_sdk()
            client = sdk.LiquidD1Client(D1HttpModel(resolve=resolve_endpoint), audio_converter=lambda audio: audio)
            _state.update(calls=0, last_ms=None, last_error='')
            _state['registration'] = _convo('convo_system_one').register_backend(BACKEND, client, sdk=sdk, config_provider=consumer_config, remote=False)
    return status()


def unregister():
    with _lock:
        registration = _state['registration']
        removed = bool(registration) and _convo('convo_system_one').unregister_backend(BACKEND, registration)
        _state['registration'] = None
    return {**status(), 'removed': removed}


def status():
    """Counts, last latency and the last error class, plus where the next call would go (source, host and base URL) or why it would
    be refused. Never contacts the decision server."""
    where = describe()
    with _lock:
        return {'backend': BACKEND, 'registered_here': _state['registration'] is not None, 'source': where['source'], 'host': where['host'],
                'base_url': where['base_url'], 'refused': where['refused'], 'refused_detail': where['detail'], 'calls': _state['calls'],
                'last_ms': _state['last_ms'], 'last_error': _state['last_error']}
