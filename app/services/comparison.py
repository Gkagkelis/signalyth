"""Head-to-head comparison of two analyses (A vs B).

Clients constantly ask "how do we stand against them". A naive answer puts two
numbers side by side; a defensible one first checks whether the two measurements
are comparable at all.

Every comparison therefore ships with a comparability audit (§3, §10, §21):

* different collection windows, different source mixes or very different sample
  sizes each weaken the comparison, and are reported as explicit caveats,
* a gap smaller than the materiality threshold is called a tie, not a win,
* nothing is attributed to a cause; the output describes measured differences.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

MATERIAL_POINTS = 3.0
THIN_EVIDENCE = 30.0
SAMPLE_RATIO_WARN = 2.5


def _num(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _side(run: dict) -> dict | None:
    intel = (run.get("intelligence") or {}).get("summary") or {}
    index = _num((intel.get("brand_reputation") or {}).get("index"))
    if index is None:
        return None
    sentiment = (intel.get("sentiment") or {}).get("weighted_percent") or {}
    confidence = intel.get("confidence") or {}
    emotions = (intel.get("emotions_organic_people") or {}).get("weighted_percent") or {}
    leading = None
    if emotions:
        ranked = sorted(((k, _num(v, 0.0) or 0.0) for k, v in emotions.items() if k != "neutral"),
                        key=lambda kv: -kv[1])
        if ranked and ranked[0][1] > 0:
            leading = ranked[0][0]
    sources = intel.get("source_breakdown") or {}
    negatives = [d.get("name") for d in (intel.get("top_negative_narrative_drivers") or [])[:3]]
    positives = [d.get("name") for d in (intel.get("top_positive_narrative_drivers") or [])[:3]]
    return {
        "run_id": run.get("run_id"),
        "label": run.get("topic") or run.get("client") or run.get("run_id"),
        "client": run.get("client"),
        "topic": run.get("topic"),
        "market": run.get("market"),
        "date_from": run.get("date_from"),
        "date_to": run.get("date_to"),
        "reputation": round(index, 2),
        "negative_share": _num(sentiment.get("negative")),
        "positive_share": _num(sentiment.get("positive")),
        "records": intel.get("records"),
        "effective_voices": _num(intel.get("effective_independent_voices")),
        "effective_sample_size": _num(confidence.get("effective_sample_size")),
        "confidence_label": confidence.get("label"),
        "leading_emotion": leading,
        "sources": sorted(sources.keys()),
        "top_negative_narratives": [n for n in negatives if n],
        "top_positive_narratives": [n for n in positives if n],
    }


def _comparability(a: dict, b: dict) -> dict:
    caveats: list[str] = []
    a_from, a_to = _date(a["date_from"]), _date(a["date_to"])
    b_from, b_to = _date(b["date_from"]), _date(b["date_to"])
    same_window = (a["date_from"], a["date_to"]) == (b["date_from"], b["date_to"])
    overlap_days = 0
    if all((a_from, a_to, b_from, b_to)):
        start, end = max(a_from, b_from), min(a_to, b_to)
        overlap_days = max(0, (end - start).days + 1)
        if not same_window:
            caveats.append("different_collection_windows")
    if a.get("market") != b.get("market"):
        caveats.append("different_markets")
    if set(a.get("sources") or []) != set(b.get("sources") or []):
        caveats.append("different_source_mix")
    sizes = [_num(a.get("effective_sample_size"), 0.0) or 0.0, _num(b.get("effective_sample_size"), 0.0) or 0.0]
    if min(sizes) < THIN_EVIDENCE:
        caveats.append("thin_evidence_on_one_side")
    if min(sizes) > 0 and max(sizes) / min(sizes) >= SAMPLE_RATIO_WARN:
        caveats.append("very_unequal_sample_sizes")
    blocking = {"thin_evidence_on_one_side", "different_markets"}
    return {
        "same_window": same_window,
        "overlap_days": overlap_days,
        "caveats": caveats,
        "fair": not (blocking & set(caveats)),
        "note": ("A comparison is only as strong as the two measurements behind it. "
                 "Differences in window, market, source mix or sample size can move "
                 "the index independently of the brands themselves."),
    }


def compare_runs(run_a: dict, run_b: dict) -> dict:
    a, b = _side(run_a), _side(run_b)
    if not a or not b:
        raise ValueError("Both runs must have a completed Brand Reputation before they can be compared.")

    comparability = _comparability(a, b)
    gap = round(a["reputation"] - b["reputation"], 2)
    material = abs(gap) >= MATERIAL_POINTS and comparability["fair"]
    if not material:
        leader, verdict = None, "tie"
    else:
        leader = a["run_id"] if gap > 0 else b["run_id"]
        verdict = "a_ahead" if gap > 0 else "b_ahead"

    def _delta(key):
        x, y = _num(a.get(key)), _num(b.get(key))
        return round(x - y, 2) if (x is not None and y is not None) else None

    shared_negative = sorted(set(a["top_negative_narratives"]) & set(b["top_negative_narratives"]))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "a": a,
        "b": b,
        "reputation_gap": gap,
        "negative_share_gap": _delta("negative_share"),
        "positive_share_gap": _delta("positive_share"),
        "voices_gap": _delta("effective_voices"),
        "verdict": verdict,
        "leader_run_id": leader,
        "material": material,
        "comparability": comparability,
        "shared_negative_narratives": shared_negative,
        "distinct_negative_a": [n for n in a["top_negative_narratives"] if n not in shared_negative],
        "distinct_negative_b": [n for n in b["top_negative_narratives"] if n not in shared_negative],
        "guardrail": ("Measured difference between two collected samples, not a market share, "
                      "a popularity ranking or a causal statement."),
    }


def comparable_runs(runs: list[dict]) -> list[dict]:
    """Runs that carry a usable Brand Reputation, newest first."""
    out = []
    for run in runs or []:
        side = _side(run)
        if side:
            out.append({
                "run_id": side["run_id"], "label": side["label"], "client": side["client"],
                "topic": side["topic"], "market": side["market"],
                "date_from": side["date_from"], "date_to": side["date_to"],
                "reputation": side["reputation"],
                "completed_at": run.get("completed_at") or run.get("updated_at"),
            })
    out.sort(key=lambda r: str(r.get("completed_at") or ""), reverse=True)
    return out
