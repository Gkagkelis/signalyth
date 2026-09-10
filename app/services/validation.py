"""Gold-set validation: measure how well the AI classifier agrees with humans.

Implements the measurable part of Chapter 19 (Validation Roadmap) of the
SIGNALYTH Scientific Methodology Foundation:

* Phase A — blind, stratified sampling. Records are served WITHOUT the model's
  own labels so the annotator cannot be anchored by them (ACL 2025 anchoring
  guardrail, §4 of the methodology).
* Phase B — macro-F1, per-class precision/recall, confusion matrix, and
  separate evaluation slices for Greek / Greeklish-mixed / English, plus
  sarcasm and origin slices.

Human labels are stored per run under ``validation/gold.json`` so a session can
be paused and resumed, and so the same gold set can re-score a future model.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timezone
from pathlib import Path

SENTIMENT_CLASSES = ("positive", "negative", "neutral", "mixed")
RELEVANCE_CLASSES = ("relevant", "not_relevant")
GOLD_FILE = "gold.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _language_slice(record: dict) -> str:
    lang = _norm((record.get("ai_analysis") or {}).get("language"))
    if lang in {"greek", "el"}:
        return "greek"
    if lang in {"greeklish", "mixed"}:
        return "greeklish_mixed"
    if lang in {"english", "en"}:
        return "english"
    return "other"


def _stable_seed(run_id: str) -> int:
    return int(hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8], 16)


def build_sample(records: list[dict], run_id: str, size: int = 60) -> list[dict]:
    """Stratified, deterministic blind sample.

    Stratification is by platform so no single source dominates the gold set,
    and the seed is derived from the run id so re-opening the screen always
    yields the same records (a gold set must be stable to be comparable).
    """
    usable = [r for r in records if str(r.get("text") or "").strip()]
    if not usable:
        return []
    by_platform: dict[str, list[dict]] = {}
    for r in usable:
        by_platform.setdefault(str(r.get("platform") or "unknown"), []).append(r)
    rng = random.Random(_stable_seed(run_id))
    for pool in by_platform.values():
        rng.shuffle(pool)
    # Proportional quota per platform, at least 1 each, capped by availability.
    total = len(usable)
    sample: list[dict] = []
    for platform, pool in sorted(by_platform.items(), key=lambda kv: -len(kv[1])):
        quota = max(1, round(size * len(pool) / total))
        sample.extend(pool[:quota])
    rng.shuffle(sample)
    return sample[:size]


def blind_items(records: list[dict], run_id: str, size: int = 60) -> list[dict]:
    """Sample rows with every model label stripped out."""
    out = []
    for r in build_sample(records, run_id, size):
        out.append({
            "record_id": str(r.get("id") or r.get("record_id") or ""),
            "platform": r.get("platform"),
            "date": r.get("date") or r.get("timestamp"),
            "url": r.get("url"),
            "text": str(r.get("text") or "")[:1200],
        })
    return [x for x in out if x["record_id"]]


def _model_label(record: dict) -> dict:
    ai = record.get("ai_analysis") or {}
    sarcasm = ai.get("sarcasm")
    return {
        "sentiment": _norm(ai.get("sentiment_label")),
        "decision": _norm(ai.get("decision")),
        "stance": _norm(ai.get("target_stance")),
        "language": _language_slice(record),
        "sarcasm": bool(sarcasm.get("detected")) if isinstance(sarcasm, dict) else bool(sarcasm),
        "origin": _norm((record.get("intelligence") or {}).get("origin_group")),
    }


def _prf(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "support": tp + fn}


def score(records: list[dict], gold: dict) -> dict:
    """Compare stored human labels against the model's own labels.

    Returns agreement, macro-F1, per-class precision/recall, a confusion matrix
    and evaluation slices. Only records the human actually labelled are scored.
    """
    by_id = {str(r.get("id") or r.get("record_id") or ""): r for r in records}
    labels = (gold or {}).get("labels") or {}
    pairs = []
    for rid, human in labels.items():
        rec = by_id.get(str(rid))
        if not rec:
            continue
        human_sent = _norm(human.get("sentiment"))
        if human_sent not in SENTIMENT_CLASSES:
            continue
        model = _model_label(rec)
        if model["sentiment"] not in SENTIMENT_CLASSES:
            continue  # model produced no usable label (e.g. still in review)
        pairs.append((rid, human, human_sent, model))

    n = len(pairs)
    result = {
        "generated_at": _utcnow(),
        "labelled": len(labels),
        "scored": n,
        "classes": list(SENTIMENT_CLASSES),
        "note": ("Agreement between independent human labels and the model. It measures "
                 "classifier reliability on this dataset, not market representativeness."),
    }
    if not n:
        result.update({"agreement": None, "macro_f1": None, "confusion": {}, "per_class": {},
                       "slices": {}, "disagreements": []})
        return result

    agree = sum(1 for _, _, h, m in pairs if h == m["sentiment"])
    confusion: dict[str, dict[str, int]] = {a: {b: 0 for b in SENTIMENT_CLASSES} for a in SENTIMENT_CLASSES}
    for _, _, h, m in pairs:
        confusion[h][m["sentiment"]] += 1

    per_class = {}
    for cls in SENTIMENT_CLASSES:
        tp = confusion[cls][cls]
        fn = sum(confusion[cls][b] for b in SENTIMENT_CLASSES if b != cls)
        fp = sum(confusion[a][cls] for a in SENTIMENT_CLASSES if a != cls)
        per_class[cls] = _prf(tp, fp, fn)
    present = [c for c in SENTIMENT_CLASSES if per_class[c]["support"] > 0]
    macro_f1 = round(sum(per_class[c]["f1"] for c in present) / len(present), 4) if present else 0.0

    def _slice(key_fn) -> dict:
        buckets: dict[str, list[bool]] = {}
        for _, _, h, m in pairs:
            buckets.setdefault(key_fn(m), []).append(h == m["sentiment"])
        return {k: {"agreement": round(sum(v) / len(v), 4), "n": len(v)}
                for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1]))}

    # Relevance is scored separately: did the model keep what a human considers
    # relevant, and drop what a human considers noise?
    rel_stats = {"scored": 0}
    rel_hits = 0
    rel_total = 0
    for rid, human, _h, model in pairs:
        human_rel = _norm(human.get("relevance"))
        if human_rel not in RELEVANCE_CLASSES:
            continue
        model_rel = "relevant" if model["decision"] == "ready" else "not_relevant"
        rel_total += 1
        rel_hits += int(human_rel == model_rel)
    if rel_total:
        rel_stats = {"scored": rel_total, "agreement": round(rel_hits / rel_total, 4)}

    disagreements = []
    for rid, human, h, m in pairs:
        if h == m["sentiment"]:
            continue
        rec = by_id[rid]
        disagreements.append({
            "record_id": rid,
            "human": h,
            "model": m["sentiment"],
            "stance": m["stance"],
            "sarcasm": m["sarcasm"],
            "language": m["language"],
            "origin": m["origin"],
            "decision": m["decision"],
            "text": str(rec.get("text") or "")[:280],
        })

    result.update({
        "agreement": round(agree / n, 4),
        "macro_f1": macro_f1,
        "confusion": confusion,
        "per_class": per_class,
        "slices": {
            "language": _slice(lambda m: m["language"]),
            "sarcasm": _slice(lambda m: "sarcasm" if m["sarcasm"] else "plain"),
            "origin": _slice(lambda m: m["origin"] or "unknown"),
        },
        "relevance": rel_stats,
        "disagreements": disagreements[:40],
    })
    return result


def load_gold(store, folder: Path) -> dict:
    data = store.read(folder / "validation" / GOLD_FILE, {}) or {}
    if not isinstance(data, dict):
        return {"labels": {}}
    data.setdefault("labels", {})
    return data


def save_label(store, folder: Path, record_id: str, sentiment: str, relevance: str | None,
               annotator: str | None = None) -> dict:
    sentiment = _norm(sentiment)
    if sentiment not in SENTIMENT_CLASSES:
        raise ValueError(f"sentiment must be one of {SENTIMENT_CLASSES}")
    relevance_norm = _norm(relevance) if relevance else None
    if relevance_norm and relevance_norm not in RELEVANCE_CLASSES:
        raise ValueError(f"relevance must be one of {RELEVANCE_CLASSES}")
    gold = load_gold(store, folder)
    gold["labels"][str(record_id)] = {
        "sentiment": sentiment,
        "relevance": relevance_norm,
        "annotator": annotator or "default",
        "labelled_at": _utcnow(),
    }
    gold["updated_at"] = _utcnow()
    store.write(folder / "validation" / GOLD_FILE, gold)
    return gold


def clear_gold(store, folder: Path) -> dict:
    empty = {"labels": {}, "updated_at": _utcnow()}
    store.write(folder / "validation" / GOLD_FILE, empty)
    return empty
