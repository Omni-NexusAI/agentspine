"""A model that writes a tool call as ordinary text must not have it spoken or shown. Nothing here runs a tool: real tool use
needs a structured proposal, an explicit request at 0.9 or more and the host's own checks. This only keeps markup out of speech."""
import random
import unittest

from plugins._convo.helpers.convo_reply_guard import ReplyGuard

MARKUP = '<tool_call><function=web_search><parameter=query>library hours</parameter></function></tool_call>'


def run(chunks):
    guard = ReplyGuard()
    out = [guard.feed(chunk) for chunk in chunks]
    out.append(guard.finish())
    return ''.join(out), guard


class Plain(unittest.TestCase):
    def test_ordinary_text_passes_through_unchanged_however_it_is_chunked(self):
        text = 'It looks clear tomorrow afternoon. Highs near 21 degrees, lows around 12; if a < b then 5 > 3, and I <3 sunshine [really].'
        rng = random.Random(3)
        for _ in range(60):
            cuts = sorted(rng.sample(range(1, len(text)), rng.randint(1, 12)))
            pieces = [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)])]
            joined, guard = run(pieces)
            self.assertEqual(joined, text, pieces)
            self.assertFalse(guard.blocked)

    def test_a_held_back_fragment_is_released_when_the_reply_ends(self):
        guard = ReplyGuard()
        self.assertEqual(guard.feed('a <'), 'a ')
        self.assertEqual(guard.finish(), '<')
        self.assertFalse(guard.blocked)


class Markup(unittest.TestCase):
    def test_markup_after_speech_drops_the_markup_and_keeps_the_speech(self):
        joined, guard = run(['Let me check. ' + MARKUP])
        self.assertEqual(joined, 'Let me check. ')
        self.assertTrue(guard.blocked)

    def test_everything_after_markup_is_dropped(self):
        joined, guard = run(['Sure. ', MARKUP, ' And more words after it.'])
        self.assertEqual(joined, 'Sure. ')
        self.assertTrue(guard.blocked)

    def test_a_marker_split_across_chunks_is_still_caught(self):
        for cut in range(1, len(MARKUP) // 2):
            joined, guard = run(['Okay. ' + MARKUP[:cut], MARKUP[cut:]])
            self.assertEqual(joined, 'Okay. ', cut)
            self.assertTrue(guard.blocked, cut)

    def test_the_known_spellings_are_caught_regardless_of_case(self):
        for marker in ('<TOOL_CALL>', '<tool_calls>', '</tool_call>', '<function=play_media>', '<function_call>', '<invoke name="x">', '<parameter=file_path>',
                       '<tool_use>', '<|tool_call_begin|>', '[TOOL_CALLS]', '[tool_call]'):
            joined, guard = run(['Hello there. ' + marker + ' rest'])
            self.assertEqual(joined, 'Hello there. ', marker)
            self.assertTrue(guard.blocked, marker)

    def test_a_reply_that_is_only_markup_yields_nothing(self):
        joined, guard = run([MARKUP])
        self.assertEqual(joined, '')
        self.assertTrue(guard.blocked)

    def test_finishing_a_blocked_reply_releases_nothing(self):
        guard = ReplyGuard()
        guard.feed('Hi <tool_call>')
        self.assertEqual(guard.finish(), '')


if __name__ == '__main__':
    unittest.main()
