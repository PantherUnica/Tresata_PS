"""Domain model shared by every stage. Every number that can reach a verdict is a Decimal."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InstrumentType(StrEnum):
    EQUITY = "equity"
    CASH = "cash"
    DEBT = "debt"
    OTHER = "other"


class CapBucket(StrEnum):
    LARGE = "large"
    MID = "mid"
    SMALL = "small"


class SchemeCategory(StrEnum):
    LARGE_CAP = "large_cap"
    LARGE_MID_CAP = "large_mid_cap"
    MID_CAP = "mid_cap"
    SMALL_CAP = "small_cap"
    MULTI_CAP = "multi_cap"
    FLEXI_CAP = "flexi_cap"
    ELSS = "elss"
    FOCUSED = "focused"
    SECTORAL_THEMATIC = "sectoral_thematic"
    VALUE = "value"
    CONTRA = "contra"
    DIVIDEND_YIELD = "dividend_yield"


class Metric(StrEnum):
    LARGE_CAP_PCT = "large_cap_pct"
    MID_CAP_PCT = "mid_cap_pct"
    SMALL_CAP_PCT = "small_cap_pct"
    EQUITY_PCT = "equity_pct"
    SECTOR_PCT = "sector_pct"
    SINGLE_ISSUER_PCT = "single_issuer_pct"
    STOCK_COUNT = "stock_count"

    @property
    def is_percentage(self) -> bool:
        return self is not Metric.STOCK_COUNT


BUCKET_METRIC: dict[CapBucket, Metric] = {
    CapBucket.LARGE: Metric.LARGE_CAP_PCT,
    CapBucket.MID: Metric.MID_CAP_PCT,
    CapBucket.SMALL: Metric.SMALL_CAP_PCT,
}
METRIC_BUCKET: dict[Metric, CapBucket] = {metric: bucket for bucket, metric in BUCKET_METRIC.items()}


class Comparator(StrEnum):
    MIN = "min"
    MAX = "max"


class Status(StrEnum):
    PASS = "PASS"
    BREACH = "BREACH"
    INDETERMINATE = "INDETERMINATE"


class RunStatus(StrEnum):
    COMPLIANT = "COMPLIANT"
    BREACH = "BREACH"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class Funding(StrEnum):
    CASH = "cash"
    PRO_RATA = "pro_rata"


# --- inputs -----------------------------------------------------------------------------


class Holding(_Model):
    row: int
    isin: str | None
    name: str
    instrument_type: InstrumentType
    market_value: Decimal = Field(gt=0)
    reported_pct: Decimal

    @property
    def issuer_key(self) -> str:
        return self.isin or "NAME:" + " ".join(self.name.lower().split())


class InputSummary(_Model):
    rows: int
    net_assets: Decimal
    holdings_sha256: str
    sid_sha256: str | None = None


class Security(_Model):
    isin: str
    name: str
    rank: int = Field(ge=1)
    bucket: CapBucket
    sector: str


# --- mandate ----------------------------------------------------------------------------


class Citation(_Model):
    quote: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class SelfImposedLimit(_Model):
    metric: Metric
    comparator: Comparator
    threshold: Decimal = Field(ge=0)
    citation: Citation | None = None

    @model_validator(mode="after")
    def _percentage_in_range(self) -> SelfImposedLimit:
        if self.metric.is_percentage and self.threshold > 100:
            raise ValueError("a percentage threshold cannot exceed 100")
        return self


class MandateProposal(_Model):
    """What the extraction agent proposes. Loose on purpose: a human completes it at Gate 1."""

    proposal_id: str
    sid_sha256: str
    extractor: str
    scheme_name: str | None = None
    category: SchemeCategory | None = None
    category_citation: Citation | None = None
    benchmark: str | None = None
    sector: str | None = None
    sector_citation: Citation | None = None
    self_imposed_limits: list[SelfImposedLimit] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class Mandate(_Model):
    scheme_name: str = Field(min_length=1)
    category: SchemeCategory
    category_citation: Citation | None = None
    benchmark: str | None = None
    sector: str | None = None
    sector_citation: Citation | None = None
    self_imposed_limits: list[SelfImposedLimit] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sector_present_when_needed(self) -> Mandate:
        needs_sector = self.category is SchemeCategory.SECTORAL_THEMATIC or any(
            limit.metric is Metric.SECTOR_PCT for limit in self.self_imposed_limits
        )
        if needs_sector and not self.sector:
            raise ValueError("a sector is required for sectoral/thematic schemes and sector limits")
        return self


class ConfirmedMandate(_Model):
    mandate_id: str
    proposal_id: str
    mandate: Mandate
    sid_sha256: str
    extractor: str
    confirmed_by: str = Field(min_length=1)
    confirmed_at: datetime
    corrected_by_reviewer: bool
    acknowledged_issues: list[str] = Field(default_factory=list)


# --- rules ------------------------------------------------------------------------------


class Rule(_Model):
    rule_id: str
    applies_to: list[str] = Field(min_length=1)
    metric: Metric
    comparator: Comparator
    threshold: Decimal
    text: str
    text_kind: Literal["paraphrase", "verbatim"]
    source_ref: str


class RulebookVersion(_Model):
    version: str
    effective_from: date
    circulars: list[str]
    note: str
    rules: list[Rule]


class Constraint(_Model):
    """A rule or a self-imposed limit in the single shape the engine evaluates."""

    constraint_id: str
    metric: Metric
    comparator: Comparator
    threshold: Decimal
    source_kind: Literal["SEBI", "SID"]
    source_ref: str
    text: str
    sector: str | None = None


# --- classification & results -----------------------------------------------------------

ClassificationMethod = Literal["isin_join", "name_match", "llm_identity", "unresolved", "not_equity"]


class ClassifiedHolding(_Model):
    holding: Holding
    method: ClassificationMethod
    confidence: float = Field(ge=0, le=1)
    resolved_isin: str | None = None
    resolved_name: str | None = None
    bucket: CapBucket | None = None
    sector: str | None = None
    note: str = ""

    @property
    def issuer_key(self) -> str:
        return self.resolved_isin or self.holding.issuer_key

    @property
    def is_unresolved_equity(self) -> bool:
        return self.holding.instrument_type is InstrumentType.EQUITY and self.bucket is None


class Fact(_Model):
    fact_id: str
    label: str
    value: Decimal
    unit: Literal["pct", "count", "inr"]
    detail: str = ""


class Finding(_Model):
    finding_id: str
    constraint: Constraint
    value_low: Decimal
    value_high: Decimal
    status: Status
    margin: Decimal
    binding: bool
    summary: str


ReviewKind = Literal[
    "unresolved_holding",
    "identity_by_name",
    "near_threshold",
    "indeterminate",
    "mandate_issue",
    "report_rejected",
]


class ReviewItem(_Model):
    review_id: str
    kind: ReviewKind
    detail: str
    ref: str | None = None


class EngineResult(_Model):
    status: RunStatus
    facts: list[Fact]
    findings: list[Finding]
    review_items: list[ReviewItem]

    def finding(self, constraint_id: str) -> Finding | None:
        return next((f for f in self.findings if f.constraint.constraint_id == constraint_id), None)


class VersionPin(_Model):
    kind: Literal["rulebook", "security_master", "engine"]
    version: str
    sha256: str | None = None


class RuleRetrievalAudit(_Model):
    version: str
    effective_from: date
    as_of: date
    rulebook_sha256: str
    query: str
    retrieved_rule_ids: list[str]
    applied_rule_ids: list[str]
    method: Literal["langchain_lexical", "langchain_retrieval_llm_rerank"]
    fallback_reason: str | None = None


class Report(_Model):
    markdown: str
    narrative_source: Literal["template", "llm"]
    llm_note: str | None = None
    llm_output_rejected: bool = False


class SignOff(_Model):
    reviewer: str = Field(min_length=1)
    decision: Literal["approved", "escalated"]
    note: str = ""
    signed_at: datetime
    acknowledged_review_ids: list[str]


class Run(_Model):
    run_id: str
    created_at: datetime
    as_of: date
    llm: str
    mandate: ConfirmedMandate
    pins: list[VersionPin]
    rule_retrieval: RuleRetrievalAudit | None = None
    holdings_sha256: str
    holdings: list[Holding]
    classification: list[ClassifiedHolding]
    constraints: list[Constraint]
    result: EngineResult
    report: Report
    sign_off: SignOff | None = None

    def pin(self, kind: str) -> VersionPin:
        return next(p for p in self.pins if p.kind == kind)


class RunSummary(_Model):
    run_id: str
    scheme_name: str
    category: SchemeCategory
    as_of: date
    status: RunStatus
    created_at: datetime
    review_items: int
    signed_by: str | None
    decision: Literal["approved", "escalated"] | None


# --- P1: screening & what-if ------------------------------------------------------------


class Trade(_Model):
    action: Literal["buy", "sell"]
    isin: str
    name: str
    pct_of_nav: Decimal = Field(gt=0, le=100)


class ConstraintDelta(_Model):
    constraint_id: str
    before_status: Status
    after_status: Status
    before_margin: Decimal
    after_margin: Decimal
    worsened: bool


class WhatIfResult(_Model):
    scenario_id: str
    run_id: str
    trades: list[Trade]
    funding: Funding
    before_status: RunStatus
    after_status: RunStatus
    deltas: list[ConstraintDelta]
    new_breaches: list[str]
    resolved_breaches: list[str]
    after_findings: list[Finding]
    after_review_items: list[ReviewItem]
    executed: Literal[False] = False
    note: str


class CandidateResult(_Model):
    isin: str | None
    name: str
    bucket: CapBucket | None
    sector: str | None
    classification_method: ClassificationMethod
    eligible: bool
    reasons: list[str]
    blocking_constraint_ids: list[str]
    post_trade_headroom_pp: Decimal | None


class ScreeningResult(_Model):
    scenario_id: str
    run_id: str
    weight_pct: Decimal
    funding: Funding
    sort_by: Literal["name", "headroom"]
    candidates: list[CandidateResult]
    note: str


class Answer(_Model):
    run_id: str
    question: str
    declined: bool
    text: str
    cited_ids: list[str]
    source: Literal["scope_guard", "template", "llm"]
