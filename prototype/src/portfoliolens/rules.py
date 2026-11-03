"""Rule retrieval: map a confirmed mandate onto the pinned rulebook version, plus the SID's own limits."""

from __future__ import annotations

from collections import Counter

from portfoliolens.domain import Constraint, Mandate, Metric, RulebookVersion
from portfoliolens.errors import RuleCoverageError

ALL_CATEGORIES = "*"


def retrieve_constraints(rulebook: RulebookVersion, mandate: Mandate) -> list[Constraint]:
    category_rules = [r for r in rulebook.rules if mandate.category.value in r.applies_to]
    if not category_rules:
        raise RuleCoverageError(
            f"Rulebook {rulebook.version} has no rules for category '{mandate.category.value}'; "
            "no verdict can be produced against it"
        )
    general_rules = [r for r in rulebook.rules if ALL_CATEGORIES in r.applies_to]

    constraints = [
        Constraint(
            constraint_id=rule.rule_id,
            metric=rule.metric,
            comparator=rule.comparator,
            threshold=rule.threshold,
            source_kind="SEBI",
            source_ref=f"{rule.rule_id}@{rulebook.version}",
            text=rule.text,
            sector=mandate.sector if rule.metric is Metric.SECTOR_PCT else None,
        )
        for rule in (*category_rules, *general_rules)
    ]

    seen: Counter[str] = Counter()
    for limit in mandate.self_imposed_limits:
        base_id = f"SID-{limit.metric.value.upper()}-{limit.comparator.value.upper()}"
        seen[base_id] += 1
        constraint_id = base_id if seen[base_id] == 1 else f"{base_id}-{seen[base_id]}"
        if limit.citation is not None:
            source_ref = f"SID chars {limit.citation.start}-{limit.citation.end}"
            text = limit.citation.quote
        else:
            source_ref = "SID (entered by reviewer at Gate 1)"
            text = "Limit entered by the reviewer at Gate 1."
        constraints.append(
            Constraint(
                constraint_id=constraint_id,
                metric=limit.metric,
                comparator=limit.comparator,
                threshold=limit.threshold,
                source_kind="SID",
                source_ref=source_ref,
                text=text,
                sector=mandate.sector if limit.metric is Metric.SECTOR_PCT else None,
            )
        )
    return constraints
