from __future__ import annotations

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
