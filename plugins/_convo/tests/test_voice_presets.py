"""Presets that apply (change 10): a choice is resolved with the speech server when it is made, outside any reply; replies only read
the stored snapshot; a refresh runs in the background between replies and keeps the last good values on failure; a reply in progress
is never changed. Stand-in speech server only (convo_synthesis.resolve is replaced); nothing is synthesised or sent anywhere."""
import asyncio
import importlib
import unittest
from unittest.mock import patch

if __package__:
    from .behavior_harness import LONG_REPLY, SPEAK, script
    from .test_behavior_corpus import Scenario
else:
    from behavior_harness import LONG_REPLY, SPEAK, script
    from test_behavior_corpus import Scenario

synthesis = importlib.import_module('plugins._convo.helpers.convo_synthesis')
OWN = {'provider': 'audiocpp', 'connection': 'external', 'url': 'http://localhost:8890/v1', 'model': 'qwen3-tts-1.7b-base-bf16',
       'capabilities': {'audio_output': True}}
VALUES = {'quality': {'temperature': .8, 'top_k': 50, 'first_block_frames': 4, 'steady_block_frames': 8},
          'low-latency': {'temperature': 1.0, 'top_k': 40, 'first_block_frames': 2, 'steady_block_frames': 4}}


class StandInServer:
    """convo_synthesis.resolve's stand-in: answers per profile, can be slow or down, and counts its calls."""
    def __init__(self, delay=0.05, down=False, default='low-latency'):
        self.delay, self.down, self.default, self.calls = delay, down, default, []

    async def resolve(self, role, headers, client_factory, profile_id, overrides):
        self.calls.append((asyncio.get_running_loop().time(), profile_id))
        await asyncio.sleep(self.delay)
        if self.down:
            raise ValueError('The speech server refused these synthesis values (HTTP 503)')
        profile = profile_id or self.default
        if profile not in VALUES:
            raise ValueError('The speech server refused these synthesis values (HTTP 404): unknown profile')
        effective = {**VALUES[profile], **(overrides or {})}
        problem = synthesis.fit_problem(effective) if overrides else ''
        if problem:
            raise ValueError(problem + ' for this preset: change or clear that override.')
        return {'effective': effective, 'profile_id': profile, 'profile_revision': 3}


class Presets(Scenario):
    def play_with(self, scenario, server, **options):
        options.setdefault('config', {'roles': {'tts': OWN}})
        options.setdefault('classifier', {'first': script(SPEAK, .2), 'second': script(SPEAK, .2)})
        with patch.object(synthesis, 'resolve', new=server.resolve):
            return self.play(scenario, **options)

    @staticmethod
    def recording(h):
        envelopes = []
        original = h.sidecar.speech
        async def speech(role, text):
            envelopes.append((h.state.get('current_turn'), role.get('reply_tuning')))
            return await original(role, text)
        h.sidecar.speech = speech
        return envelopes

    @staticmethod
    async def tune(h, profile_id='', overrides=None, **extra):
        return await h.handler.process('convo.tune', {**h.ids(), 'profile_id': profile_id, 'overrides': overrides or {}, **extra}, 'browser')

    def test_a_choice_is_resolved_when_made_and_a_preset_changed_mid_reply_changes_no_phrase_of_it(self):
        server = StandInServer()
        async def scenario(h):
            envelopes = self.recording(h)
            await h.candidate('a', h.audio('first'), final=True)
            await h.until(1.4)                                      # the reply is speaking
            self.assertTrue(envelopes)
            answer = await self.tune(h, 'quality')
            self.assertEqual((answer['ok'], answer['source'], answer['profile_id']), (True, 'own', 'quality'))
            self.assertEqual(answer['effective']['temperature'], .8)   # the values show at once
            await h.settle()
            await h.candidate('b', h.audio('second'), final=True)
            await h.settle()
            return envelopes
        envelopes, _ = self.play_with(scenario, server, model_deltas=LONG_REPLY, tts_delay=.2)
        first = [envelope for turn, envelope in envelopes if turn == 'a']
        second = [envelope for turn, envelope in envelopes if turn == 'b']
        self.assertGreater(len(first), 1)
        self.assertEqual({(e['profile_id'], e['effective']['temperature'], e['overrides']['seed']) for e in first}, {(first[0]['profile_id'], 1.0, first[0]['overrides']['seed'])})
        self.assertEqual(first[0]['profile_id'], 'low-latency')     # the server's own selection, resolved at start-up
        self.assertEqual({(e['profile_id'], e['effective']['temperature']) for e in second}, {('quality', .8)})

    def test_a_reply_never_waits_on_resolving(self):
        slow = StandInServer(delay=30)                               # the speech server takes 30 s to resolve
        async def scenario(h):
            envelopes = self.recording(h)
            await h.candidate('a', h.audio('first'), final=True)
            await h.settle()
            return envelopes, h.first_audible('a')
        (envelopes, first_audible), _ = self.play_with(scenario, slow)
        fast = StandInServer(delay=0)
        async def baseline(h):
            await h.candidate('a', h.audio('first'), final=True)
            await h.settle()
            return h.first_audible('a')
        expected, _ = self.play_with(baseline, fast)
        self.assertAlmostEqual(first_audible, expected, delta=.05)   # first-sentence timing unchanged
        envelope = envelopes[0][1]
        self.assertNotIn('effective', envelope)                     # nothing resolved yet: the minimal envelope, with its seed
        self.assertIsInstance(envelope['overrides']['seed'], int)

    def test_the_speech_server_down_when_choosing_shows_the_error_and_keeps_the_previous_voice(self):
        server = StandInServer()
        async def scenario(h):
            envelopes = self.recording(h)
            self.assertTrue((await self.tune(h, 'quality'))['ok'])
            server.down = True
            refused = await self.tune(h, 'low-latency')
            self.assertEqual((refused['ok'], refused.get('refused')), (False, True))
            self.assertIn('HTTP 503', refused['message'])
            status = await self.tune(h, read=True)
            self.assertEqual((status['profile_id'], status['source']), ('quality', 'own'))
            await h.candidate('a', h.audio('first'), final=True)
            await h.settle()
            return envelopes
        envelopes, _ = self.play_with(scenario, server)
        self.assertEqual({e['profile_id'] for _, e in envelopes}, {'quality'})

    def test_a_failed_background_refresh_keeps_the_last_good_values_and_says_so_once(self):
        server = StandInServer()
        async def scenario(h):
            envelopes = self.recording(h)
            await h.candidate('a', h.audio('first'), final=True)
            await h.settle()
            server.down = True
            await h.advance(h.module.TUNING_REFRESH + 1)           # the snapshot is old: the next reply asks for a refresh after it
            await h.candidate('b', h.audio('second'), final=True)
            await h.settle()
            await h.advance(1)
            status = await self.tune(h, read=True)
            notices = [p for _, p in h.events('notice') if 'could not be refreshed' in p.get('message', '')]
            return envelopes, status, notices
        (envelopes, status, notices), _ = self.play_with(scenario, server)
        second = [e for turn, e in envelopes if turn == 'b']
        self.assertEqual({(e['profile_id'], e['effective']['temperature']) for e in second}, {('low-latency', 1.0)})   # last good values
        self.assertIn('HTTP 503', status['error'])
        self.assertEqual(status['effective']['temperature'], 1.0)
        self.assertEqual(len(notices), 1)

    def test_while_starting_or_after_an_interruption_a_choice_waits_instead_of_erroring(self):
        server = StandInServer()
        async def scenario(h):
            h.state['preparing'] = True                              # a chat switch or start-up in progress
            pending = await self.tune(h, 'quality')
            self.assertEqual((pending['ok'], pending.get('pending')), (True, True))
            h.state['preparing'] = False
            unknown = await h.handler.process('convo.tune', {'session_id': 'not-yet-known', 'profile_id': 'quality'}, 'browser')
            self.assertEqual((unknown['ok'], unknown.get('pending')), (True, True))
            old = h.ids()['epoch']
            await h.interrupt()
            stale = await h.handler.process('convo.tune', {'session_id': h.session['id'], 'epoch': old, 'profile_id': 'quality', 'overrides': {}}, 'browser')
            self.assertEqual((stale['ok'], stale['profile_id']), (True, 'quality'))   # a voice choice is not epoch-scoped
        self.play_with(scenario, server)

    def test_an_override_that_does_not_fit_the_preset_is_refused_before_it_is_used(self):
        server = StandInServer()
        async def scenario(h):
            refused = await self.tune(h, 'low-latency', {'first_block_frames': 6})   # larger than its steady block of 4
            self.assertFalse(refused['ok'])
            self.assertIn('first block', refused['message'])
            self.assertEqual((await self.tune(h, read=True))['source'], 'default')
        self.play_with(scenario, server)


class Fit(unittest.TestCase):
    def test_overrides_that_no_longer_fit_a_preset_are_dropped_when_switching(self):
        self.assertEqual(synthesis.fit_problem({'first_block_frames': 4, 'steady_block_frames': 8}), '')
        self.assertIn('first block', synthesis.fit_problem({'first_block_frames': 9, 'steady_block_frames': 8}))
        self.assertIn('300', synthesis.fit_problem({'steady_block_frames': 290}))
        kept = synthesis.fitting(VALUES['low-latency'], {'first_block_frames': 6, 'temperature': .7, 'seed': 3})
        self.assertEqual(kept, {'temperature': .7, 'seed': 3})
        self.assertEqual(synthesis.fitting(VALUES['quality'], {'first_block_frames': 6}), {'first_block_frames': 6})


if __name__ == '__main__':
    unittest.main()
