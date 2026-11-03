"""Deterministic scope guard: forecasts and recommendations are declined before any model sees them."""

from __future__ import annotations

import re

_OUT_OF_SCOPE = re.compile(
    r"\b("
    r"predict(?:ion|ed|s)?|forecast(?:s|ed|ing)?|projection|"
    r"next (?:day|week|month|quarter|year)|in the future|"
    r"recommend(?:ation|ations|ed|s)?|advi[cs]e|"
    r"should (?:i|we) (?:buy|sell|add|trim|hold)|"
    r"best (?:stock|share|buy|pick|investment)s?|which (?:stock|share)s? (?:to|should)|"
    r"expected returns?|outperform(?:ance)?|target price"
    r")\b",
    re.IGNORECASE,
)

DECLINE_TEXT = (
    "PortfolioLens reports current mandate compliance only. It does not forecast drift, predict returns or "
    "recommend trades: that would be investment advice and could not be audited. To test a trade you have "
    "already chosen, use the what-if simulator."
)


def is_out_of_scope(text: str) -> bool:
    return bool(_OUT_OF_SCOPE.search(text))
