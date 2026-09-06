from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.services.investigations import INDICATOR_REGISTRY, load_evidence_pack, load_investigation_summary
from app.services.storage import RunStore

VISUALIZATION_RULESET_VERSION = "1.2.0"
VISUALIZATION_METHODOLOGY_VERSION = "signalyth-visual-intelligence-v1"
VISUALIZATION_CONTRACT_VERSION = "signalyth-visual-pack-v1.2"


class VisualizationCancelled(RuntimeError):
    pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value, default: float = 0.0) -> float:
    try:
        n = float(value)
        if math.isfinite(n):
            return n
    except (TypeError, ValueError):
        pass
    return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return default


def _pct(value) -> float | None:
    if value is None:
        return None
    return round(_safe_float(value) * 100.0, 2)


def _lang(en: str, el: str) -> dict:
    return {"en": en, "el": el}


def _json_safe(value):
    """Return a JSON-safe deep copy; non-finite numbers are never persisted/rendered."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return copy.deepcopy(value)


def _input_hash(pack: dict) -> str:
    blob = json.dumps(pack, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _inventory_map(pack: dict) -> dict[str, dict]:
    rows = pack.get("indicator_inventory") or []
    return {str(row.get("indicator_id")): row for row in rows if row.get("indicator_id")}


def _chart(
    chart_id: str,
    chart_type: str,
    title_en: str,
    title_el: str,
    indicator_ids: list[str],
    data: dict,
    *,
    subtitle_en: str = "",
    subtitle_el: str = "",
    unit: str | None = None,
    priority: int = 50,
    dashboard_section: str = "overview",
    dashboard_span: int = 1,
    presentation_role: str = "supporting",
    evidence_refs: list[str] | None = None,
    warnings: list[str] | None = None,
    notes: dict | None = None,
) -> dict:
    # Chart specs are data-first so Step 8 can create native editable PowerPoint charts.
    return {
        "chart_id": chart_id,
        "chart_type": chart_type,
        "title": _lang(title_en, title_el),
        "subtitle": _lang(subtitle_en, subtitle_el),
        "indicator_ids": list(indicator_ids),
        "data": _json_safe(data),
        "unit": unit,
        "priority": max(0, min(100, int(priority))),
        "dashboard": {"section": dashboard_section, "span": 2 if dashboard_span >= 2 else 1},
        "presentation": {
            "candidate": bool(data),
            "native_editable_ready": True,
            "preferred_role": presentation_role,
            "render_as_raster": False,
        },
        "evidence_refs": list(dict.fromkeys(evidence_refs or [])),
        "warnings": list(dict.fromkeys(warnings or [])),
        "notes": copy.deepcopy(notes or {}),
    }


def _distribution_chart(indicator_id: str, value: dict, labels: list[str], title: tuple[str, str], chart_id: str, *, priority=70) -> dict | None:
    if not isinstance(value, dict) or _safe_int(value.get("records"), 0) <= 0:
        return None
    weighted = value.get("weighted_percent") or value.get("percent") or {}
    data = [{"label": label, "value": round(_safe_float(weighted.get(label)), 2)} for label in labels]
    return _chart(
        chart_id, "distribution_bar", title[0], title[1], [indicator_id],
        {"categories": data, "total_records": _safe_int(value.get("records")), "basis": "weighted_percent" if value.get("weighted_percent") else "percent"},
        unit="percent", priority=priority, dashboard_section="reputation", dashboard_span=1,
        presentation_role="core_metric",
    )


def _driver_rows(pos: list[dict], neg: list[dict]) -> list[dict]:
    rows = []
    seen = set()
    for item in list(pos or []) + list(neg or []):
        name = str(item.get("name") or "Unclassified").strip() or "Unclassified"
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "name": name,
            "contribution": round(_safe_float(item.get("reputation_point_contribution")), 4),
            "mentions": _safe_int(item.get("mention_count")),
            "average_sentiment": round(_safe_float(item.get("average_sentiment")), 4),
            "average_impact": round(_safe_float(item.get("average_impact")), 4),
        })
    rows.sort(key=lambda x: abs(x["contribution"]), reverse=True)
    return rows[:14]


def _ranking_rows(items: list[dict], name_key: str) -> list[dict]:
    out = []
    for item in items or []:
        name = str(item.get(name_key) or "Unknown").strip() or "Unknown"
        out.append({
            "name": name,
            "records": _safe_int(item.get("records")),
            "attention": round(_safe_float(item.get("attention_score_sum")), 4),
            "weighted_sentiment": None if item.get("weighted_sentiment") is None else round(_safe_float(item.get("weighted_sentiment")), 4),
        })
    return out[:10]


def _story_rows(items: list[dict]) -> list[dict]:
    out=[]
    for item in items or []:
        out.append({
            "cluster_id": item.get("cluster_id") or item.get("story_cluster_id"),
            "records": _safe_int(item.get("records") or item.get("record_count") or item.get("size")),
            "sources": item.get("sources") or [],
            "representative_excerpt": item.get("representative_excerpt") or item.get("excerpt"),
        })
    return out[:10]


def _coordination_rows(items: list[dict]) -> list[dict]:
    out=[]
    for item in items or []:
        out.append({
            "cluster_id": item.get("cluster_id") or item.get("coordination_cluster_id"),
            "records": _safe_int(item.get("records") or item.get("record_count") or item.get("size")),
            "accounts": _safe_int(item.get("accounts") or item.get("account_count")),
            "risk": item.get("risk") or item.get("label"),
        })
    return out[:10]


def build_visualization_state(evidence_pack: dict, *, cancel_check: Callable[[], bool] | None = None) -> dict:
    if not isinstance(evidence_pack, dict):
        raise TypeError("evidence_pack must be a dict")
    guardrails = evidence_pack.get("presentation_guardrails") or {}
    if guardrails.get("must_review_all_indicators") is not True:
        raise RuntimeError("Step 7 requires the complete Step 6 Evidence Pack review contract.")

    inv_map = _inventory_map(evidence_pack)
    expected_ids = [x[0] for x in INDICATOR_REGISTRY]
    if list(inv_map) != expected_ids or len(inv_map) != len(expected_ids):
        raise RuntimeError("Step 7 stopped because the Evidence Pack does not contain every registered indicator exactly once.")

    if cancel_check and cancel_check():
        raise VisualizationCancelled("Visualization build cancelled before chart planning")

    charts: list[dict] = []
    indicator_to_charts: dict[str, list[str]] = {k: [] for k in expected_ids}

    def add(spec: dict | None):
        if not spec:
            return
        if not isinstance(spec.get("data"), dict):
            raise RuntimeError("Every chart must contain a structured data payload.")
        charts.append(spec)
        for indicator_id in spec.get("indicator_ids") or []:
            if indicator_id in indicator_to_charts:
                indicator_to_charts[indicator_id].append(spec["chart_id"])

    val = lambda k: copy.deepcopy((inv_map[k].get("value") if k in inv_map else None))
    avail = lambda k: bool((inv_map.get(k) or {}).get("available"))

    sample = val("sample_volume") or {}
    if avail("sample_volume"):
        add(_chart(
        "sample_overview", "kpi_group", "Evidence base", "Βάση δεδομένων",
        ["sample_volume", "effective_independent_voices"],
        {"items": [
            {"key": "analysis_ready", "label": _lang("Analysis-ready", "Έτοιμα για ανάλυση"), "value": _safe_int(sample.get("analysis_ready_records"))},
            {"key": "trusted", "label": _lang("Trusted", "Αξιόπιστα"), "value": _safe_int(sample.get("trusted_records"))},
            {"key": "reputation_eligible", "label": _lang("Reputation-eligible", "Κατάλληλα για Reputation"), "value": _safe_int(sample.get("reputation_eligible_records"))},
            {"key": "effective_voices", "label": _lang("Effective voices", "Αποτελεσματικές φωνές"), "value": round(_safe_float(sample.get("effective_independent_voices")), 2)},
        ]}, priority=88, dashboard_section="overview", dashboard_span=2, presentation_role="methodology_context",
    ))

    market = val("market_relevance") or {}
    if avail("market_relevance"):
        add(_chart(
        "market_relevance", "quality_kpi", "Target-market relevance", "Συνάφεια με την αγορά-στόχο", ["market_relevance"],
        {"market": market.get("market"), "high_relevance_share_percent": _pct(market.get("high_relevance_share")), "average_market_score_percent": _pct(market.get("average_market_score")), "records_scored": _safe_int(market.get("records_with_market_score"))},
        unit="percent", priority=82, dashboard_section="quality", presentation_role="quality_guardrail",
        ))

    rep = val("brand_reputation") or {}
    if rep.get("index") is not None:
        add(_chart(
            "brand_reputation", "gauge_kpi", "Brand Reputation", "Brand Reputation", ["brand_reputation"],
            {"value": round(_safe_float(rep.get("index")), 2), "min": 0, "max": 100, "neutral_low": 45, "neutral_high": 55, "interpretation": rep.get("interpretation")},
            unit="index", priority=100, dashboard_section="overview", presentation_role="headline",
        ))

    conf = val("evidence_confidence") or {}
    if conf.get("score") is not None:
        add(_chart(
            "evidence_confidence", "gauge_kpi", "Evidence confidence", "Βεβαιότητα evidence", ["evidence_confidence"],
            {"value": round(_safe_float(conf.get("score")), 2), "min": 0, "max": 100, "label": conf.get("label"), "effective_sample_size": conf.get("effective_sample_size")},
            unit="score", priority=92, dashboard_section="overview", presentation_role="quality_guardrail",
            warnings=[str(conf.get("note"))] if conf.get("note") else [],
        ))

    add(_distribution_chart("sentiment", val("sentiment") or {}, ["positive", "neutral", "mixed", "negative"], ("Weighted sentiment", "Σταθμισμένο sentiment"), "sentiment_distribution", priority=96))
    add(_distribution_chart("emotions", val("emotions") or {}, ["joy", "anger", "sadness", "fear", "disgust", "surprise", "neutral"], ("Emotions · organic people", "Συναισθήματα · organic άτομα"), "emotion_distribution", priority=86))
    add(_distribution_chart("stance", val("stance") or {}, ["supportive", "neutral", "mixed", "critical", "not_applicable"], ("Target stance", "Στάση απέναντι στο target"), "stance_distribution", priority=72))

    impact = val("impact_attention") or {}
    if avail("impact_attention"):
        add(_chart(
        "impact_coverage", "quality_kpi", "Public metrics coverage", "Κάλυψη δημόσιων metrics", ["impact_attention"],
        {"known_metric_share_percent": round(100.0 * _safe_float(impact.get("known_metric_share")), 2), "known_records": _safe_int(impact.get("records_with_known_public_metrics")), "records": _safe_int(impact.get("records"))},
        unit="percent", priority=58, dashboard_section="quality", presentation_role="methodology_context",
        ))

    origin = val("origin_breakdown") or {}
    if isinstance(origin, dict) and origin:
        origin_rows=[]
        for name, row in origin.items():
            origin_rows.append({"name": name, "records": _safe_int((row or {}).get("records")), "record_share": round(_safe_float((row or {}).get("record_share_percent")), 2), "attention_share": round(_safe_float((row or {}).get("attention_share_percent")), 2), "brand_reputation": (row or {}).get("brand_reputation_index")})
        add(_chart("origin_breakdown", "grouped_bar", "Media, people & owned activity", "Media, άτομα & owned activity", ["origin_breakdown"], {"rows": origin_rows}, unit="percent", priority=78, dashboard_section="sources", dashboard_span=2, presentation_role="context"))

    source = val("source_breakdown") or {}
    if isinstance(source, dict) and source:
        rows=[]
        for name, row in source.items():
            rows.append({"source": name, "records": _safe_int((row or {}).get("records")), "reputation_eligible": _safe_int((row or {}).get("reputation_eligible")), "brand_reputation": (row or {}).get("brand_reputation_index"), "average_impact": round(_safe_float((row or {}).get("average_impact")), 4), "organic_opinions": _safe_int((row or {}).get("organic_opinion_records"))})
        rows.sort(key=lambda x: x["records"], reverse=True)
        add(_chart("source_comparison", "source_matrix", "Source comparison", "Σύγκριση πηγών", ["source_breakdown"], {"rows": rows}, priority=90, dashboard_section="sources", dashboard_span=2, presentation_role="core_context"))

    pos, neg = val("positive_narrative_drivers") or [], val("negative_narrative_drivers") or []
    driver_rows = _driver_rows(pos, neg)
    if driver_rows:
        add(_chart("narrative_drivers", "diverging_bar", "What moves Brand Reputation", "Τι μετακινεί το Brand Reputation", ["positive_narrative_drivers", "negative_narrative_drivers"], {"rows": driver_rows, "zero": 0}, unit="reputation_points", priority=100, dashboard_section="drivers", dashboard_span=2, presentation_role="headline_driver"))

    topics = val("topic_drivers") or []
    if topics:
        rows=[{"name": x.get("name"), "contribution": round(_safe_float(x.get("reputation_point_contribution")),4), "mentions":_safe_int(x.get("mention_count")), "average_sentiment":round(_safe_float(x.get("average_sentiment")),4)} for x in topics[:12]]
        add(_chart("topic_drivers", "diverging_bar", "Topic contribution", "Συμβολή θεμάτων", ["topic_drivers"], {"rows": rows}, unit="reputation_points", priority=76, dashboard_section="drivers", dashboard_span=2, presentation_role="supporting_driver"))

    media = _ranking_rows(val("media_influence") or [], "author")
    if media:
        add(_chart("media_influence", "ranking", "Media influence", "Επιρροή media", ["media_influence"], {"rows": media}, priority=74, dashboard_section="influence", presentation_role="ranking"))
    people = _ranking_rows(val("people_influence") or [], "author")
    if people:
        add(_chart("people_influence", "ranking", "People / creator influence", "Επιρροή ατόμων / creators", ["people_influence"], {"rows": people}, priority=74, dashboard_section="influence", presentation_role="ranking"))

    trends = val("time_trends") or {}
    daily = copy.deepcopy(trends.get("daily") or [])
    if len(daily) >= 2:
        # Keep raw numeric series; the UI/PPT renderer decides the visual scale without changing the evidence.
        add(_chart("time_trends", "time_series", "How the conversation evolved", "Πώς εξελίχθηκε η συζήτηση", ["time_trends", "brand_reputation"], {"rows": daily, "series": ["brand_reputation_index", "records", "negative_weight_share", "anger_opinion_weight_share", "attention_score_sum"]}, priority=98, dashboard_section="trends", dashboard_span=2, presentation_role="headline_trend"))
        emotions_value = val("emotions") or {}
        emotion_percent = emotions_value.get("weighted_percent") or {}
        ranked_emotions = sorted(
            [(str(k), _safe_float(v)) for k, v in emotion_percent.items() if str(k) != "neutral" and _safe_float(v) > 0],
            key=lambda x: x[1],
            reverse=True,
        )[:3]
        emotion_series = [f"{name}_opinion_weight_share" for name, _ in ranked_emotions if any(r.get(f"{name}_opinion_weight_share") is not None for r in daily)]
        if emotion_series:
            add(_chart(
                "emotion_trends", "time_series", "Emotion evolution", "Εξέλιξη συναισθημάτων", ["time_trends", "emotions"],
                {"rows": daily, "series": emotion_series, "scale": "share_percent", "series_labels": {f"{name}_opinion_weight_share": name.title() for name, _ in ranked_emotions}},
                unit="percent", priority=86, dashboard_section="trends", dashboard_span=2, presentation_role="emotion_drilldown",
            ))

    anomalies = val("numeric_anomalies") or []
    if anomalies:
        add(_chart("anomalies", "event_timeline", "Detected anomalies", "Εντοπισμένες ανωμαλίες", ["numeric_anomalies"], {"events": copy.deepcopy(anomalies)}, priority=88, dashboard_section="trends", dashboard_span=2, presentation_role="investigation_trigger"))

    quality = val("data_quality") or {}
    coverage = val("source_coverage") or {}
    achievement = val("sample_achievement") or {}
    if any(avail(k) for k in ("data_quality", "source_coverage", "sample_achievement")):
        add(_chart("quality_matrix", "quality_matrix", "Data quality & coverage", "Ποιότητα δεδομένων & κάλυψη", ["data_quality", "source_coverage", "sample_achievement"], {"items": [
        {"key": "data_quality", "value": quality.get("score"), "label": quality.get("label")},
        {"key": "source_coverage", "value": None if coverage.get("ratio") is None else round(_safe_float(coverage.get("ratio"))*100,2), "source_count": _safe_int(coverage.get("source_count"))},
        {"key": "sample_achievement", "value": None if achievement.get("ratio") is None else round(_safe_float(achievement.get("ratio"))*100,2), "trusted_records": achievement.get("trusted_records")},
    ]}, unit="score_or_percent", priority=94, dashboard_section="quality", dashboard_span=2, presentation_role="quality_guardrail"))

    auth = val("authenticity_risk") or {}
    coord = _coordination_rows(val("coordination") or [])
    if avail("authenticity_risk") or avail("coordination"):
        add(_chart("authenticity_coordination", "risk_panel", "Authenticity & coordination", "Αυθεντικότητα & συντονισμός", ["authenticity_risk", "coordination"], {"low_authenticity_records": _safe_int(auth.get("low_authenticity_records")), "low_authenticity_share_percent": round(100*_safe_float(auth.get("low_authenticity_share")),2), "coordination_clusters": coord}, priority=82, dashboard_section="quality", dashboard_span=2, presentation_role="quality_guardrail"))

    stories = _story_rows(val("story_syndication") or [])
    if stories:
        add(_chart("story_syndication", "cluster_table", "Story replication", "Αναπαραγωγή ιστοριών", ["story_syndication"], {"rows": stories}, priority=64, dashboard_section="quality", dashboard_span=2, presentation_role="context"))

    mentions = val("top_mentions") or []
    if mentions:
        evidence_refs=[str(x.get("record_id")) for x in mentions if x.get("record_id")]
        add(_chart("top_mentions", "evidence_table", "Top evidence mentions", "Κορυφαία evidence mentions", ["top_mentions"], {"rows": copy.deepcopy(mentions[:15])}, priority=90, dashboard_section="evidence", dashboard_span=2, presentation_role="evidence", evidence_refs=evidence_refs))

    if cancel_check and cancel_check():
        raise VisualizationCancelled("Visualization build cancelled after chart planning")

    chart_ids = [c["chart_id"] for c in charts]
    if len(chart_ids) != len(set(chart_ids)):
        raise RuntimeError("Visualization chart IDs must be unique.")

    visual_review = []
    for indicator_id, label, role in INDICATOR_REGISTRY:
        inventory_row = inv_map[indicator_id]
        linked = indicator_to_charts[indicator_id]
        available = bool(inventory_row.get("available"))
        status = "visualized" if linked else ("insufficient_data" if not available else "reviewed_no_separate_visual")
        visual_review.append({
            "indicator_id": indicator_id,
            "label": label,
            "role": role,
            "examined": True,
            "available": available,
            "visual_status": status,
            "chart_ids": linked,
            "omission_reason": None if linked else ("insufficient_or_not_triggered" if not available else "covered contextually; no dedicated chart required"),
        })

    if [x["indicator_id"] for x in visual_review] != expected_ids:
        raise RuntimeError("Step 7 visual review contract failed; an indicator was omitted.")

    investigations = copy.deepcopy(evidence_pack.get("investigations") or [])
    presentation_investigations = [x for x in investigations if (x.get("presentation") or {}).get("candidate")]
    presentation_investigations.sort(key=lambda x: (_safe_int((x.get("presentation") or {}).get("priority_score")), _safe_float((x.get("confidence") or {}).get("score"))), reverse=True)

    warnings=[]
    for text in ((evidence_pack.get("headline_metrics") or {}).get("step5_warnings") or []):
        if text and str(text) not in warnings:
            warnings.append(str(text))
    if guardrails.get("must_not_present_association_as_causation"):
        warnings.append("Association ≠ proven causality.")

    dashboard = {
        "contract_version": VISUALIZATION_CONTRACT_VERSION,
        "research_context": copy.deepcopy(evidence_pack.get("research_context") or {}),
        "sections": [
            {"section_id": "overview", "title": _lang("Executive view", "Συνολική εικόνα"), "chart_ids": [c["chart_id"] for c in charts if c["dashboard"]["section"] == "overview"]},
            {"section_id": "reputation", "title": _lang("Reputation & perception", "Reputation & αντίληψη"), "chart_ids": [c["chart_id"] for c in charts if c["dashboard"]["section"] == "reputation"]},
            {"section_id": "drivers", "title": _lang("Drivers", "Drivers"), "chart_ids": [c["chart_id"] for c in charts if c["dashboard"]["section"] == "drivers"]},
            {"section_id": "sources", "title": _lang("Sources & audiences", "Πηγές & κοινά"), "chart_ids": [c["chart_id"] for c in charts if c["dashboard"]["section"] == "sources"]},
            {"section_id": "influence", "title": _lang("Influence", "Επιρροή"), "chart_ids": [c["chart_id"] for c in charts if c["dashboard"]["section"] == "influence"]},
            {"section_id": "trends", "title": _lang("Evolution & anomalies", "Εξέλιξη & ανωμαλίες"), "chart_ids": [c["chart_id"] for c in charts if c["dashboard"]["section"] == "trends"]},
            {"section_id": "quality", "title": _lang("Data integrity", "Ακεραιότητα δεδομένων"), "chart_ids": [c["chart_id"] for c in charts if c["dashboard"]["section"] == "quality"]},
            {"section_id": "evidence", "title": _lang("Evidence", "Evidence"), "chart_ids": [c["chart_id"] for c in charts if c["dashboard"]["section"] == "evidence"]},
        ],
        "investigations": _json_safe(presentation_investigations[:10]),
        "warnings": warnings,
        "causal_guardrail": "not_proven",
    }
    dashboard["sections"] = [x for x in dashboard["sections"] if x["chart_ids"]]

    presentation_chart_ids = [c["chart_id"] for c in sorted(charts, key=lambda x: x["priority"], reverse=True) if c["presentation"]["candidate"]]
    visual_pack = {
        "contract_version": VISUALIZATION_CONTRACT_VERSION,
        "research_context": copy.deepcopy(evidence_pack.get("research_context") or {}),
        "indicator_review": visual_review,
        "chart_specs": copy.deepcopy(charts),
        "presentation_chart_order": presentation_chart_ids,
        "investigation_candidates": _json_safe(presentation_investigations),
        "warnings": warnings,
        "guardrails": {
            "must_review_all_indicators": True,
            "native_editable_charts_required": True,
            "chart_data_must_come_from_evidence_pack": True,
            "must_preserve_quality_warnings": True,
            "must_not_present_association_as_causation": True,
            "may_omit_uninteresting_visual": True,
            "omission_requires_review_record": True,
        },
    }

    summary = {
        "ruleset_version": VISUALIZATION_RULESET_VERSION,
        "methodology_version": VISUALIZATION_METHODOLOGY_VERSION,
        "visual_contract_version": VISUALIZATION_CONTRACT_VERSION,
        "generated_at": _utcnow(),
        "input_hash": _input_hash(evidence_pack),
        "research_context": copy.deepcopy(evidence_pack.get("research_context") or {}),
        "chart_count": len(charts),
        "presentation_chart_count": len(presentation_chart_ids),
        "dashboard_section_count": len(dashboard["sections"]),
        "investigation_card_count": len(presentation_investigations[:10]),
        "indicator_contract": {
            "required": len(expected_ids),
            "examined": len(visual_review),
            "complete": len(visual_review) == len(expected_ids) and all(x.get("examined") for x in visual_review),
            "visualized": sum(1 for x in visual_review if x["visual_status"] == "visualized"),
            "insufficient_data": sum(1 for x in visual_review if x["visual_status"] == "insufficient_data"),
            "reviewed_no_separate_visual": sum(1 for x in visual_review if x["visual_status"] == "reviewed_no_separate_visual"),
        },
        "native_editable_ready": all((c.get("presentation") or {}).get("native_editable_ready") for c in charts),
        "warnings": warnings,
        "boundary": "Step 7 plans and renders evidence-linked visualizations. It does not invent metrics or select the final PowerPoint story; final slide planning belongs to Step 8.",
    }
    if not summary["indicator_contract"]["complete"]:
        raise RuntimeError("Step 7 stopped because the visual indicator review is incomplete.")

    audit = [{
        "chart_id": c["chart_id"],
        "chart_type": c["chart_type"],
        "indicator_ids": c["indicator_ids"],
        "priority": c["priority"],
        "presentation_candidate": c["presentation"]["candidate"],
        "native_editable_ready": c["presentation"]["native_editable_ready"],
        "evidence_ref_count": len(c.get("evidence_refs") or []),
    } for c in charts]

    methodology = {
        "ruleset_version": VISUALIZATION_RULESET_VERSION,
        "methodology_version": VISUALIZATION_METHODOLOGY_VERSION,
        "visual_contract_version": VISUALIZATION_CONTRACT_VERSION,
        "principles": [
            "No visualization may fabricate a value absent from the Step 6 Evidence Pack.",
            "Every registered indicator is reviewed even when no dedicated chart is justified.",
            "Charts persist structured source data so Step 8 can generate native editable PowerPoint charts instead of screenshots.",
            "Quality, coverage and authenticity warnings remain visually available alongside headline metrics.",
            "Association is not rendered as proven causality.",
            "Sparse evidence produces an explicit insufficient-data state rather than a decorative empty chart.",
        ],
        "design_direction": "Cycladic minimal intelligence: quiet hierarchy, restrained surfaces, data-first typography, no decorative chartjunk.",
    }

    return {
        "summary": summary,
        "chart_specs": charts,
        "indicator_review": visual_review,
        "dashboard": dashboard,
        "presentation_visual_pack": visual_pack,
        "audit": audit,
        "methodology": methodology,
    }


def persist_visualizations(folder: Path, result: dict) -> dict:
    store = RunStore()
    base = folder / "visualizations"
    store.write(base / "summary.json", result["summary"])
    store.write(base / "chart-specs.json", result["chart_specs"])
    store.write(base / "indicator-review.json", result["indicator_review"])
    store.write(base / "dashboard.json", result["dashboard"])
    store.write(base / "presentation-visual-pack.json", result["presentation_visual_pack"])
    store.write(base / "audit.json", result["audit"])
    store.write(base / "methodology.json", result["methodology"])
    return result["summary"]


def build_visualizations(folder: Path, plan: dict | None = None, force: bool = False, cancel_check: Callable[[], bool] | None = None) -> dict:
    inv_summary = load_investigation_summary(folder)
    if inv_summary is None:
        raise RuntimeError("Step 7 requires Step 6 Automatic Investigations and Evidence Pack.")
    if inv_summary.get("stale"):
        raise RuntimeError("Step 6 Evidence Pack is stale. Rebuild Automatic Investigations before Step 7.")
    pack = load_evidence_pack(folder)
    if not isinstance(pack, dict):
        raise RuntimeError("Step 7 requires the complete Step 6 Evidence Pack.")

    expected_context = pack.get("research_context") or {}
    if plan:
        for key in ("client", "topic", "market", "date_from", "date_to"):
            requested = plan.get(key)
            persisted = expected_context.get(key)
            if persisted not in (None, "") and requested not in (None, "") and str(requested) != str(persisted):
                raise RuntimeError(f"Step 7 research context mismatch for {key}; rebuild upstream evidence before visualizing a changed plan.")

    current_hash = _input_hash(pack)
    existing = RunStore.read(folder / "visualizations" / "summary.json")
    if isinstance(existing, dict) and not force and existing.get("input_hash") == current_hash and existing.get("ruleset_version") == VISUALIZATION_RULESET_VERSION:
        return existing

    result = build_visualization_state(pack, cancel_check=cancel_check)
    persist_visualizations(folder, result)
    return result["summary"]


def load_visualization_summary(folder: Path) -> dict | None:
    store = RunStore()
    summary = store.read(folder / "visualizations" / "summary.json")
    if not isinstance(summary, dict):
        return None
    inv_summary = load_investigation_summary(folder)
    pack = load_evidence_pack(folder)
    stale = inv_summary is None or bool(inv_summary.get("stale")) or not isinstance(pack, dict) or summary.get("input_hash") != _input_hash(pack) or summary.get("ruleset_version") != VISUALIZATION_RULESET_VERSION
    return {**summary, "stale": stale}


def load_dashboard(folder: Path) -> dict | None:
    summary = load_visualization_summary(folder)
    if summary is None:
        return None
    dashboard = RunStore.read(folder / "visualizations" / "dashboard.json")
    if not isinstance(dashboard, dict):
        return None
    return {**dashboard, "stale": bool(summary.get("stale"))}


def load_chart_specs(folder: Path) -> list[dict] | None:
    summary = load_visualization_summary(folder)
    if summary is None:
        return None
    charts = RunStore.read(folder / "visualizations" / "chart-specs.json")
    return charts if isinstance(charts, list) else None


def load_presentation_visual_pack(folder: Path) -> dict | None:
    summary = load_visualization_summary(folder)
    if summary is None:
        return None
    pack = RunStore.read(folder / "visualizations" / "presentation-visual-pack.json")
    if not isinstance(pack, dict):
        return None
    return {**pack, "stale": bool(summary.get("stale"))}
