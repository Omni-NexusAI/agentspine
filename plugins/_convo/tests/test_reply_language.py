"""Every language the models handle, voiced correctly (plan 2026-11-10, change 6): the declared LANGUAGE line mapped to the speech
backend's names, one language per reply, never the previous reply's, a guess only with a clear majority, the script check only on
a clear contradiction, and notices and a tool's follow-up in their reply's language. Offline: scripted providers, virtual clock."""
import importlib
import unittest

if __package__:
    from .behavior_harness import SPEAK_PLAIN, script
    from .test_behavior_corpus import WEB_SEARCH, Scenario
else:
    from behavior_harness import SPEAK_PLAIN, script
    from test_behavior_corpus import WEB_SEARCH, Scenario

language = importlib.import_module('plugins._convo.helpers.convo_language')
providers = importlib.import_module('plugins._convo.helpers.speech_providers')
QWEN3 = providers.QWEN3_LANGUAGES


class Mapping(unittest.TestCase):
    def test_names_native_names_iso_codes_and_regional_tags_map_to_the_backends_names(self):
        table = {'Spanish': 'Spanish', 'español': 'Spanish', 'es': 'Spanish', 'es-MX': 'Spanish', 'pt-BR': 'Portuguese',
                 'Português': 'Portuguese', 'zh_CN': 'Chinese', 'Mandarin': 'Chinese', '日本語': 'Japanese', 'jp': 'Japanese',
                 'ko': 'Korean', 'Deutsch': 'German', 'fr': 'French', 'RU': 'Russian', 'it': 'Italian', 'English (US)': 'English',
                 '**English**': 'English', 'en-gb': 'English'}
        for declared, expected in table.items():
            self.assertEqual(language.mapped(declared, QWEN3), expected, declared)

    def test_a_language_the_backend_does_not_take_is_auto_and_a_backend_without_a_field_gets_none(self):
        for declared in ('Dutch', 'nl', 'Ukrainian', 'Klingon', ''):
            self.assertEqual(language.mapped(declared, QWEN3), 'Auto', declared)
        self.assertIsNone(language.mapped('Spanish', None))

    def test_accepted_languages_per_speech_backend(self):
        self.assertEqual(providers.accepted_languages({'provider': 'audiocpp', 'connection': 'external'}), QWEN3)
        self.assertEqual(providers.accepted_languages({'provider': 'audiocpp', 'connection': 'managed', 'backend': 'managed_audio'}), QWEN3)
        self.assertEqual(providers.accepted_languages({'backend': 'openai'}), QWEN3)              # an older configuration
        for role in ({'provider': 'openai_tts', 'backend': 'openai'}, {'provider': 'kokoro_native', 'backend': 'host_kokoro'},
                     {'provider': 'llamacpp', 'backend': 'openai'}):
            self.assertIsNone(providers.accepted_languages(role), role)
        self.assertEqual(len(QWEN3), 11)


class OneLanguagePerReply(unittest.TestCase):
    def test_the_declaration_decides_even_with_an_english_product_name(self):
        self.assertEqual(language.reply_language('Spanish', 'Puedes usar el iPhone Pro Max para eso.', '', QWEN3), ('Spanish', 'declared'))

    def test_an_unmapped_declaration_is_auto_for_the_whole_reply(self):
        self.assertEqual(language.reply_language('Dutch', 'Dat is een goed idee.', '', QWEN3), ('Auto', 'unmapped'))

    def test_without_a_header_a_bare_si_is_auto_unless_the_users_words_say_more(self):
        self.assertEqual(language.reply_language('', 'Sí.', '', QWEN3), ('Auto', 'unclear'))
        self.assertEqual(language.reply_language('', 'Sí.', '¿Me puedes decir qué hora es en la ciudad, por favor?', QWEN3), ('Spanish', 'guess'))

    def test_the_guess_needs_a_clear_majority(self):
        self.assertEqual(language.detect('La capital de Kansas es Topeka, y está en el centro.'), 'Spanish')
        self.assertEqual(language.detect('The weather is nice and you can go there'), 'English')
        self.assertEqual(language.detect('the la die de el'), '')                  # mixed signals: no guess
        self.assertEqual(language.reply_language('', 'Okay, sir.', '', QWEN3), ('Auto', 'unclear'))

    def test_a_first_phrase_in_another_script_keeps_the_declared_reply_language_and_is_voiced_in_its_own(self):
        # Review 2026-11-17 #5: the contradiction used to make the whole reply Russian; now only that phrase is.
        self.assertEqual(language.reply_language('English', 'Привет, как дела у тебя сегодня?', '', QWEN3), ('English', 'declared'))
        self.assertEqual(language.phrase_language('English', 'Привет, как дела у тебя сегодня?', QWEN3), 'Russian')
        self.assertEqual(language.phrase_language('English', 'That means hello, how are you today.', QWEN3), 'English')

    def test_a_reply_in_a_script_language_can_voice_a_latin_phrase_in_its_language(self):
        self.assertEqual(language.phrase_language('Japanese', 'The weather is nice and you can go there.', QWEN3), 'English')
        self.assertEqual(language.phrase_language('Japanese', 'OK.', QWEN3), 'Japanese')            # unclear: the reply's
        self.assertEqual(language.phrase_language('Russian', 'Это хорошо.', QWEN3), 'Russian')

    def test_without_a_header_one_cyrillic_letter_or_one_kana_never_decides(self):
        self.assertEqual(language.detect('The answer is option я, I think.'), 'English')         # never Russian
        self.assertEqual(language.detect('Say ありがとう when you leave'), '')                 # kana a minority of the letters
        self.assertEqual(language.detect('я'), '')
        self.assertEqual(language.detect('あ'), '')
        self.assertEqual(language.detect('Привет, как дела?'), 'Russian')
        self.assertEqual(language.detect('こんにちは、お元気ですか。'), 'Japanese')
        self.assertEqual(language.detect('東京は日本の首都です。'), 'Japanese')
        self.assertEqual(language.detect('你好，今天天气很好。'), 'Chinese')
        self.assertEqual(language.reply_language('', 'я', '', QWEN3), ('Auto', 'unclear'))
        self.assertNotEqual(language.reply_language('', 'Sure, here you go.', 'What does я mean in this word?', QWEN3)[0], 'Russian')

    def test_the_script_overrides_only_a_clear_contradiction(self):
        self.assertEqual(language.reply_language('English', 'In Japan you say こんにちは to greet people.', '', QWEN3), ('English', 'declared'))
        self.assertEqual(language.reply_language('Japanese', '東京は日本の首都です。', '', QWEN3), ('Japanese', 'declared'))   # kanji alone

    def test_a_sentence_wholly_in_another_script_is_voiced_in_its_language_a_single_word_never(self):
        self.assertEqual(language.phrase_language('English', 'こんにちは、お元気ですか。', QWEN3), 'Japanese')
        self.assertEqual(language.phrase_language('English', 'Say こんにちは when you arrive.', QWEN3), 'English')
        self.assertEqual(language.phrase_language('English', '你', QWEN3), 'English')
        self.assertIsNone(language.phrase_language(None, 'こんにちは、お元気ですか。', None))


def say(text):
    return [(text, .1)]


class Spoken(Scenario):
    def run_turns(self, replies, proposal=None, tts=None):
        """One spoken turn per scripted reply; returns the language each speech request carried, per turn."""
        async def scenario(h):
            heard = []
            original = h.sidecar.speech

            async def speech(role, text):
                heard.append((h.state.get('current_turn'), role.get('language'), text))
                return await original(role, text)
            h.sidecar.speech = speech
            for index, reply in enumerate(replies):
                h.model_deltas = say(reply)
                await h.until(h.now() + 2)
                await h.candidate('t%d' % index, h.audio('ask%d' % index), final=True)
                await h.settle()
                h.proposal = None
            return heard
        classifier = {'ask%d' % index: script(SPEAK_PLAIN, .2) for index in range(len(replies))}
        options = {'proposal': proposal, 'proposal_always': False}
        if tts:
            options['config'] = {'roles': {'tts': tts}}
        return self.play(scenario, classifier=classifier, **options)[0]

    def test_a_spanish_reply_with_an_english_product_name_makes_one_language_request(self):
        heard = self.run_turns(['USER: ¿Qué teléfono compro?\nLANGUAGE: Spanish\nTe recomiendo el iPhone. Es muy bueno. También el Pixel.'])
        self.assertTrue(heard)
        self.assertEqual({lang for _, lang, _ in heard}, {'Spanish'})

    def test_a_first_phrase_in_another_script_is_voiced_alone_and_the_rest_keeps_the_declared_language(self):
        async def scenario(h):
            heard, original = [], h.sidecar.speech

            async def speech(role, text):
                heard.append((role.get('language'), text))
                return await original(role, text)
            h.sidecar.speech = speech
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return heard
        deltas = [('USER: How do I greet someone in Russian?\nLANGUAGE: English\nПривет, как дела у тебя сегодня? ', .1),
                  ('That means hello, how are you today. You can say it to friends.', 2.0)]
        heard, _ = self.play(scenario, classifier={'ask': script(SPEAK_PLAIN, .2)}, model_deltas=deltas)
        self.assertGreaterEqual(len(heard), 2, heard)
        self.assertEqual(heard[0][0], 'Russian', heard)
        self.assertEqual({lang for lang, _ in heard[1:]}, {'English'})

    def test_dutch_goes_as_auto_throughout(self):
        heard = self.run_turns(['USER: Hoe gaat het?\nLANGUAGE: Dutch\nHet gaat goed. Dank je wel voor het vragen.'])
        self.assertEqual({lang for _, lang, _ in heard}, {'Auto'})

    def test_the_previous_replys_language_is_never_carried_over(self):
        heard = self.run_turns(['USER: Hola\nLANGUAGE: Spanish\nHola, ¿qué tal?', 'Okay.'])
        by_turn = {turn: lang for turn, lang, _ in heard}
        self.assertEqual(by_turn, {'t0': 'Spanish', 't1': 'Auto'})

    def test_an_acknowledgement_and_its_tool_follow_up_share_one_language(self):
        heard = self.run_turns(['USER: ¿Qué tiempo hace?\nLANGUAGE: Spanish\nUn momento, lo busco.'], proposal=WEB_SEARCH)
        self.assertGreaterEqual(len(heard), 2)                     # the acknowledgement and the answer to the result
        self.assertEqual({lang for _, lang, _ in heard}, {'Spanish'})

    def test_a_backend_without_a_language_field_is_sent_none(self):
        heard = self.run_turns(['USER: Hola\nLANGUAGE: Spanish\nHola, ¿qué tal?'], tts={'provider': 'openai_tts'})
        self.assertTrue(heard)
        self.assertEqual({lang for _, lang, _ in heard}, {''})

    def test_the_rules_ask_the_model_to_follow_the_user_and_keep_a_requested_language(self):
        async def scenario(h):
            await h.candidate('t', h.audio('ask'), final=True)
            await h.settle()
            return h.answers[0]['system'].split('\n⁣[convo:this-turn]')[0]
        stable, _ = self.play(scenario, classifier={'ask': script(SPEAK_PLAIN, .2)})
        for words in ('may speak any language or accent, or switch mid-sentence', 'follow them naturally',
                      'keep using it until they ask otherwise'):
            self.assertIn(words, stable)


if __name__ == '__main__':
    unittest.main()
