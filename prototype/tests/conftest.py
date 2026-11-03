from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from portfoliolens.agents.llm import LLMClient
from portfoliolens.agents.mandate import HeuristicMandateExtractor
from portfoliolens.agents.report import ReportWriter
from portfoliolens.classification import HeuristicIdentityResolver
from portfoliolens.config import PROJECT_ROOT, Settings
from portfoliolens.pipeline import Coordinator
from portfoliolens.reference import ReferenceLibrary
from portfoliolens.store import JsonStore


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(output_dir=tmp_path)


@pytest.fixture
def coordinator(settings: Settings) -> Coordinator:
    return Coordinator.from_settings(settings)


@pytest.fixture
def references() -> ReferenceLibrary:
    return ReferenceLibrary(PROJECT_ROOT / "data")


@pytest.fixture
def coordinator_with_report_model(
    settings: Settings, references: ReferenceLibrary
) -> Callable[[LLMClient], Coordinator]:
    def build(client: LLMClient) -> Coordinator:
        return Coordinator(
            settings=settings,
            references=references,
            store=JsonStore(settings.output_dir / "store"),
            extractor=HeuristicMandateExtractor(),
            resolver=HeuristicIdentityResolver(),
            reports=ReportWriter(client),
        )

    return build
