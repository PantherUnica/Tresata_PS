"""Builds the synthetic reference data and backtest cases under data/. Deterministic: safe to re-run.

Everything produced here is made up: company names, ISINs, ranks, SIDs and holdings. Rule thresholds
are a paraphrased encoding of public summaries of SEBI's categorisation framework.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from portfoliolens.ingest import isin_check_digit  # noqa: E402
from portfoliolens.reference import bucket_for_rank  # noqa: E402

DATA = ROOT / "data"
NAV = Decimal(10000)  # INR crore, so market_value = pct_of_nav * 100
AS_OF = "2026-09-30"


def synthetic_isin(body9: str) -> str:
    body = f"IN{body9}"
    return f"{body}{isin_check_digit(body)}"


# key, name, sector, rank in the 2026H1 list, rank in the 2026H2 list (all fictional)
SECURITIES: list[tuple[str, str, str, int, int]] = [
    ("ARV", "Aravali Bank Ltd", "Banking", 3, 3),
    ("BOD", "Bodhi Software Ltd", "IT", 5, 6),
    ("CHO", "Chola Energy Ltd", "Energy", 8, 7),
    ("DEC", "Deccan Consumer Ltd", "FMCG", 12, 12),
    ("EAS", "Eastern Pharma Ltd", "Pharma", 18, 17),
    ("FAT", "Fateh Motors Ltd", "Auto", 22, 24),
    ("GAN", "Ganga Steel Ltd", "Metals", 30, 31),
    ("HIM", "Himalaya Telecom Ltd", "Telecom", 35, 33),
    ("IND", "Indus Infra Ltd", "Infra", 41, 44),
    ("JAI", "Jaipur Finance Ltd", "Banking", 47, 45),
    ("KAV", "Kaveri Chemicals Ltd", "Chemicals", 55, 58),
    ("LOT", "Lotus Digital Ltd", "IT", 63, 61),
    ("MAL", "Malabar Bank Ltd", "Banking", 72, 70),
    ("NAR", "Narmada Cement Ltd", "Infra", 84, 86),
    ("SPG", "Surya Power Grid Ltd", "Energy", 90, 92),
    ("ORC", "Orchid Retail Ltd", "FMCG", 99, 103),  # large in H1, mid in H2: shows why lists are dated
    ("PEA", "Peacock Logistics Ltd", "Infra", 112, 110),
    ("QUI", "Quill Media Ltd", "Telecom", 130, 128),
    ("RAN", "Rann Textiles Ltd", "FMCG", 145, 150),
    ("SAH", "Sahyadri Bank Ltd", "Banking", 160, 158),
    ("TAP", "Tapti Auto Parts Ltd", "Auto", 175, 172),
    ("SPF", "Surya Power Finance Ltd", "Banking", 188, 190),
    ("UDA", "Udaan Aviation Ltd", "Infra", 205, 207),
    ("VIN", "Vindhya Pharma Ltd", "Pharma", 220, 215),
    ("WES", "Western Software Ltd", "IT", 240, 236),
    ("XEN", "Xenon Labs Ltd", "Pharma", 270, 265),
    ("YAM", "Yamuna Foods Ltd", "FMCG", 300, 310),
    ("ZEN", "Zenith Alloys Ltd", "Metals", 340, 335),
    ("AMB", "Amber Fintech Ltd", "Banking", 390, 400),
    ("BAN", "Banyan Solar Ltd", "Energy", 420, 415),
    ("COB", "Cobalt Cables Ltd", "Infra", 480, 470),
    ("DHR", "Dhruv Data Ltd", "IT", 520, 530),
    ("EMB", "Ember Chemicals Ltd", "Chemicals", 600, 590),
]
ISIN = {key: synthetic_isin(f"ESY{n:04d}01") for n, (key, *_rest) in enumerate(SECURITIES, start=1)}
NAME = {key: name for key, name, *_rest in SECURITIES}
NOT_IN_MASTER = (synthetic_isin("ESY999901"), "Nova Quantum Ltd")
DEBT = {
    "GS30": (synthetic_isin("GSY000101"), "7.10% GOI 2030 (synthetic)"),
    "GS33": (synthetic_isin("GSY000201"), "7.25% GOI 2033 (synthetic)"),
}
CASH_NAME = "TREPS / Cash & net receivables"
UNRESOLVED_NAME = "Surya Power"

LARGE = "SEBI-CAT-LARGE-CAP-01"
ISSUER = "SEBI-MFREG-ISSUER-10"


# --- reference data ---------------------------------------------------------------------


def write_security_master() -> None:
    folder = DATA / "reference" / "security_master"
    folder.mkdir(parents=True, exist_ok=True)
    versions = [("2026H1", "2026-01-01", 3), ("2026H2", "2026-07-01", 4)]
    for version, _, rank_index in versions:
        rows = sorted(SECURITIES, key=lambda s: s[rank_index])
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["isin", "name", "rank", "bucket", "sector"])
        for security in rows:
            key, name, sector = security[0], security[1], security[2]
            rank = security[rank_index]
            writer.writerow([ISIN[key], name, rank, bucket_for_rank(rank).value, sector])
        (folder / f"{version}.csv").write_text(out.getvalue(), encoding="utf-8")
    manifest = {
        "note": "Synthetic AMFI-style market-cap lists (rank 1-100 large, 101-250 mid, 251+ small). Fictional companies.",
        "versions": [
            {"version": version, "effective_from": effective, "file": f"{version}.csv", "source": "synthetic"}
            for version, effective, _ in versions
        ],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def rule(
    rule_id: str, applies_to: list[str], metric: str, comparator: str, threshold: str, text: str, source: str
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "applies_to": applies_to,
        "metric": metric,
        "comparator": comparator,
        "threshold": threshold,
        "text": text,
        "text_kind": "paraphrase",
        "source_ref": source,
    }


C2017 = "SEBI circular SEBI/HO/IMD/DF3/CIR/P/2017/114 (06-Oct-2017), categorisation and rationalisation of schemes"
C2020 = "SEBI circulars of Sep-2020 (multi cap 25/25/25) and Nov-2020 (flexi cap category), encoded as one version"
C2026 = "SEBI categorisation circular, Feb-2026 (per PRD v0.3; category minimums carried forward) - reference to verify"
C_REGS = "SEBI (Mutual Funds) Regulations, 1996, Seventh Schedule - single-issuer equity limit"


def category_rules(source: str) -> list[dict[str, Any]]:
    return [
        rule(
            LARGE,
            ["large_cap"],
            "large_cap_pct",
            "min",
            "80",
            "At least 80% of total assets in large cap equity.",
            source,
        ),
        rule(
            "SEBI-CAT-LARGE-MID-01",
            ["large_mid_cap"],
            "large_cap_pct",
            "min",
            "35",
            "At least 35% in large cap equity.",
            source,
        ),
        rule(
            "SEBI-CAT-LARGE-MID-02",
            ["large_mid_cap"],
            "mid_cap_pct",
            "min",
            "35",
            "At least 35% in mid cap equity.",
            source,
        ),
        rule("SEBI-CAT-MID-CAP-01", ["mid_cap"], "mid_cap_pct", "min", "65", "At least 65% in mid cap equity.", source),
        rule(
            "SEBI-CAT-SMALL-CAP-01",
            ["small_cap"],
            "small_cap_pct",
            "min",
            "65",
            "At least 65% in small cap equity.",
            source,
        ),
        rule(
            "SEBI-CAT-ELSS-01", ["elss"], "equity_pct", "min", "80", "At least 80% in equity (3-year lock-in).", source
        ),
        rule("SEBI-CAT-FOCUSED-01", ["focused"], "equity_pct", "min", "65", "At least 65% in equity.", source),
        rule("SEBI-CAT-FOCUSED-02", ["focused"], "stock_count", "max", "30", "A maximum of 30 stocks.", source),
        rule(
            "SEBI-CAT-SECTORAL-01",
            ["sectoral_thematic"],
            "sector_pct",
            "min",
            "80",
            "At least 80% in the stated sector or theme.",
            source,
        ),
        rule(
            "SEBI-CAT-VALUE-01", ["value"], "equity_pct", "min", "65", "At least 65% in equity, value strategy.", source
        ),
        rule(
            "SEBI-CAT-CONTRA-01",
            ["contra"],
            "equity_pct",
            "min",
            "65",
            "At least 65% in equity, contrarian strategy.",
            source,
        ),
        rule(
            "SEBI-CAT-DIVIDEND-YIELD-01",
            ["dividend_yield"],
            "equity_pct",
            "min",
            "65",
            "At least 65% in equity, predominantly dividend-yielding (encoded as the equity floor only).",
            source,
        ),
    ]


def multi_cap_rules(source: str) -> list[dict[str, Any]]:
    return [
        rule("SEBI-CAT-MULTI-CAP-01", ["multi_cap"], "equity_pct", "min", "75", "At least 75% in equity.", source),
        rule(
            "SEBI-CAT-MULTI-CAP-02",
            ["multi_cap"],
            "large_cap_pct",
            "min",
            "25",
            "At least 25% in large cap equity.",
            source,
        ),
        rule(
            "SEBI-CAT-MULTI-CAP-03",
            ["multi_cap"],
            "mid_cap_pct",
            "min",
            "25",
            "At least 25% in mid cap equity.",
            source,
        ),
        rule(
            "SEBI-CAT-MULTI-CAP-04",
            ["multi_cap"],
            "small_cap_pct",
            "min",
            "25",
            "At least 25% in small cap equity.",
            source,
        ),
    ]


def write_rulebook() -> None:
    issuer = rule(
        ISSUER,
        ["*"],
        "single_issuer_pct",
        "max",
        "10",
        "No more than 10% of NAV in the equity of any single company (index and some sectoral carve-outs are not auto-applied).",
        C_REGS,
    )

    def flexi(source: str) -> dict[str, Any]:
        return rule(
            "SEBI-CAT-FLEXI-CAP-01",
            ["flexi_cap"],
            "equity_pct",
            "min",
            "65",
            "At least 65% in equity; no cap-wise minimums.",
            source,
        )

    versions = [
        {
            "version": "2017-10",
            "effective_from": "2017-10-06",
            "circulars": [C2017],
            "note": "Original categorisation. Multi cap: 65% equity, no cap-wise minimums. No flexi cap category.",
            "rules": [
                *category_rules(C2017),
                rule(
                    "SEBI-CAT-MULTI-CAP-01", ["multi_cap"], "equity_pct", "min", "65", "At least 65% in equity.", C2017
                ),
                issuer,
            ],
        },
        {
            "version": "2020-11",
            "effective_from": "2020-11-06",
            "circulars": [C2017, C2020],
            "note": "Multi cap gains 25% minimums in each cap; flexi cap introduced.",
            "rules": [*category_rules(C2017), *multi_cap_rules(C2020), flexi(C2020), issuer],
        },
        {
            "version": "2026-02",
            "effective_from": "2026-02-26",
            "circulars": [C2017, C2020, C2026],
            "note": "New version added, not overwritten: runs dated before 2026-02-26 still reproduce against 2020-11.",
            "rules": [*category_rules(C2026), *multi_cap_rules(C2026), flexi(C2026), issuer],
        },
    ]
    document = {
        "note": (
            "Prototype encoding of SEBI equity scheme rules. Texts are paraphrases, not quotations. Verify every "
            "threshold and effective date against the official circulars before any real use."
        ),
        "versions": versions,
    }
    path = DATA / "reference" / "sebi_rulebook.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


# --- case builders ----------------------------------------------------------------------

STRATEGY = (
    "The fund manager follows a bottom-up approach to stock selection and holds cash and money market "
    "instruments for liquidity."
)
LARGE_TYPE = "An open ended equity scheme predominantly investing in large cap stocks."
MULTI_TYPE = "An open ended equity scheme investing across large cap, mid cap, small cap stocks."
FLEXI_TYPE = "An open ended dynamic equity scheme investing across large cap, mid cap, small cap stocks."
LARGE_ALLOCATION = [
    ("Equity and equity related instruments of large cap companies", 80, 100),
    ("Other equity and equity related instruments", 0, 20),
    ("Debt and money market instruments", 0, 20),
]
MULTI_ALLOCATION = [
    ("Large cap companies", 25, 50),
    ("Mid cap companies", 25, 50),
    ("Small cap companies", 25, 50),
    ("Debt and money market instruments", 0, 25),
]
FLEXI_ALLOCATION = [("Equity and equity related instruments", 65, 100), ("Debt and money market instruments", 0, 35)]


def sid(name: str, scheme_type: str, benchmark: str, allocation: list[tuple[str, int, int]], strategy: str) -> str:
    rows = "\n".join(f"| {instrument} | {low} to {high} |" for instrument, low, high in allocation)
    return f"""# Scheme Information Document (extract)

> SYNTHETIC DOCUMENT created for testing PortfolioLens. It is not an offer document and describes no real scheme.

Name of the Scheme: {name}
Type of scheme: {scheme_type}
Benchmark: {benchmark}

## Investment objective

The investment objective of the Scheme is to generate long-term capital appreciation. There is no assurance that the investment objective of the Scheme will be achieved.

## Asset allocation

| Instrument | Indicative allocation (% of net assets) |
|---|---|
{rows}

## Investment strategy

{strategy}
"""


def holdings_csv(rows: list[tuple[str, str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["isin", "name", "instrument_type", "market_value", "pct_of_nav"])
    total = Decimal(0)
    for key, raw_pct in rows:
        pct = Decimal(raw_pct)
        total += pct
        if key == "CASH":
            isin, name, kind = "", CASH_NAME, "cash"
        elif key in DEBT:
            (isin, name), kind = DEBT[key], "debt"
        elif key == "UNRESOLVED":
            isin, name, kind = "", UNRESOLVED_NAME, "equity"
        else:
            isin, name, kind = ISIN[key], NAME[key], "equity"
        writer.writerow([isin, name, kind, f"{pct * NAV / 100:.2f}", f"{pct:.2f}"])
    if total != 100:
        raise ValueError(f"holdings sum to {total}%, not 100%")
    return out.getvalue()


def pct_rows(spec: str) -> list[tuple[str, str]]:
    """'ARV 8.5, BOD 8' -> [('ARV', '8.5'), ('BOD', '8')]"""
    return [tuple(item.split()) for item in spec.split(",")]  # type: ignore[misc]


def large_truth(name: str, limits: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "scheme_name": name,
        "category": "large_cap",
        "benchmark": "NIFTY 100 TRI",
        "self_imposed_limits": limits or [],
    }


def finding(status: str, margin: str | None = None, binding: bool | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if margin is not None:
        result["margin"] = margin
    if binding is not None:
        result["binding"] = binding
    return result


CASE1_HOLDINGS = "ARV 8.5, BOD 8.5, CHO 8.5, DEC 7.5, EAS 7, FAT 6.5, GAN 6, HIM 6, IND 5.5, JAI 5.5, KAV 5, LOT 5, MAL 4.5, NAR 4, PEA 4, QUI 3, XEN 2, CASH 3"
CASE6_LIMIT_QUOTE = "the Scheme will invest at least 85% of its net assets in equity and equity related instruments of large cap companies"
CASE13_LIMIT_QUOTE = "the fund manager will not allow exposure to any single company to exceed eight and a half per cent of the Scheme's net assets"


def cases() -> list[dict[str, Any]]:
    def large_sid(name: str, strategy: str = STRATEGY) -> str:
        return sid(name, LARGE_TYPE, "NIFTY 100 TRI", LARGE_ALLOCATION, strategy)

    return [
        {
            "dir": "01_large_cap_compliant",
            "spec": {
                "id": "01",
                "title": "Large-cap scheme at 88% large cap",
                "kind": "compliance",
                "as_of": AS_OF,
                "notes": "Happy path: every rule passes with clear margin.",
                "ground_truth_mandate": large_truth("Synthetic Bluechip Fund"),
                "expect": {
                    "run_status": "COMPLIANT",
                    "review_kinds": [],
                    "findings": {LARGE: finding("PASS", "8.00"), ISSUER: finding("PASS", "1.50")},
                },
            },
            "holdings": CASE1_HOLDINGS,
            "sid": large_sid("Synthetic Bluechip Fund"),
        },
        {
            "dir": "02_large_cap_breach",
            "spec": {
                "id": "02",
                "title": "Large-cap scheme at 78% large cap",
                "kind": "compliance",
                "as_of": AS_OF,
                "notes": "Breach of the 80% category minimum by 2 pp.",
                "ground_truth_mandate": large_truth("Synthetic Frontline Fund"),
                "expect": {"run_status": "BREACH", "review_kinds": [], "findings": {LARGE: finding("BREACH", "-2.00")}},
            },
            "holdings": "ARV 8.5, BOD 8.5, CHO 8.5, DEC 7.5, EAS 7, FAT 6.5, GAN 6, HIM 6, IND 5.5, JAI 5.5, KAV 5, LOT 3.5, PEA 5, QUI 4, RAN 3, XEN 3, YAM 2, CASH 5",
            "sid": large_sid("Synthetic Frontline Fund"),
        },
        {
            "dir": "03_multi_cap_small_leg",
            "spec": {
                "id": "03",
                "title": "Multi-cap with only 18% small cap",
                "kind": "compliance",
                "as_of": AS_OF,
                "notes": "Breach of the 25%-each rule on the small-cap leg only.",
                "ground_truth_mandate": {
                    "scheme_name": "Synthetic All Cap Fund",
                    "category": "multi_cap",
                    "benchmark": "NIFTY 500 Multicap 50:25:25 TRI",
                },
                "expect": {
                    "run_status": "BREACH",
                    "review_kinds": [],
                    "findings": {
                        "SEBI-CAT-MULTI-CAP-01": finding("PASS", "18.00"),
                        "SEBI-CAT-MULTI-CAP-02": finding("PASS", "5.00"),
                        "SEBI-CAT-MULTI-CAP-03": finding("PASS", "20.00"),
                        "SEBI-CAT-MULTI-CAP-04": finding("BREACH", "-7.00"),
                    },
                },
            },
            "holdings": "ARV 6, BOD 6, CHO 6, DEC 6, EAS 6, PEA 7, QUI 7, RAN 7, SAH 6, TAP 6, SPF 6, UDA 6, XEN 5, YAM 5, ZEN 4, AMB 4, CASH 7",
            "sid": sid(
                "Synthetic All Cap Fund", MULTI_TYPE, "NIFTY 500 Multicap 50:25:25 TRI", MULTI_ALLOCATION, STRATEGY
            ),
        },
        {
            "dir": "04_flexi_cap_equity_floor",
            "spec": {
                "id": "04",
                "title": "Flexi-cap at 70% equity",
                "kind": "compliance",
                "as_of": AS_OF,
                "notes": "Only the 65% equity floor applies; no cap-wise minimums.",
                "ground_truth_mandate": {
                    "scheme_name": "Synthetic Dynamic Equity Fund",
                    "category": "flexi_cap",
                    "benchmark": "NIFTY 500 TRI",
                },
                "expect": {
                    "run_status": "COMPLIANT",
                    "review_kinds": [],
                    "findings": {"SEBI-CAT-FLEXI-CAP-01": finding("PASS", "5.00")},
                },
            },
            "holdings": "ARV 8, BOD 8, CHO 8, DEC 8, EAS 8, PEA 7, QUI 7, RAN 6, XEN 5, YAM 5, GS30 15, GS33 10, CASH 5",
            "sid": sid("Synthetic Dynamic Equity Fund", FLEXI_TYPE, "NIFTY 500 TRI", FLEXI_ALLOCATION, STRATEGY),
        },
        {
            "dir": "05_single_issuer_breach",
            "spec": {
                "id": "05",
                "title": "Single stock at 11% of net assets",
                "kind": "compliance",
                "as_of": AS_OF,
                "notes": "Concentration breach of the 10% single-issuer limit.",
                "ground_truth_mandate": large_truth("Synthetic Leaders Fund"),
                "expect": {
                    "run_status": "BREACH",
                    "review_kinds": [],
                    "findings": {ISSUER: finding("BREACH", "-1.00"), LARGE: finding("PASS", "2.00")},
                },
            },
            "holdings": "ARV 11, BOD 8, CHO 8, DEC 7, EAS 7, FAT 7, GAN 6, HIM 6, IND 6, JAI 6, KAV 5, LOT 5, PEA 6, QUI 5, XEN 3, CASH 4",
            "sid": large_sid("Synthetic Leaders Fund"),
        },
        {
            "dir": "06_self_imposed_floor",
            "spec": {
                "id": "06",
                "title": "SID states a self-imposed 85% floor",
                "kind": "compliance",
                "as_of": AS_OF,
                "notes": "At 83% the SEBI rule passes but the stricter SID limit binds and is breached.",
                "ground_truth_mandate": large_truth(
                    "Synthetic Core Equity Fund",
                    [{"metric": "large_cap_pct", "comparator": "min", "threshold": "85", "quote": CASE6_LIMIT_QUOTE}],
                ),
                "expect": {
                    "run_status": "BREACH",
                    "review_kinds": [],
                    "findings": {
                        LARGE: finding("PASS", "3.00", binding=False),
                        "SID-LARGE_CAP_PCT-MIN": finding("BREACH", "-2.00", binding=True),
                    },
                },
            },
            "holdings": "ARV 8.5, BOD 8.5, CHO 8.5, DEC 7.5, EAS 7, FAT 6.5, GAN 6, HIM 6, IND 5.5, JAI 5.5, KAV 5, LOT 4, MAL 4.5, PEA 5, QUI 4, XEN 3, CASH 5",
            "sid": large_sid(
                "Synthetic Core Equity Fund",
                STRATEGY + " As an internal investment restriction, " + CASE6_LIMIT_QUOTE + ".",
            ),
        },
        {
            "dir": "07_ambiguous_holding",
            "spec": {
                "id": "07",
                "title": "Holding with no ISIN and an ambiguous name",
                "kind": "compliance",
                "as_of": AS_OF,
                "notes": "'Surya Power' could be Surya Power Grid (large) or Surya Power Finance (mid). It is never silently bucketed, so the large-cap verdict is indeterminate (78.5% to 81.5%).",
                "ground_truth_mandate": large_truth("Synthetic Prime Equity Fund"),
                "expect": {
                    "run_status": "NEEDS_REVIEW",
                    "review_kinds": ["unresolved_holding", "indeterminate"],
                    "findings": {LARGE: finding("INDETERMINATE", "-1.50")},
                },
            },
            "holdings": "ARV 8.5, BOD 8.5, CHO 8.5, DEC 7.5, EAS 7, FAT 6.5, GAN 6, HIM 6, IND 5.5, JAI 5.5, KAV 5, LOT 4, UNRESOLVED 3, PEA 5, QUI 4, XEN 4.5, CASH 5",
            "sid": large_sid("Synthetic Prime Equity Fund"),
        },
        {
            "dir": "08_near_threshold",
            "spec": {
                "id": "08",
                "title": "Scheme at exactly 80.1% large cap",
                "kind": "compliance",
                "as_of": AS_OF,
                "notes": "Passes by 0.1 pp, so it is forced to human review.",
                "ground_truth_mandate": large_truth("Synthetic Steady Equity Fund"),
                "expect": {
                    "run_status": "NEEDS_REVIEW",
                    "review_kinds": ["near_threshold"],
                    "findings": {LARGE: finding("PASS", "0.10")},
                },
            },
            "holdings": "ARV 8.5, BOD 8.5, CHO 8.5, DEC 7.5, EAS 7, FAT 6.5, GAN 6, HIM 6, IND 5.5, JAI 5.5, KAV 5, LOT 5.6, PEA 6, QUI 5, XEN 4, CASH 4.9",
            "sid": large_sid("Synthetic Steady Equity Fund"),
        },
        {
            "dir": "09_invalid_holdings",
            "spec": {
                "id": "09",
                "title": "Missing values, duplicate ISIN, bad ISIN and bad sums",
                "kind": "validation",
                "as_of": AS_OF,
                "notes": "Rejected with row-level errors before any verdict.",
                "expect": {"validation_codes": ["missing_value", "duplicate_isin", "invalid_isin", "bad_total"]},
            },
            "raw_holdings": (
                "isin,name,instrument_type,market_value,pct_of_nav\n"
                f"{ISIN['ARV']},Aravali Bank Ltd,equity,850.00,8.50\n"
                f"{ISIN['BOD']},Bodhi Software Ltd,equity,,8.50\n"
                f"{ISIN['ARV']},Aravali Bank Ltd (second line),equity,500.00,5.00\n"
                "INE12345,Broken Isin Ltd,equity,100.00,1.00\n"
                f"{ISIN['CHO']},Chola Energy Ltd,equity,8000.00,80.00\n"
            ),
        },
        {
            "dir": "10_forecast_request_declined",
            "spec": {
                "id": "10",
                "title": '"Predict next month\'s drift / recommend a trade"',
                "kind": "question",
                "as_of": AS_OF,
                "notes": "The scope guard declines before any model is called; current compliance is still reported.",
                "ground_truth_mandate": large_truth("Synthetic Bluechip Fund"),
                "question": "Predict next month's large-cap drift and recommend a trade to fix it.",
                "expect": {"run_status": "COMPLIANT", "declined": True},
            },
            "holdings": CASE1_HOLDINGS,
            "sid": large_sid("Synthetic Bluechip Fund"),
        },
        {
            "dir": "11_screen_large_cap_pool",
            "spec": {
                "id": "11",
                "title": "Screen a mixed-cap pool for a large-cap fund",
                "kind": "screen",
                "as_of": AS_OF,
                "notes": "Buy 3% of NAV, funded by a pro-rata trim. Mid/small names would pull large cap below 80%; Aravali would exceed 10%; Orchid became mid cap in the 2026H2 list; Nova is unclassifiable.",
                "ground_truth_mandate": large_truth("Synthetic Select Equity Fund"),
                "candidates": "candidates.csv",
                "weight_pct": "3",
                "funding": "pro_rata",
                "expect": {
                    "run_status": "COMPLIANT",
                    "findings": {LARGE: finding("PASS", "2.00")},
                    "eligible": [ISIN["NAR"], ISIN["MAL"]],
                    "blocking": {
                        ISIN["ARV"]: [ISSUER],
                        ISIN["SAH"]: [LARGE],
                        ISIN["ZEN"]: [LARGE],
                        ISIN["ORC"]: [LARGE],
                        NOT_IN_MASTER[0]: ["UNCLASSIFIED"],
                    },
                },
            },
            "holdings": "ARV 8, BOD 8, CHO 8, DEC 7.5, EAS 7, FAT 6.5, GAN 6, HIM 6, IND 5.5, JAI 5.5, KAV 5, LOT 5, MAL 4, PEA 6, QUI 6, XEN 2, CASH 4",
            "sid": large_sid("Synthetic Select Equity Fund"),
            "extra_files": {
                "candidates.csv": "isin,name\n"
                + "".join(f"{ISIN[k]},{NAME[k]}\n" for k in ("NAR", "MAL", "ARV", "SAH", "ZEN", "ORC"))
                + f"{NOT_IN_MASTER[0]},{NOT_IN_MASTER[1]}\n",
            },
        },
        {
            "dir": "12_whatif_restores_compliance",
            "spec": {
                "id": "12",
                "title": "What-if: buy large cap to move 78.3% to 80.1%",
                "kind": "whatif",
                "as_of": AS_OF,
                "notes": "Buy 1.8% of NAV of Narmada Cement from cash. Nothing is executed and nothing is forecast.",
                "ground_truth_mandate": large_truth("Synthetic Recovery Equity Fund"),
                "trades": "trades.csv",
                "expect": {
                    "run_status": "BREACH",
                    "findings": {LARGE: finding("BREACH", "-1.70")},
                    "whatif_before": {LARGE: finding("BREACH", "-1.70")},
                    "whatif_after": {LARGE: finding("PASS", "0.10")},
                    "new_breaches": [],
                    "resolved_breaches": [LARGE],
                },
            },
            "holdings": "ARV 8.5, BOD 8.5, CHO 8.5, DEC 7.5, EAS 7, FAT 6.5, GAN 6, HIM 6, IND 5.5, JAI 5.5, KAV 5, LOT 3.8, PEA 5, QUI 4, RAN 3, XEN 3, YAM 1.7, CASH 5",
            "sid": large_sid("Synthetic Recovery Equity Fund"),
            "extra_files": {"trades.csv": f"action,isin,name,pct_of_nav\nbuy,{ISIN['NAR']},{NAME['NAR']},1.8\n"},
        },
        {
            "dir": "13_limit_in_words",
            "spec": {
                "id": "13",
                "title": "Self-imposed issuer cap written in words",
                "kind": "compliance",
                "as_of": AS_OF,
                "notes": "Stress case beyond the PRD's 12: the SID caps any single company at 'eight and a half per cent'. A pattern matcher cannot read it; this is the language-understanding gap the extraction agent exists for, and Gate 1 is the backstop.",
                "ground_truth_mandate": large_truth(
                    "Synthetic Disciplined Equity Fund",
                    [
                        {
                            "metric": "single_issuer_pct",
                            "comparator": "max",
                            "threshold": "8.5",
                            "quote": CASE13_LIMIT_QUOTE,
                        }
                    ],
                ),
                "expect": {
                    "run_status": "BREACH",
                    "findings": {
                        "SID-SINGLE_ISSUER_PCT-MAX": finding("BREACH", "-0.50", binding=True),
                        ISSUER: finding("PASS", "1.00", binding=False),
                        LARGE: finding("PASS", "3.50"),
                    },
                },
            },
            "holdings": "ARV 9, BOD 8, CHO 8, DEC 7.5, EAS 7, FAT 6.5, GAN 6, HIM 6, IND 5.5, JAI 5.5, KAV 5, LOT 5, MAL 4.5, PEA 6, QUI 5, XEN 2, CASH 3.5",
            "sid": large_sid(
                "Synthetic Disciplined Equity Fund",
                STRATEGY + " As an internal risk control, " + CASE13_LIMIT_QUOTE + ".",
            ),
        },
    ]


def write_cases() -> None:
    root = DATA / "cases"
    root.mkdir(parents=True, exist_ok=True)
    for case in cases():
        folder = root / case["dir"]
        folder.mkdir(parents=True, exist_ok=True)
        holdings = case["raw_holdings"] if "raw_holdings" in case else holdings_csv(pct_rows(case["holdings"]))
        (folder / "holdings.csv").write_text(holdings, encoding="utf-8")
        if "sid" in case:
            (folder / "sid.md").write_text(case["sid"], encoding="utf-8")
        for name, content in case.get("extra_files", {}).items():
            (folder / name).write_text(content, encoding="utf-8")
        spec = {"synthetic": True, **case["spec"]}
        (folder / "case.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    write_security_master()
    write_rulebook()
    write_cases()
    print(f"Fixtures written under {DATA}")


if __name__ == "__main__":
    main()
