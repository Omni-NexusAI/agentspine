"""The connection rules every model connection shares: your network (this machine, LAN, Tailscale) needs no consent, and a
credential environment reference names a variable. A bad reference saved earlier never locks the settings: the read reports it,
an unrelated save keeps working, a new bad value is refused without being echoed, and Test connection says why."""
import json
import unittest
from pathlib import Path

if __package__:
    from .setup_harness import SetupHarness
else:
    from setup_harness import SetupHarness

from plugins._convo.helpers import convo_contract as contract
from plugins._convo.helpers.convo_contract import ContractError, credential_reference_problem, credentials, endpoint, local_host

CASES = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'connection-rules-cases.json').read_text(encoding='utf-8'))
ADDRESS = 'http://host.docker.internal:8081/v1'


class RuleTests(unittest.TestCase):
    def test_your_network_is_local_and_everything_else_public(self):
        for host in CASES['local_hosts']:
            self.assertTrue(local_host(host), host)
        for host in CASES['public_hosts']:
            self.assertFalse(local_host(host), host)

    def test_lan_and_tailscale_addresses_need_no_consent_or_https(self):
        for url in ('http://192.168.1.50:8081/v1', 'http://100.101.102.103:8081/v1', 'http://[fd7a:115c:a1e0::1]:8081/v1',
                    'http://my-desktop.tail1234.ts.net:8081/v1', 'http://desktop:8081/v1', 'http://desktop.local:8081/v1'):
            self.assertEqual(endpoint({'url': url}), url, url)
        with self.assertRaisesRegex(ContractError, 'Allow remote processing'):
            endpoint({'url': 'http://100.128.0.1:8081/v1'})
        with self.assertRaisesRegex(ContractError, 'HTTPS'):
            endpoint({'url': 'http://example.com/v1', 'remote': True})

    def test_a_credential_reference_names_a_variable_and_the_value_is_never_repeated(self):
        for name in CASES['references_ok']:
            self.assertEqual(credential_reference_problem(name), '', name)
        for name, hint in CASES['references_bad']:
            problem = credential_reference_problem(name)
            self.assertIn('name of an environment variable', problem)
            self.assertIn(hint, problem)
            self.assertNotIn(name, problem)
            with self.assertRaises(ContractError) as raised:
                credentials({'token_env': name})
            self.assertNotIn(name, str(raised.exception))

    def test_every_reference_is_reported_by_its_path(self):
        config = contract.validate_settings({'roles': {'conversation': {'token_env': ADDRESS}, 'tts': {'token_env': 'GOOD_NAME'}},
                                             'sidecar': {'token_env': 'sk-0123456789abcdef0123'}})
        problems = contract.credential_reference_problems(config)
        self.assertEqual(sorted(problems), ['roles.conversation.token_env', 'sidecar.token_env'])
        self.assertEqual(contract.credential_reference_label('roles.conversation.token_env'), 'Conversation model')


class LockoutTests(SetupHarness, unittest.IsolatedAsyncioTestCase):
    """The test host's state on 2026-10-31: an address saved in the conversation model's credential reference."""
    def setUp(self):
        super().setUp()
        self.initial['convo']['roles']['conversation']['token_env'] = ADDRESS
        self.store.atomic_write(self.path, self.initial)

    def test_the_settings_still_open_and_say_what_is_wrong(self):
        result = self.setup.read()
        self.assertFalse(result['credentials']['conversation'])
        row = {item['id']: item for item in result['readiness']}['roles_conversation_token_env']
        self.assertEqual((row['state'], row['path']), ('missing', 'roles.conversation.token_env'))
        self.assertIn('looks like an address', row['detail'])
        self.assertNotIn(ADDRESS, row['detail'])

    def test_an_unrelated_save_keeps_working_and_clearing_it_saves(self):
        result = self.setup.read()
        result['config']['style'] = 'changed persona'
        saved = self.setup.save(result['config'], result['revision'])
        self.assertEqual(saved['config']['style'], 'changed persona')
        self.assertEqual(saved['config']['roles']['conversation']['token_env'], ADDRESS)       # reported, never silently dropped
        saved['config']['roles']['conversation']['token_env'] = ''
        cleared = self.setup.save(saved['config'], saved['revision'])
        self.assertEqual(cleared['config']['roles']['conversation']['token_env'], '')
        self.assertNotIn('roles_conversation_token_env', {item['id'] for item in cleared['readiness']})

    def test_a_new_bad_reference_is_refused_and_nothing_is_written(self):
        self.initial['convo']['roles']['conversation']['token_env'] = ''
        self.store.atomic_write(self.path, self.initial)
        result = self.setup.read()
        before = self.path.read_bytes()
        key = 'sk-or-v1-0123456789abcdef0123'
        result['config']['roles']['classifier']['token_env'] = key
        with self.assertRaises(self.contract.ContractError) as raised:          # the harness's own copy of the contract
            self.setup.save(result['config'], result['revision'])
        self.assertIn('Classifier', str(raised.exception))
        self.assertNotIn(key, str(raised.exception))
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(self.contract.ContractError):
            self.setup.patch([{'path': 'roles.tts.token_env', 'base': '', 'value': ADDRESS}])
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_autosave_patch_can_clear_it(self):
        result = self.setup.patch([{'path': 'roles.conversation.token_env', 'base': ADDRESS, 'value': ''}])
        self.assertEqual(result['applied'], ['roles.conversation.token_env'])
        self.assertEqual(self.store.read(self.path)['convo']['roles']['conversation']['token_env'], '')

    async def test_test_connection_says_why_without_contacting_anything(self):
        result = await self.setup.discover('conversation', check=True)
        self.assertEqual(result['verification']['state'], 'unavailable')
        self.assertIn('looks like an address', result['verification']['message'])
        self.assertIn('Nothing was contacted', result['verification']['message'])
        self.http.assert_not_called()


class ConnectionMessageTests(SetupHarness, unittest.IsolatedAsyncioTestCase):
    async def test_each_failure_has_its_own_message(self):
        import httpx
        cases = ((self.setup.Answered(401), 'refused the key'), (self.setup.Answered(404), 'ends with the server’s /v1'),
                 (self.setup.Answered(500), 'HTTP 500'), (httpx.ConnectError('refused'), 'LAN or Tailscale'),
                 (httpx.ReadTimeout('slow'), 'within 8 s'), (ValueError('Expecting value'), 'could not be read as a models list'))
        for error, text in cases:
            self.http.side_effect = error
            message = (await self.setup.discover('conversation'))['verification']['message']
            self.assertIn(text, message, type(error).__name__)
            self.assertIn('no fallback was used', message)
            self.assertNotIn('Expecting value', message)                                   # no server or parser text is echoed


if __name__ == '__main__':
    unittest.main()
