"""Temporal tracking: the same brand measured again and again over time.

A single run answers "where does the conversation stand in this window". Monitoring
answers "what changed since last time, and is the change real". This module groups
completed runs of the same client/topic/market into a series and computes
period-over-period deltas.

Methodology guardrails carried through (§11, §21):

* Only completed runs with a real Brand Reputation value enter a series. A run
  whose evidence never reached the reputation stage would otherwise create a
  phantom dip.
* A delta is reported together with the evidence behind both points. Small moves
  on thin samples are explicitly marked as not material rather than narrated.
* Nothing here claims causality: a change is described, never explained.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Below this reputation move (points) a change is descriptive noise, not a signal.
MATERIAL_POINTS = 3.0
# Below this effective sample the point is flagged as thin evidence.
THIN_EVIDENCE = 30.0


def _num(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


def series_key(run: dict) -> str:
    """Runs belong to the same series when client, topic and market match."""
    return "|".join((_norm(run.get("client")), _norm(run.get("topic")), _norm(run.get("market"))))


def _point(run: dict) -> dict | None:
    intel = (run.get("intelligence") or {}).get("summary") or {}
    reputation = (intel.get("brand_reputation") or {}).get("index")
    index = _num(reputation)
    if index is None:
        return None
    sentiment = (intel.get("sentiment") or {}).get("weighted_percent") or {}
    confidence = intel.get("confidence") or {}
    emotions = (intel.get("emotions_organic_people") or {}).get("weighted_percent") or {}
    leading_emotion = None
    if emotions:
        ranked = sorted(((k, _num(v, 0.0) or 0.0) for k, v in emotions.items() if k != "neutral"),
                        key=lambda kv: -kv[1])
        if ranked and ranked[0][1] > 0:
            leading_emotion = ranked[0][0]
    negatives = intel.get("top_negative_narrative_drivers") or []
    return {
        "run_id": run.get("run_id"),
        "client": run.get("client"),
        "topic": run.get("topic"),
        "market": run.get("market"),
        "date_from": run.get("date_from"),
        "date_to": run.get("date_to"),
        "completed_at": run.get("completed_at") or run.get("updated_at"),
        "reputation": round(index, 2),
        "sentiment_negative": _num(sentiment.get("negative")),
        "sentiment_positive": _num(sentiment.get("positive")),
        "records": intel.get("records"),
        "effective_voices": _num(intel.get("effective_independent_voices")),
        "effective_sample_size": _num(confidence.get("effective_sample_size")),
        "confidence_label": confidence.get("label"),
        "leading_emotion": leading_emotion,
        "top_negative_narrative": (negatives[0].get("name") if negatives else None),
    }


def _sort_key(point: dict):
    return (str(point.get("date_to") or ""), str(point.get("completed_at") or ""))


def build_series(runs: list[dict]) -> list[dict]:
    """All series present in the supplied runs, most recently updated first."""
    grouped: dict[str, list[dict]] = {}
    for run in runs or []:
        point = _point(run)
        if not point:
            continue
        grouped.setdefault(series_key(run), []).append(point)
    series = []
    for key, points in grouped.items():
        points.sort(key=_sort_key)
        series.append({
            "series_key": key,
            "client": points[-1]["client"],
            "topic": points[-1]["topic"],
            "market": points[-1]["market"],
            "points": points,
            "latest": points[-1],
            "change": compare(points[-2], points[-1]) if len(points) > 1 else None,
        })
    series.sort(key=lambda s: str(s["latest"].get("completed_at") or ""), reverse=True)
    return series


def compare(previous: dict, current: dict) -> dict:
    """Period-over-period change with an explicit materiality verdict."""
    delta = round((current.get("reputation") or 0.0) - (previous.get("reputation") or 0.0), 2)
    neg_prev = _num(previous.get("sentiment_negative"))
    neg_now = _num(current.get("sentiment_negative"))
    negative_delta = round(neg_now - neg_prev, 2) if (neg_prev is not None and neg_now is not None) else None
    thin = min(
        _num(previous.get("effective_sample_size"), 0.0) or 0.0,
        _num(current.get("effective_sample_size"), 0.0) or 0.0,
    ) < THIN_EVIDENCE
    material = abs(delta) >= MATERIAL_POINTS and not thin
    direction = "stable"
    if material:
        direction = "improved" if delta > 0 else "declined"
    narrative_changed = (previous.get("top_negative_narrative") or None) != (current.get("top_negative_narrative") or None)
    return {
        "from_run": previous.get("run_id"),
        "to_run": current.get("run_id"),
        "from_period": f"{previous.get('date_from')} → {previous.get('date_to')}",
        "to_period": f"{current.get('date_from')} → {current.get('date_to')}",
        "reputation_delta": delta,
        "reputation_from": previous.get("reputation"),
        "reputation_to": current.get("reputation"),
        "negative_share_delta": negative_delta,
        "direction": direction,
        "material": material,
        "thin_evidence": thin,
        "leading_emotion_from": previous.get("leading_emotion"),
        "leading_emotion_to": current.get("leading_emotion"),
        "narrative_changed": narrative_changed,
        "top_negative_narrative_from": previous.get("top_negative_narrative"),
        "top_negative_narrative_to": current.get("top_negative_narrative"),
        "guardrail": ("Description of observed change between two collection windows. "
                      "It does not establish a cause, and differing windows or source mixes "
                      "can move the index on their own."),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def find_series(runs: list[dict], key: str) -> dict | None:
    for series in build_series(runs):
        if series["series_key"] == key:
            return series
    return None
