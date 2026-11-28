"""What the conversation model sees of earlier turns (helpers/convo_history.py), shared by the gateway and the developer view."""
import importlib
import unittest

history = importlib.import_module('plugins._convo.helpers.convo_history')
inspect = importlib.import_module('plugins._convo.helpers.convo_inspect')

CLIP = 'UklGRg' + 'A' * 640           # stands for a base64 WAV; never decoded here


class Rule(unittest.TestCase):
    def test_turns_without_words_or_audio_leave_with_their_replies_and_no_placeholder_is_ever_written(self):
        messages = history.messages([
            {'transcript': 'What is the capital of Japan?', 'assistant': 'Tokyo, sir.'},
            {'assistant': 'The audio was unclear, could you repeat that?'},       # noise answered earlier: no words, no audio left
            {'user_line': 'What about Kansas?', 'assistant': 'Topeka.'},
        ], audio_input=True)
        self.assertEqual(messages, [
            {'role': 'user', 'content': 'What is the capital of Japan?'}, {'role': 'assistant', 'content': 'Tokyo, sir.'},
            {'role': 'user', 'content': 'What about Kansas?'}, {'role': 'assistant', 'content': 'Topeka.'}])
        self.assertNotIn('not captured', repr(messages))

    def test_a_thought_the_model_waited_on_stays_and_joins_what_the_user_says_next(self):
        messages = history.messages([
            {'transcript': 'See what the recent builds are, and if...', 'held': 'wait'},
            {'transcript': 'they fixed the problem with the engines.', 'held': 'wait'},
            {'transcript': 'Search that for me.', 'assistant': 'Looking.'}], audio_input=False)
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant'])
        self.assertEqual(messages[0]['content'], 'See what the recent builds are, and if...\nthey fixed the problem with the engines.\nSearch that for me.')

    def test_declined_and_superseded_turns_never_appear(self):
        messages = history.messages([
            {'transcript': 'This is a test. Do not respond.', 'held': 'silent'},
            {'transcript': 'I need to go', 'superseded': 'x', 'assistant': 'Go where?'},
            {'transcript': 'Hello', 'assistant': 'Hello, sir.'}], audio_input=False)
        self.assertEqual([m['content'] for m in messages], ['Hello', 'Hello, sir.'])

    def test_a_turn_with_no_reply_is_joined_to_the_next_so_roles_alternate(self):
        messages = history.messages([{'transcript': 'Remember the code is 42.'}, {'transcript': 'What is the code?', 'assistant': '42.'}],
                                    audio_input=False)
        self.assertEqual(messages, [{'role': 'user', 'content': 'Remember the code is 42.\nWhat is the code?'},
                                    {'role': 'assistant', 'content': '42.'}])
        # The current message joins an unanswered last turn the same way, audio included.
        current = history.add_user(list(history.messages([{'transcript': 'Hold on'}], audio_input=True)),
                                   [{'type': 'input_audio', 'input_audio': {'data': CLIP, 'format': 'wav'}}])
        self.assertEqual(len(current), 1)
        self.assertEqual([part['type'] for part in current[0]['content']], ['text', 'input_audio'])

    def test_audio_stands_in_for_missing_words_only_for_a_model_that_hears(self):
        turn = [{'user_audio': CLIP, 'assistant': 'Sure.'}]
        self.assertEqual(history.messages(turn, audio_input=True, audio_part=lambda clip: ['audio']),
                         [{'role': 'user', 'content': ['audio']}, {'role': 'assistant', 'content': 'Sure.'}])
        self.assertEqual(history.messages(turn, audio_input=False), [])

    def test_an_interrupted_reply_says_so(self):
        messages = history.messages([{'transcript': 'Tell me a story', 'assistant': 'Once upon', 'interrupted': True}], audio_input=False)
        self.assertEqual(messages[-1]['content'], 'Once upon' + history.INTERRUPTED)


class DeveloperView(unittest.TestCase):
    def tearDown(self):
        inspect.clear()

    def test_the_view_is_text_only_with_audio_as_a_placeholder(self):
        request = {'system': 'rules', 'history': [{'transcript': 'Hi', 'assistant': 'Hello.'}], 'audio': CLIP, 'allow_tools': True}
        inspect.remember(request)
        seen = inspect.latest()
        self.assertEqual(seen['system'], 'rules')
        self.assertEqual([m['role'] for m in seen['messages']], ['user', 'assistant', 'user'])
        self.assertRegex(seen['messages'][-1]['text'], r'^\[audio \d+\.\d s\]$')
        self.assertTrue(seen['tools'])
        self.assertNotIn(CLIP, repr(seen))                                          # never the audio itself


if __name__ == '__main__':
    unittest.main()
