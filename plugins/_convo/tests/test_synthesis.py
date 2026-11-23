"""Advanced synthesis controls for the user's own audio.cpp speech server: bounds, the per-reply envelope (one seed per reply), the
server's resolve answer, and what each speech request carries. Stand-in servers only; nothing is synthesised."""
import asyncio
import importlib
import json
import unittest
import unittest.mock

if __package__:
    from .behavior_harness import LONG_REPLY, SPEAK, script
    from .test_behavior_corpus import Scenario
else:
    from behavior_harness import LONG_REPLY, SPEAK, script
    from test_behavior_corpus import Scenario

synthesis = importlib.import_module('plugins._convo.helpers.convo_synthesis')
contract = importlib.import_module('plugins._convo.helpers.convo_contract')
OWN = {'provider': 'audiocpp', 'connection': 'external', 'url': 'http://localhost:8890/v1', 'model': 'qwen3-tts-1.7b-base-bf16'}


class Values(unittest.TestCase):
    def test_overrides_stay_inside_the_servers_bounds(self):
        self.assertEqual(synthesis.validate_overrides({'temperature': .7, 'top_k': 40, 'seed': '', 'top_p': None}), {'temperature': .7, 'top_k': 40})
        for bad in ({'temperature': 0}, {'temperature': 2.5}, {'top_k': 1.5}, {'seed': -1}, {'top_p': True}, {'unknown': 1},
                    {'first_block_frames': 10, 'steady_block_frames': 5}, {'steady_block_frames': 290, 'left_context_frames': 20}):
            with self.subTest(bad), self.assertRaises(ValueError):
                synthesis.validate_overrides(bad)

    def test_the_saved_setting_is_profile_and_overrides_only(self):
        self.assertEqual(synthesis.validate_tuning(None), {'profile_id': '', 'overrides': {}})
        with self.assertRaises(ValueError):
            synthesis.validate_tuning({'profile_id': 'x', 'overrides': {}, 'extra': 1})
        with self.assertRaises(contract.ContractError):
            contract.validate_settings({'roles': {'tts': {'tuning': {'profile_id': 'x', 'overrides': {'top_k': 999}}}}})

    def test_only_the_users_own_audio_cpp_server(self):
        self.assertTrue(synthesis.applies(OWN))
        self.assertTrue(synthesis.applies({**OWN, 'connection': ''}))
        for role in ({**OWN, 'connection': 'managed'}, {**OWN, 'provider': 'openai'}, {'provider': 'kokoro'}, None):
            self.assertFalse(synthesis.applies(role))


class Envelope(unittest.TestCase):
    def test_every_reply_gets_one_seed_a_chosen_seed_is_kept(self):
        seeds = iter([11, 22])
        first, second = (synthesis.reply_tuning(OWN, rng=lambda bits: next(seeds)) for _ in range(2))
        self.assertEqual((first['overrides']['seed'], second['overrides']['seed']), (11, 22))
        self.assertEqual((first['provider'], first['scope']), ('qwen3tts-audiocpp', 'realtime'))
        self.assertNotIn('profile_id', first)                                   # minimal: the server's own selection is used
        kept = synthesis.reply_tuning({**OWN, 'tuning': {'profile_id': '', 'overrides': {'seed': 7, 'temperature': .6}}}, rng=lambda bits: 99)
        self.assertEqual(kept['overrides'], {'seed': 7, 'temperature': .6})
        self.assertIsNone(synthesis.reply_tuning({**OWN, 'provider': 'openai'}))

    def test_the_sessions_choice_wins_and_a_resolved_profile_brings_its_complete_values(self):
        saved = {**OWN, 'tuning': {'profile_id': 'saved', 'overrides': {}}}
        resolved = {'profile_id': 'quality', 'profile_revision': 4, 'effective': {'temperature': .8, 'top_k': 50, 'seed': -1}}
        envelope = synthesis.reply_tuning(saved, {'profile_id': 'quality', 'overrides': {'top_k': 30}}, resolved, rng=lambda bits: 5)
        self.assertEqual((envelope['profile_id'], envelope['profile_revision']), ('quality', 4))
        self.assertEqual(envelope['effective'], {'temperature': .8, 'top_k': 50, 'seed': 5})          # the reply's seed everywhere
        self.assertEqual(envelope['overrides'], {'top_k': 30, 'seed': 5})
        stale = synthesis.reply_tuning(saved, {'profile_id': 'other', 'overrides': {}}, resolved, rng=lambda bits: 5)
        self.assertEqual((stale['profile_id'], 'effective' in stale), ('other', False))           # never another profile's values


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code, self.payload = status, payload

    async def aiter_bytes(self):
        yield json.dumps(self.payload).encode()


class FakeClient:
    def __init__(self, answers, seen):
        self.answers, self.seen = answers, seen

    def __call__(self, timeout):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        self.seen.append((method, url, json))
        status, payload = self.answers[url.rsplit('/v1', 1)[1]]
        response = FakeResponse(status, payload)

        class Context:
            async def __aenter__(self_inner):
                return response

            async def __aexit__(self_inner, *exc):
                return False
        return Context()


class Server(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_reads_the_profile_and_the_overrides_the_server_accepted(self):
        seen = []
        client = FakeClient({'/tuning/resolve': (200, {'profile': {'id': 'balanced', 'revision': 3, 'name': 'Balanced', 'temperature': .9, 'top_k': 50,
                                                                    'max_reference_seconds': 12, 'notes': 'not a value'},
                                                       'temporaryOverrides': {'top_k': 30}, 'warnings': []})}, seen)
        resolved = await synthesis.resolve(OWN, {}, client, '', {'top_k': 30})
        self.assertEqual(seen, [('POST', 'http://localhost:8890/v1/tuning/resolve', {'provider': 'qwen3tts-audiocpp', 'scope': 'realtime', 'overrides': {'top_k': 30}})])
        self.assertEqual(resolved, {'effective': {'temperature': .9, 'top_k': 30, 'max_reference_seconds': 12}, 'profile_id': 'balanced', 'profile_revision': 3})

    async def test_a_refused_resolve_says_why(self):
        client = FakeClient({'/tuning/resolve': (404, {'detail': 'Unknown tuning profile.'})}, [])
        with self.assertRaisesRegex(ValueError, 'HTTP 404.*Unknown tuning profile'):
            await synthesis.resolve(OWN, {}, client, 'missing', {})
        with self.assertRaises(ValueError):
            await synthesis.resolve(OWN, {}, client, '', {'temperature': 0})            # refused here, before anything is sent

    async def test_read_lists_profiles_and_the_servers_own_selection(self):
        client = FakeClient({'/tuning/profiles': (200, {'profiles': {'balanced': {'name': 'Balanced', 'revision': 2}, 'quality': {'name': 'Quality'}},
                                                        'selections': {'realtime': {'qwen3tts-audiocpp': 'quality'}}})}, [])
        found = await synthesis.read(OWN, {}, client)
        self.assertEqual([p['id'] for p in found['profiles']], ['balanced', 'quality'])
        self.assertEqual(found['selected'], {'id': 'quality', 'label': 'Quality'})


class Request(unittest.TestCase):
    def test_a_streamed_request_carries_the_replys_envelope_and_language(self):
        stream = importlib.import_module('plugins._convo.sidecar.speech_stream')
        envelope = synthesis.reply_tuning(OWN, rng=lambda bits: 42)
        role = {**OWN, 'voice': 'clone:abc', 'language': 'English', 'capabilities': {'audio_output': True, 'native_pcm': True}, 'reply_tuning': envelope}
        _, _, body = stream.speech_request(role, 'Hello there.')
        self.assertEqual((body['tuning'], body['language']), (envelope, 'English'))
        with self.assertRaises(contract.ContractError):
            stream.speech_request({**role, 'reply_tuning': {'overrides': 'x'}}, 'Hello there.')


class OneSeedPerReply(Scenario):
    def test_every_phrase_of_a_reply_shares_its_seed_and_the_next_reply_gets_another(self):
        async def scenario(h):
            seeds = []
            original = h.sidecar.speech
            async def speech(role, text):
                seeds.append((h.state.get('current_turn'), (role.get('reply_tuning') or {}).get('overrides', {}).get('seed')))
                return await original(role, text)
            h.sidecar.speech = speech
            await h.candidate('a', h.audio('first'), final=True)
            await h.settle()
            await h.candidate('b', h.audio('second'), final=True)
            await h.settle()
            return seeds
        async def offline(*args, **kwargs):           # never the user's speech server: the resolve is refused at once
            raise ValueError('offline test')
        with unittest.mock.patch.object(synthesis, 'resolve', new=offline):
            seeds, _ = self._play(scenario)
        first = {seed for turn, seed in seeds if turn == 'a'}
        second = {seed for turn, seed in seeds if turn == 'b'}
        self.assertEqual((len(first), len(second)), (1, 1))                  # one seed per reply, however many requests it took
        self.assertTrue(all(isinstance(seed, int) for seed in first | second))
        self.assertNotEqual(first, second)
        self.assertGreater(sum(1 for turn, _ in seeds if turn == 'a'), 1)    # the reply did take more than one request

    def _play(self, scenario):
        return self.play(scenario, model_deltas=LONG_REPLY, tts_delay=.2,
                             config={'roles': {'tts': {'provider': 'audiocpp', 'connection': 'external', 'url': 'http://localhost:8890/v1',
                                                       'model': 'qwen3-tts-1.7b-base-bf16', 'capabilities': {'audio_output': True}}}},
                             classifier={'first': script(SPEAK, .2), 'second': script(SPEAK, .2)})


if __name__ == '__main__':
    unittest.main()
