"""Deterministic reconciliation, what-if and eligibility. No model output is ever computed here."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from portfoliolens.domain import (
    METRIC_BUCKET,
    CapBucket,
    ClassifiedHolding,
    Comparator,
    Constraint,
    ConstraintDelta,
    EngineResult,
    Fact,
    Finding,
    Funding,
    Holding,
    InstrumentType,
    Metric,
    ReviewItem,
    ReviewKind,
    RunStatus,
    Status,
    Trade,
)
from portfoliolens.errors import TradeError

ENGINE_VERSION = "1.0.0"
ZERO = Decimal(0)
HUNDRED = Decimal(100)
TWO_DP = Decimal("0.01")
FOUR_DP = Decimal("0.0001")
MONEY_STEP = Decimal("0.00000001")

FACT_NAV = "F-NAV"
FACT_EQUITY = "F-EQUITY-PCT"
FACT_BUCKET = {
    CapBucket.LARGE: "F-LARGE-CAP-PCT",
    CapBucket.MID: "F-MID-CAP-PCT",
    CapBucket.SMALL: "F-SMALL-CAP-PCT",
}
FACT_UNCLASSIFIED = "F-UNCLASSIFIED-EQUITY-PCT"
FACT_MAX_ISSUER = "F-MAX-ISSUER-PCT"
FACT_STOCK_COUNT = "F-STOCK-COUNT"

_METRIC_LABEL = {
    Metric.LARGE_CAP_PCT: "Large-cap allocation",
    Metric.MID_CAP_PCT: "Mid-cap allocation",
    Metric.SMALL_CAP_PCT: "Small-cap allocation",
    Metric.EQUITY_PCT: "Equity allocation",
    Metric.SECTOR_PCT: "Sector allocation",
    Metric.SINGLE_ISSUER_PCT: "Largest single-issuer weight",
    Metric.STOCK_COUNT: "Equity issuer count",
}
_SEVERITY = {Status.PASS: 0, Status.INDETERMINATE: 1, Status.BREACH: 2}

ReviewSpec = tuple[ReviewKind, str, str | None]


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWO_DP, rounding=ROUND_HALF_UP)


def q4(value: Decimal) -> Decimal:
    return value.quantize(FOUR_DP, rounding=ROUND_HALF_UP)


def plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


# --- metrics ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioMetrics:
    nav: Decimal
    equity_pct: Decimal
    bucket_pct: dict[CapBucket, Decimal]
    unclassified_pct: Decimal
    sector_pct: dict[str, Decimal]
    unknown_sector_pct: Decimal
    issuer_pct: dict[str, tuple[str, Decimal]] = field(default_factory=dict)
    stock_count: int = 0

    @property
    def max_issuer(self) -> tuple[str, Decimal]:
        if not self.issuer_pct:
            return "-", ZERO
        return max(self.issuer_pct.values(), key=lambda item: item[1])


def compute_metrics(classified: Sequence[ClassifiedHolding]) -> PortfolioMetrics:
    nav = sum((c.holding.market_value for c in classified), ZERO)
    if nav <= 0:
        raise ValueError("portfolio has no market value")

    bucket_mv = {bucket: ZERO for bucket in CapBucket}
    sector_mv: dict[str, Decimal] = defaultdict(lambda: ZERO)
    issuer_mv: dict[str, Decimal] = defaultdict(lambda: ZERO)
    issuer_name: dict[str, str] = {}
    equity_mv = unclassified_mv = unknown_sector_mv = ZERO

    for item in classified:
        if item.holding.instrument_type is not InstrumentType.EQUITY:
            continue
        mv = item.holding.market_value
        equity_mv += mv
        if item.bucket is None:
            unclassified_mv += mv
        else:
            bucket_mv[item.bucket] += mv
        if item.sector:
            sector_mv[item.sector.casefold()] += mv
        else:
            unknown_sector_mv += mv
        issuer_mv[item.issuer_key] += mv
        issuer_name.setdefault(item.issuer_key, item.resolved_name or item.holding.name)

    def pct(value: Decimal) -> Decimal:
        return value * HUNDRED / nav

    return PortfolioMetrics(
        nav=nav,
        equity_pct=pct(equity_mv),
        bucket_pct={bucket: pct(mv) for bucket, mv in bucket_mv.items()},
        unclassified_pct=pct(unclassified_mv),
        sector_pct={sector: pct(mv) for sector, mv in sector_mv.items()},
        unknown_sector_pct=pct(unknown_sector_mv),
        issuer_pct={key: (issuer_name[key], pct(mv)) for key, mv in issuer_mv.items()},
        stock_count=len(issuer_mv),
    )


def metric_bounds(metrics: PortfolioMetrics, constraint: Constraint) -> tuple[Decimal, Decimal]:
    """Lowest and highest value the metric can take given holdings we could not classify."""
    metric = constraint.metric
    if metric in METRIC_BUCKET:
        value = metrics.bucket_pct[METRIC_BUCKET[metric]]
        return value, value + metrics.unclassified_pct
    if metric is Metric.EQUITY_PCT:
        return metrics.equity_pct, metrics.equity_pct
    if metric is Metric.SECTOR_PCT:
        if not constraint.sector:
            raise ValueError(f"{constraint.constraint_id} is a sector rule without a sector")
        value = metrics.sector_pct.get(constraint.sector.casefold(), ZERO)
        return value, value + metrics.unknown_sector_pct
    if metric is Metric.SINGLE_ISSUER_PCT:
        _, value = metrics.max_issuer
        return value, value
    if metric is Metric.STOCK_COUNT:
        count = Decimal(metrics.stock_count)
        return count, count
    raise AssertionError(f"unhandled metric {metric}")


def evaluate(constraint: Constraint, low: Decimal, high: Decimal) -> tuple[Status, Decimal]:
    """Status plus a signed margin where positive always means headroom."""
    threshold = constraint.threshold
    if constraint.comparator is Comparator.MIN:
        margin = low - threshold
        if low >= threshold:
            return Status.PASS, margin
        return (Status.BREACH if high < threshold else Status.INDETERMINATE), margin
    margin = threshold - high
    if high <= threshold:
        return Status.PASS, margin
    return (Status.BREACH if low > threshold else Status.INDETERMINATE), margin


def binding_constraint_ids(constraints: Sequence[Constraint]) -> set[str]:
    """For each metric and direction the strictest threshold binds (ties go to the earlier, SEBI, rule)."""
    strictest: dict[tuple[Metric, Comparator, str], Constraint] = {}
    for constraint in constraints:
        key = (constraint.metric, constraint.comparator, (constraint.sector or "").casefold())
        current = strictest.get(key)
        if current is None:
            strictest[key] = constraint
            continue
        stricter = (
            constraint.threshold > current.threshold
            if constraint.comparator is Comparator.MIN
            else constraint.threshold < current.threshold
        )
        if stricter:
            strictest[key] = constraint
    return {constraint.constraint_id for constraint in strictest.values()}


# --- reconciliation ---------------------------------------------------------------------


def reconcile(
    classified: Sequence[ClassifiedHolding],
    constraints: Sequence[Constraint],
    *,
    near_threshold_band: Decimal,
    extra_reviews: Sequence[ReviewSpec] = (),
) -> EngineResult:
    metrics = compute_metrics(classified)
    binding = binding_constraint_ids(constraints)
    reviews: list[ReviewSpec] = list(_classification_reviews(classified))
    findings: list[Finding] = []

    for constraint in constraints:
        low, high = metric_bounds(metrics, constraint)
        status, margin = evaluate(constraint, low, high)
        finding_id = f"FND-{constraint.constraint_id}"
        findings.append(
            Finding(
                finding_id=finding_id,
                constraint=constraint,
                value_low=q4(low),
                value_high=q4(high),
                status=status,
                margin=q4(margin),
                binding=constraint.constraint_id in binding,
                summary=_summary(constraint, low, high, status, margin),
            )
        )
        if status is Status.INDETERMINATE:
            reviews.append(
                (
                    "indeterminate",
                    f"{constraint.constraint_id}: the verdict depends on unclassified holdings "
                    f"({q2(low)}% to {q2(high)}%).",
                    finding_id,
                )
            )
        elif status is Status.PASS and constraint.metric.is_percentage and margin < near_threshold_band:
            reviews.append(
                (
                    "near_threshold",
                    f"{constraint.constraint_id}: passes by only {q2(margin)} pp "
                    f"(review band {plain(near_threshold_band)} pp).",
                    finding_id,
                )
            )

    reviews.extend(extra_reviews)
    review_items = _number_reviews(reviews)
    return EngineResult(
        status=_overall_status(findings, review_items),
        facts=_facts(metrics, constraints),
        findings=findings,
        review_items=review_items,
    )


def with_review_items(result: EngineResult, extra: Sequence[ReviewSpec]) -> EngineResult:
    if not extra:
        return result
    existing = [(item.kind, item.detail, item.ref) for item in result.review_items]
    review_items = _number_reviews([*existing, *extra])
    return result.model_copy(
        update={"review_items": review_items, "status": _overall_status(result.findings, review_items)}
    )


def _number_reviews(reviews: Sequence[ReviewSpec]) -> list[ReviewItem]:
    return [
        ReviewItem(review_id=f"RV-{index:02d}", kind=kind, detail=detail, ref=ref)
        for index, (kind, detail, ref) in enumerate(reviews, start=1)
    ]


def _overall_status(findings: Sequence[Finding], review_items: Sequence[ReviewItem]) -> RunStatus:
    if any(f.status is Status.BREACH for f in findings):
        return RunStatus.BREACH
    if review_items:
        return RunStatus.NEEDS_REVIEW
    return RunStatus.COMPLIANT


def _classification_reviews(classified: Sequence[ClassifiedHolding]) -> list[ReviewSpec]:
    reviews: list[ReviewSpec] = []
    for item in classified:
        holding = item.holding
        if item.is_unresolved_equity:
            reviews.append(
                (
                    "unresolved_holding",
                    f"Row {holding.row}: '{holding.name}' could not be matched to the security master "
                    f"({item.note}); it is excluded from every cap bucket.",
                    f"row:{holding.row}",
                )
            )
        elif item.method in ("name_match", "llm_identity"):
            reviews.append(
                (
                    "identity_by_name",
                    f"Row {holding.row}: '{holding.name}' was matched to {item.resolved_name} ({item.resolved_isin}) "
                    f"by {'model' if item.method == 'llm_identity' else 'name'}, confidence {item.confidence:.2f}; "
                    "confirm the identity.",
                    f"row:{holding.row}",
                )
            )
    return reviews


def _facts(metrics: PortfolioMetrics, constraints: Sequence[Constraint]) -> list[Fact]:
    issuer_name, issuer_pct = metrics.max_issuer
    facts = [
        Fact(fact_id=FACT_NAV, label="Net assets (sum of market values)", value=q2(metrics.nav), unit="inr"),
        Fact(fact_id=FACT_EQUITY, label="Equity allocation", value=q4(metrics.equity_pct), unit="pct"),
        *(
            Fact(fact_id=FACT_BUCKET[bucket], label=f"{bucket.value.title()}-cap allocation", value=q4(pct), unit="pct")
            for bucket, pct in metrics.bucket_pct.items()
        ),
        Fact(
            fact_id=FACT_UNCLASSIFIED,
            label="Equity not matched to a cap bucket",
            value=q4(metrics.unclassified_pct),
            unit="pct",
        ),
        Fact(
            fact_id=FACT_MAX_ISSUER,
            label="Largest single-issuer weight",
            value=q4(issuer_pct),
            unit="pct",
            detail=issuer_name,
        ),
        Fact(
            fact_id=FACT_STOCK_COUNT, label="Distinct equity issuers", value=Decimal(metrics.stock_count), unit="count"
        ),
    ]
    for sector in sorted({c.sector for c in constraints if c.sector}):
        slug = "-".join(sector.upper().split())
        facts.append(
            Fact(
                fact_id=f"F-SECTOR-PCT-{slug}",
                label=f"Allocation to {sector}",
                value=q4(metrics.sector_pct.get(sector.casefold(), ZERO)),
                unit="pct",
            )
        )
    return facts


def _summary(constraint: Constraint, low: Decimal, high: Decimal, status: Status, margin: Decimal) -> str:
    is_pct = constraint.metric.is_percentage
    unit = "%" if is_pct else ""

    def show(value: Decimal) -> str:
        return f"{q2(value)}{unit}" if is_pct else plain(value)

    value_text = show(low) if low == high else f"{show(low)} to {show(high)}"
    bound = "minimum" if constraint.comparator is Comparator.MIN else "maximum"
    label = _METRIC_LABEL[constraint.metric] + (f" ({constraint.sector})" if constraint.sector else "")
    margin_text = f"{q2(margin):+} pp" if is_pct else f"{margin:+}"
    return (
        f"{label} is {value_text} against a {bound} of {plain(constraint.threshold)}{unit} "
        f"[{constraint.source_ref}]: {status.value}, margin {margin_text}."
    )


# --- P1: what-if and eligibility --------------------------------------------------------


@dataclass
class _Position:
    holding: Holding
    market_value: Decimal


def apply_trades(holdings: Sequence[Holding], trades: Sequence[Trade], funding: Funding) -> list[Holding]:
    """Return the hypothetical portfolio after the trades. Net assets are held constant (no costs)."""
    nav = sum((h.market_value for h in holdings), ZERO)
    positions = [_Position(h, h.market_value) for h in holdings]
    next_row = max((h.row for h in holdings), default=1) + 1

    for trade in trades:
        amount = trade.pct_of_nav * nav / HUNDRED
        if trade.action == "buy":
            _fund(positions, amount, funding, trade)
            target = _find(positions, trade.isin)
            if target is not None:
                target.market_value += amount
            else:
                positions.append(
                    _Position(
                        Holding(
                            row=next_row,
                            isin=trade.isin,
                            name=trade.name,
                            instrument_type=InstrumentType.EQUITY,
                            market_value=amount,
                            reported_pct=trade.pct_of_nav,
                        ),
                        amount,
                    )
                )
                next_row += 1
        else:
            target = _find(positions, trade.isin)
            if target is None:
                raise TradeError(f"Cannot sell {trade.isin}: it is not held")
            if target.market_value < amount:
                raise TradeError(f"Cannot sell {plain(trade.pct_of_nav)}% of NAV of {trade.name}: position is smaller")
            target.market_value -= amount
            cash = next((p for p in positions if p.holding.instrument_type is InstrumentType.CASH), None)
            if cash is None:
                cash = _Position(
                    Holding(
                        row=next_row,
                        isin=None,
                        name="Cash (sale proceeds)",
                        instrument_type=InstrumentType.CASH,
                        market_value=amount,
                        reported_pct=trade.pct_of_nav,
                    ),
                    ZERO,
                )
                positions.append(cash)
                next_row += 1
            cash.market_value += amount

    return [
        p.holding.model_copy(update={"market_value": p.market_value, "reported_pct": p.market_value * HUNDRED / nav})
        for p in positions
        if p.market_value > 0
    ]


def _find(positions: Sequence[_Position], isin: str) -> _Position | None:
    return next((p for p in positions if p.holding.isin == isin), None)


def _fund(positions: Sequence[_Position], amount: Decimal, funding: Funding, trade: Trade) -> None:
    if funding is Funding.CASH:
        pool = [p for p in positions if p.holding.instrument_type is InstrumentType.CASH]
        source = "cash"
    else:
        pool = [p for p in positions if p.holding.instrument_type is not InstrumentType.CASH]
        source = "a pro-rata trim of existing holdings"
    available = sum((p.market_value for p in pool), ZERO)
    if available < amount:
        raise TradeError(f"Cannot fund {plain(trade.pct_of_nav)}% of NAV for {trade.name} from {source}")
    # Round each trim, then give the residual to the largest position so the total moved is exactly `amount`.
    trims = [(p.market_value * amount / available).quantize(MONEY_STEP, rounding=ROUND_HALF_UP) for p in pool]
    largest = max(range(len(pool)), key=lambda i: pool[i].market_value)
    trims[largest] += amount - sum(trims, ZERO)
    for position, trim in zip(pool, trims, strict=True):
        position.market_value -= trim


def compare_results(before: EngineResult, after: EngineResult) -> list[ConstraintDelta]:
    deltas = []
    for previous in before.findings:
        current = after.finding(previous.constraint.constraint_id)
        if current is None:
            continue
        worsened = current.status is not Status.PASS and (
            _SEVERITY[current.status] > _SEVERITY[previous.status] or current.margin < previous.margin
        )
        deltas.append(
            ConstraintDelta(
                constraint_id=previous.constraint.constraint_id,
                before_status=previous.status,
                after_status=current.status,
                before_margin=previous.margin,
                after_margin=current.margin,
                worsened=worsened,
            )
        )
    return deltas


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: list[str]
    blocking_constraint_ids: list[str]
    headroom_pp: Decimal | None


def assess_eligibility(before: EngineResult, after: EngineResult) -> Eligibility:
    """A candidate is eligible when buying it creates or deepens no breach and no uncertainty."""
    blocking = [delta for delta in compare_results(before, after) if delta.worsened]
    reasons = []
    for delta in blocking:
        finding = after.finding(delta.constraint_id)
        if finding is not None:
            reasons.append(finding.summary)
    headroom = min(
        (f.margin for f in after.findings if f.constraint.metric.is_percentage),
        default=None,
    )
    return Eligibility(
        eligible=not blocking,
        reasons=reasons or ["No constraint is breached or worsened by this purchase."],
        blocking_constraint_ids=[delta.constraint_id for delta in blocking],
        headroom_pp=q2(headroom) if headroom is not None else None,
    )
