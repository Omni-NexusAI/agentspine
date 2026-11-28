"""A model that knows it hears the user (plan 2026-11-18, change 3).

Live: asked "how would you describe my accent?", a model that had just written the user's words from their audio answered "I only
receive your words as text". Nothing told it that it hears a recording, and one rule forbade any mention of the audio."""
import importlib
import unittest

if __package__:
    from .behavior_harness import SPEAK, script
    from .test_behavior_corpus import Scenario
else:
    from behavior_harness import SPEAK, script
    from test_behavior_corpus import Scenario

control = importlib.import_module('plugins._convo.helpers.convo_control')
history = importlib.import_module('plugins._convo.helpers.convo_history')
user_line = importlib.import_module('plugins._convo.helpers.convo_user_line')


class Hearing(Scenario):
    def test_an_audio_turn_says_the_model_hears_the_recording_and_only_after_the_stable_start(self):
        async def scenario(h):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            return h.answers[0]['system']
        system, _ = self.play(scenario, classifier={'question': script(SPEAK, .2)}, model_deltas=[('Fine.', .1)])
        stable, _, late = system.partition(history.LATE_MARK)
        self.assertIn(user_line.HEARING, late)
        self.assertNotIn('microphone', stable)                       # the request's start stays the same on every turn
        self.assertLess(late.index(user_line.INSTRUCTION), late.index(user_line.HEARING))

    def test_the_sentence_names_what_can_be_heard_and_that_earlier_audio_is_gone(self):
        for part in ('microphone audio', 'voice, tone, accent and pace', 'sounds around them', 'describe it when the user asks',
                     'Earlier turns appear as text'):
            with self.subTest(part):
                self.assertIn(part, user_line.HEARING)

    def test_audio_quality_is_off_limits_only_unprompted(self):
        self.assertIn('Do not bring up audio quality yourself', control.INSTRUCTION)
        self.assertIn('when the user asks about their voice or a sound, answer from what you heard', control.INSTRUCTION)
        self.assertNotIn('Never mention audio quality', control.INSTRUCTION)
        self.assertIn('never ask the user to repeat themselves', control.INSTRUCTION)      # the 2026-11-09 regression stays fixed


if __name__ == '__main__':
    unittest.main()
