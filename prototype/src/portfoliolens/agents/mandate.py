"""Mandate Extraction agent: proposes a mandate from SID prose. Every field must cite a verbatim quote."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from pydantic import ValidationError

from portfoliolens.agents.llm import LLMClient
from portfoliolens.agents.prompts import MANDATE_SYSTEM, mandate_user
from portfoliolens.citations import citation_for_span, locate_quote
from portfoliolens.domain import Citation, Comparator, MandateProposal, Metric, SchemeCategory, SelfImposedLimit
from portfoliolens.errors import LLMError
from portfoliolens.ingest import SidDocument


class MandateExtractor(Protocol):
    @property
    def name(self) -> str: ...

    def extract(self, sid: SidDocument, proposal_id: str) -> MandateProposal: ...


# --- offline stand-in -------------------------------------------------------------------
# SEBI prescribes the "type of scheme" wording for each category, so a pattern match is a strong
# baseline for the category. Free-text limits are where it falls short, which is the model's job.

_SCHEME_NAME = re.compile(
    r"^[ \t>*#-]*(?:\*\*)?(?:name of the scheme|scheme name)(?:\*\*)?\s*:\s*(?P<value>.+?)\s*$", re.I | re.M
)
_BENCHMARK = re.compile(r"^[ \t>*#-]*(?:\*\*)?benchmark(?: index)?(?:\*\*)?\s*:\s*(?P<value>.+?)\s*$", re.I | re.M)
_CATEGORY_PATTERNS: tuple[tuple[SchemeCategory, re.Pattern[str]], ...] = tuple(
    (category, re.compile(pattern, re.I))
    for category, pattern in (
        (SchemeCategory.ELSS, r"equity linked savings? scheme"),
        (
            SchemeCategory.SECTORAL_THEMATIC,
            r"equity scheme investing in (?:the )?(?P<sector>[a-z][a-z &]*?) (?:sector|theme)",
        ),
        (SchemeCategory.FOCUSED, r"maximum (?:of )?30 stocks"),
        (SchemeCategory.FLEXI_CAP, r"dynamic equity scheme investing across large cap,? mid cap,? (?:and )?small cap"),
        (SchemeCategory.MULTI_CAP, r"equity scheme investing across large cap,? mid cap,? (?:and )?small cap"),
        (SchemeCategory.LARGE_MID_CAP, r"investing in both large cap and mid cap"),
        (SchemeCategory.LARGE_CAP, r"predominantly investing in large cap"),
        (SchemeCategory.MID_CAP, r"predominantly investing in mid cap"),
        (SchemeCategory.SMALL_CAP, r"predominantly investing in small cap"),
        (SchemeCategory.DIVIDEND_YIELD, r"predominantly investing in dividend yielding"),
        (SchemeCategory.VALUE, r"following (?:a )?value investment strategy"),
        (SchemeCategory.CONTRA, r"following (?:a )?contrarian investment strategy"),
    )
)
_NUMBER_WORD = (
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred"
)
_NUMBER_VALUE = (
    rf"(?:\d{{1,3}}(?:\.\d+)?|(?:{_NUMBER_WORD})(?:[- ](?:{_NUMBER_WORD}))*(?:\s+and\s+a\s+(?:half|quarter))?)"
)
_LIMIT = re.compile(
    r"\b(?P<phrase>not less than|no less than|at least|a minimum of|minimum of|not more than|no more than|"
    r"a maximum of|maximum of|shall not exceed|will not exceed|not exceeding|to exceed)\s+"
    rf"(?P<number>{_NUMBER_VALUE})\s*(?:%|per ?cent\b|percent\b)",
    re.I,
)
_MIN_PHRASES = frozenset({"not less than", "no less than", "at least", "a minimum of", "minimum of"})
_METRIC_HINTS: tuple[tuple[Metric, re.Pattern[str]], ...] = tuple(
    (metric, re.compile(pattern, re.I))
    for metric, pattern in (
        (Metric.SINGLE_ISSUER_PCT, r"\b(?:any (?:one |single )?|a single |single )(?:company|issuer|stock)\b"),
        (Metric.LARGE_CAP_PCT, r"\blarge[- ]?cap"),
        (Metric.MID_CAP_PCT, r"\bmid[- ]?cap"),
        (Metric.SMALL_CAP_PCT, r"\bsmall[- ]?cap"),
        (Metric.EQUITY_PCT, r"\bequit(?:y|ies)\b"),
    )
)


class HeuristicMandateExtractor:
    name = "offline-heuristic"

    def extract(self, sid: SidDocument, proposal_id: str) -> MandateProposal:
        text = sid.text
        issues: list[str] = []

        scheme_name = _line_value(_SCHEME_NAME, text)
        if scheme_name is None:
            issues.append("Scheme name not found; enter it at Gate 1.")

        category: SchemeCategory | None = None
        category_citation: Citation | None = None
        sector: str | None = None
        for candidate, pattern in _CATEGORY_PATTERNS:
            match = pattern.search(text)
            if match:
                category = candidate
                category_citation = citation_for_span(text, match.start(), match.end())
                sector = (match.groupdict().get("sector") or "").strip() or None
                break
        if category is None:
            issues.append("Scheme category not recognised from the SEBI 'type of scheme' wording.")

        return MandateProposal(
            proposal_id=proposal_id,
            sid_sha256=sid.sha256,
            extractor=self.name,
            scheme_name=scheme_name,
            category=category,
            category_citation=category_citation,
            benchmark=_line_value(_BENCHMARK, text),
            sector=sector,
            sector_citation=category_citation if sector else None,
            self_imposed_limits=_find_limits(text),
            issues=issues,
        )


def _line_value(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group("value").strip(" *") if match else None


def _find_limits(text: str) -> list[SelfImposedLimit]:
    limits: list[SelfImposedLimit] = []
    seen: set[tuple[Metric, Comparator, Decimal]] = set()
    for match in _LIMIT.finditer(text):
        citation = citation_for_span(text, match.start(), match.end())
        metric = next((m for m, hint in _METRIC_HINTS if hint.search(citation.quote)), None)
        if metric is None:
            continue
        comparator = Comparator.MIN if match.group("phrase").lower() in _MIN_PHRASES else Comparator.MAX
        threshold = _parse_number(match.group("number"))
        key = (metric, comparator, threshold)
        if key in seen:
            continue
        seen.add(key)
        limits.append(SelfImposedLimit(metric=metric, comparator=comparator, threshold=threshold, citation=citation))
    return limits


def _parse_number(raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation:
        pass

    words = raw.lower().replace("-", " ").split()
    fraction = Decimal(0)
    if len(words) >= 3 and words[-3:-1] == ["and", "a"] and words[-1] in {"half", "quarter"}:
        fraction = Decimal("0.5") if words[-1] == "half" else Decimal("0.25")
        words = words[:-3]

    small_numbers = {
        "zero": 0,
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "fifty": 50,
        "sixty": 60,
        "seventy": 70,
        "eighty": 80,
        "ninety": 90,
    }
    value = 0
    for word in words:
        if word == "and":
            continue
        if word == "hundred":
            value = (value or 1) * 100
        elif word in small_numbers:
            value += small_numbers[word]
        else:
            raise ValueError(f"Unsupported number wording: {raw}")
    return Decimal(value) + fraction


# --- model-backed agent -----------------------------------------------------------------


class LLMMandateExtractor:
    def __init__(self, client: LLMClient, fallback: HeuristicMandateExtractor) -> None:
        self._client = client
        self._fallback = fallback

    @property
    def name(self) -> str:
        return f"llm:{self._client.name}"

    def extract(self, sid: SidDocument, proposal_id: str) -> MandateProposal:
        try:
            raw = self._client.complete_json(MANDATE_SYSTEM, mandate_user(sid.text))
        except LLMError as exc:
            proposal = self._fallback.extract(sid, proposal_id)
            return proposal.model_copy(
                update={
                    "extractor": f"{self._fallback.name} (fallback)",
                    "issues": [f"Model extraction failed ({exc}); the offline heuristic was used.", *proposal.issues],
                }
            )
        return parse_model_mandate(raw, sid, proposal_id, self.name)


def parse_model_mandate(raw: dict[str, Any], sid: SidDocument, proposal_id: str, extractor: str) -> MandateProposal:
    """Turn untrusted model JSON into a proposal. Anything uncited or malformed is dropped and flagged."""
    issues: list[str] = []

    def cited(key: str) -> tuple[str | None, Citation | None]:
        item = raw.get(key)
        if not isinstance(item, dict) or item.get("value") in (None, ""):
            return None, None
        value = str(item["value"]).strip()
        quote = item.get("quote")
        citation = locate_quote(sid.text, quote) if isinstance(quote, str) else None
        if citation is None:
            issues.append(f"{key}: model value '{value}' has no verbatim quote in the SID, so it was discarded.")
            return None, None
        return value, citation

    scheme_name, _ = cited("scheme_name")
    raw_category, category_citation = cited("category")
    category: SchemeCategory | None = None
    if raw_category is not None:
        try:
            category = SchemeCategory(raw_category.lower())
        except ValueError:
            issues.append(f"category: '{raw_category}' is not a recognised SEBI category, so it was discarded.")
            category_citation = None
    benchmark, _ = cited("benchmark")
    sector, sector_citation = cited("sector")

    limits: list[SelfImposedLimit] = []
    raw_limits = raw.get("self_imposed_limits")
    for item in raw_limits if isinstance(raw_limits, list) else []:
        limit = _parse_model_limit(item, sid, issues)
        if limit is not None:
            limits.append(limit)

    if scheme_name is None:
        issues.append("Scheme name not identified; enter it at Gate 1.")
    if category is None:
        issues.append("Scheme category not identified; choose it at Gate 1.")
    if category is SchemeCategory.SECTORAL_THEMATIC and not sector:
        issues.append("Sectoral/thematic scheme but no sector identified.")

    return MandateProposal(
        proposal_id=proposal_id,
        sid_sha256=sid.sha256,
        extractor=extractor,
        scheme_name=scheme_name,
        category=category,
        category_citation=category_citation,
        benchmark=benchmark,
        sector=sector,
        sector_citation=sector_citation,
        self_imposed_limits=limits,
        issues=issues,
    )


def _parse_model_limit(item: Any, sid: SidDocument, issues: list[str]) -> SelfImposedLimit | None:
    if not isinstance(item, dict):
        issues.append("A malformed self-imposed limit was discarded.")
        return None
    try:
        metric = Metric(str(item["metric"]))
        comparator = Comparator(str(item["comparator"]).lower())
        threshold = Decimal(str(item["threshold"]))
    except (KeyError, ValueError, InvalidOperation):
        issues.append(f"An unparseable self-imposed limit was discarded: {str(item)[:120]}")
        return None
    citation = locate_quote(sid.text, item.get("quote") if isinstance(item.get("quote"), str) else None)
    if citation is None:
        issues.append(
            f"{metric.value} {comparator.value} {threshold}: no verbatim quote in the SID, so it was discarded."
        )
        return None
    if not _number_written_in(threshold, citation.quote):
        issues.append(
            f"{metric.value} {comparator.value} {threshold}: the number is not written as digits in its quote "
            f'("{citation.quote}"); verify the conversion.'
        )
    try:
        return SelfImposedLimit(metric=metric, comparator=comparator, threshold=threshold, citation=citation)
    except ValidationError as exc:
        issues.append(f"{metric.value} {comparator.value} {threshold}: {exc.errors()[0]['msg']}; discarded.")
        return None


def _number_written_in(threshold: Decimal, quote: str) -> bool:
    if not threshold.is_finite():
        return False
    return any(Decimal(token) == threshold for token in re.findall(r"\d+(?:\.\d+)?", quote))
