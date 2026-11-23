// Plain spoken text from a reply that may carry Markdown: the browser's copy of helpers/convo_plaintext.py (`spoken`), used for the
// live draft so it reads as the history will once the reply is heard. Same patterns, same order; tests/fixtures/plain-text-cases.json
// keeps the two equal. Python's \w (Unicode) is the class body WORD here and a letter ([^\W\d_]) \p{L}, with the `u` flag.
const WORD = String.raw`\p{L}\p{N}\p{M}_`;
const pattern = (source, flags = 'gu') => new RegExp(source, flags);

const FENCE = pattern(String.raw`^[ \t]*(?:${'```'}|~~~)[^\n]*$`, 'gmu');
const TABLE_SEPARATOR = pattern(String.raw`^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$`, 'gmu');
const TABLE_ROW = pattern(String.raw`^([ \t]*)\|(.+)\|[ \t]*$`, 'gmu');
const RULE = pattern(String.raw`^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$`, 'gmu');
const HEADING = pattern(String.raw`^([ \t]*)#{1,6}[ \t]+`, 'gmu');
const QUOTE = pattern(String.raw`^([ \t]*)>[ \t]?`, 'gmu');
const BULLET = pattern(String.raw`^([ \t]*)[-*+•][ \t]+`, 'gmu');
const IMAGE = pattern(String.raw`!\[([^\]\n]*)\]\((?:[^()\s]|\([^()\s]*\))*\)`);
const LINK = pattern(String.raw`\[([^\]\n]+)\]\((?:[^()\s]|\([^()\s]*\))*\)`);
const CODE = pattern('`([^`\\n]+)`');
const BREAK = pattern(String.raw`<br\s*\/?>`, 'giu');
const BOLD = [pattern(String.raw`(?<![${WORD}*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![${WORD}*])`),
              pattern(String.raw`(?<![${WORD}])__(?=\S)(.+?)(?<=\S)__(?![${WORD}])`)];
const ITALIC = [pattern(String.raw`(?<![${WORD}*])\*(?=\S)(.+?)(?<=\S)\*(?!\*)`),
                pattern(String.raw`(?<![${WORD}])_(?=\S)(.+?)(?<=\S)_(?![${WORD}])`)];
const STRIKE = pattern(String.raw`~~(?=\S)(.+?)(?<=\S)~~`);
const LEFTOVER = [pattern('\\*{2,}|~~|`'),
                  pattern(String.raw`(?<![${WORD}])_{2,}|_{2,}(?![${WORD}])`),
                  pattern(String.raw`(?<=\p{L})\*|\*(?=\p{L})`),
                  pattern(String.raw`(?<![${WORD}])_(?=\p{L})|(?<=\p{L})_(?![${WORD}])`)];

const MID = '\u0000';     // put before a piece that continues a line (as the server's _MID): never markup, a word or a line start

// `lineStart`: whether `text` begins a line of the reply (the live draft always does; the server also cleans pieces cut mid-line).
export function plainText(text, { lineStart = true } = {}) {
    if (typeof text !== 'string' || !text) return typeof text === 'string' ? text : '';
    if (!lineStart) text = MID + text;
    text = clean(text);
    return !lineStart && text.startsWith(MID) ? text.slice(1) : text;
}

function clean(text) {
    text = text.replace(FENCE, '').replace(TABLE_SEPARATOR, '');
    text = text.replace(TABLE_ROW, (_, indent, inner) => indent + inner.split('|').map(cell => cell.trim()).filter(Boolean).join(', '));
    text = text.replace(RULE, '');
    for (const each of [HEADING, QUOTE, BULLET]) text = text.replace(each, '$1');
    text = text.replace(IMAGE, '$1').replace(LINK, '$1').replace(CODE, '$1').replace(BREAK, ' ');
    for (const each of [...BOLD, ...ITALIC]) text = text.replace(each, '$1');
    text = text.replace(STRIKE, '$1');
    for (const each of LEFTOVER) text = text.replace(each, '');
    return text;
}
