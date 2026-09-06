from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.services.storage import RunStore
from app.services.normalizer import VIEW_KEYS, LIKE_KEYS, COMMENT_KEYS, SHARE_KEYS, FOLLOWER_KEYS, nested

INTELLIGENCE_RULESET_VERSION = "1.0.0"
METHODOLOGY_VERSION = "signalyth-intelligence-v1"

ORIGIN_GROUPS = ("media", "person", "brand_owned", "organization", "unknown")
SENTIMENT_LABELS = ("positive", "negative", "neutral", "mixed")
EMOTION_LABELS = ("joy", "anger", "sadness", "fear", "disgust", "surprise", "neutral")
STANCE_LABELS = ("supportive", "critical", "neutral", "mixed", "not_applicable")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


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


def _input_hash(records: list[dict], cleaning_report: dict | None = None) -> str:
    compact = []
    for row in records:
        ai = row.get("ai_analysis") or {}
        cleaning = row.get("cleaning") or {}
        compact.append({
            "id": row.get("id"),
            "platform": row.get("platform"),
            "date": row.get("date"),
            "author": row.get("author"),
            "followers": row.get("followers"),
            "views": row.get("views"),
            "likes": row.get("likes"),
            "comments": row.get("comments"),
            "shares": row.get("shares"),
            "cleaning": {
                "decision": cleaning.get("decision"),
                "confidence": cleaning.get("confidence"),
                "authenticity_score": cleaning.get("authenticity_score"),
                "origin_class": cleaning.get("origin_class"),
                "content_class": cleaning.get("content_class"),
                "organic_eligible": cleaning.get("organic_eligible"),
                "independent_voice_weight": cleaning.get("independent_voice_weight"),
                "story_cluster_id": cleaning.get("story_cluster_id"),
                "coordination_cluster_id": cleaning.get("coordination_cluster_id"),
            },
            "ai": {
                "decision": ai.get("decision"),
                "semantic_relevance": ai.get("semantic_relevance"),
                "relevance_confidence": ai.get("relevance_confidence"),
                "sentiment_label": ai.get("sentiment_label"),
                "sentiment_score": ai.get("sentiment_score"),
                "sentiment_confidence": ai.get("sentiment_confidence"),
                "primary_emotion": ai.get("primary_emotion"),
                "emotion_intensity": ai.get("emotion_intensity"),
                "emotion_confidence": ai.get("emotion_confidence"),
                "target_stance": ai.get("target_stance"),
                "topic": ai.get("topic"),
                "narrative": ai.get("narrative"),
                "overall_confidence": ai.get("overall_confidence"),
                "opinion_eligible": ai.get("opinion_eligible"),
            },
        })
    payload = {
        "records": compact,
        "cleaning_report": {
            "data_quality_score": (cleaning_report or {}).get("data_quality_score"),
            "source_coverage_ratio": (cleaning_report or {}).get("source_coverage_ratio"),
            "sample_achievement_ratio": (cleaning_report or {}).get("sample_achievement_ratio"),
        },
        "ruleset": INTELLIGENCE_RULESET_VERSION,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _percentile(values: list[float], q: float) -> float | None:
    vals = sorted(v for v in values if v > 0 and math.isfinite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return vals[low]
    frac = pos - low
    return vals[low] * (1 - frac) + vals[high] * frac


def _metric_reference(records: list[dict]) -> dict[str, dict[str, float | None]]:
    metrics = ("views", "likes", "comments", "shares", "followers")
    by_platform: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    global_vals: dict[str, list[float]] = defaultdict(list)
    for row in records:
        p = str(row.get("platform") or "unknown")
        for metric in metrics:
            v = _safe_float(row.get(metric), 0.0)
            if v > 0:
                by_platform[p][metric].append(v)
                global_vals[metric].append(v)

    global_ref = {m: _percentile(global_vals[m], 0.95) for m in metrics}
    out: dict[str, dict[str, float | None]] = {}
    for p in {str(r.get("platform") or "unknown") for r in records}:
        out[p] = {}
        for m in metrics:
            local = _percentile(by_platform[p][m], 0.95)
            # Small platform samples can be unstable; fall back to the run-wide reference.
            if len(by_platform[p][m]) < 5:
                local = global_ref[m]
            out[p][m] = local
    out["__global__"] = global_ref
    return out


def _metric_present(row: dict, keys: tuple[str, ...], normalized_value) -> bool:
    if _safe_float(normalized_value, 0.0) > 0:
        return True
    raw = row.get("raw_data") or {}
    if not isinstance(raw, dict):
        return False
    return any(nested(raw, key) is not None for key in keys)


def _norm_metric(value: int | float, reference: float | None, present: bool) -> float | None:
    if not present:
        return None
    v = max(0.0, _safe_float(value, 0.0))
    if v == 0:
        return 0.0
    if not reference or reference <= 0:
        return 1.0
    return _clamp(math.log1p(v) / max(1e-12, math.log1p(reference)))


def _impact_components(row: dict, refs: dict[str, dict[str, float | None]]) -> dict:
    platform = str(row.get("platform") or "unknown")
    ref = refs.get(platform) or refs.get("__global__", {})
    views_n = _safe_int(row.get("views")); followers_n = _safe_int(row.get("followers"))
    likes_n = _safe_int(row.get("likes")); comments_n = _safe_int(row.get("comments")); shares_n = _safe_int(row.get("shares"))
    views = _norm_metric(views_n, ref.get("views"), _metric_present(row, VIEW_KEYS, views_n))
    followers = _norm_metric(followers_n, ref.get("followers"), _metric_present(row, FOLLOWER_KEYS, followers_n))
    likes = _norm_metric(likes_n, ref.get("likes"), _metric_present(row, LIKE_KEYS, likes_n))
    comments = _norm_metric(comments_n, ref.get("comments"), _metric_present(row, COMMENT_KEYS, comments_n))
    shares = _norm_metric(shares_n, ref.get("shares"), _metric_present(row, SHARE_KEYS, shares_n))

    engagement_parts = []
    for val, w in ((likes, 0.30), (comments, 0.30), (shares, 0.40)):
        if val is not None:
            engagement_parts.append((val, w))
    engagement = None
    if engagement_parts:
        den = sum(w for _, w in engagement_parts)
        engagement = sum(v * w for v, w in engagement_parts) / den

    available = []
    for name, val, w in (("visibility", views, 0.50), ("engagement", engagement, 0.30), ("authority", followers, 0.20)):
        if val is not None:
            available.append((name, val, w))

    if not available:
        impact = 0.50  # Unknown, not zero. Missing public metrics must not be interpreted as no impact.
        confidence = 0.0
    else:
        den = sum(w for _, _, w in available)
        impact = sum(v * w for _, v, w in available) / den
        confidence = sum(w for _, _, w in available) / 1.0

    return {
        "impact_score": round(_clamp(impact), 6),
        "impact_confidence": round(_clamp(confidence), 6),
        "visibility_score": None if views is None else round(views, 6),
        "engagement_score": None if engagement is None else round(engagement, 6),
        "authority_score": None if followers is None else round(followers, 6),
        "available_signals": [name for name, _, _ in available],
    }


def _origin_group(row: dict) -> str:
    cleaning = row.get("cleaning") or {}
    origin = str(cleaning.get("origin_class") or "unknown")
    if origin in {"media", "earned_media"}:
        return "media"
    if origin in {"person", "earned_person"}:
        return "person"
    if origin in {"brand_owned", "owned"}:
        return "brand_owned"
    if origin in {"organization", "earned_organization"}:
        return "organization"
    # Backward-compatible fallback using account/content classification.
    account = str(cleaning.get("account_type") or "")
    content = str(cleaning.get("content_class") or "")
    if account == "media" or content == "news":
        return "media"
    if account == "person_or_creator":
        return "person"
    if account == "brand_owned" or content == "owned":
        return "brand_owned"
    if account == "organization":
        return "organization"
    return "unknown"


def _evidence_confidence(row: dict) -> float:
    ai = row.get("ai_analysis") or {}
    cleaning = row.get("cleaning") or {}
    return _clamp(
        0.45 * _safe_float(ai.get("overall_confidence"), 0.0)
        + 0.25 * _safe_float(ai.get("sentiment_confidence"), 0.0)
        + 0.15 * _safe_float(ai.get("relevance_confidence"), 0.0)
        + 0.15 * _safe_float(cleaning.get("confidence"), 0.0)
    )


def _reputation_eligible(row: dict) -> tuple[bool, str]:
    ai = row.get("ai_analysis") or {}
    cleaning = row.get("cleaning") or {}
    if ai.get("decision") != "ready":
        return False, "not_analysis_ready"
    if ai.get("semantic_relevance") != "relevant":
        return False, "not_semantically_relevant"
    if cleaning.get("content_class") in {"owned", "promotional"}:
        return False, "owned_or_promotional"
    if ai.get("target_stance") == "not_applicable":
        return False, "no_target_stance"
    if _safe_float(ai.get("sentiment_confidence"), 0.0) <= 0:
        return False, "no_sentiment_confidence"
    return True, "eligible"


def _record_weights(row: dict, impact: dict) -> dict:
    cleaning = row.get("cleaning") or {}
    ai = row.get("ai_analysis") or {}
    independent = _clamp(_safe_float(cleaning.get("independent_voice_weight"), 1.0), 0.0, 1.0)
    authenticity = _clamp(_safe_float(cleaning.get("authenticity_score"), 100.0) / 100.0, 0.0, 1.0)
    evidence = _evidence_confidence(row)
    impact_score = _clamp(_safe_float(impact.get("impact_score"), 0.5))
    # Impact amplifies evidence but is deliberately bounded so one viral mention cannot dominate a run.
    attention_multiplier = 0.40 + 0.60 * impact_score
    eligible, reason = _reputation_eligible(row)
    rep_weight = independent * authenticity * evidence * attention_multiplier if eligible else 0.0
    opinion_weight = rep_weight if bool(ai.get("opinion_eligible")) else 0.0
    return {
        "independent_voice_weight": round(independent, 6),
        "authenticity_factor": round(authenticity, 6),
        "evidence_confidence": round(evidence, 6),
        "attention_multiplier": round(attention_multiplier, 6),
        "reputation_eligible": eligible,
        "reputation_exclusion_reason": reason,
        "reputation_weight": round(rep_weight, 8),
        "organic_opinion_weight": round(opinion_weight, 8),
    }


def _weighted_mean(rows: Iterable[dict], value_fn, weight_fn) -> float | None:
    num = 0.0
    den = 0.0
    for row in rows:
        w = max(0.0, _safe_float(weight_fn(row), 0.0))
        if w <= 0:
            continue
        v = _safe_float(value_fn(row), 0.0)
        num += v * w
        den += w
    return None if den <= 0 else num / den


def _brand_reputation(records: list[dict]) -> dict:
    eligible = [r for r in records if (r.get("intelligence") or {}).get("reputation_eligible")]
    weighted_sentiment = _weighted_mean(
        eligible,
        lambda r: (r.get("ai_analysis") or {}).get("sentiment_score", 0.0),
        lambda r: (r.get("intelligence") or {}).get("reputation_weight", 0.0),
    )
    if weighted_sentiment is None:
        return {
            "index": None,
            "weighted_sentiment": None,
            "eligible_records": 0,
            "effective_weight": 0.0,
            "interpretation": "insufficient_evidence",
        }
    score = _clamp((weighted_sentiment + 1.0) / 2.0) * 100.0
    return {
        "index": round(score, 2),
        "weighted_sentiment": round(weighted_sentiment, 6),
        "eligible_records": len(eligible),
        "effective_weight": round(sum(_safe_float((r.get("intelligence") or {}).get("reputation_weight"), 0.0) for r in eligible), 6),
        "interpretation": "negative" if score < 45 else ("positive" if score > 55 else "neutral_band"),
    }


def _effective_sample_size(weights: list[float]) -> float:
    ws = [w for w in weights if w > 0]
    if not ws:
        return 0.0
    s = sum(ws)
    sq = sum(w * w for w in ws)
    return 0.0 if sq <= 0 else (s * s) / sq


def _confidence_report(records: list[dict], cleaning_report: dict | None, brand_rep: dict) -> dict:
    eligible = [r for r in records if (r.get("intelligence") or {}).get("reputation_eligible")]
    weights = [_safe_float((r.get("intelligence") or {}).get("reputation_weight"), 0.0) for r in eligible]
    ess = _effective_sample_size(weights)
    avg_evidence = statistics.fmean([
        _safe_float((r.get("intelligence") or {}).get("evidence_confidence"), 0.0) for r in eligible
    ]) if eligible else 0.0
    quality = _clamp(_safe_float((cleaning_report or {}).get("data_quality_score"), 0.0) / 100.0)
    coverage = _clamp(_safe_float((cleaning_report or {}).get("source_coverage_ratio"), 0.0))
    # 100 effective independent voices gives full sample-size credit; larger samples do not create fake certainty.
    sample_factor = _clamp(math.sqrt(max(0.0, ess) / 100.0))
    score = 100.0 * (0.35 * sample_factor + 0.30 * quality + 0.20 * coverage + 0.15 * avg_evidence)
    if not brand_rep.get("eligible_records"):
        score = 0.0
    label = "high" if score >= 75 else ("medium" if score >= 50 else "low")
    return {
        "score": round(score, 2),
        "label": label,
        "effective_sample_size": round(ess, 2),
        "average_evidence_confidence": round(avg_evidence, 4),
        "data_quality_component": round(quality, 4),
        "source_coverage_component": round(coverage, 4),
        "note": "Confidence describes strength/coverage of the collected evidence; it is not a claim of statistical representativeness.",
    }


def _distribution(records: list[dict], field: str, labels: tuple[str, ...], weight_key: str | None = None, only_opinion: bool = False, only_reputation: bool = False) -> dict:
    counts = Counter()
    weighted = Counter()
    total_w = 0.0
    total_n = 0
    for row in records:
        ai = row.get("ai_analysis") or {}
        intel = row.get("intelligence") or {}
        if only_opinion and not ai.get("opinion_eligible"):
            continue
        if only_reputation and not intel.get("reputation_eligible"):
            continue
        label = str(ai.get(field) or "")
        if label not in labels:
            continue
        counts[label] += 1
        total_n += 1
        w = _safe_float(intel.get(weight_key), 1.0) if weight_key else 1.0
        if w > 0:
            weighted[label] += w
            total_w += w
    return {
        "counts": {k: int(counts[k]) for k in labels},
        "percent": {k: round((counts[k] / total_n * 100.0) if total_n else 0.0, 2) for k in labels},
        "weighted_percent": {k: round((weighted[k] / total_w * 100.0) if total_w else 0.0, 2) for k in labels},
        "records": total_n,
        "effective_weight": round(total_w, 6),
    }


def _group_contributions(records: list[dict], key: str, limit: int = 12) -> list[dict]:
    eligible = [r for r in records if (r.get("intelligence") or {}).get("reputation_eligible")]
    total_w = sum(_safe_float((r.get("intelligence") or {}).get("reputation_weight"), 0.0) for r in eligible)
    groups: dict[str, dict] = {}
    for row in eligible:
        ai = row.get("ai_analysis") or {}
        label = str(ai.get(key) or "Unclassified").strip() or "Unclassified"
        g = groups.setdefault(label, {"name": label, "mention_count": 0, "effective_weight": 0.0, "sentiment_numerator": 0.0, "impact_sum": 0.0})
        w = _safe_float((row.get("intelligence") or {}).get("reputation_weight"), 0.0)
        s = _safe_float(ai.get("sentiment_score"), 0.0)
        g["mention_count"] += 1
        g["effective_weight"] += w
        g["sentiment_numerator"] += w * s
        g["impact_sum"] += _safe_float((row.get("intelligence") or {}).get("impact_score"), 0.5)

    out = []
    for g in groups.values():
        ew = g["effective_weight"]
        avg_s = g["sentiment_numerator"] / ew if ew > 0 else 0.0
        contribution = 50.0 * g["sentiment_numerator"] / total_w if total_w > 0 else 0.0
        out.append({
            "name": g["name"],
            "mention_count": g["mention_count"],
            "effective_weight": round(ew, 6),
            "average_sentiment": round(avg_s, 6),
            "reputation_point_contribution": round(contribution, 4),
            "average_impact": round(g["impact_sum"] / max(1, g["mention_count"]), 4),
        })
    out.sort(key=lambda x: (abs(x["reputation_point_contribution"]), x["effective_weight"]), reverse=True)
    return out[:limit]


def _source_breakdown(records: list[dict]) -> dict:
    out = {}
    for platform in sorted({str(r.get("platform") or "unknown") for r in records}):
        rows = [r for r in records if str(r.get("platform") or "unknown") == platform]
        rep = _brand_reputation(rows)
        total_impact = sum(_safe_float((r.get("intelligence") or {}).get("impact_score"), 0.0) for r in rows)
        eligible = sum(1 for r in rows if (r.get("intelligence") or {}).get("reputation_eligible"))
        out[platform] = {
            "records": len(rows),
            "reputation_eligible": eligible,
            "brand_reputation_index": rep.get("index"),
            "average_impact": round(total_impact / max(1, len(rows)), 4),
            "organic_opinion_records": sum(1 for r in rows if (r.get("ai_analysis") or {}).get("opinion_eligible")),
        }
    return out


def _origin_breakdown(records: list[dict]) -> dict:
    out = {}
    total_attention = sum(_safe_float((r.get("intelligence") or {}).get("impact_score"), 0.0) for r in records)
    for origin in ORIGIN_GROUPS:
        rows = [r for r in records if (r.get("intelligence") or {}).get("origin_group") == origin]
        rep = _brand_reputation(rows)
        attention = sum(_safe_float((r.get("intelligence") or {}).get("impact_score"), 0.0) for r in rows)
        out[origin] = {
            "records": len(rows),
            "record_share_percent": round(len(rows) / max(1, len(records)) * 100.0, 2),
            "attention_share_percent": round(attention / total_attention * 100.0, 2) if total_attention > 0 else 0.0,
            "brand_reputation_index": rep.get("index"),
            "reputation_eligible": rep.get("eligible_records", 0),
        }
    return out


def _author_rankings(records: list[dict], origin: str, limit: int = 10) -> list[dict]:
    groups: dict[str, dict] = {}
    for row in records:
        intel = row.get("intelligence") or {}
        if intel.get("origin_group") != origin:
            continue
        author = str(row.get("author") or "Unknown").strip() or "Unknown"
        g = groups.setdefault(author, {"author": author, "records": 0, "impact": 0.0, "rep_num": 0.0, "rep_weight": 0.0})
        g["records"] += 1
        g["impact"] += _safe_float(intel.get("impact_score"), 0.0)
        w = _safe_float(intel.get("reputation_weight"), 0.0)
        g["rep_num"] += w * _safe_float((row.get("ai_analysis") or {}).get("sentiment_score"), 0.0)
        g["rep_weight"] += w
    out = []
    for g in groups.values():
        sentiment = g["rep_num"] / g["rep_weight"] if g["rep_weight"] else None
        out.append({
            "author": g["author"],
            "records": g["records"],
            "attention_score_sum": round(g["impact"], 4),
            "weighted_sentiment": None if sentiment is None else round(sentiment, 4),
        })
    out.sort(key=lambda x: (x["attention_score_sum"], x["records"]), reverse=True)
    return out[:limit]


def _parse_day(value) -> str | None:
    if not value:
        return None
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except ValueError:
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return text[:10]
        return None


def _robust_z(values: list[float]) -> list[float]:
    if len(values) < 5:
        return [0.0] * len(values)
    med = statistics.median(values)
    deviations = [abs(v - med) for v in values]
    mad = statistics.median(deviations)
    if mad <= 1e-12:
        # Flat baseline with a rare jump: MAD is zero, but the jump is still a real numeric anomaly.
        # Use a conservative deterministic fallback instead of declaring every point non-anomalous.
        scale = max(1.0, abs(med))
        return [0.0 if abs(v - med) <= 1e-12 else (10.0 * (v - med) / scale) for v in values]
    return [0.6745 * (v - med) / mad for v in values]


def _time_series(records: list[dict]) -> dict:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in records:
        day = _parse_day(row.get("date"))
        if day:
            by_day[day].append(row)
    days = sorted(by_day)
    series = []
    for day in days:
        rows = by_day[day]
        rep = _brand_reputation(rows)
        rep_rows = [r for r in rows if (r.get("intelligence") or {}).get("reputation_eligible")]
        total_w = sum(_safe_float((r.get("intelligence") or {}).get("reputation_weight"), 0.0) for r in rep_rows)
        neg_w = sum(_safe_float((r.get("intelligence") or {}).get("reputation_weight"), 0.0) for r in rep_rows if (r.get("ai_analysis") or {}).get("sentiment_label") == "negative")
        opinion_rows = [r for r in rows if (r.get("ai_analysis") or {}).get("opinion_eligible")]
        opinion_w = sum(_safe_float((r.get("intelligence") or {}).get("organic_opinion_weight"), 0.0) for r in opinion_rows)
        emotion_weights = {
            emotion: sum(
                _safe_float((r.get("intelligence") or {}).get("organic_opinion_weight"), 0.0)
                for r in opinion_rows
                if (r.get("ai_analysis") or {}).get("primary_emotion") == emotion
            )
            for emotion in EMOTION_LABELS
        }
        point = {
            "date": day,
            "records": len(rows),
            "effective_voices": round(sum(_safe_float((r.get("intelligence") or {}).get("independent_voice_weight"), 0.0) for r in rows), 4),
            "brand_reputation_index": rep.get("index"),
            "negative_weight_share": round(neg_w / total_w, 6) if total_w > 0 else None,
            "attention_score_sum": round(sum(_safe_float((r.get("intelligence") or {}).get("impact_score"), 0.0) for r in rows), 4),
        }
        for emotion in EMOTION_LABELS:
            point[f"{emotion}_opinion_weight_share"] = round(emotion_weights[emotion] / opinion_w, 6) if opinion_w > 0 else None
        series.append(point)

    volume_z = _robust_z([float(x["records"]) for x in series])
    neg_vals = [float(x["negative_weight_share"] or 0.0) for x in series]
    neg_z = _robust_z(neg_vals)
    emotion_vals = {
        emotion: [float(x.get(f"{emotion}_opinion_weight_share") or 0.0) for x in series]
        for emotion in EMOTION_LABELS
    }
    emotion_z = {emotion: _robust_z(values) for emotion, values in emotion_vals.items()}
    anomalies = []
    for i, row in enumerate(series):
        flags = []
        if volume_z[i] >= 3.5:
            flags.append("volume_spike")
        if neg_z[i] >= 3.5 and neg_vals[i] >= 0.35:
            flags.append("negative_share_spike")
        for emotion in EMOTION_LABELS:
            threshold = 0.25 if emotion == "anger" else 0.20
            if emotion_z[emotion][i] >= 3.5 and emotion_vals[emotion][i] >= threshold:
                flags.append(f"{emotion}_share_spike")
        if flags:
            event = {
                "date": row["date"],
                "flags": flags,
                "volume_robust_z": round(volume_z[i], 3),
                "negative_robust_z": round(neg_z[i], 3),
            }
            for emotion in EMOTION_LABELS:
                event[f"{emotion}_robust_z"] = round(emotion_z[emotion][i], 3)
            anomalies.append(event)
    return {"daily": series, "anomalies": anomalies}


def _clean_excerpt(value, limit: int = 320) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _top_mentions(records: list[dict], limit: int = 15) -> list[dict]:
    ranked = sorted(
        records,
        key=lambda r: (
            _safe_float((r.get("intelligence") or {}).get("impact_score"), 0.0),
            abs(_safe_float((r.get("ai_analysis") or {}).get("sentiment_score"), 0.0)),
            _safe_float((r.get("intelligence") or {}).get("evidence_confidence"), 0.0),
        ),
        reverse=True,
    )
    out = []
    for row in ranked[:limit]:
        ai = row.get("ai_analysis") or {}
        intel = row.get("intelligence") or {}
        out.append({
            "record_id": row.get("id"),
            "platform": row.get("platform"),
            "author": row.get("author"),
            "date": row.get("date"),
            "url": row.get("url"),
            "excerpt": _clean_excerpt(row.get("text") or row.get("caption") or ""),
            "origin_group": intel.get("origin_group"),
            "content_type": row.get("content_type"),
            "sentiment_label": ai.get("sentiment_label"),
            "sentiment_score": ai.get("sentiment_score"),
            "emotion": ai.get("primary_emotion"),
            "topic": ai.get("topic"),
            "narrative": ai.get("narrative"),
            "impact_score": intel.get("impact_score"),
            "impact_confidence": intel.get("impact_confidence"),
            "evidence_confidence": intel.get("evidence_confidence"),
        })
    return out


def _warnings(records: list[dict], confidence: dict, cleaning_report: dict | None) -> list[str]:
    warnings = []
    if not any((r.get("intelligence") or {}).get("reputation_eligible") for r in records):
        warnings.append("No reputation-eligible evidence is available; Brand Reputation is intentionally not calculated.")
    if confidence.get("effective_sample_size", 0) < 30:
        warnings.append("Low effective independent sample size; interpret Reputation and driver rankings cautiously.")
    missing_impact = sum(1 for r in records if _safe_float((r.get("intelligence") or {}).get("impact_confidence"), 0.0) <= 0)
    if records and missing_impact / len(records) >= 0.40:
        warnings.append("Public engagement/reach metrics are missing for a large share of records; impact rankings have lower confidence.")
    if _safe_float((cleaning_report or {}).get("source_coverage_ratio"), 0.0) < 0.75:
        warnings.append("Not all selected sources returned usable data; cross-source coverage is incomplete.")
    if _safe_float((cleaning_report or {}).get("sample_achievement_ratio"), 1.0) < 0.75:
        warnings.append("The trusted sample is materially below the requested target.")
    return warnings


def compute_intelligence(analysis_ready_records: list[dict], plan: dict, cleaning_report: dict | None = None) -> dict:
    if not isinstance(analysis_ready_records, list):
        raise TypeError("analysis_ready_records must be a list")
    original = copy.deepcopy(analysis_ready_records)
    ids = [str(r.get("id") or "") for r in analysis_ready_records]
    if any(not rid for rid in ids):
        raise RuntimeError("Every Step 4 record must have a stable id before intelligence aggregation.")
    if len(ids) != len(set(ids)):
        raise RuntimeError("Analysis-ready sample contains duplicate record ids; Step 5 stopped before aggregation.")

    refs = _metric_reference(analysis_ready_records)
    enriched = []
    for row in analysis_ready_records:
        impact = _impact_components(row, refs)
        weights = _record_weights(row, impact)
        intel = {
            "ruleset_version": INTELLIGENCE_RULESET_VERSION,
            "origin_group": _origin_group(row),
            **impact,
            **weights,
        }
        out = copy.deepcopy(row)
        out["intelligence"] = intel
        enriched.append(out)

    if analysis_ready_records != original:
        raise RuntimeError("Step 5 mutated Step 4 evidence, which is forbidden.")

    brand_rep = _brand_reputation(enriched)
    confidence = _confidence_report(enriched, cleaning_report, brand_rep)
    sentiment_all = _distribution(enriched, "sentiment_label", SENTIMENT_LABELS)
    sentiment = _distribution(enriched, "sentiment_label", SENTIMENT_LABELS, "reputation_weight", only_reputation=True)
    emotion = _distribution(enriched, "primary_emotion", EMOTION_LABELS, "organic_opinion_weight", only_opinion=True)
    stance = _distribution(enriched, "target_stance", STANCE_LABELS, "reputation_weight", only_reputation=True)
    time = _time_series(enriched)

    summary = {
        "ruleset_version": INTELLIGENCE_RULESET_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "generated_at": _utcnow(),
        "input_hash": _input_hash(analysis_ready_records, cleaning_report),
        "research_context": {
            "client": plan.get("client"),
            "topic": plan.get("topic"),
            "market": plan.get("market"),
            "date_from": plan.get("date_from"),
            "date_to": plan.get("date_to"),
        },
        "records": len(enriched),
        "reputation_eligible_records": int(brand_rep.get("eligible_records") or 0),
        "organic_opinion_records": sum(1 for r in enriched if (r.get("ai_analysis") or {}).get("opinion_eligible")),
        "effective_independent_voices": round(sum(_safe_float((r.get("intelligence") or {}).get("independent_voice_weight"), 0.0) for r in enriched), 3),
        "brand_reputation": brand_rep,
        "confidence": confidence,
        "sentiment": sentiment,
        "sentiment_all_analysis_ready": sentiment_all,
        "emotions_organic_people": emotion,
        "stance": stance,
        "origin_breakdown": _origin_breakdown(enriched),
        "source_breakdown": _source_breakdown(enriched),
        "top_positive_narrative_drivers": [x for x in _group_contributions(enriched, "narrative", 40) if x["reputation_point_contribution"] > 0][:10],
        "top_negative_narrative_drivers": [x for x in _group_contributions(enriched, "narrative", 40) if x["reputation_point_contribution"] < 0][:10],
        "topic_drivers": _group_contributions(enriched, "topic", 12),
        "media_influence": _author_rankings(enriched, "media", 10),
        "people_influence": _author_rankings(enriched, "person", 10),
        "daily_points": len(time["daily"]),
        "anomaly_count": len(time["anomalies"]),
        "warnings": _warnings(enriched, confidence, cleaning_report),
        "boundary": "Step 5 computes deterministic descriptive/weighted analytics. It does not claim causality; causal investigation belongs to Step 6.",
    }

    methodology = {
        "ruleset_version": INTELLIGENCE_RULESET_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "brand_reputation": {
            "range": "0–100",
            "neutral_point": 50,
            "formula": "50 * (1 + weighted_mean(sentiment_score))",
            "weighting": "independent-voice × authenticity × evidence-confidence × bounded impact multiplier",
            "important_note": "Impact amplifies the direction of sentiment; reach does not create positive or negative reputation by itself.",
        },
        "impact": {
            "range": "0–1",
            "signals": "platform-normalized views, engagement and followers",
            "normalization": "log1p against run/platform p95 with safe fallback; available signals are reweighted",
            "missing_metrics": "unknown impact defaults to neutral 0.5 with zero impact confidence, never to zero impact",
        },
        "duplicates_and_syndication": "independent_voice_weight from Step 3 reduces duplicated/story-cluster opinion weight while preserving raw dissemination volume.",
        "owned_and_promotional": "owned/promotional content is retained for visibility reporting but excluded from Brand Reputation weighting.",
        "factual_news": "content with target_stance=not_applicable is retained for dissemination but excluded from Brand Reputation weighting.",
        "drivers": "Narrative/topic contributions are exact additive Reputation-index point contributions; their signed sum equals Brand Reputation minus 50 (subject to displayed rounding).",
        "anomalies": "robust median/MAD thresholding flags numeric spikes only; no cause is inferred in Step 5.",
        "confidence": "separate evidence-strength score using effective sample size, Step 3 data quality, source coverage and semantic confidence; not statistical representativeness.",
    }

    audit = [{
        "record_id": r.get("id"),
        "platform": r.get("platform"),
        "origin_group": (r.get("intelligence") or {}).get("origin_group"),
        "impact_score": (r.get("intelligence") or {}).get("impact_score"),
        "impact_confidence": (r.get("intelligence") or {}).get("impact_confidence"),
        "reputation_eligible": (r.get("intelligence") or {}).get("reputation_eligible"),
        "reputation_exclusion_reason": (r.get("intelligence") or {}).get("reputation_exclusion_reason"),
        "reputation_weight": (r.get("intelligence") or {}).get("reputation_weight"),
        "sentiment_score": (r.get("ai_analysis") or {}).get("sentiment_score"),
        "narrative": (r.get("ai_analysis") or {}).get("narrative"),
    } for r in enriched]

    return {
        "records": enriched,
        "summary": summary,
        "time_series": time,
        "top_mentions": _top_mentions(enriched),
        "audit": audit,
        "methodology": methodology,
    }


def persist_intelligence(folder: Path, result: dict) -> dict:
    store = RunStore()
    base = folder / "intelligence"
    store.write(base / "records.json", result["records"])
    store.write(base / "summary.json", result["summary"])
    store.write(base / "time-series.json", result["time_series"])
    store.write(base / "top-mentions.json", result["top_mentions"])
    store.write(base / "audit.json", result["audit"])
    store.write(base / "methodology.json", result["methodology"])
    return result["summary"]


def build_intelligence(folder: Path, plan: dict, force: bool = False) -> dict:
    store = RunStore()
    ready_path = folder / "analysis" / "analysis-ready.json"
    if not ready_path.exists():
        raise RuntimeError("Step 5 requires Step 4 analysis-ready evidence.")
    records = store.read(ready_path, []) or []
    cleaning_report = store.read(folder / "cleaning" / "report.json", {}) or {}
    current_hash = _input_hash(records, cleaning_report)
    existing = store.read(folder / "intelligence" / "summary.json")
    if isinstance(existing, dict) and not force and existing.get("input_hash") == current_hash and existing.get("ruleset_version") == INTELLIGENCE_RULESET_VERSION:
        return existing
    result = compute_intelligence(records, plan=plan, cleaning_report=cleaning_report)
    persist_intelligence(folder, result)
    return result["summary"]


def load_intelligence_summary(folder: Path) -> dict | None:
    store = RunStore()
    report = store.read(folder / "intelligence" / "summary.json")
    if not isinstance(report, dict):
        return None
    ready = store.read(folder / "analysis" / "analysis-ready.json", []) or []
    cleaning_report = store.read(folder / "cleaning" / "report.json", {}) or {}
    current_hash = _input_hash(ready, cleaning_report)
    out = copy.deepcopy(report)
    out["stale"] = bool(out.get("input_hash") != current_hash or out.get("ruleset_version") != INTELLIGENCE_RULESET_VERSION)
    return out
