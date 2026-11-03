"""Shared test helpers: fixture paths and a scripted stand-in for the LLM client."""

from __future__ import annotations

from typing import Any

from portfoliolens.config import PROJECT_ROOT

CASES = PROJECT_ROOT / "data" / "cases"


def case_text(case_dir: str, name: str) -> str:
    return (CASES / case_dir / name).read_text(encoding="utf-8")


class ScriptedLLM:
    """Returns (or raises) the scripted responses in order and records every call."""

    name = "scripted-model"

    def __init__(self, *responses: dict[str, Any] | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        self.calls.append((system, user))
        if not self._responses:
            raise AssertionError("ScriptedLLM was called more times than scripted")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
