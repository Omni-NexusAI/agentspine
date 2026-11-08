"""Classifier acceptance: any registered decision backend is used honestly, whatever modalities it declares.

Stub backends declare {text}, {text,audio} or {text,audio,images}, are registered through the same
`convo_system_one.register_backend` a real System One backend will use, and are driven end to end through
`Realtime._candidate_loop` on the virtual clock. The real staged System One SDK performs request preparation and typed
answer validation, so a stub cannot accept what the SDK would refuse. Skipped without the SDK fixture paths.

This proves Convo's side of the contract (modality negotiation, original audio, no transcript substitution, no fallback,
revocation). It says nothing about any real model's accuracy or latency."""
import asyncio
import base64
import importlib
import io
import json
import sys
import types
import unittest
import wave
from dataclasses import replace
from unittest.mock import patch

if __package__:
    from .behavior_harness import Harness, make_wav, run_virtual
    from .test_system_one_consumer import load_sdk
else:
    from behavior_harness import Harness, make_wav, run_virtual
    from test_system_one_consumer import load_sdk

from plugins._convo.helpers import convo_system_one as bridge
from plugins._convo.helpers.convo_contract import merge

DEFAULTS = {'attention': 'addressed', 'action': 'none', 'explicit_request': 'yes', 'addressed_by_name': 'no'}
DELEGATE_TASK = {'type': 'tool.proposal', 'name': 'delegate_task', 'arguments': {'task': 'Write the weekly report'}}


def pcm_of(encoded):
    with wave.open(io.BytesIO(base64.b64decode(encoded))) as reader:
        return reader.readframes(reader.getnframes())


class StubBackend:
    """A provisioned generic decision backend: real SDK preparation/validation, scripted answers and latency."""
    def __init__(self, sdk, capabilities, name='stub'):
        self.sdk, self.capabilities, self.backend, self.name = sdk, capabilities, name, name
        self.requests, self.selected, self.delay, self.failure, self.confidence = [], dict(DEFAULTS), .2, None, .97
        self.distributions = {}

    def select(self, **values):
        self.selected = {**DEFAULTS, **values}

    def distribute(self, **by_question):
        """Answer a question with exactly this probability for every criterion (the most likely one is the choice)."""
        self.distributions = by_question

    async def evaluate_request(self, request):
        prepared = self.sdk.prepare_request(request, self.capabilities)      # refuses what the declaration does not cover
        self.requests.append(prepared)
        await asyncio.sleep(self.delay)
        if self.failure:
            raise self.failure
        answers = {}
        for name, question in prepared.questions.items():
            if name in self.distributions:
                probabilities = dict(self.distributions[name])
                value = max(probabilities, key=probabilities.get)
                answers[name] = {'type': 'choice', 'choice': value, 'confidence': probabilities[value], 'probabilities': probabilities}
                continue
            value, others = self.selected[name], len(question['criteria']) - 1
            probabilities = {key: self.confidence if key == value else (1 - self.confidence) / others for key in question['criteria']}
            answers[name] = {'type': 'choice', 'choice': value, 'confidence': self.confidence, 'probabilities': probabilities}
        return self.sdk.normalize_answers({'answers': answers}, prepared.questions, self.name)


class AcceptanceCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sdk = load_sdk()

    def setUp(self):
        runtime = types.ModuleType('helpers.runtime')
        helpers = types.ModuleType('helpers'); helpers.__path__ = []; helpers.runtime = runtime
        patcher = patch.dict(sys.modules, {'helpers': helpers, 'helpers.runtime': runtime})
        patcher.start(); self.addCleanup(patcher.stop)
        self.native = {'enabled': True, 'main': {'enabled': False}, 'consumers': {'convo': {'enabled': True}}}

    def caps(self, *combinations, **limits):
        values = dict(max_images=2, max_image_pixels=16, max_media_bytes=2_000_000, audio_sample_rates=frozenset({16000}),
                      audio_channels=frozenset({1}), max_audio_seconds=30)
        values.update(limits)
        return self.sdk.MediaCapabilities(frozenset({'choice'}), modality_combinations=frozenset(map(frozenset, combinations)), **values)

    def drive(self, scenario, caps, *, audio_conversation=True, config=None, **options):
        """Register a stub backend, start a real session on it and run the scenario. Returns the backend."""
        backend = StubBackend(self.sdk, caps)
        bridge.register_backend('accept', backend, sdk=self.sdk, config_provider=lambda: self.native)
        config = merge({'roles': {'conversation': {'capabilities': {'audio_input': audio_conversation}}}}, config or {})
        async def run():
            h = await Harness.create(system_one={'enabled': True, 'backend': 'accept'}, config=config, **options)
            try:
                await scenario(h, backend)
                h.assert_invariants()
            finally:
                await h.close()
        run_virtual(run())
        return backend


TEXT = ('text',)
TEXT_AUDIO = ('text', 'audio')
TEXT_AUDIO_IMAGES = ('text', 'audio', 'images')


class Negotiation(AcceptanceCase):
    def test_text_only_backend_is_refused_for_an_audio_conversation_and_never_called(self):
        async def never(h, backend):
            self.fail('The session must not start')
        with self.assertRaisesRegex(ValueError, 'text context plus direct audio'):
            self.drive(never, self.caps(TEXT))

    def test_audio_only_declaration_cannot_serve_policy_requests_that_always_carry_text(self):
        async def never(h, backend):
            self.fail('The session must not start')
        with self.assertRaisesRegex(ValueError, 'text context plus direct audio'):
            self.drive(never, self.caps(('audio',)))

    def test_backend_without_a_text_mode_is_refused_for_a_text_only_conversation(self):
        async def never(h, backend):
            self.fail('The session must not start')
        with self.assertRaisesRegex(ValueError, 'text-only conversation'):
            self.drive(never, self.caps(TEXT_AUDIO), audio_conversation=False)

    def test_backend_with_too_few_questions_is_refused(self):
        async def never(h, backend):
            self.fail('The session must not start')
        with self.assertRaisesRegex(ValueError, 'four bounded Convo policy questions'):
            self.drive(never, replace(self.caps(TEXT_AUDIO), max_questions=1))

    def test_text_and_audio_and_superset_backends_are_accepted(self):
        for name, caps in {'text+audio': self.caps(TEXT, TEXT_AUDIO), 'text+audio+images': self.caps(TEXT, TEXT_AUDIO, TEXT_AUDIO_IMAGES)}.items():
            with self.subTest(name):
                async def scenario(h, backend):
                    await h.candidate('p', h.audio('hello'), final=False)
                    await h.settle()
                    self.assertEqual(h.last_state('p'), 'admit')
                    self.assertEqual(backend.requests[-1].images, ())                 # nothing is invented for unused modalities
                # a fresh registry per subtest keeps the registration name free
                self.setUp()
                self.drive(scenario, caps)

    def test_readiness_reports_the_declared_modalities(self):
        async def scenario(h, backend):
            ready = bridge.readiness(h.config)
            self.assertTrue(ready['ready'])
            self.assertEqual(ready['capabilities']['modality_combinations'], [['audio', 'text'], ['text']])
            self.assertEqual(ready['capabilities']['audio_sample_rates'], [16000])
        self.drive(scenario, self.caps(TEXT, TEXT_AUDIO))

    def test_readiness_reports_the_question_budget_and_every_declared_limit(self):
        """The settings reference card shows what the backend can answer and accept; none of it is inferred."""
        async def scenario(h, backend):
            capabilities = bridge.readiness(h.config)['capabilities']
            self.assertEqual((capabilities['max_questions'], capabilities['max_audio_seconds'], capabilities['audio_channels']), (32, 30, [1]))
            listed = {item['id']: item for item in bridge.registered_backends()}
            self.assertEqual(listed['accept']['capabilities'], capabilities)               # the same declaration, readable before it is selected
            self.assertEqual(listed['accept']['remote'], False)
        self.drive(scenario, self.caps(TEXT, TEXT_AUDIO))


class Modalities(AcceptanceCase):
    def test_original_audio_reaches_the_backend_unchanged_and_never_as_a_transcript(self):
        async def scenario(h, backend):
            original = h.audio('question', level=321, seconds=.5)
            await h.candidate('p', original, final=False)
            await h.settle()
            request = backend.requests[-1]
            self.assertEqual(request.audio.pcm16, pcm_of(original))
            self.assertEqual((request.audio.sample_rate, request.audio.channels), (16000, 1))
            self.assertIsInstance(request.text, str)
            self.assertIn('latest thing the user said', request.text)                  # the audio is the observation; no transcript stands in for it
            self.assertNotIn('transcript', request.text.lower())
            self.assertEqual(set(request.questions), set(bridge.QUESTIONS))
        self.drive(scenario, self.caps(TEXT_AUDIO))

    def test_policy_context_carries_what_the_assistant_just_said(self):
        """A reply such as 'Yes, please.' means nothing without the question it answers; the model is told what the user last heard."""
        async def scenario(h, backend):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()
            phrase = h.audible('q')[0][1]['phrase_id']
            await h.playback('q', phrase, 'started')
            await h.playback('q', phrase)                                              # delivered: the user heard it
            await h.candidate('r', h.audio('reply'), final=False)
            await h.settle()
            self.assertIn('The assistant had just said: "A complete answer."', backend.requests[-1].text)
        self.drive(scenario, self.caps(TEXT_AUDIO))

    def test_an_unspoken_answer_is_not_offered_as_context(self):
        async def scenario(h, backend):
            await h.candidate('q', h.audio('question'), final=True)
            await h.settle()                                                           # generated, never played: the user did not hear it
            await h.candidate('r', h.audio('reply'), final=False)
            await h.settle()
            self.assertNotIn('The assistant had just said', backend.requests[-1].text)
        self.drive(scenario, self.caps(TEXT_AUDIO))

    def test_text_backend_serves_a_text_only_conversation_and_refuses_audio(self):
        async def scenario(h, backend):
            await h.candidate('t', transcript='Hello there', final=True)
            await h.settle()
            self.assertEqual(h.accepted_turns(), ['t'])
            self.assertIn(json.dumps('Hello there'), backend.requests[-1].text)         # the recognized text, quoted as data
            self.assertIsNone(backend.requests[-1].audio)
            await h.candidate('a', h.audio('sound'), final=True)                      # audio is not transcoded for a text backend
            await h.settle()
            self.assertEqual(h.last_state('a'), 'degraded')
            self.assertEqual(h.accepted_turns(), ['t'])
        self.drive(scenario, self.caps(TEXT), audio_conversation=False)

    def test_text_only_request_is_refused_by_an_audio_backend_with_no_text_mode(self):
        async def scenario(h, backend):
            await h.candidate('t', transcript='Hello there', final=True)
            await h.settle()
            self.assertEqual(h.last_state('t'), 'degraded')
            self.assertEqual((backend.requests, h.accepted_turns()), ([], []))
        self.drive(scenario, self.caps(TEXT_AUDIO))

    def test_backend_limits_decide_support_and_convo_never_resamples_or_truncates(self):
        async def scenario(h, backend):
            await h.candidate('u', h.audio('long', seconds=3), sequence=1, final=False)       # longer than the backend accepts
            await h.settle()
            await h.candidate('u', make_wav(5, seconds=.5, rate=24000), sequence=2, final=False)  # a rate the backend does not declare
            await h.settle()
            self.assertEqual([state for _, state, _, _ in h.states('u')], ['considering', 'degraded', 'considering', 'degraded'])
            self.assertEqual(backend.requests, [])                                      # nothing reached inference
        self.drive(scenario, self.caps(TEXT_AUDIO, max_audio_seconds=2))


class Decisions(AcceptanceCase):
    """The typed distributions, mapped through System One's envelope and Convo's gate, end to end."""
    def probe(self, selected, expected, *, final=False, reply=True, config=None):
        async def scenario(h, backend):
            if reply:
                h.start_reply()
            backend.select(**selected)
            await h.candidate('p', h.audio('speech'), final=final)
            await h.settle()
            self.assertEqual(h.last_state('p'), expected, selected)
        self.drive(scenario, self.caps(TEXT, TEXT_AUDIO), config=config)

    def test_partial_speech_needs_an_explicit_addressed_request_to_interrupt(self):
        """With the waving-away rule switched on (it is off by default: see the next test)."""
        cases = [({'attention': 'addressed', 'explicit_request': 'yes'}, 'admit'),
                 ({'attention': 'addressed', 'explicit_request': 'no'}, 'withhold'),
                 ({'attention': 'backchannel'}, 'ignore'),
                 ({'attention': 'side_conversation'}, 'ignore'),
                 ({'attention': 'unfinished'}, 'ignore'),
                 ({'attention': 'unclear'}, 'withhold')]
        for selected, expected in cases:
            with self.subTest(**selected):
                self.setUp()
                self.probe(selected, expected, config={'policy_gate': {'ignore_min': .9}})

    def test_by_default_partial_speech_is_never_waved_away_because_the_measured_model_cannot_do_it_reliably(self):
        """Measured on the decision model this was built against: no floor waves away non-requests without also waving away real ones."""
        for selected in ({'attention': 'backchannel'}, {'attention': 'side_conversation'}, {'attention': 'unfinished'}):
            with self.subTest(**selected):
                self.setUp()
                self.probe(selected, 'withhold')                                        # held (ducked), decided when the utterance is complete

    def test_a_backchannel_that_ends_while_the_assistant_talks_does_not_interrupt_it(self):
        async def scenario(h, backend):
            h.start_reply()
            backend.select(attention='backchannel')
            await h.candidate('b', h.audio('mm-hmm'), final=True)
            await h.settle()
            self.assertEqual(h.last_state('b'), 'ignore')
            self.assertEqual((h.epoch(), h.events('candidate.admitted')), (0, []))      # the reply in flight was never revoked
        self.drive(scenario, self.caps(TEXT_AUDIO))

    def test_final_speech_is_answered_when_addressed_or_unclear_and_ignored_otherwise(self):
        cases = [({'attention': 'addressed'}, 'admit'), ({'attention': 'unclear'}, 'admit'),
                 ({'attention': 'backchannel'}, 'ignore'), ({'attention': 'side_conversation'}, 'ignore')]
        for selected, expected in cases:
            with self.subTest(**selected):
                self.setUp()
                self.probe(selected, expected, final=True, reply=False)

    def test_delegation_needs_both_the_action_class_and_an_explicit_request(self):
        for selected, jobs in (({'action': 'delegate', 'explicit_request': 'yes'}, 1), ({'action': 'delegate', 'explicit_request': 'no'}, 0)):
            with self.subTest(**selected):
                self.setUp()
                async def scenario(h, backend, selected=selected, jobs=jobs):
                    backend.select(**selected)
                    await h.candidate('t', h.audio('ask'), final=True)
                    await h.settle()
                    self.assertEqual(len(h.jobs()), jobs)
                self.drive(scenario, self.caps(TEXT_AUDIO), proposal=DELEGATE_TASK)

    def test_backend_failure_never_falls_back_to_the_http_classifier(self):
        async def scenario(h, backend):
            backend.failure = RuntimeError('model unavailable')
            await h.candidate('f', h.audio('request'), final=True)
            await h.settle()
            self.assertEqual(h.last_state('f'), 'degraded')
            self.assertEqual((h.accepted_turns(), h.answers), ([], []))
            self.assertNotIn('policy', h.sidecar.calls)                                 # the legacy classifier was never consulted
        self.drive(scenario, self.caps(TEXT_AUDIO))

    def test_revoking_the_consumer_mid_inference_rejects_the_late_result(self):
        async def scenario(h, backend):
            h.start_reply()
            backend.delay = 1.0
            await h.candidate('p', h.audio('stop'), final=False)
            await h.until(.5)
            self.native['consumers']['convo']['enabled'] = False
            await h.settle()
            self.assertEqual(h.last_state('p'), 'degraded')
            self.assertEqual((h.epoch(), h.events('candidate.admitted')), (0, []))      # the "stop" was never acted on
        self.drive(scenario, self.caps(TEXT_AUDIO))

    def test_unregistering_the_backend_mid_inference_rejects_the_late_result(self):
        async def scenario(h, backend):
            h.start_reply()
            backend.delay = 1.0
            await h.candidate('p', h.audio('stop'), final=False)
            await h.until(.5)
            self.assertTrue(bridge.unregister_backend('accept', bridge._entry('accept')))
            await h.settle()
            self.assertEqual(h.last_state('p'), 'degraded')
            self.assertEqual(h.events('candidate.admitted'), [])
        self.drive(scenario, self.caps(TEXT_AUDIO))

    def test_attention_class_and_distribution_are_carried_through_the_policy_envelope(self):
        """BP4: the caller can tell 'unfinished' from 'side conversation' (a later continuation merge needs it), and sees every probability."""
        async def scenario(h, backend):
            for attention in ('unfinished', 'side_conversation', 'backchannel', 'addressed', 'unclear'):
                backend.select(attention=attention)
                result = await bridge.policy({'audio': h.audio('half a sentence')}, config=h.config)
                self.assertEqual(result['attention'], attention)
                self.assertEqual(set(result['attention_probabilities']), set(bridge.QUESTIONS['attention']['criteria']))
                self.assertAlmostEqual(sum(result['attention_probabilities'].values()), 1, places=6)
                self.assertEqual(max(result['attention_probabilities'], key=result['attention_probabilities'].get), attention)
        self.drive(scenario, self.caps(TEXT_AUDIO))


SPREAD = {'addressed': .35, 'side_conversation': .25, 'backchannel': .15, 'unfinished': .15, 'unclear': .10}     # most likely class at only .35
ELSEWHERE = {'addressed': .05, 'side_conversation': .60, 'backchannel': .20, 'unfinished': .12, 'unclear': .03}
MIDDLING = {'addressed': .45, 'side_conversation': .30, 'backchannel': .10, 'unfinished': .10, 'unclear': .05}


class Gate(AcceptanceCase):
    """The calibrated speech gate, end to end through the registered backend and the real candidate loop."""
    def test_a_spread_distribution_is_answered_where_the_fixed_cutoff_stayed_silent(self):
        async def scenario(h, backend):
            backend.distribute(attention=SPREAD)
            await h.candidate('s', h.audio('request'), final=True)
            await h.settle()
            self.assertEqual(h.last_state('s'), 'admit')
            self.assertEqual(h.accepted_turns(), ['s'])
        self.drive(scenario, self.caps(TEXT_AUDIO))

    def test_a_completed_utterance_with_little_chance_of_being_for_the_assistant_is_ignored(self):
        async def scenario(h, backend):
            backend.distribute(attention=ELSEWHERE)
            await h.candidate('s', h.audio('next door'), final=True)
            await h.settle()
            self.assertEqual(h.last_state('s'), 'ignore')
            self.assertEqual(h.accepted_turns(), [])
        self.drive(scenario, self.caps(TEXT_AUDIO))

    def test_a_configured_gate_floor_reaches_the_live_loop(self):
        """The same utterance (a .50 chance of being for the assistant), under the default floor and under a stricter configured one."""
        for config, expected in (({}, 'admit'), ({'policy_gate': {'respond_min': .6, 'ambient_respond_min': .9}}, 'ignore')):
            with self.subTest(config=config):
                self.setUp()
                async def scenario(h, backend, expected=expected):
                    backend.distribute(attention=MIDDLING)
                    await h.candidate('s', h.audio('maybe'), final=True)
                    await h.settle()
                    self.assertEqual(h.last_state('s'), expected)
                self.drive(scenario, self.caps(TEXT_AUDIO), config=config)

    def test_ambient_listening_needs_more_than_active_listening(self):
        for mode, expected in (('active', 'admit'), ('ambient', 'ignore')):
            with self.subTest(mode):
                self.setUp()
                async def scenario(h, backend, expected=expected):
                    backend.distribute(attention=MIDDLING)
                    await h.candidate('s', h.audio('maybe'), final=True)
                    await h.settle()
                    self.assertEqual(h.last_state('s'), expected)
                self.drive(scenario, self.caps(TEXT_AUDIO), mode=mode, config={'ambient_enabled': True})

    def test_a_partial_request_stops_the_assistant_only_when_it_is_clearly_a_request_to_it(self):
        cases = [({'addressed': .9, 'side_conversation': .04, 'backchannel': .02, 'unfinished': .02, 'unclear': .02}, .8, 'admit'),       # .72, over the .60 default
                 ({'addressed': .8, 'side_conversation': .08, 'backchannel': .04, 'unfinished': .04, 'unclear': .04}, .7, 'withhold'),     # .56, under it
                 ({'addressed': .55, 'side_conversation': .2, 'backchannel': .1, 'unfinished': .1, 'unclear': .05}, .7, 'withhold'),      # .385
                 ({'addressed': .9, 'side_conversation': .04, 'backchannel': .02, 'unfinished': .02, 'unclear': .02}, .1, 'withhold')]     # a statement, not a request
        for attention, explicit, expected in cases:
            with self.subTest(addressed=attention['addressed'], explicit=explicit):
                self.setUp()
                async def scenario(h, backend, attention=attention, explicit=explicit, expected=expected):
                    h.start_reply()
                    backend.distribute(attention=attention, explicit_request={'yes': explicit, 'no': 1 - explicit})
                    await h.candidate('p', h.audio('stop'), final=False)
                    await h.settle()
                    self.assertEqual(h.last_state('p'), expected)
                self.drive(scenario, self.caps(TEXT_AUDIO))

    def test_name_mode_answers_when_the_name_probability_reaches_the_floor(self):
        for name_yes, expected in ((.95, 'admit'), (.85, 'ignore')):
            with self.subTest(name_yes=name_yes):
                self.setUp()
                async def scenario(h, backend, name_yes=name_yes, expected=expected):
                    backend.distribute(attention={'addressed': .9, 'side_conversation': .04, 'backchannel': .02, 'unfinished': .02, 'unclear': .02},
                                       addressed_by_name={'yes': name_yes, 'no': 1 - name_yes})
                    await h.candidate('n', h.audio('hey'), final=True)
                    await h.settle()
                    self.assertEqual(h.last_state('n'), expected)
                self.drive(scenario, self.caps(TEXT_AUDIO), mode='name', config={'ambient_enabled': True, 'activation_names': ['Jarvis']})

    def test_a_configured_name_floor_reaches_the_live_loop(self):
        """A 70% chance the assistant was called by name: not enough for the default floor, enough for a configured lower one."""
        for config, expected in (({}, 'ignore'), ({'policy_gate': {'name_min': .6}}, 'admit')):
            with self.subTest(config=config):
                self.setUp()
                async def scenario(h, backend, expected=expected):
                    backend.distribute(attention={'addressed': .9, 'side_conversation': .04, 'backchannel': .02, 'unfinished': .02, 'unclear': .02},
                                       addressed_by_name={'yes': .7, 'no': .3})
                    await h.candidate('n', h.audio('hey'), final=True)
                    await h.settle()
                    self.assertEqual(h.last_state('n'), expected)
                self.drive(scenario, self.caps(TEXT_AUDIO), mode='name', config={'ambient_enabled': True, 'activation_names': ['Jarvis'], **config})

    def test_the_gate_decision_is_recorded_as_content_free_timing(self):
        async def scenario(h, backend):
            convo_metrics = importlib.import_module('plugins._convo.helpers.convo_metrics')      # the module the live loop resolves, however an earlier test left the cache
            convo_metrics.LOG.reset()
            backend.distribute(attention=SPREAD)
            await h.candidate('m', h.audio('request'), final=True)
            await h.settle()
            marks = [m for record in convo_metrics.LOG.records(200) if record['turn'] == 'm' for m in record['marks'] if m['name'] == 'policy.gate']
            self.assertEqual(len(marks), 1)
            self.assertEqual((marks[0]['gate'], marks[0]['disposition'], marks[0]['speech']), ('calibrated', 'admit', 'speak'))
            self.assertAlmostEqual(marks[0]['p_respond'], .45, places=3)
            self.assertAlmostEqual(marks[0]['p_addressed'], .35, places=3)
        self.drive(scenario, self.caps(TEXT_AUDIO))

    def test_work_still_needs_an_explicit_request_whatever_the_speech_gate_says(self):
        async def scenario(h, backend):
            backend.select(action='delegate')
            backend.distribute(attention={'addressed': .9, 'side_conversation': .04, 'backchannel': .02, 'unfinished': .02, 'unclear': .02},
                               explicit_request={'yes': .6, 'no': .4})
            await h.candidate('t', h.audio('do the thing'), final=True)
            await h.settle()
            self.assertEqual(h.last_state('t'), 'admit')            # spoken to
            self.assertEqual(len(h.jobs()), 0)                      # but no work: the request was not explicit enough
        self.drive(scenario, self.caps(TEXT_AUDIO), proposal=DELEGATE_TASK)


if __name__ == '__main__':
    unittest.main()
