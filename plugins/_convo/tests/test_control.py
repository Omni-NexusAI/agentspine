"""The conversation model's own "nothing to say" ([SILENT]) and "you're not done" ([WAIT]), read off the start of its reply."""
import importlib
import unittest

control = importlib.import_module('plugins._convo.helpers.convo_control')


def run(*chunks):
    """(choice, reason, everything released) for a reply streamed in these chunks."""
    gate = control.ControlFilter()
    out = ''.join(gate.feed(chunk) for chunk in chunks)
    choice, reason, tail = gate.finish()
    return choice, reason, out + tail


class ControlWords(unittest.TestCase):
    def test_a_control_word_is_the_decision_and_its_reason_is_kept_never_released(self):
        self.assertEqual(run('[SILENT] asked not to respond'), ('silent', 'asked not to respond', ''))
        self.assertEqual(run('[WAIT]'), ('wait', '', ''))
        self.assertEqual(run('  [wait] — they stopped mid-sentence\nmore text'), ('wait', 'they stopped mid-sentence', ''))

    def test_a_control_word_split_across_chunks_is_still_read(self):
        self.assertEqual(run('[', 'SIL', 'ENT', ']', ' noise only'), ('silent', 'noise only', ''))
        self.assertEqual(run('[W', 'AIT]'), ('wait', '', ''))

    def test_an_ordinary_reply_passes_untouched_from_its_first_chunk(self):
        gate = control.ControlFilter()
        self.assertEqual(gate.feed('Frankfort is '), 'Frankfort is ')            # no holding back for ordinary text
        self.assertEqual(gate.feed('the capital.'), 'the capital.')
        self.assertEqual(gate.finish(), (None, '', ''))

    def test_replies_that_merely_start_like_a_control_word_pass(self):
        for reply in ('Wait, what did you mean?', 'Sir, the capital is Topeka.', 'Silently, the system booted.', 'So it goes.'):
            with self.subTest(reply):
                self.assertEqual(run(*[reply[i:i + 3] for i in range(0, len(reply), 3)]), (None, '', reply))

    def test_a_bracketed_meta_statement_alone_is_silent(self):
        self.assertEqual(run('[No response provided as per instructions.]')[0], 'silent')
        self.assertEqual(run('(No response.)')[0], 'silent')
        self.assertEqual(run('[Silent - the user asked]')[0], 'silent')

    def test_a_bare_control_word_without_brackets_is_read_too(self):
        self.assertEqual(run('Silent.')[0], 'silent')
        self.assertEqual(run('WAIT')[0], 'wait')

    def test_a_leading_stage_direction_is_dropped_and_the_words_pass(self):
        self.assertEqual(run('[laughs] ', 'Sure, here it is.'), (None, '', 'Sure, here it is.'))
        self.assertEqual(run('(smiling) Of course.'), (None, '', '(smiling) Of course.'))   # parentheses are ordinary text

    def test_an_unclosed_bracket_is_released_rather_than_held_forever(self):
        long = '[' + 'x' * (control.HOLD_LIMIT + 5)
        gate = control.ControlFilter()
        self.assertEqual(gate.feed(long), long)


if __name__ == '__main__':
    unittest.main()
