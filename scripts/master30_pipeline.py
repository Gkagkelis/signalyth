from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write(rel: str, text: str):
    p = ROOT / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text.rstrip()+"\n", encoding="utf-8")


def must_replace(rel: str, old: str, new: str, count: int = 1):
    p=ROOT/rel; text=p.read_text(encoding="utf-8")
    if old not in text: raise RuntimeError(f"Master30 patch anchor not found in {rel}: {old[:140]!r}")
    p.write_text(text.replace(old,new,count),encoding="utf-8")


# ---------------- Collector: shared target, top-up routes, mapping recovery, transparent loss funnel ----------------
must_replace(
    "app/services/collector.py",
    'from app.services.normalizer import normalize_dataset, parse_date\n',
    'from app.services.normalizer import normalize_dataset, normalize_dataset_with_audit, parse_date\nfrom app.services.schema_mapping import recover_mapping\n'
)
must_replace(
    "app/services/collector.py",
    '''        charged = reservation if actual_cost is None else max(0.0, float(actual_cost))
        # The provider receives the same per-call cap. If reported usage exceeds it,
        # fail closed instead of silently consuming budget reserved for later sources.
        if charged > reservation + 1e-6:
            raise RuntimeError("Actor reported cost above the reserved per-call hard cap.")
        if self.spent + charged > self.max_budget + 1e-6:
''',
    '''        charged = reservation if actual_cost is None else max(0.0, float(actual_cost))
        # Reservation is a planning envelope, not a claim that Apify runtime/platform
        # usage must equal the Actor event cap. Enforce the user's GLOBAL hard budget;
        # do not reject a successful run merely because provider-reported total usage
        # is above the smaller reservation used for scheduling.
        if self.spent + charged > self.max_budget + 1e-6:
'''
)
must_replace(
    "app/services/collector.py",
    '''def _source_base_cap(source_plan: dict) -> float:
    return sum(float(sr.get("max_charge_usd", 0) or 0) for sr in source_plan.get("subruns", []))
''',
    '''def _source_base_cap(source_plan: dict) -> float:
    explicit = float(source_plan.get("source_budget_usd", 0) or 0)
    if explicit > 0:
        return explicit
    return sum(float(sr.get("max_charge_usd", 0) or 0) for sr in [*(source_plan.get("subruns", []) or []), *(source_plan.get("topup_subruns", []) or [])])
'''
)
must_replace(
    "app/services/collector.py",
    '''def _normalize_partial(source: str, source_raw: list[dict], desired_target: int, date_from: date, date_to: date):
    data_rows, diagnostic_rows = split_diagnostic_rows(source_raw)
    normalized_before = normalize_dataset(source, data_rows, mapping=output_mapping_for(source))
    missing_date_items = sum(1 for r in normalized_before if not parse_date(r.get("date")))
    in_range_rows = [r for r in normalized_before if in_range(r, date_from, date_to)]
    normalized = in_range_rows[:desired_target]
    metrics = {
        "raw_items": len(source_raw),
        "data_items": len(data_rows),
        "diagnostic_items": len(diagnostic_rows),
        "normalized_before_date_filter": len(normalized_before),
        "missing_date_items": missing_date_items,
        "date_filtered_out": len(normalized_before) - len(in_range_rows),
        "capped_out": max(0, len(in_range_rows) - len(normalized)),
        "collected": len(normalized),
    }
    return normalized, metrics
''',
    '''def _normalize_partial(source: str, source_raw: list[dict], desired_target: int, date_from: date, date_to: date):
    data_rows, provider_diagnostics = split_diagnostic_rows(source_raw)
    configured_mapping = output_mapping_for(source)
    audit = normalize_dataset_with_audit(source, data_rows, mapping=configured_mapping)
    normalized_before = audit["rows"]
    recovery = None
    # Self-heal schema drift only when the known contract produced no usable dated
    # evidence. Raw rows remain untouched and are always retained for audit/recovery.
    if data_rows and (not normalized_before or not any(parse_date(r.get("date")) for r in normalized_before)):
        recovery = recover_mapping(source, data_rows, current=configured_mapping)
        if recovery.get("mapping"):
            recovered_audit = normalize_dataset_with_audit(source, data_rows, mapping=recovery["mapping"])
            if len(recovered_audit["rows"]) >= len(normalized_before):
                audit = recovered_audit; normalized_before = audit["rows"]
    missing_date_items = sum(1 for r in normalized_before if not parse_date(r.get("date")))
    in_range_rows = [r for r in normalized_before if in_range(r, date_from, date_to)]
    # normalize_dataset already de-duplicates stable ids. Preserve over-delivery raw,
    # but cap the analysis candidate pool to the current source target.
    normalized = in_range_rows[:desired_target]
    metrics = {
        "raw_items": len(source_raw),
        "data_items": len(data_rows),
        "content_items": len(normalized_before),
        "metadata_items": int(audit.get("metadata_rows", 0) or 0),
        "diagnostic_items": len(provider_diagnostics) + int(audit.get("diagnostic_rows", 0) or 0),
        "normalized_before_date_filter": len(normalized_before),
        "missing_date_items": missing_date_items,
        "date_filtered_out": len(normalized_before) - len(in_range_rows),
        "unique_in_range": len(in_range_rows),
        "capped_out": max(0, len(in_range_rows) - len(normalized)),
        "collected": len(normalized),
        "topup_needed": max(0, int(desired_target) - len(normalized)),
        "mapping_recovery": recovery,
    }
    return normalized, metrics
'''
)
must_replace(
    "app/services/collector.py",
    '''        subruns = _elastic_subruns(sp, desired_target, source_cap)
        source_status["subruns_total"] = len(subruns)
''',
    '''        primary_subruns = _elastic_subruns(sp, desired_target, min(source_cap, _source_base_cap(sp)))
        # Top-up routes do NOT own quotas. They are dormant routes into the same source
        # target and execute only if the normalized in-range pool is still short.
        topup_subruns = []
        for raw_sr in (sp.get("topup_subruns", []) or []):
            sr = copy.deepcopy(raw_sr)
            old_target = max(1, int(sr.get("target_items", desired_target) or desired_target))
            sr["target_items"] = desired_target
            sr["input"] = _resize_input(source, sr.get("input", {}), old_target, desired_target)
            topup_subruns.append(sr)
        subruns = [*primary_subruns, *topup_subruns]
        source_status["subruns_total"] = len(subruns)
'''
)
must_replace(
    "app/services/collector.py",
    '''                sr_status = source_status["subruns"][sr_idx]
                sr_status.update({"status": "running", "started_at": _utcnow()})
                sync(f"{source}: collecting {sr.get('purpose', 'discovery')}", source, code="collecting_purpose", purpose=sr.get("purpose", "discovery"))

                reservation = guard.reserve(float(sr["max_charge_usd"]))
''',
    '''                sr_status = source_status["subruns"][sr_idx]
                purpose = str(sr.get("purpose", "discovery"))
                if purpose.startswith("topup"):
                    current_norm, current_metrics = _normalize_partial(source, source_raw, desired_target, date_from, date_to)
                    source_status.update(current_metrics)
                    remaining_needed = max(0, desired_target - len(current_norm))
                    if remaining_needed <= 0:
                        sr_status.update({"status":"skipped_target_met","started_at":None,"completed_at":_utcnow(),"error":None})
                        source_status["subruns_completed"] += 1
                        sync(f"{source}: target met; unused top-up route skipped", source, code="topup_skipped_target_met", purpose=purpose)
                        continue
                    old_target = max(1, int(sr.get("target_items", desired_target) or desired_target))
                    sr["target_items"] = remaining_needed
                    sr["input"] = _resize_input(source, sr.get("input", {}), old_target, remaining_needed)
                    sr_status["target_items"] = remaining_needed
                sr_status.update({"status": "running", "started_at": _utcnow()})
                sync(f"{source}: collecting {purpose}", source, code="collecting_purpose", purpose=purpose)

                safe_cap = min(float(sr["max_charge_usd"]), max(0.0, guard.remaining))
                if safe_cap <= 0:
                    sr_status.update({"status":"skipped_budget_safety","completed_at":_utcnow(),"error":"No remaining acquisition budget for this top-up route."})
                    source_status["subruns_completed"] += 1
                    continue
                sr["max_charge_usd"] = safe_cap
                reservation = guard.reserve(safe_cap)
'''
)
must_replace(
    "app/services/collector.py",
    '''    store.write(folder / "normalized-all.json", normalized_all)
    store.write(folder / "rebalancing.json", rebalance_audit)

    target_total = int(plan.get("target_total", 0) or 0)
''',
    '''    store.write(folder / "normalized-all.json", normalized_all)
    store.write(folder / "rebalancing.json", rebalance_audit)
    collection_audit = {
        "contract": "collection-loss-funnel-v1",
        "target_semantics": plan.get("target_semantics", "requested_analyzable_evidence"),
        "search_strategy": plan.get("search_strategy"),
        "sources": {
            name: {k: row.get(k) for k in (
                "base_target","adjusted_target","raw_items","data_items","content_items","metadata_items","diagnostic_items",
                "normalized_before_date_filter","missing_date_items","date_filtered_out","unique_in_range","collected","topup_needed",
                "capped_out","cost_usd","mapping_recovery","status"
            )} for name,row in status.get("sources",{}).items()
        },
    }
    store.write(folder / "collection-audit.json", collection_audit)

    target_total = int(plan.get("target_total", 0) or 0)
'''
)

# ---------------- Cleaning: hard hygiene first; lexical uncertainty goes to semantic gate ----------------
must_replace(
    "app/services/cleaning.py",
    '''    if relevance < RULESET_CONFIG["relevance_exclude_below"]:
        return "excluded", ["low_relevance"]
''',
    '''    if relevance < RULESET_CONFIG["relevance_exclude_below"]:
        # Lexical matching is only a pre-AI signal. It must not destroy viable raw
        # evidence before the semantic model has seen the record.
        reasons.append("lexical_relevance_low_semantic_review_required")
'''
)
must_replace(
    "app/services/cleaning.py",
    '''        "trusted": [r for r in cleaned if r["cleaning"]["decision"] == "trusted"],
        "organic": [r for r in cleaned if r["cleaning"]["decision"] == "trusted" and r["cleaning"]["organic_eligible"]],
''',
    '''        # Historical filename retained for API compatibility: this is now the
        # semantic-candidate pool (trusted + review), while hard hygiene exclusions
        # remain excluded before any paid AI call.
        "trusted": [r for r in cleaned if r["cleaning"]["decision"] in {"trusted", "review"}],
        "organic": [r for r in cleaned if r["cleaning"]["decision"] == "trusted" and r["cleaning"]["organic_eligible"]],
'''
)
must_replace(
    "app/services/cleaning.py",
    '''        "trusted_records": len(trusted),
        "review_records": len(review),
''',
    '''        "trusted_records": len(trusted),
        "semantic_candidate_records": len(trusted) + len(review),
        "review_records": len(review),
'''
)

# Disable old pre-AI X-only adaptive relevance expansion for Master30 plans; semantic refill now happens after AI.
must_replace(
    "app/services/run_manager.py",
    '''            if int(report.get("trusted_sample_shortfall", 0) or 0) > 0 and plan.get("search_strategy_version") == "smart-collection-v2":
''',
    '''            if int(report.get("trusted_sample_shortfall", 0) or 0) > 0 and plan.get("search_strategy_version") == "smart-collection-v2":
'''
)

# ---------------- AI: final analytic role + opinion eligibility after semantic decision ----------------
must_replace(
    "app/services/ai_analysis.py",
    '''        final["opinion_eligible"] = bool((row.get("cleaning") or {}).get("organic_eligible"))

        out = copy.deepcopy(row)
''',
    '''        cleaning = row.get("cleaning") or {}
        final["opinion_eligible"] = bool(
            decision == "ready"
            and final.get("semantic_relevance") == "relevant"
            and cleaning.get("content_class") == "organic"
            and cleaning.get("account_type") == "person_or_creator"
            and cleaning.get("authenticity_status") == "low_risk"
            and not cleaning.get("coordination_cluster_id")
        )
        if decision == "excluded":
            analytic_role = "irrelevant_quarantine"
        elif decision == "review":
            analytic_role = "review"
        elif cleaning.get("content_class") in {"owned", "promotional"}:
            analytic_role = "owned_promotional_visibility"
        elif cleaning.get("origin_class") in {"media", "earned_media"} or row.get("platform") == "news":
            analytic_role = "media_visibility"
        elif cleaning.get("account_type") == "organization":
            analytic_role = "organization_evidence"
        elif cleaning.get("coordination_cluster_id") or cleaning.get("authenticity_status") in {"suspicious", "likely_automated"}:
            analytic_role = "coordination_suspicious"
        elif final["opinion_eligible"]:
            analytic_role = "organic_opinion_reputation"
        else:
            analytic_role = "reputation_evidence" if final.get("target_stance") != "not_applicable" else "visibility_evidence"
        final["analytic_role"] = analytic_role

        out = copy.deepcopy(row)
'''
)

# ---------------- Intelligence: partial metric coverage shrinks impact toward neutral; daily confidence ----------------
must_replace(
    "app/services/intelligence.py",
    '''    if not available:
        impact = 0.50  # Unknown, not zero. Missing public metrics must not be interpreted as no impact.
        confidence = 0.0
    else:
        den = sum(w for _, _, w in available)
        impact = sum(v * w for _, v, w in available) / den
        confidence = sum(w for _, _, w in available) / 1.0
''',
    '''    if not available:
        impact = 0.50  # Unknown, not zero. Missing public metrics must not be interpreted as no impact.
        confidence = 0.0
    else:
        den = sum(w for _, _, w in available)
        measured = sum(v * w for _, v, w in available) / den
        confidence = min(1.0, sum(w for _, _, w in available) / 1.0)
        # Sparse metrics are uncertain, not extreme. Shrink measured impact toward
        # the neutral prior in proportion to actual metric coverage/confidence.
        impact = 0.50 + confidence * (measured - 0.50)
'''
)
must_replace(
    "app/services/intelligence.py",
    '''        point = {
            "date": day,
            "records": len(rows),
            "effective_voices": round(sum(_safe_float((r.get("intelligence") or {}).get("independent_voice_weight"), 0.0) for r in rows), 4),
            "brand_reputation_index": rep.get("index"),
''',
    '''        rep_weights = [_safe_float((r.get("intelligence") or {}).get("reputation_weight"), 0.0) for r in rep_rows]
        effective_sample = _effective_sample_size(rep_weights)
        source_count = len({str(r.get("platform") or "") for r in rows if r.get("platform")})
        daily_confidence = _clamp(0.72 * min(1.0, effective_sample / 20.0) + 0.28 * min(1.0, source_count / 3.0))
        point = {
            "date": day,
            "records": len(rows),
            "effective_voices": round(sum(_safe_float((r.get("intelligence") or {}).get("independent_voice_weight"), 0.0) for r in rows), 4),
            "effective_sample_size": round(effective_sample, 4),
            "source_count": source_count,
            "daily_confidence": round(daily_confidence, 4),
            "confidence_label": "high" if daily_confidence >= 0.75 else ("medium" if daily_confidence >= 0.45 else "low"),
            "brand_reputation_index": rep.get("index"),
'''
)


SEMANTIC_REFILL = r'''from __future__ import annotations

import copy
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Callable

from app.registry import output_mapping_for
from app.services.apify_service import ApifyRunner
from app.services.collector import _resize_input, in_range
from app.services.normalizer import normalize_dataset_with_audit
from app.services.resilience import run_actor_resilient, split_diagnostic_rows
from app.services.schema_mapping import recover_mapping
from app.services.storage import RunStore


def _ready_counts(folder: Path) -> Counter:
    store=RunStore(); rows=store.read(folder/"analysis"/"analysis-ready.json",[]) or []
    return Counter(str(r.get("platform") or "") for r in rows)


def semantic_refill(folder: Path, plan: dict, cancel_check: Callable[[], bool] | None=None, runner=None) -> dict:
    """One bounded post-AI acquisition pass toward FINAL analyzable per-source targets.

    It never loops indefinitely, never exceeds remaining acquisition budget, never
    deletes raw evidence, and reuses only source-specific top-up contracts from the
    approved plan. Cleaning + AI are rerun by RunManager after any new candidates.
    """
    cancel_check=cancel_check or (lambda:False); store=RunStore(); counts=_ready_counts(folder)
    status=store.read(folder/"status.json",{}) or {}; budget=dict(status.get("budget") or {})
    remaining=max(0.0,float(budget.get("remaining_usd",0) or 0)); spent=float(budget.get("spent_usd",0) or 0)
    audit={"contract":"semantic-refill-v1","status":"not_needed","before_ready":dict(counts),"steps":[],"added_normalized":0}
    if remaining <= 0: audit["status"]="budget_exhausted"; store.write(folder/"semantic-refill.json",audit); return audit
    runner=runner or ApifyRunner(); date_from=date.fromisoformat(str(plan["date_from"])); date_to=date.fromisoformat(str(plan["date_to"]))
    any_call=False
    for sp in plan.get("sources",[]) or []:
        if cancel_check(): audit["status"]="cancelled"; break
        source=str(sp.get("source") or ""); target=int(sp.get("target_items",0) or 0); short=max(0,target-int(counts.get(source,0)))
        if short<=0: continue
        routes=sp.get("semantic_topup_subruns") or sp.get("topup_subruns") or []
        if not routes: audit["steps"].append({"source":source,"shortfall":short,"status":"no_verified_topup_route"}); continue
        # One route per source in this bounded refill round; ask for headroom because
        # some newly collected candidates may still be semantically irrelevant.
        sr=copy.deepcopy(routes[0]); old=max(1,int(sr.get("target_items",short) or short)); wanted=min(max(short*2,short),max(target,short))
        sr["target_items"]=wanted; sr["input"]=_resize_input(source,sr.get("input",{}),old,wanted)
        cap=min(remaining,max(0.001,float(sr.get("max_charge_usd",remaining) or remaining)))
        if cap<=0: continue
        any_call=True
        try:
            result=run_actor_resilient(runner,sr["actor_id"],sr["input"],max_items=wanted,max_charge_usd=cap,
                                       rate_per_1000=sp.get("price_per_1000_hint"),max_calls=2)
            charged=min(remaining,max(0.0,float(result.accounted_cost_usd or 0))); remaining-=charged; spent+=charged
            combined=[*result.items,*result.diagnostics]
            raw_path=folder/f"raw-{source}.json"; raw_existing=store.read(raw_path,[]) or []; raw_all=[*raw_existing,*combined]
            store.write(raw_path,raw_all)
            data_rows,_=split_diagnostic_rows(raw_all); mapping=output_mapping_for(source)
            norm_audit=normalize_dataset_with_audit(source,data_rows,mapping=mapping); normalized=norm_audit["rows"]
            if data_rows and (not normalized or not any(r.get("date") for r in normalized)):
                rec=recover_mapping(source,data_rows,current=mapping)
                if rec.get("mapping"): normalized=normalize_dataset_with_audit(source,data_rows,mapping=rec["mapping"])["rows"]
            normalized=[r for r in normalized if in_range(r,date_from,date_to)]
            # Keep enough candidate headroom for semantic filtering; stable ids dedupe.
            dedup={str(r.get("id")):r for r in normalized if r.get("id")}; normalized=list(dedup.values())[:max(target*2,target)]
            before=store.read(folder/f"normalized-{source}.json",[]) or []; before_ids={str(r.get("id")) for r in before if r.get("id")}
            store.write(folder/f"normalized-{source}.json",normalized)
            audit["added_normalized"] += sum(1 for r in normalized if str(r.get("id")) not in before_ids)
            audit["steps"].append({"source":source,"shortfall":short,"requested":wanted,"returned":len(result.items),"normalized":len(normalized),"charged_usd":round(charged,6),"status":result.status})
        except Exception as exc:
            audit["steps"].append({"source":source,"shortfall":short,"status":"failed_safe","error":str(exc)[:500]})
        if remaining<=0: break
    # Rebuild normalized-all from source files after any refill.
    all_rows=[]
    for sp in plan.get("sources",[]) or []: all_rows.extend(store.read(folder/f"normalized-{sp.get('source')}.json",[]) or [])
    all_dedup={str(r.get("id")):r for r in all_rows if r.get("id")}; store.write(folder/"normalized-all.json",list(all_dedup.values()))
    status=store.read(folder/"status.json",{}) or {}; status["budget"]={**(status.get("budget") or {}),"spent_usd":round(spent,6),"remaining_usd":round(remaining,6)}; store.write_status_folder(folder,status)
    audit["status"]="attempted" if any_call else (audit.get("status") or "not_needed"); audit["after_candidate_total"]=len(all_dedup); store.write(folder/"semantic-refill.json",audit)
    return audit
'''
write("app/services/semantic_refill.py", SEMANTIC_REFILL)

# RunManager: semantic refill after first AI pass; exports become automatic final step.
must_replace(
    "app/services/run_manager.py",
    'from app.services.visualizations import VisualizationCancelled, build_visualizations\n',
    'from app.services.visualizations import VisualizationCancelled, build_visualizations\nfrom app.services.semantic_refill import semantic_refill\nfrom app.services.presentation import PresentationCancelled, build_exports\n'
)
must_replace(
    "app/services/run_manager.py",
    '''        self.store.checkpoint_run(run_id)
        status = self.store.read_status(run_id)
        status.update({
            "status": "running",
            "phase": "intelligence",
''',
    '''        # Master30: if semantic relevance leaves a per-source analyzable shortfall,
        # do one bounded source-specific refill pass, then re-clean/re-analyze. OpenAI
        # cache prevents re-paying unchanged records.
        if plan.get("master_spec_version") == "SIGNALYTH-master30-v1":
            try:
                refill = semantic_refill(folder, plan, cancel_check=lambda: self.store.cancel_requested_folder(folder))
                if int(refill.get("added_normalized", 0) or 0) > 0:
                    clean_run(folder, plan=plan, cancel_check=lambda: self.store.cancel_requested_folder(folder))
                    report = analyze_run(folder, plan=plan, cancel_check=lambda: self.store.cancel_requested_folder(folder), force=True)
                status_refill = self.store.read_status(run_id)
                status_refill["semantic_refill"] = {"status": refill.get("status"), "summary": refill, "completed_at": _utcnow()}
                self.store.write_status(run_id, status_refill)
            except Exception as exc:
                # Refill is quality-improving and bounded; a provider failure must not
                # erase a valid first-pass analysis.
                status_refill = self.store.read_status(run_id)
                status_refill["semantic_refill"] = {"status":"failed_safe","error":str(exc),"completed_at":_utcnow()}
                self.store.write_status(run_id, status_refill)

        self.store.checkpoint_run(run_id)
        status = self.store.read_status(run_id)
        status.update({
            "status": "running",
            "phase": "intelligence",
''',
    1
)
# Replace terminal visualizations-ready block with automatic exports.
must_replace(
    "app/services/run_manager.py",
    '''        self.store.checkpoint_run(run_id)
        status = self.store.read_status(run_id)
        status.update({
            "status": terminal_status,
            "phase": "visualizations_ready",
            "completed_at": _utcnow(),
            "visualizations": {
                "status": "succeeded",
                "ruleset_version": visualizations.get("ruleset_version"),
                "methodology_version": visualizations.get("methodology_version"),
                "visual_contract_version": visualizations.get("visual_contract_version"),
                "started_at": status.get("visualizations", {}).get("started_at"),
                "completed_at": _utcnow(),
                "error": None,
                "summary": visualizations,
            },
            "current": {"source": None, "code": "visualizations_completed", "message": "Charts, dashboard and native-editable presentation visual pack are ready"},
        })
        status.setdefault("progress", {})["percent"] = 100
        self.store.write_status(run_id, status)
''',
    '''        self.store.checkpoint_run(run_id)
        status = self.store.read_status(run_id)
        status.update({
            "status": "running",
            "phase": "exports",
            "completed_at": None,
            "visualizations": {
                "status": "succeeded",
                "ruleset_version": visualizations.get("ruleset_version"),
                "methodology_version": visualizations.get("methodology_version"),
                "visual_contract_version": visualizations.get("visual_contract_version"),
                "started_at": status.get("visualizations", {}).get("started_at"),
                "completed_at": _utcnow(),
                "error": None,
                "summary": visualizations,
            },
            "exports": {"status":"running","started_at":_utcnow(),"completed_at":None,"error":None,"summary":None},
            "current": {"source": None, "code": "building_professional_report", "message": "Building evidence-grounded professional report and editable exports"},
        })
        status.setdefault("progress", {})["percent"] = 99
        self.store.write_status(run_id, status)
        try:
            exports = build_exports(folder, plan=plan, cancel_check=lambda: self.store.cancel_requested_folder(folder))
        except PresentationCancelled:
            self._mark_cancelled_after_collection(run_id); return
        except Exception as exc:
            status=self.store.read_status(run_id); status.update({"status":"failed","phase":"exports_failed","completed_at":_utcnow(),
                "fatal_error":f"Professional report/export generation failed safely: {exc}",
                "exports":{**(status.get("exports") or {}),"status":"failed","completed_at":_utcnow(),"error":str(exc)},
                "current":{"source":None,"code":"exports_failed","message":"Report/export QA failed; all evidence and charts were preserved"}})
            status.setdefault("progress",{})["percent"]=100; self.store.write_status(run_id,status); return
        self.store.checkpoint_run(run_id)
        status=self.store.read_status(run_id); status.update({"status":terminal_status,"phase":"completed","completed_at":_utcnow(),
            "exports":{**(status.get("exports") or {}),"status":"succeeded","completed_at":_utcnow(),"error":None,"summary":exports},
            "current":{"source":None,"code":"run_completed_with_report","message":"Analysis, charts and professional report exports are ready"}})
        status.setdefault("progress",{})["percent"]=100; self.store.write_status(run_id,status)
'''
)

print("master30 pipeline patches applied")
