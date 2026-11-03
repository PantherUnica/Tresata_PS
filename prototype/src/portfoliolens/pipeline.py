"""Coordinator and LangGraph workflow for the compliance pipeline.

P0  validate -> extract mandate -> GATE 1 confirm -> retrieve rules -> classify -> reconcile -> report
    -> GATE 2 acknowledge review items -> GATE 3 sign-off
P1  screen / what-if, reusing the run's confirmed mandate, constraints and classifications

The API owns the human gates. LangGraph orchestrates the deterministic workflow after Gate 1.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from portfoliolens.agents.identity import LLMIdentityResolver
from portfoliolens.agents.llm import build_llm_client
from portfoliolens.agents.mandate import HeuristicMandateExtractor, LLMMandateExtractor, MandateExtractor
from portfoliolens.agents.report import ReportWriter
from portfoliolens.agents.rule_retrieval import RuleRetrievalAgent
from portfoliolens.classification import Classifier, HeuristicIdentityResolver, IdentityResolver
from portfoliolens.config import Settings
from portfoliolens.domain import (
    Answer,
    CandidateResult,
    ClassifiedHolding,
    ConfirmedMandate,
    Constraint,
    EngineResult,
    Funding,
    InputSummary,
    Mandate,
    MandateProposal,
    Report,
    RuleRetrievalAudit,
    Run,
    RunSummary,
    ScreeningResult,
    SignOff,
    Trade,
    VersionPin,
    WhatIfResult,
)
from portfoliolens.engine import (
    ENGINE_VERSION,
    ReviewSpec,
    apply_trades,
    assess_eligibility,
    compare_results,
    reconcile,
)
from portfoliolens.errors import GateError, InputValidationError, ReproducibilityError, TradeError, ValidationIssue
from portfoliolens.ingest import HoldingsFile, load_sid, parse_candidates, parse_holdings, parse_trades
from portfoliolens.reference import ReferenceLibrary, SecurityMaster
from portfoliolens.store import JsonStore, new_id

logger = logging.getLogger(__name__)

SCENARIO_NOTE = "Scenario only: nothing is traded, filed or forecast. The manager decides and acts outside the tool."
SortBy = Literal["name", "headroom"]


class _RunWorkflowState(TypedDict, total=False):
    mandate_id: str
    holdings_csv: str
    as_of: date
    confirmed: ConfirmedMandate
    holdings: HoldingsFile
    master: SecurityMaster
    rulebook_version: str
    rulebook_sha: str
    constraints: list[Constraint]
    rule_retrieval: RuleRetrievalAudit
    classified: list[ClassifiedHolding]
    result: EngineResult
    pins: list[VersionPin]
    report: Report
    run: Run


class Coordinator:
    def __init__(
        self,
        *,
        settings: Settings,
        references: ReferenceLibrary,
        store: JsonStore,
        extractor: MandateExtractor,
        resolver: IdentityResolver,
        reports: ReportWriter,
        rule_retriever: RuleRetrievalAgent | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._references = references
        self._store = store
        self._extractor = extractor
        self._resolver = resolver
        self._reports = reports
        self._rule_retriever = rule_retriever or RuleRetrievalAgent()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_workflow = self._build_run_workflow()

    @classmethod
    def from_settings(cls, settings: Settings, store_dir: Path | None = None) -> Coordinator:
        client = build_llm_client(settings)
        heuristic_extractor = HeuristicMandateExtractor()
        heuristic_resolver = HeuristicIdentityResolver()
        return cls(
            settings=settings,
            references=ReferenceLibrary(settings.data_dir),
            store=JsonStore(store_dir or settings.output_dir / "store"),
            extractor=LLMMandateExtractor(client, heuristic_extractor) if client else heuristic_extractor,
            resolver=LLMIdentityResolver(client, heuristic_resolver) if client else heuristic_resolver,
            reports=ReportWriter(client),
            rule_retriever=RuleRetrievalAgent(client),
        )

    @property
    def store(self) -> JsonStore:
        return self._store

    # --- P0 -----------------------------------------------------------------------------

    def validate_inputs(self, holdings_csv: str, sid_text: str | None = None) -> InputSummary:
        issues: list[ValidationIssue] = []
        holdings = sid = None
        try:
            holdings = parse_holdings(holdings_csv)
        except InputValidationError as exc:
            issues.extend(exc.issues)
        if sid_text is not None:
            try:
                sid = load_sid(sid_text)
            except InputValidationError as exc:
                issues.extend(exc.issues)
        if issues or holdings is None:
            raise InputValidationError(issues)
        return InputSummary(
            rows=len(holdings.holdings),
            net_assets=holdings.nav,
            holdings_sha256=holdings.sha256,
            sid_sha256=sid.sha256 if sid else None,
        )

    def propose_mandate(self, sid_text: str) -> MandateProposal:
        proposal = self._extractor.extract(load_sid(sid_text), new_id())
        self._store.save("proposals", proposal.proposal_id, proposal)
        return proposal

    def confirm_mandate(
        self,
        proposal_id: str,
        reviewer: str,
        corrected: Mandate | None = None,
        acknowledge_issues: bool = False,
    ) -> ConfirmedMandate:
        """GATE 1. Nothing downstream runs on a mandate a named human has not confirmed."""
        if not reviewer.strip():
            raise GateError("Gate 1 needs the reviewer's name")
        proposal = self._store.load("proposals", proposal_id, MandateProposal)
        if proposal.issues and corrected is None and not acknowledge_issues:
            raise GateError(
                "The proposal has open issues; supply a corrected mandate or acknowledge the issues explicitly: "
                + " | ".join(proposal.issues)
            )
        mandate = corrected if corrected is not None else _mandate_from(proposal)
        confirmed = ConfirmedMandate(
            mandate_id=new_id(),
            proposal_id=proposal.proposal_id,
            mandate=mandate,
            sid_sha256=proposal.sid_sha256,
            extractor=proposal.extractor,
            confirmed_by=reviewer.strip(),
            confirmed_at=self._clock(),
            corrected_by_reviewer=corrected is not None,
            acknowledged_issues=list(proposal.issues) if corrected is None else [],
        )
        self._store.save("mandates", confirmed.mandate_id, confirmed)
        return confirmed

    def run_check(self, mandate_id: str, holdings_csv: str, as_of: date) -> Run:
        state = self._run_workflow.invoke({"mandate_id": mandate_id, "holdings_csv": holdings_csv, "as_of": as_of})
        run = state.get("run")
        if not isinstance(run, Run):
            raise RuntimeError("LangGraph compliance workflow completed without producing a run")
        return run

    @property
    def run_workflow_nodes(self) -> tuple[str, ...]:
        """Node names in the compiled compliance workflow, for diagnostics and tests."""
        return tuple(self._run_workflow.get_graph().nodes)

    def _build_run_workflow(
        self,
    ) -> CompiledStateGraph[_RunWorkflowState, None, _RunWorkflowState, _RunWorkflowState]:
        workflow = StateGraph(_RunWorkflowState)
        workflow.add_node("load_confirmed_inputs", self._workflow_load_inputs)
        workflow.add_node("retrieve_rules_and_references", self._workflow_retrieve_rules)
        workflow.add_node("classify_holdings", self._workflow_classify)
        workflow.add_node("reconcile_constraints", self._workflow_reconcile)
        workflow.add_node("write_cited_report", self._workflow_report)
        workflow.add_node("persist_run", self._workflow_persist)
        workflow.add_edge(START, "load_confirmed_inputs")
        workflow.add_edge("load_confirmed_inputs", "retrieve_rules_and_references")
        workflow.add_edge("retrieve_rules_and_references", "classify_holdings")
        workflow.add_edge("classify_holdings", "reconcile_constraints")
        workflow.add_edge("reconcile_constraints", "write_cited_report")
        workflow.add_edge("write_cited_report", "persist_run")
        workflow.add_edge("persist_run", END)
        return workflow.compile()

    def _workflow_load_inputs(self, state: _RunWorkflowState) -> dict[str, object]:
        confirmed = self._store.load("mandates", state["mandate_id"], ConfirmedMandate)
        holdings = parse_holdings(state["holdings_csv"])
        return {"confirmed": confirmed, "holdings": holdings}

    def _workflow_retrieve_rules(self, state: _RunWorkflowState) -> dict[str, object]:
        as_of = state["as_of"]
        master = self._references.security_master(as_of)
        rulebook, rulebook_sha = self._references.rulebook(as_of)
        constraints, retrieval_audit = self._rule_retriever.retrieve(
            rulebook, rulebook_sha, state["confirmed"].mandate, as_of
        )
        return {
            "master": master,
            "rulebook_version": rulebook.version,
            "rulebook_sha": rulebook_sha,
            "constraints": constraints,
            "rule_retrieval": retrieval_audit,
        }

    def _workflow_classify(self, state: _RunWorkflowState) -> dict[str, object]:
        classified = self._classifier(state["master"]).classify(state["holdings"].holdings)
        return {"classified": classified}

    def _workflow_reconcile(self, state: _RunWorkflowState) -> dict[str, object]:
        confirmed = state["confirmed"]
        mandate_reviews: list[ReviewSpec] = [
            ("mandate_issue", f"Acknowledged at Gate 1: {issue}", f"mandate:{confirmed.mandate_id}")
            for issue in confirmed.acknowledged_issues
        ]
        result = reconcile(
            state["classified"],
            state["constraints"],
            near_threshold_band=self._settings.near_threshold_band_pp,
            extra_reviews=mandate_reviews,
        )
        return {"result": result}

    def _workflow_report(self, state: _RunWorkflowState) -> dict[str, object]:
        run_id = new_id()
        as_of = state["as_of"]
        confirmed = state["confirmed"]
        master = state["master"]
        holdings = state["holdings"]
        pins = [
            VersionPin(
                kind="rulebook",
                version=state["rulebook_version"],
                sha256=state["rulebook_sha"],
            ),
            VersionPin(kind="security_master", version=master.version, sha256=master.sha256),
            VersionPin(kind="engine", version=ENGINE_VERSION),
        ]
        report, result = self._reports.write(
            run_id=run_id, as_of=as_of, mandate=confirmed, pins=pins, result=state["result"]
        )
        run = Run(
            run_id=run_id,
            created_at=self._clock(),
            as_of=as_of,
            llm=self._settings.llm_label,
            mandate=confirmed,
            pins=pins,
            rule_retrieval=state["rule_retrieval"],
            holdings_sha256=holdings.sha256,
            holdings=list(holdings.holdings),
            classification=state["classified"],
            constraints=state["constraints"],
            result=result,
            report=report,
        )
        return {"pins": pins, "report": report, "run": run}

    def _workflow_persist(self, state: _RunWorkflowState) -> dict[str, object]:
        run = state["run"]
        self._store.save("runs", run.run_id, run)
        self._store.save_text("runs", run.run_id, ".md", run.report.markdown)
        return {"run": run}

    def get_run(self, run_id: str) -> Run:
        return self._store.load("runs", run_id, Run)

    def list_runs(self, limit: int = 50) -> list[RunSummary]:
        summaries: list[RunSummary] = []
        for run_id in self._store.list_ids("runs")[:limit]:
            try:
                run = self.get_run(run_id)
            except ValidationError:
                logger.warning("Skipping unreadable run record %s", run_id)
                continue
            sign_off = run.sign_off
            summaries.append(
                RunSummary(
                    run_id=run.run_id,
                    scheme_name=run.mandate.mandate.scheme_name,
                    category=run.mandate.mandate.category,
                    as_of=run.as_of,
                    status=run.result.status,
                    created_at=run.created_at,
                    review_items=len(run.result.review_items),
                    signed_by=sign_off.reviewer if sign_off else None,
                    decision=sign_off.decision if sign_off else None,
                )
            )
        return summaries

    def sign_off(
        self,
        run_id: str,
        reviewer: str,
        decision: Literal["approved", "escalated"],
        acknowledged_review_ids: list[str],
        note: str = "",
    ) -> Run:
        """GATE 2 + GATE 3. Every review item must be acknowledged by name before the run is signed."""
        run = self.get_run(run_id)
        if run.sign_off is not None:
            raise GateError(f"Run {run_id} was already signed off by {run.sign_off.reviewer}")
        required = {item.review_id for item in run.result.review_items}
        missing = sorted(required - set(acknowledged_review_ids))
        if missing:
            raise GateError(f"Gate 2: acknowledge every review item before sign-off; missing {missing}")
        unknown = sorted(set(acknowledged_review_ids) - required)
        if unknown:
            raise GateError(f"Unknown review ids {unknown}")
        sign_off = SignOff(
            reviewer=reviewer.strip(),
            decision=decision,
            note=note,
            signed_at=self._clock(),
            acknowledged_review_ids=sorted(required),
        )
        signed = run.model_copy(update={"sign_off": sign_off})
        self._store.save("runs", run_id, signed)
        return signed

    def explain(self, run_id: str, question: str) -> Answer:
        return self._reports.answer(self.get_run(run_id), question)

    # --- P1 -----------------------------------------------------------------------------

    def screen(
        self,
        run_id: str,
        candidates_csv: str,
        weight_pct: Decimal,
        funding: Funding = Funding.PRO_RATA,
        sort_by: SortBy = "headroom",
    ) -> ScreeningResult:
        if not Decimal(0) < weight_pct <= Decimal(100):
            raise InputValidationError(
                [ValidationIssue(code="invalid_number", message="weight_pct must be in (0, 100]", field="weight_pct")]
            )
        run = self.get_run(run_id)
        classifier = self._classifier(self._pinned_master(run))
        known = _known(run.classification)
        baseline = self._baseline(run)

        results: list[CandidateResult] = []
        for isin, name in parse_candidates(candidates_csv):
            resolution = classifier.resolve_security(isin, name)
            security = resolution.security
            if security is None:
                results.append(
                    CandidateResult(
                        isin=isin,
                        name=name,
                        bucket=None,
                        sector=None,
                        classification_method=resolution.method,
                        eligible=False,
                        reasons=[f"Cannot be classified ({resolution.note}); a human must identify it first."],
                        blocking_constraint_ids=["UNCLASSIFIED"],
                        post_trade_headroom_pp=None,
                    )
                )
                continue
            trade = Trade(action="buy", isin=security.isin, name=security.name, pct_of_nav=weight_pct)
            try:
                hypothetical = apply_trades(run.holdings, [trade], funding)
            except TradeError as exc:
                eligible, reasons, blocking, headroom = False, [str(exc)], ["FUNDING"], None
            else:
                after = self._reconcile(classifier.classify(hypothetical, known), run)
                verdict = assess_eligibility(baseline, after)
                eligible, reasons = verdict.eligible, verdict.reasons
                blocking, headroom = verdict.blocking_constraint_ids, verdict.headroom_pp
            results.append(
                CandidateResult(
                    isin=security.isin,
                    name=security.name,
                    bucket=security.bucket,
                    sector=security.sector,
                    classification_method=resolution.method,
                    eligible=eligible,
                    reasons=reasons,
                    blocking_constraint_ids=blocking,
                    post_trade_headroom_pp=headroom,
                )
            )

        if sort_by == "name":
            results.sort(key=lambda c: c.name.casefold())
        else:
            results.sort(key=lambda c: (not c.eligible, -(c.post_trade_headroom_pp or Decimal(-1000)), c.name))
        screening = ScreeningResult(
            scenario_id=new_id(),
            run_id=run_id,
            weight_pct=weight_pct,
            funding=funding,
            sort_by=sort_by,
            candidates=results,
            note=f"{SCENARIO_NOTE} Ordering is by the displayed '{sort_by}' field only, never by a forecast.",
        )
        self._store.save("scenarios", screening.scenario_id, screening)
        return screening

    def what_if(self, run_id: str, trades_csv: str, funding: Funding = Funding.CASH) -> WhatIfResult:
        run = self.get_run(run_id)
        trades = parse_trades(trades_csv)
        classifier = self._classifier(self._pinned_master(run))
        for trade in trades:
            if trade.action == "buy" and classifier.master.get(trade.isin) is None:
                raise TradeError(
                    f"{trade.isin} is not in security master {classifier.master.version}; cannot simulate it"
                )

        before = self._baseline(run)
        hypothetical = apply_trades(run.holdings, trades, funding)
        after = self._reconcile(classifier.classify(hypothetical, _known(run.classification)), run)
        deltas = compare_results(before, after)
        result = WhatIfResult(
            scenario_id=new_id(),
            run_id=run_id,
            trades=trades,
            funding=funding,
            before_status=before.status,
            after_status=after.status,
            deltas=deltas,
            new_breaches=[
                d.constraint_id for d in deltas if d.after_status == "BREACH" and d.before_status != "BREACH"
            ],
            resolved_breaches=[
                d.constraint_id for d in deltas if d.before_status == "BREACH" and d.after_status == "PASS"
            ],
            after_findings=after.findings,
            after_review_items=after.review_items,
            note=SCENARIO_NOTE,
        )
        self._store.save("scenarios", result.scenario_id, result)
        return result

    # --- helpers ------------------------------------------------------------------------

    def _classifier(self, master: SecurityMaster) -> Classifier:
        return Classifier(master, self._resolver, self._settings.identity_min_confidence)

    def _reconcile(self, classified: list[ClassifiedHolding], run: Run) -> EngineResult:
        return reconcile(classified, run.constraints, near_threshold_band=self._settings.near_threshold_band_pp)

    def _baseline(self, run: Run) -> EngineResult:
        return self._reconcile(run.classification, run)

    def _pinned_master(self, run: Run) -> SecurityMaster:
        master = self._references.security_master(run.as_of)
        if master.sha256 != run.pin("security_master").sha256:
            raise ReproducibilityError(
                f"Security master {master.version} changed after run {run.run_id}; start a new compliance run"
            )
        return master


def _known(classification: list[ClassifiedHolding]) -> dict[str, ClassifiedHolding]:
    return {item.holding.issuer_key: item for item in classification}


def _mandate_from(proposal: MandateProposal) -> Mandate:
    missing = [
        name for name, value in (("scheme_name", proposal.scheme_name), ("category", proposal.category)) if not value
    ]
    if missing:
        raise GateError(f"The proposal is incomplete ({', '.join(missing)}); supply a corrected mandate at Gate 1")
    try:
        return Mandate(
            scheme_name=proposal.scheme_name,
            category=proposal.category,
            category_citation=proposal.category_citation,
            benchmark=proposal.benchmark,
            sector=proposal.sector,
            sector_citation=proposal.sector_citation,
            self_imposed_limits=proposal.self_imposed_limits,
        )
    except ValidationError as exc:
        raise GateError(f"The proposal does not form a valid mandate: {exc.errors()[0]['msg']}") from exc
