"""One voice per reply: the complete synthesis values frozen per reply (the speech server's own selection included), whole-second
reference length, the start-up buffer and one language per reply."""
import importlib
import unittest

synthesis = importlib.import_module('plugins._convo.helpers.convo_synthesis')
language = importlib.import_module('plugins._convo.helpers.convo_language')
host = importlib.import_module('plugins._convo.helpers.convo_host')

ROLE = {'provider': 'audiocpp', 'connection': 'external', 'tuning': {'profile_id': '', 'overrides': {}}}
SERVER_SELECTION = {'profile_id': 'low-latency', 'profile_revision': 3,
                    'effective': {'temperature': 1.0, 'top_k': 40, 'first_block_frames': 2, 'steady_block_frames': 8}}


class Freeze(unittest.TestCase):
    def test_with_no_profile_chosen_the_reply_is_frozen_on_the_servers_own_selection(self):
        envelope = synthesis.reply_tuning(ROLE, None, SERVER_SELECTION, rng=lambda bits: 7)
        self.assertEqual((envelope['profile_id'], envelope['profile_revision']), ('low-latency', 3))
        self.assertEqual(envelope['effective'], {**SERVER_SELECTION['effective'], 'seed': 7})
        self.assertEqual(envelope['overrides'], {'seed': 7})

    def test_a_chosen_profile_is_frozen_only_on_its_own_resolution(self):
        chosen = {'profile_id': 'quality', 'overrides': {}}
        stale = synthesis.reply_tuning(ROLE, chosen, SERVER_SELECTION, rng=lambda bits: 7)     # resolved for another profile
        self.assertEqual(stale['profile_id'], 'quality')
        self.assertNotIn('effective', stale)
        quality = {**SERVER_SELECTION, 'profile_id': 'quality', 'profile_revision': 5}
        self.assertEqual(synthesis.reply_tuning(ROLE, chosen, quality, rng=lambda bits: 7)['profile_revision'], 5)

    def test_reference_length_is_whole_seconds_and_the_phrase_queue_controls_are_bounded(self):
        self.assertEqual(synthesis.validate_overrides({'max_reference_seconds': 12}), {'max_reference_seconds': 12})
        with self.assertRaisesRegex(ValueError, 'whole number'):
            synthesis.validate_overrides({'max_reference_seconds': 12.5})
        self.assertEqual(synthesis.validate_overrides({'text_lookahead': 64, 'phrase_flush_ms': 500}),
                         {'text_lookahead': 64, 'phrase_flush_ms': 500})
        for bad in ({'text_lookahead': 8}, {'phrase_flush_ms': 4000}, {'text_lookahead': 64.5}):
            with self.assertRaises(ValueError):
                synthesis.validate_overrides(bad)

    def test_the_start_up_buffer_follows_the_forks_rule(self):
        def envelope(profile, first=2):
            return {'profile_id': profile, 'effective': {'first_block_frames': first}}
        self.assertEqual([synthesis.prime_ms(envelope(p)) for p in ('low-latency', 'Balanced', 'quality', 'low')], [800, 1280, 1760, 800])
        self.assertEqual(synthesis.prime_ms(envelope('my-own', first=5)), 400)
        self.assertEqual(synthesis.prime_ms(envelope('my-own', first=300)), synthesis.PRIME_LIMIT)
        self.assertEqual(synthesis.prime_ms({'profile_id': 'balanced', 'overrides': {}}), 0)             # nothing resolved: no buffer
        self.assertEqual(synthesis.prime_ms(None), 0)


class Language(unittest.TestCase):
    def test_a_clear_sentence_names_its_language(self):
        self.assertEqual(language.detect('Frankfort is the capital of Kentucky, sir.'), 'English')
        self.assertEqual(language.detect('La capital de Kansas es Topeka, y está en el centro.'), 'Spanish')
        self.assertEqual(language.detect("La capitale de la France est Paris, et c'est une très belle ville."), 'French')
        self.assertEqual(language.detect('Die Hauptstadt ist Berlin und sie ist nicht klein.'), 'German')
        self.assertEqual(language.detect('Привет, как дела?'), 'Russian')
        self.assertEqual(language.detect('こんにちは'), 'Japanese')

    def test_a_phrase_too_short_to_tell_names_none(self):
        for text in ('Sure.', 'Okay, sir.', '', 'Topeka.'):
            self.assertEqual(language.detect(text), '')


class Clock(unittest.TestCase):
    def test_the_clock_reads_like_speech(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 11, 9, 20, 41, tzinfo=timezone(timedelta(hours=-5)))
        self.assertEqual(host.clock_text(now, 'America/New_York'), 'Monday, 9 November 2026, 8:41 PM (America/New_York, UTC-05:00)')
        self.assertEqual(host.clock_text(now.replace(hour=0, minute=5), 'UTC'), 'Monday, 9 November 2026, 12:05 AM (UTC)')
        india = datetime(2026, 11, 9, 7, 11, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        self.assertEqual(host.clock_text(india, 'Asia/Kolkata'), 'Monday, 9 November 2026, 7:11 AM (Asia/Kolkata, UTC+05:30)')


if __name__ == '__main__':
    unittest.main()
