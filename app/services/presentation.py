from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.shared import Inches as DocxInches, Pt as DocxPt, RGBColor as DocxRGBColor
from pptx import Presentation
from pptx.chart.data import ChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.dml import MSO_LINE
from pptx.util import Inches, Pt

from app.services.investigations import INDICATOR_REGISTRY, load_evidence_pack
from app.services.storage import RunStore
from app.services.visualizations import load_presentation_visual_pack, load_visualization_summary
from app.services.report_synthesis import build_report_synthesis, final_consistency_qa

PRESENTATION_RULESET_VERSION = "1.4.0"
PRESENTATION_METHODOLOGY_VERSION = "signalyth-presentation-intelligence-v1.4"
PRESENTATION_CONTRACT_VERSION = "signalyth-presentation-pack-v1.4"

BG = "FFFFFF"
INK = "1A1A1A"
MUTED = "5F6368"
STONE = "F1F1F1"
STONE_DARK = "DADADA"
WHITE = "FFFFFF"
AEGEAN = "155E75"
POS = "1E7F4F"
NEG = "B23B32"
NEUTRAL = "9AA0A6"
MIXED = "C98A2B"
WARN = "B26A00"
FONT = "Inter"

# Stable semantic colors: the same sentiment/emotion always gets the same color
# in dashboard, PPTX, PDF and Word, as the methodology requires.
SENTIMENT_COLORS = {"positive": POS, "negative": NEG, "neutral": NEUTRAL, "mixed": MIXED}
EMOTION_COLORS = {
    "joy": "E2A63D", "anger": "B23B32", "sadness": "3E6B9E", "fear": "6B4FA1",
    "disgust": "7A7F3A", "surprise": "2E8C8C", "neutral": "9AA0A6",
}
CATEGORY_LABELS_EL = {
    "positive": "Θετικό", "negative": "Αρνητικό", "neutral": "Ουδέτερο", "mixed": "Μικτό",
    "joy": "Χαρά", "anger": "Θυμός", "sadness": "Λύπη", "fear": "Φόβος",
    "disgust": "Αποστροφή", "surprise": "Έκπληξη",
    "media": "ΜΜΕ", "person": "Πρόσωπα", "brand_owned": "Owned", "organization": "Οργανισμοί", "unknown": "Άγνωστο",
}

def _cat_key(label) -> str:
    return str(label or "").strip().lower()

def _cat_label(label, lang: str) -> str:
    key = _cat_key(label)
    if lang == "el" and key in CATEGORY_LABELS_EL:
        return CATEGORY_LABELS_EL[key]
    return str(label)

def _semantic_color(label) -> str | None:
    key = _cat_key(label)
    return SENTIMENT_COLORS.get(key) or EMOTION_COLORS.get(key)


class PresentationCancelled(RuntimeError):
    pass


class PresentationValidationError(RuntimeError):
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
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _json_safe(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return copy.deepcopy(value)


def _hash_payload(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _lang_code(plan: dict | None) -> str:
    return "el" if str((plan or {}).get("report_language") or "English") == "Ελληνικά" else "en"


def _txt(value, lang: str) -> str:
    if isinstance(value, dict):
        return str(value.get(lang) or value.get("en") or value.get("el") or "")
    return str(value or "")


def _clean_text(text, limit: int = 5000) -> str:
    # Keep evidence text literal but remove control chars that can break OOXML/XML.
    out = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", " ", str(text or ""))
    out = re.sub(r"\s+", " ", out).strip()
    return out[:limit]


def _chart_map(visual_pack: dict) -> dict[str, dict]:
    return {str(c.get("chart_id")): copy.deepcopy(c) for c in (visual_pack.get("chart_specs") or []) if c.get("chart_id")}


def _inventory_map(evidence_pack: dict) -> dict[str, dict]:
    return {str(x.get("indicator_id")): x for x in (evidence_pack.get("indicator_inventory") or []) if x.get("indicator_id")}


def _indicator_value(inv: dict[str, dict], indicator_id: str):
    return copy.deepcopy((inv.get(indicator_id) or {}).get("value"))


def _claim(claim_id: str, text: str, indicator_ids: list[str], *, evidence_refs=None, claim_type="descriptive", confidence=None, causal_status="not_applicable", source_values=None) -> dict:
    return {
        "claim_id": claim_id,
        "text": _clean_text(text, 1200),
        "indicator_ids": list(dict.fromkeys(indicator_ids)),
        "evidence_refs": list(dict.fromkeys([str(x) for x in (evidence_refs or []) if x])),
        "claim_type": claim_type,
        "confidence": confidence,
        "causal_status": causal_status,
        "source_values": _json_safe(source_values or {}),
    }




def _resolve_investigation_indicator_ids(item: dict) -> list[str]:
    """Map Step 6 investigation objects to the 25-indicator contract.

    Older or externally produced investigation records may not carry explicit
    indicator_ids. Step 8 still requires traceability, so we infer a conservative
    indicator set from the investigation type and payload instead of creating
    untraceable presentation claims.
    """
    expected = {x[0] for x in INDICATOR_REGISTRY}
    ids: list[str] = []

    def add(value):
        if value is None:
            return
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple, set)):
            values = list(value)
        else:
            return
        for raw in values:
            text = str(raw or "").strip()
            if text in expected and text not in ids:
                ids.append(text)

    for key in ("indicator_ids", "related_indicator_ids", "indicator_family_ids", "presentation_indicator_ids"):
        add(item.get(key))
    for key in ("primary_indicator", "target_indicator", "indicator_id"):
        add(item.get(key))

    typ = str(item.get("type") or "").strip()
    type_map = {
        "numeric_anomaly": ["numeric_anomalies", "time_trends"],
        "reputation_daily_change": ["brand_reputation", "time_trends"],
        "period_shift": ["brand_reputation", "time_trends"],
        "negative_narrative_driver": ["negative_narrative_drivers", "brand_reputation"],
        "positive_narrative_driver": ["positive_narrative_drivers", "brand_reputation"],
        "emotion_profile": ["emotions", "sentiment"],
        "source_divergence": ["source_breakdown", "brand_reputation"],
        "media_people_divergence": ["origin_breakdown", "media_influence", "people_influence"],
        "coordination_signal": ["coordination", "authenticity_risk"],
        "data_quality_guardrail": ["data_quality", "source_coverage", "sample_achievement"],
        "emerging_narrative": ["negative_narrative_drivers", "positive_narrative_drivers", "time_trends"],
        "story_syndication": ["story_syndication", "source_breakdown"],
    }
    add(type_map.get(typ))

    metrics_blob = json.dumps(_json_safe(item.get("metrics") or item.get("trigger") or {}), ensure_ascii=False).lower()
    if "reputation" in metrics_blob:
        add(["brand_reputation"])
    if any(word in metrics_blob for word in ("anger", "fear", "emotion", "joy", "sadness", "disgust", "surprise")):
        add(["emotions"])
    if any(word in metrics_blob for word in ("source", "platform")):
        add(["source_breakdown"])
    if any(word in metrics_blob for word in ("quality", "coverage", "sample")):
        add(["data_quality", "source_coverage", "sample_achievement"])

    # Last-resort traceability: this is still a valid indicator family, and the
    # finding remains evidence-linked through evidence_record_ids when available.
    if not ids:
        add(["top_mentions"])
    return ids


def _slide(slide_id: str, slide_type: str, title: str, *, subtitle="", chart_ids=None, investigation_ids=None, claims=None, priority=50, required=False, section="body", notes=None) -> dict:
    return {
        "slide_id": slide_id,
        "slide_type": slide_type,
        "title": _clean_text(title, 250),
        "subtitle": _clean_text(subtitle, 600),
        "chart_ids": list(dict.fromkeys(chart_ids or [])),
        "investigation_ids": list(dict.fromkeys(investigation_ids or [])),
        "claims": list(claims or []),
        "priority": max(0, min(100, int(priority))),
        "required": bool(required),
        "section": section,
        "notes": copy.deepcopy(notes or {}),
    }


def _largest_distribution(chart: dict | None) -> tuple[str | None, float]:
    cats = ((chart or {}).get("data") or {}).get("categories") or []
    vals = [(str(x.get("label") or ""), _safe_float(x.get("value"))) for x in cats]
    return max(vals, key=lambda x: x[1]) if vals else (None, 0.0)


def _material_quality_warning(evidence_pack: dict) -> bool:
    inv = _inventory_map(evidence_pack)
    quality = _indicator_value(inv, "data_quality") or {}
    coverage = _indicator_value(inv, "source_coverage") or {}
    sample = _indicator_value(inv, "sample_achievement") or {}
    auth = _indicator_value(inv, "authenticity_risk") or {}
    coord = _indicator_value(inv, "coordination") or []
    return (
        _safe_float(quality.get("score"), 100) < 80
        or _safe_float(coverage.get("ratio"), 1) < 0.75
        or _safe_float(sample.get("ratio"), 1) < 0.85
        or _safe_float(auth.get("low_authenticity_share"), 0) >= 0.05
        or bool(coord)
        or bool((evidence_pack.get("headline_metrics") or {}).get("step5_warnings"))
    )


def _validate_inputs(visual_pack: dict, evidence_pack: dict, plan: dict | None = None) -> None:
    if not isinstance(visual_pack, dict) or not isinstance(evidence_pack, dict):
        raise PresentationValidationError("Step 8 requires both the Step 7 Presentation Visual Pack and the Step 6 Evidence Pack.")
    guard = visual_pack.get("guardrails") or {}
    if guard.get("must_review_all_indicators") is not True or guard.get("native_editable_charts_required") is not True:
        raise PresentationValidationError("Step 8 stopped because the Step 7 visual guardrails are incomplete.")
    review = visual_pack.get("indicator_review") or []
    expected = [x[0] for x in INDICATOR_REGISTRY]
    got = [str(x.get("indicator_id")) for x in review]
    if got != expected or len(got) != len(set(got)) or not all(x.get("examined") is True for x in review):
        raise PresentationValidationError("Step 8 stopped because not all 25 indicators were reviewed exactly once.")
    charts = visual_pack.get("chart_specs") or []
    if any((c.get("presentation") or {}).get("native_editable_ready") is not True for c in charts):
        raise PresentationValidationError("Step 8 requires every selected Step 7 chart to be native-editable ready.")
    if any((c.get("presentation") or {}).get("render_as_raster") is True for c in charts):
        raise PresentationValidationError("Step 8 refuses raster chart contracts.")
    if (visual_pack.get("guardrails") or {}).get("must_not_present_association_as_causation") is not True:
        raise PresentationValidationError("Step 8 requires the causality guardrail.")
    if plan:
        ctx = visual_pack.get("research_context") or {}
        for key in ("client", "topic", "market", "date_from", "date_to"):
            if ctx.get(key) not in (None, "") and plan.get(key) not in (None, "") and str(ctx.get(key)) != str(plan.get(key)):
                raise PresentationValidationError(f"Step 8 research context mismatch for {key}.")



GOLD_STANDARD_CAPABILITIES = (
    ("context", "Research context / cover"),
    ("evidence_base", "Evidence base, period and sample context"),
    ("methodology", "Methodology / reading guide"),
    ("reputation_sentiment", "Brand Reputation + sentiment"),
    ("sentiment_evidence", "Positive / negative evidence examples"),
    ("emotional_profile", "Emotion distribution"),
    ("emotion_evolution", "Emotion evolution when time evidence exists"),
    ("drivers", "Positive / negative narrative and topic drivers"),
    ("evolution", "Time evolution and anomalies"),
    ("investigations", "Automatic investigations when triggered"),
    ("sources_audiences", "Media / people / source context"),
    ("influence", "Media and people influence rankings"),
    ("media_evidence", "High-impact media evidence when available"),
    ("media_implications", "Media / channel implications when a divergence is detected"),
    ("quality", "Data quality / coverage / authenticity limits"),
    ("evidence_traceability", "Evidence behind findings"),
    ("strategic_synthesis", "Strategic synthesis / implications"),
    ("conclusions", "Conclusions and what to watch"),
    ("analyst_findings", "Senior analyst evidence-grounded synthesis"),
    ("recommendations", "Evidence-linked recommendations and monitoring actions"),
)


def _top_mentions(inv: dict[str, dict]) -> list[dict]:
    value = _indicator_value(inv, "top_mentions") or []
    return [copy.deepcopy(x) for x in value if isinstance(x, dict)]


def _mention_claim(prefix: str, row: dict, *, lang: str, index: int) -> dict:
    label = str(row.get("sentiment_label") or "evidence").lower()
    author = _clean_text(row.get("author") or row.get("platform") or "Source", 80)
    excerpt = _clean_text(row.get("excerpt") or "", 230)
    meta_bits = [b for b in (row.get("platform"), row.get("date")) if b]
    score = row.get("sentiment_score"); impact = row.get("impact_score")
    if score is not None: meta_bits.append(f"sentiment {_safe_float(score):+.2f}")
    if impact is not None: meta_bits.append(f"impact {_safe_float(impact):.2f}")
    meta = " · ".join(str(b) for b in meta_bits)
    text = (f"{author}: {excerpt}" if excerpt else author) + (f" | {meta}" if meta else "")
    return _claim(
        f"{prefix}-{index}", text, ["top_mentions"], evidence_refs=[row.get("record_id")],
        claim_type="evidence_example", confidence=row.get("evidence_confidence"),
        source_values={"sentiment": label, "impact": row.get("impact_score"), "origin_group": row.get("origin_group"), "platform": row.get("platform")},
    )


def _gold_standard_audit(slides: list[dict], charts: dict[str, dict], inv: dict[str, dict], investigations: dict[str, dict]) -> dict:
    slide_ids = {str(s.get("slide_id")) for s in slides}
    mentions = _top_mentions(inv)
    sentiment_labels = {str(x.get("sentiment_label") or "").lower() for x in mentions if x.get("excerpt")}
    media_mentions = [x for x in mentions if str(x.get("origin_group") or "").lower() == "media" and x.get("excerpt")]
    emotion_chart = charts.get("emotion_distribution")
    _, largest_emotion_value = _largest_distribution(emotion_chart)
    emotion_material = bool(emotion_chart) and (largest_emotion_value >= 12.0 or any(str(x.get("type")) == "emotion_profile" for x in investigations.values()))
    investigation_material = any(
        _safe_int((x.get("presentation") or {}).get("priority_score")) >= 55
        and (_txt(x.get("finding"), "en") or _txt(x.get("question"), "en"))
        for x in investigations.values()
    )
    media_implication_ids = {
        str(x.get("investigation_id")) for x in investigations.values()
        if str(x.get("type") or "") in {"source_divergence", "media_people_divergence"}
        and _safe_int((x.get("presentation") or {}).get("priority_score")) >= 55
        and (_txt(x.get("finding"), "en") or _txt(x.get("question"), "en"))
    }
    media_implication_slides = {
        str(slide.get("slide_id")) for slide in slides
        if media_implication_ids & {str(x) for x in (slide.get("investigation_ids") or [])}
    }
    applicable = {
        "context": True,
        "evidence_base": True,
        "methodology": True,
        "reputation_sentiment": bool((inv.get("brand_reputation") or {}).get("available") or (inv.get("sentiment") or {}).get("available")),
        "sentiment_evidence": "positive" in sentiment_labels and "negative" in sentiment_labels,
        "emotional_profile": emotion_material,
        "emotion_evolution": "emotion_trends" in charts,
        "drivers": bool((inv.get("positive_narrative_drivers") or {}).get("available") or (inv.get("negative_narrative_drivers") or {}).get("available") or (inv.get("topic_drivers") or {}).get("available")),
        "evolution": "time_trends" in charts,
        "investigations": investigation_material,
        "sources_audiences": bool((inv.get("origin_breakdown") or {}).get("available") or (inv.get("source_breakdown") or {}).get("available")),
        "influence": bool((inv.get("media_influence") or {}).get("available") or (inv.get("people_influence") or {}).get("available")),
        "media_evidence": len(media_mentions) >= 2,
        "media_implications": bool(media_implication_ids),
        "quality": any(bool((inv.get(k) or {}).get("available")) for k in ("data_quality","source_coverage","sample_achievement","authenticity_risk","coordination")),
        "evidence_traceability": bool(mentions),
        "strategic_synthesis": bool((inv.get("positive_narrative_drivers") or {}).get("available") or (inv.get("negative_narrative_drivers") or {}).get("available") or investigations),
        "conclusions": True,
        "analyst_findings": "analyst_findings" in slide_ids,
        "recommendations": "recommendations" in slide_ids,
    }
    coverage_map = {
        "context": {"cover"},
        "evidence_base": {"evidence_base"},
        "methodology": {"methodology"},
        "reputation_sentiment": {"reputation_sentiment"},
        "sentiment_evidence": {"sentiment_evidence"},
        "emotional_profile": {"emotions"},
        "emotion_evolution": {"emotion_evolution"},
        "drivers": {"drivers"},
        "evolution": {"evolution"},
        "investigations": {x for x in slide_ids if x.startswith("investigation_")},
        "sources_audiences": {"sources_audiences"},
        "influence": {"influence"},
        "media_evidence": {"media_evidence"},
        "media_implications": media_implication_slides,
        "quality": {"data_integrity", "evidence_base", "methodology"},
        "evidence_traceability": {"evidence"},
        "strategic_synthesis": {"strategic_synthesis"},
        "conclusions": {"conclusions"},
        "analyst_findings": {"analyst_findings"},
        "recommendations": {"recommendations"},
    }
    rows=[]
    for key,label in GOLD_STANDARD_CAPABILITIES:
        is_app=bool(applicable.get(key))
        covered=bool(slide_ids & coverage_map.get(key,set())) if is_app else False
        rows.append({"capability_id":key,"label":label,"applicable":is_app,"status":"covered" if covered else ("not_applicable" if not is_app else "missing"),"slide_ids":sorted(slide_ids & coverage_map.get(key,set()))})
    denom=sum(1 for r in rows if r["applicable"])
    covered_count=sum(1 for r in rows if r["applicable"] and r["status"]=="covered")
    return {"benchmark":"EUROJACKPOT collaborator deck capability benchmark","capabilities":rows,"applicable":denom,"covered":covered_count,"coverage_percent":round(100*covered_count/denom,2) if denom else 100.0,"complete":covered_count==denom}


def build_presentation_plan(visual_pack: dict, evidence_pack: dict, plan: dict | None = None, *, cancel_check: Callable[[], bool] | None = None) -> dict:
    _validate_inputs(visual_pack, evidence_pack, plan)
    if cancel_check and cancel_check():
        raise PresentationCancelled("Presentation planning cancelled before slide selection")

    lang = _lang_code(plan)
    ctx = copy.deepcopy(visual_pack.get("research_context") or {})
    charts = _chart_map(visual_pack)
    inv = _inventory_map(evidence_pack)
    investigations = {str(x.get("investigation_id")): x for x in (visual_pack.get("investigation_candidates") or []) if x.get("investigation_id")}

    title = _clean_text(ctx.get("topic") or "Analysis")
    client = _clean_text(ctx.get("client") or "")
    market = _clean_text(ctx.get("market") or "")
    d_from = _clean_text(ctx.get("date_from") or "")
    d_to = _clean_text(ctx.get("date_to") or "")

    slides: list[dict] = []
    slides.append(_slide("cover", "cover", title, subtitle=f"{client} · {market} · {d_from} — {d_to}".strip(" ·—"), priority=100, required=True, section="opening"))

    rep = _indicator_value(inv, "brand_reputation") or {}
    sentiment_chart = charts.get("sentiment_distribution")
    emotion_chart = charts.get("emotion_distribution")
    source_chart = charts.get("source_comparison")
    narrative_chart = charts.get("narrative_drivers")
    trend_chart = charts.get("time_trends")
    quality_chart = charts.get("quality_matrix")
    top_mentions_chart = charts.get("top_mentions")
    mentions = _top_mentions(inv)

    exec_claims = []
    if rep.get("index") is not None:
        exec_claims.append(_claim("exec-reputation", (f"Brand Reputation: {_safe_float(rep.get('index')):.1f}/100" if lang=="en" else f"Brand Reputation: {_safe_float(rep.get('index')):.1f}/100 (δείκτης ψηφιακής συζήτησης)"), ["brand_reputation"], claim_type="deterministic_metric", source_values={"brand_reputation": rep.get("index")}))
    if sentiment_chart:
        label, value = _largest_distribution(sentiment_chart)
        if label:
            exec_claims.append(_claim("exec-sentiment", (f"Largest weighted sentiment group: {label} ({value:.1f}%)" if lang=="en" else f"Μεγαλύτερη σταθμισμένη ομάδα sentiment: {_cat_label(label, lang)} ({value:.1f}%)"), ["sentiment"], claim_type="deterministic_metric", source_values={label: value}))
    if trend_chart:
        rows = (trend_chart.get("data") or {}).get("rows") or []
        if len(rows) >= 2:
            first, last = rows[0], rows[-1]
            if first.get("brand_reputation_index") is not None and last.get("brand_reputation_index") is not None:
                delta = _safe_float(last.get("brand_reputation_index")) - _safe_float(first.get("brand_reputation_index"))
                exec_claims.append(_claim("exec-trend", ((f"Brand Reputation changed {delta:+.1f} points across the observed period." if lang=="en" else f"Το Brand Reputation μεταβλήθηκε κατά {delta:+.1f} μονάδες στην περίοδο παρατήρησης.") if lang=="en" else f"Το Brand Reputation μεταβλήθηκε κατά {delta:+.1f} μονάδες στην περίοδο παρατήρησης."), ["time_trends", "brand_reputation"], claim_type="deterministic_change", source_values={"delta": round(delta, 2)}))

    inv_candidates = sorted(investigations.values(), key=lambda x: (_safe_int((x.get("presentation") or {}).get("priority_score")), _safe_float((x.get("confidence") or {}).get("score"))), reverse=True)
    if inv_candidates:
        top = inv_candidates[0]
        finding = _txt(top.get("finding"), lang)
        if finding:
            exec_claims.append(_claim("exec-investigation", finding, _resolve_investigation_indicator_ids(top), evidence_refs=top.get("evidence_record_ids") or [], claim_type=str(top.get("conclusion_type") or "investigation"), confidence=(top.get("confidence") or {}).get("score"), causal_status=str(top.get("causal_status") or "not_proven"), source_values=(top.get("metrics") or {})))
    slides.append(_slide("executive_summary", "executive_summary", "Executive summary" if lang == "en" else "Σύνοψη", chart_ids=[x for x in ["brand_reputation", "evidence_confidence"] if x in charts], claims=exec_claims, priority=100, required=True, section="opening"))

    # Gold-standard scope: period/sample/audience context must be explicit, not hidden in notes.
    scope_charts = [x for x in ["sample_overview", "origin_breakdown", "market_relevance", "impact_coverage"] if x in charts]
    scope_claims = []
    sample_value = _indicator_value(inv, "sample_volume") or {}
    market_value = _indicator_value(inv, "market_relevance") or {}
    if sample_value:
        scope_claims.append(_claim("scope-sample", f"Analysis-ready evidence: {_safe_int(sample_value.get('analysis_ready_records'))} records; effective independent voices: {_safe_float(sample_value.get('effective_independent_voices')):.1f}.", ["sample_volume", "effective_independent_voices"], claim_type="deterministic_metric", source_values=sample_value))
    if market_value.get("high_relevance_share") is not None:
        scope_claims.append(_claim("scope-market", f"High target-market relevance: {100*_safe_float(market_value.get('high_relevance_share')):.1f}%.", ["market_relevance"], claim_type="deterministic_metric", source_values=market_value))
    slides.append(_slide("evidence_base", "scope", "Evidence base & scope" if lang == "en" else "Βάση δεδομένων & εύρος", chart_ids=scope_charts[:2], claims=scope_claims, priority=97, required=True, section="opening"))

    # Reputation + sentiment are core and are grouped deliberately to mirror the original report's analytical spine.
    rep_charts = [x for x in ["brand_reputation", "sentiment_distribution", "stance_distribution"] if x in charts]
    if rep_charts:
        slides.append(_slide("reputation_sentiment", "metrics", "Reputation & sentiment" if lang == "en" else "Φήμη & συναίσθημα", chart_ids=rep_charts, priority=98, section="perception"))

    positive_mentions = [m for m in mentions if str(m.get("sentiment_label") or "").lower() == "positive" and m.get("excerpt")]
    negative_mentions = [m for m in mentions if str(m.get("sentiment_label") or "").lower() == "negative" and m.get("excerpt")]
    if positive_mentions and negative_mentions:
        evidence_claims = []
        pos_sorted = sorted(positive_mentions, key=lambda m: _safe_float(m.get("impact_score")), reverse=True)
        neg_sorted = sorted(negative_mentions, key=lambda m: _safe_float(m.get("impact_score")), reverse=True)
        for i,m in enumerate(pos_sorted[:5], start=1):
            evidence_claims.append(_mention_claim("positive-evidence", m, lang=lang, index=i))
        for i,m in enumerate(neg_sorted[:5], start=1):
            evidence_claims.append(_mention_claim("negative-evidence", m, lang=lang, index=i))
        slides.append(_slide("sentiment_evidence", "evidence_split", "Top 5 positive & negative mentions" if lang == "en" else "Top 5 θετικές & αρνητικές αναφορές", claims=evidence_claims, priority=92, section="perception", notes={"split_by":"sentiment"}))

    if emotion_chart:
        emo_label, emo_value = _largest_distribution(emotion_chart)
        material = emo_value >= 12.0 or any(str(x.get("type")) == "emotion_profile" for x in investigations.values())
        if material:
            slides.append(_slide("emotions", "distribution", "Emotional profile" if lang == "en" else "Συναισθηματικό προφίλ", chart_ids=["emotion_distribution"], claims=[_claim("emotion-leading", f"Leading emotion: {emo_label} ({emo_value:.1f}%)", ["emotions"], claim_type="deterministic_metric", source_values={emo_label: emo_value})] if emo_label else [], priority=86, section="perception"))
    if "emotion_trends" in charts:
        slides.append(_slide("emotion_evolution", "trend", "Emotion evolution" if lang == "en" else "Εξέλιξη συναισθημάτων", chart_ids=["emotion_trends"], priority=84, section="perception"))

    if narrative_chart:
        rows = ((narrative_chart.get("data") or {}).get("rows") or [])
        claims = []
        for i, r in enumerate(rows[:4]):
            claims.append(_claim(f"driver-{i}", f"{_clean_text(r.get('name'))}: {_safe_float(r.get('contribution')):+.2f} reputation points", ["positive_narrative_drivers", "negative_narrative_drivers"], claim_type="deterministic_contribution", source_values={"contribution": r.get("contribution")}))
        slides.append(_slide("drivers", "drivers", "What moves Brand Reputation" if lang == "en" else "Τι μετακινεί το Brand Reputation", chart_ids=["narrative_drivers"] + (["topic_drivers"] if "topic_drivers" in charts else []), claims=claims, priority=100, section="drivers"))

    if trend_chart:
        slides.append(_slide("evolution", "trend", "Evolution over time" if lang == "en" else "Εξέλιξη στον χρόνο", chart_ids=["time_trends"] + (["anomalies"] if "anomalies" in charts else []), priority=98, section="evolution"))

    # Top automatic investigations become dedicated slides. Keep the number bounded to avoid a bloated report.
    inv_slides = 0
    for item in inv_candidates:
        priority = _safe_int((item.get("presentation") or {}).get("priority_score"))
        if priority < 55 or inv_slides >= 3:
            continue
        finding = _txt(item.get("finding"), lang)
        question = _txt(item.get("question"), lang)
        if not finding and not question:
            continue
        iid = str(item.get("investigation_id"))
        claims = []
        if finding:
            claims.append(_claim(f"{iid}-finding", finding, _resolve_investigation_indicator_ids(item), evidence_refs=item.get("evidence_record_ids") or [], claim_type=str(item.get("conclusion_type") or "investigation"), confidence=(item.get("confidence") or {}).get("score"), causal_status=str(item.get("causal_status") or "not_proven"), source_values=item.get("metrics") or {}))
        related = []
        typ = str(item.get("type") or "")
        if typ in {"numeric_anomaly", "reputation_daily_change", "period_shift", "emerging_narrative"}:
            related = [x for x in ["time_trends", "anomalies", "narrative_drivers"] if x in charts]
        elif typ in {"negative_narrative_driver", "positive_narrative_driver"}:
            related = [x for x in ["narrative_drivers", "topic_drivers"] if x in charts]
        elif typ == "emotion_profile":
            related = [x for x in ["emotion_distribution", "time_trends"] if x in charts]
        elif typ in {"source_divergence", "media_people_divergence"}:
            related = [x for x in ["source_comparison", "origin_breakdown"] if x in charts]
        elif typ in {"coordination_signal", "data_quality_guardrail"}:
            related = [x for x in ["authenticity_coordination", "quality_matrix"] if x in charts]
        slides.append(_slide(f"investigation_{inv_slides+1}", "investigation", question or ("Automatic investigation" if lang == "en" else "Αυτόματη διερεύνηση"), subtitle=finding, chart_ids=related[:2], investigation_ids=[iid], claims=claims, priority=priority, section="investigations", notes={"causality_guardrail": "Association ≠ proven causality."}))
        inv_slides += 1

    # Gold-standard guarantee: media/source divergence investigations that qualify for
    # presentation must never be silently dropped by the slide cap.
    placed_inv_ids = {str(x) for sl in slides for x in (sl.get("investigation_ids") or [])}
    for item in inv_candidates:
        typ = str(item.get("type") or "")
        if typ not in {"source_divergence", "media_people_divergence"}:
            continue
        if _safe_int((item.get("presentation") or {}).get("priority_score")) < 55:
            continue
        iid = str(item.get("investigation_id"))
        if iid in placed_inv_ids:
            continue
        finding = _txt(item.get("finding"), lang)
        question = _txt(item.get("question"), lang)
        if not finding and not question:
            continue
        claims = []
        if finding:
            claims.append(_claim(f"{iid}-finding", finding, _resolve_investigation_indicator_ids(item), evidence_refs=item.get("evidence_record_ids") or [], claim_type=str(item.get("conclusion_type") or "investigation"), confidence=(item.get("confidence") or {}).get("score"), causal_status=str(item.get("causal_status") or "not_proven"), source_values=item.get("metrics") or {}))
        related = [x for x in ["source_comparison", "origin_breakdown"] if x in charts]
        slides.append(_slide(f"investigation_{inv_slides+1}", "investigation", question or ("Automatic investigation" if lang == "en" else "Αυτόματη διερεύνηση"), subtitle=finding, chart_ids=related[:2], investigation_ids=[iid], claims=claims, priority=56, section="investigations", notes={"causality_guardrail": "Association ≠ proven causality."}))
        inv_slides += 1
        placed_inv_ids.add(iid)

    if source_chart or "origin_breakdown" in charts:
        slides.append(_slide("sources_audiences", "sources", "Sources & audiences" if lang == "en" else "Πηγές & κοινά", chart_ids=[x for x in ["source_comparison", "origin_breakdown"] if x in charts], priority=88, section="context"))

    influence = [x for x in ["media_influence", "people_influence"] if x in charts]
    if influence:
        slides.append(_slide("influence", "influence", "Who shapes the conversation" if lang == "en" else "Ποιοι διαμορφώνουν τη συζήτηση", chart_ids=influence, priority=80, section="context"))

    media_mentions = [m for m in mentions if str(m.get("origin_group") or "").lower() == "media" and m.get("excerpt")]
    if len(media_mentions) >= 2:
        media_claims = [_mention_claim("media-evidence", m, lang=lang, index=i) for i,m in enumerate(media_mentions[:5], start=1)]
        slides.append(_slide("media_evidence", "evidence_cards", "Media coverage & message" if lang == "en" else "Media κάλυψη & μήνυμα", claims=media_claims, priority=82, section="context"))

    # Voices of the conversation: the leading voice per sentiment, then a ranked
    # appendix of up to 30 voices so the client sees WHO is talking, not just numbers.
    people_rows = [r for r in (_indicator_value(inv, "people_influence") or []) if isinstance(r, dict) and r.get("name")]
    leader_by_sent: dict[str, dict] = {}
    for m in mentions:
        lbl = str(m.get("sentiment_label") or "").lower()
        if lbl in {"positive", "negative", "neutral", "mixed"} and m.get("author"):
            cur = leader_by_sent.get(lbl)
            if cur is None or _safe_float(m.get("impact_score")) > _safe_float(cur.get("impact_score")):
                leader_by_sent[lbl] = m
    if leader_by_sent:
        lead_claims = []
        order = ["positive", "negative", "neutral", "mixed"]
        names_el = {"positive": "Κορυφαία θετική φωνή", "negative": "Κορυφαία αρνητική φωνή", "neutral": "Κορυφαία ουδέτερη φωνή", "mixed": "Κορυφαία μικτή φωνή"}
        names_en = {"positive": "Top positive voice", "negative": "Top negative voice", "neutral": "Top neutral voice", "mixed": "Top mixed voice"}
        for li, lbl in enumerate([x for x in order if x in leader_by_sent], start=1):
            m = leader_by_sent[lbl]
            head = names_en[lbl] if lang == "en" else names_el[lbl]
            body = f"{head}: {_clean_text(m.get('author'), 70)} — “{_clean_text(m.get('excerpt'), 170)}” | {m.get('platform')} · {m.get('date')} · impact {_safe_float(m.get('impact_score')):.2f}"
            lead_claims.append(_claim(f"voice-leader-{li}", body, ["top_mentions", "people_influence" if (inv.get("people_influence") or {}).get("available") else "top_mentions"], evidence_refs=[m.get("record_id")], claim_type="evidence_example", source_values={"sentiment": lbl, "valence": lbl}))
        slides.append(_slide("voices_leaders", "narratives", "Leading voices per sentiment" if lang == "en" else "Οι κορυφαίες φωνές ανά sentiment", claims=lead_claims, priority=79, section="context"))
    if people_rows:
        ranked = sorted(people_rows, key=lambda r: _safe_float(r.get("attention")), reverse=True)[:30]
        page_size = 12
        pages = [ranked[i:i + page_size] for i in range(0, len(ranked), page_size)]
        for pi, page in enumerate(pages, start=1):
            title = ("Voices of the conversation" if lang == "en" else "Οι φωνές της συζήτησης") + (f" ({pi}/{len(pages)})" if len(pages) > 1 else "")
            slides.append(_slide(f"voices_ranking_{pi}", "voices_table", title, priority=40, section="appendix", notes={"rows": [
                {"rank": (pi - 1) * page_size + i + 1, "name": _clean_text(r.get("name"), 60), "records": _safe_int(r.get("records")), "attention": _safe_float(r.get("attention")), "weighted_sentiment": _safe_float(r.get("weighted_sentiment"))}
                for i, r in enumerate(page)
            ], "denominator": len(people_rows)}))

    if _material_quality_warning(evidence_pack):
        qcharts = [x for x in ["quality_matrix", "market_relevance", "impact_coverage", "authenticity_coordination", "story_syndication"] if x in charts]
        slides.append(_slide("data_integrity", "quality", "Data integrity & interpretation limits" if lang == "en" else "Ακεραιότητα δεδομένων & όρια ερμηνείας", chart_ids=qcharts[:3], claims=[_claim("quality-causality", "Association ≠ proven causality." if lang == "en" else "Συσχέτιση ≠ αποδεδειγμένη αιτιότητα.", ["data_quality", "source_coverage"], claim_type="guardrail")], priority=94, required=True, section="quality"))

    if top_mentions_chart:
        slides.append(_slide("evidence", "evidence", "Evidence behind the findings" if lang == "en" else "Evidence πίσω από τα ευρήματα", chart_ids=["top_mentions"], priority=90, section="evidence"))

    # Strategic synthesis mirrors the old report's strengths/frictions/opportunities/risks logic without inventing strategy.
    synthesis_claims = []
    if narrative_chart:
        rows = ((narrative_chart.get("data") or {}).get("rows") or [])
        pos = [r for r in rows if _safe_float(r.get("contribution")) > 0]
        neg = [r for r in rows if _safe_float(r.get("contribution")) < 0]
        if pos:
            r=max(pos,key=lambda x:_safe_float(x.get("contribution")))
            synthesis_claims.append(_claim("synthesis-strength", f"Strength signal: {_clean_text(r.get('name'))} ({_safe_float(r.get('contribution')):+.2f} Reputation points)", ["positive_narrative_drivers"], claim_type="deterministic_contribution", source_values={"role":"strength","contribution":r.get("contribution")}))
        if neg:
            r=min(neg,key=lambda x:_safe_float(x.get("contribution")))
            synthesis_claims.append(_claim("synthesis-friction", f"Friction signal: {_clean_text(r.get('name'))} ({_safe_float(r.get('contribution')):+.2f} Reputation points)", ["negative_narrative_drivers"], claim_type="deterministic_contribution", source_values={"role":"friction","contribution":r.get("contribution")}))
    if trend_chart:
        rows=(trend_chart.get("data") or {}).get("rows") or []
        if len(rows)>=2 and rows[0].get("brand_reputation_index") is not None and rows[-1].get("brand_reputation_index") is not None:
            delta=_safe_float(rows[-1].get("brand_reputation_index"))-_safe_float(rows[0].get("brand_reputation_index"))
            synthesis_claims.append(_claim("synthesis-momentum", f"Momentum signal: Brand Reputation moved {delta:+.1f} points across the observed period.", ["brand_reputation","time_trends"], claim_type="deterministic_change", source_values={"role":"momentum","delta":round(delta,2)}))
    if inv_candidates:
        top=inv_candidates[0]
        synthesis_claims.append(_claim("synthesis-watch", ("Watch signal: " if lang=="en" else "Σήμα παρακολούθησης: ")+_txt(top.get("question"),lang), _resolve_investigation_indicator_ids(top), evidence_refs=top.get("evidence_record_ids") or [], claim_type="attention_point", causal_status="not_proven", source_values={"role":"watch"}))
    if synthesis_claims:
        slides.append(_slide("strategic_synthesis", "strategic_synthesis", "Strategic synthesis" if lang=="en" else "Στρατηγική σύνθεση", claims=synthesis_claims[:4], priority=96, section="closing", notes={"causality_guardrail":"Association ≠ proven causality."}))

    analyst = visual_pack.get("analyst_synthesis") or {}
    if analyst.get("narratives"):
        mention_by_id={str(m.get("record_id")):m for m in _top_mentions(inv)}
        groups: list[tuple[str,list[dict]]] = []
        for n in (analyst.get("narratives") or [])[:8]:
            theme=_clean_text(n.get("theme") or "", 60) or ("Other themes" if lang=="en" else "Λοιπά θέματα")
            for g in groups:
                if g[0]==theme:
                    g[1].append(n); break
            else:
                groups.append((theme,[n]))
        # One slide per theme; a large theme spills onto a continuation slide.
        nar_slides=0; ci=0
        for theme,items in groups:
            for chunk_start in range(0,len(items),3):
                if nar_slides>=3: break
                chunk=items[chunk_start:chunk_start+3]
                nar_claims=[]
                for n in chunk:
                    ci+=1
                    origin_lbl={"media":("media","ΜΜΕ"),"organic_people":("organic voices","οργανικές φωνές"),"owned":("owned","owned/promo"),"mixed":("mixed","μικτής προέλευσης"),"unknown":("unknown","άγνωστης προέλευσης")}.get(str(n.get("origin")),("mixed","μικτής προέλευσης"))
                    text=(f"{_clean_text(n.get('title'),110)} — {_clean_text(n.get('idea'),330)} | Σημασία για το brand: {_clean_text(n.get('brand_meaning'),260)} | Βαρύτητα: {_clean_text(n.get('importance'),160)} ({origin_lbl[1]})" if lang=="el" else
                          f"{_clean_text(n.get('title'),110)} — {_clean_text(n.get('idea'),330)} | Brand meaning: {_clean_text(n.get('brand_meaning'),260)} | Weight: {_clean_text(n.get('importance'),160)} ({origin_lbl[0]})")
                    quotes=[]
                    for rid in (n.get("evidence_record_ids") or [])[:2]:
                        m=mention_by_id.get(str(rid))
                        if m and m.get("excerpt"):
                            quotes.append({"excerpt":_clean_text(m.get("excerpt"),210),"platform":m.get("platform"),"date":m.get("date"),"author":m.get("author")})
                    nar_claims.append(_claim(f"narrative-{ci}",text,list(n.get("indicator_ids") or []),evidence_refs=n.get("evidence_record_ids") or [],claim_type="analyst_finding",causal_status="not_proven",source_values={"valence":n.get("valence"),"origin":n.get("origin"),"quotes":quotes}))
                nar_slides+=1
                base_title="What people really say" if lang=="en" else "Τι πραγματικά λέει ο κόσμος"
                slides.append(_slide(f"consumer_narratives_{nar_slides}","narratives",f"{base_title} · {theme}",claims=nar_claims,priority=93,section="perception",notes={"causality_guardrail":"Association ≠ proven causality."}))
    if analyst.get("strategic_implications"):
        imp_claims=[]
        for i,m in enumerate((analyst.get("strategic_implications") or [])[:4],start=1):
            text=(f"Insight: {_clean_text(m.get('insight'),260)} | Implication: {_clean_text(m.get('implication'),260)} | Action: {_clean_text(m.get('action'),260)} | Objective: {_clean_text(m.get('objective'),200)}" if lang=="en" else
                  f"Insight: {_clean_text(m.get('insight'),260)} | Επίπτωση: {_clean_text(m.get('implication'),260)} | Ενέργεια: {_clean_text(m.get('action'),260)} | Στόχος: {_clean_text(m.get('objective'),200)}")
            imp_claims.append(_claim(f"implication-{i}",text,list(m.get("indicator_ids") or []),evidence_refs=m.get("evidence_record_ids") or [],claim_type="recommendation",causal_status="not_proven"))
        slides.append(_slide("strategic_implications","evidence_cards","Strategic implications" if lang=="en" else "Στρατηγικές κατευθύνσεις",claims=imp_claims,priority=96,section="closing",notes={"causality_guardrail":"Association ≠ proven causality."}))
    if analyst.get("findings"):
        analyst_claims=[]
        for i,f in enumerate((analyst.get("findings") or [])[:6],start=1):
            text=f"{_clean_text(f.get('title'),120)} — {_clean_text(f.get('finding'),700)} | {_clean_text(f.get('interpretation'),500)}"
            analyst_claims.append(_claim(f"analyst-{i}",text,list(f.get("indicator_ids") or []),evidence_refs=f.get("evidence_record_ids") or [],
                                         claim_type="analyst_finding",confidence=f.get("confidence"),causal_status=f.get("causal_status") or "not_proven",
                                         source_values={"materiality":f.get("materiality"),"confidence_label":f.get("confidence")}))
        slides.append(_slide("analyst_findings","evidence_cards","Senior analyst synthesis" if lang=="en" else "Σύνθεση senior analyst",claims=analyst_claims,priority=97,section="closing",notes={"causality_guardrail":"Association ≠ proven causality."}))
    if analyst.get("recommendations"):
        rec_claims=[]
        for i,r in enumerate((analyst.get("recommendations") or [])[:5],start=1):
            extra_en=[]; extra_el=[]
            if r.get("timing"): extra_en.append(f"When: {_clean_text(r.get('timing'),120)}"); extra_el.append(f"Πότε: {_clean_text(r.get('timing'),120)}")
            if r.get("stop_condition"): extra_en.append(f"Stop/review if: {_clean_text(r.get('stop_condition'),200)}"); extra_el.append(f"Όριο διακοπής: {_clean_text(r.get('stop_condition'),200)}")
            text=(f"Action: {_clean_text(r.get('action'),450)} | Why: {_clean_text(r.get('rationale'),550)} | Monitor: {_clean_text(r.get('monitor'),350)}" + ("".join(" | "+e for e in extra_en)) if lang=="en" else
                  f"Ενέργεια: {_clean_text(r.get('action'),450)} | Γιατί: {_clean_text(r.get('rationale'),550)} | Παρακολούθηση: {_clean_text(r.get('monitor'),350)}" + ("".join(" | "+e for e in extra_el)))
            rec_claims.append(_claim(f"recommendation-{i}",text,list(r.get("indicator_ids") or []),evidence_refs=r.get("evidence_record_ids") or [],claim_type="recommendation",causal_status="not_proven"))
        slides.append(_slide("recommendations","evidence_cards","Recommended actions" if lang=="en" else "Προτεινόμενες ενέργειες",claims=rec_claims,priority=95,section="closing"))
    if analyst.get("benchmark_summary") and (plan or {}).get("benchmark"):
        slides.append(_slide("benchmark","evidence_cards","Benchmark / comparison" if lang=="en" else "Benchmark / σύγκριση",claims=[_claim("benchmark-1",analyst.get("benchmark_summary"),["brand_reputation","time_trends"],claim_type="benchmark",causal_status="not_proven")],priority=86,section="context"))

    methodology_claims = [
        _claim("method-reputation", "Brand Reputation is a deterministic 0–100 index built from evidence-linked sentiment, independent-voice weight, authenticity, confidence and bounded impact; reach does not create sentiment by itself.", ["brand_reputation","sentiment","effective_independent_voices","authenticity_risk","evidence_confidence","impact_attention"], claim_type="methodology"),
        _claim("method-emotions", "Emotion reporting is based on the dominant classified emotion in organic people evidence; low-signal emotions do not automatically receive a dedicated slide.", ["emotions","origin_breakdown"], claim_type="methodology"),
        _claim("method-visibility", "Owned/promotional and factual not-applicable content can remain visible for dissemination analysis while being excluded from Reputation weighting.", ["origin_breakdown","stance","impact_attention"], claim_type="methodology"),
        _claim("method-causality", "Association ≠ proven causality. Automatic investigations surface evidence-linked associations, deterministic contributions and risk signals only.", ["numeric_anomalies","time_trends","top_mentions"], claim_type="guardrail", causal_status="not_proven"),
    ]
    slides.append(_slide("methodology", "methodology", "Methodology & reading guide" if lang=="en" else "Μεθοδολογία & οδηγός ανάγνωσης", claims=methodology_claims, priority=70, required=True, section="appendix"))

    # Deterministic closing: not generic AI prose. It is assembled from the highest-priority evidence.
    closing_claims = []
    if narrative_chart:
        rows = ((narrative_chart.get("data") or {}).get("rows") or [])
        pos = [r for r in rows if _safe_float(r.get("contribution")) > 0]
        neg = [r for r in rows if _safe_float(r.get("contribution")) < 0]
        if pos:
            r = max(pos, key=lambda x: _safe_float(x.get("contribution")))
            closing_claims.append(_claim("close-positive", (f"Strongest positive driver: {_clean_text(r.get('name'))}" if lang=="en" else f"Ισχυρότερος θετικός παράγοντας: {_clean_text(r.get('name'))}"), ["positive_narrative_drivers"], claim_type="deterministic_rank"))
        if neg:
            r = min(neg, key=lambda x: _safe_float(x.get("contribution")))
            closing_claims.append(_claim("close-negative", (f"Strongest negative driver: {_clean_text(r.get('name'))}" if lang=="en" else f"Ισχυρότερος αρνητικός παράγοντας: {_clean_text(r.get('name'))}"), ["negative_narrative_drivers"], claim_type="deterministic_rank"))
    if inv_candidates:
        top = inv_candidates[0]
        closing_claims.append(_claim("close-watch", ("Watch next: " if lang == "en" else "Παρακολούθηση: ") + _txt(top.get("question"), lang), _resolve_investigation_indicator_ids(top), evidence_refs=top.get("evidence_record_ids") or [], claim_type="attention_point", causal_status="not_proven"))
    heads=(analyst.get("slide_headlines") or {}) if isinstance(analyst, dict) else {}
    for sl in slides:
        h=heads.get(sl.get("slide_id"))
        if h: sl["title"]=_clean_text(h,110)
    if analyst.get("emotion_interpretation"):
        for sl in slides:
            if sl.get("slide_id")=="emotions" and not sl.get("subtitle"):
                sl["subtitle"]=_clean_text(analyst.get("emotion_interpretation"),260)
    conclusions_notes={}
    pr=(analyst.get("priorities") or {}) if isinstance(analyst, dict) else {}
    if any(pr.get(k) for k in ("protect","improve","amplify","monitor")):
        conclusions_notes["priorities"]={k:list(pr.get(k) or [])[:3] for k in ("protect","improve","amplify","monitor")}
    if analyst.get("final_takeaway"):
        conclusions_notes["final_takeaway"]=_clean_text(analyst.get("final_takeaway"),420)
    slides.append(_slide("conclusions", "conclusions", "Conclusions & what to watch" if lang == "en" else "Συμπεράσματα & η επόμενη κίνηση", claims=closing_claims, priority=100, required=True, section="closing", notes=conclusions_notes or None))

    if cancel_check and cancel_check():
        raise PresentationCancelled("Presentation planning cancelled after slide selection")

    # Complete Step 8 indicator disposition contract.
    indicator_review = []
    for item in visual_pack.get("indicator_review") or []:
        indicator_id = str(item.get("indicator_id"))
        linked_slides = [s["slide_id"] for s in slides if indicator_id in {i for c in s.get("chart_ids") or [] for i in (charts.get(c, {}).get("indicator_ids") or [])} or any(indicator_id in (cl.get("indicator_ids") or []) for cl in s.get("claims") or [])]
        if linked_slides:
            status = "included"
            reason = None
        elif item.get("available"):
            status = "reviewed_omitted_low_signal"
            reason = "Reviewed by Step 8; no dedicated or contextual slide justified by the current evidence."
        else:
            status = "reviewed_omitted_insufficient_data"
            reason = "Evidence unavailable or insufficient; no decorative slide generated."
        indicator_review.append({
            "indicator_id": indicator_id,
            "examined": True,
            "step7_visual_status": item.get("visual_status"),
            "presentation_status": status,
            "slide_ids": linked_slides,
            "omission_reason": reason,
        })

    expected = [x[0] for x in INDICATOR_REGISTRY]
    if [x["indicator_id"] for x in indicator_review] != expected or not all(x["examined"] for x in indicator_review):
        raise PresentationValidationError("Step 8 presentation indicator review is incomplete.")

    chart_review = []
    used_chart_ids = {cid for s in slides for cid in s.get("chart_ids") or []}
    for cid in visual_pack.get("presentation_chart_order") or []:
        c = charts.get(cid)
        if not c:
            continue
        chart_review.append({
            "chart_id": cid,
            "examined": True,
            "used": cid in used_chart_ids,
            "slide_ids": [s["slide_id"] for s in slides if cid in (s.get("chart_ids") or [])],
            "omission_reason": None if cid in used_chart_ids else "Reviewed but omitted to avoid low-information or redundant client slides.",
        })

    claim_ledger = [copy.deepcopy(cl) | {"slide_id": s["slide_id"]} for s in slides for cl in s.get("claims") or []]
    _validate_claim_ledger(claim_ledger, expected)
    gold_standard_audit = _gold_standard_audit(slides, charts, inv, investigations)
    missing_caps = [r["capability_id"] for r in gold_standard_audit["capabilities"] if r["applicable"] and r["status"] == "missing"]
    if missing_caps:
        # Documented (never silent) omission: the export still ships, and the gap is
        # recorded prominently in the audit and the plan-level warnings.
        gold_standard_audit["warnings"] = list(gold_standard_audit.get("warnings") or []) + [
            f"Capability without dedicated coverage in this deck: {cap}" for cap in missing_caps
        ]

    return {
        "contract_version": PRESENTATION_CONTRACT_VERSION,
        "ruleset_version": PRESENTATION_RULESET_VERSION,
        "methodology_version": PRESENTATION_METHODOLOGY_VERSION,
        "generated_at": _utcnow(),
        "language": lang,
        "research_context": ctx,
        "slides": slides,
        "indicator_review": indicator_review,
        "chart_review": chart_review,
        "claim_ledger": claim_ledger,
        "gold_standard_audit": gold_standard_audit,
        "warnings": list(dict.fromkeys([_clean_text(x, 500) for x in (visual_pack.get("warnings") or []) if x])),
        "guardrails": {
            "all_25_indicators_reviewed": True,
            "no_invented_metrics": True,
            "material_claims_require_traceability": True,
            "native_editable_powerpoint_objects": True,
            "quality_warnings_preserved": True,
            "association_not_causation": True,
            "raw_evidence_never_modified": True,
        },
    }


def _validate_claim_ledger(claims: list[dict], expected_indicator_ids: list[str] | None = None) -> None:
    expected = set(expected_indicator_ids or [x[0] for x in INDICATOR_REGISTRY])
    seen = set()
    for cl in claims:
        cid = str(cl.get("claim_id") or "")
        if not cid or cid in seen:
            raise PresentationValidationError("Every presentation claim must have a unique claim_id.")
        seen.add(cid)
        ids = cl.get("indicator_ids") or []
        if cl.get("claim_type") not in {"guardrail"} and not ids:
            raise PresentationValidationError(f"Material claim {cid} has no indicator traceability.")
        if any(i not in expected for i in ids):
            raise PresentationValidationError(f"Claim {cid} references an unknown indicator.")
        if cl.get("causal_status") not in {None, "not_applicable", "not_proven", "deterministic_contribution"} and "causal" in str(cl.get("claim_type")):
            raise PresentationValidationError(f"Claim {cid} violates the causality contract.")


# ---------- PowerPoint renderer ----------

def _rgb(hex_str: str) -> RGBColor:
    return RGBColor.from_string(hex_str)


def _set_bg(slide, color: str = BG):
    fill = slide.background.fill
    fill.solid(); fill.fore_color.rgb = _rgb(color)


def _add_text(slide, text, x, y, w, h, *, size=18, color=INK, bold=False, align=PP_ALIGN.LEFT, valign=MSO_ANCHOR.TOP, font=FONT):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame; tf.clear(); tf.word_wrap = True; tf.vertical_anchor = valign
    p = tf.paragraphs[0]; p.alignment = align
    run = p.add_run(); run.text = _clean_text(text, 4000)
    run.font.name = font; run.font.size = Pt(size); run.font.bold = bold; run.font.color.rgb = _rgb(color)
    return box


def _add_rule(slide, x, y, w, color=STONE_DARK, width=1.0):
    line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(0.01))
    line.fill.solid(); line.fill.fore_color.rgb = _rgb(color); line.line.fill.background()
    return line


def _add_header(slide, title, section_no=None):
    if section_no is not None:
        _add_text(slide, f"{section_no:02d}", 0.58, 0.45, 0.5, 0.28, size=9, color=MUTED, bold=True)
    clean = _clean_text(title, 250)
    size = 25 if len(clean) <= 54 else 20 if len(clean) <= 82 else 17
    _add_text(slide, clean, 1.15, 0.30, 11.4, 0.82, size=size, bold=True, valign=MSO_ANCHOR.MIDDLE)
    _add_rule(slide, 0.58, 1.15, 12.15)


def _add_footer(slide, page, context):
    topic = _clean_text(context.get("topic") or "SIGNALYTH", 80)
    _add_text(slide, topic, 0.58, 7.12, 8.8, 0.18, size=7.5, color=MUTED)
    _add_text(slide, str(page), 12.05, 7.12, 0.65, 0.18, size=7.5, color=MUTED, align=PP_ALIGN.RIGHT)


def _add_logo(slide, logo_path: Path, x, y, w):
    if logo_path.exists():
        try:
            return slide.shapes.add_picture(str(logo_path), Inches(x), Inches(y), width=Inches(w))
        except Exception:
            return None
    return None


def _add_kpi_card(slide, label, value, x, y, w, h, *, suffix="", accent=AEGEAN):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.adjustments[0] = 0.06
    shape.fill.solid(); shape.fill.fore_color.rgb = _rgb("FAFAFA"); shape.line.color.rgb = _rgb(STONE_DARK)
    shape.shadow.inherit = False
    label_txt = str(label or "").upper()
    _add_text(slide, label_txt, x+0.2, y+0.17, w-0.4, 0.5, size=9, color=MUTED, bold=True)
    value_size = 36 if w >= 2.4 else 26 if w >= 1.7 else 20
    _add_text(slide, f"{value}{suffix}", x+0.2, y+0.52, w-0.4, h-0.66, size=value_size, color=accent, bold=True, valign=MSO_ANCHOR.MIDDLE)
    return shape


def _native_bar(slide, categories, series, x, y, w, h, *, horizontal=False, diverging=False, percent=False, lang="en", point_colors=None, show_values=True):
    data = ChartData(); data.categories = [_clean_text(_cat_label(c, lang), 60) for c in categories]
    for name, vals in series:
        data.add_series(_clean_text(name, 50), [float(_safe_float(v)) for v in vals])
    chart_type = XL_CHART_TYPE.BAR_CLUSTERED if horizontal else XL_CHART_TYPE.COLUMN_CLUSTERED
    chart = slide.shapes.add_chart(chart_type, Inches(x), Inches(y), Inches(w), Inches(h), data).chart
    chart.has_legend = len(series) > 1
    if chart.has_legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.font.size = Pt(9)
        chart.legend.include_in_layout = False
    chart.has_title = False
    chart.value_axis.has_major_gridlines = True
    chart.value_axis.major_gridlines.format.line.color.rgb = _rgb(STONE)
    chart.value_axis.tick_labels.font.size = Pt(9)
    chart.category_axis.tick_labels.font.size = Pt(10)
    chart.category_axis.tick_labels.font.name = FONT
    chart.value_axis.tick_labels.font.name = FONT
    if percent:
        chart.value_axis.maximum_scale = 100
        chart.value_axis.minimum_scale = 0
    if diverging:
        chart.value_axis.crosses_at = 0
        try:
            from pptx.enum.chart import XL_TICK_LABEL_POSITION
            chart.category_axis.tick_labels.font.size = Pt(9)
            chart.category_axis.tick_label_position = XL_TICK_LABEL_POSITION.LOW
        except Exception:
            pass
    palette = [AEGEAN, "3E6B9E", NEG, POS]
    values0 = [float(_safe_float(v)) for v in series[0][1]] if series else []
    for i, sr in enumerate(chart.series):
        sr.format.fill.solid(); sr.format.fill.fore_color.rgb = _rgb(palette[i % len(palette)])
        sr.format.line.color.rgb = _rgb(WHITE)
    if len(series) == 1:
        sr = chart.series[0]
        try:
            for j, pt in enumerate(sr.points):
                col = None
                if point_colors:
                    col = point_colors[j] if j < len(point_colors) else None
                elif diverging:
                    col = POS if values0[j] >= 0 else NEG
                else:
                    col = _semantic_color(categories[j])
                if col:
                    pt.format.fill.solid(); pt.format.fill.fore_color.rgb = _rgb(col)
        except Exception:
            pass
        if show_values:
            try:
                plot = chart.plots[0]
                plot.has_data_labels = True
                dl = plot.data_labels
                dl.font.size = Pt(9); dl.font.name = FONT; dl.font.color.rgb = _rgb(INK)
                dl.number_format = '0.0'; dl.number_format_is_linked = False
            except Exception:
                pass
        try:
            chart.plots[0].gap_width = 60
        except Exception:
            pass
    return chart


def _native_doughnut(slide, categories, values, x, y, w, h, *, lang="en"):
    data = ChartData(); data.categories = [_clean_text(_cat_label(c, lang), 40) for c in categories]
    data.add_series("share", [float(_safe_float(v)) for v in values])
    chart = slide.shapes.add_chart(XL_CHART_TYPE.DOUGHNUT, Inches(x), Inches(y), Inches(w), Inches(h), data).chart
    chart.has_title = False
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.RIGHT
    chart.legend.font.size = Pt(11); chart.legend.font.name = FONT
    chart.legend.include_in_layout = False
    sr = chart.series[0]
    try:
        for j, pt in enumerate(sr.points):
            col = _semantic_color(categories[j]) or [AEGEAN, "3E6B9E", NEUTRAL, MIXED, NEG, POS][j % 6]
            pt.format.fill.solid(); pt.format.fill.fore_color.rgb = _rgb(col)
            pt.format.line.color.rgb = _rgb(WHITE); pt.format.line.width = Pt(2)
    except Exception:
        pass
    try:
        plot = chart.plots[0]
        plot.has_data_labels = True
        dl = plot.data_labels
        dl.show_value = True
        dl.font.size = Pt(11); dl.font.bold = True; dl.font.name = FONT; dl.font.color.rgb = _rgb(WHITE)
        dl.number_format = '0"%"'; dl.number_format_is_linked = False
    except Exception:
        pass
    return chart


def _native_line(slide, categories, series, x, y, w, h, *, min_y=None, max_y=None):
    data = ChartData(); data.categories = [_clean_text(c, 30) for c in categories]
    for name, vals in series:
        data.add_series(_clean_text(name, 50), [float(_safe_float(v)) for v in vals])
    chart = slide.shapes.add_chart(XL_CHART_TYPE.LINE_MARKERS, Inches(x), Inches(y), Inches(w), Inches(h), data).chart
    chart.has_legend = len(series) > 1
    if chart.has_legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM; chart.legend.font.size = Pt(8)
    chart.value_axis.has_major_gridlines = True; chart.value_axis.major_gridlines.format.line.color.rgb = _rgb(STONE_DARK)
    chart.value_axis.tick_labels.font.size = Pt(8); chart.category_axis.tick_labels.font.size = Pt(8)
    chart.value_axis.tick_labels.font.name = FONT; chart.category_axis.tick_labels.font.name = FONT
    if min_y is not None: chart.value_axis.minimum_scale = min_y
    if max_y is not None: chart.value_axis.maximum_scale = max_y
    palette = [AEGEAN, NEG, POS]
    for i, s in enumerate(chart.series):
        s.format.line.color.rgb = _rgb(palette[i % len(palette)]); s.format.line.width = Pt(2)
        s.marker.format.fill.solid(); s.marker.format.fill.fore_color.rgb = _rgb(palette[i % len(palette)])
    return chart


def _add_editable_table(slide, headers, rows, x, y, w, h, *, col_widths=None, font_size=9):
    rows = list(rows)
    effective_h = min(h, max(0.82, 0.42 * (len(rows) + 1)))
    table_shape = slide.shapes.add_table(len(rows)+1, len(headers), Inches(x), Inches(y), Inches(w), Inches(effective_h))
    table = table_shape.table
    table.rows[0].height = Inches(0.36)
    for i in range(1, len(table.rows)):
        rr = table.rows[i]
        rr.height = Inches(max(0.34, min(0.48, (effective_h-0.36)/max(1,len(rows)))))
    if col_widths:
        total = sum(col_widths)
        for i, cw in enumerate(col_widths):
            table.columns[i].width = Inches(w * cw / total)
    for j, head in enumerate(headers):
        cell = table.cell(0,j); cell.text = _clean_text(head, 80); cell.fill.solid(); cell.fill.fore_color.rgb = _rgb("EFEFEF")
        for p in cell.text_frame.paragraphs:
            for r in p.runs:
                r.font.name=FONT; r.font.size=Pt(font_size); r.font.bold=True; r.font.color.rgb=_rgb(INK)
    for i, row in enumerate(rows, start=1):
        for j, val in enumerate(row):
            cell=table.cell(i,j); cell.text=_clean_text(val, 500); cell.fill.solid(); cell.fill.fore_color.rgb=_rgb(WHITE if i%2 else "F7F7F7")
            for p in cell.text_frame.paragraphs:
                for r in p.runs:
                    r.font.name=FONT; r.font.size=Pt(font_size); r.font.color.rgb=_rgb(INK)
    return table


def _render_chart_spec(slide, chart: dict, x, y, w, h, lang: str):
    typ = chart.get("chart_type"); data = chart.get("data") or {}
    if typ not in {"gauge_kpi", "quality_kpi", "kpi_group"}:
        _add_text(slide, _txt(chart.get("title"), lang), x, y, w, 0.3, size=10.5, color=MUTED, bold=True)
        y += 0.38
        h = max(0.5, h - 0.38)
    if typ == "gauge_kpi":
        val = data.get("value")
        title_txt = _txt(chart.get("title"), lang)
        cid = str(chart.get("chart_id") or "").lower()
        if "confidence" in cid or "βεβαιότητα" in title_txt.lower() or "confidence" in title_txt.lower():
            # §14: confidence is shown as High/Medium/Low with the profile score as
            # secondary context — never as false client-facing precision.
            score = _safe_float(val)
            label = ("High" if score >= 70 else "Medium" if score >= 40 else "Low") if lang == "en" else ("Υψηλή" if score >= 70 else "Μέτρια" if score >= 40 else "Χαμηλή")
            _add_kpi_card(slide, title_txt, label, x, y, w, h, accent=AEGEAN)
            _add_text(slide, ("evidence profile " if lang == "en" else "προφίλ evidence ") + f"{score:.0f}/100", x+0.2, y+h-0.34, w-0.4, 0.26, size=8.5, color=MUTED)
        else:
            _add_kpi_card(slide, title_txt, f"{_safe_float(val):.1f}", x, y, w, h, suffix=" / 100", accent=AEGEAN)
            if "reputation" in cid or "reputation" in title_txt.lower() or "φήμη" in title_txt.lower():
                # §9: Brand Reputation is client-facing only with its scientific scope subtitle.
                _add_text(slide, "Digital conversation signal" if lang == "en" else "Δείκτης ψηφιακής συζήτησης — όχι μέτρηση κοινής γνώμης", x+0.2, y+h-0.34, w-0.4, 0.26, size=8.5, color=MUTED)
        return "editable_shapes"
    if typ == "quality_kpi":
        val = next((data.get(k) for k in ("known_metric_share_percent","high_relevance_share_percent","value") if data.get(k) is not None), None)
        _add_kpi_card(slide, _txt(chart.get("title"), lang), f"{_safe_float(val):.1f}" if val is not None else "—", x, y, w, h, suffix="%" if val is not None else "", accent=AEGEAN)
        return "editable_shapes"
    if typ == "kpi_group":
        items=data.get("items") or []; n=max(1,len(items)); gap=.16
        cols = n if (w - gap*(n-1))/n >= 1.9 else max(1, (n + 1)//2)
        rows_n = (n + cols - 1)//cols
        cw=(w-gap*(cols-1))/cols; ch=(h-gap*(rows_n-1))/rows_n
        for i,item in enumerate(items):
            label=_txt(item.get("label"),lang); val=item.get("value")
            r,c=divmod(i,cols)
            _add_kpi_card(slide,label, f"{_safe_float(val):.0f}" if isinstance(val,(int,float)) else str(val), x+c*(cw+gap),y+r*(ch+gap),cw,ch,accent=AEGEAN)
        return "editable_shapes"
    if typ == "distribution_bar":
        cats=data.get("categories") or []
        labels=[c.get("label") for c in cats]
        values=[c.get("value") for c in cats]
        keys={_cat_key(l) for l in labels}
        if keys and keys.issubset(set(SENTIMENT_COLORS) | {"media","person","brand_owned","organization","unknown"}):
            _native_doughnut(slide,labels,values,x,y,w,h,lang=lang)
        else:
            order=sorted(range(len(labels)),key=lambda i:-_safe_float(values[i]))
            labels=[labels[i] for i in order]; values=[values[i] for i in order]
            _native_bar(slide,labels,[("%",values)],x,y,w,h,horizontal=True,percent=True,lang=lang)
        return "native_chart"
    if typ == "diverging_bar":
        rows=data.get("rows") or []
        _native_bar(slide,[r.get("name") for r in rows[:10]],[("Contribution",[r.get("contribution") for r in rows[:10]])],x,y,w,h,horizontal=True,diverging=True,lang=lang)
        return "native_chart"
    if typ == "grouped_bar":
        rows=data.get("rows") or []
        _native_bar(slide,[r.get("name") for r in rows],[("Record share" if lang=="en" else "Μερίδιο αναφορών",[r.get("record_share") for r in rows]),("Attention share" if lang=="en" else "Μερίδιο προσοχής",[r.get("attention_share") for r in rows])],x,y,w,h,horizontal=False,percent=True,lang=lang)
        return "native_chart"
    if typ == "time_series":
        rows=data.get("rows") or []
        categories=[r.get("date") for r in rows]
        requested=list(data.get("series") or [])
        labels=data.get("series_labels") or {}
        if data.get("scale") == "share_percent" and requested:
            series=[]
            for key in requested[:4]:
                values=[None if r.get(key) is None else 100*_safe_float(r.get(key)) for r in rows]
                series.append((labels.get(key) or key.replace("_opinion_weight_share","").replace("_"," ").title(),values))
            _native_line(slide,categories,series,x,y,w,h,min_y=0,max_y=100)
            return "native_chart"
        # Separate metric families: Brand Reputation is the client-readable headline series.
        if any(r.get("brand_reputation_index") is not None for r in rows):
            _native_line(slide,categories,[("Brand Reputation",[r.get("brand_reputation_index") for r in rows])],x,y,w,h,min_y=0,max_y=100)
            return "native_chart"
    if typ in {"source_matrix","ranking","event_timeline","quality_matrix","risk_panel","cluster_table","evidence_table"}:
        if typ == "source_matrix":
            rows=data.get("rows") or []
            vals=[[r.get("source"),r.get("records"),"—" if r.get("brand_reputation") is None else f"{_safe_float(r.get('brand_reputation')):.1f}",f"{_safe_float(r.get('average_impact')):.2f}"] for r in rows[:8]]
            _add_editable_table(slide,["Source","Records","Reputation","Impact"],vals,x,y,w,h,font_size=8.5)
        elif typ == "ranking":
            rows=data.get("rows") or []
            vals=[[r.get("name"),r.get("records"),f"{_safe_float(r.get('attention')):.2f}","—" if r.get("weighted_sentiment") is None else f"{_safe_float(r.get('weighted_sentiment')):+.2f}"] for r in rows[:8]]
            _add_editable_table(slide,["Name","Records","Attention","Sentiment"],vals,x,y,w,h,font_size=8.5)
        elif typ == "event_timeline":
            ev=data.get("events") or []
            vals=[[e.get("date"),", ".join(e.get("flags") or []),f"{_safe_float(e.get('volume_robust_z')):.1f}"] for e in ev[:8]]
            _add_editable_table(slide,["Date","Signal","Volume z"],vals,x,y,w,h,font_size=8.2)
        elif typ == "quality_matrix":
            items=data.get("items") or []
            friendly={"data_quality":"Data quality","source_coverage":"Source coverage","sample_achievement":"Sample achievement"}
            vals=[[friendly.get(i.get("key"),i.get("key")),"—" if i.get("value") is None else f"{_safe_float(i.get('value')):.1f}",i.get("label") or ""] for i in items]
            _add_editable_table(slide,["Measure","Value","Label"],vals,x,y,w,h,font_size=9)
        elif typ == "risk_panel":
            vals=[["Low authenticity",data.get("low_authenticity_records"),f"{_safe_float(data.get('low_authenticity_share_percent')):.1f}%"],["Coordination clusters",len(data.get("coordination_clusters") or []),""]]
            _add_editable_table(slide,["Signal","Count","Share"],vals,x,y,w,h,font_size=9)
        elif typ == "cluster_table":
            rows=data.get("rows") or []
            vals=[[r.get("cluster_id"),r.get("records"),", ".join(r.get("sources") or []),r.get("representative_excerpt") or ""] for r in rows[:6]]
            _add_editable_table(slide,["Cluster","Records","Sources","Example"],vals,x,y,w,h,col_widths=[1,1,1.3,3],font_size=7.5)
        elif typ == "evidence_table":
            rows=data.get("rows") or []
            vals=[[r.get("platform"),r.get("author"),r.get("date"),r.get("excerpt") or "",f"{_safe_float(r.get('impact_score')):.2f}"] for r in rows[:6]]
            _add_editable_table(slide,["Source","Author","Date","Evidence","Impact"],vals,x,y,w,h,col_widths=[1,1.2,1.1,4.4,0.9],font_size=7.2)
        return "editable_table"
    # Never rasterize unsupported charts: show their structured data as an editable evidence box.
    _add_text(slide, _txt(chart.get("title"),lang), x, y, w, .35, size=11, bold=True)
    _add_text(slide, json.dumps(_json_safe(data), ensure_ascii=False)[:1200], x, y+.45, w, h-.45, size=7.5, color=MUTED)
    return "editable_text_fallback"



def _add_claim_card(slide, text, x, y, w, h, *, label=None, accent=AEGEAN, font_size=11.5):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.adjustments[0] = 0.05
    shape.shadow.inherit = False
    shape.fill.solid(); shape.fill.fore_color.rgb = _rgb("FAFAFA"); shape.line.color.rgb = _rgb(STONE_DARK)
    if label:
        _add_text(slide, label, x+.18, y+.14, w-.36, .24, size=8.5, color=accent, bold=True)
        _add_text(slide, text, x+.18, y+.47, w-.36, h-.58, size=font_size, color=INK)
    else:
        _add_text(slide, text, x+.18, y+.18, w-.36, h-.36, size=font_size, color=INK, valign=MSO_ANCHOR.MIDDLE)
    return shape


CLAIM_TYPE_LABELS = {
    "descriptive": ("FACT", "ΔΕΔΟΜΕΝΟ"), "deterministic_rank": ("FACT", "ΔΕΔΟΜΕΝΟ"),
    "investigation": ("INTERPRETATION", "ΕΡΜΗΝΕΙΑ"), "analyst_finding": ("INTERPRETATION", "ΕΡΜΗΝΕΙΑ"),
    "hypothesis": ("HYPOTHESIS", "ΥΠΟΘΕΣΗ"), "recommendation": ("RECOMMENDATION", "ΣΥΣΤΑΣΗ"),
    "benchmark": ("BENCHMARK", "ΣΥΓΚΡΙΣΗ"),
}

def _claim_type_label(claim_type: str, lang: str) -> str | None:
    pair = CLAIM_TYPE_LABELS.get(str(claim_type or ""))
    if not pair:
        return None
    return pair[1] if lang == "el" else pair[0]


def _role_label(role: str, lang: str) -> str:
    labels = {
        "strength": ("Strength", "Δύναμη"),
        "friction": ("Friction", "Τριβή"),
        "momentum": ("Momentum", "Δυναμική"),
        "watch": ("Watch", "Παρακολούθηση"),
    }
    en, el = labels.get(role, (role.title() if role else "Signal", "Σήμα"))
    return el if lang == "el" else en


def generate_pptx(presentation_plan: dict, visual_pack: dict, output_path: Path, logo_path: Path) -> dict:
    prs = Presentation(); prs.slide_width = Inches(13.333333); prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]
    charts = _chart_map(visual_pack); lang = presentation_plan.get("language") or "en"; ctx = presentation_plan.get("research_context") or {}
    render_audit=[]
    for idx, spec in enumerate(presentation_plan.get("slides") or [], start=1):
        slide=prs.slides.add_slide(blank); _set_bg(slide)
        typ=spec.get("slide_type")
        if typ=="cover":
            _add_logo(slide,logo_path,.72,.7,5.4)
            _add_text(slide,spec.get("title"),.75,3.35,11.8,1.05,size=38,bold=True)
            _add_text(slide,spec.get("subtitle"),.78,4.48,10.8,.45,size=13,color=MUTED)
            _add_rule(slide,.78,5.25,2.05,color=INK,width=2)
            _add_text(slide,"Brand intelligence report" if lang=="en" else "Αναφορά brand intelligence",.78,5.5,5,.4,size=10,color=MUTED,bold=True)
        else:
            _add_header(slide,spec.get("title"),idx-1)
            _add_footer(slide,idx,ctx)
            if spec.get("subtitle"):
                _add_text(slide,spec.get("subtitle"),1.15,1.35,11.3,.72,size=12.5,color=MUTED)
            cids=spec.get("chart_ids") or []
            claims=spec.get("claims") or []
            chart_modes=[]
            if typ == "executive_summary":
                if len(cids) >= 1:
                    chart_modes.append((cids[0],_render_chart_spec(slide,charts[cids[0]],1.12,1.55,5.35,2.15,lang)))
                if len(cids) >= 2:
                    chart_modes.append((cids[1],_render_chart_spec(slide,charts[cids[1]],6.82,1.55,5.35,2.15,lang)))
                for i,cl in enumerate(claims[:3]):
                    _add_claim_card(slide,cl.get("text"),1.12+i*3.72,4.15,3.47,1.62,font_size=10.5)
            elif typ == "evidence_split":
                pos=[c for c in claims if str((c.get("source_values") or {}).get("sentiment"))=="positive"]
                neg=[c for c in claims if str((c.get("source_values") or {}).get("sentiment"))=="negative"]
                _add_text(slide,"Top 5 positive" if lang=="en" else "Top 5 θετικές",1.12,1.5,5.3,.32,size=11.5,color=POS,bold=True)
                _add_text(slide,"Top 5 negative" if lang=="en" else "Top 5 αρνητικές",6.82,1.5,5.3,.32,size=11.5,color=NEG,bold=True)
                def _rank_card(cl, x, yy, accent, rank):
                    txt=str(cl.get("text") or "").replace(" | ","\n")
                    badge = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x), Inches(yy+0.06), Inches(0.3), Inches(0.3))
                    badge.fill.solid(); badge.fill.fore_color.rgb=_rgb(accent); badge.line.fill.background(); badge.shadow.inherit=False
                    _add_text(slide,str(rank),x,yy+0.06,0.3,0.3,size=11,color=WHITE,bold=True,align=PP_ALIGN.CENTER,valign=MSO_ANCHOR.MIDDLE)
                    _add_claim_card(slide,txt,x+0.4,yy,4.95,0.93,font_size=8.6)
                for i,cl in enumerate(pos[:5]): _rank_card(cl,1.12,1.92+i*1.03,POS,i+1)
                for i,cl in enumerate(neg[:5]): _rank_card(cl,6.82,1.92+i*1.03,NEG,i+1)
            elif typ == "voices_table":
                rows=((spec.get("notes") or {}).get("rows") or [])
                headers=["#", "Voice" if lang=="en" else "Φωνή", "Mentions" if lang=="en" else "Αναφορές", "Attention" if lang=="en" else "Προσοχή", "Sentiment"]
                body=[[str(r.get("rank")), str(r.get("name")), str(r.get("records")), f"{r.get('attention'):.2f}", f"{r.get('weighted_sentiment'):+.2f}"] for r in rows]
                _add_editable_table(slide, headers, body, 1.12, 1.6, 11.1, min(5.0, 0.42+0.31*len(body)), font_size=9, col_widths=[0.55, 5.6, 1.55, 1.6, 1.8])
                note=("Attention = observed digital attention share; weighted sentiment in [-1, +1]." if lang=="en" else "Προσοχή = παρατηρούμενο ψηφιακό μερίδιο προσοχής· σταθμισμένο sentiment σε κλίμακα [-1, +1].")
                _add_text(slide, note, 1.12, 6.75, 11.0, .3, size=8.5, color=MUTED)
            elif typ == "narratives" and spec.get("slide_id") == "voices_leaders":
                col_map={"positive":POS,"negative":NEG,"neutral":NEUTRAL,"mixed":MIXED}
                for i,cl in enumerate(claims[:4]):
                    col=i%2; row=i//2
                    x0=1.12+col*5.72; y0=1.7+row*2.45
                    lbl=str((cl.get("source_values") or {}).get("valence") or "")
                    dot = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x0), Inches(y0+0.1), Inches(0.2), Inches(0.2))
                    dot.fill.solid(); dot.fill.fore_color.rgb=_rgb(col_map.get(lbl,NEUTRAL)); dot.line.fill.background(); dot.shadow.inherit=False
                    txt=str(cl.get("text") or "").replace(" | ","\n").replace(": ",":\n",1)
                    _add_claim_card(slide,txt,x0+0.32,y0,5.25,2.2,font_size=10)
            elif typ == "narratives":
                n=min(3,len(claims)); block=(4.95-(n-1)*0.18)/max(1,n)
                val_col={"positive":POS,"negative":NEG,"mixed":MIXED}
                for i,cl in enumerate(claims[:3]):
                    yy=1.6+i*(block+0.18)
                    sv=cl.get("source_values") or {}
                    valence=str(sv.get("valence") or "")
                    quotes=[q for q in (sv.get("quotes") or []) if q.get("excerpt")][:2]
                    qh=0.34*len(quotes)
                    card_h=max(0.8, block-qh-0.06)
                    dot = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(1.12), Inches(yy+0.16), Inches(0.18), Inches(0.18))
                    dot.fill.solid(); dot.fill.fore_color.rgb=_rgb(val_col.get(valence,NEUTRAL)); dot.line.fill.background(); dot.shadow.inherit=False
                    txt=str(cl.get("text") or "").replace(" | ","\n")
                    _add_claim_card(slide,txt,1.48,yy,10.75,card_h,font_size=10.2 if n<=2 else 9.3)
                    # Verbatim voice of the audience: real excerpts only, with source and date.
                    for qi,q in enumerate(quotes):
                        src=" · ".join(str(x) for x in (q.get("platform"),q.get("date")) if x)
                        line=f"“{q.get('excerpt')}” — {src}" if src else f"“{q.get('excerpt')}”"
                        _add_text(slide,line,1.86,yy+card_h+0.04+qi*0.32,10.3,0.3,size=9,color=MUTED)
            elif typ == "evidence_cards" and spec.get("slide_id") == "recommendations":
                n=min(5,len(claims)); ch=(5.0-(n-1)*0.16)/max(1,n)
                for i,cl in enumerate(claims[:5]):
                    yy=1.62+i*(ch+0.16)
                    badge = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(1.12), Inches(yy+ch/2-0.21), Inches(0.42), Inches(0.42))
                    badge.fill.solid(); badge.fill.fore_color.rgb=_rgb(AEGEAN); badge.line.fill.background(); badge.shadow.inherit=False
                    _add_text(slide,str(i+1),1.12,yy+ch/2-0.21,0.42,0.42,size=15,color=WHITE,bold=True,align=PP_ALIGN.CENTER,valign=MSO_ANCHOR.MIDDLE)
                    txt=str(cl.get("text") or "").replace(" | ","\n")
                    _add_claim_card(slide,txt,1.72,yy,10.5,ch,font_size=10.5 if n<=4 else 9.5)
            elif typ == "evidence_cards":
                for i,cl in enumerate(claims[:4]):
                    col=i%2; row=i//2
                    tag=_claim_type_label(cl.get("claim_type"),lang)
                    _add_claim_card(slide,cl.get("text"),1.12+col*5.7,1.72+row*2.35,5.35,1.95,label=tag,accent=AEGEAN,font_size=10.0)
            elif typ == "strategic_synthesis":
                for i,cl in enumerate(claims[:4]):
                    role=str((cl.get("source_values") or {}).get("role") or "")
                    col=i%2; row=i//2
                    accent=POS if role=="strength" else NEG if role=="friction" else AEGEAN if role=="momentum" else WARN
                    _add_claim_card(slide,cl.get("text"),1.12+col*5.7,1.65+row*2.38,5.35,2.0,label=_role_label(role,lang),accent=accent,font_size=10.4)
            elif typ == "methodology":
                if cids:
                    chart_modes.append((cids[0],_render_chart_spec(slide,charts[cids[0]],1.12,1.7,4.15,4.65,lang)))
                x=5.65 if cids else 1.12; w=6.52 if cids else 11.05
                for i,cl in enumerate(claims[:4]):
                    _add_claim_card(slide,cl.get("text"),x,1.55+i*1.28,w,1.08,accent=AEGEAN,font_size=9.2)
            elif len(cids)==1:
                chart_modes.append((cids[0],_render_chart_spec(slide,charts[cids[0]],1.12,2.05,11.1,4.45,lang)))
            elif len(cids)>=2:
                chart_modes.append((cids[0],_render_chart_spec(slide,charts[cids[0]],1.12,2.0,5.35,4.35,lang)))
                chart_modes.append((cids[1],_render_chart_spec(slide,charts[cids[1]],6.82,2.0,5.35,4.35,lang)))
            elif typ == "conclusions" and (spec.get("notes") or {}).get("priorities"):
                prm=(spec.get("notes") or {}).get("priorities") or {}
                quad=[("protect", "Protect" if lang=="en" else "ΠΡΟΣΤΑΤΕΨΕ", POS),
                      ("improve", "Improve" if lang=="en" else "ΔΙΟΡΘΩΣΕ", NEG),
                      ("amplify", "Amplify" if lang=="en" else "ΕΝΙΣΧΥΣΕ", AEGEAN),
                      ("monitor", "Monitor" if lang=="en" else "ΠΑΡΑΚΟΛΟΥΘΗΣΕ", MIXED)]
                has_take=bool((spec.get("notes") or {}).get("final_takeaway"))
                qh=2.18 if has_take else 2.5
                for i,(key,lbl,accent) in enumerate(quad):
                    col=i%2; row=i//2
                    items=[_clean_text(v,150) for v in (prm.get(key) or [])][:3]
                    body="\n".join("• "+v for v in items) if items else ("—")
                    _add_claim_card(slide,body,1.12+col*5.72,1.6+row*(qh+0.18),5.5,qh,label=lbl,accent=accent,font_size=10)
                if has_take:
                    strip = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1.12), Inches(6.18), Inches(11.1), Inches(0.92))
                    strip.adjustments[0]=0.1; strip.shadow.inherit=False
                    strip.fill.solid(); strip.fill.fore_color.rgb=_rgb("EEF4F6"); strip.line.fill.background()
                    prefix="Final takeaway: " if lang=="en" else "Τελικό συμπέρασμα: "
                    _add_text(slide,prefix+_clean_text((spec.get("notes") or {}).get("final_takeaway"),380),1.34,6.28,10.66,.74,size=12,color=INK,bold=True,valign=MSO_ANCHOR.MIDDLE)
            elif claims:
                sub_norm=_clean_text(spec.get("subtitle") or "", 400)
                # The subtitle already states the finding; never repeat it as a card.
                claims=[c for c in claims if _clean_text(c.get("text"),400)!=sub_norm]
                y=1.8
                for cl in claims[:5]:
                    _add_claim_card(slide,cl.get("text"),1.15,y,10.9,.82,font_size=12.5)
                    y+=1.0
            if claims and cids and typ not in {"executive_summary","methodology"}:
                # Plain-language takeaway strip: what this slide means, in one sentence.
                strip = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1.12), Inches(6.42), Inches(11.1), Inches(0.62))
                strip.adjustments[0] = 0.14
                strip.shadow.inherit = False
                strip.fill.solid(); strip.fill.fore_color.rgb = _rgb("EEF4F6"); strip.line.fill.background()
                prefix = "What it means: " if lang == "en" else "Τι σημαίνει: "
                note = prefix + _clean_text(claims[0].get("text"), 220)
                _add_text(slide, note, 1.32, 6.5, 10.7, .48, size=10.5, color=INK, valign=MSO_ANCHOR.MIDDLE)
            if spec.get("notes",{}).get("causality_guardrail"):
                _add_text(slide,"Association ≠ proven causality." if lang=="en" else "Συσχέτιση ≠ αποδεδειγμένη αιτιότητα.",8.3,6.92,4.0,.2,size=7.2,color=WARN,align=PP_ALIGN.RIGHT)
            render_audit.extend({"slide_id":spec.get("slide_id"),"chart_id":cid,"render_mode":mode} for cid,mode in chart_modes)
    output_path.parent.mkdir(parents=True,exist_ok=True); prs.save(str(output_path))
    return {"slides":len(prs.slides),"chart_render_audit":render_audit,"path":str(output_path)}


# ---------- Internal brief ----------

def _docx_set_cell_text(cell, text, *, bold=False, size=9, color=INK):
    cell.text=""; p=cell.paragraphs[0]; r=p.add_run(_clean_text(text,1000)); r.bold=bold; r.font.name=FONT; r.font.size=DocxPt(size); r.font.color.rgb=DocxRGBColor.from_string(color); cell.vertical_alignment=WD_CELL_VERTICAL_ALIGNMENT.CENTER


def generate_internal_docx(presentation_plan: dict, evidence_pack: dict, output_path: Path, logo_path: Path) -> dict:
    lang=presentation_plan.get("language") or "en"; ctx=presentation_plan.get("research_context") or {}
    doc=Document(); sec=doc.sections[0]; sec.top_margin=DocxInches(.65); sec.bottom_margin=DocxInches(.65); sec.left_margin=DocxInches(.75); sec.right_margin=DocxInches(.75)
    styles=doc.styles
    styles["Normal"].font.name=FONT; styles["Normal"].font.size=DocxPt(9.5); styles["Normal"].font.color.rgb=DocxRGBColor.from_string(INK)
    if logo_path.exists():
        try: doc.add_picture(str(logo_path),width=DocxInches(3.1))
        except Exception: pass
    p=doc.add_paragraph(); p.alignment=WD_ALIGN_PARAGRAPH.LEFT
    r=p.add_run(("Internal Intelligence Brief" if lang=="en" else "Εσωτερικό Intelligence Brief")); r.bold=True;r.font.name=FONT;r.font.size=DocxPt(22)
    p=doc.add_paragraph(); r=p.add_run(f"{ctx.get('client','')} · {ctx.get('topic','')} · {ctx.get('market','')} · {ctx.get('date_from','')} — {ctx.get('date_to','')}");r.font.name=FONT;r.font.size=DocxPt(9);r.font.color.rgb=DocxRGBColor.from_string(MUTED)

    def heading(text):
        p=doc.add_paragraph(); r=p.add_run(text); r.bold=True;r.font.name=FONT;r.font.size=DocxPt(14);r.font.color.rgb=DocxRGBColor.from_string(INK); return p
    heading("What matters" if lang=="en" else "Τι έχει σημασία")
    for cl in presentation_plan.get("claim_ledger") or []:
        if cl.get("slide_id") in {"executive_summary","conclusions"}:
            p=doc.add_paragraph(style=None); p.style=doc.styles["Normal"]; p.add_run("• ").bold=True; p.add_run(_clean_text(cl.get("text"),800))

    heading("What to watch / verify" if lang=="en" else "Τι να προσέξουμε / επαληθεύσουμε")
    warnings=list(presentation_plan.get("warnings") or [])
    if "Association ≠ proven causality." not in warnings: warnings.append("Association ≠ proven causality.")
    for w in warnings:
        p=doc.add_paragraph(); p.add_run("• ").bold=True; p.add_run(_clean_text(w,800))

    heading("Automatic investigations" if lang=="en" else "Αυτόματες διερευνήσεις")
    for inv in (evidence_pack.get("investigations") or [])[:10]:
        q=_txt(inv.get("question"),lang); f=_txt(inv.get("finding"),lang); conf=(inv.get("confidence") or {}).get("score")
        p=doc.add_paragraph(); rr=p.add_run(q or "Investigation");rr.bold=True;rr.font.name=FONT
        if f: doc.add_paragraph(f)
        meta=f"Confidence: {conf if conf is not None else '—'} · Causality: {inv.get('causal_status','not_proven')} · Evidence refs: {len(inv.get('evidence_record_ids') or [])}"
        p=doc.add_paragraph(); rr=p.add_run(meta);rr.italic=True;rr.font.size=DocxPt(8);rr.font.color.rgb=DocxRGBColor.from_string(MUTED)

    heading("Presentation review audit" if lang=="en" else "Έλεγχος παρουσίασης")
    rows=presentation_plan.get("indicator_review") or []
    table=doc.add_table(rows=1,cols=3);table.alignment=WD_TABLE_ALIGNMENT.CENTER;table.style="Table Grid"
    for j,h in enumerate(["Indicator","Status","Slide(s)"]): _docx_set_cell_text(table.rows[0].cells[j],h,bold=True,color=WHITE);table.rows[0].cells[j].shading if False else None
    for row in rows:
        cells=table.add_row().cells
        _docx_set_cell_text(cells[0],row.get("indicator_id"),size=8)
        _docx_set_cell_text(cells[1],row.get("presentation_status"),size=8)
        _docx_set_cell_text(cells[2],", ".join(row.get("slide_ids") or []) or "—",size=8)

    heading("PowerPoint gold-standard audit" if lang=="en" else "Gold-standard έλεγχος PowerPoint")
    gold=(presentation_plan.get("gold_standard_audit") or {})
    p=doc.add_paragraph(); rr=p.add_run(f"Capability coverage: {_safe_float(gold.get('coverage_percent')):.1f}%"); rr.bold=True; rr.font.name=FONT
    table=doc.add_table(rows=1,cols=3);table.style="Table Grid";table.alignment=WD_TABLE_ALIGNMENT.CENTER
    for j,h in enumerate(["Capability","Status","Slide(s)"]): _docx_set_cell_text(table.rows[0].cells[j],h,bold=True,size=8)
    for row in gold.get("capabilities") or []:
        cells=table.add_row().cells
        _docx_set_cell_text(cells[0],row.get("label") or row.get("capability_id"),size=7.2)
        _docx_set_cell_text(cells[1],row.get("status"),size=7.2)
        _docx_set_cell_text(cells[2],", ".join(row.get("slide_ids") or []) or "—",size=7.2)

    heading("Claim ledger" if lang=="en" else "Claim ledger")
    table=doc.add_table(rows=1,cols=4);table.style="Table Grid";table.alignment=WD_TABLE_ALIGNMENT.CENTER
    for j,h in enumerate(["Slide","Claim","Indicators","Evidence"]): _docx_set_cell_text(table.rows[0].cells[j],h,bold=True,size=8)
    for cl in (presentation_plan.get("claim_ledger") or [])[:40]:
        cells=table.add_row().cells
        _docx_set_cell_text(cells[0],cl.get("slide_id"),size=7.2)
        _docx_set_cell_text(cells[1],cl.get("text"),size=7.2)
        _docx_set_cell_text(cells[2],", ".join(cl.get("indicator_ids") or []),size=7.2)
        _docx_set_cell_text(cells[3],", ".join(cl.get("evidence_refs") or []) or "—",size=7.2)

    output_path.parent.mkdir(parents=True,exist_ok=True);doc.save(str(output_path));return {"path":str(output_path),"paragraphs":len(doc.paragraphs),"tables":len(doc.tables)}


def _convert_to_pdf(input_path: Path, out_dir: Path) -> Path | None:
    soffice=shutil.which("libreoffice") or shutil.which("soffice")
    if not soffice: return None
    out_dir.mkdir(parents=True,exist_ok=True)
    cmd=[soffice,"--headless","--convert-to","pdf","--outdir",str(out_dir),str(input_path)]
    proc=subprocess.run(cmd,capture_output=True,text=True,timeout=120)
    out=out_dir/(input_path.stem+".pdf")
    return out if proc.returncode==0 and out.exists() and out.stat().st_size>0 else None


def _evidence_rows(evidence_pack: dict) -> list[dict]:
    inv=_inventory_map(evidence_pack); rows=_indicator_value(inv,"top_mentions") or []
    return [_json_safe({k:r.get(k) for k in ("record_id","platform","author","origin_group","content_type","date","url","excerpt","sentiment_label","sentiment_score","emotion","topic","narrative","impact_score","evidence_confidence")}) for r in rows]


def persist_presentation_bundle(folder: Path, result: dict) -> dict:
    store=RunStore(); base=folder/"exports"
    store.write(base/"summary.json",result["summary"])
    store.write(base/"presentation-plan.json",result["presentation_plan"])
    store.write(base/"indicator-review.json",result["presentation_plan"]["indicator_review"])
    store.write(base/"chart-review.json",result["presentation_plan"]["chart_review"])
    store.write(base/"claim-ledger.json",result["presentation_plan"]["claim_ledger"])
    store.write(base/"gold-standard-audit.json",result["presentation_plan"].get("gold_standard_audit") or {})
    store.write(base/"manifest.json",result["manifest"])
    store.write(base/"qa.json",result["qa"])
    return result["summary"]


def build_exports(folder: Path, plan: dict, *, force: bool=False, cancel_check: Callable[[], bool] | None=None) -> dict:
    visual_summary=load_visualization_summary(folder)
    if visual_summary is None: raise RuntimeError("Step 8 requires Step 7 Charts & Dashboard.")
    if visual_summary.get("stale"): raise RuntimeError("Step 7 is stale; rebuild Charts & Dashboard before exports.")
    visual_pack=load_presentation_visual_pack(folder); evidence_pack=load_evidence_pack(folder)
    _validate_inputs(visual_pack,evidence_pack,plan)
    input_hash=_hash_payload({"visual_pack":visual_pack,"evidence_contract":evidence_pack.get("contract_version"),"research_context":evidence_pack.get("research_context"),"report_language":plan.get("report_language"),"search_strategy":plan.get("search_strategy"),"keyword_roles":plan.get("keyword_roles"),"benchmark":plan.get("benchmark"),"master_spec_version":plan.get("master_spec_version")})
    old=RunStore.read(folder/"exports"/"summary.json")
    if old and not force and old.get("input_hash")==input_hash and not old.get("stale"):
        return old
    if cancel_check and cancel_check(): raise PresentationCancelled("Exports cancelled before planning")
    synthesis=build_report_synthesis(folder,plan,visual_pack,evidence_pack)
    visual_pack=copy.deepcopy(visual_pack); visual_pack["analyst_synthesis"]=synthesis
    pplan=build_presentation_plan(visual_pack,evidence_pack,plan,cancel_check=cancel_check)
    base=folder/"exports"; base.mkdir(parents=True,exist_ok=True)
    lang=pplan["language"]; ctx=pplan["research_context"]
    stem=re.sub(r"[^A-Za-z0-9Α-Ωα-ω_-]+","_",f"{ctx.get('client','')}_{ctx.get('topic','')}_{ctx.get('date_from','')}_{ctx.get('date_to','')}").strip("_")[:140] or "SIGNALYTH_Report"
    pptx_path=base/f"{stem}.pptx"; docx_path=base/f"{stem}_Internal.docx"
    logo_path=Path(__file__).resolve().parents[2]/"logo.png"
    pptx_meta=generate_pptx(pplan,visual_pack,pptx_path,logo_path)
    if cancel_check and cancel_check(): raise PresentationCancelled("Exports cancelled after PowerPoint generation")
    docx_meta=generate_internal_docx(pplan,evidence_pack,docx_path,logo_path)
    presentation_pdf=_convert_to_pdf(pptx_path,base)
    internal_pdf=_convert_to_pdf(docx_path,base)

    evidence_rows=_evidence_rows(evidence_pack)
    evidence_json=base/f"{stem}_Evidence.json"; evidence_json.write_text(json.dumps(evidence_rows,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    evidence_csv=base/f"{stem}_Evidence.csv"
    with evidence_csv.open("w",encoding="utf-8-sig",newline="") as f:
        fields=list(evidence_rows[0].keys()) if evidence_rows else ["record_id","platform","author","date","url","excerpt"]
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows([{k:(json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v) for k,v in r.items()} for r in evidence_rows])

    files=[pptx_path,docx_path,evidence_json,evidence_csv]
    if presentation_pdf: files.append(presentation_pdf)
    if internal_pdf: files.append(internal_pdf)
    manifest={"files":[]}
    for p in files:
        manifest["files"].append({"name":p.name,"type":p.suffix.lower().lstrip("."),"bytes":p.stat().st_size,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()})

    # Export QA is structural here; release workflow additionally renders PPTX/DOCX/PDF visually.
    consistency_qa=final_consistency_qa(pplan,evidence_pack,visual_pack,synthesis,plan)
    RunStore().write(folder/"exports"/"final-consistency-qa.json",consistency_qa)
    qa={
        "indicator_review_complete":len(pplan["indicator_review"])==len(INDICATOR_REGISTRY) and all(x.get("examined") for x in pplan["indicator_review"]),
        "claim_ledger_valid":True,
        "native_editable_chart_contract":all((c.get("presentation") or {}).get("native_editable_ready") and not (c.get("presentation") or {}).get("render_as_raster") for c in (visual_pack.get("chart_specs") or [])),
        "pptx_slides":pptx_meta["slides"],
        "pptx_render_modes":pptx_meta["chart_render_audit"],
        "docx_tables":docx_meta["tables"],
        "presentation_pdf_available":bool(presentation_pdf),
        "internal_pdf_available":bool(internal_pdf),
        "evidence_rows":len(evidence_rows),
        "causality_guardrail":True,
        "gold_standard_capability_coverage_percent": _safe_float((pplan.get("gold_standard_audit") or {}).get("coverage_percent")),
        "gold_standard_capability_complete": bool((pplan.get("gold_standard_audit") or {}).get("complete")),
        "analyst_synthesis_provider": synthesis.get("provider"),
        "final_consistency_qa_passed": bool(consistency_qa.get("passed")),
    }
    if not all([qa["indicator_review_complete"],qa["claim_ledger_valid"],qa["native_editable_chart_contract"],qa["pptx_slides"]>=3,qa["gold_standard_capability_complete"],qa["final_consistency_qa_passed"]]):
        raise RuntimeError("Step 8 export QA failed before persistence.")

    summary={
        "ruleset_version":PRESENTATION_RULESET_VERSION,"methodology_version":PRESENTATION_METHODOLOGY_VERSION,"presentation_contract_version":PRESENTATION_CONTRACT_VERSION,
        "generated_at":_utcnow(),"input_hash":input_hash,"research_context":copy.deepcopy(ctx),"language":lang,"slide_count":pptx_meta["slides"],"claim_count":len(pplan["claim_ledger"]),"indicator_contract":{"required":len(INDICATOR_REGISTRY),"examined":len(pplan["indicator_review"]),"complete":qa["indicator_review_complete"]},"gold_standard_capability_coverage_percent":qa["gold_standard_capability_coverage_percent"],"gold_standard_capability_complete":qa["gold_standard_capability_complete"],"files":[x["name"] for x in manifest["files"]],"stale":False,
        "boundary":"Step 8 exports evidence-linked, editable client and internal deliverables. A polished report does not imply complete platform coverage or proven causality."
    }
    result={"summary":summary,"presentation_plan":pplan,"manifest":manifest,"qa":qa}
    persist_presentation_bundle(folder,result)
    return summary


def load_export_summary(folder: Path) -> dict | None:
    summary=RunStore.read(folder/"exports"/"summary.json")
    if not isinstance(summary,dict): return None
    current=load_presentation_visual_pack(folder); evidence=load_evidence_pack(folder); plan=RunStore.read(folder/"plan.json",{}) or {}
    if not isinstance(current,dict) or not isinstance(evidence,dict):
        return {**summary,"stale":True}
    try:
        now_hash=_hash_payload({"visual_pack":current,"evidence_contract":evidence.get("contract_version"),"research_context":evidence.get("research_context"),"report_language":plan.get("report_language"),"search_strategy":plan.get("search_strategy"),"keyword_roles":plan.get("keyword_roles"),"benchmark":plan.get("benchmark"),"master_spec_version":plan.get("master_spec_version")})
    except Exception:
        return {**summary,"stale":True}
    return {**summary,"stale":summary.get("input_hash")!=now_hash}


def load_export_manifest(folder: Path) -> dict | None:
    value=RunStore.read(folder/"exports"/"manifest.json")
    return value if isinstance(value,dict) else None


def allowed_export_file(folder: Path, filename: str) -> Path | None:
    manifest=load_export_manifest(folder) or {}; names={x.get("name") for x in manifest.get("files") or []}
    if filename not in names or Path(filename).name!=filename: return None
    p=folder/"exports"/filename
    return p if p.is_file() else None
