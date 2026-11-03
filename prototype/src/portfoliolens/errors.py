"""Expected, user-facing failures. Anything else is a bug and should surface as one."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel


class ValidationIssue(BaseModel):
    code: str
    message: str
    row: int | None = None
    field: str | None = None


class PortfolioLensError(Exception):
    """Base class for failures the caller can act on."""


class ConfigError(PortfolioLensError):
    pass


class InputValidationError(PortfolioLensError):
    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = list(issues)
        super().__init__(
            f"{len(self.issues)} input validation issue(s): " + "; ".join(i.message for i in self.issues[:5])
        )


class NotFoundError(PortfolioLensError):
    pass


class GateError(PortfolioLensError):
    """A human gate has not been satisfied, so the pipeline refuses to continue."""


class RuleCoverageError(PortfolioLensError):
    """The pinned rulebook or reference data cannot support a verdict."""


class ReproducibilityError(PortfolioLensError):
    """Reference data changed after a run was recorded."""


class TradeError(PortfolioLensError):
    pass


class LLMError(PortfolioLensError):
    pass
