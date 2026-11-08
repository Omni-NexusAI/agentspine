"""setup_patch: compare-and-set per setting path under the shared settings lock (see convo_setup.patch), on temporary
settings with a mocked host (see setup_harness)."""
import copy
import json
import sys
import threading
import types
import unittest

if __package__:
    from .setup_harness import SetupHarness
else:
    from setup_harness import SetupHarness


class PatchTests(SetupHarness, unittest.TestCase):
    def stored(self):
        return self.store.read(self.path)

    def test_a_change_applies_where_the_saved_value_still_equals_its_base_and_everything_else_is_kept(self):
        before = self.stored()
        result = self.setup.patch([{'path': 'roles.conversation.model', 'base': 'local-model', 'value': 'omni'},
                                   {'path': 'voice_message_draft', 'base': 'single', 'value': 'continuous'}])
        after = self.stored()
        self.assertEqual((result['applied'], result['conflicts']), (['roles.conversation.model', 'voice_message_draft'], []))
        self.assertEqual((after['convo']['roles']['conversation']['model'], after['convo']['voice_message_draft']), ('omni', 'continuous'))
        self.assertEqual(after['native'], before['native'])                                   # other plugins' and native nodes untouched
        self.assertEqual(after['future_top'], before['future_top'])
        self.assertEqual(after['convo']['future_option'], {'keep': 17})                        # unknown Convo fields survive
        self.assertEqual(after['convo']['style'], 'existing persona')                         # untouched paths keep their value
        self.assertEqual(after['convo']['roles']['tts']['voice'], 'clone:old')
        self.assertEqual(after['_convo_write_epoch'], before.get('_convo_write_epoch', 0) + 1)  # every open editor notices
        self.assertEqual(result['revision'], self.store.settings_revision(after))
        self.assertEqual(result['config']['voice_message_draft'], 'continuous')               # the answer is a full settings read
        self.assertIn('readiness', result)

    def test_a_stale_base_is_a_conflict_and_leaves_that_path_alone(self):
        result = self.setup.patch([{'path': 'style', 'base': 'what this editor saw long ago', 'value': 'mine'},
                                   {'path': 'hotkey', 'base': 'Ctrl+Shift+Space', 'value': 'Ctrl+Alt+K'}])
        self.assertEqual(result['conflicts'], [{'path': 'style', 'mine': 'mine', 'theirs': 'existing persona'}])
        self.assertEqual(result['applied'], ['hotkey'])
        self.assertEqual((self.stored()['convo']['style'], self.stored()['convo']['hotkey']), ('existing persona', 'Ctrl+Alt+K'))

    def test_nothing_is_written_when_nothing_applies(self):
        before = self.path.read_bytes()
        result = self.setup.patch([{'path': 'style', 'base': 'stale', 'value': 'mine'}])
        self.assertEqual((result['applied'], len(result['conflicts'])), ([], 1))
        self.assertEqual(self.path.read_bytes(), before)

    def test_absent_and_removed_paths(self):
        self.setup.patch([{'path': 'roles.tts.saved', 'value': {'openai': {'voice': 'alloy'}}}])          # no base: it was absent
        self.assertEqual(self.stored()['convo']['roles']['tts']['saved'], {'openai': {'voice': 'alloy'}})
        again = self.setup.patch([{'path': 'roles.tts.saved', 'value': {'openai': {'voice': 'echo'}}}])    # absent no longer: conflict
        self.assertEqual(again['conflicts'][0]['theirs'], {'openai': {'voice': 'alloy'}})
        self.setup.patch([{'path': 'roles.tts.saved', 'base': {'openai': {'voice': 'alloy'}}}])          # no value: removed
        self.assertNotIn('saved', self.stored()['convo']['roles']['tts'])                                # a removal survives the write
        self.setup.patch([{'path': 'style', 'base': 'existing persona'}])                                 # removing a default resets it
        self.assertEqual(self.stored()['convo']['style'], '')

    def test_numbers_compare_as_the_browser_sends_them_but_true_is_not_one(self):
        self.setup.patch([{'path': 'policy_gate.ignore_min', 'base': 1, 'value': .95}])                  # stored 1.0, sent as 1
        self.assertEqual(self.stored()['convo']['policy_gate']['ignore_min'], .95)
        result = self.setup.patch([{'path': 'enabled', 'base': 1, 'value': False}])                       # stored true
        self.assertEqual(len(result['conflicts']), 1)
        self.assertIs(self.stored()['convo']['enabled'], True)

    def test_credentials_are_refused_wherever_they_appear_and_nothing_is_written(self):
        before = self.path.read_bytes()
        for change in ({'path': 'roles.conversation.api_key', 'value': 'not-allowed'},
                       {'path': 'roles.tts', 'base': {}, 'value': {'voice': 'x', 'token': 'not-allowed'}},
                       {'path': 'roles.conversation._convo_key', 'value': 'not-allowed'},
                       {'path': 'sidecar.authorization', 'value': 'not-allowed'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.setup.patch([change])
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_result_is_validated_like_a_full_save(self):
        before = self.path.read_bytes()
        for change in ({'path': 'policy_gate.respond_min', 'base': .3, 'value': 1.5},
                       {'path': 'voice_message_draft', 'base': 'single', 'value': 'always'},
                       {'path': 'decider.url', 'base': '', 'value': 'https://example.com:9931'},          # public, no consent
                       {'path': 'roles.classifier.provider', 'base': '', 'value': 'jev'},                  # derives a public root, no consent
                       {'path': 'roles.classifier.context_window', 'base': 8192, 'value': 512},
                       {'path': 'roles.tts.provider', 'base': '', 'value': 'strata'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.setup.patch([change])
        self.assertEqual(self.path.read_bytes(), before)

    def test_paths_are_known_settings_and_a_request_is_bounded(self):
        before = self.path.read_bytes()
        bad = [[], 'style', [{'path': 'style'}] * 65, [{'value': 'x'}], [{'path': 'style', 'value': 'x', 'extra': 1}],
               [{'path': 'unknown_top', 'value': 1}], [{'path': 'roles.someone.url', 'value': 'x'}], [{'path': 'roles', 'value': {}}],
               [{'path': 'Style', 'value': 'x'}], [{'path': 'style.', 'value': 'x'}], [{'path': 'a.b.c.d.e.f.g.h.i', 'value': 1}], [{'path': 5}],
               [{'path': 'style', 'base': 'existing persona', 'value': 'a'}, {'path': 'style', 'base': 'existing persona', 'value': 'b'}],
               [{'path': 'roles.tts', 'base': {}, 'value': {}}, {'path': 'roles.tts.voice', 'value': 'x'}]]
        for changes in bad:
            with self.subTest(changes=str(changes)[:80]), self.assertRaises(ValueError):
                self.setup.patch(changes)
        self.assertEqual(self.path.read_bytes(), before)

    def test_ai_link_development_mode_still_needs_voice_to_be_idle(self):
        runtime = sys.modules['helpers.runtime']
        runtime._convo_service = types.SimpleNamespace(connections={'browser': object()}, connection_lock=threading.RLock())
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'End the active'):
            self.setup.patch([{'path': 'ai_link_development_mode', 'base': False, 'value': True}])
        self.assertEqual(self.path.read_bytes(), before)
        runtime._convo_service = types.SimpleNamespace(connections={}, connection_lock=threading.RLock(), retiring_connections={},
                                                      store=types.SimpleNamespace(lock=threading.RLock(), db=types.SimpleNamespace(execute=lambda *a: types.SimpleNamespace(fetchone=lambda: None))))
        self.setup.patch([{'path': 'ai_link_development_mode', 'base': False, 'value': True}])
        self.assertIs(self.stored()['convo']['ai_link_development_mode'], True)

    def test_a_patch_and_a_full_save_see_each_others_revision(self):
        shared = self.setup.read()
        self.setup.patch([{'path': 'hotkey', 'base': 'Ctrl+Shift+Space', 'value': 'Ctrl+Alt+K'}])
        with self.assertRaises(self.store.Conflict):
            self.setup.save(shared['config'], shared['revision'])                             # the full editor must merge first
        latest = self.setup.read()
        changed = copy.deepcopy(latest['config']); changed['style'] = 'from the full editor'
        self.setup.save(changed, latest['revision'])
        result = self.setup.patch([{'path': 'hotkey', 'base': 'Ctrl+Alt+K', 'value': 'Ctrl+Alt+J'}])
        self.assertEqual((result['applied'], self.stored()['convo']['style']), (['hotkey'], 'from the full editor'))
        self.assertNotIn('_convo_key', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
