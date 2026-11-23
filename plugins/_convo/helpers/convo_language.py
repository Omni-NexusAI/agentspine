"""The language a reply is spoken in, decided once per reply so every phrase is synthesised alike.

A speech request without a language leaves Qwen3's engine on "Auto": it guesses per phrase, and a short or ambiguous phrase can be
voiced with another language's sound (one cause of a voice that shifts between statements). So one language is chosen per reply and
frozen for all its phrases:

1. The model's own choice: the hidden header's `LANGUAGE:` line (convo_user_line), mapped to the names the speech backend accepts
   (`accepted`, from speech_providers.accepted_languages: audio.cpp's Qwen3 takes ten names plus "Auto"; a backend without a
   language field gets none). English names, native names, ISO codes and regional tags map as in the standalone fork
   (`QWEN3_LANGUAGE_ALIASES` in its qwen3_tts_handler.py). A declared language the backend does not take makes the whole reply
   "Auto", never an older language.
2. No header: a guess from the reply's whole first phrase together with the user's own words: a non-Latin script that holds a clear
   share of the letters (SCRIPT_SHARE; one Cyrillic letter or kana never decides), or a common-word vote that needs a clear
   majority. Unclear: "Auto".
3. A script never overrides the declaration for the reply: a phrase (the first included) almost wholly in another script is voiced
   in that script's language, and the reply's other phrases keep the declared one.
The previous reply's language is never carried over. Within a reply, a sentence wholly in another script (a quoted Japanese phrase)
is voiced in its language; a single foreign word never is. In a reply whose language has its own script, a phrase with none of it
that clearly reads as a Latin-script language (the common-word vote) is voiced in that language.
"""
from __future__ import annotations

import re

SUPPORTED = ('Chinese', 'English', 'Japanese', 'Korean', 'German', 'French', 'Russian', 'Portuguese', 'Spanish', 'Italian')
AUTO = 'Auto'
QWEN3 = SUPPORTED + (AUTO,)

# Declared names, native names, ISO 639 codes and regional tags -> the English name (lower case keys).
ALIASES = {
    'zh': 'Chinese', 'zh-cn': 'Chinese', 'zh-hans': 'Chinese', 'zh-hant': 'Chinese', 'zh-tw': 'Chinese', 'cmn': 'Chinese',
    'chinese': 'Chinese', 'mandarin': 'Chinese', 'mandarin chinese': 'Chinese', 'simplified chinese': 'Chinese',
    'traditional chinese': 'Chinese', '中文': 'Chinese', '汉语': 'Chinese', '漢語': 'Chinese', '普通话': 'Chinese',
    'en': 'English', 'en-us': 'English', 'en-gb': 'English', 'en-au': 'English', 'eng': 'English', 'english': 'English',
    'ja': 'Japanese', 'jp': 'Japanese', 'jpn': 'Japanese', 'japanese': 'Japanese', '日本語': 'Japanese',
    'ko': 'Korean', 'kr': 'Korean', 'kor': 'Korean', 'korean': 'Korean', '한국어': 'Korean',
    'de': 'German', 'de-de': 'German', 'deu': 'German', 'ger': 'German', 'german': 'German', 'deutsch': 'German',
    'fr': 'French', 'fr-fr': 'French', 'fr-ca': 'French', 'fra': 'French', 'fre': 'French', 'french': 'French', 'français': 'French',
    'francais': 'French',
    'ru': 'Russian', 'ru-ru': 'Russian', 'rus': 'Russian', 'russian': 'Russian', 'русский': 'Russian',
    'pt': 'Portuguese', 'pt-br': 'Portuguese', 'pt-pt': 'Portuguese', 'por': 'Portuguese', 'portuguese': 'Portuguese',
    'português': 'Portuguese', 'portugues': 'Portuguese', 'brazilian portuguese': 'Portuguese',
    'es': 'Spanish', 'es-es': 'Spanish', 'es-mx': 'Spanish', 'es-419': 'Spanish', 'spa': 'Spanish', 'spanish': 'Spanish',
    'español': 'Spanish', 'espanol': 'Spanish', 'castellano': 'Spanish', 'castilian': 'Spanish',
    'it': 'Italian', 'it-it': 'Italian', 'ita': 'Italian', 'italian': 'Italian', 'italiano': 'Italian',
}

STOPWORDS = {
    'English': set('the and is are was you your to of it that in for with this what be have on can will not there here my me we '
                   'at do as but they he she his her our their would could should about from sir yes'.split()),
    'Spanish': set('el los las del que y en es una por para con no se lo al como más pero su está son sí muy también hay usted '
                   'esto esta este'.split()),
    'French': set('le les des et est une que qui pour dans pas vous je il elle ce sur avec au du mais ne nous sont très oui '
                  'cette cet'.split()),
    'German': set('der die das und ist nicht ein eine zu den ich sie es mit auf für von dem auch sich wir sind aber wie im noch '
                  'ja kein'.split()),
    'Portuguese': set('os as que um uma para com não em do da no na por mais mas você são também muito está isso este'.split()),
    'Italian': set('il lo gli di che per con non del della sono ma anche più questo mi ti ci come molto sì questa'.split()),
}
# A word shared by two languages' lists ("que", "no", "con") counts for neither: it cannot tell them apart.
_SHARED = {word for language, words in STOPWORDS.items() for other, more in STOPWORDS.items() if other != language for word in words & more}

# Scripts that name one language Qwen3 speaks. Kana decides Japanese even beside kanji; Han alone is Chinese.
_SCRIPTS = (('Russian', lambda c: 'Ѐ' <= c <= 'ԯ'), ('Korean', lambda c: '가' <= c <= '힯' or 'ᄀ' <= c <= 'ᇿ'),
            ('Japanese', lambda c: '぀' <= c <= 'ヿ'), ('Chinese', lambda c: '一' <= c <= '鿿'))
# Japanese is often written in kanji alone: Han script never contradicts a Japanese reply.
COMPATIBLE = {('Japanese', 'Chinese')}
CONTRADICTION = 0.8     # share of a phrase's letters in another script before it contradicts a declared language
SCRIPT_SHARE = 0.5      # share of the letters (and at least SCRIPT_LETTERS of them) a script needs to name the language of unlabelled text
SCRIPT_LETTERS = 2
_LETTER = re.compile(r'\w', re.U)


def canonical(declared):
    """The English name of a declared language ('es', 'Español', 'pt-BR' -> 'Spanish', 'Spanish', 'Portuguese'), or '' when it is
    not one this module knows (the declaration is kept as written by the caller for the developer view)."""
    text = ' '.join(str(declared or '').split()).strip().strip('*_`"\'.').strip().lower().replace('_', '-')
    if not text:
        return ''
    if text in ALIASES:
        return ALIASES[text]
    head = re.split(r'[\s(,/]', text, 1)[0]
    return ALIASES.get(head, '')


def mapped(declared, accepted):
    """The name the speech backend takes for a declared language: the backend's own spelling when it accepts it, else "Auto" when
    the backend has it, else ''. `accepted` None: the backend has no language field, so nothing is sent (None)."""
    if accepted is None:
        return None
    name = canonical(declared)
    for item in accepted:
        if name and item.lower() == name.lower():
            return item
    return AUTO if AUTO in accepted else ''


def scripts(text):
    """{language: share of the text's letters} for the scripts above (Japanese includes its kanji when any kana is present)."""
    letters = _LETTER.findall(str(text or ''))
    letters = [c for c in letters if not c.isdigit() and c != '_']
    if not letters:
        return {}
    counts = {}
    for language, test in _SCRIPTS:
        counts[language] = sum(1 for c in letters if test(c))
    if counts.get('Japanese'):
        counts['Japanese'] += counts.pop('Chinese', 0)
    return {language: count / len(letters) for language, count in counts.items() if count}


def script_language(text, share=CONTRADICTION):
    """The language a phrase's script names when (nearly) all its letters are in it, else ''."""
    found = scripts(text)
    best = max(found.items(), key=lambda item: item[1], default=('', 0))
    return best[0] if best[1] >= share else ''


def detect(text):
    """A language name Qwen3 accepts, or '' when the text does not say clearly: a non-Latin script, or a common-word vote with a
    clear majority (at least two distinguishing words, at least twice the next language's and most of all the words counted)."""
    text = str(text or '')
    found = scripts(text)
    if found:
        language, share = max(found.items(), key=lambda item: item[1])
        tests = dict(_SCRIPTS)
        letters = [c for c in _LETTER.findall(text) if not c.isdigit() and c != '_']
        own = [c for c in letters if tests[language](c)]   # for Japanese: its kana only, not the kanji beside them
        if language == 'Japanese' and len(own) < SCRIPT_LETTERS:
            language = 'Chinese'                            # one kana among Han is still Chinese text
            own = [c for c in letters if tests['Chinese'](c)]
        if share >= SCRIPT_SHARE and len(own) >= SCRIPT_LETTERS:
            return language
        if share >= SCRIPT_SHARE:
            return ''                                       # too little text to tell
    words = re.findall(r"[a-zà-öø-ÿ']+", text.lower())
    if len(words) < 3:
        return ''
    scores = sorted(((sum(word in vocabulary and word not in _SHARED for word in words), language)
                     for language, vocabulary in STOPWORDS.items()), reverse=True)
    (best, language), (second, _) = scores[0], scores[1]
    total = sum(score for score, _ in scores)
    return language if best >= 2 and best >= 2 * second and best * 2 > total else ''


def reply_language(declared, phrase, user_words, accepted):
    """The one language a reply is spoken in (see the module rule); returns (language, why). `language` is the backend's own name,
    "Auto", '' (backend without "Auto" and nothing known) or None (backend without a language field). `why` is for the developer
    view: 'declared', 'unmapped', 'script', 'guess' or 'unclear'."""
    if accepted is None:
        return None, 'no language field'
    name = canonical(declared) if declared else ''
    script = script_language(phrase)
    if declared:
        # A first phrase wholly in another script (a quoted line) never changes the reply's language: phrase_language voices that
        # phrase in its script and every other phrase keeps the declared language.
        language = mapped(declared, accepted)
        return language, 'declared' if name and language and language != AUTO else 'unmapped'
    # A phrase wholly in one script names it only with enough of its letters (detect: never one Cyrillic letter or one kana).
    guess = (detect(phrase) if script else '') or detect(' '.join(part for part in (str(phrase or ''), str(user_words or '')) if part))
    if guess:
        return mapped(guess, accepted), 'guess'
    return (AUTO if AUTO in accepted else ''), 'unclear'


def phrase_language(reply, phrase, accepted):
    """The language one phrase of a reply is voiced in: the reply's, unless the phrase is wholly in another script that names a
    language the backend takes (a quoted Japanese sentence in an English reply). A single foreign word never changes it."""
    if accepted is None or not reply:
        return reply
    script = script_language(phrase, share=0.9)
    if not script and canonical(reply) in dict(_SCRIPTS) and not scripts(phrase):
        # A reply in a language with its own script, and a phrase with none of it: voiced in the Latin-script language it clearly
        # reads as (the common-word vote), else the reply's.
        latin = detect(phrase)
        language = mapped(latin, accepted) if latin else ''
        return language if language and language != AUTO else reply
    if not script or len(_LETTER.findall(str(phrase or ''))) < 2:
        return reply
    if (canonical(reply), script) in COMPATIBLE:
        return reply
    language = mapped(script, accepted)
    return language if language and language != AUTO and language.lower() != str(reply).lower() else reply
