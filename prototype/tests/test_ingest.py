from decimal import Decimal

import pytest

from helpers import case_text
from portfoliolens.errors import InputValidationError
from portfoliolens.ingest import is_valid_isin, load_sid, parse_candidates, parse_holdings, parse_trades

HEADER = "isin,name,instrument_type,market_value,pct_of_nav\n"


def codes(exc: pytest.ExceptionInfo[InputValidationError]) -> set[str]:
    return {issue.code for issue in exc.value.issues}


def test_isin_check_digit_matches_a_published_isin() -> None:
    assert is_valid_isin("US0378331005")
    assert not is_valid_isin("US0378331006")
    assert not is_valid_isin("INE12345")


def test_parses_a_valid_holdings_file() -> None:
    holdings = parse_holdings(case_text("01_large_cap_compliant", "holdings.csv"))
    assert len(holdings.holdings) == 18
    assert holdings.nav == Decimal("10000.00")
    assert len(holdings.sha256) == 64


def test_invalid_file_reports_every_row_level_issue() -> None:
    with pytest.raises(InputValidationError) as exc:
        parse_holdings(case_text("09_invalid_holdings", "holdings.csv"))
    assert {"missing_value", "duplicate_isin", "invalid_isin", "bad_total"} <= codes(exc)
    missing = next(issue for issue in exc.value.issues if issue.code == "missing_value")
    assert (missing.row, missing.field) == (3, "market_value")


def test_missing_columns_are_rejected_up_front() -> None:
    with pytest.raises(InputValidationError) as exc:
        parse_holdings("isin,name\nX,Y\n")
    assert codes(exc) == {"missing_column"}


def test_non_finite_numbers_are_rejected() -> None:
    with pytest.raises(InputValidationError) as exc:
        parse_holdings(HEADER + ",Cash,cash,NaN,100\n")
    assert "invalid_number" in codes(exc)


def test_reported_percentage_must_match_market_value_share() -> None:
    with pytest.raises(InputValidationError) as exc:
        parse_holdings(HEADER + ",Cash A,cash,500,60\n,Cash B,cash,500,40\n")
    assert codes(exc) == {"row_pct_mismatch"}


def test_empty_inputs_are_rejected() -> None:
    with pytest.raises(InputValidationError):
        parse_holdings("")
    with pytest.raises(InputValidationError):
        load_sid("   ")


def test_candidates_and_trades_are_validated() -> None:
    with pytest.raises(InputValidationError) as exc:
        parse_candidates("isin,name\nINE12345,Bad\nUS0378331005,\n")
    assert {"invalid_isin", "missing_value"} <= codes(exc)
    with pytest.raises(InputValidationError) as exc:
        parse_trades("action,isin,name,pct_of_nav\nhold,US0378331005,Apple,1\n")
    assert codes(exc) == {"invalid_trade"}
