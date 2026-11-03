"""Report / Explanation agent. The model may only restate the facts object; its output is checked in code."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from portfoliolens.agents.llm import LLMClient
from portfoliolens.agents.prompts import QA_SYSTEM, REPORT_SYSTEM, facts_user
from portfoliolens.agents.scope import DECLINE_TEXT, is_out_of_scope
from portfoliolens.domain import (
    Answer,
    ConfirmedMandate,
    EngineResult,
    Finding,
    Metric,
    Report,
    Run,
    RunStatus,
    Status,
    VersionPin,
)
from portfoliolens.engine import plain, q2, with_review_items
from portfoliolens.errors import LLMError

DISCLAIMER = (
    "Compliance decision-support only: not assurance, legal advice or investment advice. "
    "A qualified reviewer is accountable for the sign-off."
)
MAX_SECTIONS = 6
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_ISIN_TOKEN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")
_CLAIMS_COMPLIANCE = re.compile(r"(?<!non-)(?<!not )\bcompliant\b", re.IGNORECASE)
_ROUNDINGS = (Decimal(1), Decimal("0.1"), Decimal("0.01"))
_QUESTION_HINTS: tuple[tuple[tuple[str, ...], Metric], ...] = (
    (("large",), Metric.LARGE_CAP_PCT),
    (("mid",), Metric.MID_CAP_PCT),
    (("small",), Metric.SMALL_CAP_PCT),
    (("issuer", "concentration", "single", "largest", "biggest"), Metric.SINGLE_ISSUER_PCT),
    (("sector", "theme"), Metric.SECTOR_PCT),
    (("how many", "count", "number of"), Metric.STOCK_COUNT),
    (("equity",), Metric.EQUITY_PCT),
)


def facts_payload(result: EngineResult) -> dict[str, Any]:
    return {
        "run_status": result.status.value,
        "facts": [
            {"id": f.fact_id, "label": f.label, "value": str(q2(f.value)), "unit": f.unit, "detail": f.detail}
            for f in result.facts
        ],
        "findings": [
            {
                "id": f.finding_id,
                "constraint_id": f.constraint.constraint_id,
                "source": f.constraint.source_kind,
                "rule": f.constraint.source_ref,
                "metric": f.constraint.metric.value,
                "comparator": f.constraint.comparator.value,
                "threshold": plain(f.constraint.threshold),
                "value_low": str(q2(f.value_low)),
                "value_high": str(q2(f.value_high)),
                "status": f.status.value,
                "margin": str(q2(f.margin)),
            }
            for f in result.findings
        ],
        "review_items": [{"id": r.review_id, "kind": r.kind, "detail": r.detail} for r in result.review_items],
    }


class GroundingContext:
    """What a model-written sentence is allowed to mention: known ids and numbers present in the facts."""

    def __init__(self, result: EngineResult, extra_texts: Sequence[str]) -> None:
        self.result = result
        self.ids = (
            {f.fact_id for f in result.facts}
            | {f.finding_id for f in result.findings}
            | {r.review_id for r in result.review_items}
        )
        self._strip_tokens = sorted(
            self.ids
            | {f.constraint.constraint_id for f in result.findings}
            | {f.constraint.source_ref for f in result.findings},
            key=len,
            reverse=True,
        )
        numbers: set[Decimal] = {Decimal(n) for n in range(11)}
        values = [f.value for f in result.facts]
        for finding in result.findings:
            values.extend([finding.constraint.threshold, finding.value_low, finding.value_high, finding.margin])
        for value in values:
            magnitude = abs(value)
            numbers.add(magnitude)
            numbers.update(magnitude.quantize(step, rounding=ROUND_HALF_UP) for step in _ROUNDINGS)
        for text in [*extra_texts, *(r.detail for r in result.review_items)]:
            numbers.update(Decimal(token.replace(",", "")) for token in _NUMBER.findall(text))
        self._numbers = numbers

    def ungrounded_numbers(self, text: str) -> list[str]:
        cleaned = text
        for token in self._strip_tokens:
            cleaned = cleaned.replace(token, " ")
        cleaned = _ISIN_TOKEN.sub(" ", cleaned)
        return [token for token in _NUMBER.findall(cleaned) if Decimal(token.replace(",", "")) not in self._numbers]

    def ground(self, text: object, cited_ids: object, *, require_citation: bool) -> tuple[str, list[str]]:
        """Return (text, cited_ids) if the untrusted model output is grounded; raise UngroundedOutput if not."""
        if not isinstance(text, str) or not text.strip():
            raise UngroundedOutput("empty text")
        if not isinstance(cited_ids, list) or not all(isinstance(i, str) for i in cited_ids):
            raise UngroundedOutput("cited_ids is not a list of ids")
        if require_citation and not cited_ids:
            raise UngroundedOutput("a section cites nothing")
        unknown = [i for i in cited_ids if i not in self.ids]
        if unknown:
            raise UngroundedOutput(f"cites unknown ids {unknown[:3]}")
        ungrounded = self.ungrounded_numbers(text)
        if ungrounded:
            raise UngroundedOutput(f"states numbers not present in the facts {ungrounded[:3]}")
        if is_out_of_scope(text):
            raise UngroundedOutput("contains forecast or advice language")
        if self.result.status is not RunStatus.COMPLIANT and _CLAIMS_COMPLIANCE.search(text):
            raise UngroundedOutput("describes the scheme as compliant although the run is not")
        return text.strip(), list(cited_ids)


class UngroundedOutput(Exception):
    """Model output that cannot be traced to the facts object."""


class ReportWriter:
    def __init__(self, client: LLMClient | None) -> None:
        self._client = client

    def write(
        self,
        *,
        run_id: str,
        as_of: date,
        mandate: ConfirmedMandate,
        pins: Sequence[VersionPin],
        result: EngineResult,
    ) -> tuple[Report, EngineResult]:
        """Return the report and the result, which gains a review item if the model narrative was rejected."""

        def render(current: EngineResult, narrative: list[tuple[str, list[str]]] | None, note: str | None) -> str:
            return render_report(run_id, as_of, mandate, pins, current, narrative, note)

        if self._client is None:
            return Report(markdown=render(result, None, None), narrative_source="template"), result

        try:
            raw = self._client.complete_json(REPORT_SYSTEM, facts_user(facts_payload(result)))
        except LLMError as exc:
            note = f"Model narrative unavailable ({exc}); the template report stands alone."
            return Report(markdown=render(result, None, note), narrative_source="template", llm_note=note), result

        grounding = GroundingContext(result, _context_texts(as_of, pins, mandate))
        sections, problem = _validated_sections(raw, grounding)
        if problem is not None:
            note = f"Model narrative rejected: {problem}."
            result = with_review_items(result, [("report_rejected", note, None)])
            report = Report(
                markdown=render(result, None, note),
                narrative_source="template",
                llm_note=note,
                llm_output_rejected=True,
            )
            return report, result
        return Report(markdown=render(result, sections, None), narrative_source="llm"), result

    def answer(self, run: Run, question: str) -> Answer:
        if is_out_of_scope(question):
            return Answer(
                run_id=run.run_id,
                question=question,
                declined=True,
                text=DECLINE_TEXT,
                cited_ids=[],
                source="scope_guard",
            )
        if self._client is None:
            return _template_answer(run, question, note=None)
        try:
            raw = self._client.complete_json(QA_SYSTEM, facts_user(facts_payload(run.result), question))
        except LLMError as exc:
            return _template_answer(run, question, note=f"model unavailable ({exc})")
        grounding = GroundingContext(run.result, _context_texts(run.as_of, run.pins, run.mandate))
        try:
            text, cited = grounding.ground(raw.get("answer"), raw.get("cited_ids", []), require_citation=False)
        except UngroundedOutput as exc:
            return _template_answer(run, question, note=f"model answer rejected: {exc}")
        return Answer(run_id=run.run_id, question=question, declined=False, text=text, cited_ids=cited, source="llm")


def _validated_sections(
    raw: dict[str, Any], grounding: GroundingContext
) -> tuple[list[tuple[str, list[str]]] | None, str | None]:
    sections = raw.get("sections")
    if not isinstance(sections, list) or not sections:
        return None, "no sections returned"
    parsed: list[tuple[str, list[str]]] = []
    for section in sections[:MAX_SECTIONS]:
        if not isinstance(section, dict):
            return None, "a section is not an object"
        try:
            parsed.append(grounding.ground(section.get("text"), section.get("cited_ids"), require_citation=True))
        except UngroundedOutput as exc:
            return None, str(exc)
    return parsed, None


def _context_texts(as_of: date, pins: Sequence[VersionPin], mandate: ConfirmedMandate) -> list[str]:
    return [
        as_of.isoformat(),
        *(pin.version for pin in pins),
        mandate.mandate.scheme_name,
        mandate.mandate.sector or "",
    ]


def _template_answer(run: Run, question: str, note: str | None) -> Answer:
    lowered = question.lower()
    wanted = {metric for hints, metric in _QUESTION_HINTS if any(hint in lowered for hint in hints)}
    findings: list[Finding] = [f for f in run.result.findings if f.constraint.metric in wanted]
    if not findings:
        findings = [f for f in run.result.findings if f.status is not Status.PASS] or list(run.result.findings)
    prefix = "From the run's facts (no model used)" + (f"; {note}" if note else "") + ":"
    text = "\n".join([prefix, *(f"- {f.summary}" for f in findings)])
    return Answer(
        run_id=run.run_id,
        question=question,
        declined=False,
        text=text,
        cited_ids=[f.finding_id for f in findings],
        source="template",
    )


def render_report(
    run_id: str,
    as_of: date,
    mandate: ConfirmedMandate,
    pins: Sequence[VersionPin],
    result: EngineResult,
    narrative: Sequence[tuple[str, Sequence[str]]] | None,
    llm_note: str | None,
) -> str:
    m = mandate.mandate
    pin_text = " · ".join(f"{p.kind}: `{p.version}`" for p in pins)
    lines = [
        f"# Compliance exception report: {m.scheme_name}",
        "",
        f"- **Run:** `{run_id}`",
        f"- **As-of date:** {as_of.isoformat()}",
        f"- **Status:** **{result.status.value}**",
        f"- **Category:** {m.category.value}" + (f" (sector: {m.sector})" if m.sector else ""),
        f"- **Mandate confirmed by:** {mandate.confirmed_by}"
        + (" (corrected at Gate 1)" if mandate.corrected_by_reviewer else ""),
        f"- **Pinned versions:** {pin_text}",
        "",
        "## Findings",
        "",
        "| Status | Constraint | Source | Threshold | Value | Margin | Binding |",
        "|---|---|---|---|---|---|---|",
    ]
    for f in result.findings:
        unit = "%" if f.constraint.metric.is_percentage else ""
        value = (
            f"{q2(f.value_low)}{unit}"
            if f.value_low == f.value_high
            else f"{q2(f.value_low)} to {q2(f.value_high)}{unit}"
        )
        bound = "≥" if f.constraint.comparator.value == "min" else "≤"
        lines.append(
            f"| {f.status.value} | `{f.constraint.constraint_id}` | {_cell(f.constraint.source_ref)} | "
            f"{bound} {plain(f.constraint.threshold)}{unit} | {value} | {q2(f.margin):+} | "
            f"{'yes' if f.binding else 'no'} |"
        )

    lines += ["", "## Facts", "", "| Fact | Value | Detail |", "|---|---|---|"]
    for fact in result.facts:
        suffix = {"pct": "%", "inr": " (INR)", "count": ""}[fact.unit]
        value = plain(fact.value) if fact.unit == "count" else f"{q2(fact.value)}{suffix}"
        lines.append(f"| `{fact.fact_id}` {_cell(fact.label)} | {value} | {_cell(fact.detail)} |")

    lines += ["", "## Items for human review (Gate 2)", ""]
    if result.review_items:
        lines += [f"- **{r.review_id}** `{r.kind}`: {r.detail}" for r in result.review_items]
    else:
        lines.append("- None.")

    lines += ["", "## Narrative", ""]
    if narrative:
        lines.append("_Model-written, citation-checked against the facts above._")
        lines.append("")
        lines += [f"- {text} [{', '.join(cited)}]" for text, cited in narrative]
    else:
        lines.append("_Template report: every statement above is computed by the deterministic engine._")
    if llm_note:
        lines += ["", f"> {llm_note}"]

    lines += ["", "---", "", f"_{DISCLAIMER}_", ""]
    return "\n".join(lines)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")
