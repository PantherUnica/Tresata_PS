from portfoliolens.citations import locate_quote, sentence_span

SOURCE = "Type of scheme: equity.\nThe Scheme will invest\n  at least 85% in large–cap companies. Other text."


def test_quote_is_found_despite_case_whitespace_and_typography() -> None:
    citation = locate_quote(SOURCE, "the scheme will invest at least 85% in large-cap companies")
    assert citation is not None
    assert SOURCE[citation.start : citation.end] == citation.quote
    assert citation.quote.startswith("The Scheme will invest")


def test_a_paraphrase_is_not_a_citation() -> None:
    assert locate_quote(SOURCE, "invests a minimum of 85% in large caps") is None


def test_short_or_missing_quotes_are_rejected() -> None:
    assert locate_quote(SOURCE, "85%") is None
    assert locate_quote(SOURCE, None) is None


def test_sentence_span_does_not_split_on_decimal_points() -> None:
    text = "Intro line.\nCap any company at 8.5% of NAV. Next sentence."
    start = text.index("8.5")
    begin, end = sentence_span(text, start, start + 3)
    assert text[begin:end] == "Cap any company at 8.5% of NAV."
