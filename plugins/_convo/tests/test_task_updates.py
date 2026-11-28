"""Which delegated-job events are spoken, per `spoken_updates` level, and honest job-status wording."""
import unittest

from plugins._convo.helpers import convo_task_updates as updates
from plugins._convo.helpers.convo_contract import SPOKEN_UPDATES


class TaskUpdates(unittest.TestCase):
    def test_permission_error_and_decision_are_spoken_at_every_level(self):
        for level in SPOKEN_UPDATES:
            for kind in ('permission', 'failed', 'decision_needed'):
                result = updates.should_speak_update(level, kind)
                self.assertTrue(result.speak, (level, kind)); self.assertEqual(result.reason, 'always_spoken')

    def test_completion_speaks_only_the_outcome(self):
        speak = {k for k in updates.KINDS if updates.should_speak_update('completion', k).speak}
        self.assertEqual(speak, {'completed', 'failed', 'permission', 'decision_needed'})

    def test_milestones_add_results_and_blockers_but_not_routine_progress(self):
        speak = {k for k in updates.KINDS if updates.should_speak_update('milestones', k).speak}
        self.assertEqual(speak, {'completed', 'failed', 'permission', 'decision_needed', 'milestone', 'blocked'})
        for routine in ('started', 'progress'):
            result = updates.should_speak_update('milestones', routine)
            self.assertFalse(result.speak); self.assertEqual(result.reason, 'text_only_at_milestones')

    def test_frequent_adds_progress_but_never_every_step(self):
        self.assertTrue(updates.should_speak_update('frequent', 'progress').speak)
        self.assertTrue(updates.should_speak_update('frequent', {'kind': 'progress', 'seconds_since_last_spoken': 60}).speak)
        quick = updates.should_speak_update('frequent', {'kind': 'progress', 'seconds_since_last_spoken': 3})
        self.assertFalse(quick.speak); self.assertEqual(quick.reason, 'rate_limited')
        # An outcome is never throttled.
        self.assertTrue(updates.should_speak_update('frequent', {'kind': 'completed', 'seconds_since_last_spoken': 0}).speak)
        self.assertTrue(updates.should_speak_update('frequent', {'kind': 'permission', 'seconds_since_last_spoken': 0}).speak)

    def test_unknown_events_and_levels_fail_closed(self):
        self.assertFalse(updates.should_speak_update('frequent', 'queued').speak)
        self.assertFalse(updates.should_speak_update('frequent', 'something_new').speak)
        self.assertFalse(updates.should_speak_update('frequent', None).speak)
        self.assertEqual(updates.should_speak_update('bogus', 'completed'), updates.should_speak_update('completion', 'completed'))
        self.assertFalse(updates.should_speak_update('bogus', 'milestone').speak)         # unknown level means the default level

    def test_result_is_a_small_typed_value(self):
        result = updates.should_speak_update('completion', 'completed')
        self.assertIsInstance(result, updates.UpdateDecision)
        self.assertEqual((result.speak, bool(result.reason)), (True, True))
        with self.assertRaises(Exception):
            result.speak = False                                                          # frozen

    def test_store_statuses_map_honestly(self):
        self.assertEqual(updates.event_kind('completed'), 'completed')
        self.assertEqual(updates.event_kind('running'), 'started')
        self.assertEqual(updates.event_kind('uncertain'), 'failed')                       # an unconfirmed outcome is reported as an error
        for status in ('queued', 'dispatching'):
            self.assertFalse(updates.should_speak_update('frequent', updates.event_kind(status)).speak)
        self.assertFalse(updates.should_speak_update('frequent', updates.event_kind('cancelled')).speak)
        self.assertFalse(updates.should_speak_update('frequent', updates.event_kind('???')).speak)

    def test_only_a_terminal_status_is_worded_as_finished(self):
        for status in ('queued', 'dispatching', 'running', 'cancel_requested'):
            words = updates.notice_instruction(status)
            self.assertNotIn('finished', words); self.assertNotIn('completed', words)
        self.assertIn('finished', updates.notice_instruction('completed'))
        self.assertIn('failed', updates.notice_instruction('failed'))
        self.assertNotIn('finished', updates.notice_instruction('uncertain'))
        self.assertIn('unknown', updates.notice_instruction('uncertain'))


if __name__ == '__main__':
    unittest.main()
