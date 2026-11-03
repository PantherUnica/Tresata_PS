"""Identity agent: when a holding has no ISIN and the name is ambiguous, a model picks from real candidates."""

from __future__ import annotations

from collections.abc import Sequence

from portfoliolens.agents.llm import LLMClient
from portfoliolens.agents.prompts import IDENTITY_SYSTEM, identity_user
from portfoliolens.classification import HeuristicIdentityResolver, IdentityDecision, ScoredCandidate
from portfoliolens.errors import LLMError


class LLMIdentityResolver:
    """Deterministic match first; the model may only choose among shortlisted candidates, or abstain."""

    def __init__(self, client: LLMClient, fallback: HeuristicIdentityResolver) -> None:
        self._client = client
        self._fallback = fallback

    @property
    def name(self) -> str:
        return f"llm:{self._client.name}"

    def resolve(self, holding_name: str, candidates: Sequence[ScoredCandidate]) -> IdentityDecision:
        quick = self._fallback.resolve(holding_name, candidates)
        if quick.isin is not None or not candidates:
            return quick
        try:
            raw = self._client.complete_json(
                IDENTITY_SYSTEM, identity_user(holding_name, [(c.security.isin, c.security.name) for c in candidates])
            )
        except LLMError as exc:
            return IdentityDecision(None, quick.confidence, f"{quick.reason}; model unavailable ({exc})", "name_match")

        reason = str(raw.get("reason", "")).strip()[:200] or "no reason given"
        choice = raw.get("isin")
        if choice in (None, "", "null"):
            return IdentityDecision(None, 0.0, f"model abstained: {reason}", "llm_identity")
        if choice not in {c.security.isin for c in candidates}:
            return IdentityDecision(None, 0.0, "model chose an ISIN outside the shortlist; rejected", "llm_identity")
        return IdentityDecision(str(choice), _clamp(raw.get("confidence")), f"model: {reason}", "llm_identity")


def _clamp(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return min(max(number, 0.0), 1.0)
