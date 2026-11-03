# Data

**Everything in this folder is synthetic.** Company names, ISINs, market-cap ranks, SID extracts and holdings are made up. Nothing is client or personal data. Regenerate it with `python scripts/build_fixtures.py`.

| Path | What it is | Real or synthetic |
|---|---|---|
| `reference/security_master/2026H1.csv`, `2026H2.csv` | AMFI-style market-cap lists: `isin,name,rank,bucket,sector`. Rank 1–100 is large, 101–250 mid, 251+ small. | Synthetic (33 fictional companies, ISINs with valid check digits) |
| `reference/security_master/manifest.json` | Which list is in force from which date | Synthetic dates |
| `reference/sebi_rulebook.json` | SEBI equity category minimums and the 10% single-issuer limit, as three dated versions (`2017-10`, `2020-11`, `2026-02`) | Thresholds follow public summaries of SEBI's framework; rule texts are **paraphrases**. Verify against the circulars before any real use. |
| `cases/NN_*/` | Backtest cases: `case.json` (expectations), `holdings.csv`, `sid.md`, plus `candidates.csv` / `trades.csv` for P1 cases | Synthetic |

`Orchid Retail Ltd` is ranked 99 (large) in 2026H1 and 103 (mid) in 2026H2. It exists to show why the market-cap list must be pinned by date.

## Input formats

**Holdings CSV** (one row per position; net assets = sum of `market_value`):

```csv
isin,name,instrument_type,market_value,pct_of_nav
INESY0001011,Aravali Bank Ltd,equity,850.00,8.50
,TREPS / Cash & net receivables,cash,300.00,3.00
```

- `instrument_type` is one of `equity`, `cash`, `debt`, `other`.
- `isin` may be blank. The holding is then resolved by name, and any name match goes to human review.

**Candidates CSV** (P1 screening): `isin,name`. **Trades CSV** (P1 what-if): `action,isin,name,pct_of_nav` with `action` = `buy` or `sell`.

**SID**: a plain-text or Markdown extract containing the SEBI "type of scheme" description and any self-imposed limits.
