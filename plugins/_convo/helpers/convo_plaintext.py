"""Plain spoken text from a reply that may carry Markdown (2026-11-17).

A model asked for a short spoken answer still writes Markdown at times: emphasis, bullets, headings, links, code, tables. None of it is
meant to be heard (a speech engine reads "**" or "#" aloud, or stumbles on it), and none of it belongs in the history the model reads
next or in the panel. `spoken` removes the markup and keeps the words. It works from the syntax's form, never from particular words.

The same function runs on every phrase before it is synthesised (so what is heard and what the history keeps are the same text) and
on the whole reply before it is settled (so heard and generated text compare like for like in the panel). Whitespace and every
character that is not markup are kept, so phrases cleaned one by one join up to the reply cleaned whole; a marker split between two
phrases is removed from each. Text without markup comes back unchanged. `webui/plain-text.js` mirrors this for the live draft, and
`tests/fixtures/plain-text-cases.json` keeps the two equal."""
from __future__ import annotations

import re

_FENCE = re.compile(r'^[ \t]*(?:```|~~~)[^\n]*$', re.M)                                   # a code fence line (the code itself stays)
_TABLE_SEPARATOR = re.compile(r'^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$', re.M)
_TABLE_ROW = re.compile(r'^([ \t]*)\|(.+)\|[ \t]*$', re.M)
_RULE = re.compile(r'^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$', re.M)                     # ---, ***, ___ (before bullets: "* * *")
_HEADING = re.compile(r'^([ \t]*)#{1,6}[ \t]+', re.M)
_QUOTE = re.compile(r'^([ \t]*)>[ \t]?', re.M)
_BULLET = re.compile(r'^([ \t]*)[-*+•][ \t]+', re.M)                                 # numbered items keep their numbers
_IMAGE = re.compile(r'!\[([^\]\n]*)\]\((?:[^()\s]|\([^()\s]*\))*\)')
_LINK = re.compile(r'\[([^\]\n]+)\]\((?:[^()\s]|\([^()\s]*\))*\)')
_CODE = re.compile(r'`([^`\n]+)`')
_BREAK = re.compile(r'<br\s*/?>', re.I)
_BOLD = (re.compile(r'(?<![\w*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])'), re.compile(r'(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)'))
_ITALIC = (re.compile(r'(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?!\*)'), re.compile(r'(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)'))
_STRIKE = re.compile(r'~~(?=\S)(.+?)(?<=\S)~~')
# What is left of a marker once pairs are gone (its partner was in another phrase, or it sat inside a word, as emphasis does in
# languages written without spaces): a run of stars, a strike or code marker, a run of underscores at a word's edge, a single star
# touching a letter, and a single underscore at a word's edge. "2 * 3", "2*3" and "my_name" stay.
_LEFTOVER = (re.compile(r'\*{2,}|~~|`'), re.compile(r'(?<!\w)_{2,}|_{2,}(?!\w)'),
             re.compile(r'(?<=[^\W\d_])\*|\*(?=[^\W\d_])'),
             re.compile(r'(?<!\w)_(?=[^\W\d_])|(?<=[^\W\d_])_(?!\w)'))


def _row(match):
    cells = [cell.strip() for cell in match.group(2).split('|')]
    return match.group(1) + ', '.join(cell for cell in cells if cell)


_MID = '\x00'      # put before a piece that continues a line: no rule reads it as markup, a word or a line start, and it is removed after


def spoken(text, line_start=True):
    """`text` without Markdown markup: the words as they should be heard and remembered. `line_start`: whether `text` begins a line
    of the reply (a piece cut after ". " continues its line, so a "- " or "#" there is not a bullet or a heading)."""
    if not isinstance(text, str) or not text:
        return '' if not isinstance(text, str) else text
    if not line_start:
        text = _MID + text
    text = _clean(text)
    return text[1:] if not line_start and text.startswith(_MID) else text


def _clean(text):
    text = _FENCE.sub('', text)
    text = _TABLE_SEPARATOR.sub('', text)
    text = _TABLE_ROW.sub(_row, text)
    text = _RULE.sub('', text)
    for pattern in (_HEADING, _QUOTE, _BULLET):
        text = pattern.sub(r'\1', text)
    text = _IMAGE.sub(r'\1', text)
    text = _LINK.sub(r'\1', text)
    text = _CODE.sub(r'\1', text)
    text = _BREAK.sub(' ', text)
    for pattern in _BOLD + _ITALIC:
        text = pattern.sub(r'\1', text)
    text = _STRIKE.sub(r'\1', text)
    for pattern in _LEFTOVER:
        text = pattern.sub('', text)
    return text
