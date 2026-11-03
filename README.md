# PortfolioLens AI

PortfolioLens AI is a prototype for checking whether an Indian equity mutual fund's holdings comply with its stated Scheme Information Document (SID) mandate and the applicable SEBI equity-category rules. It also demonstrates constraint-aware candidate screening and deterministic what-if trade simulations.

> **Decision-support prototype only.** It is not compliance assurance, legal advice, investment advice, a recommendation, or a trading system. The bundled holdings, securities, SIDs, and market-cap reference lists are synthetic. Do not use the bundled rules or data to make a real compliance decision.

## Problem statement

Mutual-fund compliance and risk teams repeatedly compare month-end holdings against mandate language, regulatory thresholds, market-cap classifications, and issuer limits. The data comes from different sources and changes over time. Manually repeating the process in spreadsheets takes time and makes it harder to reproduce how a finding was reached.

PortfolioLens is intended to help a compliance reviewer:

- Check holdings against a scheme's mandate and the applicable equity-category constraints.
- Identify breaches, near-threshold results, and holdings that cannot safely be classified.
- Trace results to holdings, cited SID text, and dated rule/reference versions.
- Screen candidate securities for mandate eligibility.
- Simulate proposed trades without placing or recommending them.

Its central rule is: **the model reads and explains; deterministic code calculates and evaluates; a named human confirms and signs off.** Model output is treated as untrusted and checked before it is used. It must not calculate percentages, determine compliance, predict returns, rank securities by expected performance, execute trades, or submit regulatory filings.

## Intended users

- AMC compliance and risk analysts checking portfolios and documenting exceptions.
- Fund managers checking eligibility and testing proposed changes before acting outside the tool.
- Trustees, auditors, and other reviewers inspecting a run's evidence and sign-off.
- FoF/distributor due-diligence teams screening scheme mandate drift (future validation needed).

## Scope and limitations

### Included in this prototype

- Holdings CSV and plain-text/Markdown SID input, with validation.
- Mandate extraction proposal with source quotations and a required human confirmation step.
- Versioned, date-selected rulebook and security-master lookup.
- Deterministic allocation, concentration, eligibility, and what-if calculations.
- Findings, review items, a cited report, grounded Q&A, and attributable sign-off.
- A browser UI, CLI, HTTP API, synthetic examples, and a backtest harness.

### Explicitly out of scope

- Return forecasts, "best buy" rankings, autonomous rebalancing, and trade execution.
- Debt, hybrid, or solution-oriented scheme rules; liquidity, credit, derivatives, and stress tests.
- Cross-scheme overlap checks, live custodian/RTA/market-data connections, and regulatory filings.

### Not ready for real fund use

- The included company names, ISINs, holdings, SIDs, and cap lists are fictional.
- The rulebook contains paraphrased public-rule summaries. Verify every rule and effective date against authoritative SEBI sources before any real use.
- The bundled security master is not an official AMFI list.
- OpenAI/model-backed behavior has not been validated against the full test corpus using a live model. The live model's output must be reviewed at Gate 1 and every flagged result reviewed by a qualified person.
- Local run records are JSON files; this is a local prototype, not a hosted, multi-user or production audit platform.

## Architecture and workflow

The backend is a Python/FastAPI modular monolith. The browser app is static HTML, CSS, and JavaScript served by FastAPI (no frontend build step). Pydantic validates structured data; the engine uses decimal arithmetic. Run artifacts are written as JSON and Markdown files. LangGraph, from the LangChain ecosystem, runs the fixed post-confirmation compliance workflow.

```text
Holdings CSV + SID
        |
Validate inputs -> propose mandate with source citations
        |
Human Gate 1: review and confirm/correct mandate
        |
Select dated rulebook -> LangChain retrieval (optional GPT rerank) + dated security master
        |
Validate full applicable rule set -> classify holdings
        |
Deterministic reconciliation -> report and review items
        |
Human Gate 2: acknowledge review items
        |
Human Gate 3: named sign-off
        |
Grounded Q&A | candidate screening | what-if scenarios
```

The LangChain retriever works only over the date-selected local rulebook and returns every applicable category and global rule. When a model is configured, it may reorder those rule IDs for relevance; the application rejects unknown, missing, or duplicate IDs and falls back to the retriever's order. It cannot change rule text or thresholds. Deterministic code remains the sole authority for numeric calculations and verdicts. Retrieval method, version, effective date, SHA-256, query, and rule IDs are stored in each run. This small prototype uses lexical retrieval rather than embeddings or a vector database; the rulebook paraphrases still need verification against official SEBI sources. The API handles Gate 1 confirmation and Gates 2–3 sign-off outside the graph so human approval remains explicit and persisted.

See [Docs/Architecture.md](Docs/Architecture.md) for the architecture rationale and [PortfolioLens_AI_PRD_v0.3.md](PortfolioLens_AI_PRD_v0.3.md) for the product requirements.

## Requirements

- Windows PowerShell instructions are below; Python 3.11 or newer is required.
- Optional: an OpenAI Platform API key with API access to `gpt-4o-mini` to run the model-backed agents.
- No API key is required for offline mode, tests, or the synthetic backtest.

**Model configured for this project:** OpenAI GPT-4o mini, API model ID `gpt-4o-mini`. Tests in this workspace used mocked model responses; the offline backtest used deterministic stand-ins. Therefore, GPT-4o mini has **not** been represented as live-tested.

## Run locally (Windows PowerShell)

From the repository root:

```powershell
cd C:\GitHub_Projects\Tresata_PS\prototype

# If .venv is not already present:
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Run the test suite and offline backtest:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m portfoliolens backtest
```

Start the browser app:

```powershell
.\.venv\Scripts\python.exe -m portfoliolens serve
```

Open <http://127.0.0.1:8000/>. The API's interactive documentation is at <http://127.0.0.1:8000/docs>. Stop the server with **Ctrl+C**.

## Configure the live OpenAI model

The project now supports a direct OpenAI Platform key (not an OpenRouter key). Keep the key local:

1. In `C:\GitHub_Projects\Tresata_PS\prototype`, copy the example configuration:

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

2. Set these values in `prototype\.env`:

   ```dotenv
   PL_LLM_MODE=openai
   OPENAI_API_KEY=your-OpenAI-Platform-key
   OPENAI_MODEL=gpt-4o-mini
   ```

3. Save `.env`, then start the server or run a command as above. The app should identify the model mode as `openai:gpt-4o-mini`.

The API key is sent to OpenAI from the Python backend; it is not sent by the browser UI. Never paste the key into chat, source code, screenshots, or a committed file. `prototype\.env` is ignored by Git. API access and billing are controlled by your OpenAI account.

For an initial live-model check, use a single, small synthetic example in **New check**, review the proposed mandate at Gate 1, and continue through the workflow. Avoid starting with the full live-model backtest: it makes repeated model requests. The regular backtest and tests remain offline unless `PL_LLM_MODE=openai` is set.

OpenRouter is also supported as an alternative:

```dotenv
PL_LLM_MODE=openrouter
OPENROUTER_API_KEY=your-OpenRouter-key
OPENROUTER_MODEL=openai/gpt-4o-mini
```

An OpenAI Platform key and an OpenRouter key are issued by different services and are not interchangeable. Do not set both modes at once.

## Try the complete workflow

### Browser UI

1. Open **New check** and select a synthetic example such as **07 — Holding with no ISIN and an ambiguous name**.
2. Click **Extract mandate**. Inspect the proposal and its quoted SID evidence; correct it if needed, then confirm at **Gate 1**.
3. Review findings and classification warnings. Acknowledge every flagged item at **Gate 2**.
4. Sign off with your reviewer name at **Gate 3**.
5. View the cited report and ask a question grounded in the run's computed facts.
6. Open **Explore changes** to screen candidates or simulate a trade. Scenarios do not execute trades.
7. Open **Backtest** and run the offline suite to see the test corpus results.

### CLI

Run these commands from `prototype`. The check command interactively asks you to review/confirm the mandate; the example is synthetic.

```powershell
$py = ".\.venv\Scripts\python.exe"

& $py -m portfoliolens check `
  --holdings data\cases\07_ambiguous_holding\holdings.csv `
  --sid data\cases\07_ambiguous_holding\sid.md `
  --as-of 2026-09-30 `
  --reviewer "Your Name"
```

The command prints a run ID and the required sign-off command. Use that ID for the remaining operations:

```powershell
& $py -m portfoliolens ask <run_id> "What is the large cap allocation?"
& $py -m portfoliolens sign-off <run_id> --reviewer "Your Name" --ack RV-01 RV-02
& $py -m portfoliolens screen <run_id> `
  --candidates data\cases\11_screen_large_cap_pool\candidates.csv --weight 3
& $py -m portfoliolens whatif <run_id> `
  --trades data\cases\12_whatif_restores_compliance\trades.csv
```

Use the exact review IDs printed for your run with `--ack`. Do not assume the example IDs above will match.

## Examples tested

There are 13 synthetic cases under [prototype/data/cases](prototype/data/cases). Each backtest case is evaluated against an extracted mandate and (where applicable) a reviewer-supplied ground-truth mandate.

| Case | Example | Expected behavior |
|---|---|---|
| 01 | Large-cap fund at 88% | Meets the large-cap minimum |
| 02 | Large-cap fund at 78% | Shows the allocation breach and signed margin |
| 03 | Multi-cap fund with 18% small cap | Flags the small-cap minimum breach |
| 04 | Flexi-cap fund at 70% equity | Meets the equity minimum without fixed cap-bucket minimums |
| 05 | One issuer at 11% | Flags concentration above the configured limit |
| 06 | SID self-imposes an 85% floor | Applies the stricter SID constraint |
| 07 | Ambiguous holding without an ISIN | Does not silently classify it; requests review |
| 08 | Large-cap fund at 80.1% | Passes but is sent for near-threshold review |
| 09 | Invalid holdings input | Rejects input with row-level validation issues |
| 10 | Forecast/recommendation question | Declines out-of-scope investment advice |
| 11 | Mixed candidate pool | Shows eligible candidates and constraint-based exclusions |
| 12 | Proposed buy to restore a large-cap threshold | Recomputes before/after status; places no trade |
| 13 | SID issuer cap written as words | Extracts the 8.5% cap and flags the holding above it |

### Verification performed in this workspace

- `python -m pytest`: **73 passed**.
- Offline backtest: **13/13 ground-truth cases passed**; **13/13 extracted-mandate cases passed**; extraction field accuracy **24/24**.
- The offline extractor parses the tested “eight and a half per cent” issuer cap; this synthetic case does not validate extraction across varied real SID wording.
- The test for direct OpenAI integration uses a mocked HTTP response; no live API key or live GPT-4o mini request was used in this verification.

The backtest writes `prototype\outputs\backtest\report.md` and `results.json`; run audit records and reports are written under `prototype\outputs\store\`.

## API overview

The FastAPI docs at `/docs` list request/response schemas and all routes. Main endpoints include:

- `POST /checks/validate`, `/checks/extract-mandate`, `/checks/confirm-mandate`, `/checks/run`
- `POST /checks/explain`; `GET /checks/runs`, `/checks/runs/{id}`, `/checks/runs/{id}/report`
- `POST /checks/runs/{id}/sign-off`
- `POST /screen`, `/whatif`
- `GET /samples`, `/samples/{name}`; `POST /backtest/run`, `GET /backtest/latest`

## Project files

- [prototype/](prototype/) — Python application, test suite, sample data, configuration.
- [prototype/README.md](prototype/README.md) — implementation and CLI details.
- [prototype/data/README.md](prototype/data/README.md) — synthetic-data disclosure and input schemas.
- [Docs/Architecture.md](Docs/Architecture.md) — architecture and design trade-offs.
- [PortfolioLens_AI_PRD_v0.3.md](PortfolioLens_AI_PRD_v0.3.md) — requirements used for this README.

**Source note:** The separate problem-statement PDF mentioned in the request was not present in the workspace when this README was prepared. This document summarizes the available PRD v0.3. Provide the PDF to reconcile this README against any requirements that appear only in that document.
