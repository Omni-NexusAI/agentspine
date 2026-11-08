import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class SettingsBridgeTests(unittest.TestCase):
    def test_native_voice_wins_and_unknown_configuration_survives(self):
        fake_plugins = types.ModuleType('helpers.plugins')
        with patch.dict(sys.modules, {'helpers.plugins':fake_plugins}):
            config = load('convo_test_config', ROOT / 'helpers/config.py')
        values = {'_enhanced_speech': {'legacy_flag':True}, '_convo': {'convo':{'enabled':True}, 'tts':{'kokoro':{'unknown':'keep','primary_voice':'old'}}}, '_kokoro_tts':{'voice':'new','secondary_voice':'second','voice_blend':27}, '_whisper_stt':{'language':'fr'}}
        writes = []
        with patch.object(config, '_plugin_config', side_effect=lambda key:values.get(key,{})), patch.object(config, '_write_plugin_config', side_effect=lambda *args:writes.append(args)):
            result = config.get_effective_config()
            self.assertEqual(result['tts']['kokoro']['voice'], 'new')
            self.assertEqual(result['tts']['kokoro']['unknown'], 'keep')
            self.assertEqual(result['stt']['whisper']['language'], 'fr')
            self.assertTrue(result['legacy_flag']); self.assertTrue(result['convo']['enabled'])
            self.assertEqual(writes, [])
            config.sync_enhanced_from_provider('_kokoro_tts', {'voice':'changed'})
        self.assertEqual(writes[0][1]['tts']['kokoro']['unknown'],'keep')
        self.assertTrue(writes[0][1]['convo']['enabled'])

    def test_general_convo_save_never_rewrites_native_provider(self):
        fake_extension = types.ModuleType('helpers.extension'); fake_extension.Extension = object
        path = ROOT / 'extensions/python/_functions/api/plugins/Plugins/_save_config/end/_10_convo_sync.py'
        with patch.dict(sys.modules, {'helpers.extension':fake_extension}):
            module = load('convo_save_test', path)
        with patch.object(module, '_load_config_helper', side_effect=AssertionError('Native provider was touched')):
            payload = {'plugin_name':'_convo','settings':{'convo':{'enabled':True}},'_agentspine_speech_before':{'voice':'old'}}
            module.EnhancedSpeechSaveSync().execute(data={'args':[None,payload]})
        self.assertNotIn('_agentspine_speech_before',payload)


if __name__ == '__main__': unittest.main()
