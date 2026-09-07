from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.services.storage import RunStore

REPORT_SYNTHESIS_VERSION="signalyth-analyst-synthesis-v1"
CONFIDENCE=("high","medium","low")
MATERIALITY=("material","directional","weak_signal")


def _utcnow(): return datetime.now(timezone.utc).isoformat()

def _safe_text(v,limit=1000): return re.sub(r"\s+"," ",str(v or "")).strip()[:limit]

def _inventory(evidence_pack): return {str(x.get("indicator_id")):x for x in (evidence_pack.get("indicator_inventory") or []) if x.get("indicator_id")}

def _indicator_values(evidence_pack): return {k:copy.deepcopy(v.get("value")) for k,v in _inventory(evidence_pack).items() if v.get("available")}

def _evidence_ids(evidence_pack):
    inv=_inventory(evidence_pack); rows=(inv.get("top_mentions") or {}).get("value") or []
    return {str(x.get("record_id")) for x in rows if isinstance(x,dict) and x.get("record_id")}

def _lang(plan): return "el" if str(plan.get("report_language"))=="Ελληνικά" else "en"

SCHEMA={
 "type":"object","additionalProperties":False,
 "properties":{
   "executive_summary":{"type":"string"},
   "findings":{"type":"array","maxItems":6,"items":{"type":"object","additionalProperties":False,"properties":{
      "title":{"type":"string"},"finding":{"type":"string"},"interpretation":{"type":"string"},
      "confidence":{"type":"string","enum":list(CONFIDENCE)},"materiality":{"type":"string","enum":list(MATERIALITY)},
      "indicator_ids":{"type":"array","items":{"type":"string"}},"evidence_record_ids":{"type":"array","items":{"type":"string"}},
      "causal_status":{"type":"string","enum":["not_applicable","not_proven","deterministic_contribution"]}},
      "required":["title","finding","interpretation","confidence","materiality","indicator_ids","evidence_record_ids","causal_status"]}},
   "recommendations":{"type":"array","maxItems":5,"items":{"type":"object","additionalProperties":False,"properties":{
      "action":{"type":"string"},"rationale":{"type":"string"},"monitor":{"type":"string"},
      "timing":{"type":["string","null"]},"stop_condition":{"type":["string","null"]},
      "indicator_ids":{"type":"array","items":{"type":"string"}},"evidence_record_ids":{"type":"array","items":{"type":"string"}}},
      "required":["action","rationale","monitor","timing","stop_condition","indicator_ids","evidence_record_ids"]}},
   "limitations":{"type":"array","maxItems":6,"items":{"type":"string"}},
   "benchmark_summary":{"type":["string","null"]},
   "narratives":{"type":"array","maxItems":8,"items":{"type":"object","additionalProperties":False,"properties":{
      "title":{"type":"string"},"theme":{"type":"string"},"idea":{"type":"string"},"brand_meaning":{"type":"string"},
      "origin":{"type":"string","enum":["media","organic_people","owned","mixed","unknown"]},
      "valence":{"type":"string","enum":["positive","negative","mixed"]},
      "importance":{"type":"string"},
      "indicator_ids":{"type":"array","items":{"type":"string"}},"evidence_record_ids":{"type":"array","items":{"type":"string"}}},
      "required":["title","theme","idea","brand_meaning","origin","valence","importance","indicator_ids","evidence_record_ids"]}},
   "strategic_implications":{"type":"array","maxItems":4,"items":{"type":"object","additionalProperties":False,"properties":{
      "insight":{"type":"string"},"implication":{"type":"string"},"action":{"type":"string"},"objective":{"type":"string"},
      "indicator_ids":{"type":"array","items":{"type":"string"}},"evidence_record_ids":{"type":"array","items":{"type":"string"}}},
      "required":["insight","implication","action","objective","indicator_ids","evidence_record_ids"]}},
   "priorities":{"type":"object","additionalProperties":False,"properties":{
      "protect":{"type":"array","maxItems":3,"items":{"type":"string"}},
      "improve":{"type":"array","maxItems":3,"items":{"type":"string"}},
      "amplify":{"type":"array","maxItems":3,"items":{"type":"string"}},
      "monitor":{"type":"array","maxItems":3,"items":{"type":"string"}}},
      "required":["protect","improve","amplify","monitor"]},
   "final_takeaway":{"type":["string","null"]},
   "emotion_interpretation":{"type":["string","null"]},
   "slide_headlines":{"type":"object","additionalProperties":False,"properties":{
      "reputation_sentiment":{"type":["string","null"]},"emotions":{"type":["string","null"]},
      "drivers":{"type":["string","null"]},"evolution":{"type":["string","null"]},
      "sources_audiences":{"type":["string","null"]},"influence":{"type":["string","null"]}},
      "required":["reputation_sentiment","emotions","drivers","evolution","sources_audiences","influence"]}
 },"required":["executive_summary","findings","recommendations","limitations","benchmark_summary",
               "narratives","strategic_implications","priorities","final_takeaway","emotion_interpretation","slide_headlines"]}


def _deterministic_fallback(plan,evidence_pack):
    inv=_indicator_values(evidence_pack); rep=inv.get("brand_reputation") or {}; pos=inv.get("positive_narrative_drivers") or []; neg=inv.get("negative_narrative_drivers") or []
    lang=_lang(plan); idx=rep.get("index")
    summary=(f"Η ανάλυση αποτυπώνει Brand Reputation {idx:.1f}/100 με evidence-linked drivers και ρητές δικλείδες ποιότητας." if lang=="el" and isinstance(idx,(int,float)) else
             f"The analysis reports Brand Reputation {idx:.1f}/100 with evidence-linked drivers and explicit quality guardrails." if isinstance(idx,(int,float)) else
             ("Η διαθέσιμη τεκμηρίωση δεν επαρκεί για ασφαλή συνολικό δείκτη Brand Reputation." if lang=="el" else "Available evidence is insufficient for a safe overall Brand Reputation index."))
    findings=[]
    for label,rows,mat in (("Positive driver",pos,"directional"),("Negative driver",neg,"directional")):
        if isinstance(rows,list) and rows:
            r=rows[0]; name=str(r.get("name") or "driver"); contrib=r.get("reputation_point_contribution")
            text=f"{name}"+(f" ({float(contrib):+.2f} Reputation points)" if isinstance(contrib,(int,float)) else "")
            findings.append({"title":label,"finding":text,"interpretation":"Evidence-linked deterministic contribution; not a standalone causal claim.","confidence":"medium","materiality":mat,"indicator_ids":["positive_narrative_drivers" if label.startswith("Positive") else "negative_narrative_drivers"],"evidence_record_ids":[],"causal_status":"deterministic_contribution"})
    return {"executive_summary":summary,"findings":findings,"recommendations":[],"limitations":list(evidence_pack.get("warnings") or [])[:6],"benchmark_summary":None,
            "narratives":[],"strategic_implications":[],"priorities":{"protect":[],"improve":[],"amplify":[],"monitor":[]},
            "final_takeaway":None,"emotion_interpretation":None,"slide_headlines":{},
            "provider":"deterministic_fallback","model":None}


def _payload(plan,visual_pack,evidence_pack):
    inv=_indicator_values(evidence_pack)
    investigations=[]
    for row in (evidence_pack.get("investigations") or [])[:12]:
        investigations.append({k:copy.deepcopy(row.get(k)) for k in ("investigation_id","type","question","finding","confidence","causal_status","evidence_record_ids","indicator_ids","metrics")})
    return {
      "research_scope":{"client":plan.get("client"),"topic":plan.get("topic"),"market":plan.get("market"),"date_from":plan.get("date_from"),"date_to":plan.get("date_to"),
                        "search_strategy":plan.get("search_strategy"),"keyword_roles":plan.get("keyword_roles"),"target_total":plan.get("target_total"),"benchmark":plan.get("benchmark")},
      "deterministic_indicators":inv,
      "investigations":investigations,
      "quality_warnings":list(visual_pack.get("warnings") or []),
    }


def _validate(result,plan,evidence_pack):
    allowed_ind=set(_inventory(evidence_pack)); allowed_ev=_evidence_ids(evidence_pack)
    out=copy.deepcopy(result)
    clean_find=[]
    for f in out.get("findings") or []:
        f["indicator_ids"]=[x for x in f.get("indicator_ids",[]) if x in allowed_ind]
        f["evidence_record_ids"]=[x for x in f.get("evidence_record_ids",[]) if x in allowed_ev]
        if not f["indicator_ids"]: continue
        if f.get("causal_status") not in {"not_applicable","not_proven","deterministic_contribution"}: f["causal_status"]="not_proven"
        clean_find.append(f)
    out["findings"]=clean_find[:6]
    clean_rec=[]
    for r in out.get("recommendations") or []:
        r["indicator_ids"]=[x for x in r.get("indicator_ids",[]) if x in allowed_ind]
        r["evidence_record_ids"]=[x for x in r.get("evidence_record_ids",[]) if x in allowed_ev]
        if r["indicator_ids"]: clean_rec.append(r)
    out["recommendations"]=clean_rec[:5]
    clean_nar=[]
    for n in out.get("narratives") or []:
        n["indicator_ids"]=[x for x in n.get("indicator_ids",[]) if x in allowed_ind]
        n["evidence_record_ids"]=[x for x in n.get("evidence_record_ids",[]) if x in allowed_ev]
        if n["indicator_ids"]: clean_nar.append(n)
    out["narratives"]=clean_nar[:8]
    clean_imp=[]
    for m in out.get("strategic_implications") or []:
        m["indicator_ids"]=[x for x in m.get("indicator_ids",[]) if x in allowed_ind]
        m["evidence_record_ids"]=[x for x in m.get("evidence_record_ids",[]) if x in allowed_ev]
        if m["indicator_ids"]: clean_imp.append(m)
    out["strategic_implications"]=clean_imp[:4]
    pr=out.get("priorities") or {}
    out["priorities"]={k:[_safe_text(v,220) for v in (pr.get(k) or [])[:3]] for k in ("protect","improve","amplify","monitor")}
    heads=out.get("slide_headlines") or {}
    out["slide_headlines"]={k:_safe_text(v,110) for k,v in heads.items() if isinstance(v,str) and v.strip()}
    if out.get("final_takeaway"): out["final_takeaway"]=_safe_text(out["final_takeaway"],420)
    if out.get("emotion_interpretation"): out["emotion_interpretation"]=_safe_text(out["emotion_interpretation"],420)
    # Benchmark language is forbidden unless the research plan contains a real benchmark payload.
    if not plan.get("benchmark"): out["benchmark_summary"]=None
    return out


def build_report_synthesis(folder:Path,plan:dict,visual_pack:dict,evidence_pack:dict)->dict:
    base=_deterministic_fallback(plan,evidence_pack); payload=_payload(plan,visual_pack,evidence_pack)
    if settings.signalyth_ai_enabled and settings.openai_api_key:
        try:
            from openai import OpenAI
            client=OpenAI(api_key=settings.openai_api_key,max_retries=0,timeout=60.0)
            lang="Greek" if _lang(plan)=="el" else "English"
            response=client.responses.create(model=settings.signalyth_ai_reasoning_model,store=False,
                instructions=(f"You are the senior analyst synthesis layer of SIGNALYTH. Write in {lang}. Use ONLY the supplied deterministic indicators, investigations and evidence references. "
                              "Do not recompute metrics, invent facts, invent benchmarks, or present association as causation. Keep findings distinct from recommendations. Every recommendation must include timing (when to act) and stop_condition (a measurable condition under which the recommendation should be stopped or reviewed); use null only when the evidence truly cannot support one. "
                              "Recommendations must be specific, evidence-linked and operational; if evidence does not justify an action, omit it. Confidence and materiality must reflect evidence strength. "
                              "Do not introduce numerical claims unless that exact number is present in the supplied payload. "
                              "MASTER REPORT RULES: (1) Every material finding follows: what happens -> what lies behind it -> which evidence supports it -> what it means for the brand -> what the client should do; put the human story behind the numbers, grouped into consumer narratives, never generic AI phrasing. "
                              "(2) narratives: 5-8 substantive consumer narratives from the supplied evidence, each assigned to a short thematic group label in `theme` (e.g. trust, price, experience, communication — in the report language) so the report can dedicate one slide per theme; for EVERY narrative cite in evidence_record_ids the 1-3 most representative supplied top-mention record ids, because their verbatim excerpts are shown to the client under the narrative. Do not invent psychological motives; frame unsupported motives as hypotheses. "
                              "(3) strategic_implications: 3-4 items, each insight -> business implication -> specific recommended action -> expected objective, all grounded in supplied indicators/evidence. "
                              "(4) priorities: classify concrete items into protect / improve / amplify / monitor. "
                              "(5) slide_headlines: conclusion-style headlines (not topic labels) for the listed slides, e.g. the style of 'Η εμπιστοσύνη είναι το βασικό σημείο τριβής' — only when the data clearly supports them, else null. "
                              "(6) final_takeaway: one strong closing synthesis answering 'what did we learn and what is the most important next move'. "
                              "(7) emotion_interpretation: which narratives the leading emotions connect to. "
                              "FORBIDDEN: vague filler like 'παρατηρείται μια ενδιαφέρουσα δυναμική', 'σημαντικές ευκαιρίες', 'ενισχύστε την επικοινωνία' without concrete specifics; causal claims from association alone; mixing reach with positive sentiment."),
                input=json.dumps(payload,ensure_ascii=False,default=str),
                text={"format":{"type":"json_schema","name":"signalyth_professional_report_synthesis","schema":SCHEMA,"strict":True}},max_output_tokens=6000)
            decoded=json.loads(response.output_text or "{}"); decoded["provider"]="openai";decoded["model"]=str(getattr(response,"model",settings.signalyth_ai_reasoning_model));decoded["response_id"]=getattr(response,"id",None)
            base=decoded
        except Exception as exc:
            base["provider_error"]=_safe_text(exc,500)
    out=_validate(base,plan,evidence_pack);out.update({"ruleset_version":REPORT_SYNTHESIS_VERSION,"generated_at":_utcnow(),"research_scope":payload["research_scope"]})
    RunStore().write(folder/"exports"/"analyst-synthesis.json",out)
    return out


def final_consistency_qa(presentation_plan:dict,evidence_pack:dict,visual_pack:dict,synthesis:dict,plan:dict)->dict:
    allowed_ind=set(_inventory(evidence_pack));allowed_ev=_evidence_ids(evidence_pack)
    claims=presentation_plan.get("claim_ledger") or []
    unknown_ind=sorted({i for c in claims for i in (c.get("indicator_ids") or []) if i not in allowed_ind})
    analyst_claims=[c for c in claims if str(c.get("claim_id") or "").startswith(("analyst-","recommendation-","benchmark-"))]
    unsupported_evidence=sorted({e for c in analyst_claims for e in (c.get("evidence_refs") or []) if e not in allowed_ev})
    causal_bad=[c.get("claim_id") for c in claims if c.get("causal_status") not in {None,"not_applicable","not_proven","deterministic_contribution"}]
    ctx=presentation_plan.get("research_context") or {}
    date_match=str(ctx.get("date_from"))==str(plan.get("date_from")) and str(ctx.get("date_to"))==str(plan.get("date_to"))
    benchmark_ok=bool(plan.get("benchmark")) or not synthesis.get("benchmark_summary")
    material_ai=[c for c in analyst_claims if c.get("claim_type") in {"analyst_finding","recommendation"}]
    traceable=all(c.get("indicator_ids") and (c.get("evidence_refs") or c.get("claim_type")=="recommendation" or c.get("causal_status")=="deterministic_contribution") for c in material_ai)
    checks={"unknown_indicator_refs":not unknown_ind,"unsupported_evidence_refs":not unsupported_evidence,"causality_contract":not causal_bad,
            "research_dates_match":date_match,"benchmark_not_invented":benchmark_ok,"analyst_claims_traceable":traceable,"gold_standard_complete":bool((presentation_plan.get("gold_standard_audit") or {}).get("complete"))}
    return {"contract":"final-report-consistency-qa-v1","checks":checks,"passed":all(checks.values()),"details":{"unknown_indicators":unknown_ind,"unsupported_evidence":unsupported_evidence,"bad_causal_claims":causal_bad},"generated_at":_utcnow()}
