"""Actual host loader/toggle and provider restoration, with empty tmpfs user state."""
import importlib
import asyncio
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


class HostSmoke(unittest.TestCase):
    def test_native_websocket_security_contract(self):
        from helpers import login, ws
        from helpers.modules import load_classes_from_file
        classes=load_classes_from_file('plugins/_convo/api/realtime.py',ws.WsHandler)
        self.assertEqual(len(classes),1)
        handler=classes[0]
        self.assertTrue(handler.requires_auth()); self.assertTrue(handler.requires_csrf())
        with patch.object(login,'get_credentials_hash',return_value='test-identity'):
            for auth, token, client, cookie, expected in (
                (None,'csrf','csrf','csrf','AUTH_REQUIRED'),
                ('wrong','csrf','csrf','csrf','AUTH_REQUIRED'),
                ('test-identity',None,None,None,'CSRF_MISSING'),
                ('test-identity','csrf','wrong','csrf','CSRF_INVALID'),
                ('test-identity','csrf','csrf','wrong','CSRF_COOKIE'),
                ('test-identity','csrf','csrf','csrf',None),
            ):
                context=ws._SecurityContext(auth_hash=auth,csrf_token=token,client_csrf_token=client,
                                            csrf_cookie=cookie,remote_addr='127.0.0.1',api_key=None)
                result=ws._check_security(handler,context)
                self.assertEqual(result['code'] if result else None,expected)
        print('WS_SECURITY_PASS: actual Convo handler discovery and native auth/CSRF admission; no socket started.')

    def test_native_http_auth_and_csrf_protect_diagnostics_and_convo(self):
        from flask import Flask
        from helpers import api, cache, login, plugins
        app = Flask('convo-isolated-auth')
        app.secret_key = 'isolated-test-only-not-a-service-secret'
        app.add_url_rule('/login', 'login_handler', lambda: 'Login')
        app.add_url_rule('/', 'serve_index', lambda: 'Isolated host')
        api.register_api_route(app, threading.RLock())
        with patch.object(plugins, 'send_frontend_reload_notification'), patch.object(login, 'get_credentials_hash', return_value='test-identity'):
            # Doctor is source-mounted read-only and available without a toggle
            # override; do not write a switch file into that source mount.
            self.assertNotEqual(plugins.get_toggle_state('plugin_doctor'), 'disabled')
            plugins.toggle_plugin('_convo', True)
            client = app.test_client()
            for endpoint, payload in (
                ('plugin_doctor/diagnose', {'action':'list'}),
                ('_convo/conversation', {'action':'status'}),
            ):
                # Exercise actual route discovery/wrapping without starting a web server.
                cache.clear(api.CACHE_AREA)
                with client.session_transaction() as session: session.clear()
                url = '/api/plugins/' + endpoint
                self.assertEqual(client.post(url, json=payload).status_code, 302)
                with client.session_transaction() as session: session['authentication']='test-identity'
                self.assertEqual(client.post(url, json=payload).status_code, 403)
                with client.session_transaction() as session: session['csrf_token']='test-csrf'
                self.assertEqual(client.post(url, json=payload, headers={'X-CSRF-Token':'wrong'}).status_code, 403)
                result=client.post(url, json=payload, headers={'X-CSRF-Token':'test-csrf'})
                self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
                self.assertTrue(result.get_json()['ok'])
            plugins.toggle_plugin('_convo', False)
            cache.clear(api.CACHE_AREA)
        print('HTTP_SECURITY_PASS: real host routes reject missing auth/CSRF and accept authenticated requests; no server started.')

    def test_doctor_refresh_rejects_active_unknown_and_unconfirmed_targets(self):
        from helpers import plugins
        from usr.plugins.plugin_doctor.api.diagnose import Diagnose
        target=Path('usr/plugins/convo_refresh_fixture'); target.mkdir(parents=True, exist_ok=True)
        (target/'plugin.yaml').write_text('name: convo_refresh_fixture\ntitle: Fixture\n')
        handler=Diagnose.__new__(Diagnose)
        for toggle, confirmed in (('enabled',True), ('always_enabled',True), ('unknown',True), ('disabled',False), ('disabled','true')):
            with self.subTest(toggle=toggle,confirmed=confirmed), patch.object(plugins,'get_toggle_state',return_value=toggle), patch.object(plugins,'after_plugin_change') as refresh:
                with self.assertRaises(ValueError):
                    asyncio.run(handler.process({'action':'refresh_disabled','target':'convo_refresh_fixture','confirmed':confirmed},None))
                refresh.assert_not_called()
        with patch.object(plugins,'get_toggle_state',side_effect=RuntimeError('broken loader')), patch.object(plugins,'after_plugin_change') as refresh:
            with self.assertRaises(ValueError):
                asyncio.run(handler.process({'action':'refresh_disabled','target':'convo_refresh_fixture','confirmed':True},None))
            refresh.assert_not_called()
        for name in ('plugin_doctor','../escape'):
            with self.assertRaises(ValueError):
                asyncio.run(handler.process({'target':name},None))

    def test_doctor_diagnoses_disabled_broken_plugin_without_target_import(self):
        from helpers import plugins
        from usr.plugins.plugin_doctor.api.diagnose import Diagnose
        target = Path('usr/plugins/convo_broken_fixture')
        target.mkdir(parents=True,exist_ok=True)
        (target/'plugin.yaml').write_text('name: convo_broken_fixture\ntitle: Broken fixture\n')
        (target/'.toggle-0').write_text('')
        (target/'hooks.py').write_text('raise RuntimeError("Never execute")\ndef broken(:\n')
        (target/'config.json').write_text('{"private_token":"do-not-return"}')
        handler = Diagnose.__new__(Diagnose)
        report = asyncio.run(handler.process({'target':'convo_broken_fixture'},None))
        self.assertEqual(report['toggle'],'disabled')
        self.assertFalse(report['report']['passed'])
        self.assertNotIn('do-not-return',json.dumps(report))
        with self.assertRaises(ValueError):
            asyncio.run(handler.process({'action':'refresh_disabled','target':'convo_broken_fixture'},None))
        # A test-owned source repair, not code execution by the diagnostic API.
        (target/'hooks.py').write_text('value = "fixed"\n')
        with patch.object(plugins,'send_frontend_reload_notification'):
            result=asyncio.run(handler.process({'action':'refresh_disabled','target':'convo_broken_fixture','confirmed':True},None))
        self.assertEqual(result['toggle'],'disabled')
        self.assertTrue(asyncio.run(handler.process({'target':'convo_broken_fixture'},None))['report']['passed'])
        self.assertFalse(any('convo_broken_fixture' in key for key in sys.modules))
        print('DOCTOR_PASS: diagnose broken OFF plugin, repair, refresh, remain OFF; target never imported.')

    def test_host_loader_disable_fix_reenable(self):
        from helpers import plugins
        from plugins._convo.helpers import lifecycle
        roots = plugins.get_plugin_roots()
        self.assertTrue(any('plugins' in str(root) for root in roots))
        # Quiet only frontend notification delivery: actual toggle files, host
        # extension discovery, config reader and cache invalidation stay real.
        with patch.object(plugins, 'send_frontend_reload_notification'):
            # Snapshot native settings in empty test-owned user storage. No real
            # profile or settings mount is present in this container.
            files={}
            for native in ('_kokoro_tts','_whisper_stt','_enhanced_speech'):
                path=Path('usr/plugins')/native/'config.json'; path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps({'unknown_field':'keep', 'voice':'fixture', 'voice_blend':[]}),encoding='utf-8')
                files[path]=path.read_bytes()
            plugins.toggle_plugin('_convo', True)
            self.assertNotEqual(plugins.get_toggle_state('_convo'), 'disabled')
            providers = []
            for name, fields in (
                ('plugins._kokoro_tts.helpers.runtime', ['normalize_config','get_config','synthesize_sentences','is_downloaded']),
                ('plugins._whisper_stt.helpers.runtime', ['normalize_config']),
            ):
                module = importlib.import_module(name)
                providers.extend((module, field, getattr(module,field)) for field in fields)
                if hasattr(module,'whisper'):
                    providers.append((module.whisper,'load_model',module.whisper.load_model))
            absent=object()
            for name, fields in (
                ('helpers.settings', ['get_settings','get_stt_device_options','get_tts_device_options','get_stt_defaults','get_tts_defaults','_agentspine_convo_patched']),
                ('helpers.build_type', ['get_tts_device_options','get_tts_defaults']),
            ):
                try: module=importlib.import_module(name)
                except ImportError: continue
                providers.extend((module,field,getattr(module,field,absent)) for field in fields)
            for cycle in range(3):
                plugins.toggle_plugin('_convo', True)
                self.assertFalse(lifecycle.state().blocked)
                for name in ('kokoro_adapter', 'whisper_adapter'):
                    self.assertTrue(importlib.import_module('plugins._convo.helpers.'+name).patch_runtime())
                for name in ('runtime_capabilities','remote_tts'):
                    importlib.import_module('plugins._convo.helpers.'+name).patch_runtime()
                self.assertTrue(lifecycle.state().patches)
                plugins.toggle_plugin('_convo', False)
                self.assertEqual(plugins.get_toggle_state('_convo'), 'disabled')
                self.assertTrue(lifecycle.state().blocked)
                self.assertEqual(lifecycle.state().patches, [])
                for module, field, original in providers:
                    self.assertIs(getattr(module,field,absent),original,field)
                for path, original in files.items(): self.assertEqual(path.read_bytes(),original)
                # Late hooks from cached modules cannot repatch an OFF plugin.
                for name in ('kokoro_adapter','whisper_adapter','runtime_capabilities','remote_tts'):
                    self.assertFalse(importlib.import_module('plugins._convo.helpers.'+name).patch_runtime())
                self.assertEqual(lifecycle.state().patches, [])
            self.assertFalse(any('_convo' in p for p in plugins.get_enabled_plugin_paths(None)))
            print('HOST_RECOVERY_PASS: three enable/disable cycles; all speech hooks and native settings restored; OFF hooks inert; same process/image.')


if __name__ == '__main__':
    if os.environ.get('CONVO_ISOLATED_TEST') != '1':
        raise SystemExit('Only run via the isolated in-image runner.')
    # Do not start app initialization, agents, model workers, sockets or watchers.
    unittest.main()
