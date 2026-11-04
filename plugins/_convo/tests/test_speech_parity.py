"""Offline native speech contract and exact patch-ownership regression tests."""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Import native dependencies before patch.dict snapshots the host registry.
# Removing freshly imported NumPy C extensions on cleanup cannot unload their
# process-level state and makes the next test attempt an invalid second import.
import numpy
import soundfile

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('convo_parity_' + name, ROOT / 'helpers' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_immediate(coroutine):
    """These guarded paths never await I/O; avoid even loopback event-loop sockets."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError('Offline speech path unexpectedly suspended')


class SpeechParityTests(unittest.TestCase):
    def setUp(self):
        # Never import the host settings stack or read its persisted configuration.
        host = types.ModuleType('helpers')
        host.plugins = types.SimpleNamespace(get_toggle_state=lambda _: 'enabled')
        host.runtime = types.ModuleType('helpers.runtime')
        self.modules = patch.dict(sys.modules, {'helpers': host, 'helpers.plugins': host.plugins,
                                              'helpers.runtime': host.runtime})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.config = load('config')
        self.read = patch.object(self.config, '_plugin_config', return_value={})
        self.read.start()
        self.addCleanup(self.read.stop)

    def test_native_weights_override_aliases_and_normalize_idempotently(self):
        cfg = self.config.normalize_kokoro_config({'voice': 'af_heart', 'voice_weights': {'bm_george': 2, 'af_nova': 7},
            'primary_voice': 'am_adam', 'secondary_voice': 'am_echo', 'voice_blend': 90,
            'tts_kokoro_voice': 'am_santa', 'custom': {'keep': True}, 'device': 'cpu', 'speed': 1.3})
        self.assertEqual(cfg['voice'], 'bm_george,af_nova')
        self.assertEqual(cfg['voice_weights'], {'bm_george': 2, 'af_nova': 7})
        self.assertEqual(cfg['custom'], {'keep': True})
        self.assertEqual(self.config.normalize_kokoro_config(cfg), cfg)

    def test_explicit_empty_weights_never_revive_legacy_pair(self):
        for voice in ('af_nova', 'af_nova,bm_george'):
            cfg = self.config.normalize_kokoro_config({'voice': voice, 'voice_weights': {}, 'secondary_voice': 'am_echo', 'voice_blend': 90})
            self.assertEqual((cfg['voice'], cfg['voice_weights']), (voice, {}))

    def test_legacy_pair_migrates_once_and_weight_maps_replace(self):
        cfg = self.config.normalize_kokoro_config({'voice': 'af_nova', 'secondary_voice': 'bm_george', 'voice_blend': 30})
        self.assertEqual(cfg['voice_weights'], {'af_nova': 3, 'bm_george': 7})
        self.assertEqual(self.config.normalize_kokoro_config(cfg), cfg)
        for weights in ({}, {'af_heart': 4}):
            self.assertEqual(self.config._deep_merge({'voice_weights': {'am_echo': 8}}, {'voice_weights': weights})['voice_weights'], weights)

    def test_provider_legacy_edit_replaces_migrated_weights_without_losing_convo(self):
        current = {'convo': {'enabled': True}, 'tts': {'kokoro': {'voice_weights': {'am_echo': 8}, 'custom': 'keep'}}}
        writes = []
        with patch.object(self.config, '_plugin_config', return_value=current), patch.object(self.config, '_write_plugin_config', side_effect=lambda name, value: writes.append(value)):
            self.config.sync_enhanced_from_provider('_kokoro_tts', {'voice': 'af_nova', 'secondary_voice': 'bm_george', 'voice_blend': 30})
        self.assertEqual(writes[0]['tts']['kokoro']['voice_weights'], {'af_nova': 3, 'bm_george': 7})
        self.assertEqual(writes[0]['tts']['kokoro']['custom'], 'keep')
        self.assertEqual(writes[0]['convo'], {'enabled': True})

    def test_invalid_weights_and_atomic_write(self):
        cfg = self.config.provider_kokoro_config({'voice': 'af_nova', 'voice_weights': {'af_nova': 2, 'bm_george': float('nan'), 'bad': 3, 'am_echo': -1}, 'unrelated': 42})
        self.assertEqual(cfg['voice_weights'], {'af_nova': 2})
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.config, '_config_path', return_value=Path(tmp) / 'test.json'):
            self.config._write_plugin_config('test', cfg)
            self.assertEqual(json.loads((Path(tmp) / 'test.json').read_text()), cfg)
            self.assertEqual(list(Path(tmp).glob('*.tmp')), [])

    def test_remote_translation_and_rejection(self):
        adapter = load('kokoro_adapter')
        self.assertEqual(adapter.remote_voice_pair({'voice_weights': {'af_nova': 3, 'bm_george': 7}}), ('af_nova', 'bm_george', 30))
        self.assertEqual(adapter.remote_voice_pair({'voice': 'af_nova,bm_george', 'voice_weights': {}}), ('af_nova', 'bm_george', 50))
        self.assertEqual(adapter.remote_voice_pair({'voice': 'af_nova'}), ('af_nova', '', 100))
        for cfg in ({'voice': 'af_nova,bm_george,am_echo'}, {'voice_weights': {'af_nova': 2, 'bm_george': 7}}, {'voice_weights': {'af_nova': .1, 'bm_george': 10}}):
            with self.assertRaises(ValueError):
                adapter.remote_voice_pair(cfg)

    def test_native_resolver_and_no_load_guard_cover_all_weighted_voices(self):
        adapter = load('kokoro_adapter')
        seen = []
        class Pipeline:
            voices = {'af_nova': 1, 'bm_george': 2}
            def __call__(self, text, **kwargs):
                seen.append((text, kwargs))
                return []
        pipeline = Pipeline()
        def resolver(pipeline, voice, weights):
            seen.append((voice, weights))
            return 'native-pack'
        runtime = types.SimpleNamespace(_pipeline=pipeline, _resolve_voice=resolver)
        cfg = {'voice': 'af_nova,bm_george', 'voice_weights': {'af_nova': 2, 'bm_george': 7}, 'speed': 1.4,
               '_convo_no_load': True, '_convo_pipeline': pipeline}
        run_immediate(adapter._synthesize_local(runtime, ['hello'], cfg))
        self.assertEqual(seen, [('af_nova,bm_george', cfg['voice_weights']), ('hello', {'voice': 'native-pack', 'speed': 1.4})])
        with self.assertRaisesRegex(ValueError, 'Prepare the selected'):
            run_immediate(adapter._ensure_pipeline_for_device(runtime, {**cfg, 'voice_weights': {'am_echo': 1}}))

    def test_kokoro_compatibility_marker_and_functions_restore_on_off(self):
        adapter = load('kokoro_adapter')
        runtime = types.ModuleType('plugins._kokoro_tts.helpers.runtime')
        runtime.normalize_config = lambda cfg=None: cfg
        original = runtime.normalize_config
        with patch.dict(sys.modules, {'plugins._kokoro_tts.helpers.runtime': runtime}):
            control = adapter.lifecycle.state()
            with patch.object(control, 'watch'):
                for _ in range(2):
                    control.blocked = False  # Completed explicit native ON.
                    self.assertTrue(adapter.patch_runtime())
                    self.assertTrue(runtime._agentspine_enhanced_speech_patched)
                    self.assertIsNot(runtime.normalize_config, original)
                    control.disable()
                    self.assertIs(runtime.normalize_config, original)
                    self.assertFalse(hasattr(runtime, '_agentspine_enhanced_speech_patched'))

    def test_whisper_compatibility_marker_and_device_patch_restore_on_off(self):
        adapter = load('whisper_adapter')
        runtime = types.ModuleType('plugins._whisper_stt.helpers.runtime')
        runtime.normalize_config = lambda cfg=None: dict(cfg or {})
        runtime.whisper = types.SimpleNamespace(load_model=lambda **kwargs: kwargs)
        original = runtime.whisper.load_model
        with patch.dict(sys.modules, {'plugins._whisper_stt.helpers.runtime': runtime}):
            control = adapter._lifecycle().state()
            with patch.object(control, 'watch'), patch.object(adapter.speech_config, 'whisper_runtime_config', return_value={'device': 'cpu'}), patch.object(adapter, '_resolve_device', return_value=('cpu', 'CPU')):
                self.assertTrue(adapter.patch_runtime())
                self.assertTrue(runtime._agentspine_enhanced_speech_whisper_patched)
                self.assertEqual(runtime.whisper.load_model()['device'], 'cpu')
                control.disable()
                self.assertIs(runtime.whisper.load_model, original)
                self.assertFalse(hasattr(runtime, '_agentspine_enhanced_speech_whisper_patched'))

    def test_remote_rejection_precedes_worker_and_preserves_existing_marker(self):
        adapter = load('kokoro_adapter')
        runtime = types.ModuleType('plugins._kokoro_tts.helpers.runtime')
        runtime._agentspine_enhanced_speech_patched = True
        cfg = {'voice': 'af_nova,bm_george,am_echo', 'voice_weights': {}, 'device': 'remote', 'remote_enabled': True}
        with patch.dict(sys.modules, {'plugins._kokoro_tts.helpers.runtime': runtime}), patch.object(adapter.speech_config, 'kokoro_runtime_config', return_value=cfg), patch.object(adapter.remote_worker, 'synthesize', side_effect=AssertionError('Worker contacted')):
            control = adapter.lifecycle.state()
            with patch.object(control, 'watch'):
                adapter.patch_runtime()
                with self.assertRaisesRegex(ValueError, 'one or two voices'):
                    run_immediate(runtime.synthesize_sentences(['hello']))
                replacement = lambda: 'another owner'
                runtime.get_config = replacement
                control.disable()
                self.assertIs(runtime.get_config, replacement)
                self.assertTrue(runtime._agentspine_enhanced_speech_patched)

    def test_settings_compatibility_marker_restores_without_native_write(self):
        settings = types.ModuleType('helpers.settings')
        settings.get_tts_device_options = lambda: []
        settings.get_tts_defaults = lambda: {}
        settings.get_settings = lambda: {}
        build_type = types.ModuleType('helpers.build_type')
        build_type.get_tts_device_options = settings.get_tts_device_options
        build_type.get_tts_defaults = settings.get_tts_defaults
        with patch.dict(sys.modules, {'helpers.settings': settings, 'helpers.build_type': build_type}):
            adapter = load('remote_tts')
            control = adapter._lifecycle().state()
            original = settings.get_settings
            with patch.object(control, 'watch'):
                self.assertTrue(adapter.patch_runtime())
                self.assertTrue(settings._agentspine_enhanced_speech_patched)
                control.disable()
                self.assertIs(settings.get_settings, original)
                self.assertFalse(hasattr(settings, '_agentspine_enhanced_speech_patched'))


if __name__ == '__main__':
    unittest.main()
