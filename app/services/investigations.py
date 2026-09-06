from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

from app.services.storage import RunStore

INVESTIGATION_RULESET_VERSION = "1.1.0"
INVESTIGATION_METHODOLOGY_VERSION = "signalyth-investigations-v1"
PRESENTATION_EVIDENCE_CONTRACT_VERSION = "signalyth-evidence-pack-v1.1"

# This is deliberately explicit. Step 8 will consume this inventory and therefore cannot
# silently forget an indicator family just because no investigation was triggered for it.
INDICATOR_REGISTRY = (
    ("sample_volume", "Sample volume / usable evidence", "quality"),
    ("market_relevance", "Target-market relevance", "quality"),
    ("brand_reputation", "Brand Reputation", "core"),
    ("evidence_confidence", "Evidence confidence", "quality"),
    ("sentiment", "Sentiment", "core"),
    ("emotions", "Emotions", "core"),
    ("stance", "Target stance", "core"),
    ("impact_attention", "Impact / attention", "core"),
    ("effective_independent_voices", "Effective independent voices", "quality"),
    ("origin_breakdown", "Media / people / owned / organization", "core"),
    ("source_breakdown", "Source breakdown", "core"),
    ("positive_narrative_drivers", "Positive narrative drivers", "core"),
    ("negative_narrative_drivers", "Negative narrative drivers", "core"),
    ("topic_drivers", "Topic drivers", "core"),
    ("media_influence", "Media influence", "core"),
    ("people_influence", "People / creator influence", "core"),
    ("time_trends", "Time trends", "core"),
    ("numeric_anomalies", "Numeric anomalies", "core"),
    ("data_quality", "Data quality", "quality"),
    ("source_coverage", "Source coverage", "quality"),
    ("sample_achievement", "Sample achievement", "quality"),
    ("authenticity_risk", "Authenticity / bot risk", "quality"),
    ("coordination", "Coordinated behaviour", "quality"),
    ("story_syndication", "Story syndication / replication", "context"),
    ("top_mentions", "Top evidence mentions", "core"),
)


class InvestigationCancelled(RuntimeError):
    pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value, default: float = 0.0) -> float:
    try:
        out = float(value)
        if math.isfinite(out):
            return out
    except (TypeError, ValueError):
        pass
    return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, _safe_float(value, lo)))


def _parse_date(value) -> date | None:
    if value is None:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except (ValueError, TypeError):
            return None


def _record_day(row: dict) -> str | None:
    d = _parse_date(row.get("date"))
    return d.isoformat() if d else None


def _input_hash(intelligence_summary: dict, intelligence_records: list[dict], time_series: dict, cleaning_report: dict) -> str:
    compact_records = []
    for row in intelligence_records:
        ai = row.get("ai_analysis") or {}
        intel = row.get("intelligence") or {}
        cleaning = row.get("cleaning") or {}
        compact_records.append({
            "id": row.get("id"),
            "platform": row.get("platform"),
            "date": row.get("date"),
            "author": row.get("author"),
            "url": row.get("url"),
            "text_hash": hashlib.sha256(str(row.get("text") or "").encode("utf-8")).hexdigest(),
            "sentiment_score": ai.get("sentiment_score"),
            "sentiment_label": ai.get("sentiment_label"),
            "emotion": ai.get("primary_emotion"),
            "stance": ai.get("target_stance"),
            "topic": ai.get("topic"),
            "narrative": ai.get("narrative"),
            "impact": intel.get("impact_score"),
            "impact_confidence": intel.get("impact_confidence"),
            "reputation_weight": intel.get("reputation_weight"),
            "opinion_weight": intel.get("organic_opinion_weight"),
            "origin": intel.get("origin_group"),
            "independent_voice_weight": intel.get("independent_voice_weight"),
            "authenticity_score": cleaning.get("authenticity_score"),
            "market_score": cleaning.get("market_score"),
            "coordination_cluster_id": cleaning.get("coordination_cluster_id"),
            "story_cluster_id": cleaning.get("story_cluster_id"),
        })
    payload = {
        "intelligence_input_hash": intelligence_summary.get("input_hash"),
        "intelligence_ruleset": intelligence_summary.get("ruleset_version"),
        "intelligence_methodology": intelligence_summary.get("methodology_version"),
        "research_context": intelligence_summary.get("research_context") or {},
        "records": compact_records,
        "daily": (time_series or {}).get("daily", []),
        "anomalies": (time_series or {}).get("anomalies", []),
        "cleaning": {
            "data_quality_score": cleaning_report.get("data_quality_score"),
            "source_coverage_ratio": cleaning_report.get("source_coverage_ratio"),
            "sample_achievement_ratio": cleaning_report.get("sample_achievement_ratio"),
            "trusted_records": cleaning_report.get("trusted_records"),
            "review_records": cleaning_report.get("review_records"),
            "excluded_records": cleaning_report.get("excluded_records"),
            "total_records": cleaning_report.get("total_records"),
            "organic_opinion_records": cleaning_report.get("organic_opinion_records"),
            "high_greece_market_relevance": cleaning_report.get("high_greece_market_relevance"),
        },
        "ruleset": INVESTIGATION_RULESET_VERSION,
        "contract": PRESENTATION_EVIDENCE_CONTRACT_VERSION,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _weighted_reputation(rows: list[dict]) -> float | None:
    num = 0.0
    den = 0.0
    for row in rows:
        intel = row.get("intelligence") or {}
        if not intel.get("reputation_eligible"):
            continue
        w = _safe_float(intel.get("reputation_weight"), 0.0)
        if w <= 0:
            continue
        s = _safe_float((row.get("ai_analysis") or {}).get("sentiment_score"), 0.0)
        num += w * s
        den += w
    if den <= 0:
        return None
    return round(50.0 * (1.0 + num / den), 2)


def _weighted_share(rows: list[dict], field: str, label: str, weight_key: str, *, opinion_only: bool = False) -> float | None:
    num = 0.0
    den = 0.0
    for row in rows:
        ai = row.get("ai_analysis") or {}
        intel = row.get("intelligence") or {}
        if opinion_only and not ai.get("opinion_eligible"):
            continue
        if weight_key == "reputation_weight" and not intel.get("reputation_eligible"):
            continue
        w = _safe_float(intel.get(weight_key), 0.0)
        if w <= 0:
            continue
        den += w
        if str(ai.get(field) or "") == label:
            num += w
    return None if den <= 0 else num / den


def _weight_total(rows: list[dict], key: str, *, reputation_only: bool = False, opinion_only: bool = False) -> float:
    total = 0.0
    for row in rows:
        ai = row.get("ai_analysis") or {}
        intel = row.get("intelligence") or {}
        if reputation_only and not intel.get("reputation_eligible"):
            continue
        if opinion_only and not ai.get("opinion_eligible"):
            continue
        total += max(0.0, _safe_float(intel.get(key), 0.0))
    return total


def _share_by(rows: list[dict], dimension: str, *, weight_key: str = "reputation_weight", eligibility: str = "reputation") -> dict[str, dict]:
    groups: dict[str, dict] = defaultdict(lambda: {"weight": 0.0, "records": 0, "sentiment_num": 0.0, "impact": 0.0})
    total = 0.0
    for row in rows:
        ai = row.get("ai_analysis") or {}
        intel = row.get("intelligence") or {}
        if eligibility == "reputation" and not intel.get("reputation_eligible"):
            continue
        if eligibility == "opinion" and not ai.get("opinion_eligible"):
            continue
        if dimension == "platform":
            label = str(row.get("platform") or "unknown")
        elif dimension == "origin_group":
            label = str(intel.get("origin_group") or "unknown")
        else:
            label = str(ai.get(dimension) or "Unclassified").strip() or "Unclassified"
        w = max(0.0, _safe_float(intel.get(weight_key), 0.0))
        if w <= 0:
            continue
        g = groups[label]
        g["weight"] += w
        g["records"] += 1
        g["sentiment_num"] += w * _safe_float(ai.get("sentiment_score"), 0.0)
        g["impact"] += _safe_float(intel.get("impact_score"), 0.5)
        total += w
    out = {}
    for label, g in groups.items():
        out[label] = {
            "records": g["records"],
            "weight": round(g["weight"], 6),
            "share": round(g["weight"] / total, 6) if total > 0 else 0.0,
            "average_sentiment": round(g["sentiment_num"] / g["weight"], 6) if g["weight"] > 0 else None,
            "average_impact": round(g["impact"] / max(1, g["records"]), 6),
        }
    return out


def _top_shift(target: dict[str, dict], baseline: dict[str, dict], limit: int = 5) -> list[dict]:
    labels = set(target) | set(baseline)
    out = []
    for label in labels:
        t = target.get(label, {})
        b = baseline.get(label, {})
        delta = _safe_float(t.get("share"), 0.0) - _safe_float(b.get("share"), 0.0)
        if abs(delta) < 0.02 and _safe_int(t.get("records"), 0) < 2:
            continue
        out.append({
            "name": label,
            "target_share": round(_safe_float(t.get("share"), 0.0), 6),
            "baseline_share": round(_safe_float(b.get("share"), 0.0), 6),
            "share_delta": round(delta, 6),
            "target_records": _safe_int(t.get("records"), 0),
            "baseline_records": _safe_int(b.get("records"), 0),
            "target_average_sentiment": t.get("average_sentiment"),
            "baseline_average_sentiment": b.get("average_sentiment"),
        })
    out.sort(key=lambda x: (-abs(x["share_delta"]), -x["target_records"], x["name"]))
    return out[:limit]


def _evidence_rows(rows: list[dict], limit: int = 8) -> list[dict]:
    ranked = sorted(
        rows,
        key=lambda r: (
            -abs(_safe_float((r.get("ai_analysis") or {}).get("sentiment_score"), 0.0)) * max(0.01, _safe_float((r.get("intelligence") or {}).get("reputation_weight"), 0.0)),
            -_safe_float((r.get("intelligence") or {}).get("impact_score"), 0.0),
            -_safe_float((r.get("intelligence") or {}).get("evidence_confidence"), 0.0),
            str(r.get("id") or ""),
        ),
    )
    out = []
    for row in ranked[:limit]:
        ai = row.get("ai_analysis") or {}
        intel = row.get("intelligence") or {}
        cleaning = row.get("cleaning") or {}
        text = str(row.get("text") or "").strip()
        out.append({
            "record_id": row.get("id"),
            "platform": row.get("platform"),
            "author": row.get("author"),
            "date": row.get("date"),
            "url": row.get("url"),
            "excerpt": text[:280],
            "sentiment_label": ai.get("sentiment_label"),
            "sentiment_score": ai.get("sentiment_score"),
            "emotion": ai.get("primary_emotion"),
            "topic": ai.get("topic"),
            "narrative": ai.get("narrative"),
            "impact_score": intel.get("impact_score"),
            "evidence_confidence": intel.get("evidence_confidence"),
            "origin_group": intel.get("origin_group"),
            "coordination_cluster_id": cleaning.get("coordination_cluster_id"),
            "story_cluster_id": cleaning.get("story_cluster_id"),
        })
    return out


def _confidence(local_rows: list[dict], overall_confidence: float, cleaning_report: dict, *, driver_strength: float = 0.0) -> tuple[float, str]:
    n = len(local_rows)
    sample = _clamp(math.sqrt(n / 25.0))
    source_diversity = len({str(r.get("platform") or "unknown") for r in local_rows})
    source_factor = _clamp(source_diversity / 3.0)
    evidence_values = [_safe_float((r.get("intelligence") or {}).get("evidence_confidence"), 0.0) for r in local_rows]
    evidence = statistics.fmean(evidence_values) if evidence_values else 0.0
    quality = _clamp(_safe_float(cleaning_report.get("data_quality_score"), 0.0) / 100.0)
    overall = _clamp(overall_confidence / 100.0)
    strength = _clamp(driver_strength)
    score = 100.0 * (0.25 * overall + 0.20 * quality + 0.20 * sample + 0.15 * source_factor + 0.15 * evidence + 0.05 * strength)
    label = "high" if score >= 75 else ("medium" if score >= 50 else "low")
    return round(score, 2), label


def _priority(score: float) -> str:
    return "high" if score >= 70 else ("medium" if score >= 40 else "low")


def _evidence_status(confidence_score: float, local_rows: list[dict]) -> str:
    sources = len({str(r.get("platform") or "unknown") for r in local_rows})
    authors = len({str(r.get("author") or "unknown") for r in local_rows})
    if confidence_score >= 75 and (sources >= 2 or authors >= 5):
        return "strong_association"
    if confidence_score >= 50 and (sources >= 1 and authors >= 2):
        return "moderate_association"
    return "limited_evidence"


def _quality_caveats(local_rows: list[dict], cleaning_report: dict) -> list[str]:
    caveats = []
    if len(local_rows) < 5:
        caveats.append("small_local_evidence_base")
    if _safe_float(cleaning_report.get("source_coverage_ratio"), 0.0) < 0.75:
        caveats.append("incomplete_source_coverage")
    if _safe_float(cleaning_report.get("sample_achievement_ratio"), 1.0) < 0.75:
        caveats.append("sample_shortfall")
    if local_rows:
        missing_impact = sum(1 for r in local_rows if _safe_float((r.get("intelligence") or {}).get("impact_confidence"), 0.0) <= 0)
        if missing_impact / len(local_rows) >= 0.40:
            caveats.append("impact_metrics_missing")
        low_semantic = sum(1 for r in local_rows if _safe_float((r.get("intelligence") or {}).get("evidence_confidence"), 0.0) < 0.55)
        if low_semantic / len(local_rows) >= 0.30:
            caveats.append("semantic_confidence_mixed")
    return caveats


def _coordination_clusters(records: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in records:
        cid = str((row.get("cleaning") or {}).get("coordination_cluster_id") or "").strip()
        if cid:
            groups[cid].append(row)
    out = []
    for cid, rows in groups.items():
        authors = {str(r.get("author") or "unknown") for r in rows}
        platforms = {str(r.get("platform") or "unknown") for r in rows}
        auth = [_safe_float((r.get("cleaning") or {}).get("authenticity_score"), 100.0) for r in rows]
        sent = [_safe_float((r.get("ai_analysis") or {}).get("sentiment_score"), 0.0) for r in rows]
        out.append({
            "cluster_id": cid,
            "records": len(rows),
            "unique_authors": len(authors),
            "platforms": sorted(platforms),
            "average_authenticity_score": round(statistics.fmean(auth), 2) if auth else None,
            "average_sentiment": round(statistics.fmean(sent), 4) if sent else None,
            "attention_score_sum": round(sum(_safe_float((r.get("intelligence") or {}).get("impact_score"), 0.0) for r in rows), 4),
            "record_ids": [r.get("id") for r in rows],
        })
    out.sort(key=lambda x: (-x["records"], -x["unique_authors"], -x["attention_score_sum"], x["cluster_id"]))
    return out


def _story_clusters(records: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in records:
        cid = str((row.get("cleaning") or {}).get("story_cluster_id") or "").strip()
        if cid:
            groups[cid].append(row)
    out = []
    for cid, rows in groups.items():
        if len(rows) < 2:
            continue
        out.append({
            "cluster_id": cid,
            "records": len(rows),
            "unique_authors": len({str(r.get("author") or "unknown") for r in rows}),
            "platforms": sorted({str(r.get("platform") or "unknown") for r in rows}),
            "attention_score_sum": round(sum(_safe_float((r.get("intelligence") or {}).get("impact_score"), 0.0) for r in rows), 4),
            "record_ids": [r.get("id") for r in rows],
        })
    out.sort(key=lambda x: (-x["records"], -x["attention_score_sum"], x["cluster_id"]))
    return out


def _period_split(records: list[dict], plan: dict) -> dict:
    start = _parse_date(plan.get("date_from"))
    end = _parse_date(plan.get("date_to"))
    dated = [(r, _parse_date(r.get("date"))) for r in records]
    dated = [(r, d) for r, d in dated if d]
    if not dated:
        return {"available": False, "reason": "no_dated_records"}
    if start is None:
        start = min(d for _, d in dated)
    if end is None:
        end = max(d for _, d in dated)
    if end < start:
        return {"available": False, "reason": "invalid_period"}
    span = (end - start).days
    if span < 3:
        return {"available": False, "reason": "period_too_short"}
    midpoint = start + timedelta(days=span // 2)
    first = [r for r, d in dated if start <= d <= midpoint]
    second = [r for r, d in dated if midpoint < d <= end]
    if not first or not second:
        return {"available": False, "reason": "insufficient_half_coverage"}
    first_rep = _weighted_reputation(first)
    second_rep = _weighted_reputation(second)
    first_neg = _weighted_share(first, "sentiment_label", "negative", "reputation_weight")
    second_neg = _weighted_share(second, "sentiment_label", "negative", "reputation_weight")
    first_anger = _weighted_share(first, "primary_emotion", "anger", "organic_opinion_weight", opinion_only=True)
    second_anger = _weighted_share(second, "primary_emotion", "anger", "organic_opinion_weight", opinion_only=True)
    return {
        "available": True,
        "first_window": {"from": start.isoformat(), "to": midpoint.isoformat(), "records": len(first), "brand_reputation": first_rep, "negative_share": first_neg, "anger_share": first_anger},
        "second_window": {"from": (midpoint + timedelta(days=1)).isoformat(), "to": end.isoformat(), "records": len(second), "brand_reputation": second_rep, "negative_share": second_neg, "anger_share": second_anger},
        "deltas": {
            "brand_reputation_points": None if first_rep is None or second_rep is None else round(second_rep - first_rep, 2),
            "negative_share_points": None if first_neg is None or second_neg is None else round((second_neg - first_neg) * 100.0, 2),
            "anger_share_points": None if first_anger is None or second_anger is None else round((second_anger - first_anger) * 100.0, 2),
        },
        "first_rows": first,
        "second_rows": second,
    }


def _indicator_inventory(summary: dict, time_series: dict, records: list[dict], cleaning_report: dict) -> list[dict]:
    coordination = _coordination_clusters(records)
    stories = _story_clusters(records)
    auth_scores = [_safe_float((r.get("cleaning") or {}).get("authenticity_score"), 100.0) for r in records]
    low_auth = sum(1 for x in auth_scores if x < 60)
    total = max(1, len(records))
    source_breakdown = summary.get("source_breakdown") or {}
    impact_known = sum(1 for r in records if _safe_float((r.get("intelligence") or {}).get("impact_confidence"), 0.0) > 0)

    top_mentions = sorted(
        records,
        key=lambda r: (
            -_safe_float((r.get("intelligence") or {}).get("impact_score"), 0.0),
            -abs(_safe_float((r.get("ai_analysis") or {}).get("sentiment_score"), 0.0)),
            -_safe_float((r.get("intelligence") or {}).get("evidence_confidence"), 0.0),
            str(r.get("id") or ""),
        ),
    )[:15]
    top_mentions = [{
        "record_id": r.get("id"),
        "platform": r.get("platform"),
        "author": r.get("author"),
        "date": r.get("date"),
        "url": r.get("url"),
        "excerpt": str(r.get("text") or "").strip()[:280],
        "sentiment_label": (r.get("ai_analysis") or {}).get("sentiment_label"),
        "sentiment_score": (r.get("ai_analysis") or {}).get("sentiment_score"),
        "emotion": (r.get("ai_analysis") or {}).get("primary_emotion"),
        "topic": (r.get("ai_analysis") or {}).get("topic"),
        "narrative": (r.get("ai_analysis") or {}).get("narrative"),
        "impact_score": (r.get("intelligence") or {}).get("impact_score"),
        "evidence_confidence": (r.get("intelligence") or {}).get("evidence_confidence"),
    } for r in top_mentions]

    market_scores = [_safe_float((r.get("cleaning") or {}).get("market_score"), -1.0) for r in records]
    market_scores = [x for x in market_scores if x >= 0.0]
    high_market = sum(1 for x in market_scores if x >= 0.65)
    market_total = len(market_scores)
    market_name = ((summary.get("research_context") or {}).get("market") or "").strip() or None

    values = {
        "sample_volume": {
            "analysis_ready_records": _safe_int(summary.get("records"), len(records)),
            "reputation_eligible_records": _safe_int(summary.get("reputation_eligible_records"), 0),
            "organic_opinion_records": _safe_int(summary.get("organic_opinion_records"), 0),
            "trusted_records": cleaning_report.get("trusted_records"),
            "collected_cleaning_records": cleaning_report.get("total_records"),
            "effective_independent_voices": summary.get("effective_independent_voices"),
        },
        "market_relevance": {
            "market": market_name,
            "records_with_market_score": market_total,
            "high_relevance_records": high_market,
            "high_relevance_share": round(high_market / market_total, 4) if market_total else None,
            "average_market_score": round(statistics.fmean(market_scores), 4) if market_scores else None,
            "step3_high_greece_market_relevance": cleaning_report.get("high_greece_market_relevance") if str(market_name or "").casefold() == "greece" else None,
        },
        "brand_reputation": {"index": (summary.get("brand_reputation") or {}).get("index"), "interpretation": (summary.get("brand_reputation") or {}).get("interpretation")},
        "evidence_confidence": summary.get("confidence") or {},
        "sentiment": summary.get("sentiment") or {},
        "emotions": summary.get("emotions_organic_people") or {},
        "stance": summary.get("stance") or {},
        "impact_attention": {"records_with_known_public_metrics": impact_known, "records": len(records), "known_metric_share": round(impact_known / total, 4)},
        "effective_independent_voices": summary.get("effective_independent_voices"),
        "origin_breakdown": summary.get("origin_breakdown") or {},
        "source_breakdown": source_breakdown,
        "positive_narrative_drivers": summary.get("top_positive_narrative_drivers") or [],
        "negative_narrative_drivers": summary.get("top_negative_narrative_drivers") or [],
        "topic_drivers": summary.get("topic_drivers") or [],
        "media_influence": summary.get("media_influence") or [],
        "people_influence": summary.get("people_influence") or [],
        "time_trends": {"daily_points": len((time_series or {}).get("daily", [])), "daily": (time_series or {}).get("daily", [])},
        "numeric_anomalies": (time_series or {}).get("anomalies", []),
        "data_quality": {"score": cleaning_report.get("data_quality_score"), "label": cleaning_report.get("data_quality_label")},
        "source_coverage": {"ratio": cleaning_report.get("source_coverage_ratio"), "source_count": len(source_breakdown)},
        "sample_achievement": {"ratio": cleaning_report.get("sample_achievement_ratio"), "trusted_records": cleaning_report.get("trusted_records")},
        "authenticity_risk": {"low_authenticity_records": low_auth, "low_authenticity_share": round(low_auth / total, 4)},
        "coordination": coordination,
        "story_syndication": stories,
        "top_mentions": top_mentions,
    }

    def available(indicator_id: str, value) -> bool:
        if indicator_id == "sample_volume":
            return _safe_int(value.get("analysis_ready_records"), 0) > 0 or _safe_int(value.get("trusted_records"), 0) > 0
        if indicator_id == "market_relevance":
            return _safe_int(value.get("records_with_market_score"), 0) > 0 or value.get("step3_high_greece_market_relevance") is not None
        if indicator_id == "brand_reputation":
            return value.get("index") is not None
        if indicator_id in {"sentiment", "emotions", "stance"}:
            return _safe_int(value.get("records"), 0) > 0
        if indicator_id == "time_trends":
            return _safe_int(value.get("daily_points"), 0) >= 2
        if indicator_id in {"numeric_anomalies", "positive_narrative_drivers", "negative_narrative_drivers", "topic_drivers", "media_influence", "people_influence", "coordination", "story_syndication", "top_mentions"}:
            return bool(value)
        if indicator_id == "source_breakdown":
            return bool(value)
        if indicator_id == "origin_breakdown":
            return any(_safe_int((v or {}).get("records"), 0) > 0 for v in value.values()) if isinstance(value, dict) else False
        if indicator_id == "evidence_confidence":
            return "score" in value
        if indicator_id == "impact_attention":
            return len(records) > 0
        if indicator_id == "effective_independent_voices":
            return value is not None
        if indicator_id in {"data_quality", "source_coverage", "sample_achievement"}:
            return any(v is not None for v in value.values())
        if indicator_id == "authenticity_risk":
            return len(records) > 0
        return value is not None

    out = []
    for indicator_id, label, role in INDICATOR_REGISTRY:
        value = copy.deepcopy(values[indicator_id])
        is_available = available(indicator_id, value)
        out.append({
            "indicator_id": indicator_id,
            "label": label,
            "role": role,
            "examined": True,
            "available": is_available,
            "availability": "available" if is_available else "insufficient_or_not_triggered",
            "value": value,
        })
    return out


def _make_investigation(
    *,
    inv_type: str,
    question_en: str,
    question_el: str,
    finding_en: str,
    finding_el: str,
    trigger: dict,
    local_rows: list[dict],
    overall_confidence: float,
    cleaning_report: dict,
    priority_score: float,
    driver_strength: float = 0.0,
    drivers: list[dict] | None = None,
    metrics: dict | None = None,
    time_window: dict | None = None,
    recommended_visual: str = "evidence_table",
    alternative_explanations: list[str] | None = None,
    caveats: list[str] | None = None,
    conclusion_type: str = "association",
) -> dict:
    confidence_score, confidence_label = _confidence(local_rows, overall_confidence, cleaning_report, driver_strength=driver_strength)
    evidence_status = _evidence_status(confidence_score, local_rows)
    priority_score = round(_clamp(priority_score / 100.0) * 100.0, 2)
    c = list(caveats or []) + _quality_caveats(local_rows, cleaning_report)
    c = list(dict.fromkeys(c))
    presentation_candidate = priority_score >= 40 and evidence_status != "limited_evidence"
    return {
        "investigation_id": None,
        "type": inv_type,
        "status": "completed",
        "priority": _priority(priority_score),
        "priority_score": priority_score,
        "question": {"en": question_en, "el": question_el},
        "finding": {"en": finding_en, "el": finding_el},
        "trigger": copy.deepcopy(trigger),
        "time_window": copy.deepcopy(time_window),
        "metrics": copy.deepcopy(metrics or {}),
        "drivers": copy.deepcopy(drivers or []),
        "evidence_status": evidence_status,
        "confidence": {"score": confidence_score, "label": confidence_label},
        "causal_status": "not_proven",
        "conclusion_type": conclusion_type,
        "evidence": _evidence_rows(local_rows),
        "evidence_record_ids": [r.get("record_id") for r in _evidence_rows(local_rows)],
        "alternative_explanations": list(dict.fromkeys(alternative_explanations or ["sampling_or_source_mix_change", "external_event_not_observed_in_dataset"])),
        "caveats": c,
        "presentation": {
            "candidate": presentation_candidate,
            "priority_score": priority_score,
            "recommended_visual": recommended_visual,
            "evidence_required": True,
        },
    }


def _anomaly_investigations(records: list[dict], time_series: dict, summary: dict, cleaning_report: dict) -> list[dict]:
    out = []
    daily = {str(x.get("date")): x for x in (time_series or {}).get("daily", [])}
    overall_conf = _safe_float((summary.get("confidence") or {}).get("score"), 0.0)
    for anomaly in (time_series or {}).get("anomalies", []):
        day = str(anomaly.get("date") or "")
        target = [r for r in records if _record_day(r) == day]
        target_date = _parse_date(day)
        if not target_date:
            continue
        baseline_dates = {(target_date - timedelta(days=i)).isoformat() for i in range(1, 8)}
        baseline = [r for r in records if _record_day(r) in baseline_dates]
        current = daily.get(day, {})
        narrative_shift = _top_shift(_share_by(target, "narrative"), _share_by(baseline, "narrative"), 5)
        topic_shift = _top_shift(_share_by(target, "topic"), _share_by(baseline, "topic"), 4)
        source_shift = _top_shift(_share_by(target, "platform"), _share_by(baseline, "platform"), 4)
        origin_shift = _top_shift(_share_by(target, "origin_group"), _share_by(baseline, "origin_group"), 4)
        drivers = []
        for dim, shifts in (("narrative", narrative_shift), ("topic", topic_shift), ("source", source_shift), ("origin", origin_shift)):
            for item in shifts[:2]:
                if item["share_delta"] > 0.03:
                    drivers.append({"dimension": dim, **item})
        driver_strength = max([abs(_safe_float(d.get("share_delta"), 0.0)) for d in drivers] or [0.0])
        flags = list(anomaly.get("flags") or [])
        metric_parts = []
        if "volume_spike" in flags:
            metric_parts.append(f"volume {int(current.get('records') or 0)}")
        if "negative_share_spike" in flags:
            metric_parts.append(f"negative share {100*_safe_float(current.get('negative_weight_share'),0):.1f}%")
        if "anger_share_spike" in flags:
            metric_parts.append(f"anger share {100*_safe_float(current.get('anger_opinion_weight_share'),0):.1f}%")
        strongest = drivers[0]["name"] if drivers else None
        association_en = f" The strongest associated shift in the observed evidence was '{strongest}'." if strongest else " No single dominant associated driver was strong enough to name safely."
        association_el = f" Η ισχυρότερη συσχετισμένη μεταβολή στα διαθέσιμα evidence ήταν «{strongest}»." if strongest else " Δεν υπήρχε αρκετά ισχυρός μοναδικός driver ώστε να ονομαστεί με ασφάλεια."
        finding_en = f"On {day}, SIGNALYTH detected {', '.join(metric_parts) or 'a numeric anomaly'}.{association_en} This is an evidence association, not proven causality."
        finding_el = f"Στις {day}, το SIGNALYTH εντόπισε {', '.join(metric_parts) or 'αριθμητική ανωμαλία'}.{association_el} Πρόκειται για συσχέτιση evidence και όχι για αποδεδειγμένη αιτιότητα."
        severity = max(abs(_safe_float(anomaly.get("volume_robust_z"))), abs(_safe_float(anomaly.get("negative_robust_z"))), abs(_safe_float(anomaly.get("anger_robust_z"))))
        priority = min(100.0, 45.0 + min(45.0, severity * 5.0) + min(10.0, driver_strength * 40.0))
        out.append(_make_investigation(
            inv_type="numeric_anomaly",
            question_en=f"What is associated with the signal change on {day}?",
            question_el=f"Τι συνδέεται με τη μεταβολή των δεικτών στις {day};",
            finding_en=finding_en,
            finding_el=finding_el,
            trigger={"indicator": "numeric_anomalies", "date": day, "flags": flags, "robust_z": {k: anomaly.get(k) for k in ("volume_robust_z", "negative_robust_z", "anger_robust_z")}},
            local_rows=target,
            overall_confidence=overall_conf,
            cleaning_report=cleaning_report,
            priority_score=priority,
            driver_strength=driver_strength,
            drivers=drivers,
            metrics={"day": current, "baseline_records": len(baseline)},
            time_window={"target": day, "baseline_from": min(baseline_dates) if baseline_dates else None, "baseline_to": max(baseline_dates) if baseline_dates else None},
            recommended_visual="time_series_with_driver_breakdown",
            alternative_explanations=["sampling_or_source_mix_change", "external_event_not_observed_in_dataset", "syndication_or_coordination_effect"],
        ))
    return out


def _period_change_investigation(records: list[dict], summary: dict, cleaning_report: dict, plan: dict) -> list[dict]:
    split = _period_split(records, plan)
    if not split.get("available"):
        return []
    delta = (split.get("deltas") or {}).get("brand_reputation_points")
    neg_delta = (split.get("deltas") or {}).get("negative_share_points")
    anger_delta = (split.get("deltas") or {}).get("anger_share_points")
    if delta is None and neg_delta is None and anger_delta is None:
        return []
    material = max(abs(_safe_float(delta)), abs(_safe_float(neg_delta)), abs(_safe_float(anger_delta)))
    if material < 6.0:
        return []
    first = split["first_rows"]
    second = split["second_rows"]
    narrative_shift = _top_shift(_share_by(second, "narrative"), _share_by(first, "narrative"), 6)
    topic_shift = _top_shift(_share_by(second, "topic"), _share_by(first, "topic"), 5)
    source_shift = _top_shift(_share_by(second, "platform"), _share_by(first, "platform"), 5)
    origin_shift = _top_shift(_share_by(second, "origin_group"), _share_by(first, "origin_group"), 5)
    drivers = []
    for dim, shifts in (("narrative", narrative_shift), ("topic", topic_shift), ("source", source_shift), ("origin", origin_shift)):
        for item in shifts[:2]:
            if abs(item["share_delta"]) >= 0.05:
                drivers.append({"dimension": dim, **item})
    strongest = drivers[0]["name"] if drivers else None
    first_rep = split["first_window"].get("brand_reputation")
    second_rep = split["second_window"].get("brand_reputation")
    direction_en = "fell" if _safe_float(delta) < 0 else "rose"
    direction_el = "υποχώρησε" if _safe_float(delta) < 0 else "ενισχύθηκε"
    rep_sentence_en = f"Brand Reputation {direction_en} by {abs(_safe_float(delta)):.1f} points ({first_rep} → {second_rep})." if delta is not None else "Brand Reputation was not calculable in both halves."
    rep_sentence_el = f"Το Brand Reputation {direction_el} κατά {abs(_safe_float(delta)):.1f} μονάδες ({first_rep} → {second_rep})." if delta is not None else "Το Brand Reputation δεν μπορούσε να υπολογιστεί και στα δύο μισά της περιόδου."
    assoc_en = f" The largest associated mix shift was '{strongest}'." if strongest else " No single mix shift dominated the evidence."
    assoc_el = f" Η μεγαλύτερη συσχετισμένη μεταβολή στο mix ήταν «{strongest}»." if strongest else " Καμία μεμονωμένη μεταβολή στο mix δεν κυριάρχησε στο evidence."
    overall_conf = _safe_float((summary.get("confidence") or {}).get("score"), 0.0)
    driver_strength = max([abs(_safe_float(d.get("share_delta"), 0.0)) for d in drivers] or [0.0])
    priority = min(100.0, 45.0 + min(35.0, material * 2.5) + min(20.0, driver_strength * 50.0))
    target_rows = second if abs(_safe_float(delta)) >= max(abs(_safe_float(neg_delta)), abs(_safe_float(anger_delta))) else (second + first)
    return [_make_investigation(
        inv_type="period_shift",
        question_en="What changed between the first and second half of the research period?",
        question_el="Τι άλλαξε μεταξύ του πρώτου και του δεύτερου μισού της περιόδου έρευνας;",
        finding_en=rep_sentence_en + assoc_en + " The relationship is descriptive/associational and does not prove cause.",
        finding_el=rep_sentence_el + assoc_el + " Η σχέση είναι περιγραφική/συσχετιστική και δεν αποδεικνύει αιτιότητα.",
        trigger={"indicator": "time_trends", "deltas": split["deltas"]},
        local_rows=target_rows,
        overall_confidence=overall_conf,
        cleaning_report=cleaning_report,
        priority_score=priority,
        driver_strength=driver_strength,
        drivers=drivers,
        metrics={"first_window": split["first_window"], "second_window": split["second_window"], "deltas": split["deltas"]},
        time_window={"first": {k: split["first_window"][k] for k in ("from", "to")}, "second": {k: split["second_window"][k] for k in ("from", "to")}},
        recommended_visual="period_comparison_waterfall",
        alternative_explanations=["source_mix_change", "sampling_change", "external_event_not_observed_in_dataset", "classification_uncertainty"],
    )]


def _largest_daily_reputation_change(records: list[dict], time_series: dict, summary: dict, cleaning_report: dict) -> list[dict]:
    daily = [x for x in (time_series or {}).get("daily", []) if x.get("brand_reputation_index") is not None]
    if len(daily) < 2:
        return []
    changes = []
    for prev, cur in zip(daily, daily[1:]):
        delta = _safe_float(cur.get("brand_reputation_index")) - _safe_float(prev.get("brand_reputation_index"))
        changes.append((abs(delta), delta, prev, cur))
    changes.sort(reverse=True, key=lambda x: x[0])
    magnitude, delta, prev, cur = changes[0]
    if magnitude < 8.0:
        return []
    target_day = str(cur.get("date"))
    prev_day = str(prev.get("date"))
    target = [r for r in records if _record_day(r) == target_day]
    baseline = [r for r in records if _record_day(r) == prev_day]
    drivers = []
    for dim in ("narrative", "topic", "platform", "origin_group"):
        eligibility = "reputation"
        shifts = _top_shift(_share_by(target, dim, eligibility=eligibility), _share_by(baseline, dim, eligibility=eligibility), 4)
        for item in shifts[:2]:
            if abs(item["share_delta"]) >= 0.05:
                drivers.append({"dimension": "source" if dim == "platform" else ("origin" if dim == "origin_group" else dim), **item})
    strongest = drivers[0]["name"] if drivers else None
    overall_conf = _safe_float((summary.get("confidence") or {}).get("score"), 0.0)
    driver_strength = max([abs(_safe_float(d.get("share_delta"), 0.0)) for d in drivers] or [0.0])
    direction_en = "fell" if delta < 0 else "rose"
    direction_el = "έπεσε" if delta < 0 else "ανέβηκε"
    assoc_en = f" The strongest associated composition shift was '{strongest}'." if strongest else " No single composition shift was strong enough to name safely."
    assoc_el = f" Η ισχυρότερη συσχετισμένη μεταβολή στη σύνθεση του evidence ήταν «{strongest}»." if strongest else " Καμία μεμονωμένη μεταβολή δεν ήταν αρκετά ισχυρή ώστε να ονομαστεί με ασφάλεια."
    return [_make_investigation(
        inv_type="reputation_daily_change",
        question_en=f"Why did Brand Reputation change sharply on {target_day}?",
        question_el=f"Τι συνδέεται με την απότομη μεταβολή του Brand Reputation στις {target_day};",
        finding_en=f"Brand Reputation {direction_en} by {abs(delta):.1f} points from {prev.get('brand_reputation_index')} to {cur.get('brand_reputation_index')}.{assoc_en} Causality is not proven.",
        finding_el=f"Το Brand Reputation {direction_el} κατά {abs(delta):.1f} μονάδες, από {prev.get('brand_reputation_index')} σε {cur.get('brand_reputation_index')}.{assoc_el} Η αιτιότητα δεν θεωρείται αποδεδειγμένη.",
        trigger={"indicator": "brand_reputation", "date": target_day, "previous_date": prev_day, "delta_points": round(delta, 2)},
        local_rows=target,
        overall_confidence=overall_conf,
        cleaning_report=cleaning_report,
        priority_score=min(100.0, 55.0 + magnitude * 2.5),
        driver_strength=driver_strength,
        drivers=drivers,
        metrics={"previous": prev, "current": cur, "delta_points": round(delta, 2)},
        time_window={"baseline": prev_day, "target": target_day},
        recommended_visual="reputation_delta_with_driver_breakdown",
    )]


def _driver_investigations(records: list[dict], summary: dict, cleaning_report: dict) -> list[dict]:
    out = []
    overall_conf = _safe_float((summary.get("confidence") or {}).get("score"), 0.0)
    for polarity, key in (("negative", "top_negative_narrative_drivers"), ("positive", "top_positive_narrative_drivers")):
        drivers = summary.get(key) or []
        if not drivers:
            continue
        top = drivers[0]
        contribution = _safe_float(top.get("reputation_point_contribution"), 0.0)
        if abs(contribution) < 1.0:
            continue
        name = str(top.get("name") or "Unclassified")
        local = [r for r in records if str((r.get("ai_analysis") or {}).get("narrative") or "Unclassified").strip() == name and (r.get("intelligence") or {}).get("reputation_eligible")]
        direction_word_en = "negative" if polarity == "negative" else "positive"
        direction_word_el = "αρνητικός" if polarity == "negative" else "θετικός"
        finding_en = f"'{name}' is the strongest {direction_word_en} narrative driver in the current evidence, contributing {contribution:+.2f} Brand Reputation points relative to neutral. This is deterministic contribution, not a causal claim."
        finding_el = f"Το «{name}» είναι ο ισχυρότερος {direction_word_el} narrative driver στο διαθέσιμο evidence, με συμβολή {contribution:+.2f} μονάδων Brand Reputation ως προς το ουδέτερο σημείο. Πρόκειται για deterministic contribution και όχι για αιτιώδη ισχυρισμό."
        out.append(_make_investigation(
            inv_type=f"{polarity}_narrative_driver",
            question_en=f"What is the strongest {direction_word_en} narrative affecting Brand Reputation?",
            question_el=f"Ποιο είναι το ισχυρότερο {direction_word_el} narrative που επηρεάζει μαθηματικά το Brand Reputation;",
            finding_en=finding_en,
            finding_el=finding_el,
            trigger={"indicator": f"{polarity}_narrative_drivers", "narrative": name, "reputation_point_contribution": contribution},
            local_rows=local,
            overall_confidence=overall_conf,
            cleaning_report=cleaning_report,
            priority_score=min(95.0, 35.0 + abs(contribution) * 8.0),
            driver_strength=min(1.0, abs(contribution) / 10.0),
            drivers=[{"dimension": "narrative", **copy.deepcopy(top)}],
            metrics={"driver": copy.deepcopy(top)},
            recommended_visual="narrative_driver_bar_with_evidence",
            conclusion_type="deterministic_contribution",
            alternative_explanations=["other_smaller_narratives", "source_mix_change"],
        ))
    return out


def _emotion_investigation(records: list[dict], summary: dict, cleaning_report: dict) -> list[dict]:
    emotion = summary.get("emotions_organic_people") or {}
    weighted = emotion.get("weighted_percent") or {}
    candidates = [(k, _safe_float(v)) for k, v in weighted.items() if k != "neutral"]
    if not candidates:
        return []
    candidates.sort(key=lambda x: x[1], reverse=True)
    label, pct = candidates[0]
    if pct < 25.0 or _safe_int(emotion.get("records"), 0) < 5:
        return []
    local = [r for r in records if (r.get("ai_analysis") or {}).get("opinion_eligible") and str((r.get("ai_analysis") or {}).get("primary_emotion")) == label]
    narratives = _share_by(local, "narrative", weight_key="organic_opinion_weight", eligibility="opinion")
    top = sorted(narratives.items(), key=lambda kv: kv[1]["share"], reverse=True)[:3]
    drivers = [{"dimension": "narrative", "name": name, **vals} for name, vals in top]
    overall_conf = _safe_float((summary.get("confidence") or {}).get("score"), 0.0)
    strongest = top[0][0] if top else None
    assoc_en = f" The most common associated narrative within that emotion is '{strongest}'." if strongest else ""
    assoc_el = f" Το συχνότερο συσχετισμένο narrative μέσα σε αυτό το emotion είναι «{strongest}»." if strongest else ""
    return [_make_investigation(
        inv_type="emotion_profile",
        question_en=f"What is behind the prominence of {label} in organic opinion?",
        question_el=f"Τι συνδέεται με την έντονη παρουσία του emotion «{label}» στις organic γνώμες;",
        finding_en=f"{label.title()} represents {pct:.1f}% of weighted organic opinion.{assoc_en} The association does not prove cause.",
        finding_el=f"Το emotion «{label}» αντιστοιχεί στο {pct:.1f}% των weighted organic opinions.{assoc_el} Η συσχέτιση δεν αποδεικνύει αιτιότητα.",
        trigger={"indicator": "emotions", "emotion": label, "weighted_percent": pct},
        local_rows=local,
        overall_confidence=overall_conf,
        cleaning_report=cleaning_report,
        priority_score=min(85.0, 35.0 + pct),
        driver_strength=min(1.0, pct / 100.0),
        drivers=drivers,
        metrics={"emotion_distribution": emotion},
        recommended_visual="emotion_share_with_narrative_breakdown",
    )]


def _source_divergence_investigation(records: list[dict], summary: dict, cleaning_report: dict) -> list[dict]:
    source = summary.get("source_breakdown") or {}
    eligible = [(name, data) for name, data in source.items() if data.get("brand_reputation_index") is not None and _safe_int(data.get("reputation_eligible"), 0) >= 3]
    if len(eligible) < 2:
        return []
    eligible.sort(key=lambda x: _safe_float(x[1].get("brand_reputation_index")))
    low_name, low = eligible[0]
    high_name, high = eligible[-1]
    gap = _safe_float(high.get("brand_reputation_index")) - _safe_float(low.get("brand_reputation_index"))
    if gap < 20.0:
        return []
    local = [r for r in records if str(r.get("platform")) in {low_name, high_name}]
    overall_conf = _safe_float((summary.get("confidence") or {}).get("score"), 0.0)
    return [_make_investigation(
        inv_type="source_divergence",
        question_en="Do different sources tell materially different reputation stories?",
        question_el="Δίνουν διαφορετικές πηγές ουσιαστικά διαφορετική εικόνα για το reputation;",
        finding_en=f"Brand Reputation differs by {gap:.1f} points between {low_name} ({low.get('brand_reputation_index')}) and {high_name} ({high.get('brand_reputation_index')}). This is a source-level difference and should not be interpreted as a population estimate.",
        finding_el=f"Το Brand Reputation διαφέρει κατά {gap:.1f} μονάδες μεταξύ {low_name} ({low.get('brand_reputation_index')}) και {high_name} ({high.get('brand_reputation_index')}). Πρόκειται για διαφορά μεταξύ πηγών και όχι για εκτίμηση του συνολικού πληθυσμού.",
        trigger={"indicator": "source_breakdown", "lowest_source": low_name, "highest_source": high_name, "gap_points": round(gap, 2)},
        local_rows=local,
        overall_confidence=overall_conf,
        cleaning_report=cleaning_report,
        priority_score=min(90.0, 30.0 + gap * 1.5),
        driver_strength=min(1.0, gap / 50.0),
        metrics={"lowest": {"source": low_name, **copy.deepcopy(low)}, "highest": {"source": high_name, **copy.deepcopy(high)}},
        recommended_visual="source_reputation_comparison",
        alternative_explanations=["platform_audience_difference", "source_collection_coverage_difference", "content_format_difference"],
        conclusion_type="descriptive_difference",
    )]


def _origin_divergence_investigation(records: list[dict], summary: dict, cleaning_report: dict) -> list[dict]:
    origin = summary.get("origin_breakdown") or {}
    media = origin.get("media") or {}
    person = origin.get("person") or {}
    if media.get("brand_reputation_index") is None or person.get("brand_reputation_index") is None:
        return []
    if _safe_int(media.get("reputation_eligible"), 0) < 3 or _safe_int(person.get("reputation_eligible"), 0) < 3:
        return []
    gap = _safe_float(person.get("brand_reputation_index")) - _safe_float(media.get("brand_reputation_index"))
    if abs(gap) < 15.0:
        return []
    local = [r for r in records if (r.get("intelligence") or {}).get("origin_group") in {"media", "person"}]
    overall_conf = _safe_float((summary.get("confidence") or {}).get("score"), 0.0)
    warmer = "people" if gap > 0 else "media"
    warmer_el = "οι χρήστες/δημιουργοί" if gap > 0 else "τα media"
    return [_make_investigation(
        inv_type="media_people_divergence",
        question_en="Are media and people framing the brand differently?",
        question_el="Διαμορφώνουν τα media και οι χρήστες διαφορετική εικόνα για το brand;",
        finding_en=f"The media-vs-people Brand Reputation gap is {abs(gap):.1f} points; {warmer} are more positive in the observed evidence. This is a descriptive difference, not a causal conclusion.",
        finding_el=f"Η διαφορά Brand Reputation μεταξύ media και χρηστών είναι {abs(gap):.1f} μονάδες· {warmer_el} εμφανίζονται πιο θετικοί στο διαθέσιμο evidence. Πρόκειται για περιγραφική διαφορά και όχι για αιτιώδες συμπέρασμα.",
        trigger={"indicator": "origin_breakdown", "media_index": media.get("brand_reputation_index"), "people_index": person.get("brand_reputation_index"), "gap_points_people_minus_media": round(gap, 2)},
        local_rows=local,
        overall_confidence=overall_conf,
        cleaning_report=cleaning_report,
        priority_score=min(85.0, 35.0 + abs(gap) * 1.5),
        driver_strength=min(1.0, abs(gap) / 40.0),
        metrics={"media": copy.deepcopy(media), "people": copy.deepcopy(person)},
        recommended_visual="media_people_reputation_comparison",
        alternative_explanations=["different_content_roles", "audience_composition_difference", "source_coverage_difference"],
        conclusion_type="descriptive_difference",
    )]


def _coordination_investigation(records: list[dict], summary: dict, cleaning_report: dict) -> list[dict]:
    clusters = _coordination_clusters(records)
    material = [c for c in clusters if c["records"] >= 3 and c["unique_authors"] >= 2]
    if not material:
        return []
    cluster = material[0]
    ids = set(cluster["record_ids"])
    local = [r for r in records if r.get("id") in ids]
    overall_conf = _safe_float((summary.get("confidence") or {}).get("score"), 0.0)
    avg_auth = cluster.get("average_authenticity_score")
    risk_word = "suspicious" if avg_auth is not None and avg_auth < 60 else "coordinated"
    return [_make_investigation(
        inv_type="coordination_signal",
        question_en="Is coordinated activity materially present in the conversation?",
        question_el="Υπάρχει ουσιαστικά συντονισμένη δραστηριότητα στη συζήτηση;",
        finding_en=f"A {risk_word} cluster contains {cluster['records']} records from {cluster['unique_authors']} authors across {len(cluster['platforms'])} source(s). Coordination is observable; automation or malicious intent is not proven.",
        finding_el=f"Ένα {risk_word} cluster περιλαμβάνει {cluster['records']} records από {cluster['unique_authors']} authors σε {len(cluster['platforms'])} source(s). Ο συντονισμός είναι παρατηρήσιμος· δεν θεωρείται αποδεδειγμένο ούτε automation ούτε κακόβουλη πρόθεση.",
        trigger={"indicator": "coordination", "cluster": copy.deepcopy(cluster)},
        local_rows=local,
        overall_confidence=overall_conf,
        cleaning_report=cleaning_report,
        priority_score=min(95.0, 45.0 + cluster["records"] * 2.0 + cluster["unique_authors"] * 2.0),
        driver_strength=min(1.0, cluster["records"] / max(10.0, len(records) * 0.20)),
        metrics={"cluster": copy.deepcopy(cluster), "all_material_clusters": material[:5]},
        recommended_visual="coordination_cluster_evidence",
        alternative_explanations=["legitimate_campaign_or_shared_source", "news_syndication", "community_copying"],
        conclusion_type="risk_signal",
    )]


def _quality_investigation(records: list[dict], summary: dict, cleaning_report: dict) -> list[dict]:
    quality = _safe_float(cleaning_report.get("data_quality_score"), 0.0)
    coverage = _safe_float(cleaning_report.get("source_coverage_ratio"), 0.0)
    achievement = _safe_float(cleaning_report.get("sample_achievement_ratio"), 0.0)
    issues = []
    if quality < 75:
        issues.append(f"data quality {quality:.0f}/100")
    if coverage < 0.75:
        issues.append(f"source coverage {coverage*100:.0f}%")
    if achievement < 0.75:
        issues.append(f"sample achievement {achievement*100:.0f}%")
    if not issues:
        return []
    overall_conf = _safe_float((summary.get("confidence") or {}).get("score"), 0.0)
    severity = max(100 - quality, (1 - coverage) * 100, (1 - achievement) * 100)
    return [_make_investigation(
        inv_type="data_quality_guardrail",
        question_en="Are there data-quality limitations that should change how the findings are presented?",
        question_el="Υπάρχουν περιορισμοί ποιότητας δεδομένων που πρέπει να αλλάξουν τον τρόπο παρουσίασης των ευρημάτων;",
        finding_en=f"Quality guardrails were triggered: {', '.join(issues)}. These limitations must remain visible in internal review and any client-facing interpretation.",
        finding_el=f"Ενεργοποιήθηκαν quality guardrails: {', '.join(issues)}. Οι περιορισμοί αυτοί πρέπει να παραμείνουν ορατοί στο internal review και σε κάθε client-facing ερμηνεία.",
        trigger={"indicator": "data_quality", "quality": quality, "coverage": coverage, "sample_achievement": achievement},
        local_rows=records,
        overall_confidence=overall_conf,
        cleaning_report=cleaning_report,
        priority_score=min(95.0, 45.0 + severity * 0.5),
        metrics={"data_quality_score": quality, "source_coverage_ratio": coverage, "sample_achievement_ratio": achievement, "step5_warnings": summary.get("warnings") or []},
        recommended_visual="quality_guardrail_panel",
        alternative_explanations=[],
        conclusion_type="quality_guardrail",
    )]


def _emerging_narrative_investigation(records: list[dict], summary: dict, cleaning_report: dict, plan: dict) -> list[dict]:
    split = _period_split(records, plan)
    if not split.get("available"):
        return []
    first = split["first_rows"]
    second = split["second_rows"]
    first_share = _share_by(first, "narrative")
    second_share = _share_by(second, "narrative")
    shifts = _top_shift(second_share, first_share, 10)
    candidates = []
    for item in shifts:
        second_data = second_share.get(item["name"], {})
        if item["share_delta"] >= 0.12 and _safe_int(second_data.get("records"), 0) >= 3 and _safe_float(second_data.get("share"), 0.0) >= 0.15:
            candidates.append(item)
    if not candidates:
        return []
    item = candidates[0]
    name = item["name"]
    local = [r for r in second if str((r.get("ai_analysis") or {}).get("narrative") or "Unclassified").strip() == name]
    avg_sent = _safe_float((second_share.get(name) or {}).get("average_sentiment"), 0.0)
    polarity = "negative" if avg_sent < -0.15 else ("positive" if avg_sent > 0.15 else "mixed/neutral")
    polarity_el = "αρνητικό" if avg_sent < -0.15 else ("θετικό" if avg_sent > 0.15 else "μικτό/ουδέτερο")
    overall_conf = _safe_float((summary.get("confidence") or {}).get("score"), 0.0)
    return [_make_investigation(
        inv_type="emerging_narrative",
        question_en="Is a new or accelerating narrative emerging during the research period?",
        question_el="Εμφανίζεται ή επιταχύνεται κάποιο νέο narrative μέσα στην περίοδο έρευνας;",
        finding_en=f"'{name}' increased its weighted evidence share by {item['share_delta']*100:.1f} percentage points in the second half and is {polarity} on average. This supports an emerging/accelerating signal, not proof of an external cause.",
        finding_el=f"Το «{name}» αύξησε το weighted evidence share του κατά {item['share_delta']*100:.1f} ποσοστιαίες μονάδες στο δεύτερο μισό και είναι κατά μέσο όρο {polarity_el}. Αυτό στηρίζει signal εμφάνισης/επιτάχυνσης, όχι απόδειξη εξωτερικής αιτίας.",
        trigger={"indicator": "narrative_drivers", "narrative": name, "share_delta": item["share_delta"], "second_share": item["target_share"]},
        local_rows=local,
        overall_confidence=overall_conf,
        cleaning_report=cleaning_report,
        priority_score=min(90.0, 45.0 + item["share_delta"] * 180.0),
        driver_strength=min(1.0, item["share_delta"] * 3.0),
        drivers=[{"dimension": "narrative", **copy.deepcopy(item)}],
        metrics={"narrative_shift": copy.deepcopy(item), "average_sentiment_second_half": avg_sent},
        time_window={"first": {k: split["first_window"][k] for k in ("from", "to")}, "second": {k: split["second_window"][k] for k in ("from", "to")}},
        recommended_visual="narrative_emergence_time_share",
    )]


def _dedupe_investigations(items: list[dict]) -> list[dict]:
    # Keep distinct trigger types, but avoid two investigations with the exact same type/date/narrative.
    seen = set()
    out = []
    for item in items:
        trigger = item.get("trigger") or {}
        key = (
            item.get("type"),
            trigger.get("date"),
            trigger.get("narrative"),
            trigger.get("lowest_source"),
            trigger.get("cluster", {}).get("cluster_id") if isinstance(trigger.get("cluster"), dict) else None,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=lambda x: (-x.get("priority_score", 0), -x.get("confidence", {}).get("score", 0), x.get("type", ""), json.dumps(x.get("trigger") or {}, sort_keys=True, default=str)))
    for i, item in enumerate(out, 1):
        item["investigation_id"] = f"INV-{i:03d}"
    return out


def compute_investigations(
    intelligence_records: list[dict],
    intelligence_summary: dict,
    time_series: dict,
    plan: dict,
    cleaning_report: dict | None = None,
) -> dict:
    if not isinstance(intelligence_records, list):
        raise TypeError("intelligence_records must be a list")
    if not isinstance(intelligence_summary, dict):
        raise TypeError("intelligence_summary must be a dict")
    if intelligence_summary.get("stale"):
        raise RuntimeError("Step 6 refuses stale Step 5 intelligence.")
    original_records = copy.deepcopy(intelligence_records)
    ids = [str(r.get("id") or "") for r in intelligence_records]
    if any(not rid for rid in ids):
        raise RuntimeError("Every Step 5 record must have a stable id before investigation.")
    if len(ids) != len(set(ids)):
        raise RuntimeError("Step 5 intelligence contains duplicate record ids; Step 6 stopped before investigation.")
    cleaning_report = cleaning_report or {}

    # Step 6 must investigate the exact research frame that Step 5 computed.
    # A changed plan after Step 5 would otherwise let period comparisons silently use
    # different dates/market/topic than the persisted Intelligence evidence.
    summary_context = intelligence_summary.get("research_context") or {}
    effective_plan = copy.deepcopy(plan or {})
    for key in ("client", "topic", "market", "date_from", "date_to"):
        persisted = summary_context.get(key)
        requested = effective_plan.get(key)
        if persisted not in (None, ""):
            if requested not in (None, "") and str(requested) != str(persisted):
                raise RuntimeError(f"Step 6 research context mismatch for {key}; rebuild Step 5 before investigating a changed plan.")
            effective_plan[key] = persisted

    inventory = _indicator_inventory(intelligence_summary, time_series, intelligence_records, cleaning_report)
    # Contract invariant: every registered indicator must be examined exactly once.
    inventory_ids = [x["indicator_id"] for x in inventory]
    expected_ids = [x[0] for x in INDICATOR_REGISTRY]
    if inventory_ids != expected_ids or len(inventory_ids) != len(set(inventory_ids)):
        raise RuntimeError("Indicator coverage contract was not satisfied; Step 6 stopped rather than omit an indicator silently.")

    items = []
    items += _anomaly_investigations(intelligence_records, time_series, intelligence_summary, cleaning_report)
    items += _largest_daily_reputation_change(intelligence_records, time_series, intelligence_summary, cleaning_report)
    items += _period_change_investigation(intelligence_records, intelligence_summary, cleaning_report, effective_plan)
    items += _driver_investigations(intelligence_records, intelligence_summary, cleaning_report)
    items += _emotion_investigation(intelligence_records, intelligence_summary, cleaning_report)
    items += _emerging_narrative_investigation(intelligence_records, intelligence_summary, cleaning_report, effective_plan)
    items += _source_divergence_investigation(intelligence_records, intelligence_summary, cleaning_report)
    items += _origin_divergence_investigation(intelligence_records, intelligence_summary, cleaning_report)
    items += _coordination_investigation(intelligence_records, intelligence_summary, cleaning_report)
    items += _quality_investigation(intelligence_records, intelligence_summary, cleaning_report)
    items = _dedupe_investigations(items)

    if intelligence_records != original_records:
        raise RuntimeError("Step 6 mutated Step 5 evidence, which is forbidden.")

    available_count = sum(1 for x in inventory if x["available"])
    high_count = sum(1 for x in items if x["priority"] == "high")
    medium_count = sum(1 for x in items if x["priority"] == "medium")
    presentation_candidates = sum(1 for x in items if (x.get("presentation") or {}).get("candidate"))
    input_hash = _input_hash(intelligence_summary, intelligence_records, time_series, cleaning_report)

    summary = {
        "ruleset_version": INVESTIGATION_RULESET_VERSION,
        "methodology_version": INVESTIGATION_METHODOLOGY_VERSION,
        "evidence_contract_version": PRESENTATION_EVIDENCE_CONTRACT_VERSION,
        "generated_at": _utcnow(),
        "input_hash": input_hash,
        "research_context": copy.deepcopy(intelligence_summary.get("research_context") or {
            "client": effective_plan.get("client"), "topic": effective_plan.get("topic"), "market": effective_plan.get("market"),
            "date_from": effective_plan.get("date_from"), "date_to": effective_plan.get("date_to"),
        }),
        "investigation_count": len(items),
        "high_priority_count": high_count,
        "medium_priority_count": medium_count,
        "presentation_candidate_count": presentation_candidates,
        "indicator_contract": {
            "required": len(INDICATOR_REGISTRY),
            "examined": len(inventory),
            "available": available_count,
            "omitted": 0,
            "complete": len(inventory) == len(INDICATOR_REGISTRY),
        },
        "causal_guardrail": "No Step 6 finding is labelled as proven causality. Findings are deterministic contribution, descriptive difference, risk signal, or evidence-supported association.",
        "boundary": "Automatic Investigations explains what changed in the observed evidence and surfaces associated drivers. It does not prove external causality or market representativeness.",
    }

    methodology = {
        "ruleset_version": INVESTIGATION_RULESET_VERSION,
        "methodology_version": INVESTIGATION_METHODOLOGY_VERSION,
        "evidence_contract_version": PRESENTATION_EVIDENCE_CONTRACT_VERSION,
        "indicator_completeness": "Every registered Step 3/Step 5/presentation-critical indicator is examined and written into the Evidence Pack even when it does not trigger an investigation.",
        "trigger_families": [
            "numeric anomaly", "daily reputation change", "period shift", "narrative driver", "emotion prominence",
            "emerging narrative", "source divergence", "media-vs-people divergence", "coordination signal", "data quality guardrail",
        ],
        "attribution": "Driver shifts are calculated from observed weighted evidence composition; deterministic Reputation contributions retain Step 5 arithmetic semantics.",
        "causality": "Association is never automatically promoted to causality. External causes require separately observed evidence and, where appropriate, human validation.",
        "presentation_contract": "Step 8 must consume the complete evidence-pack indicator inventory, not only high-priority investigations.",
        "raw_evidence": "Step 6 never mutates RAW, Cleaning, AI Analysis, or Step 5 Intelligence evidence.",
    }

    evidence_pack = {
        "contract_version": PRESENTATION_EVIDENCE_CONTRACT_VERSION,
        "generated_at": summary["generated_at"],
        "research_context": copy.deepcopy(summary["research_context"]),
        "indicator_inventory": inventory,
        "headline_metrics": {
            "sample_volume": copy.deepcopy(next((x["value"] for x in inventory if x["indicator_id"] == "sample_volume"), {})),
            "market_relevance": copy.deepcopy(next((x["value"] for x in inventory if x["indicator_id"] == "market_relevance"), {})),
            "top_mentions": copy.deepcopy(next((x["value"] for x in inventory if x["indicator_id"] == "top_mentions"), [])),
            "brand_reputation": copy.deepcopy(intelligence_summary.get("brand_reputation") or {}),
            "evidence_confidence": copy.deepcopy(intelligence_summary.get("confidence") or {}),
            "sentiment": copy.deepcopy(intelligence_summary.get("sentiment") or {}),
            "emotions": copy.deepcopy(intelligence_summary.get("emotions_organic_people") or {}),
            "stance": copy.deepcopy(intelligence_summary.get("stance") or {}),
            "origin_breakdown": copy.deepcopy(intelligence_summary.get("origin_breakdown") or {}),
            "source_breakdown": copy.deepcopy(intelligence_summary.get("source_breakdown") or {}),
            "positive_narrative_drivers": copy.deepcopy(intelligence_summary.get("top_positive_narrative_drivers") or []),
            "negative_narrative_drivers": copy.deepcopy(intelligence_summary.get("top_negative_narrative_drivers") or []),
            "topic_drivers": copy.deepcopy(intelligence_summary.get("topic_drivers") or []),
            "media_influence": copy.deepcopy(intelligence_summary.get("media_influence") or []),
            "people_influence": copy.deepcopy(intelligence_summary.get("people_influence") or []),
            "time_series": copy.deepcopy(time_series or {}),
            "step5_warnings": copy.deepcopy(intelligence_summary.get("warnings") or []),
        },
        "investigations": copy.deepcopy(items),
        "presentation_guardrails": {
            "must_review_all_indicators": True,
            "must_preserve_quality_warnings": True,
            "must_not_present_association_as_causation": True,
            "must_attach_evidence_to_material_claims": True,
            "may_omit_uninteresting_slide": True,
            "omitting_slide_does_not_mean_omitting_indicator_review": True,
        },
    }

    audit = [{
        "investigation_id": inv["investigation_id"],
        "type": inv["type"],
        "priority": inv["priority"],
        "confidence": inv["confidence"],
        "evidence_status": inv["evidence_status"],
        "causal_status": inv["causal_status"],
        "trigger": inv["trigger"],
        "evidence_record_ids": inv["evidence_record_ids"],
        "presentation_candidate": inv["presentation"]["candidate"],
    } for inv in items]

    return {
        "summary": summary,
        "indicator_inventory": inventory,
        "investigations": items,
        "evidence_pack": evidence_pack,
        "audit": audit,
        "methodology": methodology,
    }


def persist_investigations(folder: Path, result: dict) -> dict:
    store = RunStore()
    base = folder / "investigations"
    store.write(base / "summary.json", result["summary"])
    store.write(base / "indicator-inventory.json", result["indicator_inventory"])
    store.write(base / "investigations.json", result["investigations"])
    store.write(base / "evidence-pack.json", result["evidence_pack"])
    store.write(base / "audit.json", result["audit"])
    store.write(base / "methodology.json", result["methodology"])
    return result["summary"]


def build_investigations(folder: Path, plan: dict, force: bool = False, cancel_check: Callable[[], bool] | None = None) -> dict:
    store = RunStore()
    summary_path = folder / "intelligence" / "summary.json"
    records_path = folder / "intelligence" / "records.json"
    time_path = folder / "intelligence" / "time-series.json"
    if not summary_path.exists() or not records_path.exists() or not time_path.exists():
        raise RuntimeError("Step 6 requires complete Step 5 intelligence evidence.")
    if cancel_check and cancel_check():
        raise InvestigationCancelled("Investigation cancelled before processing.")
    intelligence_summary = store.read(summary_path, {}) or {}
    intelligence_records = store.read(records_path, []) or []
    time_series = store.read(time_path, {}) or {}
    cleaning_report = store.read(folder / "cleaning" / "report.json", {}) or {}
    current_hash = _input_hash(intelligence_summary, intelligence_records, time_series, cleaning_report)
    existing = store.read(folder / "investigations" / "summary.json")
    if isinstance(existing, dict) and not force and existing.get("input_hash") == current_hash and existing.get("ruleset_version") == INVESTIGATION_RULESET_VERSION:
        return existing
    if cancel_check and cancel_check():
        raise InvestigationCancelled("Investigation cancelled before evidence synthesis.")
    result = compute_investigations(intelligence_records, intelligence_summary, time_series, plan=plan, cleaning_report=cleaning_report)
    if cancel_check and cancel_check():
        raise InvestigationCancelled("Investigation cancelled before persistence.")
    persist_investigations(folder, result)
    return result["summary"]


def load_investigation_summary(folder: Path) -> dict | None:
    store = RunStore()
    report = store.read(folder / "investigations" / "summary.json")
    if not isinstance(report, dict):
        return None
    intelligence_summary = store.read(folder / "intelligence" / "summary.json", {}) or {}
    intelligence_records = store.read(folder / "intelligence" / "records.json", []) or []
    time_series = store.read(folder / "intelligence" / "time-series.json", {}) or {}
    cleaning_report = store.read(folder / "cleaning" / "report.json", {}) or {}
    current_hash = _input_hash(intelligence_summary, intelligence_records, time_series, cleaning_report)
    out = copy.deepcopy(report)
    out["stale"] = bool(out.get("input_hash") != current_hash or out.get("ruleset_version") != INVESTIGATION_RULESET_VERSION)
    return out


def load_evidence_pack(folder: Path) -> dict | None:
    store = RunStore()
    pack = store.read(folder / "investigations" / "evidence-pack.json")
    return pack if isinstance(pack, dict) else None
