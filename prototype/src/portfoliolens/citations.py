"""Verbatim-quote location. A citation exists only if its text is really in the source document."""

from __future__ import annotations

import re

from portfoliolens.domain import Citation

MIN_QUOTE_CHARS = 8
_TYPOGRAPHY = str.maketrans(
    {
        "\N{LEFT SINGLE QUOTATION MARK}": "'",
        "\N{RIGHT SINGLE QUOTATION MARK}": "'",
        "\N{LEFT DOUBLE QUOTATION MARK}": '"',
        "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
        "\N{EN DASH}": "-",
        "\N{EM DASH}": "-",
        "\N{NO-BREAK SPACE}": " ",
    }
)
_SENTENCE_BOUNDARY = re.compile(r"[.!?](?=\s|$)|\n")
_EDGE_CHARS = " \t\r\n-*#>|"


def _normalise(text: str) -> tuple[str, list[int]]:
    """Lower-case, unify typography and collapse whitespace, keeping a map back to source offsets."""
    chars: list[str] = []
    offsets: list[int] = []
    previous_was_space = True
    for index, char in enumerate(text.translate(_TYPOGRAPHY)):
        if char.isspace():
            if previous_was_space:
                continue
            chars.append(" ")
            offsets.append(index)
            previous_was_space = True
            continue
        for lowered in char.lower():
            chars.append(lowered)
            offsets.append(index)
        previous_was_space = False
    return "".join(chars), offsets


def locate_quote(source: str, quote: str | None) -> Citation | None:
    if not quote:
        return None
    haystack, offsets = _normalise(source)
    needle = _normalise(quote)[0].strip()
    if len(needle) < MIN_QUOTE_CHARS:
        return None
    position = haystack.find(needle)
    if position < 0:
        return None
    start = offsets[position]
    end = offsets[position + len(needle) - 1] + 1
    return Citation(quote=source[start:end], start=start, end=end)


def sentence_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand [start, end) to the sentence or line that contains it."""
    sentence_start = 0
    for match in _SENTENCE_BOUNDARY.finditer(text, 0, start):
        sentence_start = match.end()
    following = _SENTENCE_BOUNDARY.search(text, end)
    sentence_end = following.end() if following else len(text)
    while sentence_start < sentence_end and text[sentence_start] in _EDGE_CHARS:
        sentence_start += 1
    while sentence_end > sentence_start and text[sentence_end - 1] in _EDGE_CHARS:
        sentence_end -= 1
    return sentence_start, sentence_end


def citation_for_span(text: str, start: int, end: int) -> Citation:
    sentence_start, sentence_end = sentence_span(text, start, end)
    return Citation(quote=text[sentence_start:sentence_end], start=sentence_start, end=sentence_end)
