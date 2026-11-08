"""The decision-backend provisioner: pinned SDK, the classifier's locality rule (consent for public addresses), Convo as the only
consumer, idempotent registration, the decision server resolved for every call from Convo's classifier choice (`decider.source`,
the root derived from roles.classifier), and Convo's classifier key sent as a Bearer, all read through Convo's own settings
accessor on a stubbed host."""
import asyncio
import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1]))

from plugins.convo_d1_decider.helpers import provisioner  # noqa: E402
from plugins._convo.helpers import convo_contract  # noqa: E402
from plugins._convo.helpers import convo_system_one as convo_one  # noqa: E402

D1_TEXT = {'backend': 'openrouter', 'provider': 'openrouter', 'model': 'typesafe/jev-latest', 'endpoint': 'https://api.typesafe.ai/v1/systemone', 'context_window': 32000}
# The same table as plugins/_convo/tests/test_classifier_catalog.py: the conversation model's rule. This machine, Convo's containers
# and private networks as they are (http or https); a public address only with the classifier's consent and over HTTPS.
GOOD = ('http://host.docker.internal:9931', 'http://localhost:9931/', 'http://127.0.0.1:1', 'http://[::1]:9931', 'http://LOCALHOST:9931',
        'http://192.168.0.178:9931', 'http://10.0.0.2', 'https://localhost:9931', 'https://172.16.0.4:8443', 'http://convo-runtime:8090',
        'http://100.101.102.103:9931', 'http://my-desktop.tail1234.ts.net:9931', 'http://desktop:9931', 'http://nas.local:9931')
PUBLIC_HTTPS = ('https://api.typesafe.ai', 'https://example.com:9931', 'https://localhost.evil.com:1', 'https://8.8.8.8')
PUBLIC_HTTP = ('http://example.com:9931', 'http://localhost.evil.com:1', 'http://0.0.0.0:9931', 'http://169.254.0.5:9931', 'http://100.128.0.1:9931')
INVALID = ('http://user:pw@localhost:1', 'http://localhost:1/x', 'http://localhost:1/?a=b', 'ftp://localhost:1', 'ftp://example.com',
           'http://localhost:1#top', 'http://localhost:abc', 'http://localhost:99999', 'localhost:9931', '')


class Answer:
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def read(self): return json.dumps({'answers': {'attention': {'type': 'choice', 'choice': 'a', 'confidence': .9, 'probabilities': {'a': .9, 'b': .1}}}}).encode()


class Base(unittest.TestCase):
    """A stubbed host: plugin toggles, plugin settings files in a temporary folder (Convo's, System One's), an empty key store."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix='d1-provisioner-'))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.toggle, self.convo, self.system_one_installed = 'enabled', 'enabled', True
        self.clock = 1_700_000_000
        runtime = types.ModuleType('helpers.runtime')
        helpers = types.ModuleType('helpers'); helpers.__path__ = []; helpers.runtime = runtime
        plugins = types.ModuleType('helpers.plugins')
        plugins.CONFIG_FILE_NAME = 'config.json'
        plugins.get_toggle_state = lambda name: {'convo_d1_decider': self.toggle, '_convo': self.convo}.get(name, 'enabled' if name == 'system_1' and self.system_one_installed else 'disabled')
        plugins.get_plugin_meta = lambda name: None if name == 'system_1' and not self.system_one_installed else {'name': name}
        plugins.determine_plugin_asset_path = lambda name, project, profile, *parts: str(self.folder.joinpath(name, *parts))
        plugins.get_plugin_config = lambda name, *args, **kwargs: self.read(name)
        secrets = types.ModuleType('helpers.secrets')
        self.keys = {}                                                                 # Convo's key store (connections.env)
        secrets.SecretsManager = type('SecretsManager', (), {'get_instance': staticmethod(lambda path: secrets.SecretsManager()),
                                                             'load_secrets': lambda manager: dict(self.keys)})
        helpers.plugins, helpers.secrets = plugins, secrets
        host = patch.dict(sys.modules, {'helpers': helpers, 'helpers.runtime': runtime, 'helpers.plugins': plugins, 'helpers.secrets': secrets})
        host.start(); self.addCleanup(host.stop)
        own = patch.object(provisioner, '_own_config_path', lambda: self.folder / 'convo_d1_decider' / 'config.json')
        own.start(); self.addCleanup(own.stop)
        provisioner._state.update(registration=None, calls=0, last_ms=None, last_error='')
        provisioner._resolved.update(key=None, value=None)
        self.addCleanup(provisioner.unregister)

    def read(self, name):
        path = self.folder / name / 'config.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None

    def write(self, name, value):
        """Save a plugin's settings file with a strictly later modification time, as a real save would."""
        path = self.folder / name / 'config.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        self.clock += 10
        os.utime(path, ns=(self.clock * 10**9, self.clock * 10**9))

    def decider(self, source, url='', classifier=None, **extra):
        roles = {'roles': {'classifier': classifier}} if classifier else {}
        self.write('_convo', {'convo': {'enabled': True, 'system_one': {'enabled': True, 'backend': 'd1_llama_cpp'},
                                        'decider': {'source': source, 'kind': 'd1_llama_cpp', 'url': url, 'model': ''}, **roles, **extra}})


class UrlTests(Base):
    def url(self, text):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'config.json').write_text(json.dumps({'base_url': text}))
            with patch.object(provisioner, '_own_config_path', lambda: Path(folder) / 'config.json'):
                return provisioner.base_url()

    def test_the_default_is_the_host_machine(self):
        self.assertEqual(provisioner.base_url(), 'http://host.docker.internal:9931')

    def code(self, url, remote=False, path=False):
        try:
            provisioner.allowed_url(url, remote=remote, path=path)
            return None
        except provisioner.DeciderUnavailable as error:
            return error.code

    def test_local_and_private_addresses_are_accepted_and_a_public_one_needs_consent_and_https(self):
        for good in GOOD:
            self.assertTrue(self.url(good), good)                                # this plugin's own address: no consent exists here
        for bad in PUBLIC_HTTPS + PUBLIC_HTTP + INVALID:
            with self.assertRaises(ValueError, msg=bad):
                self.url(bad)
        for url in PUBLIC_HTTPS + PUBLIC_HTTP:
            self.assertEqual(self.code(url), 'remote', url)                       # named as such: it would receive the audio
        for url in PUBLIC_HTTPS:
            self.assertIsNone(self.code(url, remote=True), url)                   # with the classifier's consent, over HTTPS
        for url in PUBLIC_HTTP:
            self.assertEqual(self.code(url, remote=True), 'invalid_url', url)     # never plain http to a public address
        for url in INVALID:
            self.assertEqual(self.code(url, remote=True), 'invalid_url', url)
        self.assertIsNone(self.code('http://localhost:1/x', path=True))           # a root derived from an API base may carry a path
        self.assertEqual(self.code('http://localhost:1/x?a=b', path=True), 'invalid_url')
        self.assertEqual(self.code('http://localhost:1', remote='yes'), None)
        self.assertEqual(self.code('https://example.com', remote='yes'), 'remote')  # consent is exactly true

    def test_the_default_and_the_rule_agree_with_convo(self):
        self.assertEqual(provisioner.DEFAULT_URL, convo_one.DEFAULT_DECIDER_URL)
        self.assertEqual(provisioner.LOCAL_NAMES, convo_contract.LOCAL_NAMES)
        self.assertEqual(provisioner.LOCAL_SUFFIXES, convo_contract.LOCAL_SUFFIXES)
        cases = json.loads((ROOT.parent / '_convo' / 'tests' / 'fixtures' / 'connection-rules-cases.json').read_text(encoding='utf-8'))
        for host in cases['local_hosts'] + cases['public_hosts']:
            self.assertEqual(provisioner.local_host(host), convo_contract.local_host(host), host)   # one table for both copies
        for url in GOOD + PUBLIC_HTTPS + PUBLIC_HTTP + INVALID + ('http://localhost:1/x',):
            for remote in (False, True):
                for path in (False, True):
                    refusal = convo_one.url_refusal(url, remote, path)
                    mine = self.code(url, remote, path)
                    self.assertEqual(refusal[0] if refusal else None, {'invalid_url': 'invalid'}.get(mine, mine), (url, remote, path))


class SdkTests(Base):
    def test_the_pinned_files_match_system_ones_canonical_hashes(self):
        for file, digest in provisioner.PINNED.items():
            self.assertEqual(hashlib.sha256((ROOT / 'helpers' / 'core' / file).read_bytes()).hexdigest(), digest)

    def test_a_changed_sdk_file_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / 'helpers'
            shutil.copytree(ROOT / 'helpers', copy)
            (copy / 'core' / 'media_decision.py').write_text('# tampered\n', encoding='utf-8')
            with patch.object(provisioner, 'HERE', copy):
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    provisioner.load_sdk()


class RegistrationTests(Base):
    def test_registration_is_idempotent_and_visible_to_convo(self):
        first = provisioner.ensure_registered()
        again = provisioner.ensure_registered()
        self.assertTrue(first['registered_here'] and again['registered_here'])
        self.assertEqual([item['id'] for item in convo_one.registered_backends()].count('d1_llama_cpp'), 1)

    def test_the_agent_start_hook_registers_nothing_while_convo_is_off(self):
        self.convo = 'disabled'
        self.assertFalse(provisioner.ensure_registered(only_with_convo=True)['registered_here'])
        self.assertEqual(convo_one.registered_backends(), [])
        self.convo = 'enabled'
        self.assertTrue(provisioner.ensure_registered(only_with_convo=True)['registered_here'])
        self.assertTrue(provisioner.ensure_registered()['registered_here'])      # an explicit request still works

    def test_unregister_leaves_nothing_behind(self):
        provisioner.ensure_registered()
        self.assertTrue(provisioner.unregister()['removed'])
        self.assertEqual([item['id'] for item in convo_one.registered_backends()], [])
        self.assertFalse(provisioner.unregister()['removed'])

    def test_the_consumer_gate_follows_the_plugin_switch_and_names_convo_only(self):
        config = provisioner.consumer_config()
        self.assertTrue(config['enabled'])
        self.assertEqual(config['consumers'], {'convo': {'enabled': True}})
        self.toggle = 'disabled'
        self.assertFalse(provisioner.consumer_config()['enabled'])

    def test_a_bad_address_never_blocks_registration_and_status_names_the_refusal(self):
        self.write('convo_d1_decider', {'base_url': 'https://example.com:9931'})
        with patch('urllib.request.urlopen', side_effect=AssertionError('nothing may be sent')):
            state = provisioner.ensure_registered()
            self.assertTrue(state['registered_here'])
            self.assertEqual((state['source'], state['refused'], state['host']), ('', 'remote', ''))
            with self.assertRaises(provisioner.DeciderUnavailable) as caught:
                provisioner.D1HttpModel().system_one('s', {'attention': {}})
        self.assertEqual(caught.exception.code, 'remote')
        self.assertEqual(provisioner.status()['last_error'], 'refused: remote')


class SourceTests(Base):
    def test_unset_source_uses_this_plugins_own_address_or_the_default(self):
        self.decider('')
        self.assertEqual(provisioner.resolve_endpoint(), {'source': '', 'base_url': 'http://host.docker.internal:9931', 'host': 'host.docker.internal:9931'})
        self.write('convo_d1_decider', {'base_url': 'http://127.0.0.1:9935'})
        self.assertEqual(provisioner.resolve_endpoint()['base_url'], 'http://127.0.0.1:9935')

    def test_convo_source_uses_convos_own_address_and_its_default(self):
        self.decider('convo', 'http://localhost:9940')
        self.assertEqual(provisioner.resolve_endpoint(), {'source': 'convo', 'base_url': 'http://localhost:9940', 'host': 'localhost:9940', 'model': ''})
        self.decider('convo', '')
        self.assertEqual(provisioner.resolve_endpoint()['base_url'], 'http://host.docker.internal:9931')
        self.write('convo_d1_decider', {'base_url': 'http://127.0.0.1:9935'})                       # this plugin's own address is not the source now
        self.assertEqual(provisioner.resolve_endpoint()['base_url'], 'http://host.docker.internal:9931')

    def test_system_one_source_is_refused_typed_for_every_text_only_kind(self):
        self.decider('system_one')
        for kind in ('jev', 'openrouter', 'llama_decision', 'chat'):
            self.write('system_1', {'decider': {**D1_TEXT, 'backend': kind}})
            with self.assertRaises(provisioner.DeciderUnavailable) as caught:
                provisioner.resolve_endpoint()
            self.assertEqual((caught.exception.code, caught.exception.source), ('text_only', 'system_one'), kind)
            self.assertIn('Decides from text', str(caught.exception))

    def test_system_one_source_uses_an_audio_kind_locally_and_a_public_one_only_with_consent(self):
        self.decider('system_one')
        self.write('system_1', {'decider': {'backend': 'd1_llama_cpp', 'model': 'd1', 'endpoint': 'http://host.docker.internal:9944/v1/systemone'}})
        self.assertEqual(provisioner.resolve_endpoint(), {'source': 'system_one', 'base_url': 'http://host.docker.internal:9944', 'host': 'host.docker.internal:9944'})
        self.write('system_1', {'decider': {'backend': 'd1_llama_cpp', 'model': 'd1', 'endpoint': 'http://192.168.0.178:9944/v1/systemone'}})
        self.assertEqual(provisioner.resolve_endpoint()['base_url'], 'http://192.168.0.178:9944')         # a private network is local
        self.write('system_1', {'decider': {'backend': 'd1_llama_cpp', 'model': 'd1', 'endpoint': 'https://d1.example.com/v1/systemone'}})
        with self.assertRaises(provisioner.DeciderUnavailable) as caught:
            provisioner.resolve_endpoint()
        self.assertEqual(caught.exception.code, 'remote')
        self.decider('system_one', classifier={'remote': True})                                            # Convo's classifier consent
        self.assertEqual(provisioner.resolve_endpoint()['base_url'], 'https://d1.example.com')
        self.system_one_installed = False
        self.write('system_1', {'decider': D1_TEXT})
        with self.assertRaises(provisioner.DeciderUnavailable) as caught:
            provisioner.resolve_endpoint()
        self.assertEqual(caught.exception.code, 'not_installed')

    def test_the_same_rule_applies_to_every_source_even_if_a_setting_slipped_past_validation(self):
        for decider, code in (({'source': 'convo', 'url': 'https://example.com:9931'}, 'remote'),
                              ({'source': 'convo', 'url': 'http://example.com:9931', 'remote': True}, 'invalid_url'),
                              ({'source': 'convo', 'root': '', 'path': True}, 'not_configured')):
            provisioner._resolved.update(key=None, value=None)
            with patch.object(provisioner, '_convo_decider', return_value=decider):
                with self.assertRaises(provisioner.DeciderUnavailable) as caught:
                    provisioner.resolve_endpoint()
            self.assertEqual((caught.exception.code, caught.exception.source), (code, 'convo'), decider)

    def test_convo_source_uses_the_root_its_classifier_catalog_derives_from_the_provider(self):
        local = {'provider': 'llama_cpp', 'kind': 'decision', 'api_base': 'http://192.168.1.5:9931/v1'}
        self.decider('convo', classifier=local)                         # decider.url stays '' (the previous rule could not hold it)
        self.assertEqual(provisioner.resolve_endpoint(), {'source': 'convo', 'base_url': 'http://192.168.1.5:9931', 'host': '192.168.1.5:9931', 'model': ''})
        self.decider('convo', classifier={'provider': 'other', 'kind': 'decision', 'api_base': 'http://localhost:7000/prefix/v1'})
        self.assertEqual(provisioner.resolve_endpoint()['base_url'], 'http://localhost:7000/prefix')     # a path prefix from an API base
        self.decider('convo', classifier={'provider': 'jev'})                                            # public, no consent
        with self.assertRaises(provisioner.DeciderUnavailable) as caught:
            provisioner.resolve_endpoint()
        self.assertEqual(caught.exception.code, 'remote')
        self.decider('convo', classifier={'provider': 'jev', 'remote': True})
        self.assertEqual(provisioner.resolve_endpoint()['base_url'], 'https://api.typesafe.ai')
        self.decider('convo', classifier={'provider': 'unsloth_studio', 'kind': 'decision'})             # no address: never the default
        with self.assertRaises(provisioner.DeciderUnavailable) as caught:
            provisioner.resolve_endpoint()
        self.assertEqual(caught.exception.code, 'not_configured')

    def test_unreadable_convo_settings_refuse_the_call_typed(self):
        self.write('_convo', {'convo': {'echo_guard': 'loud'}})                                      # fails Convo's own validation
        with self.assertRaises(provisioner.DeciderUnavailable) as caught:
            provisioner.resolve_endpoint()
        self.assertEqual(caught.exception.code, 'unavailable')

    def test_with_convo_off_convo_is_never_imported_and_this_plugins_address_applies(self):
        self.decider('convo', 'http://localhost:9940')
        self.convo = 'disabled'
        with patch.object(provisioner, '_convo', side_effect=AssertionError('imported Convo while it is off')):
            self.assertEqual(provisioner.resolve_endpoint()['base_url'], 'http://host.docker.internal:9931')
            state = provisioner.status()
            self.assertEqual((state['source'], state['host'], state['registered_here']), ('', 'host.docker.internal:9931', False))
            self.assertFalse(provisioner.ensure_registered(only_with_convo=True)['registered_here'])

    def test_status_reports_the_source_and_host_without_contacting_the_server(self):
        self.decider('convo', 'http://localhost:9940')
        with patch('urllib.request.urlopen', side_effect=AssertionError('status never calls the server')):
            state = provisioner.ensure_registered()
        self.assertEqual((state['source'], state['host'], state['base_url'], state['refused']), ('convo', 'localhost:9940', 'http://localhost:9940', ''))
        self.decider('system_one')
        self.write('system_1', {'decider': D1_TEXT})
        state = provisioner.status()
        self.assertEqual((state['source'], state['refused'], state['host']), ('system_one', 'text_only', ''))
        self.assertNotIn('typesafe', json.dumps(state))                                              # no address of System One's leaks into status


class ResolutionTests(Base):
    def settings_reads(self):
        service = provisioner._convo('convo_service')          # the module the provisioner reads through, not a stale package attribute
        return patch.object(service, 'settings', wraps=service.settings)

    def test_settings_are_read_again_only_when_a_saved_file_changes(self):
        self.decider('convo', 'http://localhost:9940')
        with self.settings_reads() as reads:
            for _ in range(3):
                provisioner.resolve_endpoint()
            self.assertEqual(reads.call_count, 1)
            self.decider('convo', 'http://localhost:9941')
            self.assertEqual(provisioner.resolve_endpoint()['base_url'], 'http://localhost:9941')
            self.assertEqual(reads.call_count, 2)
            self.write('system_1', {'decider': D1_TEXT})                                              # another file it depends on
            provisioner.resolve_endpoint()
            self.assertEqual(reads.call_count, 3)

    def test_an_address_change_takes_effect_on_the_next_call_without_registering_again(self):
        self.decider('convo', 'http://localhost:9940')
        provisioner.ensure_registered()
        registration = provisioner._state['registration']
        sdk = provisioner.load_sdk()
        request = sdk.DecisionRequest({'attention': {'type': 'choice', 'instructions': 'Who?', 'criteria': {'a': 'A', 'b': 'B'}}},
                                      text='state', audio=sdk.AudioInput(b'\x01\x00' * 160, 16000, 1))
        seen = []

        def urlopen(request, timeout):
            seen.append(request.full_url)
            return Answer()

        with patch('urllib.request.urlopen', urlopen):
            asyncio.run(registration['consumer'].evaluate_request(request))
            self.decider('convo', 'http://127.0.0.1:9941')
            answers = asyncio.run(registration['consumer'].evaluate_request(request))
        self.assertEqual(seen, ['http://localhost:9940/v1/systemone', 'http://127.0.0.1:9941/v1/systemone'])
        self.assertEqual(answers['attention'].value, 'a')
        self.assertIs(provisioner._state['registration'], registration)
        self.assertEqual([item['id'] for item in convo_one.registered_backends()], ['d1_llama_cpp'])
        self.assertEqual(provisioner.status()['calls'], 2)

    def test_a_refused_call_degrades_through_the_sdk_and_sends_nothing(self):
        self.decider('system_one')
        self.write('system_1', {'decider': D1_TEXT})
        provisioner.ensure_registered()
        sdk = provisioner.load_sdk()
        request = sdk.DecisionRequest({'attention': {'type': 'choice', 'instructions': 'Who?', 'criteria': {'a': 'A', 'b': 'B'}}}, text='state')
        with patch('urllib.request.urlopen', side_effect=AssertionError('nothing may be sent')):
            with self.assertRaises(sdk.DecisionError) as caught:
                asyncio.run(provisioner._state['registration']['consumer'].evaluate_request(request))
        self.assertIsInstance(caught.exception.__cause__, provisioner.DeciderUnavailable)
        self.assertEqual(caught.exception.__cause__.code, 'text_only')
        self.assertEqual(provisioner.status()['last_error'], 'refused: text_only')


class BearerTests(Base):
    KEY = 'secret-classifier-key'

    def call(self):
        seen = {}

        def urlopen(request, timeout):
            seen['url'], seen['authorization'] = request.full_url, request.get_header('Authorization')
            return Answer()

        with patch('urllib.request.urlopen', urlopen):
            provisioner.D1HttpModel().system_one('s', {'attention': {}})
        return seen

    def test_convos_own_decision_classifier_gets_its_key_as_a_bearer_and_the_other_sources_none(self):
        self.keys.update(CONVO_CLASSIFIER_API_KEY=self.KEY, CONVO_CLASSIFIER_API_KEY_URL='http://localhost:9940')
        self.decider('convo', 'http://localhost:9940')
        self.assertEqual(self.call(), {'url': 'http://localhost:9940/v1/systemone', 'authorization': 'Bearer ' + self.KEY})
        self.keys['CONVO_CLASSIFIER_API_KEY_URL'] = 'http://localhost:9999'           # a key saved for another address stays there
        self.assertIsNone(self.call()['authorization'])
        with patch.dict(os.environ, {'D1_TEST_CLASSIFIER_REF': 'env-key'}):
            self.decider('convo', 'http://localhost:9940', classifier={'token_env': 'D1_TEST_CLASSIFIER_REF'})
            self.assertEqual(self.call()['authorization'], 'Bearer env-key')         # an environment reference is authoritative
        self.keys['CONVO_CLASSIFIER_API_KEY_URL'] = 'http://host.docker.internal:9944'
        self.decider('system_one')
        self.write('system_1', {'decider': {'backend': 'd1_llama_cpp', 'model': 'd1', 'endpoint': 'http://host.docker.internal:9944/v1/systemone'}})
        self.assertEqual(self.call(), {'url': 'http://host.docker.internal:9944/v1/systemone', 'authorization': None})   # System One's decider: no Convo key

    def test_only_the_convo_source_asks_for_a_key(self):
        asked, seen = [], {}
        def bearer():
            asked.append(True)
            return 'Bearer must-not-be-sent'
        def urlopen(request, timeout):
            seen.setdefault('authorization', []).append(request.get_header('Authorization'))
            return Answer()
        with patch('urllib.request.urlopen', urlopen):
            for source in ('system_one', ''):
                model = provisioner.D1HttpModel(resolve=lambda: {'source': source, 'base_url': 'http://localhost:9944', 'host': 'localhost:9944'}, bearer=bearer)
                model.system_one('s', {'attention': {}})
            provisioner.D1HttpModel('http://localhost:9944', bearer=bearer).system_one('s', {'attention': {}})     # a fixed address
        self.assertEqual((asked, seen['authorization']), ([], [None, None, None]))

    def test_the_chosen_model_is_named_in_each_decision_and_nothing_else_changes_without_one(self):
        bodies = []
        def urlopen(request, timeout):
            bodies.append(json.loads(request.data))
            return Answer()
        with patch('urllib.request.urlopen', urlopen):
            for model in ('d1-omni-600m', ''):
                where = {'source': 'convo', 'base_url': 'http://localhost:9944', 'host': 'localhost:9944', 'model': model}
                provisioner.D1HttpModel(resolve=lambda: where, bearer=lambda: '').system_one('s', {'attention': {}})
        self.assertEqual(bodies[0]['model'], 'd1-omni-600m')        # a router serving several decision models needs the name
        self.assertNotIn('model', bodies[1])                       # the 9931 default without a name works as before
        self.write('_convo', {'convo': {'enabled': True, 'system_one': {'enabled': True, 'backend': 'd1_llama_cpp'},
                                        'decider': {'source': 'convo', 'kind': 'd1_llama_cpp', 'url': 'http://localhost:9940', 'model': 'd1-omni-600m'}}})
        self.assertEqual(provisioner.resolve_endpoint()['model'], 'd1-omni-600m')

    def test_the_key_is_bound_to_the_derived_root_and_never_reaches_status_or_the_cache(self):
        self.keys.update(CONVO_CLASSIFIER_API_KEY=self.KEY, CONVO_CLASSIFIER_API_KEY_URL='https://api.typesafe.ai')
        self.decider('convo', classifier={'provider': 'jev', 'remote': True, 'key_source': 'convo'})
        self.assertEqual(self.call()['authorization'], 'Bearer ' + self.KEY)
        state = provisioner.ensure_registered()
        for text in (json.dumps(state), json.dumps(provisioner.status()), json.dumps(provisioner._resolved, default=str), json.dumps(provisioner._state, default=str)):
            self.assertNotIn(self.KEY, text)
        with patch.object(provisioner, '_convo', side_effect=RuntimeError('Convo unavailable')):
            self.assertEqual(provisioner.classifier_bearer(), '')                     # a failed key read sends no key, never raises


class ModelCallTests(Base):
    def test_audio_is_sent_as_a_wav_to_the_systemone_endpoint_and_the_answers_returned(self):
        seen = {}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self): return json.dumps({'answers': {'attention': {'choice': 'addressed'}}}).encode()

        def fake_urlopen(request, timeout):
            seen['url'], seen['body'], seen['timeout'] = request.full_url, json.loads(request.data), timeout
            return Response()

        sdk = provisioner.load_sdk()
        audio = sdk.AudioInput(b'\x01\x00' * 160, 16000, 1)
        with patch('urllib.request.urlopen', fake_urlopen):
            result = provisioner.D1HttpModel('http://host.docker.internal:9931').system_one('state text', {'attention': {}}, audio=audio)
        self.assertEqual(result, {'answers': {'attention': {'choice': 'addressed'}}})
        self.assertEqual(seen['url'], 'http://host.docker.internal:9931/v1/systemone')
        self.assertEqual(seen['body']['state'], 'state text')
        header, payload = seen['body']['files'][0].split(',', 1)
        self.assertEqual(header, 'data:audio/wav;base64')
        with wave.open(io.BytesIO(base64.b64decode(payload)), 'rb') as stream:
            self.assertEqual((stream.getframerate(), stream.getnchannels(), stream.getnframes()), (16000, 1, 160))

    def test_images_are_refused_and_failures_are_recorded_without_content(self):
        model = provisioner.D1HttpModel('http://localhost:1')
        with self.assertRaises(ValueError):
            model.system_one('s', {}, images=[object()])
        with patch('urllib.request.urlopen', side_effect=OSError('secret detail')):
            with self.assertRaises(OSError):
                model.system_one('s', {})
        self.assertEqual(provisioner._state['last_error'], 'OSError')

    def test_a_fixed_address_obeys_the_same_rule(self):
        with self.assertRaises(provisioner.DeciderUnavailable):
            provisioner.D1HttpModel('http://example.com:9931')


if __name__ == '__main__':
    unittest.main()
