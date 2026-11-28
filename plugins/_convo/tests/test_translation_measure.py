"""The live-translation measurement (tests/translation): its texts, its cutting and scoring, and that it sends nothing unless it is told to,
and then only where it is told to. No real model or speech server is used here."""
import importlib.util
import io
import json
import math
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent / 'translation'
spec = importlib.util.spec_from_file_location('_convo_translation_measure', ROOT / 'measure.py')
measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure)
DATA = measure.load()


def speech(*parts):
    """Samples: (seconds, loud) after one another."""
    out = []
    for seconds, loud in parts:
        out += [int(loud * math.sin(index * .2)) for index in range(int(seconds * measure.RATE))]
    return out


class Texts(unittest.TestCase):
    def test_four_languages_both_ways_and_enough_of_each(self):
        self.assertEqual(set(DATA['languages']), {'en', 'es', 'de', 'ja'})
        directions = {}
        for run in measure.runs(DATA):
            directions.setdefault((run['source'], run['target']), []).append(run['id'])
        self.assertEqual({key: len(value) for key, value in directions.items()},
                         {(a, b): 8 for a, b in (('en', 'es'), ('en', 'de'), ('en', 'ja'), ('es', 'en'), ('de', 'en'), ('ja', 'en'))})
        self.assertEqual(len({run['id'] for run in measure.runs(DATA)}), 48)
        self.assertEqual(len(measure.runs(DATA, limit=2)), 12)
        self.assertEqual({run['target'] for run in measure.runs(DATA, (('en', 'ja'),))}, {'ja', 'en'})

    def test_every_reference_holds_every_fact_of_its_text(self):
        for run in measure.runs(DATA):
            self.assertGreaterEqual(len(run['facts']), 3, run['id'])
            for fact, count in zip(run['facts'], measure.facts_kept(run['facts'], run['reference'])):
                self.assertGreaterEqual(count, 1, (run['id'], fact))
                for spelling in fact:
                    re.compile(spelling)
            self.assertNotEqual(run['text'], run['reference'])
            # A fact is something a translation may lose: the source text does not hold it by itself (names and numbers may).
            self.assertLess(measure.compare(run['facts'], '', run['reference'])['kept'], 1)

    def test_no_other_texts_reference_passes_for_this_one(self):
        wanted = {}
        for run in measure.runs(DATA):
            wanted.setdefault(run['target'], []).append(run)
        for found in wanted.values():
            for run in found:
                for other in found:
                    if other['id'] != run['id']:
                        kept = measure.compare(run['facts'], other['reference'], run['reference'])
                        self.assertLess(kept['kept'], kept['of'], (run['id'], other['id']))


class Cutting(unittest.TestCase):
    def test_pieces_end_in_the_pauses_and_hold_all_the_speech(self):
        samples = speech((1.8, 9000), (.3, 20), (2.2, 9000), (.3, 20), (1.9, 9000), (.5, 20))
        pieces = measure.pieces_of(samples, 'small pauses')
        self.assertEqual(len(pieces), 3)
        for (_, end), middle in zip(pieces, (1.95, 4.45, 6.5)):
            self.assertAlmostEqual(end, middle, delta=.4)
        self.assertAlmostEqual(sum(len(audio) for audio, _ in pieces) / measure.RATE, len(samples) / measure.RATE, delta=.6)

    def test_a_pause_too_early_does_not_cut_and_speech_without_one_is_cut_anyway(self):
        self.assertEqual(len(measure.pieces_of(speech((.6, 9000), (.25, 20), (.5, 9000)), 'small pauses')), 1)
        long = measure.pieces_of(speech((13.0, 9000)), 'small pauses')
        self.assertGreaterEqual(len(long), 3)
        self.assertLessEqual(max(len(audio) for audio, _ in long) / measure.RATE, 5.1)
        clauses = measure.pieces_of(speech((1.8, 9000), (.2, 20), (2.0, 9000), (.6, 20), (3.0, 9000)), 'clauses')
        self.assertEqual(len(clauses), 2)                                              # a small pause is not a clause's end

    def test_fixed_pieces_are_about_as_long_as_asked_and_one_piece_is_everything(self):
        samples = speech((9.0, 9000))
        fixed = measure.pieces_of(samples, 'fixed 2 s')
        self.assertTrue(all(1.5 <= len(audio) / measure.RATE <= 2.9 for audio, _ in fixed[:-1]), [len(a) / measure.RATE for a, _ in fixed])      # the last is what is left
        self.assertEqual(len(measure.pieces_of(samples, 'whole')), 1)
        self.assertEqual(measure.pieces_of([0] * measure.RATE * 3, 'small pauses'), [])  # silence is no piece

    def test_speech_of_any_rate_and_two_channels_is_read_as_16_khz(self):
        import wave
        for rate, channels in ((24000, 1), (44100, 2), (16000, 1), (8000, 1)):
            out = io.BytesIO()
            with wave.open(out, 'wb') as clip:
                clip.setnchannels(channels); clip.setsampwidth(2); clip.setframerate(rate)
                clip.writeframes(b'\x10\x00' * channels * rate)
            samples = measure.read_wav(out.getvalue())
            self.assertAlmostEqual(len(samples) / measure.RATE, 1.0, delta=.01)
            self.assertEqual(set(samples[10:-10]), {16})
        self.assertEqual(measure.read_wav(measure.wav_bytes([1, 2, 3])), [1, 2, 3])


class Reading(unittest.TestCase):
    def test_the_three_lines_in_any_dress(self):
        self.assertEqual(measure.parse('LANGUAGE: Spanish\nHEARD: buenos días\nSAY: good morning'), {'language': 'Spanish', 'heard': 'buenos días', 'say': 'good morning'})
        self.assertEqual(measure.parse('**Language:** German\n- Heard: ich habe\n* Say: I have\nlost my key')['say'], 'I have lost my key')
        for nothing in ('SAY: ...', 'SAY: …', 'SAY:', 'HEARD: x', '', None):
            self.assertEqual(measure.parse(nothing)['say'], '')
        self.assertFalse(measure.say_started('LANGUAGE: Spanish\nHEARD: buenos días\nSAY:'))
        self.assertTrue(measure.say_started('LANGUAGE: Spanish\nHEARD: buenos días\nSAY: g'))

    def test_the_language_is_recognised_by_name_or_code_in_any_language(self):
        for said, language, right in (('Spanish', 'es', True), ('Español', 'es', True), ('es-ES', 'es', True), ('Deutsch', 'de', True), ('日本語', 'ja', True),
                                      ('English (US)', 'en', True), ('French', 'es', False), ('', 'en', False)):
            self.assertEqual(measure.recognised(said, language), right, said)

    def test_a_judgement_is_one_word(self):
        self.assertEqual([measure.verdict(text) for text in ('YES', 'yes.', 'NO', 'No, because', 'It is fine', '', None)], [True, True, False, False, None, None, None])


class Scoring(unittest.TestCase):
    FACTS = [['two', r'\b2\b'], ['friday'], ['not', "n't"]]

    def test_what_is_lost_and_what_is_said_twice_at_the_cuts(self):
        whole = 'We do not need the 2 chairs before Friday.'
        self.assertEqual(measure.compare(self.FACTS, whole, whole), {'of': 3, 'kept': 3, 'lost': 0, 'doubled': 0})
        self.assertEqual(measure.compare(self.FACTS, "We don't need the chairs before Friday. Before Friday.", whole), {'of': 3, 'kept': 2, 'lost': 1, 'doubled': 1})
        self.assertEqual(measure.compare(self.FACTS, '', whole)['lost'], 3)
        self.assertEqual(measure.found(['two', 'two chairs'], 'the two chairs'), 1)     # one place, however it may be spelled

    def test_likeness_in_any_script(self):
        self.assertAlmostEqual(measure.chrf('the same words', 'the same words'), 1.0)
        self.assertEqual(measure.chrf('abc def', 'xyz uvw'), 0.0)
        self.assertGreater(measure.chrf('電車は空港に行きますか', '電車は空港へ行きますか'), .5)
        self.assertGreater(measure.chrf('we met last year', 'we met in Vienna last year'), measure.chrf('it is raining', 'we met in Vienna last year'))
        self.assertEqual(measure.chrf('', 'something'), 0.0)

    def test_how_far_behind_the_speaker(self):
        # Quick enough: each piece is heard its text time and the first sound after it ended.
        self.assertEqual([round(delay, 3) for delay in measure.timeline([{'end': 2.0, 'text': .5, 'seconds': 1.0}, {'end': 5.0, 'text': .5, 'seconds': 1.0}], 1.0)], [1.5, 1.5])
        # Longer to say than to hear: every piece waits for the one before it, so the delay grows.
        slow = measure.timeline([{'end': 2.0 * n, 'text': 1.0, 'seconds': 3.0} for n in range(1, 6)], 1.0)
        self.assertEqual([round(delay, 3) for delay in slow], [2.0, 3.0, 4.0, 5.0, 6.0])
        self.assertEqual(measure.timeline([{'end': 2.0, 'text': 1.0, 'seconds': 0}], 1.0), [])    # nothing said yet
        self.assertEqual(measure.spread([3, 1, 2, None]), {'n': 3, 'usual': 2, 'slow (9 of 10)': 3, 'slowest': 3})
        self.assertIsNone(measure.spread([]))

    def test_the_rule_stage_one_is_held_to(self):
        line = {'pieces that keep their meaning (judged)': .91}
        self.assertTrue(measure.passes(line, 6.9))
        self.assertFalse(measure.passes(line, 7.1))
        self.assertFalse(measure.passes({'pieces that keep their meaning (judged)': .89}, 3.0))
        self.assertFalse(measure.passes({'pieces that keep their meaning (judged)': None}, 3.0))    # not judged is not passed
        self.assertFalse(measure.passes(line, None))


class FakeServer:
    """Answers like a perfect interpreter that hears everything at once; records what it was sent."""
    made = []

    def __init__(self, url='', key='', **_):
        self.url, self.key, self.asked, self.spoken = url, key, [], []
        FakeServer.made.append(self)

    def chat(self, body, started=measure.say_started):
        self.asked.append(body)
        if body['messages'][0]['content'] == measure.JUDGE:
            return 'YES', .1, .1, .1
        return 'LANGUAGE: English\nHEARD: some words\nSAY: a translation', .2, .4, .6

    def speech(self, model, voice, text, language=''):
        self.spoken.append((model, voice, text, language))
        return measure.wav_bytes(speech((2.0, 9000), (.3, 20), (2.0, 9000))), 1.0, 2.0


class Running(unittest.TestCase):
    def setUp(self):
        FakeServer.made = []

    def test_without_go_nothing_is_sent_and_it_says_what_it_would_send(self):
        said = []
        self.assertEqual(measure.main(['--model-url', 'http://127.0.0.1:1/v1'], say=said.append, server=lambda *a, **k: self.fail('nothing may be opened')), 0)
        self.assertIn('Nothing was sent', said[-1])
        plan = json.loads(said[0].split(': ', 1)[1])
        self.assertEqual((plan['texts spoken'], plan['runs'], plan['judge requests']), (32, 48, 0))
        self.assertGreater(plan['interpreter requests'], 300)

    def test_it_goes_only_where_it_is_told_and_never_outside_by_itself(self):
        said = []
        full = ['--go', '--model', 'm', '--speech-model', 's', '--voice', 'v', '--model-url', 'http://127.0.0.1:1/v1', '--speech-url']
        for address in ('https://api.example.com/v1', 'http://8.8.8.8/v1', 'example.org:80'):
            self.assertEqual(measure.main(full + [address], say=said.append, server=lambda *a, **k: self.fail('nothing may be opened')), 2, address)
            self.assertIn('Refused', said[-1])
        self.assertEqual(measure.main(['--go'], say=said.append, server=lambda *a, **k: self.fail('nothing may be opened')), 2)
        self.assertIn('Missing: --model-url', said[-1])
        for host, inside in (('127.0.0.1', True), ('::1', True), ('192.168.1.5', True), ('100.101.102.103', True), ('desktop', True), ('pc.local', True), ('pc.tail1.ts.net', True),
                             ('host.docker.internal', True), ('100.128.0.1', False), ('example.com', False), ('', False)):
            self.assertEqual(measure.local(host), inside, host)

    def test_a_whole_run_on_a_stand_in(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'A_TEST_KEY': 'test-key-value'}):
            said = []
            code = measure.main(['--go', '--model-url', 'http://127.0.0.1:1/v1', '--model', 'm', '--speech-url', 'http://127.0.0.1:2/v1', '--speech-model', 's', '--voice', 'v',
                                 '--quick-url', 'http://127.0.0.1:3/v1', '--quick-model', 'q', '--quick-voice', 'w', '--judge', '--limit', '1', '--key-env', 'A_TEST_KEY',
                                 '--speak', '3', '--out', folder], say=said.append, server=FakeServer)
            self.assertEqual(code, 0)
            report = json.loads((Path(folder) / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(set(report['ways of cutting']), set(measure.CUTS))
            line = report['ways of cutting']['small pauses']
            self.assertEqual((line['texts'], line['pieces that keep their meaning (judged)'], line['nothing said yet']), (6, 1.0, 0.0))
            self.assertEqual(line['seconds to the first word of the translation']['usual'], .4)
            self.assertEqual(report['seconds to the first sound']['a quicker voice']['usual'], 1.0)
            self.assertTrue(line['passes stage 1'])
            self.assertIn('| some words | a translation | yes |', (Path(folder) / 'pieces.md').read_text(encoding='utf-8'))
            everything = ' '.join(said) + (Path(folder) / 'report.json').read_text(encoding='utf-8') + (Path(folder) / 'pieces.md').read_text(encoding='utf-8')
            self.assertNotIn('test-key-value', everything)
            model = FakeServer.made[0]
            self.assertEqual(model.key, 'test-key-value')                              # only the conversation model gets the key
            self.assertEqual([server.key for server in FakeServer.made[1:]], [''] * (len(FakeServer.made) - 1))
            # A second model that judges is another server, without that key.
            before = len(FakeServer.made)
            measure.main(['--go', '--model-url', 'http://127.0.0.1:1/v1', '--model', 'm', '--speech-url', 'http://127.0.0.1:2/v1', '--speech-model', 's', '--voice', 'v', '--limit', '1',
                          '--judge', '--judge-url', 'http://127.0.0.1:4/v1', '--judge-model', 'j', '--key-env', 'A_TEST_KEY', '--cuts', 'clauses', '--speak', '0', '--out', folder],
                         say=lambda line: None, server=FakeServer)
            self.assertEqual([server.key for server in FakeServer.made[before:]], ['test-key-value', '', ''])
            judged = [body['messages'][0]['content'] == measure.JUDGE for body in next(s for s in FakeServer.made[before:] if s.url.endswith(':4/v1')).asked]
            self.assertTrue(judged and all(judged))
            # Every piece carries its audio and the pieces before it as text.
            parts = [body['messages'][1]['content'] for body in model.asked if body['messages'][0]['content'] != measure.JUDGE]
            self.assertTrue(all(part[1]['type'] == 'input_audio' for part in parts))
            self.assertTrue(any('Heard: some words\nSaid: a translation' in part[0]['text'] for part in parts))
            # A second run does not speak the test texts again.
            before = len(FakeServer.made)
            measure.main(['--go', '--model-url', 'http://127.0.0.1:1/v1', '--model', 'm', '--speech-url', 'http://127.0.0.1:2/v1', '--speech-model', 's', '--voice', 'v', '--limit', '1',
                          '--cuts', 'clauses', '--speak', '0', '--out', folder], say=lambda line: None, server=FakeServer)
            self.assertEqual([server.spoken for server in FakeServer.made[before:]], [[], []])

    def test_the_stream_is_read_as_it_comes(self):
        chunks = [b'data: {"choices":[{"delta":{"content":"LANGUAGE: Spanish\\nHEARD: hola\\n"}}]}\n', b': keep-alive\n', b'\n',
                  b'data: {"choices":[{"delta":{"content":"SAY: hel"}}]}\n', b'data: not json\n', b'data: {"choices":[{"delta":{"content":"lo"}}]}\n', b'data: [DONE]\n']
        times = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        sent = []

        class Answer:
            status = 200
            lines = iter(chunks)

            def readline(self):
                return next(self.lines, b'')

        class Connection:
            def request(self, method, path, body, headers):
                sent.append((method, path, json.loads(body), headers))

            def getresponse(self):
                return Answer()
        server = measure.Server('http://127.0.0.1:9/v1', 'k', opener=Connection, clock=lambda: next(times))
        self.assertEqual(server.chat({'model': 'm'}), ('LANGUAGE: Spanish\nHEARD: hola\nSAY: hello', 1.0, 2.0, 4.0))
        self.assertEqual((sent[0][:2], sent[0][3]['Authorization']), (('POST', '/v1/chat/completions'), 'Bearer k'))


if __name__ == '__main__':
    unittest.main()
