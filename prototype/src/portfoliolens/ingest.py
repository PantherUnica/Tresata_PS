"""Input parsing and validation. Bad input yields row-level issues, never a fabricated verdict."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from portfoliolens.domain import Holding, InstrumentType, Trade
from portfoliolens.errors import InputValidationError, ValidationIssue

MAX_HOLDINGS_BYTES = 2_000_000
MAX_SID_BYTES = 1_000_000
MAX_ROWS = 5_000
HOLDINGS_COLUMNS = ("isin", "name", "instrument_type", "market_value", "pct_of_nav")
CANDIDATE_COLUMNS = ("isin", "name")
TRADE_COLUMNS = ("action", "isin", "name", "pct_of_nav")
TOTAL_TOLERANCE_PP = Decimal("0.5")
ROW_TOLERANCE_PP = Decimal("0.01")
HUNDRED = Decimal(100)

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def isin_check_digit(body: str) -> int:
    """ISO 6166 check digit for the first 11 characters of an ISIN."""
    digits = "".join(str(int(char, 36)) for char in body)
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return (10 - total % 10) % 10


def is_valid_isin(isin: str) -> bool:
    return bool(_ISIN_RE.match(isin)) and isin_check_digit(isin[:11]) == int(isin[11])


@dataclass(frozen=True)
class SidDocument:
    text: str
    sha256: str


@dataclass(frozen=True)
class HoldingsFile:
    holdings: tuple[Holding, ...]
    sha256: str

    @property
    def nav(self) -> Decimal:
        return sum((h.market_value for h in self.holdings), Decimal(0))


def load_sid(text: str) -> SidDocument:
    if not text or not text.strip():
        raise InputValidationError([ValidationIssue(code="empty_file", message="SID text is empty", field="sid")])
    if len(text.encode("utf-8")) > MAX_SID_BYTES:
        raise InputValidationError(
            [ValidationIssue(code="too_large", message=f"SID exceeds {MAX_SID_BYTES} bytes", field="sid")]
        )
    return SidDocument(text=text, sha256=sha256_text(text))


def parse_holdings(csv_text: str) -> HoldingsFile:
    rows = _read_rows(csv_text, HOLDINGS_COLUMNS, label="Holdings file", max_bytes=MAX_HOLDINGS_BYTES)
    issues: list[ValidationIssue] = []
    holdings: list[Holding] = []
    first_row_for_isin: dict[str, int] = {}
    reported_total = Decimal(0)

    for row_no, record in rows:
        row_issues: list[ValidationIssue] = []
        name = record["name"]
        if not name:
            row_issues.append(_issue("missing_value", "name is required", row_no, "name"))

        instrument: InstrumentType | None = None
        try:
            instrument = InstrumentType(record["instrument_type"].lower())
        except ValueError:
            allowed = ", ".join(t.value for t in InstrumentType)
            row_issues.append(
                _issue(
                    "invalid_instrument_type", f"instrument_type must be one of: {allowed}", row_no, "instrument_type"
                )
            )

        market_value = _decimal(record["market_value"], row_no, "market_value", row_issues)
        if market_value is not None and market_value <= 0:
            row_issues.append(
                _issue("non_positive_value", "market_value must be greater than zero", row_no, "market_value")
            )
        reported_pct = _decimal(record["pct_of_nav"], row_no, "pct_of_nav", row_issues)
        if reported_pct is not None:
            reported_total += reported_pct

        isin = record["isin"].upper() or None
        if isin is not None:
            if not is_valid_isin(isin):
                row_issues.append(_issue("invalid_isin", f"'{isin}' is not a valid ISIN", row_no, "isin"))
            elif isin in first_row_for_isin:
                row_issues.append(
                    _issue(
                        "duplicate_isin",
                        f"ISIN {isin} already appears on row {first_row_for_isin[isin]}",
                        row_no,
                        "isin",
                    )
                )
            else:
                first_row_for_isin[isin] = row_no

        if row_issues:
            issues.extend(row_issues)
            continue
        assert instrument is not None and market_value is not None and reported_pct is not None
        holdings.append(
            Holding(
                row=row_no,
                isin=isin,
                name=name,
                instrument_type=instrument,
                market_value=market_value,
                reported_pct=reported_pct,
            )
        )

    if abs(reported_total - HUNDRED) > TOTAL_TOLERANCE_PP:
        issues.append(
            _issue(
                "bad_total",
                f"pct_of_nav sums to {reported_total}%, expected 100% ± {TOTAL_TOLERANCE_PP}",
                None,
                "pct_of_nav",
            )
        )
    if not issues:
        issues.extend(_row_consistency_issues(holdings))
    if issues:
        raise InputValidationError(issues)
    return HoldingsFile(holdings=tuple(holdings), sha256=sha256_text(csv_text))


def parse_candidates(csv_text: str) -> list[tuple[str | None, str]]:
    rows = _read_rows(csv_text, CANDIDATE_COLUMNS, label="Candidate file", max_bytes=MAX_HOLDINGS_BYTES)
    issues: list[ValidationIssue] = []
    candidates: list[tuple[str | None, str]] = []
    seen: set[str] = set()
    for row_no, record in rows:
        isin = record["isin"].upper() or None
        name = record["name"]
        if not name:
            issues.append(_issue("missing_value", "name is required", row_no, "name"))
            continue
        if isin is not None and not is_valid_isin(isin):
            issues.append(_issue("invalid_isin", f"'{isin}' is not a valid ISIN", row_no, "isin"))
            continue
        key = isin or name.casefold()
        if key in seen:
            issues.append(_issue("duplicate_candidate", f"'{isin or name}' is listed twice", row_no, "isin"))
            continue
        seen.add(key)
        candidates.append((isin, name))
    if issues:
        raise InputValidationError(issues)
    return candidates


def parse_trades(csv_text: str) -> list[Trade]:
    rows = _read_rows(csv_text, TRADE_COLUMNS, label="Trades file", max_bytes=MAX_HOLDINGS_BYTES)
    issues: list[ValidationIssue] = []
    trades: list[Trade] = []
    for row_no, record in rows:
        row_issues: list[ValidationIssue] = []
        isin = record["isin"].upper()
        if not is_valid_isin(isin):
            row_issues.append(
                _issue("invalid_isin", f"'{isin}' is not a valid ISIN (required for a trade)", row_no, "isin")
            )
        pct = _decimal(record["pct_of_nav"], row_no, "pct_of_nav", row_issues)
        if row_issues:
            issues.extend(row_issues)
            continue
        try:
            trades.append(
                Trade(action=record["action"].lower(), isin=isin, name=record["name"] or isin, pct_of_nav=pct)
            )
        except ValidationError as exc:
            issues.append(_issue("invalid_trade", exc.errors()[0]["msg"], row_no, None))
    if issues:
        raise InputValidationError(issues)
    return trades


def _read_rows(
    csv_text: str, required: Sequence[str], *, label: str, max_bytes: int
) -> list[tuple[int, dict[str, str]]]:
    if len(csv_text.encode("utf-8")) > max_bytes:
        raise InputValidationError([_issue("too_large", f"{label} exceeds {max_bytes} bytes", None, None)])
    text = csv_text.lstrip("﻿")
    if not text.strip():
        raise InputValidationError([_issue("empty_file", f"{label} is empty", None, None)])

    reader = csv.reader(io.StringIO(text))
    header = [column.strip().lower() for column in next(reader)]
    missing = [column for column in required if column not in header]
    if missing:
        raise InputValidationError(
            [_issue("missing_column", f"{label} is missing column(s): {', '.join(missing)}", 1, None)]
        )

    rows: list[tuple[int, dict[str, str]]] = []
    for row_no, values in enumerate(reader, start=2):
        if not any(value.strip() for value in values):
            continue
        if len(rows) >= MAX_ROWS:
            raise InputValidationError(
                [_issue("too_many_rows", f"{label} has more than {MAX_ROWS} rows", row_no, None)]
            )
        record = {column: (values[i].strip() if i < len(values) else "") for i, column in enumerate(header)}
        rows.append((row_no, record))
    if not rows:
        raise InputValidationError([_issue("empty_file", f"{label} has a header but no rows", None, None)])
    return rows


def _row_consistency_issues(holdings: Sequence[Holding]) -> list[ValidationIssue]:
    nav = sum((h.market_value for h in holdings), Decimal(0))
    issues = []
    for holding in holdings:
        implied = holding.market_value * HUNDRED / nav
        if abs(implied - holding.reported_pct) > ROW_TOLERANCE_PP:
            issues.append(
                _issue(
                    "row_pct_mismatch",
                    f"pct_of_nav {holding.reported_pct}% disagrees with market_value share {implied:.4f}%",
                    holding.row,
                    "pct_of_nav",
                )
            )
    return issues


def _decimal(raw: str, row: int, field: str, issues: list[ValidationIssue]) -> Decimal | None:
    if not raw:
        issues.append(_issue("missing_value", f"{field} is required", row, field))
        return None
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        issues.append(_issue("invalid_number", f"{field} '{raw}' is not a number", row, field))
        return None
    if not value.is_finite():
        issues.append(_issue("invalid_number", f"{field} '{raw}' is not a finite number", row, field))
        return None
    return value


def _issue(code: str, message: str, row: int | None, field: str | None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, row=row, field=field)
