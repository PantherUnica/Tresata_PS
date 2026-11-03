from decimal import Decimal

import pytest

from portfoliolens.domain import (
    CapBucket,
    ClassifiedHolding,
    Comparator,
    Constraint,
    Funding,
    Holding,
    InstrumentType,
    Metric,
    RunStatus,
    Status,
    Trade,
)
from portfoliolens.engine import apply_trades, assess_eligibility, reconcile
from portfoliolens.errors import TradeError

BAND = Decimal("1.0")


def holding(row: int, isin: str | None, pct: str, kind: InstrumentType = InstrumentType.EQUITY) -> Holding:
    return Holding(
        row=row,
        isin=isin,
        name=isin or f"Unnamed {row}",
        instrument_type=kind,
        market_value=Decimal(pct),
        reported_pct=Decimal(pct),
    )


def equity(row: int, isin: str, pct: str, bucket: CapBucket) -> ClassifiedHolding:
    return ClassifiedHolding(
        holding=holding(row, isin, pct),
        method="isin_join",
        confidence=1.0,
        resolved_isin=isin,
        resolved_name=isin,
        bucket=bucket,
        sector="Banking",
    )


def cash(row: int, pct: str) -> ClassifiedHolding:
    return ClassifiedHolding(holding=holding(row, None, pct, InstrumentType.CASH), method="not_equity", confidence=1.0)


def unresolved(row: int, pct: str) -> ClassifiedHolding:
    return ClassifiedHolding(holding=holding(row, None, pct), method="unresolved", confidence=0.0, note="ambiguous")


def rule(cid: str, metric: Metric, comparator: Comparator, threshold: str, kind: str = "SEBI") -> Constraint:
    return Constraint(
        constraint_id=cid,
        metric=metric,
        comparator=comparator,
        threshold=Decimal(threshold),
        source_kind=kind,  # type: ignore[arg-type]
        source_ref=cid,
        text=cid,
    )


LARGE_80 = rule("LARGE-80", Metric.LARGE_CAP_PCT, Comparator.MIN, "80")
ISSUER_10 = rule("ISSUER-10", Metric.SINGLE_ISSUER_PCT, Comparator.MAX, "10")


def large_fund(large_pct: str) -> list[ClassifiedHolding]:
    """Large-cap position split over ten names, plus a mid-cap name and cash, summing to 100."""
    large = Decimal(large_pct)
    slice_ = large / 10
    rows = [equity(i, f"L{i}", str(slice_), CapBucket.LARGE) for i in range(10)]
    mid = Decimal(95) - large
    rows.append(equity(20, "M1", str(mid), CapBucket.MID))
    rows.append(cash(21, "5"))
    return rows


def test_compliant_portfolio() -> None:
    result = reconcile(large_fund("88"), [LARGE_80, ISSUER_10], near_threshold_band=BAND)
    assert result.status is RunStatus.COMPLIANT
    finding = result.finding("LARGE-80")
    assert finding is not None and finding.status is Status.PASS and finding.margin == Decimal("8.0000")


def test_threshold_exactly_met_passes_but_is_flagged_for_review() -> None:
    result = reconcile(large_fund("80"), [LARGE_80], near_threshold_band=BAND)
    finding = result.finding("LARGE-80")
    assert finding is not None and finding.status is Status.PASS and finding.margin == 0
    assert [r.kind for r in result.review_items] == ["near_threshold"]
    assert result.status is RunStatus.NEEDS_REVIEW


def test_stricter_self_imposed_limit_binds() -> None:
    sid_85 = rule("SID-85", Metric.LARGE_CAP_PCT, Comparator.MIN, "85", kind="SID")
    result = reconcile(large_fund("83"), [LARGE_80, sid_85], near_threshold_band=BAND)
    sebi, sid = result.finding("LARGE-80"), result.finding("SID-85")
    assert sebi is not None and sebi.status is Status.PASS and not sebi.binding
    assert sid is not None and sid.status is Status.BREACH and sid.binding and sid.margin == Decimal("-2.0000")
    assert result.status is RunStatus.BREACH


def test_unclassified_holdings_make_a_close_verdict_indeterminate() -> None:
    rows = [equity(1, "L1", "78.5", CapBucket.LARGE), unresolved(2, "3"), cash(3, "18.5")]
    result = reconcile(rows, [LARGE_80], near_threshold_band=BAND)
    finding = result.finding("LARGE-80")
    assert finding is not None and finding.status is Status.INDETERMINATE
    assert (finding.value_low, finding.value_high) == (Decimal("78.5000"), Decimal("81.5000"))
    assert {r.kind for r in result.review_items} == {"unresolved_holding", "indeterminate"}


def test_unclassified_holdings_cannot_rescue_a_clear_breach() -> None:
    rows = [equity(1, "L1", "70", CapBucket.LARGE), unresolved(2, "3"), cash(3, "27")]
    finding = reconcile(rows, [LARGE_80], near_threshold_band=BAND).finding("LARGE-80")
    assert finding is not None and finding.status is Status.BREACH


def test_max_rule_margin_is_headroom() -> None:
    within = [equity(1, "A", "9", CapBucket.LARGE), equity(2, "B", "8", CapBucket.LARGE), cash(3, "83")]
    finding = reconcile(within, [ISSUER_10], near_threshold_band=Decimal(0)).finding("ISSUER-10")
    assert finding is not None and finding.status is Status.PASS and finding.margin == Decimal("1.0000")

    over = [equity(1, "A", "11", CapBucket.LARGE), cash(2, "89")]
    finding = reconcile(over, [ISSUER_10], near_threshold_band=Decimal(0)).finding("ISSUER-10")
    assert finding is not None and finding.status is Status.BREACH and finding.margin == Decimal("-1.0000")


def test_cash_funded_buy_requires_enough_cash() -> None:
    holdings = [c.holding for c in large_fund("88")]
    with pytest.raises(TradeError):
        apply_trades(holdings, [Trade(action="buy", isin="NEW", name="New", pct_of_nav=Decimal("6"))], Funding.CASH)


def test_pro_rata_funding_keeps_net_assets_constant() -> None:
    holdings = [c.holding for c in large_fund("88")]
    after = apply_trades(
        holdings, [Trade(action="buy", isin="NEW", name="New", pct_of_nav=Decimal("3"))], Funding.PRO_RATA
    )
    assert sum(h.market_value for h in after) == sum(h.market_value for h in holdings)
    new = next(h for h in after if h.isin == "NEW")
    assert new.market_value == Decimal(3)
    cash_row = next(h for h in after if h.instrument_type is InstrumentType.CASH)
    assert cash_row.market_value == Decimal(5)


def test_sell_moves_proceeds_to_cash() -> None:
    holdings = [c.holding for c in large_fund("88")]
    after = apply_trades(holdings, [Trade(action="sell", isin="L0", name="L0", pct_of_nav=Decimal("2"))], Funding.CASH)
    assert next(h for h in after if h.instrument_type is InstrumentType.CASH).market_value == Decimal(7)
    with pytest.raises(TradeError):
        apply_trades(holdings, [Trade(action="sell", isin="NOPE", name="x", pct_of_nav=Decimal("1"))], Funding.CASH)


def test_eligibility_blocks_only_new_or_deeper_breaches() -> None:
    before = reconcile(large_fund("82"), [LARGE_80], near_threshold_band=BAND)
    worse = reconcile(large_fund("79"), [LARGE_80], near_threshold_band=BAND)
    better = reconcile(large_fund("84"), [LARGE_80], near_threshold_band=BAND)
    blocked = assess_eligibility(before, worse)
    assert not blocked.eligible and blocked.blocking_constraint_ids == ["LARGE-80"]
    assert assess_eligibility(before, better).eligible
