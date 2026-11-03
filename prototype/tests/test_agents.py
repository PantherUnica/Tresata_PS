import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal

import httpx
import pytest

from helpers import CASES, ScriptedLLM, case_text
from portfoliolens.agents import llm as llm_module
from portfoliolens.agents.identity import LLMIdentityResolver
from portfoliolens.agents.llm import LLMClient, OpenAIClient, OpenRouterClient, build_llm_client, parse_json_object
from portfoliolens.agents.mandate import HeuristicMandateExtractor, LLMMandateExtractor, parse_model_mandate
from portfoliolens.agents.scope import is_out_of_scope
from portfoliolens.classification import Classifier, HeuristicIdentityResolver
from portfoliolens.config import LLMMode, Settings
from portfoliolens.domain import Comparator, Metric, RunStatus, SchemeCategory
from portfoliolens.errors import LLMError
from portfoliolens.ingest import load_sid
from portfoliolens.pipeline import Coordinator
from portfoliolens.reference import ReferenceLibrary

AS_OF = date(2026, 9, 30)


# --- mandate extraction -----------------------------------------------------------------

EXTRACTABLE = [
    path.parent.name
    for path in sorted(CASES.glob("*/case.json"))
    if (path.parent / "sid.md").exists() and not path.parent.name.startswith("13_")
]


@pytest.mark.parametrize("case_dir", EXTRACTABLE)
def test_offline_extractor_recovers_the_ground_truth(case_dir: str) -> None:
    truth = json.loads(case_text(case_dir, "case.json"))["ground_truth_mandate"]
    proposal = HeuristicMandateExtractor().extract(load_sid(case_text(case_dir, "sid.md")), "p")
    assert proposal.category == SchemeCategory(truth["category"])
    assert proposal.scheme_name == truth["scheme_name"]
    assert proposal.category_citation is not None
    assert {(l.metric.value, l.comparator.value, l.threshold) for l in proposal.self_imposed_limits} == {
        (l["metric"], l["comparator"], Decimal(l["threshold"])) for l in truth.get("self_imposed_limits", [])
    }


def test_offline_extractor_reads_a_limit_written_in_words() -> None:
    proposal = HeuristicMandateExtractor().extract(load_sid(case_text("13_limit_in_words", "sid.md")), "p")
    assert [(limit.metric, limit.comparator, limit.threshold) for limit in proposal.self_imposed_limits] == [
        (Metric.SINGLE_ISSUER_PCT, Comparator.MAX, Decimal("8.5"))
    ]


def test_model_values_without_a_verbatim_quote_are_discarded() -> None:
    sid = load_sid(case_text("01_large_cap_compliant", "sid.md"))
    raw = {
        "scheme_name": {"value": "Synthetic Bluechip Fund", "quote": "Name of the Scheme: Synthetic Bluechip Fund"},
        "category": {"value": "large_cap", "quote": "a fund that mostly buys big companies"},
        "self_imposed_limits": [{"metric": "equity_pct", "comparator": "min", "threshold": 90, "quote": "invented"}],
    }
    proposal = parse_model_mandate(raw, sid, "p", "llm:test")
    assert proposal.scheme_name == "Synthetic Bluechip Fund"
    assert proposal.category is None
    assert proposal.self_imposed_limits == []
    assert any("no verbatim quote" in issue for issue in proposal.issues)


def test_model_category_outside_the_enum_is_discarded() -> None:
    sid = load_sid(case_text("01_large_cap_compliant", "sid.md"))
    quote = "An open ended equity scheme predominantly investing in large cap stocks"
    proposal = parse_model_mandate({"category": {"value": "bluechip", "quote": quote}}, sid, "p", "llm:test")
    assert proposal.category is None
    assert any("not a recognised SEBI category" in issue for issue in proposal.issues)


def test_threshold_written_in_words_is_kept_but_flagged_for_the_reviewer() -> None:
    sid = load_sid(case_text("13_limit_in_words", "sid.md"))
    quote = "will not allow exposure to any single company to exceed eight and a half per cent"
    raw = {
        "self_imposed_limits": [{"metric": "single_issuer_pct", "comparator": "max", "threshold": 8.5, "quote": quote}]
    }
    proposal = parse_model_mandate(raw, sid, "p", "llm:test")
    assert [(l.metric, l.comparator, l.threshold) for l in proposal.self_imposed_limits] == [
        (Metric.SINGLE_ISSUER_PCT, Comparator.MAX, Decimal("8.5"))
    ]
    assert any("not written as digits" in issue for issue in proposal.issues)


def test_extractor_falls_back_to_the_heuristic_when_the_model_fails() -> None:
    extractor = LLMMandateExtractor(ScriptedLLM(LLMError("timeout")), HeuristicMandateExtractor())
    proposal = extractor.extract(load_sid(case_text("01_large_cap_compliant", "sid.md")), "p")
    assert proposal.category is SchemeCategory.LARGE_CAP
    assert proposal.extractor.endswith("(fallback)")
    assert "Model extraction failed" in proposal.issues[0]


# --- identity resolution ----------------------------------------------------------------


def test_ambiguous_name_stays_unresolved_offline(references: ReferenceLibrary) -> None:
    classifier = Classifier(references.security_master(AS_OF), HeuristicIdentityResolver(), 0.85)
    assert classifier.resolve_security(None, "Surya Power").security is None
    assert classifier.resolve_security(None, "Aravali Bank Limited").security is not None


def test_model_may_only_pick_from_the_shortlist(references: ReferenceLibrary) -> None:
    master = references.security_master(AS_OF)
    grid = next(s for s in master.securities.values() if s.name == "Surya Power Grid Ltd")

    def resolve(response: dict[str, object]) -> object:
        resolver = LLMIdentityResolver(ScriptedLLM(response), HeuristicIdentityResolver())
        return Classifier(master, resolver, 0.85).resolve_security(None, "Surya Power")

    picked = resolve({"isin": grid.isin, "confidence": 0.9, "reason": "grid operator"})
    assert picked.security == grid and picked.method == "llm_identity"
    assert resolve({"isin": "US0378331005", "confidence": 0.99, "reason": "x"}).security is None
    assert resolve({"isin": grid.isin, "confidence": 0.4, "reason": "unsure"}).security is None
    assert resolve({"isin": None, "confidence": 0, "reason": "ambiguous"}).security is None


# --- report agent -----------------------------------------------------------------------

GOOD = {
    "sections": [
        {"text": "Large-cap allocation is 88.00% against a minimum of 80%.", "cited_ids": ["FND-SEBI-CAT-LARGE-CAP-01"]}
    ]
}


@pytest.mark.parametrize(
    ("response", "problem"),
    [
        (
            {"sections": [{"text": "Large-cap allocation is 91%.", "cited_ids": ["F-LARGE-CAP-PCT"]}]},
            "numbers not present",
        ),
        ({"sections": [{"text": "Large-cap allocation is 88%.", "cited_ids": ["F-MADE-UP"]}]}, "unknown ids"),
        ({"sections": [{"text": "Large-cap allocation is 88%.", "cited_ids": []}]}, "cites nothing"),
        ({"sections": [{"text": "We recommend adding Aravali.", "cited_ids": ["F-NAV"]}]}, "advice"),
        ({"text": "not the requested shape"}, "no sections"),
    ],
)
def test_ungrounded_model_narrative_is_rejected_and_raised_for_review(
    coordinator_with_report_model: Callable[[LLMClient], Coordinator], response: dict[str, object], problem: str
) -> None:
    coordinator = coordinator_with_report_model(ScriptedLLM(response))
    run = _run_case(coordinator, "01_large_cap_compliant")
    assert run.report.narrative_source == "template"
    assert run.report.llm_output_rejected and problem in (run.report.llm_note or "")
    assert [r.kind for r in run.result.review_items] == ["report_rejected"]
    assert run.result.status is RunStatus.NEEDS_REVIEW


def test_grounded_model_narrative_is_accepted(
    coordinator_with_report_model: Callable[[LLMClient], Coordinator],
) -> None:
    run = _run_case(coordinator_with_report_model(ScriptedLLM(GOOD)), "01_large_cap_compliant")
    assert run.report.narrative_source == "llm"
    assert "88.00%" in run.report.markdown
    assert run.result.status is RunStatus.COMPLIANT


def test_forecast_questions_never_reach_the_model(
    coordinator_with_report_model: Callable[[LLMClient], Coordinator],
) -> None:
    client = ScriptedLLM(GOOD)
    coordinator = coordinator_with_report_model(client)
    run = _run_case(coordinator, "01_large_cap_compliant")
    answer = coordinator.explain(run.run_id, "Should we buy more large caps next month?")
    assert answer.declined and answer.source == "scope_guard"
    assert len(client.calls) == 1  # only the report call


def test_scope_guard() -> None:
    assert is_out_of_scope("Predict the drift")
    assert is_out_of_scope("which stocks should we add? recommend some")
    assert not is_out_of_scope("What is the large-cap allocation?")


def _run_case(coordinator: Coordinator, case_dir: str):  # type: ignore[no-untyped-def]
    proposal = coordinator.propose_mandate(case_text(case_dir, "sid.md"))
    confirmed = coordinator.confirm_mandate(proposal.proposal_id, "tester")
    return coordinator.run_check(confirmed.mandate_id, case_text(case_dir, "holdings.csv"), AS_OF)


# --- OpenRouter client ------------------------------------------------------------------


def test_json_is_extracted_from_fenced_model_output() -> None:
    assert parse_json_object('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json_object("no json here")
    with pytest.raises(LLMError):
        parse_json_object("[1, 2]")


def test_client_retries_rate_limits_then_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module.time, "sleep", lambda _: None)
    responses = iter(
        [
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]}),
        ]
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(responses)

    client = OpenRouterClient(
        api_key="test-key",
        model="test/model",
        base_url="https://example.invalid/api/v1",
        timeout_s=5,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.complete_json("system", "user") == {"ok": True}
    assert len(seen) == 2
    assert seen[0].headers["Authorization"] == "Bearer test-key"
    assert json.loads(seen[0].content)["temperature"] == 0


def test_openai_mode_builds_configured_gpt_mini_client() -> None:
    settings = Settings(
        llm_mode=LLMMode.OPENAI,
        openai_api_key="openai-test-key",
        openai_model="gpt-4o-mini",
    )
    client = build_llm_client(settings)
    assert isinstance(client, OpenAIClient)
    assert client.name == "gpt-4o-mini"
    assert settings.llm_label == "openai:gpt-4o-mini"


def test_client_fails_fast_on_client_errors() -> None:
    client = OpenRouterClient(
        api_key="k",
        model="m",
        base_url="https://example.invalid",
        timeout_s=5,
        http=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401, text="bad key"))),
    )
    with pytest.raises(LLMError, match="HTTP 401"):
        client.complete_json("s", "u")


def test_openai_client_uses_platform_key_and_chat_completions() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    client = OpenAIClient(
        api_key="openai-test-key",
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        timeout_s=5,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.complete_json("system", "user") == {"ok": True}
    assert seen[0].url == "https://api.openai.com/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer openai-test-key"
    assert json.loads(seen[0].content)["model"] == "gpt-4o-mini"
