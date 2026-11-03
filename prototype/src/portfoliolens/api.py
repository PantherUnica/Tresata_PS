"""HTTP API matching the PRD's proposed endpoints. Interactive docs are served at /docs."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from portfoliolens import __version__
from portfoliolens.agents.report import DISCLAIMER
from portfoliolens.backtest import report_payload, run_backtest, write_report
from portfoliolens.config import Settings
from portfoliolens.domain import (
    Answer,
    ConfirmedMandate,
    Funding,
    InputSummary,
    Mandate,
    MandateProposal,
    Run,
    RunSummary,
    ScreeningResult,
    WhatIfResult,
)
from portfoliolens.errors import (
    ConfigError,
    GateError,
    InputValidationError,
    LLMError,
    NotFoundError,
    PortfolioLensError,
    ReproducibilityError,
    RuleCoverageError,
    TradeError,
)
from portfoliolens.ingest import MAX_HOLDINGS_BYTES, MAX_SID_BYTES
from portfoliolens.pipeline import Coordinator
from portfoliolens.samples import Sample, SampleInfo, list_samples, load_sample

UI_DIR = Path(__file__).parent / "ui"

_STATUS_BY_ERROR: tuple[tuple[type[PortfolioLensError], int], ...] = (
    (InputValidationError, 422),
    (NotFoundError, 404),
    (GateError, 409),
    (ReproducibilityError, 409),
    (RuleCoverageError, 422),
    (TradeError, 422),
    (LLMError, 502),
    (ConfigError, 500),
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidateBody(_Body):
    holdings_csv: str = Field(max_length=MAX_HOLDINGS_BYTES)
    sid_text: str | None = Field(default=None, max_length=MAX_SID_BYTES)


class ExtractBody(_Body):
    sid_text: str = Field(max_length=MAX_SID_BYTES)


class ConfirmBody(_Body):
    proposal_id: str
    reviewer: str = Field(min_length=1, max_length=120)
    corrected_mandate: Mandate | None = None
    acknowledge_issues: bool = False


class RunBody(_Body):
    mandate_id: str
    holdings_csv: str = Field(max_length=MAX_HOLDINGS_BYTES)
    as_of: date


class ExplainBody(_Body):
    run_id: str
    question: str = Field(min_length=1, max_length=1000)


class SignOffBody(_Body):
    reviewer: str = Field(min_length=1, max_length=120)
    decision: Literal["approved", "escalated"]
    acknowledged_review_ids: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=2000)


class ScreenBody(_Body):
    run_id: str
    candidates_csv: str = Field(max_length=MAX_HOLDINGS_BYTES)
    weight_pct: Decimal = Field(gt=0, le=100)
    funding: Funding = Funding.PRO_RATA
    sort_by: Literal["name", "headroom"] = "headroom"


class WhatIfBody(_Body):
    run_id: str
    trades_csv: str = Field(max_length=MAX_HOLDINGS_BYTES)
    funding: Funding = Funding.CASH


def create_app(coordinator: Coordinator | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    coord = coordinator or Coordinator.from_settings(settings)
    app = FastAPI(title="PortfolioLens AI", version=__version__, description=DISCLAIMER)

    @app.exception_handler(PortfolioLensError)
    async def _domain_error(_: Request, exc: PortfolioLensError) -> JSONResponse:
        status = next((code for error_type, code in _STATUS_BY_ERROR if isinstance(exc, error_type)), 400)
        body: dict[str, object] = {"error": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, InputValidationError):
            body["issues"] = [issue.model_dump() for issue in exc.issues]
        return JSONResponse(status_code=status, content=body)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "llm": settings.llm_label}

    @app.post("/checks/validate")
    def validate(body: ValidateBody) -> InputSummary:
        return coord.validate_inputs(body.holdings_csv, body.sid_text)

    @app.post("/checks/extract-mandate")
    def extract_mandate(body: ExtractBody) -> MandateProposal:
        return coord.propose_mandate(body.sid_text)

    @app.post("/checks/confirm-mandate")
    def confirm_mandate(body: ConfirmBody) -> ConfirmedMandate:
        return coord.confirm_mandate(body.proposal_id, body.reviewer, body.corrected_mandate, body.acknowledge_issues)

    @app.post("/checks/run")
    def run_check(body: RunBody) -> Run:
        return coord.run_check(body.mandate_id, body.holdings_csv, body.as_of)

    @app.post("/checks/explain")
    def explain(body: ExplainBody) -> Answer:
        return coord.explain(body.run_id, body.question)

    @app.get("/checks/runs")
    def list_runs() -> list[RunSummary]:
        return coord.list_runs()

    @app.get("/checks/runs/{run_id}")
    def get_run(run_id: str) -> Run:
        return coord.get_run(run_id)

    @app.get("/checks/runs/{run_id}/report", response_class=PlainTextResponse)
    def get_report(run_id: str) -> str:
        return coord.get_run(run_id).report.markdown

    @app.post("/checks/runs/{run_id}/sign-off")
    def sign_off(run_id: str, body: SignOffBody) -> Run:
        return coord.sign_off(run_id, body.reviewer, body.decision, body.acknowledged_review_ids, body.note)

    @app.post("/screen")
    def screen(body: ScreenBody) -> ScreeningResult:
        return coord.screen(body.run_id, body.candidates_csv, body.weight_pct, body.funding, body.sort_by)

    @app.post("/whatif")
    def what_if(body: WhatIfBody) -> WhatIfResult:
        return coord.what_if(body.run_id, body.trades_csv, body.funding)

    cases_dir = settings.data_dir / "cases"

    @app.get("/samples")
    def samples() -> list[SampleInfo]:
        return list_samples(cases_dir)

    @app.get("/samples/{name}")
    def sample(name: str) -> Sample:
        return load_sample(cases_dir, name)

    backtest_dir = settings.output_dir / "backtest"

    @app.get("/backtest/latest")
    def backtest_latest() -> dict[str, Any] | None:
        path = backtest_dir / "results.json"
        if not path.is_file():
            return None
        latest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return latest

    @app.post("/backtest/run")
    def backtest_run() -> dict[str, Any]:
        runner = Coordinator.from_settings(settings, store_dir=backtest_dir / "store")
        report = run_backtest(runner, cases_dir, settings.llm_label)
        write_report(report, backtest_dir)
        return report_payload(report)

    if UI_DIR.is_dir():
        # Mounted last so it never shadows an API route.
        app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")

    return app
