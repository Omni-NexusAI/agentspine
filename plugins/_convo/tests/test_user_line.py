"""The reply's hidden header (helpers/convo_user_line.py): USER / LANGUAGE lines never spoken, words and language reported."""
import importlib
import unittest

header = importlib.import_module('plugins._convo.helpers.convo_user_line')


def run(text, size=None, record=True):
    """Stream `text` through a filter in chunks of `size` (every split when None) and return (spoken, filter)."""
    sizes = [size] if size else range(1, 8)
    results = set()
    for step in sizes:
        f = header.HeaderFilter(record=record)
        out = ''.join(f.feed(text[i:i + step]) for i in range(0, len(text), step)) + f.finish()
        results.add((out, f.line, f.language))
    assert len(results) == 1, results          # the outcome never depends on how the stream was split
    out, _, _ = results.pop()
    return out, f


class Header(unittest.TestCase):
    def test_two_line_header_is_removed_and_reported(self):
        out, f = run('USER: Tell me a joke.\nLANGUAGE: English\nWhy did the chicken cross the road?')
        self.assertEqual(out, 'Why did the chicken cross the road?')
        self.assertEqual((f.line, f.language), ('Tell me a joke.', 'English'))

    def test_words_on_the_line_after_an_indented_empty_user_line_never_leak(self):
        # The "Write a joke." incident: the words came on an indented line under an empty USER:.
        out, f = run('USER:\n    Write a joke.\nSure, sir. Why did the scarecrow win an award?')
        self.assertEqual(out, 'Sure, sir. Why did the scarecrow win an award?')
        self.assertEqual(f.line, 'Write a joke.')

    def test_words_after_an_empty_user_line_count_only_when_a_language_line_follows(self):
        out, f = run('USER:\nTell me a joke.\nLANGUAGE: English\nHere is one.')
        self.assertEqual((out, f.line), ('Here is one.', 'Tell me a joke.'))
        # An empty USER line followed by the answer never turns the answer's first sentence into the user's words.
        out, f = run('USER:\nSure, here is one.\nWhy did the chicken cross the road?')
        self.assertEqual(out, 'Sure, here is one.\nWhy did the chicken cross the road?')
        self.assertEqual(f.line, '')

    def test_aliases_case_and_markdown(self):
        out, f = run('**User_Transcript:** ¿Qué hora es?\n*assistant_language*: Spanish\nSon las tres.')
        self.assertEqual((out, f.line, f.language), ('Son las tres.', '¿Qué hora es?', 'Spanish'))
        out, f = run('transcript: hello\nlang: en\nHi.')
        self.assertEqual((out, f.line, f.language), ('Hi.', 'hello', 'en'))

    def test_only_user_line_and_language_first(self):
        out, f = run('USER: What about Kansas?\nTopeka.')
        self.assertEqual((out, f.line, f.language), ('Topeka.', 'What about Kansas?', ''))
        out, f = run('LANGUAGE: French\nUSER: Bonjour\nBonjour, monsieur.')
        self.assertEqual((out, f.line, f.language), ('Bonjour, monsieur.', 'Bonjour', 'French'))

    def test_an_answer_label_is_removed(self):
        out, _ = run('USER: hi\nLANGUAGE: English\nASSISTANT: Hello, sir.')
        self.assertEqual(out, 'Hello, sir.')
        out, _ = run('Response: Hello.')
        self.assertEqual(out, 'Hello.')

    def test_no_header_passes_untouched(self):
        for text in ('Sure, sir.', 'As you wish.', 'Users often ask that.', 'Language matters.\nIndeed.', 'Responsibly, yes.'):
            out, f = run(text)
            self.assertEqual(out, text)
            self.assertEqual((f.line, f.language), ('', ''))

    def test_header_only_answers_and_control_words(self):
        out, f = run('USER: This is a test. Do not respond.\nLANGUAGE: English\n[SILENT]')
        self.assertEqual((out, f.line), ('[SILENT]', 'This is a test. Do not respond.'))
        out, f = run('USER: hello\nLANGUAGE: English')
        self.assertEqual((out, f.line, f.language), ('', 'hello', 'English'))
        out, f = run('USER:')
        self.assertEqual((out, f.line), ('', ''))

    def test_follow_up_passes_remove_the_header_but_report_no_words(self):
        out, f = run('USER: search the web\nLANGUAGE: English\nIt is 2 PM in Tokyo.', record=False)
        self.assertEqual((out, f.line, f.language), ('It is 2 PM in Tokyo.', '', 'English'))

    def test_assistant_at_the_start_of_the_users_words_is_kept(self):
        out, f = run('USER: Assistant, what time is it?\nLANGUAGE: English\nIt is noon.')
        self.assertEqual((out, f.line), ('It is noon.', 'Assistant, what time is it?'))
        self.assertEqual(header.validate('ASSISTANT: hi'), '')
        self.assertEqual(header.validate('[inaudible]'), '')

    def test_holding_is_bounded(self):
        f = header.HeaderFilter()
        out = f.feed('USE' + 'R' * 800)           # never a header marker: past SCAN the text is released instead of held forever
        self.assertTrue(out)
        self.assertEqual(header.validate_language('Spanish (Mexico)'), 'Spanish (Mexico)')
        self.assertEqual(header.validate_language('I will answer in English because you asked'), '')

    def test_a_user_line_longer_than_the_scan_is_never_spoken_and_its_start_is_kept(self):
        words = 'word ' * 400                     # 2000 characters, no newline
        out, f = run('USER: ' + words + '\nLANGUAGE: English\nHere is the answer.', size=50)
        self.assertEqual(out, 'Here is the answer.')
        self.assertEqual(f.language, 'English')
        self.assertTrue(f.line.startswith('word word') and len(f.line) <= header.LIMIT)
        # The answer ends inside the long line: nothing of it is spoken.
        out, f = run('**USER:** ' + words, size=100)
        self.assertEqual(out, '')
        self.assertNotIn('USER', out)
        # A line that never ends is dropped only up to DISCARD; what follows is the answer, without the marker.
        f = header.HeaderFilter()
        spoken = f.feed('USER: ' + 'x' * (header.DISCARD + 10)) + f.feed('Answer.') + f.finish()
        self.assertEqual(spoken, 'Answer.')
        # A long LANGUAGE line is dropped too.
        out, f = run('USER: hi\nLANGUAGE: ' + words + '\nHello.', size=64)
        self.assertEqual((out, f.line, f.language), ('Hello.', 'hi', ''))

    def test_bad_language_values_are_ignored(self):
        out, f = run('USER: hi\nLANGUAGE: [unknown]\nHello.')
        self.assertEqual((out, f.language), ('Hello.', ''))


if __name__ == '__main__':
    unittest.main()
