"""Speech over the assistant's reply (the browser's echo guard confirmed the user's own voice and ducked the reply): yield with a fade,
go on, or keep listening; a safety yield after `barge_in_seconds` of confirmed voice; the history keeps what the user heard.
Virtual clock, scripted providers, synthetic audio: this says nothing about real voices, rooms or echo."""
import base64
import importlib
import unittest

if __package__:
    from .behavior_harness import LONG_REPLY, SPEAK, make_wav, script
    from .test_behavior_corpus import Scenario
else:
    from behavior_harness import LONG_REPLY, SPEAK, make_wav, script
    from test_behavior_corpus import Scenario

policy = importlib.import_module('plugins._convo.helpers.convo_policy')
store_module = importlib.import_module('plugins._convo.helpers.convo_store')


def typed_env(**probabilities):
    """A System One envelope with this attention distribution (what the typed consumer returns)."""
    distribution = {'addressed': 0.0, 'backchannel': 0.0, 'side_conversation': 0.0, 'unfinished': 0.0, 'unclear': 0.0, **probabilities}
    top = max(distribution, key=distribution.get)
    return {'speech': 'speak' if top == 'addressed' else 'silent', 'action': 'none', 'confidence': distribution[top], 'explicit_request': False,
            'attention': top, 'attention_probabilities': distribution, 'explicit_probability': .1, 'name_probability': 0.0}


REQUEST = typed_env(addressed=.8, unclear=.05, backchannel=.05, side_conversation=.05, unfinished=.05)
ACK = typed_env(backchannel=.85, side_conversation=.05, addressed=.04, unclear=.03, unfinished=.03)
UNSURE = typed_env(addressed=.3, backchannel=.3, side_conversation=.1, unfinished=.2, unclear=.1)
ACK_FINAL = {**ACK, 'speech_seconds': .5, 'ack_respond': .07}          # what the consumer adds for a short finished turn
FIRST = 'The first sentence of this reply is deliberately long enough to be spoken.'     # 13 words


class Rule(unittest.TestCase):
    def decision(self, envelope):
        return policy.Decision.parse(envelope, typed=True)

    def test_a_request_yields_an_acknowledgement_goes_on_anything_else_listens(self):
        self.assertEqual(policy.overlap_disposition(self.decision(REQUEST), voiced=.5), ('admit', 'request'))
        self.assertEqual(policy.overlap_disposition(self.decision(ACK), voiced=.5), ('continue', ''))
        self.assertEqual(policy.overlap_disposition(self.decision(UNSURE), voiced=.5), ('listen', ''))

    def test_the_safety_yield_comes_after_the_set_time_and_twice_that_after_a_confident_go_on(self):
        self.assertEqual(policy.overlap_disposition(self.decision(UNSURE), voiced=1.0, safety=1.0), ('admit', 'safety'))
        self.assertEqual(policy.overlap_disposition(self.decision(ACK), voiced=1.5, safety=1.0), ('continue', ''))
        self.assertEqual(policy.overlap_disposition(self.decision(ACK), voiced=2.0, safety=1.0), ('admit', 'safety'))
        self.assertEqual(policy.overlap_disposition(self.decision(UNSURE), voiced=1.5, safety=1.0, continuing=True), ('listen', ''))
        self.assertEqual(policy.overlap_disposition(self.decision(UNSURE), voiced=None, safety=1.0), ('listen', ''))   # unknown: no yield by time

    def test_a_classifier_without_probabilities_uses_its_fixed_confidence(self):
        self.assertEqual(policy.overlap_disposition(policy.Decision('speak', 'none', .9)), ('admit', 'request'))
        self.assertEqual(policy.overlap_disposition(policy.Decision('silent', 'none', .9)), ('continue', ''))
        self.assertEqual(policy.overlap_disposition(policy.Decision('speak', 'none', .5)), ('listen', ''))


class History(unittest.TestCase):
    def test_an_interrupted_reply_keeps_what_was_heard_once_and_is_marked(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            store = store_module.Store(Path(folder) / 'db')
            try:
                session = store.start('browser', 'a')
                store.accept_turn(session['id'], 'browser', session['epoch'], 'q', {'input': 'audio'})
                store.delivered(session['id'], 'browser', session['epoch'], 'q', 'p1', 'Hello there.')
                self.assertEqual(store.interrupted(session['id'], 'browser', 'q', 'The second  sentence'), 'Hello there. The second sentence')
                self.assertIsNone(store.interrupted(session['id'], 'browser', 'q', 'again'))          # once
                content = store.context('a')['turns'][0]['content']
                self.assertEqual((content['assistant'], content['interrupted']), ('Hello there. The second sentence', True))
                self.assertEqual([e['payload'] for e in store.events('a') if e['kind'] == 'turn.interrupted'], [{'heard': 'The second sentence'}])
            finally:
                store.close()


class OverReply(Scenario):
    """A question is being answered (LONG_REPLY; its first phrase plays from 0.9 s); the user speaks over it at 1.0 s."""

    def run_over(self, probes, *, final=None, classifier=None):
        async def scenario(h):
            h.handler._typed = lambda state: True
            original = h.sidecar.speech
            async def speech(role, text):
                await original(role, text)
                return base64.b64decode(make_wav(500, 2.6))                  # every phrase lasts 2.6 s: 0.2 s a word for FIRST
            h.sidecar.speech = speech
            await h.candidate('q', h.audio('question'), final=True)
            await h.until(1.0)
            phrase = h.audible('q')[0][1]['phrase_id']
            states = []
            for index, (tag, voiced) in enumerate(probes, 1):
                await h.candidate('s', h.audio(tag), sequence=index, final=False, overlap=True, voiced_s=voiced,
                                  heard={'phrase_id': phrase, 'played_s': 1.0})
                await h.until(h.now() + .5)
                states.append(h.last_state('s'))
            if final is not None:
                await h.candidate('s', h.audio(final), sequence=len(probes) + 1, final=True, overlap=True, voiced_s=.5)
            await h.settle()
            q = [t['content'] for t in h.store.context('a')['turns'] if t['id'] == 'q'][0]
            admitted = h.events('candidate.admitted', 's')
            return {'states': states, 'fade': admitted[0][1].get('fade_ms') if admitted else None, 'q': q, 'epoch': h.epoch(),
                    'systems': [a['system'] for a in h.answers], 'policy': [c[2] for c in h.log('policy.start')],
                    'q_phrases': len(h.audible('q')), 'answers': len(h.answers)}
        facts, _ = self.play(scenario, model_deltas=LONG_REPLY, tts_delay=.4,
                             classifier={'question': script(SPEAK, .2), **(classifier or {})})
        return facts

    def test_a_request_over_the_reply_yields_with_a_fade_and_the_history_keeps_what_was_heard(self):
        facts = self.run_over([('wait', .5)], final='wait, final', classifier={'wait': script(REQUEST, .2), 'wait, final': script(REQUEST, .2)})
        self.assertEqual((facts['states'][0], facts['fade']), ('admit', 100))
        self.assertEqual(facts['q'].get('interrupted'), True)
        self.assertEqual(facts['q'].get('assistant'), 'The first sentence of this')           # 1.0 s of 2.6 s: five of thirteen words
        self.assertIn('they had heard it up to: "The first sentence of this"', facts['systems'][-1])

    def test_speech_in_the_echo_tail_of_a_reply_heard_to_its_end_marks_nothing_interrupted(self):
        async def scenario(h):
            h.handler._typed = lambda state: True
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            phrases = [p['phrase_id'] for _, p in h.audible('q')]
            for phrase in phrases:                                                       # the whole reply was heard
                await h.playback('q', phrase, 'started'); await h.playback('q', phrase)
            await h.candidate('s', h.audio('tail'), sequence=1, final=False, overlap=True, voiced_s=1.0, heard={'phrase_id': phrases[-1], 'played_s': 2.0})
            await h.settle()
            q = [t['content'] for t in h.store.context('a')['turns'] if t['id'] == 'q'][0]
            return q, [e['kind'] for e in h.store.events('a')]
        (q, kinds), _ = self.play(scenario, model_deltas=[('A complete answer.', .2)], tts_delay=.2, classifier={'question': script(SPEAK, .2), 'tail': script(UNSURE, .2)})
        self.assertEqual((q.get('interrupted'), q.get('assistant')), (None, 'A complete answer.'))
        self.assertNotIn('turn.interrupted', kinds)

    def test_an_acknowledgement_over_the_reply_lets_it_go_on(self):
        facts = self.run_over([('mm', .5)], final='mm, final', classifier={'mm': script(ACK, .2), 'mm, final': script(ACK_FINAL, .2)})
        self.assertEqual(facts['states'], ['continue'])
        self.assertEqual((facts['epoch'], facts['answers'], facts['fade']), (1, 1, None))   # only the question moved the epoch; no second answer
        self.assertNotIn('interrupted', facts['q'])

    def test_unsure_speech_keeps_listening_then_the_safety_yield_needs_no_classifier(self):
        facts = self.run_over([('hmm', .5), ('hmm more', 1.0)], classifier={'hmm': script(UNSURE, .2), 'hmm more': script(UNSURE, .2)})
        self.assertEqual(facts['states'], ['listen', 'admit'])
        self.assertEqual(facts['fade'], 100)
        self.assertNotIn('hmm more', facts['policy'])                                        # yielded on time alone

    def test_after_a_confident_go_on_the_safety_yield_waits_twice_as_long(self):
        facts = self.run_over([('mm', .5), ('mm more', 1.5), ('mm on', 2.0)],
                              classifier={'mm': script(ACK, .2), 'mm more': script(ACK, .2), 'mm on': script(ACK, .2)})
        self.assertEqual(facts['states'], ['continue', 'continue', 'admit'])


if __name__ == '__main__':
    unittest.main()
