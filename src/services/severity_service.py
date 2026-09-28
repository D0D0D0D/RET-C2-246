"""AgentCore Platform v1.0 — deterministic severity scoring & classification.

Severity scoring is rule-based (NOT LLM) so the output is reproducible and
auditable — a core design decision for a safety domain (docs/02_design.md).
The LLM is used only for natural-language alert / advisory / trend generation.

No business secrets, no I/O, no routing here — pure functions only.
"""

from __future__ import annotations

from typing import Any

# Canonical anomaly categories and their base hazard weight (higher = worse).
# Raw incident `type` strings are mapped onto these via keyword matching.
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "slip_zone": ("slip", "wet", "spill", "転倒", "滑", "濡"),
    "egress_obstruction": ("egress", "exit", "block", "obstruct", "避難", "通路", "非常口"),
    "fire_hazard": ("fire", "smoke", "burn", "火", "煙"),
    "overcrowding": ("crowd", "congest", "queue", "混雑", "密"),
    "structural": ("collapse", "fall", "shelf", "落下", "崩"),
}

_CATEGORY_WEIGHT: dict[str, int] = {
    "fire_hazard": 5,
    "egress_obstruction": 4,
    "structural": 4,
    "slip_zone": 3,
    "overcrowding": 2,
    "other": 1,
}


def classify_type(raw_type: str) -> str:
    """Map a raw incident type string onto a canonical anomaly category."""
    text = (raw_type or "").lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(k in text for k in keywords):
            return category
    return "other"


def classify_incidents(normalized_incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach a canonical anomaly ``category`` to each normalized incident."""
    classified: list[dict[str, Any]] = []
    for inc in normalized_incidents:
        category = classify_type(inc.get("type", ""))
        classified.append({**inc, "category": category})
    return classified


def _severity_label(score: int) -> str:
    # Scores range 1..10 (base weight 1..5, doubled to <=10 for injury/crowding).
    # high: fire / doubled hazards; medium: structural-class single hazards; low: minor.
    if score >= 5:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


def score_incident(incident: dict[str, Any]) -> dict[str, Any]:
    """Deterministically score a single classified incident.

    score = category_weight * crowd_multiplier, capped at 10.
    crowd_multiplier is 2 when the incident is flagged as affecting many people
    (``injury`` true or ``people_affected`` >= 5), else 1.
    """
    category = incident.get("category") or classify_type(incident.get("type", ""))
    base = _CATEGORY_WEIGHT.get(category, _CATEGORY_WEIGHT["other"])
    people = incident.get("people_affected", 0) or 0
    injury = bool(incident.get("injury", False))
    crowd_multiplier = 2 if (injury or (isinstance(people, int) and people >= 5)) else 1
    score = min(base * crowd_multiplier, 10)
    return {
        "id": incident.get("id", ""),
        "location": incident.get("location", ""),
        "category": category,
        "score": score,
        "severity": _severity_label(score),
    }


def score_incidents(classified_incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score every classified incident; returns a list ordered by score desc."""
    scored = [score_incident(inc) for inc in classified_incidents]
    scored.sort(key=lambda s: s["score"], reverse=True)
    return scored
