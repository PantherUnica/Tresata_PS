# PortfolioLens AI — Product Requirements Document

**A multi-agent compliance and constraint-aware portfolio assistant for mutual funds**

| Field | Value |
|---|---|
| Version | 0.3 — combined scope (mandate compliance + constraint-aware screening) |
| Date | 03 November 2026 |
| Status | Build-ready direction; product assumptions require validation |
| Product | PortfolioLens AI — one product, two capabilities |
| Jurisdiction | SEBI (India) — equity mutual fund scheme categorisation |
| Data policy | Public or synthetic data only; no client or personal financial data |

> **Scope decision:** One product, two capabilities built on one constraint engine.
> (1) **Compliance core** — check whether a scheme's actual month-end holdings comply with its stated mandate and SEBI's rules.
> (2) **Constraint-aware screening + what-if** — use the same mandate constraints to screen for eligible securities and simulate how a rebalance affects compliance.
> The tool never predicts returns or recommends a "best" investment — the manager decides; the tool only shows what is *allowed* and *compliant*.

---

## 1. Executive summary

PortfolioLens AI answers a question an AMC compliance analyst answers by hand every month, for every scheme: **does this fund's actual portfolio still match what it promised and what the regulator requires?** A large-cap fund must hold ≥80% large-cap stocks; a multi-cap fund must hold ≥25% each in large, mid and small caps. When the live portfolio drifts below a threshold, the scheme is no longer "true to label" — a reportable breach.

This is a **document-and-rules reconciliation problem**: the mandate is natural-language prose in the Scheme Information Document (SID), the thresholds live in SEBI circulars that change over time, and the holdings arrive in a monthly disclosure file. That mix — language understanding plus exact, auditable arithmetic — is where an LLM earns its place, and where a single model call does not.

### Guiding principle
The LLM **reads, extracts, retrieves and explains**. Deterministic code **computes and decides**. These zones never blur: **no agent ever computes a percentage or declares a pass/fail.** Every verdict traces back to the holdings, the mandate clause and the SEBI rule version that produced it. A qualified human reviews each flag and signs off — the system flags, it never decides.

### Two capabilities, one constraint engine
The rules that **judge** compliance are the same rules that **define** what the manager is allowed to buy. One engine, built once, serves both.

| Capability | What it does | Priority |
|---|---|---|
| Compliance check (the spine) | Prove each scheme's holdings comply with its mandate and SEBI rules; cited exception report. | **P0 — core** |
| Constraint-aware screening | Use mandate constraints to surface securities the fund is actually allowed to hold. | P1 |
| What-if rebalance simulator | Recompute allocation and concentration for a proposed trade; show whether it keeps/restores compliance. | P1 |
| Explicitly rejected | Predicting expected returns or recommending a "best" buy — that is advice, and not auditable. | Out of scope |

### Product principles
- **Deterministic numbers first** — every %, margin and pass/fail is computed in code, never by a model.
- **Cited, not recalled** — SEBI thresholds come from a versioned retrieval index with effective dates, never from model memory.
- **Fail safely** — missing holdings, unmatched securities or an unparseable mandate produce validation errors, not a fabricated verdict.
- **No autonomous decisions** — outputs are compliance decision-support; a qualified human stays accountable.
- **Reproducibility** — every run pins the rulebook version, AMFI list version, dataset hash, parameters and engine version.
- **Honest scope** — SEBI equity categories only; the prototype shows its failure modes.

---

## 2. Why this problem (and why AI is necessary)

Performance analytics (Sharpe, alpha, drawdown) is arithmetic over a price series — a spreadsheet, not an AI problem, with no clause to cite and no regulator to satisfy. **Mandate compliance is the opposite:** the rule to apply is written in prose, the thresholds change, and a wrong answer is a liability. That is exactly the archetype the brief rewards — a bottleneck that exists because a human has to read documents and reconcile them against rules.

- **Why AI, not a pure rules engine:** the mandate lives in prose and SID wording varies by AMC; a rules engine alone can't extract it.
- **Why rules/retrieval, not a pure LLM:** thresholds must be cited from the current rule version; the arithmetic must be exact and auditable.
- **Jurisdiction portability:** the architecture is jurisdiction-agnostic. Only the rulebook index and the market-cap reference table are India-specific; swap them for UCITS (EU) or '40 Act (US) and the extraction and reconciliation stages are unchanged.

---

## 3. Product

### 3.1 Problem statement
A mutual fund manager and the compliance team supporting them must constantly keep a portfolio true to label — within the fund's stated mandate and SEBI's rules — both when **checking** the current portfolio and when **changing** it. Today both are done by hand in spreadsheets, re-applying the same rules over and over.

- **Checking:** monthly — read the SID, classify each stock into large/mid/small cap using the current AMFI list, sum the buckets, check concentration, confirm every number clears the right threshold.
- **Changing:** on every trade decision — "is this security allowed under my mandate?" and "will this move push me out of compliance?"

| Who | What task | How often | What it costs today |
|---|---|---|---|
| Fund manager + compliance/risk analyst at an AMC | Check holdings vs mandate + SEBI rules; screen/simulate trades that keep the fund compliant | Checking: monthly per scheme. Changing: on every trade; dozens to 100+ schemes per house | Hours of repetitive spreadsheet work; manual market-cap classification redone for every check and what-if; breach risk if a threshold is missed |

**The insight that ties it together:** the rules that judge compliance are the same rules that define the eligible investment universe. Nobody treats these as one problem today.

**Why now:** SEBI's February 2026 categorisation circular (superseding the Oct/Nov 2025 provisions) tightens "true to label" expectations, naming norms and portfolio-overlap limits for sectoral/thematic schemes, with a six-month compliance window.

### 3.2 Target users
- **Primary — AMC compliance/risk analyst:** check each scheme monthly and evidence it is true to label.
- **Primary — fund manager:** before trading, know what is mandate-eligible and whether a rebalance stays compliant.
- **Secondary — trustee/audit reviewer:** independently challenge a scheme and inspect how a flag was derived.
- **Secondary — FoF/distributor due-diligence:** screen external schemes for mandate drift.

### 3.3 Goals
- Ingest a holdings file and a SID (or extract) using documented schemas.
- Extract the stated mandate (category, benchmark, numeric limits, self-imposed constraints) with cited source clauses.
- Retrieve matching SEBI rules from a versioned rulebook with effective dates.
- Classify each holding into a market-cap bucket, sector and instrument type via a dated AMFI reference.
- Reconcile allocation and concentration against every threshold, deterministically (compliance core).
- Screen a candidate universe for mandate-eligible securities (P1).
- Simulate a rebalance and recompute allocation, concentration and compliance status (P1).
- Produce a cited exception report and answer grounded questions, with a full audit trail.

### 3.4 Non-goals (MVP)
- No return prediction, "best buy" ranking, or autonomous rebalancing — screening shows eligibility, not advice.
- No debt/hybrid/solution scheme rules; equity only.
- No liquidity, credit-risk, derivatives or stress-test limits.
- No cross-scheme overlap checks (later scope).
- No live market-data/custodian/RTA integrations; files only.
- No automatic regulatory filing; no authority to alter a verdict or place a trade.

### 3.5 Primary user journey (compliance, P0)
1. Start a new compliance check.
2. Upload holdings CSV + SID (or load a synthetic example).
3. Confirm the rulebook version and AMFI list version.
4. The Mandate Extraction agent proposes a structured mandate; user reviews cited clauses and confirms.
5. Pipeline runs: rules retrieved, holdings classified, engine reconciles.
6. Review the dashboard: per-rule pass/fail/breach with margin, classification breakdown, concentration panel.
7. Open any flag to see citations (holdings + clause + rule version); ask grounded questions.
8. Sign off and export; run ID, versions and dataset hash retained.

### 3.6 Secondary journey (screening / what-if, P1)
1. From a run, open "Explore changes".
2. Screen a candidate universe → engine filters to eligible securities (right bucket/sector, won't breach concentration), with a reason each is in/out.
3. Optionally rank the eligible list by a **manager-chosen, displayed** metric — the tool sorts, it does not forecast.
4. Build a what-if: propose a trade → engine recomputes allocation/concentration → shows before/after compliance and any new breach.
5. Keep the what-if as a scenario artifact (not an order); the manager decides and acts outside the tool.

### 3.7 Functional requirements (selected)
| ID | Requirement | Acceptance |
|---|---|---|
| PL-FR-04 | Extract mandate | Structured mandate with a cited clause per field |
| PL-FR-05 | Confirm mandate | Human confirmation required before reconciliation |
| PL-FR-06 | Retrieve rules | Return rule IDs, thresholds and effective dates |
| PL-FR-07 | Classify holdings | Dated AMFI join; flag unmatched/low-confidence items |
| PL-FR-10 | Reconcile vs thresholds | Output pass/fail/breach + signed margin |
| PL-FR-11 | Generate explanation | Every numeric claim maps to a fact ID |
| PL-FR-14 | Screen eligible universe | In/out reason per candidate citing the binding constraint |
| PL-FR-15 | What-if simulation | Recompute before/after compliance; never execute a trade |
| PL-FR-16 | Transparent ranking | Sort only by a manager-chosen, displayed metric |

---

## 4. Multi-agent architecture

### 4.1 Why multiple agents (and not one call)
A single LLM call would do the arithmetic (not auditable), recall thresholds from memory (not cited, stale), and produce a one-shot answer no regulator could reproduce. The problem breaks into stages with **different capabilities and different trust levels** — the honest justification for separate agents:

| Stage | Capability | Trust level |
|---|---|---|
| Read the mandate | Language understanding over SID prose | Probabilistic — must be confirmed |
| Retrieve the rule | Indexed retrieval over a versioned rulebook | Probabilistic retrieval, exact cited payload |
| Classify holdings | Identity resolution + deterministic reference join | Mostly deterministic; LLM only at the margin |
| Reconcile numbers | Exact arithmetic and threshold logic | **Deterministic — no LLM** |
| Screen eligibility (P1) | Apply mandate constraints as a filter | Deterministic filter; LLM only tags sector/bucket |
| Simulate a rebalance (P1) | Recompute allocation/concentration | **Deterministic — no LLM** |
| Explain the verdict | Grounded natural-language generation | Probabilistic, constrained to a facts object |

### 4.2 Agent roster
| Component | Type | Responsibility |
|---|---|---|
| Coordinator | Orchestrator (code) | Route the package through a fixed DAG; manage state; pin versions |
| Mandate Extraction agent | LLM | Extract category, benchmark, limits; cite each source clause |
| Rule Retrieval agent | LLM + RAG | Retrieve the matching SEBI rule set from the versioned rulebook |
| Holdings Classification agent | LLM-assisted | Resolve identity (name→ISIN) + assign bucket via dated AMFI join |
| **Reconciliation engine** | **Deterministic code** | Compute allocations, concentration, consistency; pass/fail/breach + margin |
| Candidate Screening agent (P1) | LLM-assisted | Tag candidate sector/bucket so the engine can test eligibility |
| **Eligibility & What-if engine (P1)** | **Deterministic code** | Filter candidates; recompute what-if allocation; report post-trade compliance |
| Report / Explanation agent | LLM | Turn the facts object into a cited report; answer grounded questions |
| Fund manager / compliance officer | Human | Review, confirm the mandate, sign off, decide and act |

### 4.3 The trust boundary
The whole design turns on one boundary. **No agent computes a percentage, a margin or a pass/fail.** The model locates and proposes; the engine measures and judges.

| Probabilistic zone (LLM proposes) | Deterministic zone (code decides) |
|---|---|
| Extracting the mandate from SID prose | Summing market value by bucket / net assets |
| Retrieving which rule applies | Comparing each allocation to its threshold |
| Resolving a security's identity and bucket | Computing single-stock and sector concentration |
| Tagging a candidate's sector/bucket | Filtering eligibility; recomputing a what-if |
| Writing the explanation | Producing pass/fail/breach and the signed margin |

### 4.4 Control flow (fixed DAG, two paths)
```
COMPLIANCE PATH (P0)
ingest(holdings, SID) -> validate
  -> [Mandate Extraction] -> proposed mandate + cited clauses
  -> HUMAN GATE: confirm / correct mandate
  -> [Rule Retrieval] (pin rulebook version) -> cited rule set
  -> [Holdings Classification] (pin AMFI version) -> ISIN->bucket map
  -> Reconciliation engine (code) -> facts object (pass/fail/breach + margins)
  -> [Report agent] -> cited exception report
  -> HUMAN SIGN-OFF -> export + audit record

SCREENING / WHAT-IF PATH (P1)  [reuses confirmed mandate + rule set]
candidate_universe / proposed_trade
  -> [Candidate Screening] -> sector/bucket tags (where needed)
  -> Eligibility & What-if engine (code) -> eligible list OR post-trade facts
  -> [Report agent] -> scenario explanation (no advice)
  -> HUMAN decides and acts outside the tool
```
Neither path loops back. The screening/what-if path branches off the **confirmed** mandate and the same rule set — it never re-reads the SID or re-derives thresholds, so a scenario cannot quietly change a compliance verdict.

### 4.5 Where knowledge lives (and freshness)
| Knowledge | Where it lives | When it changes |
|---|---|---|
| The fund's mandate | SID document; extracted per run | New SID supersedes it; re-extract and re-confirm |
| SEBI thresholds | Versioned retrieval index with effective dates — not the model | New circular adds a version; old runs stay pinned |
| Market-cap buckets | Dated AMFI semi-annual list | Republished twice a year; runs pin the in-force version |
| Reconciliation logic | Code under version control | Logic change bumps the engine version |
| The final judgement | The human | Sign-off is per run and attributable |

> The Feb 2026 circular is the live example: add a new rulebook version effective 2026-02-26, don't overwrite. A June 2025 run still reproduces against the rule in force then.

### 4.6 Weakest point
**Holdings classification** — assigning each security to the right market-cap bucket. It depends on resolving the holding to the right ISIN and using the current AMFI list. A security near a bucket boundary that AMFI reclassifies, a recent IPO, or a dual-listed name can be misclassified — which can flip a pass to a breach near a threshold.
- *Mitigation:* deterministic ISIN join; LLM only resolves identity, with confidence; always display margin-to-threshold; near-threshold results forced to human review; unmatched items never silently bucketed.

### 4.7 Alternatives rejected
| Alternative | Why rejected |
|---|---|
| One large LLM call → verdict | Model does arithmetic (not auditable), recalls thresholds (stale), one-shot (not reproducible) |
| Pure rules engine, no LLM | Can't parse the prose mandate; SID wording varies |
| Agentic feedback loop (self-correcting) | Compliance needs a fixed, reproducible path a regulator can audit |
| Merge retrieval into extraction | Thresholds must be a separate, cited step, not recalled by the model that read the SID |
| LLM classifies buckets freely | Constrained to a deterministic ISIN→AMFI join for reproducibility |
| **Return-prediction / "best buy" agent** | Reintroduces advice and non-auditable forecasts — the reason the advisory idea was reframed; screening stays a deterministic eligibility filter |
| Microservices per agent | Overhead without benefit; a modular monolith keeps boundaries clear |

> **What I'd stand behind without AI:** the trust boundary and the rejection of the agentic loop — why arithmetic and pass/fail must sit in deterministic code, why thresholds must be cited not recalled, and why a reproducible fixed pipeline beats a loop when a regulator must re-derive every number.

---

## 5. Technical architecture

- **Modular monolith** — one FastAPI backend with domain packages per agent and the engine.
- **LLM outputs are untrusted** — validated against Pydantic schemas before crossing any boundary.
- The deterministic engine never runs on an unvalidated / human-unconfirmed mandate.
- All runs reproducible from dataset hashes, pinned rule/AMFI versions, parameters and engine version.

**Stack:** Next.js + TypeScript (frontend) · Python + FastAPI (backend) · Pydantic (validation) · FAISS/pgvector (rulebook index) · Pandas + NumPy (numerics) · OpenRouter-compatible LLM (configurable) · LangGraph *optional* for the fixed DAG (plain orchestration is fine) · SQLite local / PostgreSQL hosted · Pytest.

**Security:** public/synthetic data only; server-side keys; upload size/MIME checks; no raw holdings in prompt logs; every report labelled compliance decision-support, not assurance or advice.

---

## 6. API (proposed)
| Method + endpoint | Purpose |
|---|---|
| `POST /checks/validate` | Validate holdings + SID inputs |
| `POST /checks/extract-mandate` | Run mandate extraction |
| `POST /checks/run` | Run the reconciliation pipeline on a confirmed mandate |
| `POST /checks/explain` | Grounded explanation / Q&A |
| `GET /checks/runs/{id}` | Retrieve a run + audit metadata |
| `POST /screen` (P1) | Screen a candidate universe for eligibility |
| `POST /whatif` (P1) | Simulate a rebalance; never executes a trade |

---

## 7. Testing (12 cases: 10 compliance + 2 screening/what-if)
| # | Scenario | Expected |
|---|---|---|
| 1 | Large-cap scheme at 88% large cap | All pass; no flags |
| 2 | Large-cap scheme at 78% | Breach of 80% rule; margin −2.0; cited |
| 3 | Multi-cap with only 18% small cap | Breach of the 25%-each rule (small leg) |
| 4 | Flexi-cap at 70% equity | Pass (≥65%); no cap minimums apply |
| 5 | Single stock at 11% of net assets | Concentration breach of the 10% limit |
| 6 | SID states a self-imposed 85% floor | Apply the stricter stated limit; cite the SID clause |
| 7 | Holding with no ISIN, ambiguous name | Raise at human gate; don't silently bucket |
| 8 | Scheme at exactly 80.1% large cap | Pass, but surfaced for near-threshold review |
| 9 | Missing values / duplicate ISIN / bad sums | Reject with row-level validation errors |
| 10 | "Predict next month's drift / recommend a trade" | Decline; report current compliance only |
| 11 | Screen a mixed-cap pool for a large-cap fund | Only eligible names returned; each exclusion cites its constraint |
| 12 | What-if: buy large-cap to move 78.3% → 80.1% | before=BREACH, after=PASS (+0.1); no trade executed; no forecast |

---

## 8. Delivery plan
1. **M0 Foundation** — repo, schemas, synthetic fixtures, rulebook v1, AMFI sample.
2. **M1 Deterministic core** — reconciliation engine; unit tests match hand-calculated verdicts.
3. **M2 Classification + retrieval** — ISIN→AMFI join; versioned rulebook retrieval with citations.
4. **M3 Agents + human gate** — mandate extraction, report agent, confirm + sign-off.
5. **M4 Dashboard + export** — compliance workflow end-to-end.
6. **M4b Screening + what-if (P1)** — eligibility filter + rebalance simulator on the same engine.
7. **M5 Validation + hardening** — 12-case suite, audit records, README, demo.

> Ship the compliance core (P0) end-to-end before starting the screening/what-if layer (P1).

---

## 9. Key risks
| Risk | Mitigation |
|---|---|
| Holdings misclassification | Deterministic ISIN join; confidence flags; near-threshold → review; margins shown |
| Stale AMFI list | Pin the version in force for the as-of date |
| Outdated rule version | Versioned rulebook with effective dates; runs pin a version |
| Mandate extraction error | Human confirmation gate; cite every clause |
| LLM hallucination | Facts-only prompting; `cited_fact_ids[]` required; reject unknown IDs |
| **Advisory creep** | Screening shows only eligibility; no forecast/ranking by predicted performance; "not advice" framing |
| Stale prices in a what-if | Show as-of date + price source; label it a scenario, not a live quote |
| Financial liability | Decision-support disclaimer; human sign-off; no advice or filing |

---

## 10. SEBI rule set (Appendix)

### Equity scheme minimum allocations
| Category | Minimum allocation |
|---|---|
| Large Cap | ≥80% large-cap |
| Large & Mid Cap | ≥35% large-cap AND ≥35% mid-cap |
| Mid Cap | ≥65% mid-cap |
| Small Cap | ≥65% small-cap |
| Multi Cap | ≥75% equity, with ≥25% each in large/mid/small |
| Flexi Cap | ≥65% equity (no fixed cap minimums) |
| ELSS | ≥80% equity (3-yr lock-in) |
| Focused | ≥65% equity, max 30 stocks |
| Sectoral/Thematic | ≥80% in the stated sector/theme |
| Value / Contra | ≥65% equity |
| Dividend Yield | ≥65% equity, predominantly dividend-yielding |

### Market-cap buckets (AMFI, semi-annual)
- **Large cap:** rank 1–100 by full market cap
- **Mid cap:** rank 101–250
- **Small cap:** rank 251+

### Concentration (MVP)
- Single-issuer equity limit: generally ≤10% of NAV (index/some sectoral funds have carve-outs; flagged for review, not auto-applied).

### Core formulas
```
bucket_pct(b)    = sum(mv_i for i in bucket b) / net_assets * 100
category pass    = bucket_pct(required_bucket) >= threshold
margin           = bucket_pct(required_bucket) - threshold   # signed
single_stock_pct = max_i(mv_i / net_assets * 100)
binding_limit    = max(sebi_threshold, self_imposed_threshold)  # stricter wins

# P1
eligible(c)      = bucket(c) in allowed_buckets
                   AND sector(c) allowed
                   AND post_add_weight(c) <= single_issuer_cap
what_if(trade)   = recompute bucket_pct, single_stock_pct on apply(trade, holdings)
                   -> {before, after, new_breaches}   # deterministic, no forecast
```

---

## Source basis
1. Company take-home assignment, October 2026 (problem selection, architecture, auditability, liability, submission).
2. Original idea notes (portfolio-analytics concept, reframed into mandate compliance + constraint-aware screening).
3. SEBI scheme-categorisation framework as updated by the **February 2026 circular** (superseding Oct/Nov 2025), carrying forward the Oct 2017 category minimums and the Sep 2020 multi-cap amendment; AMFI market-cap definitions. *Verified against public summaries, Nov 2026 — confirm exact figures against the SEBI circular and current AMFI list before production use.*

> **Reframing note:** v0.3 is one product with two capabilities on one engine. It folds the original advisory idea back in but keeps only the defensible part — eligibility + deterministic what-if — and deliberately rejects return prediction and buy recommendations.
