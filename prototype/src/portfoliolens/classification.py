"""Holdings classification: a deterministic ISIN join, with identity resolution only at the margin."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal, Protocol

from portfoliolens.domain import ClassificationMethod, ClassifiedHolding, Holding, InstrumentType, Security
from portfoliolens.reference import SecurityMaster

_NAME_NOISE = frozenset(
    {"ltd", "limited", "pvt", "private", "the", "co", "company", "inc", "corp", "corporation", "plc"}
)
MIN_CANDIDATE_SCORE = 0.5


def normalize_company_name(name: str) -> str:
    return " ".join(token for token in re.findall(r"[a-z0-9]+", name.lower()) if token not in _NAME_NOISE)


@dataclass(frozen=True)
class ScoredCandidate:
    security: Security
    score: float


@dataclass(frozen=True)
class IdentityDecision:
    isin: str | None
    confidence: float
    reason: str
    method: Literal["name_match", "llm_identity"]


class IdentityResolver(Protocol):
    @property
    def name(self) -> str: ...

    def resolve(self, holding_name: str, candidates: Sequence[ScoredCandidate]) -> IdentityDecision: ...


def rank_candidates(holding_name: str, master: SecurityMaster, limit: int = 5) -> list[ScoredCandidate]:
    query = normalize_company_name(holding_name)
    if not query:
        return []
    scored = [
        ScoredCandidate(security, SequenceMatcher(None, query, normalize_company_name(security.name)).ratio())
        for security in master.securities.values()
    ]
    scored.sort(key=lambda candidate: (-candidate.score, candidate.security.isin))
    return [candidate for candidate in scored[:limit] if candidate.score >= MIN_CANDIDATE_SCORE]


class HeuristicIdentityResolver:
    """Accepts a name match only when it is both strong and clearly ahead of the runner-up."""

    name = "offline-heuristic"

    def __init__(self, accept_score: float = 0.92, min_gap: float = 0.08) -> None:
        self._accept_score = accept_score
        self._min_gap = min_gap

    def resolve(self, holding_name: str, candidates: Sequence[ScoredCandidate]) -> IdentityDecision:
        if not candidates:
            return IdentityDecision(None, 0.0, "no similar name in the security master", "name_match")
        top = candidates[0]
        runner_up = candidates[1].score if len(candidates) > 1 else 0.0
        if top.score >= self._accept_score and top.score - runner_up >= self._min_gap:
            return IdentityDecision(
                top.security.isin, round(top.score, 3), f"name similarity {top.score:.2f}", "name_match"
            )
        shortlist = ", ".join(f"{c.security.name} ({c.score:.2f})" for c in candidates[:3])
        return IdentityDecision(None, round(top.score, 3), f"ambiguous name; closest: {shortlist}", "name_match")


@dataclass(frozen=True)
class Resolution:
    security: Security | None
    method: ClassificationMethod
    confidence: float
    note: str


class Classifier:
    def __init__(self, master: SecurityMaster, resolver: IdentityResolver, min_confidence: float) -> None:
        self._master = master
        self._resolver = resolver
        self._min_confidence = min_confidence

    @property
    def master(self) -> SecurityMaster:
        return self._master

    def classify(
        self, holdings: Sequence[Holding], known: Mapping[str, ClassifiedHolding] | None = None
    ) -> list[ClassifiedHolding]:
        """Classify holdings, reusing earlier decisions by issuer so what-ifs never re-ask a model."""
        known = known or {}
        classified = []
        for holding in holdings:
            previous = known.get(holding.issuer_key)
            if previous is not None:
                classified.append(previous.model_copy(update={"holding": holding}))
            else:
                classified.append(self._classify_one(holding))
        return classified

    def resolve_security(self, isin: str | None, name: str) -> Resolution:
        if isin:
            security = self._master.get(isin)
            if security is not None:
                return Resolution(security, "isin_join", 1.0, "")
            return Resolution(None, "unresolved", 0.0, f"ISIN {isin} is not in security master {self._master.version}")

        decision = self._resolver.resolve(name, rank_candidates(name, self._master))
        if decision.isin is None:
            return Resolution(None, "unresolved", decision.confidence, decision.reason)
        security = self._master.get(decision.isin)
        if security is None:
            return Resolution(None, "unresolved", 0.0, "resolver returned an ISIN outside the security master")
        if decision.confidence < self._min_confidence:
            return Resolution(
                None,
                "unresolved",
                decision.confidence,
                f"best match {security.name} is below the confidence threshold "
                f"({decision.confidence:.2f} < {self._min_confidence:.2f})",
            )
        return Resolution(security, decision.method, decision.confidence, decision.reason)

    def _classify_one(self, holding: Holding) -> ClassifiedHolding:
        if holding.instrument_type is not InstrumentType.EQUITY:
            return ClassifiedHolding(holding=holding, method="not_equity", confidence=1.0)
        resolution = self.resolve_security(holding.isin, holding.name)
        if resolution.security is None:
            return ClassifiedHolding(
                holding=holding, method="unresolved", confidence=resolution.confidence, note=resolution.note
            )
        security = resolution.security
        return ClassifiedHolding(
            holding=holding,
            method=resolution.method,
            confidence=resolution.confidence,
            resolved_isin=security.isin,
            resolved_name=security.name,
            bucket=security.bucket,
            sector=security.sector,
            note=resolution.note,
        )
