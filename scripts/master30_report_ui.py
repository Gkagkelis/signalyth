from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def write(rel,text):
    p=ROOT/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text.rstrip()+"\n",encoding="utf-8")

def must_replace(rel,old,new,count=1):
    p=ROOT/rel;text=p.read_text(encoding="utf-8")
    if old not in text: raise RuntimeError(f"Master30 patch anchor not found in {rel}: {old[:160]!r}")
    p.write_text(text.replace(old,new,count),encoding="utf-8")

REPORT_SYNTHESIS=r'''from __future__ import annotations

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
      "indicator_ids":{"type":"array","items":{"type":"string"}},"evidence_record_ids":{"type":"array","items":{"type":"string"}}},
      "required":["action","rationale","monitor","indicator_ids","evidence_record_ids"]}},
   "limitations":{"type":"array","maxItems":6,"items":{"type":"string"}},
   "benchmark_summary":{"type":["string","null"]}
 },"required":["executive_summary","findings","recommendations","limitations","benchmark_summary"]}


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
                              "Do not recompute metrics, invent facts, invent benchmarks, or present association as causation. Keep findings distinct from recommendations. "
                              "Recommendations must be specific, evidence-linked and operational; if evidence does not justify an action, omit it. Confidence and materiality must reflect evidence strength. "
                              "Do not introduce numerical claims unless that exact number is present in the supplied payload."),
                input=json.dumps(payload,ensure_ascii=False,default=str),
                text={"format":{"type":"json_schema","name":"signalyth_professional_report_synthesis","schema":SCHEMA,"strict":True}},max_output_tokens=3500)
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
    traceable=all(c.get("indicator_ids") and (c.get("evidence_refs") or c.get("claim_type")=="recommendation") for c in material_ai)
    checks={"unknown_indicator_refs":not unknown_ind,"unsupported_evidence_refs":not unsupported_evidence,"causality_contract":not causal_bad,
            "research_dates_match":date_match,"benchmark_not_invented":benchmark_ok,"analyst_claims_traceable":traceable,"gold_standard_complete":bool((presentation_plan.get("gold_standard_audit") or {}).get("complete"))}
    return {"contract":"final-report-consistency-qa-v1","checks":checks,"passed":all(checks.values()),"details":{"unknown_indicators":unknown_ind,"unsupported_evidence":unsupported_evidence,"bad_causal_claims":causal_bad},"generated_at":_utcnow()}
'''
write("app/services/report_synthesis.py",REPORT_SYNTHESIS)

# Presentation integrates AI synthesis while keeping deterministic numbers locked.
must_replace("app/services/presentation.py",
 'from app.services.visualizations import load_presentation_visual_pack, load_visualization_summary\n',
 'from app.services.visualizations import load_presentation_visual_pack, load_visualization_summary\nfrom app.services.report_synthesis import build_report_synthesis, final_consistency_qa\n')
must_replace("app/services/presentation.py",
 '''    ("conclusions", "Conclusions and what to watch"),
)''',
 '''    ("conclusions", "Conclusions and what to watch"),
    ("analyst_findings", "Senior analyst evidence-grounded synthesis"),
    ("recommendations", "Evidence-linked recommendations and monitoring actions"),
)''')
must_replace("app/services/presentation.py",
 '''        "conclusions": True,
    }
''',
 '''        "conclusions": True,
        "analyst_findings": bool((visual_pack.get("analyst_synthesis") or {}).get("findings")),
        "recommendations": bool((visual_pack.get("analyst_synthesis") or {}).get("recommendations")),
    }
''')
must_replace("app/services/presentation.py",
 '''        "conclusions": {"conclusions"},
    }
''',
 '''        "conclusions": {"conclusions"},
        "analyst_findings": {"analyst_findings"},
        "recommendations": {"recommendations"},
    }
''')
# Add synthesis slides before methodology.
must_replace("app/services/presentation.py",
 '''    methodology_claims = [
''',
 '''    analyst = visual_pack.get("analyst_synthesis") or {}
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
            text=(f"Action: {_clean_text(r.get('action'),450)} | Why: {_clean_text(r.get('rationale'),550)} | Monitor: {_clean_text(r.get('monitor'),350)}" if lang=="en" else
                  f"Ενέργεια: {_clean_text(r.get('action'),450)} | Γιατί: {_clean_text(r.get('rationale'),550)} | Παρακολούθηση: {_clean_text(r.get('monitor'),350)}")
            rec_claims.append(_claim(f"recommendation-{i}",text,list(r.get("indicator_ids") or []),evidence_refs=r.get("evidence_record_ids") or [],claim_type="recommendation",causal_status="not_proven"))
        slides.append(_slide("recommendations","evidence_cards","Recommended actions" if lang=="en" else "Προτεινόμενες ενέργειες",claims=rec_claims,priority=95,section="closing"))
    if analyst.get("benchmark_summary") and (plan or {}).get("benchmark"):
        slides.append(_slide("benchmark","evidence_cards","Benchmark / comparison" if lang=="en" else "Benchmark / σύγκριση",claims=[_claim("benchmark-1",analyst.get("benchmark_summary"),["brand_reputation","time_trends"],claim_type="benchmark",causal_status="not_proven")],priority=86,section="context"))

    methodology_claims = [
''')
# build_exports: build synthesis first, inject into visual pack, persist final QA.
must_replace("app/services/presentation.py",
 '''    pplan=build_presentation_plan(visual_pack,evidence_pack,plan,cancel_check=cancel_check)
''',
 '''    synthesis=build_report_synthesis(folder,plan,visual_pack,evidence_pack)
    visual_pack=copy.deepcopy(visual_pack); visual_pack["analyst_synthesis"]=synthesis
    pplan=build_presentation_plan(visual_pack,evidence_pack,plan,cancel_check=cancel_check)
''')
must_replace("app/services/presentation.py",
 '''    qa={
''',
 '''    consistency_qa=final_consistency_qa(pplan,evidence_pack,visual_pack,synthesis,plan)
    RunStore().write(folder/"exports"/"final-consistency-qa.json",consistency_qa)
    qa={
''')
must_replace("app/services/presentation.py",
 '''        "gold_standard_capability_complete": bool((pplan.get("gold_standard_audit") or {}).get("complete")),
    }
''',
 '''        "gold_standard_capability_complete": bool((pplan.get("gold_standard_audit") or {}).get("complete")),
        "analyst_synthesis_provider": synthesis.get("provider"),
        "final_consistency_qa_passed": bool(consistency_qa.get("passed")),
    }
''')
must_replace("app/services/presentation.py",
 '''    if not all([qa["indicator_review_complete"],qa["claim_ledger_valid"],qa["native_editable_chart_contract"],qa["pptx_slides"]>=3,qa["gold_standard_capability_complete"]]):
''',
 '''    if not all([qa["indicator_review_complete"],qa["claim_ledger_valid"],qa["native_editable_chart_contract"],qa["pptx_slides"]>=3,qa["gold_standard_capability_complete"],qa["final_consistency_qa_passed"]]):
''')
# Ensure cached export hash changes when Master30 scope/benchmark changes.
must_replace("app/services/presentation.py",
 '''    input_hash=_hash_payload({"visual_pack":visual_pack,"evidence_contract":evidence_pack.get("contract_version"),"research_context":evidence_pack.get("research_context"),"report_language":plan.get("report_language")})
''',
 '''    input_hash=_hash_payload({"visual_pack":visual_pack,"evidence_contract":evidence_pack.get("contract_version"),"research_context":evidence_pack.get("research_context"),"report_language":plan.get("report_language"),"search_strategy":plan.get("search_strategy"),"keyword_roles":plan.get("keyword_roles"),"benchmark":plan.get("benchmark"),"master_spec_version":plan.get("master_spec_version")})
''')
# Matching stale hash computation.
must_replace("app/services/presentation.py",
 '''        now_hash=_hash_payload({"visual_pack":current,"evidence_contract":evidence.get("contract_version"),"research_context":evidence.get("research_context"),"report_language":plan.get("report_language")})
''',
 '''        now_hash=_hash_payload({"visual_pack":current,"evidence_contract":evidence.get("contract_version"),"research_context":evidence.get("research_context"),"report_language":plan.get("report_language"),"search_strategy":plan.get("search_strategy"),"keyword_roles":plan.get("keyword_roles"),"benchmark":plan.get("benchmark"),"master_spec_version":plan.get("master_spec_version")})
''')

# Main UI injection: minimal, isolated overlay. Existing giant UI remains untouched.
PANEL=r'''<style>
#master30SearchPanel{position:fixed;right:18px;bottom:18px;z-index:99999;width:min(430px,calc(100vw - 36px));background:#fff;border:1px solid #d8d5cf;border-radius:14px;box-shadow:0 14px 45px rgba(0,0,0,.16);font-family:Inter,Arial,sans-serif;color:#181818}
#master30SearchPanel summary{cursor:pointer;padding:12px 14px;font-weight:700;font-size:13px}#master30SearchPanel .m30body{padding:0 14px 14px;font-size:12px}#master30SearchPanel select,#master30SearchPanel input{width:100%;box-sizing:border-box;margin:5px 0 9px;padding:8px;border:1px solid #cbc7c0;border-radius:8px;background:#fff}#m30preview{max-height:190px;overflow:auto;background:#f7f5f0;padding:8px;border-radius:8px;white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:10px}
</style><details id="master30SearchPanel"><summary>Search Strategy & Query Preview</summary><div class="m30body"><label>Research Scope Mode</label><select id="m30strategy"><option value="balanced_smart">Balanced Smart</option><option value="topic_first">Topic-first</option><option value="context_first">Context-first</option></select><label>Required Context (comma separated)</label><input id="m30required" placeholder="π.χ. ΔΕΘ"><label>Aliases (comma separated)</label><input id="m30aliases" placeholder="π.χ. Tsipras"><label>Watch only (comma separated)</label><input id="m30watch" placeholder="θέματα προς μέτρηση, όχι υποχρεωτικό φίλτρο"><div style="margin:5px 0 6px;color:#67635d">Τα queries είναι routes αναζήτησης, όχι quotas. Ο στόχος παραμένει το τελικό analyzable sample ανά πηγή.</div><div id="m30preview">Το Query Preview θα εμφανιστεί μόλις γίνει planning/run.</div></div></details><script>
(()=>{const originalFetch=window.fetch.bind(window);const split=id=>(document.getElementById(id)?.value||'').split(',').map(x=>x.trim()).filter(Boolean);const rolePayload=()=>{const roles={};split('m30required').forEach(x=>roles[x]='required_context');split('m30aliases').forEach(x=>roles[x]='alias');split('m30watch').forEach(x=>roles[x]='watch');return roles};window.fetch=async function(input,init){let url=typeof input==='string'?input:(input&&input.url)||'';let next=init?{...init}:{};if(next.body&&typeof next.body==='string'&&next.method&&String(next.method).toUpperCase()==='POST'&&(url.includes('/api/plan')||url.match(/\/api\/runs(?:\?|$)/))){try{const body=JSON.parse(next.body);body.search_strategy=document.getElementById('m30strategy')?.value||'balanced_smart';body.keyword_roles={...(body.keyword_roles||{}),...rolePayload()};body.keywords=Array.isArray(body.keywords)?body.keywords:[];for(const x of [...split('m30required'),...split('m30aliases'),...split('m30watch')])if(!body.keywords.includes(x))body.keywords.push(x);next.body=JSON.stringify(body)}catch(e){}}
const resp=await originalFetch(input,next);if(url.includes('/api/plan')&&resp.ok){try{const clone=resp.clone();const data=await clone.json();const q=data.query_preview||{};document.getElementById('m30preview').textContent=Object.entries(q).map(([s,rows])=>s.toUpperCase()+':\n'+(rows||[]).map(r=>`• [${r.route}] ${r.query}`).join('\n')).join('\n\n')||'Δεν δημιουργήθηκαν queries.'}catch(e){}}return resp};})();
</script>'''
# home() reads static template and injects the independent panel before </body>.
must_replace("app/main.py",
 '''@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})
''',
 '''@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    html=(BASE_DIR / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    panel='''+repr(PANEL)+'''
    html=html.replace("</body>",panel+"</body>") if "</body>" in html else html+panel
    return HTMLResponse(html)
''')
# Health exposes the upgraded contract.
must_replace("app/main.py",
 '''        "cloud_storage_configured": cloud_persistence.enabled,
''',
 '''        "cloud_storage_configured": cloud_persistence.enabled,
        "master_spec_version": "SIGNALYTH-master30-v1",
        "final_analyzable_target_semantics": True,
''')

TESTS=r'''from datetime import date

from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.normalizer import normalize_item, classify_source_row
from app.services.intelligence import _impact_components


def draft(**kw):
    base=dict(client="test",topic="Αλέξης Τσίπρας",market="Greece",date_from=date(2026,9,2),date_to=date(2026,9,4),keywords=[],sources=["x","tiktok","instagram","facebook","youtube","news"],sample_mode="perSource",per_source={s:50 for s in ["x","tiktok","instagram","facebook","youtube","news"]},max_budget_usd=2.0,smart_search=True,report_language="Ελληνικά",search_strategy="topic_first")
    base.update(kw);return AnalysisDraft(**base)


def test_topic_only_is_valid_and_facebook_uses_whole_target():
    plan=build_collection_plan(draft())
    fb=next(x for x in plan.sources if x.source=="facebook")
    assert len(fb.subruns)==1 and fb.subruns[0].target_items==50
    assert fb.subruns[0].input["query"]=="Αλέξης Τσίπρας" and fb.subruns[0].input["resultsCount"]==50


def test_x_has_global_target_without_per_query_quota():
    plan=build_collection_plan(draft())
    x=next(x for x in plan.sources if x.source=="x")
    assert x.subruns[0].input["maxItems"]==50
    assert "maxItemsPerTarget" not in x.subruns[0].input


def test_instagram_uses_direct_hashtag_content_route_and_metadata_is_not_evidence():
    plan=build_collection_plan(draft())
    ig=next(x for x in plan.sources if x.source=="instagram")
    assert ig.subruns[0].input.get("directUrls") and ig.subruns[0].input.get("resultsType")=="posts"
    assert "searchType" not in ig.subruns[0].input
    assert classify_source_row("instagram",{"hashtag":"tsipras","postsCount":123,"url":"https://instagram.com/explore/tags/tsipras/"})=="metadata"


def test_tiktok_real_nested_output_normalizes():
    row=normalize_item("tiktok",{"id":"v1","desc":"Τσίπρας στη ΔΕΘ","createTime":1788372000,"webVideoUrl":"https://tiktok/v1","author":{"uniqueId":"u"},"authorStats":{"followerCount":321},"stats":{"playCount":1000,"diggCount":80,"commentCount":9,"shareCount":4}})
    assert row["text"].startswith("Τσίπρας") and row["author"]=="u" and row["views"]==1000 and row["likes"]==80 and row["comments"]==9 and row["shares"]==4
    assert row["metric_availability"]["views_known"] is True and row["date"]


def test_facebook_nested_output_normalizes():
    row=normalize_item("facebook",{"postId":"p1","postText":"κείμενο","timestamp":"2026-09-03T10:00:00Z","url":"https://fb/p1","author":{"name":"Page"},"reactionsCount":55,"commentsCount":7,"sharesCount":3})
    assert row["author"]=="Page" and row["likes"]==55 and row["comments"]==7 and row["shares"]==3


def test_youtube_publish_date_and_channel_name_normalize():
    row=normalize_item("youtube",{"videoId":"y1","title":"Video","publishDate":"Sep 03, 2026","url":"https://youtube/y1","channel":{"name":"Channel"},"viewCount":120})
    assert row["author"]=="Channel" and row["date"].startswith("2026-09-03") and row["views"]==120


def test_missing_metrics_are_unknown_not_zero_impact():
    row=normalize_item("news",{"title":"Article","publishedAt":"2026-09-03T10:00:00Z","link":"https://news/a"})
    assert row["views"]==0 and row["metric_availability"]["views_known"] is False
    impact=_impact_components(row,{"news":{},"__global__":{}})
    assert impact["impact_score"]==0.5 and impact["impact_confidence"]==0.0


def test_context_first_uses_context_as_primary_not_broad_topic():
    d=draft(keywords=["ΔΕΘ"],keyword_roles={"ΔΕΘ":"required_context"},search_strategy="context_first")
    plan=build_collection_plan(d);fb=next(x for x in plan.sources if x.source=="facebook")
    assert fb.subruns[0].input["query"]=="Αλέξης Τσίπρας ΔΕΘ"
'''
write("tests/test_master30_contract.py",TESTS)
print("master30 report/ui patches applied")
