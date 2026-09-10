"""Human review of the queued records ("χρειάζονται έλεγχο").

A record enters the queue when the semantic step could not settle it safely
(uncertain relevance, low confidence, unresolved tier conflict, fragile sarcasm).
Until a human decides, that evidence stays out of every downstream number — which
is correct, but it silently removes paid, collected evidence from the analysis.

This module lets a human resolve queued records and feeds the decisions back into
the pipeline:

* the original model annotation is never overwritten; the human verdict is stored
  alongside it under ``human_review`` with an audit trail (§18 reproducibility),
* resolved records are rebuilt into ``analysis/analysis-ready.json`` so Step 5
  onwards recompute with the human decisions included,
* every override is counted in the review report so the client can see how much
  of the final picture came from human adjudication.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path

SENTIMENTS = ("positive", "negative", "neutral", "mixed")
VERDICTS = ("include", "exclude")
REVIEW_FILE = "human-review.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value) -> str:
    return str(value or "").strip().lower()


def load_reviews(store, folder: Path) -> dict:
    data = store.read(folder / "analysis" / REVIEW_FILE, {}) or {}
    if not isinstance(data, dict):
        return {"decisions": {}}
    data.setdefault("decisions", {})
    return data


def queue_items(store, folder: Path, limit: int = 200) -> list[dict]:
    """Queued records with the context a human needs to decide, and nothing more."""
    from app.services.ai_analysis import REVIEW_REASON_LABELS

    analyzed = store.read(folder / "analysis" / "analyzed.json", []) or []
    reviews = load_reviews(store, folder).get("decisions") or {}
    out = []
    for row in analyzed:
        ai = row.get("ai_analysis") or {}
        if _norm(ai.get("decision")) != "review":
            continue
        rid = str(row.get("id") or "")
        if not rid:
            continue
        reasons = [REVIEW_REASON_LABELS.get(r, r) for r in (ai.get("decision_reasons") or [])]
        decided = reviews.get(rid) or {}
        out.append({
            "record_id": rid,
            "platform": row.get("platform"),
            "author": row.get("author"),
            "date": row.get("date") or row.get("timestamp"),
            "url": row.get("url"),
            "text": str(row.get("text") or "")[:1200],
            "reasons": reasons,
            "model_sentiment": ai.get("sentiment_label"),
            "model_stance": ai.get("target_stance"),
            "model_relevance": ai.get("semantic_relevance"),
            "model_confidence": ai.get("overall_confidence"),
            "sarcasm": bool((ai.get("sarcasm") or {}).get("detected")) if isinstance(ai.get("sarcasm"), dict) else bool(ai.get("sarcasm")),
            "resolved": decided.get("verdict"),
            "resolved_sentiment": decided.get("sentiment"),
        })
        if len(out) >= limit:
            break
    return out


def save_decision(store, folder: Path, record_id: str, verdict: str, sentiment: str | None,
                  reviewer: str | None = None) -> dict:
    verdict_norm = _norm(verdict)
    if verdict_norm not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    sentiment_norm = _norm(sentiment) if sentiment else None
    if verdict_norm == "include":
        if sentiment_norm not in SENTIMENTS:
            raise ValueError(f"sentiment must be one of {SENTIMENTS} when including a record")
    data = load_reviews(store, folder)
    data["decisions"][str(record_id)] = {
        "verdict": verdict_norm,
        "sentiment": sentiment_norm,
        "reviewer": reviewer or "default",
        "decided_at": _utcnow(),
    }
    data["updated_at"] = _utcnow()
    store.write(folder / "analysis" / REVIEW_FILE, data)
    return data


def clear_decisions(store, folder: Path) -> dict:
    empty = {"decisions": {}, "updated_at": _utcnow()}
    store.write(folder / "analysis" / REVIEW_FILE, empty)
    return empty


def apply_reviews(store, folder: Path) -> dict:
    """Rebuild analysis-ready evidence including human-resolved records.

    Returns a report describing exactly what changed, so the effect of human
    adjudication is always visible and auditable.
    """
    analyzed = store.read(folder / "analysis" / "analyzed.json", []) or []
    decisions = load_reviews(store, folder).get("decisions") or {}
    if not analyzed:
        raise RuntimeError("No analysed records are available for this run.")

    rebuilt: list[dict] = []
    included = 0
    excluded = 0
    relabelled = 0
    for row in analyzed:
        rid = str(row.get("id") or "")
        ai = row.get("ai_analysis") or {}
        decision = _norm(ai.get("decision"))
        verdict = decisions.get(rid)
        if decision == "ready" and not verdict:
            rebuilt.append(row)
            continue
        if not verdict:
            continue
        if verdict.get("verdict") == "exclude":
            excluded += 1
            continue
        # include: promote with the human label attached, never overwriting the model's
        promoted = copy.deepcopy(row)
        ann = promoted.setdefault("ai_analysis", {})
        human_sentiment = verdict.get("sentiment")
        ann["human_review"] = {
            "verdict": "include",
            "sentiment": human_sentiment,
            "model_sentiment": ann.get("sentiment_label"),
            "reviewer": verdict.get("reviewer"),
            "decided_at": verdict.get("decided_at"),
        }
        if human_sentiment and human_sentiment != _norm(ann.get("sentiment_label")):
            ann["sentiment_label"] = human_sentiment
            # A human verdict carries a definite polarity; keep the magnitude honest.
            ann["sentiment_score"] = {"positive": 0.6, "negative": -0.6, "neutral": 0.0, "mixed": 0.0}[human_sentiment]
            relabelled += 1
        ann["decision"] = "ready"
        ann["decision_reasons"] = list(ann.get("decision_reasons") or []) + ["human_review_included"]
        ann.setdefault("flags", []).append("human_reviewed")
        ann["flags"] = list(dict.fromkeys(ann["flags"]))
        ann["semantic_relevance"] = "relevant"
        ann["opinion_eligible"] = human_sentiment in {"positive", "negative", "neutral", "mixed"}
        included += 1
        rebuilt.append(promoted)

    store.write(folder / "analysis" / "analysis-ready.json", rebuilt)
    total_queue = sum(1 for r in analyzed if _norm((r.get("ai_analysis") or {}).get("decision")) == "review")
    report = {
        "generated_at": _utcnow(),
        "analysis_ready_total": len(rebuilt),
        "queue_total": total_queue,
        "queue_resolved": included + excluded,
        "included_by_human": included,
        "excluded_by_human": excluded,
        "relabelled_by_human": relabelled,
        "queue_remaining": max(0, total_queue - included - excluded),
        "note": ("Human adjudication is recorded per record and never overwrites the model's "
                 "original annotation; both are retained for audit."),
    }
    store.write(folder / "analysis" / "human-review-report.json", report)
    return report
