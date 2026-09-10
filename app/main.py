from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import re

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, RUNNING_ON_VERCEL, settings
from app.models import (
    AnalysisDraft, SourceConfigUpdate, ReviewDecision, AIReviewDecision, CredentialUpdate,
    ActorLookupRequest, ActorProbeRequest, ActorMappingUpdate, CommentRouteSmokeRequest,
)
from app.registry import (
    public_registry, update_source, commit_actor_configuration, rollback_source, source_history,
    commit_comment_route_verification, clear_comment_route_verification,
)
from app.services.query_planner import build_collection_plan
from app.services import validation as validation_service
from app.services import review_queue as review_service
from app.services import trends as trends_service
from app.services.run_manager import RunManager, RunStateError
from app.services.cleaning import clean_run, apply_review_decision, load_cleaning_summary, load_review_queue
from app.services.ai_analysis import analyze_run, apply_ai_review_decision, load_analysis_summary, load_analysis_review_queue
from app.services.intelligence import build_intelligence, load_intelligence_summary
from app.services.investigations import build_investigations, load_investigation_summary, load_evidence_pack
from app.services.visualizations import build_visualizations, load_visualization_summary, load_dashboard, load_chart_specs, load_presentation_visual_pack
from app.services.presentation import build_exports, load_export_summary, load_export_manifest, allowed_export_file
from app.services.storage import RunNotFound, RunStore
from app.services.cloud_persistence import CloudPersistenceError, cloud_persistence
from app.services.integrations import (
    IntegrationError, actor_input_compatibility, build_probe_input, clear_actor_candidate,
    get_actor_candidate, infer_output_mapping, integration_status, lookup_actor,
    save_actor_candidate, sanitize_actor_input_types, smoke_test_actor, smoke_test_openai, test_apify_connection,
    test_openai_connection, update_actor_candidate, validate_actor_input, write_server_secrets,
)
from app.services.source_capabilities import source_capabilities, build_comment_smoke_input

store = RunStore()
manager = RunManager(store=store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.recover_interrupted_runs()
    yield
    manager.shutdown(wait=False)


app = FastAPI(title="SIGNALYTH", version="1.8.4", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@app.middleware("http")
async def persist_cloud_run_mutations(request: Request, call_next):
    """Snapshot run files after successful mutating API calls in cloud mode."""
    response = await call_next(request)
    if cloud_persistence.enabled and request.method in {"POST", "PATCH", "DELETE"} and response.status_code < 400:
        match = re.match(r"^/api/runs/([^/]+)", request.url.path)
        if match:
            try:
                store.checkpoint_run(match.group(1))
            except Exception:
                # Live status/plan/control metadata are persisted separately. A later
                # pipeline checkpoint will retry the full archive snapshot.
                pass
    return response


@app.middleware("http")
async def _fresh_ui_after_deploys(request, call_next):
    """A plain reload always fetches the newest UI — hard refresh never required."""
    response = await call_next(request)
    if request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    html=(BASE_DIR / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    panel='<style>\n#master30SearchPanel{position:fixed;right:18px;bottom:18px;z-index:99999;width:min(430px,calc(100vw - 36px));background:#fff;border:1px solid #d8d5cf;border-radius:14px;box-shadow:0 14px 45px rgba(0,0,0,.16);font-family:Inter,Arial,sans-serif;color:#181818}\n#master30SearchPanel summary{cursor:pointer;padding:12px 14px;font-weight:700;font-size:13px}#master30SearchPanel .m30body{padding:0 14px 14px;font-size:12px}#master30SearchPanel select,#master30SearchPanel input{width:100%;box-sizing:border-box;margin:5px 0 9px;padding:8px;border:1px solid #cbc7c0;border-radius:8px;background:#fff}#m30preview{max-height:190px;overflow:auto;background:#f7f5f0;padding:8px;border-radius:8px;white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:10px}\n</style><details id="master30SearchPanel"><summary>Search Strategy & Query Preview</summary><div class="m30body"><label>Research Scope Mode</label><select id="m30strategy"><option value="balanced_smart">Balanced Smart</option><option value="topic_first">Topic-first</option><option value="context_first">Context-first</option></select><label>Required Context (comma separated)</label><input id="m30required" placeholder="π.χ. ΔΕΘ"><label>Aliases (comma separated)</label><input id="m30aliases" placeholder="π.χ. Tsipras"><label>Watch only (comma separated)</label><input id="m30watch" placeholder="θέματα προς μέτρηση, όχι υποχρεωτικό φίλτρο"><div style="margin:5px 0 6px;color:#67635d">Τα queries είναι routes αναζήτησης, όχι quotas. Ο στόχος παραμένει το τελικό analyzable sample ανά πηγή.</div><div id="m30preview">Το Query Preview θα εμφανιστεί μόλις γίνει planning/run.</div></div></details><script>\n(()=>{const originalFetch=window.fetch.bind(window);const split=id=>(document.getElementById(id)?.value||\'\').split(\',\').map(x=>x.trim()).filter(Boolean);const rolePayload=()=>{const roles={};split(\'m30required\').forEach(x=>roles[x]=\'required_context\');split(\'m30aliases\').forEach(x=>roles[x]=\'alias\');split(\'m30watch\').forEach(x=>roles[x]=\'watch\');return roles};window.fetch=async function(input,init){let url=typeof input===\'string\'?input:(input&&input.url)||\'\';let next=init?{...init}:{};if(next.body&&typeof next.body===\'string\'&&next.method&&String(next.method).toUpperCase()===\'POST\'&&(url.includes(\'/api/plan\')||url.match(/\\/api\\/runs(?:\\?|$)/))){try{const body=JSON.parse(next.body);body.search_strategy=document.getElementById(\'m30strategy\')?.value||\'balanced_smart\';body.keyword_roles={...(body.keyword_roles||{}),...rolePayload()};body.keywords=Array.isArray(body.keywords)?body.keywords:[];for(const x of [...split(\'m30required\'),...split(\'m30aliases\'),...split(\'m30watch\')])if(!body.keywords.includes(x))body.keywords.push(x);next.body=JSON.stringify(body)}catch(e){}}\nconst resp=await originalFetch(input,next);if(url.includes(\'/api/plan\')&&resp.ok){try{const clone=resp.clone();const data=await clone.json();const q=data.query_preview||{};document.getElementById(\'m30preview\').textContent=Object.entries(q).map(([s,rows])=>s.toUpperCase()+\':\\n\'+(rows||[]).map(r=>`• [${r.route}] ${r.query}`).join(\'\\n\')).join(\'\\n\\n\')||\'Δεν δημιουργήθηκαν queries.\'}catch(e){}}return resp};})();\n</script>'
    html=html.replace("</body>",panel+"</body>") if "</body>" in html else html+panel
    return HTMLResponse(html)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "app": "SIGNALYTH",
        "version": "1.8.4",
        "apify_connected": bool(settings.apify_token),
        "dry_run": settings.signalyth_dry_run,
        "max_parallel_runs": settings.signalyth_max_parallel_runs,
        "openai_connected": bool(settings.openai_api_key),
        "ai_enabled": settings.signalyth_ai_enabled,
        "ai_bulk_model": settings.signalyth_ai_bulk_model,
        "ai_reasoning_model": settings.signalyth_ai_reasoning_model,
        "running_on_vercel": RUNNING_ON_VERCEL,
        "execution_backend": settings.signalyth_execution_backend,
        "cloud_storage_requested": settings.signalyth_cloud_storage,
        "cloud_storage_configured": cloud_persistence.enabled,
        "master_spec_version": "SIGNALYTH-master30-v1",
        "final_analyzable_target_semantics": True,
    }


@app.get("/api/sources")
def sources():
    return public_registry()


@app.patch("/api/sources/{source}")
def patch_source(source: str, update: SourceConfigUpdate):
    try:
        return update_source(source, update.model_dump(exclude_none=True))
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown source")
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))




def _require_local_secret_setup(request: Request) -> None:
    if RUNNING_ON_VERCEL:
        raise HTTPException(
            status_code=409,
            detail="Cloud credential editing is disabled. Set APIFY_TOKEN and OPENAI_API_KEY in Vercel Environment Variables.",
        )
    if settings.signalyth_allow_remote_secret_setup:
        return
    host = (request.client.host if request.client else "") or ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(status_code=403, detail="Credential setup is localhost-only. Deploy behind authentication + TLS before enabling remote secret setup.")
    origin = request.headers.get("origin")
    host_header = request.headers.get("host")
    if origin and host_header:
        from urllib.parse import urlparse
        parsed = urlparse(origin)
        origin_host = parsed.netloc.casefold()
        if origin_host != host_header.casefold():
            raise HTTPException(status_code=403, detail="Cross-origin credential setup is blocked.")


@app.get("/api/integrations/status")
def integrations_status():
    return integration_status()


@app.post("/api/integrations/credentials")
def save_integration_credentials(update: CredentialUpdate, request: Request):
    _require_local_secret_setup(request)
    try:
        return write_server_secrets(
            apify_token=update.apify_token,
            openai_api_key=update.openai_api_key,
            clear_apify=update.clear_apify,
            clear_openai=update.clear_openai,
            ai_enabled=update.ai_enabled,
            live_collection_enabled=update.live_collection_enabled,
            bulk_model=update.bulk_model,
            reasoning_model=update.reasoning_model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/integrations/apify/credential")
def save_apify_credential(update: CredentialUpdate, request: Request):
    _require_local_secret_setup(request)
    if update.apify_token is None:
        raise HTTPException(status_code=422, detail="Apify token is required.")
    try:
        return write_server_secrets(apify_token=update.apify_token)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/integrations/openai/credential")
def save_openai_credential(update: CredentialUpdate, request: Request):
    _require_local_secret_setup(request)
    if update.openai_api_key is None:
        raise HTTPException(status_code=422, detail="OpenAI API key is required.")
    try:
        return write_server_secrets(openai_api_key=update.openai_api_key, ai_enabled=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/integrations/apify/test")
def test_apify():
    try:
        return test_apify_connection()
    except IntegrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/integrations/openai/test")
def test_openai():
    try:
        return test_openai_connection()
    except IntegrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/integrations/openai/smoke")
def paid_openai_smoke(confirm: bool = Query(default=False)):
    if not confirm:
        raise HTTPException(status_code=422, detail="Explicit confirm=true is required because this test makes a paid model request.")
    try:
        return smoke_test_openai()
    except IntegrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/sources/{source}/actor/lookup")
def actor_lookup(source: str, body: ActorLookupRequest):
    registry = public_registry()
    if source not in registry:
        raise HTTPException(status_code=404, detail="Unknown source")
    try:
        result = lookup_actor(body.actor_ref)
        result["compatibility"] = actor_input_compatibility(source, result.get("input_mapping_suggestion") or {})
        save_actor_candidate(source, result)
        return result
    except (ValueError, IntegrationError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/sources/{source}/actor/candidate")
def actor_candidate(source: str):
    if source not in public_registry():
        raise HTTPException(status_code=404, detail="Unknown source")
    candidate = get_actor_candidate(source)
    if not candidate:
        raise HTTPException(status_code=404, detail="No Actor candidate")
    # Candidate data contains no provider secret. Sample output is intentionally retained only server-side.
    safe = dict(candidate)
    smoke = safe.get("smoke_test")
    if isinstance(smoke, dict):
        smoke = {k: v for k, v in smoke.items() if k != "sample_items"}
        safe["smoke_test"] = smoke
    return safe


@app.post("/api/sources/{source}/actor/probe")
def actor_probe(source: str, body: ActorProbeRequest):
    if source not in public_registry():
        raise HTTPException(status_code=404, detail="Unknown source")
    candidate = get_actor_candidate(source)
    if not candidate:
        raise HTTPException(status_code=409, detail="Find the Actor first.")
    actor_id = (candidate.get("actor") or {}).get("actor_id")
    if not actor_id:
        raise HTTPException(status_code=409, detail="Candidate Actor metadata is incomplete.")
    schema = candidate.get("input_schema") or {}
    context = {
        "query": body.query,
        "topic": body.query,
        "date_from": body.date_from.isoformat() if body.date_from else None,
        "date_to": body.date_to.isoformat() if body.date_to else None,
        "country": body.country,
        "language": body.language,
        "comments": body.comments,
        "max_items": body.max_items,
        "urls": ["https://www.instagram.com/explore/tags/signalyth/"] if source == "instagram" else [],
    }
    auto_input, _unresolved, input_mapping = build_probe_input(schema, context)
    probe_input = dict(candidate.get("example_input") or {})
    probe_input.update(auto_input)
    probe_input.update(body.input_overrides or {})
    probe_input, type_mismatches = sanitize_actor_input_types(schema, probe_input)
    required = set((schema or {}).get("required") or [])
    unresolved = sorted({name for name in required if probe_input.get(name) in (None, "", [])} | (required & set(type_mismatches)))
    compatibility = actor_input_compatibility(source, input_mapping)
    if unresolved:
        update_actor_candidate(source, {
            "probe_input": probe_input,
            "unresolved_required": unresolved,
            "input_mapping": input_mapping,
        })
        return {
            "ok": False,
            "requires_input": True,
            "unresolved_required": unresolved,
            "probe_input": probe_input,
            "input_mapping": input_mapping,
            "input_type_mismatches": type_mismatches,
            "compatibility": compatibility,
            "paid_smoke_test_run": False,
        }
    try:
        validation = validate_actor_input(actor_id, probe_input)
        changes = {
            "probe_input": probe_input,
            "unresolved_required": [],
            "input_mapping": input_mapping,
            "validated_at": validation.get("validated_at"),
        }
        result = {
            "ok": True,
            "validation": validation,
            "probe_input": probe_input,
            "input_mapping": input_mapping,
            "compatibility": compatibility,
            "paid_smoke_test_run": False,
        }
        if body.run_paid_smoke_test:
            smoke = smoke_test_actor(
                actor_id,
                probe_input,
                max_items=body.max_items,
                max_charge_usd=body.max_charge_usd,
            )
            inferred = smoke.get("output_mapping") or infer_output_mapping(smoke.get("sample_items") or [])
            changes.update({
                "smoke_test": smoke,
                "output_mapping": inferred.get("mapping") or {},
                "output_mapping_confidence": inferred.get("confidence"),
                "available_output_paths": inferred.get("available_paths") or [],
            })
            result.update({
                "paid_smoke_test_run": True,
                "smoke_test": {k: v for k, v in smoke.items() if k != "sample_items"},
                "output_mapping": inferred,
            })
        update_actor_candidate(source, changes)
        return result
    except (ValueError, IntegrationError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.patch("/api/sources/{source}/actor/mapping")
def actor_mapping(source: str, body: ActorMappingUpdate):
    candidate = get_actor_candidate(source)
    if not candidate:
        raise HTTPException(status_code=404, detail="No Actor candidate")
    available = set(candidate.get("available_output_paths") or [])
    invalid = [path for path in body.output_mapping.values() if path not in available]
    if invalid:
        raise HTTPException(status_code=422, detail="Mapping contains fields not present in the smoke-test sample: " + ", ".join(invalid))
    update_actor_candidate(source, {"output_mapping": body.output_mapping})
    return {"ok": True, "output_mapping": body.output_mapping}


@app.post("/api/sources/{source}/actor/commit")
def actor_commit(source: str):
    registry = public_registry()
    if source not in registry:
        raise HTTPException(status_code=404, detail="Unknown source")
    candidate = get_actor_candidate(source)
    if not candidate:
        raise HTTPException(status_code=409, detail="Find and test an Actor first.")
    smoke = candidate.get("smoke_test") or {}
    if not smoke.get("ok"):
        raise HTTPException(status_code=409, detail="A successful paid 1–10 item smoke test is required before locking a new Actor.")
    compatibility = actor_input_compatibility(source, candidate.get("input_mapping") or {})
    if not compatibility.get("compatible"):
        raise HTTPException(status_code=409, detail="Actor input is not compatible with SIGNALYTH discovery for this source.")
    summary_fields = {}
    for field in (candidate.get("input_summary") or {}).get("fields") or []:
        if isinstance(field, dict) and field.get("name"):
            summary_fields[field["name"]] = {"type": field.get("type"), "required": field.get("required", False)}
    try:
        saved = commit_actor_configuration(
            source,
            actor_id=(candidate.get("actor") or {}).get("actor_id"),
            actor_metadata=candidate.get("actor") or {},
            input_mapping=candidate.get("input_mapping") or {},
            input_schema_fields=summary_fields,
            input_template=candidate.get("probe_input") or {},
            output_mapping=candidate.get("output_mapping") or {},
            mapping_confidence=candidate.get("output_mapping_confidence"),
            verified_at=candidate.get("validated_at") or candidate.get("looked_up_at"),
            smoke_tested_at=smoke.get("tested_at"),
        )
        clear_actor_candidate(source)
        return saved
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/sources/{source}/comments/smoke")
def comment_route_smoke(source: str, body: CommentRouteSmokeRequest):
    registry = public_registry()
    if source not in registry:
        raise HTTPException(status_code=404, detail="Unknown source")
    if not body.confirm_paid_smoke_test:
        raise HTTPException(status_code=422, detail="Explicit confirm_paid_smoke_test=true is required because this makes a paid Actor request.")

    cap = (source_capabilities(source).get("comment_deepening") or {})
    if cap.get("mode") == "not_applicable":
        raise HTTPException(status_code=409, detail=f"Comment deepening is not applicable to {source}.")
    actor_id = str(body.actor_ref or registry[source].get("comment_actor_id") or cap.get("candidate_actor_id") or "").strip()
    if not actor_id:
        raise HTTPException(status_code=409, detail="No comment/reply Actor candidate is configured for this source.")

    try:
        run_input = build_comment_smoke_input(source, body.seed_refs, body.max_items)
        run_input.update(body.input_overrides or {})
        # Validation is free and must pass before the explicit paid smoke.
        validation = validate_actor_input(actor_id, run_input)
        if not validation.get("ok"):
            raise IntegrationError("Comment/reply Actor input validation did not pass.")
        smoke = smoke_test_actor(
            actor_id, run_input, max_items=body.max_items, max_charge_usd=body.max_charge_usd
        )
        saved = commit_comment_route_verification(
            source, actor_id=actor_id, smoke_tested_at=smoke.get("tested_at"),
            route=cap.get("input_route"), input_field=cap.get("input_field"),
            output_mapping=(smoke.get("output_mapping") or {}).get("mapping") or {},
        )
        return {
            "ok": True,
            "source": source,
            "actor_id": actor_id,
            "validation": validation,
            "smoke_test": {k: v for k, v in smoke.items() if k != "sample_items"},
            "comment_deepening_status": saved.get("comment_deepening_status"),
            "comment_last_smoke_test_at": saved.get("comment_last_smoke_test_at"),
        }
    except (ValueError, IntegrationError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.delete("/api/sources/{source}/comments/verification")
def comment_route_clear(source: str):
    try:
        return clear_comment_route_verification(source, reason="manual_comment_route_clear")
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown source")


@app.post("/api/sources/{source}/actor/rollback")
def actor_rollback(source: str):
    try:
        return rollback_source(source)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown source")
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/sources/{source}/actor/history")
def actor_history(source: str):
    if source not in public_registry():
        raise HTTPException(status_code=404, detail="Unknown source")
    return source_history(source)


@app.post("/api/plan")
def plan(draft: AnalysisDraft):
    return build_collection_plan(draft).model_dump(mode="json")


@app.get("/api/runs")
def runs():
    return store.list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    try:
        return store.get_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")


@app.post("/api/runs")
def create_run(draft: AnalysisDraft, start: bool = Query(default=True)):
    collection_plan = build_collection_plan(draft)
    if collection_plan.budget_check == "estimate_over_budget":
        raise HTTPException(status_code=422, detail="Estimated acquisition cost exceeds the maximum budget.")

    payload = collection_plan.model_dump(mode="json")
    try:
        run_id, _folder = store.create(payload)
    except CloudPersistenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not start:
        return {"run_id": run_id, "status": "planned", "started": False, "reason": "start=false"}
    if settings.signalyth_dry_run:
        return {"run_id": run_id, "status": "planned", "started": False, "reason": "dry_run"}
    if not settings.apify_token:
        return {"run_id": run_id, "status": "planned", "started": False, "reason": "apify_not_connected"}

    try:
        status = manager.enqueue(run_id)
        return {"run_id": run_id, "status": status["status"], "started": True}
    except RunStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/runs/{run_id}/start")
def start_run(run_id: str):
    if settings.signalyth_dry_run:
        raise HTTPException(status_code=409, detail="Collection is disabled while SIGNALYTH_DRY_RUN=true.")
    if not settings.apify_token:
        raise HTTPException(status_code=409, detail="Apify is not connected. Configure APIFY_TOKEN securely on the server.")
    try:
        status = manager.enqueue(run_id)
        return status
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    except RunStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    """Permanently delete one run. Active runs must be cancelled first, unless the
    worker is provably dead (no status write for the stale window)."""
    try:
        status = store.read_status(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    current = str(status.get("status") or "")
    if current in {"queued", "running", "cancelling"}:
        stale_after = max(60, int(settings.signalyth_stale_running_after_seconds))
        stamp = status.get("updated_at")
        age = None
        if stamp:
            try:
                from datetime import datetime, timezone
                updated = datetime.fromisoformat(str(stamp))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - updated).total_seconds()
            except Exception:
                age = None
        if age is None or age < stale_after:
            raise HTTPException(status_code=409, detail="This run is still active. Cancel it first, then delete it.")
    store.delete_run(run_id)
    return {"run_id": run_id, "deleted": True}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    try:
        return manager.cancel(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")


@app.post("/api/runs/{run_id}/clean")
def clean_existing_run(run_id: str):
    try:
        folder = store.folder_for(run_id)
        plan = store.read_plan(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    if not (folder / "normalized-all.json").exists():
        raise HTTPException(status_code=409, detail="This run has no normalized collection data to clean yet.")
    try:
        report = clean_run(folder, plan=plan)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cleaning failed safely: {exc}")
    status = store.read_status(run_id)
    status["cleaning"] = {
        "status": "succeeded",
        "ruleset_version": report.get("ruleset_version"),
        "started_at": status.get("cleaning", {}).get("started_at"),
        "completed_at": report.get("generated_at"),
        "error": None,
        "summary": report,
    }
    if status.get("phase") in {"completed", "analyzed", "ai_analysis_failed"}:
        status["phase"] = "cleaned"
    prior_ai = load_analysis_summary(folder)
    if prior_ai is not None and prior_ai.get("stale"):
        status["analysis"] = {**(status.get("analysis") or {}), "status": "stale", "summary": prior_ai}
    prior_investigations = load_investigation_summary(folder)
    if prior_investigations is not None:
        status["investigations"] = {**(status.get("investigations") or {}), "status": "stale", "summary": prior_investigations}
    prior_visualizations = load_visualization_summary(folder)
    if prior_visualizations is not None:
        status["visualizations"] = {**(status.get("visualizations") or {}), "status": "stale", "summary": prior_visualizations}
    store.write_status(run_id, status)
    return report


@app.get("/api/runs/{run_id}/cleaning")
def get_cleaning(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    report = load_cleaning_summary(folder)
    if report is None:
        raise HTTPException(status_code=404, detail="Cleaning results are not available for this run")
    return report


@app.get("/api/runs/{run_id}/review")
def get_review_queue(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"items": load_review_queue(folder)}


@app.post("/api/runs/{run_id}/review/{record_id}")
def review_record(run_id: str, record_id: str, decision: ReviewDecision):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        result = apply_review_decision(
            folder, record_id, decision.action, decision.note,
            account_type=decision.account_type, content_class=decision.content_class,
        )
        prior_ai = load_analysis_summary(folder)
        if prior_ai is not None and prior_ai.get("stale"):
            status = store.read_status(run_id)
            status["analysis"] = {**(status.get("analysis") or {}), "status": "stale", "summary": prior_ai}
            prior_investigations = load_investigation_summary(folder)
            if prior_investigations is not None:
                status["investigations"] = {**(status.get("investigations") or {}), "status": "stale", "summary": prior_investigations}
            prior_visualizations = load_visualization_summary(folder)
            if prior_visualizations is not None:
                status["visualizations"] = {**(status.get("visualizations") or {}), "status": "stale", "summary": prior_visualizations}
            status["phase"] = "cleaned"
            store.write_status(run_id, status)
        return result
    except KeyError:
        raise HTTPException(status_code=404, detail="Record not found in cleaned dataset")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/runs/{run_id}/analyze")
def analyze_existing_run(run_id: str, force: bool = Query(default=False)):
    try:
        folder = store.folder_for(run_id)
        plan = store.read_plan(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    if not (folder / "cleaning" / "trusted.json").exists():
        raise HTTPException(status_code=409, detail="This run has no trusted Step 3 sample to analyze yet.")
    if not settings.signalyth_ai_enabled:
        raise HTTPException(status_code=409, detail="AI Analysis is disabled. Set SIGNALYTH_AI_ENABLED=true on the server when ready.")
    if not settings.openai_api_key:
        raise HTTPException(status_code=409, detail="OpenAI is not connected. Configure OPENAI_API_KEY securely on the server.")

    status = store.read_status(run_id)
    status["analysis"] = {
        **(status.get("analysis") or {}),
        "status": "running",
        "ruleset_version": "0.9.0",
        "prompt_version": "signalyth-semantic-v0.9",
        "started_at": status.get("analysis", {}).get("started_at") or datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "error": None,
    }
    status["phase"] = "ai_analysis"
    store.write_status(run_id, status)
    try:
        report = analyze_run(folder, plan=plan, force=force)
    except Exception as exc:
        status = store.read_status(run_id)
        status["analysis"] = {**(status.get("analysis") or {}), "status": "failed", "completed_at": datetime.now(timezone.utc).isoformat(), "error": str(exc)}
        status["phase"] = "ai_analysis_failed"
        store.write_status(run_id, status)
        raise HTTPException(status_code=500, detail=f"AI analysis failed safely: {exc}")

    status = store.read_status(run_id)
    status["analysis"] = {
        **(status.get("analysis") or {}),
        "status": "succeeded",
        "ruleset_version": report.get("ruleset_version"),
        "prompt_version": report.get("prompt_version"),
        "completed_at": report.get("generated_at"),
        "error": None,
        "summary": report,
    }
    prior_intelligence = load_intelligence_summary(folder)
    if prior_intelligence is not None:
        status["intelligence"] = {**(status.get("intelligence") or {}), "status": "stale", "summary": prior_intelligence}
    prior_investigations = load_investigation_summary(folder)
    if prior_investigations is not None:
        status["investigations"] = {**(status.get("investigations") or {}), "status": "stale", "summary": prior_investigations}
    prior_visualizations = load_visualization_summary(folder)
    if prior_visualizations is not None:
        status["visualizations"] = {**(status.get("visualizations") or {}), "status": "stale", "summary": prior_visualizations}
    status["phase"] = "analyzed"
    store.write_status(run_id, status)
    return report


@app.get("/api/runs/{run_id}/analysis")
def get_analysis(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    report = load_analysis_summary(folder)
    if report is None:
        raise HTTPException(status_code=404, detail="AI Analysis results are not available for this run")
    return report


@app.get("/api/runs/{run_id}/analysis/review")
def get_analysis_review(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"items": load_analysis_review_queue(folder)}


@app.post("/api/runs/{run_id}/analysis/review/{record_id}")
def review_analysis_record(run_id: str, record_id: str, decision: AIReviewDecision):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        result = apply_ai_review_decision(
            folder, record_id, decision.action, decision.note,
            sentiment_label=decision.sentiment_label, sentiment_score=decision.sentiment_score,
            primary_emotion=decision.primary_emotion, target_stance=decision.target_stance,
            topic=decision.topic, narrative=decision.narrative, sarcasm=decision.sarcasm,
        )
        prior_intelligence = load_intelligence_summary(folder)
        if prior_intelligence is not None:
            status = store.read_status(run_id)
            status["intelligence"] = {**(status.get("intelligence") or {}), "status": "stale", "summary": prior_intelligence}
            prior_investigations = load_investigation_summary(folder)
            if prior_investigations is not None:
                status["investigations"] = {**(status.get("investigations") or {}), "status": "stale", "summary": prior_investigations}
            prior_visualizations = load_visualization_summary(folder)
            if prior_visualizations is not None:
                status["visualizations"] = {**(status.get("visualizations") or {}), "status": "stale", "summary": prior_visualizations}
            status["phase"] = "analyzed"
            store.write_status(run_id, status)
        return result
    except KeyError:
        raise HTTPException(status_code=404, detail="Record not found in analyzed dataset")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/runs/{run_id}/intelligence")
def build_run_intelligence(run_id: str, force: bool = Query(default=False)):
    try:
        folder = store.folder_for(run_id)
        plan = store.read_plan(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    if not (folder / "analysis" / "analysis-ready.json").exists():
        raise HTTPException(status_code=409, detail="This run has no Step 4 analysis-ready evidence yet.")
    status = store.read_status(run_id)
    status["intelligence"] = {
        **(status.get("intelligence") or {}),
        "status": "running",
        "ruleset_version": "1.0.0",
        "methodology_version": "signalyth-intelligence-v1",
        "started_at": status.get("intelligence", {}).get("started_at") or datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "error": None,
    }
    status["phase"] = "intelligence"
    store.write_status(run_id, status)
    try:
        report = build_intelligence(folder, plan=plan, force=force)
    except Exception as exc:
        status = store.read_status(run_id)
        status["intelligence"] = {**(status.get("intelligence") or {}), "status": "failed", "completed_at": datetime.now(timezone.utc).isoformat(), "error": str(exc)}
        status["phase"] = "intelligence_failed"
        store.write_status(run_id, status)
        raise HTTPException(status_code=500, detail=f"Intelligence aggregation failed safely: {exc}")
    status = store.read_status(run_id)
    status["intelligence"] = {
        **(status.get("intelligence") or {}),
        "status": "succeeded",
        "ruleset_version": report.get("ruleset_version"),
        "methodology_version": report.get("methodology_version"),
        "completed_at": report.get("generated_at"),
        "error": None,
        "summary": report,
    }
    prior_investigations = load_investigation_summary(folder)
    if prior_investigations is not None:
        status["investigations"] = {**(status.get("investigations") or {}), "status": "stale", "summary": prior_investigations}
    prior_visualizations = load_visualization_summary(folder)
    if prior_visualizations is not None:
        status["visualizations"] = {**(status.get("visualizations") or {}), "status": "stale", "summary": prior_visualizations}
    status["phase"] = "intelligence_ready"
    store.write_status(run_id, status)
    return report


@app.get("/api/runs/{run_id}/intelligence")
def get_run_intelligence(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    report = load_intelligence_summary(folder)
    if report is None:
        raise HTTPException(status_code=404, detail="Intelligence results are not available for this run")
    return report


@app.post("/api/runs/{run_id}/investigations")
def build_run_investigations(run_id: str, force: bool = Query(default=False)):
    try:
        folder = store.folder_for(run_id)
        plan = store.read_plan(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    intelligence = load_intelligence_summary(folder)
    if intelligence is None:
        raise HTTPException(status_code=409, detail="This run has no Step 5 intelligence evidence yet.")
    if intelligence.get("stale"):
        raise HTTPException(status_code=409, detail="Step 5 intelligence is stale. Rebuild Intelligence before Automatic Investigations.")
    status = store.read_status(run_id)
    status["investigations"] = {
        **(status.get("investigations") or {}),
        "status": "running",
        "ruleset_version": "1.1.0",
        "methodology_version": "signalyth-investigations-v1",
        "evidence_contract_version": "signalyth-evidence-pack-v1.1",
        "started_at": status.get("investigations", {}).get("started_at") or datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "error": None,
    }
    status["phase"] = "investigations"
    store.write_status(run_id, status)
    try:
        report = build_investigations(folder, plan=plan, force=force)
    except Exception as exc:
        status = store.read_status(run_id)
        status["investigations"] = {**(status.get("investigations") or {}), "status": "failed", "completed_at": datetime.now(timezone.utc).isoformat(), "error": str(exc)}
        status["phase"] = "investigations_failed"
        store.write_status(run_id, status)
        raise HTTPException(status_code=500, detail=f"Automatic investigations failed safely: {exc}")
    status = store.read_status(run_id)
    status["investigations"] = {
        **(status.get("investigations") or {}),
        "status": "succeeded",
        "ruleset_version": report.get("ruleset_version"),
        "methodology_version": report.get("methodology_version"),
        "evidence_contract_version": report.get("evidence_contract_version"),
        "completed_at": report.get("generated_at"),
        "error": None,
        "summary": report,
    }
    prior_visualizations = load_visualization_summary(folder)
    if prior_visualizations is not None:
        status["visualizations"] = {**(status.get("visualizations") or {}), "status": "stale", "summary": prior_visualizations}
    status["phase"] = "investigations_ready"
    store.write_status(run_id, status)
    return report


@app.get("/api/runs/{run_id}/investigations")
def get_run_investigations(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    report = load_investigation_summary(folder)
    if report is None:
        raise HTTPException(status_code=404, detail="Automatic Investigation results are not available for this run")
    return report


@app.get("/api/runs/{run_id}/evidence-pack")
def get_run_evidence_pack(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    report = load_investigation_summary(folder)
    if report is None:
        raise HTTPException(status_code=404, detail="Presentation evidence pack is not available for this run")
    if report.get("stale"):
        raise HTTPException(status_code=409, detail="Presentation evidence pack is stale. Rebuild Automatic Investigations first.")
    pack = load_evidence_pack(folder)
    if pack is None:
        raise HTTPException(status_code=404, detail="Presentation evidence pack is not available for this run")
    return pack


@app.post("/api/runs/{run_id}/visualizations")
def build_run_visualizations(run_id: str, force: bool = Query(default=False)):
    try:
        folder = store.folder_for(run_id)
        plan = store.read_plan(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    investigations = load_investigation_summary(folder)
    if investigations is None:
        raise HTTPException(status_code=409, detail="This run has no Step 6 Evidence Pack yet.")
    if investigations.get("stale"):
        raise HTTPException(status_code=409, detail="Step 6 Evidence Pack is stale. Rebuild Automatic Investigations before Charts & Dashboard.")
    evidence_pack = load_evidence_pack(folder)
    if not isinstance(evidence_pack, dict):
        raise HTTPException(status_code=409, detail="Step 6 Evidence Pack is missing or incomplete.")
    persisted_context = evidence_pack.get("research_context") or {}
    for key in ("client", "topic", "market", "date_from", "date_to"):
        if persisted_context.get(key) not in (None, "") and plan.get(key) not in (None, "") and str(persisted_context.get(key)) != str(plan.get(key)):
            raise HTTPException(status_code=409, detail=f"Step 7 research context mismatch for {key}; rebuild upstream evidence before Charts & Dashboard.")
    status = store.read_status(run_id)
    status["visualizations"] = {
        **(status.get("visualizations") or {}),
        "status": "running",
        "ruleset_version": "1.2.0",
        "methodology_version": "signalyth-visual-intelligence-v1",
        "visual_contract_version": "signalyth-visual-pack-v1.2",
        "started_at": status.get("visualizations", {}).get("started_at") or datetime.now(timezone.utc).isoformat(),
        "completed_at": None, "error": None,
    }
    status["phase"] = "visualizations"
    store.write_status(run_id, status)
    try:
        report = build_visualizations(folder, plan=plan, force=force)
    except Exception as exc:
        status = store.read_status(run_id)
        status["visualizations"] = {**(status.get("visualizations") or {}), "status": "failed", "completed_at": datetime.now(timezone.utc).isoformat(), "error": str(exc)}
        status["phase"] = "visualizations_failed"
        store.write_status(run_id, status)
        raise HTTPException(status_code=500, detail=f"Charts & Dashboard build failed safely: {exc}")
    status = store.read_status(run_id)
    status["visualizations"] = {
        **(status.get("visualizations") or {}), "status": "succeeded",
        "ruleset_version": report.get("ruleset_version"), "methodology_version": report.get("methodology_version"),
        "visual_contract_version": report.get("visual_contract_version"), "completed_at": report.get("generated_at"),
        "error": None, "summary": report,
    }
    status["phase"] = "visualizations_ready"
    store.write_status(run_id, status)
    return report


@app.get("/api/runs/{run_id}/visualizations")
def get_run_visualizations(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    report = load_visualization_summary(folder)
    if report is None:
        raise HTTPException(status_code=404, detail="Charts & Dashboard are not available for this run")
    return report


@app.get("/api/runs/{run_id}/dashboard")
def get_run_dashboard(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    report = load_visualization_summary(folder)
    if report is None:
        raise HTTPException(status_code=404, detail="Dashboard is not available for this run")
    if report.get("stale"):
        raise HTTPException(status_code=409, detail="Dashboard is stale. Rebuild Charts & Dashboard first.")
    dashboard = load_dashboard(folder)
    charts = load_chart_specs(folder)
    if dashboard is None or charts is None:
        raise HTTPException(status_code=404, detail="Dashboard is not available for this run")
    return {"summary": report, "dashboard": dashboard, "charts": charts}


@app.get("/api/runs/{run_id}/presentation-visual-pack")
def get_presentation_visual_pack(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    report = load_visualization_summary(folder)
    if report is None:
        raise HTTPException(status_code=404, detail="Presentation visual pack is not available for this run")
    if report.get("stale"):
        raise HTTPException(status_code=409, detail="Presentation visual pack is stale. Rebuild Charts & Dashboard first.")
    pack = load_presentation_visual_pack(folder)
    if pack is None:
        raise HTTPException(status_code=404, detail="Presentation visual pack is not available for this run")
    return pack


@app.post("/api/runs/{run_id}/exports")
def build_run_exports(run_id: str, force: bool = Query(default=False), media_handling: str | None = Query(default=None)):
    try:
        folder = store.folder_for(run_id)
        plan = store.read_plan(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    if media_handling:
        choice = str(media_handling).strip().lower()
        if choice not in {"blended", "separate", "exclude"}:
            raise HTTPException(status_code=422, detail="media_handling must be one of: blended, separate, exclude")
        # Export-time override: re-render the report with a different media policy
        # without touching the stored research plan or re-running collection.
        plan = {**plan, "media_handling": choice}
        force = True
    visual = load_visualization_summary(folder)
    if visual is None:
        raise HTTPException(status_code=409, detail="This run has no Step 7 Presentation Visual Pack yet.")
    if visual.get("stale"):
        raise HTTPException(status_code=409, detail="Step 7 is stale. Rebuild Charts & Dashboard before exports.")
    status = store.read_status(run_id)
    status["exports"] = {
        **(status.get("exports") or {}),
        "status": "running",
        "ruleset_version": "1.3.0",
        "methodology_version": "signalyth-presentation-intelligence-v1",
        "presentation_contract_version": "signalyth-presentation-pack-v1.3",
        "started_at": (status.get("exports") or {}).get("started_at") or datetime.now(timezone.utc).isoformat(),
        "completed_at": None, "error": None,
    }
    status["phase"] = "exports"
    store.write_status(run_id, status)
    try:
        report = build_exports(folder, plan, force=force)
    except Exception as exc:
        status = store.read_status(run_id)
        status["exports"] = {**(status.get("exports") or {}), "status": "failed", "completed_at": datetime.now(timezone.utc).isoformat(), "error": str(exc)}
        status["phase"] = "exports_failed"
        store.write_status(run_id, status)
        raise HTTPException(status_code=500, detail=f"Presentation/export generation failed safely: {exc}")
    status = store.read_status(run_id)
    status["exports"] = {
        **(status.get("exports") or {}), "status": "succeeded",
        "ruleset_version": report.get("ruleset_version"),
        "methodology_version": report.get("methodology_version"),
        "presentation_contract_version": report.get("presentation_contract_version"),
        "completed_at": report.get("generated_at"), "error": None, "summary": report,
    }
    status["phase"] = "exports_ready"
    store.write_status(run_id, status)
    return report


@app.get("/api/runs/{run_id}/exports")
def get_run_exports(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    summary = load_export_summary(folder)
    if summary is None:
        raise HTTPException(status_code=404, detail="Exports are not available for this run")
    manifest = load_export_manifest(folder) or {"files": []}
    return {"summary": summary, "manifest": manifest}


@app.get("/api/series")
def get_series(key: str | None = Query(default=None)):
    """Runs of the same brand grouped into a time series, with period-over-period change."""
    runs = store.list_runs()
    detailed = []
    for row in runs:
        if str(row.get("status")) not in {"succeeded", "completed", "completed_shortfall"}:
            continue
        try:
            detailed.append(store.read_status(row["run_id"]))
        except Exception:
            continue
    series = trends_service.build_series(detailed)
    if key:
        one = next((s for s in series if s["series_key"] == key), None)
        if not one:
            raise HTTPException(status_code=404, detail="Series not found")
        return one
    return {"series": series, "count": len(series)}


@app.get("/api/runs/{run_id}/review-queue")
def get_review_queue(run_id: str, limit: int = Query(default=200, ge=1, le=500)):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    items = review_service.queue_items(store, folder, limit=limit)
    report = store.read(folder / "analysis" / "human-review-report.json", None)
    return {"run_id": run_id, "items": items, "count": len(items), "last_apply": report}


@app.post("/api/runs/{run_id}/review-queue")
def post_review_decision(run_id: str, payload: dict):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    record_id = str(payload.get("record_id") or "").strip()
    if not record_id:
        raise HTTPException(status_code=422, detail="record_id is required")
    try:
        review_service.save_decision(
            store, folder, record_id, payload.get("verdict"),
            payload.get("sentiment"), payload.get("reviewer"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        store.checkpoint_run(run_id)
    except Exception:
        pass
    return {"ok": True}


@app.delete("/api/runs/{run_id}/review-queue")
def delete_review_decisions(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    review_service.clear_decisions(store, folder)
    return {"ok": True}


@app.post("/api/runs/{run_id}/review-queue/apply")
def apply_review_decisions(run_id: str):
    """Rebuild the evidence base with human decisions, then recompute downstream."""
    try:
        folder = store.folder_for(run_id)
        plan = store.read_plan(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        report = review_service.apply_reviews(store, folder)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    from app.services.intelligence import build_intelligence
    from app.services.investigations import build_investigations
    from app.services.visualizations import build_visualizations
    build_intelligence(folder, plan, force=True)
    build_investigations(folder, plan, force=True)
    build_visualizations(folder, plan, force=True)
    status = store.read_status(run_id)
    status["human_review"] = report
    status["exports"] = {**(status.get("exports") or {}), "stale": True}
    store.write_status(run_id, status)
    try:
        store.checkpoint_run(run_id)
    except Exception:
        pass
    return {"ok": True, "report": report}


@app.get("/api/runs/{run_id}/validation")
def get_validation(run_id: str, size: int = Query(default=60, ge=10, le=300)):
    """Blind gold-set items plus current metrics.

    Items are served without any model label so the annotator is not anchored.
    """
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    records = store.read(folder / "analysis" / "analyzed.json", []) or []
    if not records:
        raise HTTPException(status_code=409, detail="Analysis has not produced records for this run yet.")
    gold = validation_service.load_gold(store, folder)
    items = validation_service.blind_items(records, run_id, size=size)
    labelled = gold.get("labels") or {}
    return {
        "run_id": run_id,
        "total_records": len(records),
        "items": [{**item, "labelled": labelled.get(item["record_id"], {}).get("sentiment")} for item in items],
        "metrics": validation_service.score(records, gold),
    }


@app.post("/api/runs/{run_id}/validation")
def post_validation_label(run_id: str, payload: dict):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    record_id = str(payload.get("record_id") or "").strip()
    if not record_id:
        raise HTTPException(status_code=422, detail="record_id is required")
    try:
        validation_service.save_label(
            store, folder, record_id,
            payload.get("sentiment"), payload.get("relevance"), payload.get("annotator"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    records = store.read(folder / "analysis" / "analyzed.json", []) or []
    gold = validation_service.load_gold(store, folder)
    try:
        store.checkpoint_run(run_id)
    except Exception:
        pass
    return {"ok": True, "metrics": validation_service.score(records, gold)}


@app.delete("/api/runs/{run_id}/validation")
def delete_validation(run_id: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    validation_service.clear_gold(store, folder)
    return {"ok": True}


@app.get("/api/runs/{run_id}/exports/{filename}")
def download_run_export(run_id: str, filename: str):
    try:
        folder = store.folder_for(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    summary = load_export_summary(folder)
    if summary is None:
        raise HTTPException(status_code=404, detail="Exports are not available for this run")
    if summary.get("stale"):
        raise HTTPException(status_code=409, detail="Exports are stale. Rebuild Presentation & Exports first.")
    path = allowed_export_file(folder, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Export file not found")
    return FileResponse(path, filename=path.name)
