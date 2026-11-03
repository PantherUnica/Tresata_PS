from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from helpers import ScriptedLLM, case_text
from portfoliolens.agents.rule_retrieval import RuleRetrievalAgent
from portfoliolens.api import create_app
from portfoliolens.backtest import run_backtest
from portfoliolens.config import PROJECT_ROOT, Settings
from portfoliolens.domain import CapBucket, Mandate, Metric, Run, SchemeCategory
from portfoliolens.errors import GateError, LLMError, NotFoundError, RuleCoverageError, TradeError
from portfoliolens.ingest import sha256_text
from portfoliolens.pipeline import Coordinator
from portfoliolens.reference import ReferenceLibrary
from portfoliolens.rules import retrieve_constraints
from portfoliolens.store import JsonStore

AS_OF = date(2026, 9, 30)
UNRECOGNISED_SID = "Name of the Scheme: Mystery Fund\nThe fund invests in whatever looks good.\n"


def run_case(coordinator: Coordinator, case_dir: str) -> Run:
    proposal = coordinator.propose_mandate(case_text(case_dir, "sid.md"))
    confirmed = coordinator.confirm_mandate(proposal.proposal_id, "tester")
    return coordinator.run_check(confirmed.mandate_id, case_text(case_dir, "holdings.csv"), AS_OF)


# --- gates ------------------------------------------------------------------------------


def test_gate1_refuses_a_proposal_with_open_issues(coordinator: Coordinator) -> None:
    proposal = coordinator.propose_mandate(UNRECOGNISED_SID)
    assert proposal.category is None and proposal.issues
    with pytest.raises(GateError, match="open issues"):
        coordinator.confirm_mandate(proposal.proposal_id, "tester")
    with pytest.raises(GateError, match="incomplete"):
        coordinator.confirm_mandate(proposal.proposal_id, "tester", acknowledge_issues=True)
    corrected = Mandate(scheme_name="Mystery Fund", category=SchemeCategory.FLEXI_CAP)
    confirmed = coordinator.confirm_mandate(proposal.proposal_id, "tester", corrected=corrected)
    assert confirmed.corrected_by_reviewer and confirmed.confirmed_by == "tester"


def test_gate1_requires_a_reviewer_name(coordinator: Coordinator) -> None:
    proposal = coordinator.propose_mandate(case_text("01_large_cap_compliant", "sid.md"))
    with pytest.raises(GateError):
        coordinator.confirm_mandate(proposal.proposal_id, "   ")


def test_sign_off_requires_every_review_item_and_happens_once(coordinator: Coordinator) -> None:
    run = run_case(coordinator, "07_ambiguous_holding")
    review_ids = [item.review_id for item in run.result.review_items]
    assert review_ids == ["RV-01", "RV-02"]
    with pytest.raises(GateError, match="missing"):
        coordinator.sign_off(run.run_id, "lead", "approved", ["RV-01"])
    with pytest.raises(GateError, match="Unknown"):
        coordinator.sign_off(run.run_id, "lead", "approved", [*review_ids, "RV-99"])
    signed = coordinator.sign_off(run.run_id, "lead", "escalated", review_ids, note="identify Surya Power")
    assert signed.sign_off is not None and signed.sign_off.decision == "escalated"
    assert coordinator.get_run(run.run_id).sign_off == signed.sign_off
    with pytest.raises(GateError, match="already signed"):
        coordinator.sign_off(run.run_id, "lead", "approved", review_ids)


# --- reproducibility & reference versioning ---------------------------------------------


def test_run_pins_versions_and_input_hashes(coordinator: Coordinator) -> None:
    run = run_case(coordinator, "02_large_cap_breach")
    assert {(p.kind, p.version) for p in run.pins} == {
        ("rulebook", "2026-02"),
        ("security_master", "2026H2"),
        ("engine", "1.0.0"),
    }
    assert run.holdings_sha256 == sha256_text(case_text("02_large_cap_breach", "holdings.csv"))
    assert run.mandate.sid_sha256 == sha256_text(case_text("02_large_cap_breach", "sid.md"))
    assert coordinator.store.path_for("runs", run.run_id, ".md").is_file()
    assert run.rule_retrieval is not None
    assert run.rule_retrieval.version == run.pin("rulebook").version
    assert run.rule_retrieval.as_of == run.as_of


def test_compliance_run_uses_the_fixed_langgraph_workflow(coordinator: Coordinator) -> None:
    nodes = coordinator.run_workflow_nodes
    assert {
        "load_confirmed_inputs",
        "retrieve_rules_and_references",
        "classify_holdings",
        "reconcile_constraints",
        "write_cited_report",
        "persist_run",
    } <= set(nodes)


def test_rulebook_versions_are_selected_by_as_of_date(references: ReferenceLibrary) -> None:
    multi_cap = Mandate(scheme_name="X", category=SchemeCategory.MULTI_CAP)
    old, _ = references.rulebook(date(2019, 6, 30))
    new, _ = references.rulebook(AS_OF)
    assert old.version == "2017-10" and new.version == "2026-02"
    old_metrics = {c.metric for c in retrieve_constraints(old, multi_cap) if c.source_kind == "SEBI"}
    new_metrics = {c.metric for c in retrieve_constraints(new, multi_cap) if c.source_kind == "SEBI"}
    assert Metric.SMALL_CAP_PCT not in old_metrics
    assert {Metric.LARGE_CAP_PCT, Metric.MID_CAP_PCT, Metric.SMALL_CAP_PCT} <= new_metrics


def test_rule_rag_retrieval_is_scoped_to_the_effective_version(references: ReferenceLibrary) -> None:
    mandate = Mandate(scheme_name="X", category=SchemeCategory.MULTI_CAP)
    rulebook, sha256 = references.rulebook(AS_OF)
    constraints, audit = RuleRetrievalAgent().retrieve(rulebook, sha256, mandate, AS_OF)
    ids = {constraint.constraint_id for constraint in constraints}
    assert audit.version == "2026-02"
    assert audit.rulebook_sha256 == sha256
    assert audit.as_of == AS_OF
    assert audit.method == "langchain_lexical"
    assert set(audit.retrieved_rule_ids) == set(audit.applied_rule_ids)
    assert {
        "SEBI-CAT-MULTI-CAP-01",
        "SEBI-CAT-MULTI-CAP-02",
        "SEBI-CAT-MULTI-CAP-03",
        "SEBI-CAT-MULTI-CAP-04",
    } <= ids
    assert "SEBI-CAT-FLEXI-CAP-01" not in ids


def test_rule_rag_llm_can_only_reorder_the_complete_retrieved_set(
    references: ReferenceLibrary,
) -> None:
    rulebook, sha256 = references.rulebook(AS_OF)
    mandate = Mandate(scheme_name="X", category=SchemeCategory.LARGE_CAP)
    baseline, _ = RuleRetrievalAgent().retrieve(rulebook, sha256, mandate, AS_OF)
    ids = [constraint.constraint_id for constraint in baseline]
    client = ScriptedLLM({"ordered_rule_ids": list(reversed(ids))})
    constraints, audit = RuleRetrievalAgent(client).retrieve(rulebook, sha256, mandate, AS_OF)
    assert [constraint.constraint_id for constraint in constraints] == list(reversed(ids))
    assert audit.method == "langchain_retrieval_llm_rerank"
    assert client.calls and "Retrieved rule records" in client.calls[0][1]


def test_rule_rag_rejects_model_rule_omissions_and_uses_retriever_order(
    references: ReferenceLibrary,
) -> None:
    rulebook, sha256 = references.rulebook(AS_OF)
    mandate = Mandate(scheme_name="X", category=SchemeCategory.MULTI_CAP)
    baseline, _ = RuleRetrievalAgent().retrieve(rulebook, sha256, mandate, AS_OF)
    client = ScriptedLLM({"ordered_rule_ids": [baseline[0].constraint_id]})
    constraints, audit = RuleRetrievalAgent(client).retrieve(rulebook, sha256, mandate, AS_OF)
    assert [constraint.constraint_id for constraint in constraints] == [
        constraint.constraint_id for constraint in baseline
    ]
    assert audit.method == "langchain_lexical"
    assert audit.fallback_reason is not None


def test_rule_rag_rejects_unknown_model_rule_ids(references: ReferenceLibrary) -> None:
    rulebook, sha256 = references.rulebook(AS_OF)
    mandate = Mandate(scheme_name="X", category=SchemeCategory.LARGE_CAP)
    baseline, _ = RuleRetrievalAgent().retrieve(rulebook, sha256, mandate, AS_OF)
    invalid_ids = [constraint.constraint_id for constraint in baseline]
    invalid_ids[-1] = "SEBI-UNKNOWN-RULE"
    client = ScriptedLLM({"ordered_rule_ids": invalid_ids})
    constraints, audit = RuleRetrievalAgent(client).retrieve(rulebook, sha256, mandate, AS_OF)
    assert [constraint.constraint_id for constraint in constraints] == [
        constraint.constraint_id for constraint in baseline
    ]
    assert audit.method == "langchain_lexical"
    assert audit.fallback_reason is not None


def test_rule_rag_uses_retriever_order_when_model_call_fails(references: ReferenceLibrary) -> None:
    rulebook, sha256 = references.rulebook(AS_OF)
    mandate = Mandate(scheme_name="X", category=SchemeCategory.LARGE_CAP)
    baseline, _ = RuleRetrievalAgent().retrieve(rulebook, sha256, mandate, AS_OF)
    client = ScriptedLLM(LLMError("simulated model failure"))
    constraints, audit = RuleRetrievalAgent(client).retrieve(rulebook, sha256, mandate, AS_OF)
    assert [constraint.constraint_id for constraint in constraints] == [
        constraint.constraint_id for constraint in baseline
    ]
    assert audit.method == "langchain_lexical"
    assert audit.fallback_reason == "Model reranking unavailable: simulated model failure"


def test_rule_rag_never_retrieves_rules_from_another_version(references: ReferenceLibrary) -> None:
    old, old_sha = references.rulebook(date(2019, 6, 30))
    mandate = Mandate(scheme_name="X", category=SchemeCategory.MULTI_CAP)
    constraints, audit = RuleRetrievalAgent().retrieve(old, old_sha, mandate, date(2019, 6, 30))
    assert audit.version == "2017-10"
    assert audit.as_of == date(2019, 6, 30)
    assert all(constraint.source_ref.endswith("@2017-10") for constraint in constraints)
    assert Metric.SMALL_CAP_PCT not in {constraint.metric for constraint in constraints}


def test_category_without_rules_in_force_cannot_get_a_verdict(references: ReferenceLibrary) -> None:
    old, _ = references.rulebook(date(2019, 6, 30))
    with pytest.raises(RuleCoverageError):
        retrieve_constraints(old, Mandate(scheme_name="X", category=SchemeCategory.FLEXI_CAP))


def test_security_master_is_dated(references: ReferenceLibrary) -> None:
    def orchid_bucket(as_of: date) -> CapBucket:
        master = references.security_master(as_of)
        return next(s.bucket for s in master.securities.values() if s.name == "Orchid Retail Ltd")

    assert orchid_bucket(date(2026, 3, 31)) is CapBucket.LARGE
    assert orchid_bucket(AS_OF) is CapBucket.MID
    with pytest.raises(RuleCoverageError):
        references.security_master(date(2025, 12, 31))


def test_store_rejects_ids_that_could_escape_its_directory(tmp_path: object) -> None:
    store = JsonStore(PROJECT_ROOT / "outputs" / "never-written")
    with pytest.raises(NotFoundError):
        store.load("runs", "../../secrets", Run)


# --- P1 ---------------------------------------------------------------------------------


def test_what_if_never_simulates_an_unclassified_purchase(coordinator: Coordinator) -> None:
    run = run_case(coordinator, "12_whatif_restores_compliance")
    trades = "action,isin,name,pct_of_nav\nbuy,INESY9999017,Nova Quantum Ltd,1\n"
    with pytest.raises(TradeError, match="not in security master"):
        coordinator.what_if(run.run_id, trades)


def test_screening_respects_the_manager_chosen_sort(coordinator: Coordinator) -> None:
    run = run_case(coordinator, "11_screen_large_cap_pool")
    result = coordinator.screen(
        run.run_id, case_text("11_screen_large_cap_pool", "candidates.csv"), Decimal(3), sort_by="name"
    )
    names = [c.name for c in result.candidates]
    assert names == sorted(names, key=str.casefold)
    assert "never by a forecast" in result.note


# --- backtest & API ---------------------------------------------------------------------


def test_backtest_engine_and_supported_extraction_cases_pass(coordinator: Coordinator) -> None:
    report = run_backtest(coordinator, PROJECT_ROOT / "data" / "cases", "offline")
    assert report.passed("ground_truth") == (13, 13)
    assert report.passed("extracted") == (13, 13)
    assert report.extraction_accuracy() == (24, 24)


def test_api_end_to_end(settings: Settings) -> None:
    client = TestClient(create_app(settings=settings))
    assert client.get("/health").json()["status"] == "ok"

    bad = client.post("/checks/validate", json={"holdings_csv": case_text("09_invalid_holdings", "holdings.csv")})
    assert bad.status_code == 422 and {i["code"] for i in bad.json()["issues"]} >= {"duplicate_isin", "bad_total"}

    proposal = client.post(
        "/checks/extract-mandate", json={"sid_text": case_text("08_near_threshold", "sid.md")}
    ).json()
    confirmed = client.post(
        "/checks/confirm-mandate", json={"proposal_id": proposal["proposal_id"], "reviewer": "api-tester"}
    ).json()
    run = client.post(
        "/checks/run",
        json={
            "mandate_id": confirmed["mandate_id"],
            "holdings_csv": case_text("08_near_threshold", "holdings.csv"),
            "as_of": "2026-09-30",
        },
    ).json()
    assert run["result"]["status"] == "NEEDS_REVIEW"
    assert "Compliance exception report" in client.get(f"/checks/runs/{run['run_id']}/report").text

    refused = client.post(f"/checks/runs/{run['run_id']}/sign-off", json={"reviewer": "lead", "decision": "approved"})
    assert refused.status_code == 409
    signed = client.post(
        f"/checks/runs/{run['run_id']}/sign-off",
        json={"reviewer": "lead", "decision": "approved", "acknowledged_review_ids": ["RV-01"]},
    )
    assert signed.status_code == 200 and signed.json()["sign_off"]["reviewer"] == "lead"

    assert client.get("/checks/runs/not-an-id").status_code == 404
    answer = client.post("/checks/explain", json={"run_id": run["run_id"], "question": "Forecast next quarter"})
    assert answer.json()["declined"] is True

    listed = client.get("/checks/runs").json()
    assert run["run_id"] in {r["run_id"] for r in listed}
    assert listed[0]["scheme_name"] and listed[0]["status"]


def test_ui_and_sample_endpoints(settings: Settings) -> None:
    client = TestClient(create_app(settings=settings))

    index = client.get("/")
    assert index.status_code == 200 and "PortfolioLens" in index.text
    assert client.get("/app.js").status_code == 200

    samples = client.get("/samples").json()
    assert len(samples) == 13 and {"name", "title", "kind"} <= samples[0].keys()

    one = client.get("/samples/06_self_imposed_floor").json()
    assert one["holdings_csv"] and one["sid_text"] and one["as_of"]
    assert client.get("/samples/does-not-exist").status_code == 404

    assert client.get("/backtest/latest").json() is None
    report = client.post("/backtest/run").json()
    assert report["summary"]["ground_truth"] == {"passed": 13, "total": 13}
    assert client.get("/backtest/latest").json()["summary"]["ground_truth"]["passed"] == 13
