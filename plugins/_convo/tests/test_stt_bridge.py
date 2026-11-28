"""The Whisper settings bridge (api/stt_config.py, api/stt_status.py, helpers/config.py) and Convo's drafting flag.

Agent Zero's Whisper stores only send|draft (its store turns anything else into send), so "Draft in composer (continuous)" is the
host's draft plus Convo's own `voice_message_draft`, written in ONE Convo settings transaction that advances its revision so open
Convo editors notice. Temporary files and a fake host only (see setup_harness); nothing installed is read or written."""
import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__:
    from .setup_harness import ROOT, SetupHarness
else:
    from setup_harness import ROOT, SetupHarness


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Gpu:
    @staticmethod
    def resolve_local_device(device):
        return 'cpu', 'CPU'

    @staticmethod
    def get_cuda_devices():
        return []


class SttBridgeTests(SetupHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name) / 'a0'
        (self.root / 'plugins').mkdir(parents=True)
        (self.root / 'usr' / 'plugins').mkdir(parents=True)
        # Convo's file is the one every settings writer shares (the harness's transactions guard it); Whisper's is the host's own.
        self.path = self.root / 'usr' / 'plugins' / '_convo' / 'config.json'
        self.whisper_path = self.root / 'usr' / 'plugins' / '_whisper_stt' / 'config.json'
        self.store.atomic_write(self.path, {**self.initial, 'stt': {'provider': 'whisper', 'whisper': {'model_size': 'small', 'language': 'en', 'message_mode': 'send'}}})
        self.store.atomic_write(self.whisper_path, {'model_size': 'small', 'language': 'en', 'message_mode': 'send', 'host_only': 'keep'})
        api = types.ModuleType('helpers.api')
        api.ApiHandler = object
        api.Request = object
        modules = patch.dict(sys.modules, {'helpers.api': api})
        modules.start(); self.addCleanup(modules.stop)
        self.config = load('convo_stt_bridge_config', ROOT / 'helpers' / 'config.py')
        self.route = load('convo_stt_bridge_route', ROOT / 'api' / 'stt_config.py')
        self.status = load('convo_stt_bridge_status', ROOT / 'api' / 'stt_status.py')
        paths = {'_convo': self.path, '_whisper_stt': self.whisper_path}
        for target, name, value in ((self.config, '_transactions', lambda: self.store), (self.config, '_config_path', lambda plugin: paths.get(plugin)),
                                    (self.route, '_config_helper', lambda: self.config), (self.route, '_gpu_helper', lambda: Gpu),
                                    (self.route, '_runtime_root', lambda: self.root), (self.route, '_whisper_enabled', lambda: True),
                                    (self.status, '_config_helper', lambda: self.config), (self.status, '_gpu_helper', lambda: Gpu),
                                    (self.status, '_runtime_root', lambda: self.root), (self.status, '_whisper_is_enabled', lambda: True)):
            patcher = patch.object(target, name, new=value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.transactions = []
        original = self.store.transaction
        def counted(path, update, expected=None):
            self.transactions.append(Path(path))
            return original(path, update, expected)
        patcher = patch.object(self.store, 'transaction', new=counted)
        patcher.start(); self.addCleanup(patcher.stop)

    def save(self, **values):
        return asyncio.run(self.route.SttConfig().process(values, None))

    def read_status(self):
        return asyncio.run(self.status.SttStatus().process({}, None))

    def convo(self):
        return self.store.read(self.path)

    def whisper(self):
        return json.loads(self.whisper_path.read_text(encoding='utf-8'))

    def test_continuous_is_host_draft_plus_the_convo_flag_in_one_transaction_that_advances_the_revision(self):
        before = self.convo()
        result = self.save(message_mode='continuous')
        after = self.convo()
        self.assertEqual(self.whisper()['message_mode'], 'draft')                     # Agent Zero's own value: it knows only send|draft
        self.assertEqual(self.whisper()['host_only'], 'keep')
        self.assertEqual(after['stt']['whisper']['message_mode'], 'draft')
        self.assertEqual(after['convo']['voice_message_draft'], 'continuous')
        self.assertEqual(self.transactions, [self.path])                               # ONE settings transaction on Convo's file
        self.assertEqual(after['_convo_write_epoch'], before.get('_convo_write_epoch', 0) + 1)
        self.assertNotEqual(self.store.settings_revision(after), self.store.settings_revision(before))   # open Convo editors notice
        self.assertEqual(result['convo_revision'], self.store.settings_revision(after))
        self.assertEqual((result['voice_message_draft'], result['config']['message_mode']), ('continuous', 'draft'))
        self.assertEqual(after['convo']['style'], 'existing persona')                  # the rest of Convo's settings is kept
        self.assertEqual(after['convo']['future_option'], {'keep': 17})
        self.assertEqual(after['convo']['roles']['tts']['voice'], 'clone:old')
        self.assertEqual((after['native'], after['future_top']), (before['native'], before['future_top']))
        self.assertEqual(after['stt']['whisper']['model_size'], 'small')

    def test_send_and_draft_are_that_host_mode_with_a_single_draft(self):
        self.save(message_mode='continuous')
        epoch = self.convo()['_convo_write_epoch']
        result = self.save(message_mode='draft')
        self.assertEqual((self.whisper()['message_mode'], self.convo()['convo']['voice_message_draft'], result['voice_message_draft']), ('draft', 'single', 'single'))
        self.assertEqual(self.convo()['_convo_write_epoch'], epoch + 1)
        revision = self.store.settings_revision(self.convo())
        result = self.save(message_mode='send')
        self.assertEqual((self.whisper()['message_mode'], self.convo()['stt']['whisper']['message_mode'], self.convo()['convo']['voice_message_draft']), ('send', 'send', 'single'))
        # the flag was single already: Convo's settings did not change, so its editors are not told they did
        self.assertEqual((self.convo()['_convo_write_epoch'], self.store.settings_revision(self.convo()), result['convo_revision']), (epoch + 1, revision, revision))

    def test_a_save_without_a_message_mode_or_with_the_same_one_leaves_the_flag_and_the_revision_alone(self):
        self.save(message_mode='continuous')
        stored = self.convo()
        self.save(device='cpu', silence_duration=1200)
        self.save(message_mode='continuous')
        after = self.convo()
        self.assertEqual(after['convo'], stored['convo'])
        self.assertEqual(after['_convo_write_epoch'], stored['_convo_write_epoch'])
        self.assertEqual((after['stt']['whisper']['device'], after['stt']['whisper']['silence_duration'], self.whisper()['message_mode']), ('cpu', 1200, 'draft'))

    def test_the_host_value_is_never_continuous(self):
        result = self.save(message_mode='continuous', model_size='base')
        status = self.read_status()
        for where, value in (('provider file', self.whisper()['message_mode']), ('Convo stt node', self.convo()['stt']['whisper']['message_mode']),
                             ('save answer', result['config']['message_mode']), ('status answer', status['config']['message_mode'])):
            self.assertEqual(value, 'draft', where)
        # The normalizer still accepts only send|draft: handing it 'continuous' would silently mean "send immediately".
        self.assertEqual(self.config.normalize_whisper_config({'message_mode': 'continuous'})['message_mode'], 'send')
        self.assertEqual(self.config.split_message_mode('continuous'), ('draft', 'continuous'))
        self.assertEqual(self.config.split_message_mode('Draft'), ('draft', 'single'))
        self.assertEqual(self.config.split_message_mode('nonsense'), ('send', 'single'))

    def test_status_reports_the_flag_beside_the_host_mode(self):
        self.assertEqual(self.read_status()['voice_message_draft'], 'single')               # absent = Agent Zero's own draft behaviour
        self.save(message_mode='continuous')
        status = self.read_status()
        self.assertEqual((status['voice_message_draft'], status['config']['message_mode']), ('continuous', 'draft'))
        self.assertNotIn('voice_message_draft', status['config'])                           # the host store's config stays the host's
        self.assertEqual(self.save()['voice_message_draft'], 'continuous')                  # the bridge's read answers it too
        stored = self.convo(); stored['convo']['voice_message_draft'] = 'sometimes'
        self.store.atomic_write(self.path, stored)
        self.assertEqual(self.read_status()['voice_message_draft'], 'single')

    def test_invalid_convo_settings_refuse_the_change_and_write_nothing(self):
        stored = self.convo(); stored['convo']['hotkey'] = 'Nope+X'
        self.store.atomic_write(self.path, stored)
        before_convo, before_whisper = self.path.read_bytes(), self.whisper_path.read_bytes()
        with self.assertRaises(ValueError):
            self.save(message_mode='continuous')
        self.assertEqual((self.path.read_bytes(), self.whisper_path.read_bytes()), (before_convo, before_whisper))

    def test_the_host_save_path_maps_continuous_too_and_repairs_the_host_value(self):
        # A save through Agent Zero's generic plugin route writes the modal's value as is; Convo's end hook then makes it host draft.
        self.store.atomic_write(self.whisper_path, {**self.whisper(), 'message_mode': 'continuous'})
        self.config.sync_enhanced_from_provider('_whisper_stt', {'model_size': 'small', 'language': 'en', 'message_mode': 'continuous'})
        after = self.convo()
        self.assertEqual((self.whisper()['message_mode'], after['stt']['whisper']['message_mode'], after['convo']['voice_message_draft']), ('draft', 'draft', 'continuous'))
        self.assertEqual(self.whisper()['host_only'], 'keep')
        epoch = after['_convo_write_epoch']
        self.config.sync_enhanced_from_provider('_whisper_stt', {'model_size': 'small', 'language': 'en', 'message_mode': 'send'})
        after = self.convo()
        self.assertEqual((after['stt']['whisper']['message_mode'], after['convo']['voice_message_draft'], after['_convo_write_epoch']), ('send', 'single', epoch + 1))
        self.config.sync_enhanced_from_provider('_whisper_stt', {'language': 'fr'})        # no mode in the save: the flag is left alone
        self.assertEqual((self.convo()['convo']['voice_message_draft'], self.convo()['_convo_write_epoch']), ('single', epoch + 1))


if __name__ == '__main__':
    unittest.main()
