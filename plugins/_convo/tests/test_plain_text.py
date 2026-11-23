"""Markdown never reaches the voice or the history (2026-11-17: a reply's "**" and bullets were spoken and stored).

helpers/convo_plaintext.spoken removes the markup and keeps the words, by the syntax's form; webui/plain-text.js is its browser copy
for the live draft. Both are checked against one hand-written table (tests/fixtures/plain-text-cases.json)."""
import json
import unittest
from pathlib import Path

if __package__:
    from .behavior_harness import Harness, SPEAK, run_virtual, script
else:
    from behavior_harness import Harness, SPEAK, run_virtual, script

from plugins._convo.helpers.convo_firstclause import FirstClause
from plugins._convo.helpers.convo_plaintext import spoken

CASES = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'plain-text-cases.json').read_text(encoding='utf-8'))['cases']
REPLY = ("## Planets with rings\n**Saturn** has the *brightest* rings by far. Jupiter has faint ones, found in 1979.\n"
         "- **Uranus** has thirteen known rings.\n- Neptune's are dark: see [NASA](https://science.nasa.gov/neptune/).\n"
         "So *every* giant planet has rings, even if `most` are hard to see.")
MID_LINE = "Saturn has the brightest rings of all the planets. - Jupiter has faint ones. **Uranus** too."


def squash(text):
    return ' '.join(text.split())


class PlainText(unittest.TestCase):
    def test_every_case_in_the_shared_table(self):
        for case in CASES:
            with self.subTest(case['name']):
                self.assertEqual(spoken(case['in'], line_start=case.get('line_start', True)), case['out'])

    def test_nothing_that_is_not_markup_is_lost(self):
        for case in CASES:
            letters = lambda text: [c for c in text if c.isalnum()]
            if not any(markup in case['in'].lower() for markup in ('](', '```', '<br')):   # an address, a fence's language, a tag
                with self.subTest(case['name']):
                    self.assertEqual(letters(spoken(case['in'], line_start=case.get('line_start', True))), letters(case['in']))

    def test_phrases_cleaned_one_by_one_join_up_to_the_reply_cleaned_whole(self):
        """What is heard (each phrase, cleaned as it is cut from the reply) and what was generated (the whole reply, cleaned before it
        is settled) must compare like for like: the panel calls a reply interrupted when less was heard than generated."""
        for reply in (REPLY, MID_LINE):
            clause, phrases, rest = FirstClause(), [], reply
            taken = clause.take(rest)
            while taken:
                phrase, rest = taken
                phrases.append(phrase)
                taken = clause.take(rest)
            phrases.append(rest)
            self.assertGreater(len(phrases), 1)
            # As the reply loop does (realtime `say`): each piece knows whether it begins a line; the speech queue trims what it speaks
            # and the history joins the phrases with one space.
            line_open, heard = False, []
            for phrase in phrases:
                heard.append(spoken(phrase, line_start=not line_open).strip())
                tail = phrase.rstrip(' \t')
                if tail:
                    line_open = not tail.endswith('\n')
            self.assertEqual(squash(' '.join(piece for piece in heard if piece)), squash(spoken(reply)), reply)
        for marker in ('**', '##', '](', '`', '\n- '):
            self.assertNotIn(marker, spoken(REPLY))
        self.assertIn(' - Jupiter', spoken(MID_LINE))                   # a dash inside a line is not a bullet, cut or whole

    def test_text_without_markup_is_returned_as_is(self):
        for text in ('Saturn has rings.', 'It is 5 * 3, not 2*3.', 'Open my_file.txt', ' leading and trailing \n', ''):
            self.assertEqual(spoken(text), text)
            self.assertEqual(spoken(text, line_start=False), text)
        self.assertEqual(spoken(None), '')


class SpokenAndRemembered(unittest.TestCase):
    """End to end through the real handler (scripted model and speech): what is synthesised, what the history keeps as heard, and what
    is settled as generated carry no markup, and the panel's heard-against-generated comparison still sees the whole reply."""
    def test_markdown_in_a_reply_is_never_spoken_or_remembered(self):
        deltas = [('## Rings\n**Saturn** has the *brightest* rings by far.\n', .1),
                  ('- Jupiter has faint ones; see [NASA](https://science.nasa.gov/jupiter/). ', .1), ('That is `all` - really.', .1)]
        async def run():
            h = await Harness.create(model_deltas=deltas, classifier={'question': script(SPEAK, .2)})
            try:
                await h.candidate('q', h.audio('question'), final=True)
                await h.settle()
                for _, payload in h.audible('q'):
                    await h.playback('q', payload['phrase_id'])
                heard = h.store.context('a')['turns'][-1]['content']['assistant']        # the history the model reads next
                generated = [r['payload']['text'] for r in h.journal('assistant.generated')]
                return [text for _, text in h.spoken], heard, generated
            finally:
                await h.close()
        spoken_texts, heard, generated = run_virtual(run())
        self.assertTrue(spoken_texts)
        for text in spoken_texts + [heard] + generated:
            for marker in ('**', '##', '](', '`', '\n- ', '*b'):
                self.assertNotIn(marker, text)
        self.assertEqual(squash(heard), squash(generated[0]))                    # heard in full: the panel shows no interruption
        self.assertEqual(squash(heard), 'Rings Saturn has the brightest rings by far. Jupiter has faint ones; see NASA. That is all - really.')


if __name__ == '__main__':
    unittest.main()
