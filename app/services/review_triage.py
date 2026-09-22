from __future__ import annotations

"""Triage for records that did not settle automatically.

A run can leave hundreds of records in the review queue. They do NOT count
towards Brand Reputation until a decision is made (`_reputation_eligible`
requires `decision == "ready"`), so leaving the queue untouched silently
discards evidence the operator paid to collect.

Asking someone to adjudicate 300 records one by one guarantees the queue is
never touched at all. Most of them cannot move the number: a record with no
reach, low authenticity and weak evidence carries almost no weight in the
reputation average. A handful can move it several points.

This module estimates, for each pending record, how much the published score
would move if it were admitted — using the SAME weighting the intelligence
engine applies — so the operator spends their attention where it changes the
answer, and can see at a glance how much of the queue is merely volume.
"""

import math

#: Records whose potential movement is below this (in Brand Reputation points)
#: are volume, not signal: admitting or dropping them changes nothing visible.
MATERIAL_POINT_THRESHOLD = 0.15

#: How many records are proposed for human attention by default.
DEFAULT_PRIORITY_SIZE = 20


def _f(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(out) or math.isinf(out) else out


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def potential_weight(row: dict) -> float:
    """Weight this record would carry if it were admitted as analysis-ready.

    Mirrors `_record_weights` in the intelligence engine: independent voice ×
    authenticity × evidence confidence × attention. It is deliberately the same
    formula — a triage that ranked by anything else would send the operator to
    records the score does not actually listen to.
    """
    cleaning = row.get("cleaning") if isinstance(row.get("cleaning"), dict) else {}
    ai = row.get("ai_analysis") if isinstance(row.get("ai_analysis"), dict) else {}

    independent = _clamp(_f(cleaning.get("independent_voice_weight"), 1.0))
    authenticity = _clamp(_f(cleaning.get("authenticity_score"), 100.0) / 100.0)
    evidence = (
        0.45 * _f(ai.get("overall_confidence"))
        + 0.25 * _f(ai.get("sentiment_confidence"))
        + 0.15 * _f(ai.get("relevance_confidence"))
        + 0.15 * _f(cleaning.get("confidence"))
    )
    impact = _clamp(_f(ai.get("impact_score"), 0.5))
    attention = 0.40 + 0.60 * impact
    return independent * authenticity * _clamp(evidence) * attention


def _sentiment_distance(row: dict) -> float:
    """How far this record sits from the middle of the 0–100 scale.

    A strongly negative record pulls the score down hard; a neutral one barely
    moves it however heavy it is. Movement is driven by weight AND distance.
    """
    ai = row.get("ai_analysis") if isinstance(row.get("ai_analysis"), dict) else {}
    score = _clamp(_f(ai.get("sentiment_score")), -1.0, 1.0)
    return abs(score)


def estimated_point_movement(row: dict, admitted_weight_total: float, ready_count: int) -> float:
    """Brand Reputation points this single record could move, if admitted.

    The published score is a weighted mean on a 0–100 scale. Adding one record
    of weight w at distance d from the current mean moves that mean by roughly
    ``d * 50 * w / (W + w)`` where W is the weight already in the average. With
    no weights available yet, the record count is used as a proxy.
    """
    w = potential_weight(row)
    if w <= 0:
        return 0.0
    base = admitted_weight_total if admitted_weight_total > 0 else float(max(1, ready_count))
    return _sentiment_distance(row) * 50.0 * (w / (base + w))


def triage_review_queue(
    pending: list[dict],
    ready_records: list[dict] | None = None,
    priority_size: int = DEFAULT_PRIORITY_SIZE,
) -> dict:
    """Split a review queue into what matters and what is merely volume."""
    ready_records = ready_records or []
    admitted_weight = sum(potential_weight(r) for r in ready_records)
    ready_count = len(ready_records)

    scored: list[dict] = []
    for row in pending:
        movement = estimated_point_movement(row, admitted_weight, ready_count)
        scored.append({"record": row, "movement": round(movement, 4)})
    scored.sort(key=lambda item: item["movement"], reverse=True)

    material = [item for item in scored if item["movement"] >= MATERIAL_POINT_THRESHOLD]
    priority = scored[:max(0, int(priority_size))] if not material else material[:max(0, int(priority_size))]

    return {
        "total_pending": len(pending),
        "material_count": len(material),
        "volume_count": len(pending) - len(material),
        "priority": priority,
        "priority_count": len(priority),
        "max_movement": round(scored[0]["movement"], 3) if scored else 0.0,
        "combined_movement": round(sum(item["movement"] for item in material), 3),
        "threshold_points": MATERIAL_POINT_THRESHOLD,
    }
