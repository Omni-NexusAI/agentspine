"""Setup contracts with temporary settings and mocked host, secrets and HTTP (see setup_harness)."""
import copy
import json
import os
import sys
import threading
import types
import unittest
from unittest.mock import patch

if __package__:
    from .setup_harness import PACKAGE, ROOT, SetupHarness
else:
    from setup_harness import PACKAGE, ROOT, SetupHarness


class SetupTests(SetupHarness, unittest.IsolatedAsyncioTestCase):
    def test_read_is_side_effect_free_and_readiness_is_not_inference_certification(self):
        before = self.path.read_bytes()
        with patch('subprocess.Popen', side_effect=AssertionError('no child process on reads')):
            result = self.setup.read()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.parent.joinpath('data').exists())
        checks = {item['id']:item for item in result['readiness']}
        self.assertEqual(checks['runtime']['state'], 'ready')
        self.assertEqual(checks['conversation_configured']['state'], 'ready')
        self.assertEqual(checks['conversation_connection']['state'], 'unverified')
        self.assertNotIn('native', result['config'])
        self.http.assert_not_called(); self.manager.start.assert_not_called()

    def test_ai_link_mode_does_not_require_configured_conversation_models(self):
        self.initial['convo'].update(enabled=False, ai_link_development_mode=True)
        self.initial['convo']['roles']['conversation']['model'] = ''
        self.initial['convo']['roles']['classifier']['model'] = ''
        self.store.atomic_write(self.path, self.initial)
        result = self.setup.read()
        checks = {item['id']: item for item in result['readiness']}
        self.assertEqual(checks['plugin']['state'], 'ready')
        self.assertEqual(checks['voice_mode']['state'], 'idle')
        self.assertEqual(checks['classifier_configured']['state'], 'missing')
        self.manager.start.assert_not_called()

    def test_readiness_says_the_speech_gate_is_unsupported_for_a_classifier_without_probabilities(self):
        result = self.setup.read()
        gate = {item['id']: item for item in result['readiness']}['speech_gate']
        self.assertEqual(gate['state'], 'unsupported')
        self.assertIn('0.85', gate['detail'])
        self.assertIn('does not switch to another classifier', gate['detail'])
        self.assertEqual(result['config']['policy_gate'], self.contract.DEFAULTS['policy_gate'])      # the stored constants are readable either way

    def test_readiness_always_says_input_is_completed_turns_never_incremental(self):
        for convo in ({}, {'system_one': {'enabled': True, 'backend': 'not_registered'}},
                      {'roles': {'conversation': {'capabilities': {'audio_input': False}}}}):
            self.initial['convo'] = {**self.initial['convo'], **convo}
            self.store.atomic_write(self.path, self.initial)
            row = {item['id']: item for item in self.setup.read()['readiness']}['incremental_input']
            self.assertEqual((row['state'], row['mode']), ('unsupported', 'completed_clip'))
            self.assertIn('Completed turns only; audio input is not incremental or duplex input', row['detail'])

    def test_a_declared_incremental_flag_is_unverified_not_supported(self):
        config = self.contract.validate_settings({'roles': {'conversation': {'capabilities': {'audio_input': True, 'incremental_input': True}}}})
        row = self.setup.incremental_input_row(config, [])
        self.assertEqual((row['state'], row['mode']), ('unverified', 'completed_clip'))
        self.assertIn('does not use or verify', row['detail'])
        backend = self.setup.incremental_input_row(self.contract.validate_settings({}), [{'id': 'x', 'capabilities': {'incremental_input': True}}])
        self.assertEqual(backend['state'], 'unverified')
        self.assertEqual(self.setup.incremental_input_row(self.contract.validate_settings({}), [{'id': 'x', 'capabilities': None}])['state'], 'unsupported')

    def test_no_interface_text_implies_streaming_input(self):
        import re
        pattern = re.compile(r'(?i)stream(s|ing|ed)?\s+(in\s+)?(your|the)\s+(speech|voice|audio|microphone|mic)\b|as you (speak|talk)|while you (speak|talk)|full[- ]duplex|live\s+transcri')
        for path in (ROOT / 'webui').glob('*'):
            if path.suffix in {'.html', '.js'} and path.name not in {'audio.js'}:
                self.assertIsNone(pattern.search(path.read_text(encoding='utf-8')), path.name)

    def test_readiness_says_the_calibrated_gate_has_nothing_to_act_on_while_the_selected_backend_is_missing(self):
        self.initial['convo']['system_one'] = {'enabled': True, 'backend': 'not_registered'}
        self.store.atomic_write(self.path, self.initial)
        checks = {item['id']: item for item in self.setup.read()['readiness']}
        self.assertEqual(checks['speech_gate']['state'], 'unavailable')
        self.assertNotEqual(checks['system_one']['state'], 'ready')

    def test_an_invalid_gate_setting_is_refused_by_the_shared_save_and_leaves_the_file_alone(self):
        result = self.setup.read()
        before = self.path.read_bytes()
        result['config']['policy_gate']['respond_min'] = 1.5
        with self.assertRaises(ValueError):
            self.setup.save(result['config'], result['revision'])
        self.assertEqual(self.path.read_bytes(), before)
        fine = self.setup.read()
        fine['config']['policy_gate']['respond_min'] = .4
        self.assertEqual(self.setup.save(fine['config'], fine['revision'])['config']['policy_gate']['respond_min'], .4)

    def test_all_settings_writers_reject_development_mode_during_voice(self):
        runtime = sys.modules['helpers.runtime']
        runtime._convo_service = types.SimpleNamespace(
            connections={'browser': object()}, connection_lock=threading.RLock())
        shared = self.setup.read()
        shared['config']['ai_link_development_mode'] = True
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'End the active'):
            self.setup.save(shared['config'], shared['revision'])
        native = self.store.native_read(self.initial)
        native['convo']['ai_link_development_mode'] = True
        with self.assertRaisesRegex(ValueError, 'End the active'):
            self.store.native_save(native)
        self.assertEqual(self.path.read_bytes(), before)

    def test_shared_save_rejects_stale_native_editor(self):
        native = self.store.native_read(self.initial)
        shared = self.setup.read(); shared['config']['style'] = 'new style'
        self.setup.save(shared['config'], shared['revision'])
        native['convo']['style'] = 'stale'
        with self.assertRaises(self.store.Conflict): self.store.native_save(native)
        self.assertEqual(self.store.read(self.path)['convo']['style'], 'new style')

    def test_native_save_rejects_stale_shared_editor(self):
        shared = self.setup.read(); native = self.store.native_read(self.initial)
        native['convo']['style'] = 'native change'; self.store.native_save(native)
        with self.assertRaises(self.store.Conflict): self.setup.save(shared['config'],shared['revision'])
        self.assertEqual(self.store.read(self.path)['convo']['style'], 'native change')

    def test_unknown_and_native_fields_survive_narrow_save(self):
        result = self.setup.read(); self.setup.save({'style':'new'},result['revision'])
        after = self.store.read(self.path)
        self.assertEqual(after['native'],self.initial['native'])
        self.assertEqual(after['future_top'],self.initial['future_top'])
        self.assertEqual(after['convo']['future_option'],{'keep':17})

    def test_write_only_key_is_bound_to_endpoint_and_empty_env_never_falls_back(self):
        initial = self.setup.read()
        result = self.setup.save(initial['config'],initial['revision'],{'conversation':'private-value'})
        self.assertTrue(result['credentials']['conversation'])
        self.assertNotIn('private-value',json.dumps(result))
        self.assertNotIn('private-value',self.path.read_text())
        private = self.setup.attach_keys(result['config'])
        self.assertEqual(self.contract.credentials(private['roles']['conversation']),{'Authorization':'Bearer private-value'})
        changed = copy.deepcopy(result['config']); changed['roles']['conversation']['url']='http://127.0.0.1:9090/v1'
        self.assertEqual(self.contract.credentials(self.setup.attach_keys(changed)['roles']['conversation']),{})
        changed = copy.deepcopy(result['config']); changed['roles']['conversation']['token_env']='CONVO_TEST_EXPLICIT_EMPTY'
        with patch.dict(os.environ, {'CONVO_TEST_EXPLICIT_EMPTY':''}):
            self.assertEqual(self.contract.credentials(self.setup.attach_keys(changed)['roles']['conversation']),{})

    def test_explicit_key_clear_does_not_clear_other_roles(self):
        initial = self.setup.read(); result = self.setup.save(initial['config'],initial['revision'],{'conversation':'one','tts':'two'})
        cleared = self.setup.save(result['config'],result['revision'],{'conversation':''})
        self.assertFalse(cleared['credentials']['conversation']); self.assertTrue(cleared['credentials']['tts'])

    def test_inline_credentials_rejected_by_shared_and_native_saves(self):
        initial=self.setup.read(); initial['config']['roles']['conversation']['api_key']='not-allowed'
        with self.assertRaises(ValueError): self.setup.save(initial['config'],initial['revision'])
        native=self.store.native_read(self.initial); native['convo']['roles']['tts']['_convo_key']='not-allowed'
        with self.assertRaises(ValueError): self.store.native_save(native)

    def test_import_preview_translates_loopback_and_does_not_write(self):
        before=self.path.read_bytes()
        result=self.setup.preview({'gemma_url':'http://127.0.0.1:8818/v1','gemma_model':'new',
            'style':'ignore','system_prompt':'ignore','compaction':{'context_tokens':1},'api_key':'ignore'})
        changes={item['path']:item for item in result['changes']}
        self.assertEqual(changes['roles.conversation.url']['after'],'http://host.docker.internal:8818/v1')
        self.assertEqual(set(changes),{'roles.conversation.url','roles.conversation.model'})
        self.assertEqual(before,self.path.read_bytes()); self.assertFalse(self.path.parent.joinpath('data').exists())

    def test_import_selected_only_and_conflict_after_preview(self):
        document={'gemma_model':'new','tts_voice':'clone:new'}; preview=self.setup.preview(document)
        result=self.setup.apply(document,['roles.tts.voice'],preview['revision'])
        self.assertEqual(result['config']['roles']['conversation']['model'],'local-model')
        self.assertEqual(result['config']['roles']['tts']['voice'],'clone:new')
        self.assertEqual(result['config']['style'],'existing persona')
        with self.assertRaises(self.store.Conflict): self.setup.apply(document,['roles.conversation.model'],preview['revision'])

    def test_actual_standalone_export_fields_are_imported(self):
        document={'gemma_audio_base_url':'http://127.0.0.1:8818/v1','model_name':'gemma-model',
            'qwen3_tts_api_base_url':'http://127.0.0.1:8881/v1','qwen3_tts_api_model':'qwen3-tts',
            'qwen3_tts_api_voice':'clone:jarvis','qwen3_tts_backend':'audio-cpp'}
        paths={item['path'] for item in self.setup.preview(document)['changes']}
        self.assertIn('roles.conversation.url',paths)
        self.assertIn('roles.conversation.model',paths)
        self.assertIn('roles.tts.voice',paths)

    def test_key_only_saves_advance_revision_and_reject_stale_clear(self):
        initial = self.setup.read()
        normalized = self.setup.save(initial['config'], initial['revision'])
        updated = self.setup.save(normalized['config'], normalized['revision'], {'conversation': 'new-private-key'})
        self.assertNotEqual(updated['revision'], normalized['revision'])
        with self.assertRaises(self.store.Conflict):
            self.setup.save(normalized['config'], normalized['revision'], {'conversation': ''})

    def test_native_speech_saves_are_not_conflicts_with_an_open_convo_editor(self):
        shared = self.setup.read(); shared['config']['style'] = 'edited while Kokoro changed'
        written = self.store.read(self.path)
        written['tts'] = {'kokoro': {'voice': 'af_heart', 'speed': 1.3}}          # what the native Kokoro bridge writes to the same file
        self.store.atomic_write(self.path, written)
        result = self.setup.save(shared['config'], shared['revision'])            # the editor's revision still matches: Convo's own node is untouched
        after = self.store.read(self.path)
        self.assertEqual(after['tts'], {'kokoro': {'voice': 'af_heart', 'speed': 1.3}})
        self.assertEqual(after['convo']['style'], 'edited while Kokoro changed')
        self.assertNotEqual(result['revision'], shared['revision'])

    def test_scoped_revision_follows_the_convo_node_and_the_save_epoch_only(self):
        base = {'convo': {'style': 'a'}, '_convo_write_epoch': 1, 'tts': {'kokoro': {'voice': 'x'}}}
        same = self.store.settings_revision(base)
        self.assertEqual(self.store.settings_revision({**base, 'tts': {'kokoro': {'voice': 'y'}}, 'stt': {}, 'unknown_top': 1}), same)
        self.assertNotEqual(self.store.settings_revision({**base, 'convo': {'style': 'b'}}), same)
        self.assertNotEqual(self.store.settings_revision({**base, '_convo_write_epoch': 2}), same)      # a key-only save still advances it
        self.assertEqual(self.store.settings_revision({}), self.store.settings_revision({'convo': {}, '_convo_write_epoch': 0}))
        self.assertEqual(self.setup.read()['revision'], self.store.settings_revision(self.store.read(self.path)))   # status and read agree
        self.assertNotEqual(self.store.revision(base), self.store.revision({**base, 'tts': {}}))        # the generic hash is unchanged (role fingerprints use it)

    def test_a_stale_host_editor_copy_cannot_revert_the_native_speech_aggregates(self):
        native = self.store.native_read(self.initial)
        written = self.store.read(self.path)
        written['tts'] = {'kokoro': {'voice': 'new_voice'}}                        # saved natively after the host editor read its copy
        self.store.atomic_write(self.path, written)
        native['tts'] = {'kokoro': {'voice': 'stale_voice'}}; native['stt'] = {'whisper': {'model_size': 'tiny'}}
        native['convo']['style'] = 'from the host editor'
        self.store.native_save(native)
        after = self.store.read(self.path)
        self.assertEqual(after['tts'], {'kokoro': {'voice': 'new_voice'}})        # not reverted
        self.assertNotIn('stt', after)                                             # not introduced
        self.assertEqual(after['convo']['style'], 'from the host editor')
        self.assertEqual(after['native'], self.initial['native'])

    def test_unselected_and_unrecognized_import_paths_are_rejected(self):
        document={'gemma_model':'new'}; preview=self.setup.preview(document)
        with self.assertRaises(ValueError): self.setup.apply(document,['style'],preview['revision'])

    def test_discovery_options_accept_common_shapes_and_deduplicate(self):
        options=self.setup._options({'data':[{'id':'a','name':'A'},{'id':'a'},'b',None,{'name':'missing'}]},('data',))
        self.assertEqual(options,[{'id':'a','label':'A'},{'id':'b','label':'b'}])
        self.assertEqual(self.setup._options({'models':{'a':{'label':'Alpha'}}},('models',)),[{'id':'a','label':'Alpha'}])

    def test_uncertain_studio_operation_is_visible_without_starting_runtime(self):
        admission = __import__(PACKAGE + '.studio_admission', fromlist=['dispatched'])
        admission.dispatched(self.path.parent / 'data' / admission.FILENAME)
        before = self.path.read_bytes()
        result = self.setup.read()
        self.assertTrue(result['studio_admission']['blocked'])
        check = next(item for item in result['readiness'] if item['id'] == 'studio_admission')
        self.assertEqual(check['state'], 'unavailable')
        self.assertIn('reconciliation', check['detail'])
        self.assertEqual(self.path.read_bytes(), before)
        self.manager.start.assert_not_called()

    async def test_failed_provider_does_not_fall_back_or_change_selection(self):
        self.http.side_effect=ValueError('offline'); before=self.path.read_bytes()
        result=await self.setup.discover('conversation')
        self.assertEqual(result['verification']['state'],'unavailable')
        self.assertIn('no fallback',result['verification']['message'])
        self.assertEqual(self.http.await_count,1); self.assertEqual(before,self.path.read_bytes())

    async def test_discovery_is_bounded_to_selected_endpoint_and_check_records_evidence(self):
        self.http.side_effect=None;self.http.return_value={'data':[{'id':'local-model'}]}
        result=await self.setup.discover('conversation',check=True)
        self.assertEqual(result['models'],[{'id':'local-model','label':'local-model'}])
        self.http.assert_awaited_once_with('http://127.0.0.1:8080/v1/models',{})
        checks={item['id']:item for item in self.setup.read()['readiness']}
        self.assertEqual(checks['conversation_connection']['state'],'ready')
        self.assertIn('capabilities',checks['conversation_connection']['detail'])

    async def test_draft_endpoint_does_not_receive_saved_endpoint_key(self):
        initial=self.setup.read();self.setup.save(initial['config'],initial['revision'],{'conversation':'private'})
        self.http.side_effect=None; self.http.return_value={'data':[]}
        await self.setup.discover('conversation',{'url':'http://127.0.0.1:9090/v1'})
        self.http.assert_awaited_once_with('http://127.0.0.1:9090/v1/models',{})

    def auth(self, config, role='conversation'):
        return self.contract.credentials(self.setup.attach_keys(config)['roles'][role]).get('Authorization')

    def test_keys_come_from_the_reference_then_agent_zeros_provider_key_then_convos_endpoint_key(self):
        initial = self.setup.read()
        config = copy.deepcopy(self.setup.save(initial['config'], initial['revision'], {'conversation': 'convo-key'})['config'])
        conversation = config['roles']['conversation']
        self.assertEqual(self.auth(config), 'Bearer convo-key')                       # no provider: the endpoint as entered, as before
        conversation['provider'] = 'openai'
        self.assertEqual(self.auth(config), 'Bearer convo-key')                       # Agent Zero holds no key: Convo's key bound to this endpoint
        self.host_keys['openai'] = 'host-openai-key'
        self.assertEqual(self.auth(config), 'Bearer host-openai-key')                 # Agent Zero's key for the provider comes first
        conversation['key_source'] = 'convo'
        self.assertEqual(self.auth(config), 'Bearer convo-key')                       # unless the role asks for Convo's own key
        conversation['key_source'] = 'provider'
        conversation['token_env'] = 'CONVO_TEST_EXPLICIT_REF'
        with patch.dict(os.environ, {'CONVO_TEST_EXPLICIT_REF': 'env-key'}):
            self.assertEqual(self.auth(config), 'Bearer env-key')                     # an explicit reference is authoritative...
        with patch.dict(os.environ, {'CONVO_TEST_EXPLICIT_REF': ''}):
            self.assertIsNone(self.auth(config))                                      # ...even when it is empty: nothing else is tried
        self.assertNotIn('_convo_key', self.setup.attach_keys(config)['roles']['conversation'])   # nor attached beside it
        conversation['token_env'] = ''
        conversation['url'] = 'http://127.0.0.1:9999/v1'
        self.assertEqual(self.auth(config), 'Bearer host-openai-key')                 # the provider key is not bound to a URL
        del self.host_keys['openai']
        self.assertIsNone(self.auth(config))                                          # and Convo's key never leaves the endpoint it was saved for

    def test_agent_zeros_unset_answers_and_failures_mean_no_key(self):
        initial = self.setup.read()
        config = copy.deepcopy(self.setup.save(initial['config'], initial['revision'], {'conversation': 'convo-key'})['config'])
        config['roles']['conversation']['provider'] = 'openrouter'
        for unset in ('None', 'NA', '', '   ', None, 7):
            self.host_keys['openrouter'] = unset
            self.assertEqual(self.auth(config), 'Bearer convo-key', repr(unset))
        self.host_keys['openrouter'] = 'host-openrouter-key'
        self.assertEqual(self.auth(config), 'Bearer host-openrouter-key')
        broken = types.ModuleType('models')
        broken.get_api_key = lambda provider: (_ for _ in ()).throw(RuntimeError('host unavailable'))
        with patch.dict(sys.modules, {'models': broken}):
            self.assertEqual(self.auth(config), 'Bearer convo-key')

    def test_only_providers_with_an_agent_zero_key_use_one(self):
        initial = self.setup.read()
        config = copy.deepcopy(self.setup.save(initial['config'], initial['revision'], {'conversation': 'convo-key', 'tts': 'convo-tts'})['config'])
        self.host_keys.update(openai='host-openai-key', other='host-other-key')
        config['roles']['conversation']['provider'] = 'other'
        self.assertEqual(self.auth(config), 'Bearer convo-key')                       # a custom endpoint keeps Convo's own key
        config['roles']['conversation']['provider'] = 'lm_studio'
        self.assertEqual(self.auth(config), 'Bearer convo-key')                       # a provider Convo cannot use lends no key
        config['roles']['tts']['provider'] = 'openai_compatible'
        self.assertEqual(self.auth(config, 'tts'), 'Bearer convo-tts')
        config['roles']['tts']['provider'] = 'openai_tts'
        self.assertEqual(self.auth(config, 'tts'), 'Bearer host-openai-key')          # OpenAI speech uses Agent Zero's openai key
        self.assertIsNone(self.auth(config, 'classifier'))                            # the classifier has no provider: its own key only

    def test_no_key_value_reaches_a_settings_response(self):
        self.host_keys['openai'] = 'host-openai-secret'
        initial = self.setup.read()
        initial['config']['roles']['conversation'].update(provider='openai', remote=True, model='gpt-4o-audio-preview')
        result = self.setup.save(initial['config'], initial['revision'], {'tts': 'convo-tts-secret', 'conversation': 'convo-own-secret'})
        self.assertTrue(result['credentials']['conversation'])                        # readiness knows a key is there...
        self.assertTrue(result['credentials']['tts'])
        catalog = self.catalog()
        with patch.object(catalog, '_post', new=self.provider(200, {'choices': [{'message': {'content': 'OK'}}]})):
            probe = self.probe(catalog, {'provider': 'openai'})
        answers = (result, self.setup.read(), self.setup.patch([{'path': 'style', 'base': 'existing persona', 'value': 'x'}]),
                   catalog.key_copy({'provider': 'openai'}), probe)
        for answer in answers:
            self.assertNotIn('host-openai-secret', json.dumps(answer))                # ...and never says what it is
            self.assertNotIn('convo-tts-secret', json.dumps(answer))
            self.assertNotIn('convo-own-secret', json.dumps(answer))
        self.assertNotIn('host-openai-secret', self.path.read_text())

    # ---- The conversation model as an Agent Zero model type: endpoint derivation, the key copy and Test audio input.
    def catalog(self):
        import importlib
        return importlib.import_module(PACKAGE + '.convo_model_catalog')

    def probe(self, catalog, request):
        """Run Test audio input on its own event loop (the settings writes around it are synchronous file work)."""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(catalog.audio_probe(request))
        finally:
            loop.close()

    def provider(self, status, data, calls=None):
        """A stand-in for the one provider request audio_probe makes; nothing leaves the machine."""
        async def post(url, body, headers):
            if calls is not None:
                calls.append({'url': url, 'body': body, 'headers': headers})
            if isinstance(data, Exception):
                raise data
            return status, data
        return post

    def dotenv(self):
        """Agent Zero's .env as the host key store writes it (API_KEY_<PROVIDER>), recorded instead of written."""
        writes = []
        module = types.ModuleType('helpers.dotenv')
        def save(key, value):
            writes.append(key)
            if key.startswith('API_KEY_'):
                self.host_keys[key[len('API_KEY_'):].lower()] = value
        module.save_dotenv_value = save
        sys.modules['helpers'].dotenv = module
        modules = patch.dict(sys.modules, {'helpers.dotenv': module})
        modules.start(); self.addCleanup(modules.stop)
        self.addCleanup(lambda: vars(sys.modules['helpers']).pop('dotenv', None))
        return writes

    def choose(self, **role):
        """Store conversation-role fields through the shared patch path (normalized and validated like every writer)."""
        current = self.setup.read()['config']['roles']['conversation']
        result = self.setup.patch([{'path': 'roles.conversation.' + name, 'base': current.get(name), 'value': value} for name, value in role.items()])
        self.assertEqual(result['conflicts'], [])
        return self.store.read(self.path)['convo']['roles']['conversation']

    def test_a_provider_writes_its_endpoint_and_the_endpoint_only_setup_keeps_its_url(self):
        before = self.store.read(self.path)['convo']['roles']['conversation']['url']
        self.assertEqual(self.choose(model='omni')['url'], before)                     # provider '': the URL as entered, untouched
        initial = self.setup.read()
        self.setup.save(initial['config'], initial['revision'], {'conversation': 'local-key'})
        stored = self.choose(provider='other', api_base='http://127.0.0.1:8080/v1/')
        self.assertEqual(stored['url'], 'http://127.0.0.1:8080/v1')                   # Other: its API base becomes the endpoint ...
        self.assertEqual(self.auth(self.setup.read()['config']), 'Bearer local-key')   # ... so the key saved for that address still applies
        self.assertEqual(self.choose(api_base='http://127.0.0.1:9090/v1')['url'], 'http://127.0.0.1:9090/v1')
        self.assertIsNone(self.auth(self.setup.read()['config']))                     # and never follows the address elsewhere
        with self.assertRaisesRegex(ValueError, 'remote'):
            self.choose(provider='openrouter', api_base='')                            # a cloud provider needs remote consent
        stored = self.choose(provider='openrouter', api_base='', remote=True)
        self.assertEqual(stored['url'], 'https://openrouter.ai/api/v1')
        rows = {row['id']: row for row in self.setup.read()['readiness']}
        self.assertEqual(rows['conversation_provider']['state'], 'ready')
        self.assertIn('https://openrouter.ai/api/v1', rows['conversation_provider']['detail'])
        self.choose(api_base='https://proxy.example/v1')
        rows = {row['id']: row for row in self.setup.read()['readiness']}
        self.assertEqual(rows['conversation_key_route']['state'], 'warning')           # Agent Zero's key would go to a custom base

    def openrouter_with_convo_key(self, key='convo-openrouter-key'):
        """Convo's own key saved for OpenRouter's address while the endpoint was entered by hand, then OpenRouter chosen."""
        self.choose(url='https://openrouter.ai/api/v1', remote=True)
        initial = self.setup.read()
        self.setup.save(initial['config'], initial['revision'], {'conversation': key})
        self.choose(provider='openrouter')
        return key

    def test_the_key_copy_fills_an_empty_agent_zero_slot_once_and_never_returns_or_moves_the_key(self):
        writes, key = self.dotenv(), self.openrouter_with_convo_key()
        self.assertEqual(self.auth(self.setup.read()['config']), 'Bearer ' + key)    # Agent Zero has none: Convo's key, bound to this address
        catalog = self.catalog()
        self.host_keys['openrouter'] = ''                                              # an empty value is no key either ('None' is the default)
        result = catalog.key_copy({'provider': 'openrouter'})
        self.assertEqual((result['ok'], result['copied'], result['provider']), (True, True, 'openrouter'))
        self.assertNotIn(key, json.dumps(result))
        self.assertEqual((writes, self.host_keys['openrouter']), (['API_KEY_OPENROUTER'], key))
        self.assertEqual(self.secrets.load_secrets()['CONVO_CONVERSATION_API_KEY'], key)        # Convo keeps its own copy
        again = catalog.key_copy({'provider': 'openrouter'})
        self.assertEqual((again['ok'], again['code']), (False, 'occupied'))                     # never over a key
        self.host_keys['openrouter'] = 'the-users-own-key'
        self.assertEqual(catalog.key_copy({})['code'], 'occupied')
        self.assertEqual((writes, self.host_keys['openrouter']), (['API_KEY_OPENROUTER'], 'the-users-own-key'))

    def test_every_refused_copy_is_typed_and_writes_nothing(self):
        writes, catalog = self.dotenv(), self.catalog()
        self.assertEqual(catalog.key_copy({})['code'], 'not_applicable')                        # the endpoint as entered keeps Convo's key
        self.choose(provider='other', api_base='http://127.0.0.1:8080/v1')
        self.assertEqual(catalog.key_copy({})['code'], 'not_applicable')                        # so does Other
        self.choose(provider='openrouter', api_base='', remote=True)
        self.assertEqual(catalog.key_copy({'provider': 'openai'})['code'], 'unsaved')           # only the saved provider
        self.assertEqual(catalog.key_copy({})['code'], 'no_convo_key')
        self.choose(provider='')
        self.choose(url='http://127.0.0.1:8080/v1')
        initial = self.setup.read()
        self.setup.save(initial['config'], initial['revision'], {'conversation': 'local-server-key'})
        self.choose(provider='openrouter')
        refused = catalog.key_copy({'provider': 'openrouter'})                                   # a key for another address stays there
        self.assertEqual(refused['code'], 'different_endpoint')
        self.assertNotIn('local-server-key', json.dumps(refused))
        self.assertEqual(writes, [])
        with patch.object(self.setup, '_read_config', side_effect=RuntimeError('store unavailable')):
            self.assertEqual(catalog.key_copy({})['code'], 'unavailable')
        self.assertEqual(writes, [])

    def test_test_audio_input_sends_one_short_clip_with_the_saved_key_and_answers_without_it(self):
        self.host_keys['openrouter'] = 'host-openrouter-secret'
        self.choose(provider='openrouter', remote=True, model='google/gemini-2.5-flash')
        calls, catalog = [], self.catalog()
        with patch.object(catalog, '_post', new=self.provider(200, {'choices': [{'message': {'content': ' OK. '}}]}, calls)):
            result = self.probe(catalog, {'action': 'audio_probe', 'provider': 'openrouter'})
        self.assertEqual((result['ok'], result['passed'], result['reply'], result['status']), (True, True, 'OK.', 200))
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call['url'], 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(call['headers'], {'Authorization': 'Bearer host-openrouter-secret'})   # attached here, server-side
        self.assertEqual(call['body']['model'], 'google/gemini-2.5-flash')
        audio = [part for part in call['body']['messages'][-1]['content'] if part['type'] == 'input_audio']
        self.assertEqual(len(audio), 1)
        self.assertEqual(self.contract.validate_audio(audio[0]['input_audio']['data'], maximum=2), audio[0]['input_audio']['data'])
        self.assertEqual(audio[0]['input_audio']['format'], 'wav')
        self.assertNotIn('host-openrouter-secret', json.dumps(result))
        for status, code in ((401, 'refused'), (400, 'refused'), (200, 'format')):
            with patch.object(catalog, '_post', new=self.provider(status, {'error': {'message': 'key sk-or-***cret rejected'}})):
                answer = self.probe(catalog, {})
            self.assertEqual((answer['ok'], answer['passed'], answer['code']), (True, False, code), status)
            self.assertNotIn('sk-or', json.dumps(answer))                                       # the provider's own text never comes back
        with patch.object(catalog, '_post', new=self.provider(0, OSError('connection refused'))):
            self.assertEqual(self.probe(catalog, {})['code'], 'unreachable')

    def test_test_audio_input_refuses_without_sending_anything(self):
        calls, catalog = [], self.catalog()
        refusals = []
        with patch.object(catalog, '_post', new=self.provider(200, {}, calls)):
            self.choose(model='')
            refusals.append(self.probe(catalog, {}))                                     # no model
            self.choose(model='omni', capabilities={'audio_input': False, 'cancellation': True, 'tool_calls': True})
            refusals.append(self.probe(catalog, {}))                                     # this model gets transcripts
            self.choose(capabilities={'audio_input': True, 'cancellation': True, 'tool_calls': True})
            refusals.append(self.probe(catalog, {'provider': 'openai'}))                  # not the saved provider
            runtime = sys.modules['helpers.runtime']
            runtime._convo_service = types.SimpleNamespace(connections={'browser': object()}, connection_lock=threading.RLock())
            refusals.append(self.probe(catalog, {}))                                     # a voice session is live
            runtime._convo_service = None
            catalog._probing = True
            try:
                refusals.append(self.probe(catalog, {}))                                 # one test at a time
            finally:
                catalog._probing = False
            self.choose(provider='lm_studio')
            refusals.append(self.probe(catalog, {}))                                     # a provider Convo cannot use
        self.assertEqual([item['code'] for item in refusals], ['not_configured', 'not_configured', 'unsaved', 'busy', 'busy', 'not_usable'])
        self.assertTrue(all(item['ok'] is False and item['error'] for item in refusals))
        self.assertEqual(calls, [])

    # ---- The classifier card: derived runtime keys, the key bound to the address its requests go to, Check server's key.
    def classifier_catalog(self):
        import importlib
        return importlib.import_module(PACKAGE + '.convo_classifier_catalog')

    def choose_classifier(self, **fields):
        """Store classifier fields (and `decider_source`) through the shared patch path, normalized and validated like every writer."""
        config = self.setup.read()['config']
        changes = []
        for name, value in fields.items():
            path = 'decider.source' if name == 'decider_source' else 'roles.classifier.' + name
            node = config
            for part in path.split('.'):
                node = node.get(part) if isinstance(node, dict) else None
            changes.append({'path': path, 'base': node, 'value': value})
        result = self.setup.patch(changes)
        self.assertEqual(result['conflicts'], [])
        return self.store.read(self.path)['convo']

    def test_setup_read_delivers_the_classifier_catalog_and_no_key(self):
        result = self.setup.read()
        self.assertEqual(result['classifier_catalog'], self.classifier_catalog().catalog())
        self.assertEqual(list(result['classifier_catalog']), ['providers', 'disabled', 'unknown_compatible', 'unknown'])
        self.assertNotIn('API_KEY', json.dumps(result['classifier_catalog']))

    def test_a_decision_classifier_is_derived_and_its_key_is_bound_to_the_server_root(self):
        stored = self.choose_classifier(provider='llama_cpp', kind='decision', api_base='http://127.0.0.1:9931/v1', model='d1-omni-600M')
        self.assertEqual((stored['system_one'], stored['decider']['source'], stored['decider']['url'], stored['decider']['model']),
                         ({'enabled': True, 'backend': 'd1_llama_cpp'}, 'convo', 'http://127.0.0.1:9931', 'd1-omni-600M'))
        self.assertEqual(stored['roles']['classifier']['url'], 'http://127.0.0.1:8081/v1')      # the chat endpoint is left as it was
        initial = self.setup.read()
        result = self.setup.save(initial['config'], initial['revision'], {'classifier': 'classifier-secret'})
        self.assertEqual(self.secrets.load_secrets()['CONVO_CLASSIFIER_API_KEY_URL'], 'http://127.0.0.1:9931')
        self.assertTrue(result['credentials']['classifier'])
        config = self.setup.read()['config']
        self.assertEqual(self.setup.classifier_bearer(config), 'Bearer classifier-secret')
        self.assertEqual(self.auth(config, 'classifier'), 'Bearer classifier-secret')
        self.host_keys['llama_cpp'] = 'host-llama-key'
        self.assertEqual(self.setup.classifier_bearer(config), 'Bearer host-llama-key')           # Agent Zero's key for the provider first
        config['roles']['classifier']['key_source'] = 'convo'
        self.assertEqual(self.setup.classifier_bearer(config), 'Bearer classifier-secret')
        moved = self.choose_classifier(api_base='http://127.0.0.1:9940/v1', key_source='convo')
        self.assertEqual(moved['decider']['url'], 'http://127.0.0.1:9940')
        self.assertEqual(self.setup.classifier_bearer(self.setup.read()['config']), '')          # Convo's key never follows the address
        on = self.choose_classifier(decider_source='system_one', key_source='provider')        # Agent Zero's llama.cpp key is there
        self.assertEqual(on['system_one'], {'enabled': True, 'backend': 'd1_llama_cpp'})
        self.assertEqual(self.setup.classifier_bearer(self.setup.read()['config']), '')          # System One's decider: no Convo key
        for answer in (result, self.setup.read()):
            self.assertNotIn('classifier-secret', json.dumps(answer))
            self.assertNotIn('host-llama-key', json.dumps(answer))

    def test_a_classifier_key_saved_without_an_address_is_never_sent(self):
        self.choose_classifier(url='')
        initial = self.setup.read()
        self.setup.save(initial['config'], initial['revision'], {'classifier': 'classifier-secret'})
        self.assertEqual(self.secrets.load_secrets()['CONVO_CLASSIFIER_API_KEY_URL'], '')
        self.assertIsNone(self.auth(self.setup.read()['config'], 'classifier'))

    def test_a_chat_classifier_writes_its_endpoint_and_switches_the_decision_plugin_off(self):
        self.choose_classifier(provider='llama_cpp', kind='decision')
        with self.assertRaisesRegex(ValueError, 'remote'):
            self.choose_classifier(provider='openrouter', kind='auto')                             # a cloud chat base needs consent
        stored = self.choose_classifier(provider='openrouter', kind='auto', remote=True, model='google/gemini-2.5-flash')
        self.assertEqual((stored['roles']['classifier']['url'], stored['system_one']['enabled']), ('https://openrouter.ai/api/v1', False))
        self.host_keys['openrouter'] = 'host-openrouter-key'
        self.assertEqual(self.auth(self.setup.read()['config'], 'classifier'), 'Bearer host-openrouter-key')
        rows = {row['id']: row for row in self.setup.read()['readiness']}
        self.assertEqual(rows['classifier_provider']['state'], 'ready')
        self.assertIn('https://openrouter.ai/api/v1', rows['classifier_provider']['detail'])
        self.assertNotIn('host-openrouter-key', json.dumps(rows))

    async def test_classifier_discovery_asks_the_providers_openai_base(self):
        self.http.side_effect = None
        self.http.return_value = {'data': [{'id': 'd1-omni-600M'}]}
        result = await self.setup.discover('classifier', {'provider': 'llama_cpp', 'kind': 'decision', 'api_base': 'http://127.0.0.1:9931/v1'})
        self.http.assert_awaited_once_with('http://127.0.0.1:9931/v1/models', {})
        self.assertEqual(result['models'], [{'id': 'd1-omni-600M', 'label': 'd1-omni-600M'}])

    async def test_check_server_sends_the_key_server_side_and_never_returns_it(self):
        import httpx
        self.choose_classifier(provider='llama_cpp', kind='decision', api_base='http://127.0.0.1:9931/v1')
        initial = self.setup.read()
        self.setup.save(initial['config'], initial['revision'], {'classifier': 'classifier-secret'})
        seen = []
        def handler(request):
            seen.append((request.method, str(request.url), request.headers.get('authorization')))
            if request.url.path == '/v1/systemone':
                return httpx.Response(200, json={'answers': {'check': {'choice': 'yes', 'confidence': .9, 'probabilities': {'yes': .9, 'no': .1}}}})
            return httpx.Response(200, json={'data': [{'id': 'd1-omni-600M'}]} if request.url.path.endswith('models') else {'status': 'ok'})
        result = await self.classifier_catalog().check({'provider': 'llama_cpp', 'api_base': 'http://127.0.0.1:9931/v1', 'kind': 'decision'},
                                                       transport=httpx.MockTransport(handler))
        self.assertEqual(result['state'], 'decision')
        self.assertEqual({authorization for _, _, authorization in seen}, {'Bearer classifier-secret'})
        self.assertNotIn('classifier-secret', json.dumps(result))
        seen.clear()
        other = await self.classifier_catalog().check({'provider': 'llama_cpp', 'api_base': 'http://127.0.0.1:9999/v1', 'kind': 'decision'},
                                                      transport=httpx.MockTransport(handler))
        self.assertEqual(other['state'], 'decision')
        self.assertEqual({authorization for _, _, authorization in seen}, {None})                  # a key for another address stays there

    def test_an_imported_classifier_endpoint_selects_the_endpoint_as_entered(self):
        self.choose_classifier(provider='openrouter', remote=True)
        preview = self.setup.preview({'classifier_url': 'http://192.168.1.9:8082/v1'})
        self.assertEqual({change['path']: change['after'] for change in preview['changes']},
                         {'roles.classifier.url': 'http://192.168.1.9:8082/v1', 'roles.classifier.provider': ''})
        applied = self.setup.apply({'classifier_url': 'http://192.168.1.9:8082/v1'}, ['roles.classifier.url', 'roles.classifier.provider'], preview['revision'])
        self.assertEqual(applied['config']['roles']['classifier']['url'], 'http://192.168.1.9:8082/v1')    # not re-derived from the provider


if __name__ == '__main__': unittest.main()
