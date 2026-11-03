"""Version-scoped LangChain retrieval with constrained LLM reranking."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import Field

from portfoliolens.agents.llm import LLMClient
from portfoliolens.agents.prompts import RULE_RETRIEVAL_SYSTEM
from portfoliolens.domain import Constraint, Mandate, Rule, RulebookVersion, RuleRetrievalAudit
from portfoliolens.errors import LLMError
from portfoliolens.rules import retrieve_constraints

_TOKEN = re.compile(r"[a-z0-9]+")


class _RulebookRetriever(BaseRetriever):
    documents: list[Document] = Field(default_factory=list)

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        terms = set(_TOKEN.findall(query.lower()))
        ranked = sorted(
            self.documents,
            key=lambda document: (
                -len(terms & set(_TOKEN.findall(document.page_content.lower()))),
                str(document.metadata["rule_id"]),
            ),
        )
        return ranked


class RuleRetrievalAgent:
    """Retrieve all applicable rules from one pinned version; the model can only reorder their IDs."""

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client

    def retrieve(
        self,
        rulebook: RulebookVersion,
        rulebook_sha256: str,
        mandate: Mandate,
        as_of: date,
    ) -> tuple[list[Constraint], RuleRetrievalAudit]:
        base_constraints = retrieve_constraints(rulebook, mandate)
        rule_constraints = [constraint for constraint in base_constraints if constraint.source_kind == "SEBI"]
        rules_by_id = {rule.rule_id: rule for rule in rulebook.rules}
        applicable = [rules_by_id[constraint.constraint_id] for constraint in rule_constraints]
        constraints_by_id = {constraint.constraint_id: constraint for constraint in rule_constraints}
        documents = [_rule_document(rule, rulebook, mandate) for rule in applicable]
        query = _query_for(mandate)
        retrieved = _RulebookRetriever(documents=documents).invoke(query)
        deterministic_ids = [str(document.metadata["rule_id"]) for document in retrieved]
        ordered_ids = deterministic_ids
        method = "langchain_lexical"
        fallback_reason: str | None = None

        if self._client is not None:
            try:
                response = self._client.complete_json(
                    RULE_RETRIEVAL_SYSTEM,
                    _rerank_request(rulebook, mandate, retrieved, as_of),
                )
            except LLMError as exc:
                fallback_reason = f"Model reranking unavailable: {exc}"
            else:
                proposed = response.get("ordered_rule_ids")
                if (
                    isinstance(proposed, list)
                    and all(isinstance(item, str) for item in proposed)
                    and len(proposed) == len(deterministic_ids)
                    and set(proposed) == set(deterministic_ids)
                ):
                    ordered_ids = proposed
                    method = "langchain_retrieval_llm_rerank"
                else:
                    fallback_reason = "Model returned invalid rule IDs; deterministic retrieval order was used."

        ordered_constraints = [constraints_by_id[rule_id] for rule_id in ordered_ids]
        sid_constraints = [constraint for constraint in base_constraints if constraint.source_kind == "SID"]
        applied_constraints = [*ordered_constraints, *sid_constraints]

        audit = RuleRetrievalAudit(
            version=rulebook.version,
            effective_from=rulebook.effective_from,
            as_of=as_of,
            rulebook_sha256=rulebook_sha256,
            query=query,
            retrieved_rule_ids=deterministic_ids,
            applied_rule_ids=[constraint.constraint_id for constraint in ordered_constraints],
            method=method,
            fallback_reason=fallback_reason,
        )
        return applied_constraints, audit


def _rule_document(rule: Rule, rulebook: RulebookVersion, mandate: Mandate) -> Document:
    return Document(
        page_content=(
            f"{rule.rule_id} {rule.text} category {' '.join(rule.applies_to)} "
            f"metric {rule.metric.value} comparator {rule.comparator.value} threshold {rule.threshold}"
        ),
        metadata={
            "rule_id": rule.rule_id,
            "version": rulebook.version,
            "effective_from": rulebook.effective_from.isoformat(),
            "applies_to": rule.applies_to,
            "metric": rule.metric.value,
            "comparator": rule.comparator.value,
            "threshold": str(rule.threshold),
            "text": rule.text,
            "text_kind": rule.text_kind,
            "source_ref": rule.source_ref,
            "mandate_category": mandate.category.value,
        },
    )


def _query_for(mandate: Mandate) -> str:
    limits = " ".join(
        f"{limit.metric.value} {limit.comparator.value} {limit.threshold}" for limit in mandate.self_imposed_limits
    )
    sector = mandate.sector or ""
    return f"{mandate.category.value} {sector} equity category allocation issuer concentration {limits}".strip()


def _rerank_request(rulebook: RulebookVersion, mandate: Mandate, documents: list[Document], as_of: date) -> str:
    records: list[dict[str, Any]] = [
        {
            "rule_id": document.metadata["rule_id"],
            "version": document.metadata["version"],
            "effective_from": document.metadata["effective_from"],
            "applies_to": document.metadata["applies_to"],
            "metric": document.metadata["metric"],
            "comparator": document.metadata["comparator"],
            "threshold": document.metadata["threshold"],
            "text": document.metadata["text"],
            "source_ref": document.metadata["source_ref"],
        }
        for document in documents
    ]
    return (
        f"Confirmed category: {mandate.category.value}\n"
        f"As-of date: {as_of.isoformat()}\n"
        f"Selected rulebook version: {rulebook.version}, effective from {rulebook.effective_from.isoformat()}\n"
        f"Retrieved rule records:\n{json.dumps(records, indent=2, default=str)}"
    )
