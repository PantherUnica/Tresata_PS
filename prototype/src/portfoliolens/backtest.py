"""Backtest harness: runs every case through the real pipeline and reports, honestly, where it fails.

Each case runs twice:
- extracted:    the agent's mandate proposal is accepted unchanged at Gate 1 (agents + engine, end to end)
- ground_truth: the mandate a reviewer would confirm at Gate 1 (isolates the deterministic engine)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portfoliolens.citations import locate_quote
from portfoliolens.domain import (
    Comparator,
    ConfirmedMandate,
    Funding,
    Mandate,
    MandateProposal,
    Metric,
    RunStatus,
    SchemeCategory,
    SelfImposedLimit,
    Status,
)
from portfoliolens.engine import plain, q2
from portfoliolens.errors import InputValidationError, PortfolioLensError
from portfoliolens.pipeline import Coordinator

MandateSource = Literal["extracted", "ground_truth"]
SOURCES: tuple[MandateSource, ...] = ("extracted", "ground_truth")
AUTO_REVIEWER = "backtest-harness (auto-confirm)"
ENGINE_REVIEW_KINDS = frozenset({"unresolved_holding", "identity_by_name", "near_threshold", "indeterminate"})


# --- case specification -----------------------------------------------------------------


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedFinding(_Spec):
    status: Status
    margin: Decimal | None = None
    binding: bool | None = None


class GroundTruthLimit(_Spec):
    metric: Metric
    comparator: Comparator
    threshold: Decimal
    quote: str


class GroundTruthMandate(_Spec):
    scheme_name: str
    category: SchemeCategory
    sector: str | None = None
    benchmark: str | None = None
    self_imposed_limits: list[GroundTruthLimit] = Field(default_factory=list)


class Expectation(_Spec):
    run_status: RunStatus | None = None
    findings: dict[str, ExpectedFinding] = Field(default_factory=dict)
    review_kinds: list[str] | None = None
    validation_codes: list[str] | None = None
    declined: bool | None = None
    eligible: list[str] | None = None
    blocking: dict[str, list[str]] = Field(default_factory=dict)
    whatif_before: dict[str, ExpectedFinding] = Field(default_factory=dict)
    whatif_after: dict[str, ExpectedFinding] = Field(default_factory=dict)
    new_breaches: list[str] | None = None
    resolved_breaches: list[str] | None = None


class CaseSpec(_Spec):
    id: str
    title: str
    kind: Literal["compliance", "validation", "question", "screen", "whatif"]
    as_of: date
    synthetic: bool = True
    notes: str = ""
    holdings: str = "holdings.csv"
    sid: str = "sid.md"
    ground_truth_mandate: GroundTruthMandate | None = None
    question: str | None = None
    candidates: str | None = None
    weight_pct: Decimal | None = None
    funding: Funding = Funding.PRO_RATA
    sort_by: Literal["name", "headroom"] = "headroom"
    trades: str | None = None
    expect: Expectation


# --- outcomes ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    expected: str
    actual: str
    ok: bool


@dataclass
class CaseOutcome:
    case_id: str
    title: str
    kind: str
    source: MandateSource
    notes: str
    checks: list[Check] = field(default_factory=list)
    error: str | None = None
    run_id: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and bool(self.checks) and all(check.ok for check in self.checks)

    def expect_equal(self, name: str, expected: Any, actual: Any) -> None:
        self.checks.append(Check(name, _show(expected), _show(actual), expected == actual))


@dataclass
class BacktestReport:
    generated_at: datetime
    llm: str
    outcomes: list[CaseOutcome]

    def for_source(self, source: MandateSource) -> list[CaseOutcome]:
        return [o for o in self.outcomes if o.source == source]

    def passed(self, source: MandateSource) -> tuple[int, int]:
        outcomes = self.for_source(source)
        return sum(o.passed for o in outcomes), len(outcomes)

    def extraction_accuracy(self) -> tuple[int, int]:
        checks = [c for o in self.for_source("extracted") for c in o.checks if c.name.startswith("extract.")]
        return sum(c.ok for c in checks), len(checks)


# --- running ----------------------------------------------------------------------------


def load_cases(cases_dir: Path) -> list[tuple[CaseSpec, Path]]:
    cases = [
        (CaseSpec.model_validate_json(path.read_text(encoding="utf-8")), path.parent)
        for path in sorted(cases_dir.glob("*/case.json"))
    ]
    if not cases:
        raise FileNotFoundError(f"No */case.json files under {cases_dir}")
    return cases


def run_backtest(
    coordinator: Coordinator,
    cases_dir: Path,
    llm_label: str,
    sources: tuple[MandateSource, ...] = SOURCES,
) -> BacktestReport:
    outcomes = [
        run_case(coordinator, spec, case_dir, source) for spec, case_dir in load_cases(cases_dir) for source in sources
    ]
    return BacktestReport(generated_at=datetime.now(UTC), llm=llm_label, outcomes=outcomes)


def run_case(coordinator: Coordinator, spec: CaseSpec, case_dir: Path, source: MandateSource) -> CaseOutcome:
    outcome = CaseOutcome(spec.id, spec.title, spec.kind, source, spec.notes)
    expect = spec.expect
    try:
        holdings_csv = (case_dir / spec.holdings).read_text(encoding="utf-8")
        if spec.kind == "validation":
            _check_validation(coordinator, holdings_csv, expect, outcome)
            return outcome

        sid_text = (case_dir / spec.sid).read_text(encoding="utf-8")
        confirmed = _confirm(coordinator, spec, sid_text, source, outcome)
        run = coordinator.run_check(confirmed.mandate_id, holdings_csv, spec.as_of)
        outcome.run_id = run.run_id

        if expect.run_status is not None:
            outcome.expect_equal("run.status", expect.run_status, run.result.status)
        _check_findings(
            outcome,
            "finding",
            expect.findings,
            {f.constraint.constraint_id: (f.status, f.margin, f.binding) for f in run.result.findings},
        )
        if expect.review_kinds is not None:
            actual_kinds = {r.kind for r in run.result.review_items if r.kind in ENGINE_REVIEW_KINDS}
            outcome.expect_equal("review kinds", set(expect.review_kinds), actual_kinds)

        if spec.kind == "question":
            assert spec.question is not None
            answer = coordinator.explain(run.run_id, spec.question)
            outcome.expect_equal("question declined", expect.declined, answer.declined)
        elif spec.kind == "screen":
            assert spec.candidates is not None and spec.weight_pct is not None
            screening = coordinator.screen(
                run.run_id,
                (case_dir / spec.candidates).read_text(encoding="utf-8"),
                spec.weight_pct,
                spec.funding,
                spec.sort_by,
            )
            if expect.eligible is not None:
                outcome.expect_equal(
                    "eligible ISINs", set(expect.eligible), {c.isin for c in screening.candidates if c.eligible}
                )
            by_isin = {c.isin: c for c in screening.candidates}
            for isin, constraint_ids in expect.blocking.items():
                candidate = by_isin.get(isin)
                outcome.expect_equal(
                    f"blocking {isin}",
                    set(constraint_ids),
                    set(candidate.blocking_constraint_ids) if candidate else None,
                )
        elif spec.kind == "whatif":
            assert spec.trades is not None
            what_if = coordinator.what_if(run.run_id, (case_dir / spec.trades).read_text(encoding="utf-8"))
            _check_findings(
                outcome,
                "before",
                expect.whatif_before,
                {d.constraint_id: (d.before_status, d.before_margin, None) for d in what_if.deltas},
            )
            _check_findings(
                outcome,
                "after",
                expect.whatif_after,
                {d.constraint_id: (d.after_status, d.after_margin, None) for d in what_if.deltas},
            )
            if expect.new_breaches is not None:
                outcome.expect_equal("new breaches", set(expect.new_breaches), set(what_if.new_breaches))
            if expect.resolved_breaches is not None:
                outcome.expect_equal("resolved breaches", set(expect.resolved_breaches), set(what_if.resolved_breaches))
    except PortfolioLensError as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"
    return outcome


def _check_validation(coordinator: Coordinator, holdings_csv: str, expect: Expectation, outcome: CaseOutcome) -> None:
    try:
        coordinator.validate_inputs(holdings_csv)
    except InputValidationError as exc:
        wanted = set(expect.validation_codes or [])
        outcome.expect_equal("validation codes raised", wanted, {issue.code for issue in exc.issues} & wanted)
        return
    outcome.expect_equal("input rejected before any verdict", True, False)


def _confirm(
    coordinator: Coordinator, spec: CaseSpec, sid_text: str, source: MandateSource, outcome: CaseOutcome
) -> ConfirmedMandate:
    if spec.ground_truth_mandate is None:
        raise ValueError(f"case {spec.id} needs a ground_truth_mandate")
    proposal = coordinator.propose_mandate(sid_text)
    if source == "extracted":
        _check_extraction(outcome, proposal, spec.ground_truth_mandate)
        return coordinator.confirm_mandate(proposal.proposal_id, AUTO_REVIEWER, acknowledge_issues=True)
    return coordinator.confirm_mandate(
        proposal.proposal_id, AUTO_REVIEWER, corrected=ground_truth_mandate(spec.ground_truth_mandate, sid_text)
    )


def ground_truth_mandate(truth: GroundTruthMandate, sid_text: str) -> Mandate:
    limits = []
    for limit in truth.self_imposed_limits:
        citation = locate_quote(sid_text, limit.quote)
        if citation is None:
            raise ValueError(f"fixture quote not found in the SID: {limit.quote!r}")
        limits.append(
            SelfImposedLimit(
                metric=limit.metric, comparator=limit.comparator, threshold=limit.threshold, citation=citation
            )
        )
    return Mandate(
        scheme_name=truth.scheme_name,
        category=truth.category,
        sector=truth.sector,
        benchmark=truth.benchmark,
        self_imposed_limits=limits,
    )


def _check_extraction(outcome: CaseOutcome, proposal: MandateProposal, truth: GroundTruthMandate) -> None:
    outcome.expect_equal("extract.category", truth.category, proposal.category)
    if truth.sector or proposal.sector:
        outcome.expect_equal("extract.sector", (truth.sector or "").casefold(), (proposal.sector or "").casefold())
    outcome.expect_equal(
        "extract.self_imposed_limits",
        _limit_keys((l.metric, l.comparator, l.threshold) for l in truth.self_imposed_limits),
        _limit_keys((l.metric, l.comparator, l.threshold) for l in proposal.self_imposed_limits),
    )


def _limit_keys(limits: Any) -> tuple[str, ...]:
    return tuple(
        sorted(f"{metric.value} {comparator.value} {plain(threshold)}" for metric, comparator, threshold in limits)
    )


def _check_findings(
    outcome: CaseOutcome,
    label: str,
    expected: dict[str, ExpectedFinding],
    actual: dict[str, tuple[Status, Decimal, bool | None]],
) -> None:
    for constraint_id, want in expected.items():
        got = actual.get(constraint_id)
        if got is None:
            outcome.checks.append(Check(f"{label} {constraint_id}", want.status.value, "missing", False))
            continue
        status, margin, binding = got
        outcome.expect_equal(f"{label} {constraint_id} status", want.status, status)
        if want.margin is not None:
            outcome.expect_equal(f"{label} {constraint_id} margin", q2(want.margin), q2(margin))
        if want.binding is not None:
            outcome.expect_equal(f"{label} {constraint_id} binding", want.binding, binding)


def _show(value: Any) -> str:
    if isinstance(value, (set, frozenset)):
        return "{" + ", ".join(sorted(str(v) for v in value)) + "}"
    return str(value)


# --- reporting --------------------------------------------------------------------------


def report_payload(report: BacktestReport) -> dict[str, Any]:
    return {
        "generated_at": report.generated_at.isoformat(),
        "llm": report.llm,
        "summary": {source: dict(zip(("passed", "total"), report.passed(source), strict=True)) for source in SOURCES},
        "extraction_accuracy": dict(zip(("correct", "total"), report.extraction_accuracy(), strict=True)),
        "outcomes": [{**asdict(o), "passed": o.passed} for o in report.outcomes],
    }


def write_report(report: BacktestReport, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(report_payload(report), indent=2, default=str), encoding="utf-8")
    markdown_path = out_dir / "report.md"
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return markdown_path


def render_markdown(report: BacktestReport) -> str:
    extracted_ok, extracted_total = report.passed("extracted")
    truth_ok, truth_total = report.passed("ground_truth")
    fields_ok, fields_total = report.extraction_accuracy()
    lines = [
        "# PortfolioLens backtest report",
        "",
        f"- **Generated:** {report.generated_at:%Y-%m-%d %H:%M UTC}",
        f"- **Model mode:** {report.llm}",
        "- **Data:** synthetic holdings, SID extracts and reference lists (see `data/README.md`)",
        "",
        "## Summary",
        "",
        "| Mandate source at Gate 1 | Cases passed | What it measures |",
        "|---|---|---|",
        f"| extracted (agent proposal auto-confirmed) | {extracted_ok}/{extracted_total} "
        "| agents + engine end to end |",
        f"| ground truth (reviewer-corrected mandate) | {truth_ok}/{truth_total} | the deterministic engine |",
        "",
        f"Mandate extraction field accuracy: **{fields_ok}/{fields_total}** checks.",
        "",
        "## Cases",
        "",
        "| Case | Title | Kind | Extracted | Ground truth |",
        "|---|---|---|---|---|",
    ]
    by_case: dict[str, dict[str, CaseOutcome]] = {}
    for outcome in report.outcomes:
        by_case.setdefault(outcome.case_id, {})[outcome.source] = outcome
    for case_id, outcomes in by_case.items():
        first = next(iter(outcomes.values()))
        cells = [_verdict(outcomes.get(source)) for source in SOURCES]
        lines.append(f"| {case_id} | {first.title} | {first.kind} | {cells[0]} | {cells[1]} |")

    lines += ["", "## Where it fails", ""]
    failures = [o for o in report.outcomes if not o.passed]
    if not failures:
        lines.append("No failures in this run.")
    for outcome in failures:
        lines.append(f"### Case {outcome.case_id}: {outcome.title} ({outcome.source})")
        if outcome.notes:
            lines.append(f"_{outcome.notes}_")
        if outcome.error:
            lines.append(f"- Error: `{outcome.error}`")
        lines += [
            f"- `{check.name}`: expected `{check.expected}`, got `{check.actual}`"
            for check in outcome.checks
            if not check.ok
        ]
        lines.append("")
    lines += [
        "",
        "## How to read this",
        "",
        "- A ground-truth failure is an engine or fixture bug. An extracted-only failure is an agent miss that "
        "Gate 1 exists to catch.",
        "- Review-item checks compare only engine-raised kinds; acknowledged mandate issues are process notes.",
        "",
    ]
    return "\n".join(lines)


def _verdict(outcome: CaseOutcome | None) -> str:
    if outcome is None:
        return "-"
    return "PASS" if outcome.passed else "**FAIL**"
