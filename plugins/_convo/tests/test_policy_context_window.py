"""The classifier's context window bounds the conversation context the legacy JSON classifier is shown."""
import json
import unittest

from plugins._convo.sidecar import app


class PolicyContextWindow(unittest.TestCase):
    def setUp(self):
        self.payload = {'context': {'recent': ['x' * 50000]}}

    def test_without_a_window_the_previous_fixed_cut_applies(self):
        self.assertEqual(len(app.policy_context(self.payload, {})), 6000)

    def test_a_small_window_cuts_more_and_never_below_the_floor(self):
        self.assertEqual(len(app.policy_context(self.payload, {'context_window': 1024})), 3072)
        self.assertEqual(len(app.policy_context(self.payload, {'context_window': 100})), 1000)

    def test_a_large_window_never_shows_more_than_before(self):
        self.assertEqual(len(app.policy_context(self.payload, {'context_window': 1000000})), 6000)

    def test_only_a_whole_positive_number_counts(self):
        for value in (True, '4096', -5, 0, 4.5, None):
            self.assertEqual(len(app.policy_context(self.payload, {'context_window': value})), 6000, value)

    def test_short_context_is_unchanged(self):
        payload = {'context': {'mode': 'active'}}
        self.assertEqual(app.policy_context(payload, {'context_window': 1024}), json.dumps({'mode': 'active'}))


if __name__ == '__main__':
    unittest.main()
