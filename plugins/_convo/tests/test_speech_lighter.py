"""Speech features learned from refusals (Realtime._lighter_speech / _plain_speech / _speak)."""
import asyncio
import importlib
import unittest

import sys
import types
from unittest.mock import patch


class WsStub:
    def __init__(self, *args, **kwargs): pass


_ws = types.ModuleType('helpers.ws'); _ws.WsHandler = WsStub
with patch.dict(sys.modules, {'helpers.ws': _ws}):
    Realtime = importlib.import_module('plugins._convo.api.realtime').Realtime


class Refused(Exception):
    def __init__(self, status):
        self.response = type('Response', (), {'status_code': status})()


ROLE = {'backend': 'openai', 'model': 'm', 'voice': 'v', 'instructions': 'calm', 'capabilities': {'native_pcm': True, 'audio_output': True}}


class Lighter(unittest.TestCase):
    def test_one_feature_at_a_time_and_only_after_the_servers_own_refusal(self):
        lighter, dropped = Realtime._lighter_speech(ROLE, 422)
        self.assertEqual((dropped, lighter['capabilities']['native_pcm'], lighter['instructions']), ('stream', False, 'calm'))
        self.assertEqual(ROLE['capabilities']['native_pcm'], True)                      # the saved role is never changed
        plain, dropped = Realtime._lighter_speech(lighter, 400)
        self.assertEqual((dropped, 'instructions' in plain), ('instructions', False))
        self.assertEqual(Realtime._lighter_speech(plain, 400), (None, ''))              # nothing left to drop: the failure stands
        self.assertEqual(Realtime._lighter_speech(ROLE, 502)[1], 'stream')              # a streamed answer in another format
        self.assertEqual(Realtime._lighter_speech(lighter, 502), (None, ''))            # a 502 says nothing about instructions
        for status in (401, 403, 409, 429, 500, 503, None, '422'):
            self.assertEqual(Realtime._lighter_speech(ROLE, status), (None, ''), status)
        self.assertEqual(Realtime._lighter_speech({**ROLE, 'backend': 'host_kokoro'}, 422), (None, ''))
        self.assertEqual(Realtime._lighter_speech(None, 422), (None, ''))

    def test_what_was_learned_applies_to_the_following_phrases(self):
        self.assertIs(Realtime._plain_speech({}, ROLE), ROLE)
        role = Realtime._plain_speech({'speech_plain': {'stream'}}, ROLE)
        self.assertEqual((role['capabilities']['native_pcm'], role['instructions']), (False, 'calm'))
        role = Realtime._plain_speech({'speech_plain': {'stream', 'instructions'}}, ROLE)
        self.assertEqual((role['capabilities']['native_pcm'], 'instructions' in role), (False, False))

    def run_speak(self, outcomes, state=None):
        """_speak with a scripted _speak_once: each call gets the next outcome (an exception to raise, or a value to return)."""
        calls, state = [], state if state is not None else {}

        class Stub(Realtime):
            def __init__(self): pass
            async def _speak_once(self, sid, state, session, turn, text, role, began):
                calls.append(role)
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            def _current(self, *args): return True
            def _mark(self, *args, **kwargs): pass
            def _busy(self): return ()
            async def _stalled(self, *args): pass

        try:
            result = asyncio.run(Stub()._speak('sid', state, {'epoch': 1}, 'turn', 'Hello there.', ROLE))
        except Exception as error:
            result = error
        return result, calls, state

    def test_a_refused_streamed_phrase_is_spoken_whole_and_the_rest_of_the_conversation_follows(self):
        result, calls, state = self.run_speak([Refused(422), 'spoken'])
        self.assertEqual(result, 'spoken')
        self.assertEqual([call['capabilities']['native_pcm'] for call in calls], [True, False])
        self.assertEqual(state['speech_plain'], {'stream'})
        result, calls, state = self.run_speak(['spoken'], state)                         # the next phrase: asked the plain way at once
        self.assertEqual([call['capabilities']['native_pcm'] for call in calls], [False])

    def test_both_features_refused_then_plain_works(self):
        result, calls, state = self.run_speak([Refused(422), Refused(400), 'spoken'])
        self.assertEqual(result, 'spoken')
        self.assertEqual([(call['capabilities']['native_pcm'], 'instructions' in call) for call in calls], [(True, True), (False, True), (False, False)])
        self.assertEqual(state['speech_plain'], {'stream', 'instructions'})

    def test_a_refusal_nothing_explains_still_fails_and_nothing_is_learned(self):
        result, calls, state = self.run_speak([Refused(422), Refused(422), Refused(422)])
        self.assertIsInstance(result, Refused)
        self.assertEqual(len(calls), 3)
        self.assertNotIn('speech_plain', state)
        result, calls, state = self.run_speak([Refused(401)])                            # a key problem is never retried lighter
        self.assertEqual((type(result), len(calls)), (Refused, 1))


if __name__ == '__main__':
    unittest.main()
