# CLAUDE.md: PortfolioLens prototype

Guidance for anyone (human or agent) changing this code, and the record of the major decisions made while building it, with the reasons. The product spec is [../PortfolioLens_AI_PRD_v0.3.md](../PortfolioLens_AI_PRD_v0.3.md); the diagram is [../Docs/Architecture.md](../Docs/Architecture.md).

## Commands

```powershell
.\.venv\Scripts\python.exe -m pytest                          # unit + integration + backtest tests
.\.venv\Scripts\python.exe -m ruff check src tests scripts    # lint
.\.venv\Scripts\python.exe -m ruff format src tests scripts   # format
.\.venv\Scripts\python.exe -m mypy                            # strict typing on src/
.\.venv\Scripts\python.exe -m portfoliolens backtest          # exit 1 if any ground-truth case fails
.\.venv\Scripts\python.exe scripts\build_fixtures.py          # regenerate data/ after editing case specs
```

## Invariants: do not break these

1. **The trust boundary.** `engine.py` never imports from `agents/`. No model output is ever a percentage, a margin or a pass/fail. If a change needs a model to produce a number that reaches a verdict, the design is wrong.
2. **Nothing runs on an unconfirmed mandate.** `run_check` takes a `mandate_id` that exists only after `confirm_mandate` (Gate 1) records a named reviewer.
3. **Citations are verbatim or absent.** A model value whose quote is not found in the SID by `citations.locate_quote` is discarded and raised as an issue.
4. **Reference data is added, never overwritten.** A new circular means a new rulebook version with its own `effective_from`. A new AMFI list means a new security-master file plus a manifest entry. Runs pin both by SHA-256.
5. **Fail safe, never fail open.** Bad input raises row-level errors. Unresolvable holdings make a verdict INDETERMINATE. A model failure falls back to the deterministic stand-in and is recorded as an issue.
6. **No advice.** No forecast, no "best buy", no ranking by predicted performance. The scope guard runs before any model call.

## Major decisions and why

### Shape of the system

| # | Decision | Why | Rejected alternative |
|---|---|---|---|
| D1 | **Separate `prototype/` package, `src/` layout, one modular monolith** (CLI + FastAPI over one `Coordinator`) | The PRD calls for a modular monolith. The CLI and API share one code path, so the gates cannot be bypassed through either | Microservice per agent: network and ops cost with no benefit at this scale |
| D2 | **Web app is static HTML/CSS/vanilla JS served by FastAPI; no Next.js, no build step** | A reviewer opens one URL after `serve`; there is nothing to compile and no `node_modules`. The app calls the same documented endpoints as the CLI, so there is one code path and the gates cannot be bypassed. The PRD names Next.js, but a build toolchain would add weight without reducing any real risk for a prototype | React/Next build (toolchain overhead); shipping only `/docs` and the CLI (too thin to show the gated workflow) |
| D2a | **Every dynamic value in the UI is escaped through one `html` tagged-template helper** | Report text, SID quotes and holding names are rendered as data, never as markup, so there is no XSS path even though the report Markdown is rendered to HTML. `localStorage` reads/writes are wrapped so a private window cannot break the app | Innerhtml with raw interpolation; a heavy sanitizer library |
| D3 | **A fixed DAG in plain Python, not LangGraph** | The PRD marks LangGraph optional. A fixed, loop-free pipeline is the auditable path a regulator can re-derive, and plain code makes the gates obvious in `pipeline.py` | Agentic self-correcting loop: rejected in the PRD because it cannot be reproduced |
| D4 | **Offline mode is the default; the model is opt-in via `.env`** | Tests and the backtest must be deterministic and runnable without a key or rate limits. Each agent has a deterministic stand-in behind the same interface (`MandateExtractor`, `IdentityResolver`, `ReportWriter`) | Mocking the model only in tests would leave no runnable baseline to compare the model against |

### Numbers and verdicts

| # | Decision | Why |
|---|---|---|
| D5 | **`Decimal` for every value that can reach a verdict; floats never** | `80.1` and `80.0` must compare exactly. Binary floats make threshold comparisons and margins non-reproducible. Rounding (`ROUND_HALF_UP`) is applied only for display and storage, never before a comparison |
| D6 | **Three-valued status: PASS / BREACH / INDETERMINATE** | An unresolved holding could belong to any bucket. The engine computes a low and a high bound (unresolved weight excluded or included). If the threshold falls between them, the honest answer is "cannot tell", which goes to a human. A guess would silently flip pass and breach near a threshold (the PRD's stated weakest point) |
| D7 | **Signed margin where positive always means headroom**, for both min and max rules | One reading rule for reviewers and one sort key for screening. The PRD's formula covers min rules; max rules use `threshold − value` |
| D8 | **"Stricter wins" by evaluating every constraint and marking the binding one** | Both the SEBI rule and the SID limit appear in the report, so a reviewer sees why the SID limit binds. Evaluating everything is equivalent to `max(sebi, self_imposed)` and loses no information |
| D9 | **Near-threshold band is configurable (default 1.0 pp)** and passes inside it become review items | The PRD wants 80.1% surfaced. The band is a policy choice, so it lives in config rather than code |
| D10 | **Issuer key = ISIN** (unresolved holdings keyed by name) | This is enough for the 10% single-issuer rule on equity. Group-level aggregation is a known limitation, listed below |

### Agents and the trust boundary

| # | Decision | Why |
|---|---|---|
| D11 | **Rule Retrieval is a deterministic, keyed lookup, not an LLM + RAG step.** This is a deviation from the PRD and the diagram | The category comes from a mandate a human has already confirmed. With 12 categories, a keyed lookup into the pinned rulebook version is exact and reproducible, whereas embedding retrieval would add a failure mode and give no benefit. RAG becomes worth it when the rulebook holds full circular text and the task is to find candidate passages; even then a human would confirm the result. The Docs diagram still shows "Rule Retrieval (RAG)" and should be updated if this decision stands |
| D12 | **Verbatim-quote rule for every extracted field** (`locate_quote`: whitespace-, case- and typography-insensitive, minimum 8 characters) | This is a deterministic hallucination check. A quote must be a real span of the SID, which also gives the auditor an exact character range |
| D13 | **A number written in words is kept but flagged** | "Eight and a half per cent" is a real way a SID can state a limit. Dropping it loses a binding limit (case 13); trusting it unseen breaks D12. The reviewer sees the flag at Gate 1 |
| D14 | **Identity: deterministic shortlist first; the model may only pick from it, or abstain** | This mirrors the CLUZO locator pattern: the model chooses among real candidates and its answer is validated against them. Any match not made by ISIN becomes a review item, whatever its confidence |
| D15 | **The template report is always produced; the model narrative is optional and validated** | Every cited id must exist and every number must appear in the facts (ids and ISINs are stripped first). There must be no advice language, and no "compliant" when the run is not. A rejected narrative becomes a `report_rejected` review item, so the reviewer knows the model misbehaved |
| D16 | **The scope guard is a deterministic regex that runs before any model call** | Declining forecasts and advice must not depend on a model's judgement (PRD case 10). The same guard also screens model answers |
| D17 | **Only aggregates go to the model** | The SID is a public document. The model receives facts and findings (percentages) and holding names for identity, never per-holding market values (PRD security note) |
| D18 | **OpenRouter call: temperature 0, no `response_format`**, with a tolerant JSON-object parser and retries on 408/429/5xx | Many free-tier models reject `response_format`. The parser accepts fenced JSON and rejects anything that is not an object |

### Human gates (enforced in code, not by convention)

| Gate | Where | Rule |
|---|---|---|
| 1. Confirm mandate | `Coordinator.confirm_mandate` | Needs a reviewer name. A proposal with issues needs an explicit acknowledgement or a corrected mandate. An incomplete proposal cannot be confirmed |
| 2. Exception review | `Coordinator.sign_off` | Every review id must be acknowledged; unknown ids are rejected |
| 3. Sign-off | `Coordinator.sign_off` | Once per run, attributable, `approved` or `escalated` |

### P1: screening and what-if

| # | Decision | Why |
|---|---|---|
| D19 | **Eligibility = a what-if purchase that creates or deepens no breach and no uncertainty** | This is "one engine, built once" taken literally. A separate allowed-bucket filter would duplicate the rules and get funding wrong. For example, a cash-funded mid-cap buy does not lower the large-cap percentage, but a fully invested fund that trims pro-rata does |
| D20 | **Funding is explicit**: pro-rata trim for screening (a fully invested fund), cash for what-if; net assets held constant | The verdict depends on how a buy is funded, so it is a displayed parameter rather than a hidden assumption. Trims are rounded to 8 dp and the residual goes to the largest position, so net assets are conserved exactly |
| D21 | **Scenarios reuse the run's confirmed constraints and classifications**, and check that the security master's SHA still matches the run | A scenario must not re-read the SID, re-derive thresholds or re-ask a model, so it cannot quietly change a compliance verdict |
| D22 | **Ranking only by a displayed field** (`headroom` or `name`) | PRD PL-FR-16: the tool sorts, it never forecasts |

### Data, storage, testing

| # | Decision | Why |
|---|---|---|
| D23 | **The rulebook is stored as full snapshots per version** (`2017-10`, `2020-11`, `2026-02`), each with `effective_from` | Pinning one snapshot by SHA makes every run reproducible. The multi-cap 25/25/25 change shows why: a 2019 run must not see it |
| D24 | **The security master is dated**, and each bucket is validated against its rank at load time | AMFI republishes twice a year. Orchid Retail (rank 99 → 103) shows a name crossing the boundary |
| D25 | **Fixtures are generated by `scripts/build_fixtures.py`**: fictional names and synthetic ISINs with valid check digits | Cases are exact by construction (NAV 10,000; MV = % × 100), and regeneration is deterministic. The guidelines require public or synthetic data only |
| D26 | **The backtest runs every case twice: extracted vs ground truth** | This separates agent accuracy from engine correctness. A ground-truth failure is a bug (non-zero exit); an extracted-only failure is an agent miss that Gate 1 exists to catch |
| D27 | **Case 13 is added beyond the PRD's 12** | It shows honestly where the offline stand-in fails, and why a language model belongs in extraction |
| D28 | **JSON file store with a strict id pattern and atomic writes** | Enough for a single-user prototype. The `^[a-f0-9]{12}$` pattern also blocks path traversal through API ids |

## Known limitations (deliberate, for the prototype)

- **Rule texts.** They are paraphrases. The thresholds follow public summaries, and the Feb-2026 circular reference is taken from the PRD and unverified. Verify everything before real use.
- **Rule coverage.** Equity categories only. Index and sectoral carve-outs to the 10% issuer limit are not applied.
- **Issuer aggregation.** Exposure is not aggregated across share classes or group companies. An unresolved holding is not added to the issuer upper bound.
- **Sector names.** For sectoral schemes, the SID's sector wording is not mapped to the security master's taxonomy, so the reviewer enters the master sector at Gate 1.
- **Prototype infrastructure.** There is no authentication on the API, and the file store is not safe under concurrent writes.
- **Model path not backtested.** The model path is covered by unit tests with a scripted client, but has not been backtested against a live model. Set `.env` and run `portfoliolens backtest`.

## Conventions

- **Data access.** Domain models are frozen Pydantic models (`extra="forbid"`). Untrusted model JSON is validated into them, or discarded with an issue.
- **Errors.** Expected failures subclass `PortfolioLensError`; the API maps them to 4xx/5xx and the CLI to exit code 1.
- **Testing changes to the engine.** Add or extend a case in `scripts/build_fixtures.py`, regenerate, and keep `ground_truth` at 100%.
