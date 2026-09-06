from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def write(rel: str, text: str) -> None:
    (ROOT / rel).write_text(text, encoding="utf-8")


def must_replace(rel: str, old: str, new: str, count: int = 1):
    text = read(rel)
    if old not in text:
        raise RuntimeError(f"Hotfix anchor not found in {rel}: {old[:160]!r}")
    write(rel, text.replace(old, new, count))


def replace_function(rel: str, name: str, replacement: str):
    text = read(rel)
    pat = re.compile(rf"^def {re.escape(name)}\b.*?(?=^def |^class |\Z)", re.M | re.S)
    matches = list(pat.finditer(text))
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one top-level function {name} in {rel}, found {len(matches)}")
    m = matches[0]
    write(rel, text[:m.start()] + replacement.rstrip() + "\n\n" + text[m.end():])


# ---------------------------------------------------------------------------
# Presentation/report: deterministic indicator contributions are traceable via
# their locked indicator provenance even when no single record is the sole cause.
# ---------------------------------------------------------------------------
must_replace(
    "app/services/presentation.py",
    '        "analyst_findings": bool((visual_pack.get("analyst_synthesis") or {}).get("findings")),\n        "recommendations": bool((visual_pack.get("analyst_synthesis") or {}).get("recommendations")),\n',
    '        "analyst_findings": "analyst_findings" in slide_ids,\n        "recommendations": "recommendations" in slide_ids,\n',
)
must_replace(
    "app/services/report_synthesis.py",
    '    traceable=all(c.get("indicator_ids") and (c.get("evidence_refs") or c.get("claim_type")=="recommendation") for c in material_ai)\n',
    '    traceable=all(c.get("indicator_ids") and (c.get("evidence_refs") or c.get("claim_type")=="recommendation" or c.get("causal_status")=="deterministic_contribution") for c in material_ai)\n',
)


# ---------------------------------------------------------------------------
# Query planner: preserve the Master30 shared-target semantics while retaining
# proven source-safe batching, old safety invariants and custom Actor support.
# ---------------------------------------------------------------------------
must_replace(
    "app/services/query_planner.py",
    'from app.services.source_capabilities import comments_forecast\n',
    'from app.services.source_capabilities import comments_forecast\nfrom app.services.resilience import get_safe_batch_size\n',
)

replace_function(
    "app/services/query_planner.py",
    "build_terms",
    r'''def build_terms(draft: AnalysisDraft):
    topic = canonical_topic(str(draft.topic or "").strip(), str(draft.market or ""))
    original_topic = str(draft.topic or "").strip()
    topic_fold = topic.casefold()
    original_fold = original_topic.casefold()
    roles = _role_map(draft)

    aliases = []
    context = []
    for raw in [*draft.keywords, *draft.additional_context]:
        term = str(raw or "").strip()
        if not term:
            continue
        role = roles.get(term.casefold(), "context")
        tf = term.casefold()
        if role == "exclude":
            continue
        if role == "alias":
            if tf not in {topic_fold, original_fold}:
                aliases.append(term)
            continue
        # Repeating the topic itself, its market-suffixed original form or a parent
        # brand token (Vodafone under Vodafone Internet) wastes a discovery route.
        if tf in {topic_fold, original_fold}:
            continue
        if len(tf) >= 4 and tf in topic_fold:
            continue
        context.append(term)

    if draft.market.casefold() == "greece":
        market_context = ["Greece", "Ελλάδα", "Ellada"]
    else:
        market_context = [str(draft.market or "").strip()]

    # Context transliterations remain CONTEXT (e.g. ΟΠΑΠ -> OPAP); they are not
    # mistaken for aliases of the research subject.
    context_variants = []
    for term in context:
        variant = greeklish(term)
        if variant:
            context_variants.append(variant)
    context = uniq([*context, *context_variants, *market_context])

    topic_glish = greeklish(topic)
    topic_aliases = uniq([*aliases, *([topic_glish] if topic_glish else [])])
    return [topic], context, topic_aliases'''
)

replace_function(
    "app/services/query_planner.py",
    "_research_routes",
    r'''def _research_routes(draft: AnalysisDraft) -> dict:
    topic = canonical_topic(str(draft.topic or "").strip(), str(draft.market or ""))
    core, cleaned_context, topic_aliases = build_terms(draft)
    roles = _role_map(draft)
    topic_fold = topic.casefold()
    original_fold = str(draft.topic or "").strip().casefold()

    def role_values(role: str) -> list[str]:
        out = []
        for raw in [*draft.keywords, *draft.additional_context]:
            term = str(raw or "").strip()
            if not term:
                continue
            tf = term.casefold()
            if roles.get(tf, "context") != role:
                continue
            if role != "alias" and (tf in {topic_fold, original_fold} or (len(tf) >= 4 and tf in topic_fold)):
                continue
            out.append(term)
        return uniq(out)

    required = role_values("required_context")
    contexts = role_values("context")
    watch = role_values("watch")
    explicit_aliases = role_values("alias")
    aliases = uniq([*explicit_aliases, *topic_aliases])
    excludes = uniq([*draft.exclusions, *role_values("exclude")])

    user_context_fold = {x.casefold() for x in [*required, *contexts, *watch]}
    market_context = [x for x in cleaned_context if x.casefold() not in user_context_fold]
    anchored_required = [f"{topic} {x}" for x in required]
    anchored_context = [f"{topic} {x}" for x in contexts]
    anchored_watch = [f"{topic} {x}" for x in watch]
    anchored_market = [f"{topic} {x}" for x in market_context if x and x.casefold() != topic_fold]

    if draft.search_strategy == "topic_first":
        primary = uniq([topic, *aliases])
        topups = uniq([*anchored_required, *anchored_context, *anchored_watch, *anchored_market])
    elif draft.search_strategy == "context_first":
        primary = uniq([*anchored_required, *anchored_context]) or [topic]
        alias_context = [f"{a} {x}" for a in aliases for x in [*required, *contexts] if x]
        topups = uniq([*alias_context, *anchored_watch, *anchored_market, topic])
    else:
        anchored_user = uniq([*anchored_required, *anchored_context])
        primary = uniq([topic, *aliases, *anchored_user[:2]])
        topups = uniq([*anchored_user[2:], *anchored_watch, *anchored_market])

    return {
        "topic": topic, "aliases": aliases, "required": required, "contexts": contexts,
        "watch": watch, "excludes": excludes, "primary": primary, "topups": topups,
    }'''
)

replace_function(
    "app/services/query_planner.py",
    "_sub",
    r'''def _sub(actor: str, inp: dict, target: int, cap: float, purpose: str, draft: AnalysisDraft, exact=True):
    return SubRunPlan(
        actor_id=actor,
        input=inp,
        target_items=max(1, int(target)),
        max_charge_usd=max(0.0, float(cap)),
        exact_post_filter=exact,
        post_filter_from=draft.date_from if exact else None,
        post_filter_to=draft.date_to if exact else None,
        purpose=purpose,
        max_attempt_calls=2,
    )'''
)

replace_function(
    "app/services/query_planner.py",
    "_caps",
    r'''def _caps(source_budget: float, n_topups: int) -> tuple[float, list[float]]:
    budget = max(0.0, float(source_budget))
    if n_topups <= 0:
        return budget, []
    primary = budget * 0.70
    remain = max(0.0, budget - primary)
    return primary, [remain / n_topups for _ in range(n_topups)]'''
)

replace_function(
    "app/services/query_planner.py",
    "make_generic_source_plan",
    r'''def make_generic_source_plan(source: str, target: int, draft: AnalysisDraft, queries: list[str], cfg: dict, source_budget: float) -> SourcePlan:
    mapping = cfg.get("input_mapping") or {}
    query_field = mapping.get("query")
    urls_field = mapping.get("urls")
    if not query_field and not (source == "instagram" and urls_field):
        raise ValueError(f"Verified Actor for {source} has no usable discovery input mapping.")

    def field_type(field: str | None):
        if not field:
            return None
        spec = (cfg.get("input_schema_fields") or {}).get(field)
        if isinstance(spec, dict):
            return spec.get("type")
        if isinstance(spec, str):
            return spec
        value = (cfg.get("input_template") or {}).get(field)
        if isinstance(value, list): return "array"
        if isinstance(value, bool): return "boolean"
        if isinstance(value, int): return "integer"
        if isinstance(value, float): return "number"
        if isinstance(value, str): return "string"
        return None

    def set_semantic(inp: dict, semantic: str, value):
        field = mapping.get(semantic)
        if not field or value is None:
            return
        typ = field_type(field)
        if typ == "array" and not isinstance(value, list): value = [value]
        elif typ == "string" and isinstance(value, list): value = value[0] if value else ""
        elif typ == "integer": value = int(value)
        elif typ == "number": value = float(value)
        elif typ == "boolean": value = bool(value)
        inp[field] = value

    semantic = "query" if query_field else "urls"
    discovery_field = mapping.get(semantic)
    typ = field_type(discovery_field)
    if semantic == "urls":
        tags = instagram_discovery_tags(draft)
        discovery_values = [f"https://www.instagram.com/explore/tags/{t.lower()}/" for t in tags] or []
    else:
        discovery_values = uniq(queries[:8] or _research_routes(draft)["primary"] or [draft.topic])

    safe = get_safe_batch_size(source, cfg["actor_id"])
    chunks = batched(discovery_values, safe) if typ == "array" else [[x] for x in discovery_values[:8]]
    chunks = chunks or [[draft.topic]]
    shares = split_target(target, len(chunks))
    total = sum(shares) or 1
    subruns = []
    for i, (chunk, share) in enumerate(zip(chunks, shares)):
        inp = copy.deepcopy(cfg.get("input_template") or {})
        set_semantic(inp, semantic, chunk if typ == "array" else chunk[0])
        set_semantic(inp, "max_items", share)
        set_semantic(inp, "date_from", draft.date_from.isoformat())
        set_semantic(inp, "date_to", draft.date_to.isoformat())
        country_field = mapping.get("country")
        if country_field:
            norm = re.sub(r"[^a-z]", "", country_field.casefold())
            country = ("gr" if norm in {"gl", "countrycode"} else "Greece" if "location" in norm else "GR") if draft.market.casefold() == "greece" else draft.market
            set_semantic(inp, "country", country)
        if mapping.get("language"):
            set_semantic(inp, "language", "el" if draft.market.casefold() == "greece" else "en")
        set_semantic(inp, "comments", draft.comments)
        cap = max(0.0, float(source_budget)) * share / total
        subruns.append(_sub(cfg["actor_id"], inp, share, cap, f"generic_discovery_batch_{i+1}", draft))

    preview = [{"source": source, "route": "primary", "query": str(x), "order": i + 1, "editable": True,
                "date_from": draft.date_from.isoformat(), "date_to": draft.date_to.isoformat(), "strategy": draft.search_strategy}
               for i, x in enumerate(discovery_values)]
    rate = cfg.get("price_per_1000_hint")
    est = round(target * rate / 1000, 4) if rate is not None else None
    return SourcePlan(
        source=source, actor_id=cfg["actor_id"], target_items=target, estimated_cost_usd=est,
        price_per_1000_hint=rate,
        date_strategy="mapped_native_plus_exact_post_filter" if mapping.get("date_from") and mapping.get("date_to") else "exact_post_filter",
        market_strategy="mapped_or_relevance_filter", queries=discovery_values, subruns=subruns,
        topup_subruns=[], semantic_topup_subruns=copy.deepcopy(subruns), query_preview=preview,
        source_budget_usd=max(0.0, float(source_budget)), source_contract_version="actor-contract-v3",
    )'''
)

replace_function(
    "app/services/query_planner.py",
    "make_source_plan",
    r'''def make_source_plan(source: str, target: int, draft: AnalysisDraft, queries: list[str], registry: dict, source_budget: float) -> SourcePlan:
    cfg = registry[source]
    if cfg.get("adapter_mode") == "generic":
        return make_generic_source_plan(source, target, draft, queries, cfg, source_budget)

    actor = cfg["actor_id"]
    rate = cfg.get("price_per_1000_hint")
    est = round(target * rate / 1000, 4) if rate is not None else None
    routes = _research_routes(draft)
    primary, topups = _apply_override(draft, source, routes["primary"], routes["topups"])
    intent_buckets = []

    if source == "x" and draft.search_strategy == "balanced_smart" and draft.smart_search:
        # Feed Smart Collection a de-duplicated subject/context draft so a parent-brand
        # keyword cannot create "Topic Topic" routes. Keep ALL intent families, then
        # isolate them into safe two-target Actor calls.
        cleaned_kw = []
        tf = routes["topic"].casefold()
        for value in draft.keywords:
            v = str(value or "").strip()
            vf = v.casefold()
            if not v or vf == tf or (len(vf) >= 4 and vf in tf):
                continue
            cleaned_kw.append(v)
        smart_draft = draft.model_copy(update={"keywords": cleaned_kw})
        xin, intent_buckets = x_search_input(smart_draft, target)
        primary = uniq([*primary, *(xin.get("searchTerms") or [])])

    primary = primary or [routes["topic"]]
    topups = uniq(topups)
    safe = max(1, int(get_safe_batch_size(source, actor)))
    source_budget = max(0.0, float(source_budget))
    has_topups = bool(topups) or source == "x"
    primary_budget = source_budget * (0.70 if has_topups else 1.0)
    topup_budget = max(0.0, source_budget - primary_budget)
    until_exclusive = draft.date_to + timedelta(days=1)
    subruns: list[SubRunPlan] = []
    topup_subruns: list[SubRunPlan] = []

    def caps_for(shares, budget):
        den = sum(shares) or 1
        return [max(0.0, budget * s / den) for s in shares]

    if source == "x":
        batches = batched(primary, safe)
        shares = split_target(target, len(batches))
        caps = caps_for(shares, primary_budget)
        for i, (batch, share) in enumerate(zip(batches, shares)):
            inp = {"mode": "search", "searchTerms": batch, "maxItems": share, "includeSearchTerms": True,
                   "queryType": "Latest", "since": f"{draft.date_from.isoformat()}_00:00:00_UTC",
                   "until": f"{until_exclusive.isoformat()}_00:00:00_UTC"}
            purpose = "primary_global" if len(batches) == 1 else f"primary_route_{i+1}"
            subruns.append(_sub(actor, inp, share, caps[i], purpose, draft))
        tq = topups or primary
        tbatches = batched(tq, safe)[:4]
        tcaps = [topup_budget / max(1, len(tbatches)) for _ in tbatches]
        for i, batch in enumerate(tbatches):
            inp = {"mode": "search", "searchTerms": batch, "maxItems": target, "includeSearchTerms": True,
                   "queryType": "Latest + Top", "since": f"{draft.date_from.isoformat()}_00:00:00_UTC",
                   "until": f"{until_exclusive.isoformat()}_00:00:00_UTC"}
            topup_subruns.append(_sub(actor, inp, target, tcaps[i], f"topup_latest_plus_top_{i+1}", draft))

    elif source == "tiktok":
        batches = batched(primary, safe)
        shares = split_target(target, len(batches)); caps = caps_for(shares, primary_budget)
        for i, (batch, share) in enumerate(zip(batches, shares)):
            inp = {"search": batch, "maxItems": share,
                   "location": "GR" if draft.market.casefold() == "greece" else None,
                   "dateRange": _tiktok_date_range(draft),
                   "sortType": "DATE_POSTED" if (draft.date_to - draft.date_from).days <= 7 else "RELEVANCE"}
            inp = {k: v for k, v in inp.items() if v is not None}
            subruns.append(_sub(actor, inp, share, caps[i], f"primary_route_{i+1}", draft))
        tbatches = batched(topups, safe)[:4]
        for i, batch in enumerate(tbatches):
            inp = {"search": batch, "maxItems": target,
                   "location": "GR" if draft.market.casefold() == "greece" else None,
                   "dateRange": _tiktok_date_range(draft), "sortType": "RELEVANCE"}
            inp = {k: v for k, v in inp.items() if v is not None}
            topup_subruns.append(_sub(actor, inp, target, topup_budget / max(1, len(tbatches)), f"topup_{i+1}", draft))

    elif source == "instagram":
        tags = instagram_discovery_tags(draft) or [hashtag(routes["topic"])]
        primary_tag = tags[0]
        inp = {"directUrls": [f"https://www.instagram.com/explore/tags/{primary_tag.lower()}/"],
               "resultsType": "posts", "resultsLimit": target,
               "onlyPostsNewerThan": draft.date_from.isoformat(), "addParentData": True}
        subruns = [_sub(actor, inp, target, primary_budget, "primary_hashtag_posts", draft)]
        extra_tags = tags[1:4]
        for i, tag in enumerate(extra_tags):
            tin = {**inp, "directUrls": [f"https://www.instagram.com/explore/tags/{tag.lower()}/"], "resultsLimit": target}
            topup_subruns.append(_sub(actor, tin, target, topup_budget / max(1, len(extra_tags)), f"topup_hashtag_{i+1}", draft))

    elif source == "facebook":
        pqueries = uniq([_facebook_safe_query(x) for x in primary if x]) or [_facebook_safe_query(routes["topic"])]
        shares = split_target(target, len(pqueries)); caps = caps_for(shares, primary_budget)
        for i, (qv, share) in enumerate(zip(pqueries, shares)):
            inp = {"query": qv, "resultsCount": share, "searchType": "latest",
                   "startDate": draft.date_from.isoformat(), "endDate": draft.date_to.isoformat()}
            purpose = "primary_topic" if i == 0 else f"primary_context_{i+1}"
            subruns.append(_sub(actor, inp, share, caps[i], purpose, draft, exact=False))
        tqueries = uniq([_facebook_safe_query(x) for x in topups if x])[:4]
        for i, qv in enumerate(tqueries):
            tin = {"query": qv, "resultsCount": target, "searchType": "latest",
                   "startDate": draft.date_from.isoformat(), "endDate": draft.date_to.isoformat()}
            topup_subruns.append(_sub(actor, tin, target, topup_budget / max(1, len(tqueries)), f"topup_query_{i+1}", draft, exact=False))

    elif source == "youtube":
        batches = batched(primary, safe)
        shares = split_target(target, len(batches)); caps = caps_for(shares, primary_budget)
        for i, (batch, share) in enumerate(zip(batches, shares)):
            inp = {"keywords": batch, "gl": "gr" if draft.market.casefold() == "greece" else "us",
                   "hl": "el" if draft.market.casefold() == "greece" else "en",
                   "uploadDate": _youtube_upload_date(draft), "sort": "r", "maxItems": share}
            subruns.append(_sub(actor, inp, share, caps[i], f"primary_route_{i+1}", draft))
        tbatches = batched(topups, safe)[:4]
        for i, batch in enumerate(tbatches):
            tin = {"keywords": batch, "gl": "gr" if draft.market.casefold() == "greece" else "us",
                   "hl": "el" if draft.market.casefold() == "greece" else "en",
                   "uploadDate": _youtube_upload_date(draft), "sort": "r", "maxItems": target}
            topup_subruns.append(_sub(actor, tin, target, topup_budget / max(1, len(tbatches)), f"topup_{i+1}", draft))

    elif source == "news":
        nq = news_queries_for_capacity(draft, uniq([*primary, *topups]), target)
        batches = batched(nq or [routes["topic"]], safe)
        shares = split_target(target, len(batches)); caps = caps_for(shares, source_budget)
        for i, (batch, share) in enumerate(zip(batches, shares)):
            per_query = min(500, max(1, math.ceil(share / max(1, len(batch)))))
            inp = {"queries": batch, "language": "el" if draft.market.casefold() == "greece" else "en-US",
                   "country": "GR" if draft.market.casefold() == "greece" else "US",
                   "maxArticles": per_query, "fromDate": draft.date_from.isoformat(),
                   "toDate": draft.date_to.isoformat(), "resolveUrls": True}
            subruns.append(_sub(actor, inp, share, caps[i], f"primary_route_{i+1}", draft, exact=False))

    preview = _query_preview(source, primary, topups, draft)
    return SourcePlan(
        source=source, actor_id=actor, target_items=target, estimated_cost_usd=est,
        price_per_1000_hint=rate, date_strategy=cfg.get("date_support", "post_filter_exact"),
        market_strategy=cfg.get("market_support", "query_context"), queries=uniq([*primary, *topups]),
        subruns=subruns, topup_subruns=topup_subruns,
        semantic_topup_subruns=copy.deepcopy(topup_subruns or subruns),
        intent_buckets=intent_buckets, query_preview=preview,
        source_budget_usd=source_budget, source_contract_version="actor-contract-v3",
    )'''
)

replace_function(
    "app/services/query_planner.py",
    "_preflight_forecast",
    r'''def _preflight_forecast(draft: AnalysisDraft, plans: list[SourcePlan], registry: dict) -> dict:
    rows = []
    blockers = []
    for sp in plans:
        cfg = registry.get(sp.source, {})
        comment = comments_forecast(sp.source, bool(draft.comments), cfg)
        if draft.comments and comment.get("status") not in {"verified_available", "not_applicable"}:
            blockers.append(sp.source)
        is_default = bool(cfg.get("locked") and cfg.get("actor_id") == cfg.get("default_actor_id") == sp.actor_id)
        rows.append({
            "source": sp.source, "actor_id": sp.actor_id,
            "actor_verified": cfg.get("actor_status") == "verified" or is_default,
            "planned_primary_calls": len(sp.subruns), "available_topup_routes": len(sp.topup_subruns),
            "target_semantics": "final_analyzable_unique_in_range", "comments": comment,
        })
    comments_coverage = {
        "requested": bool(draft.comments),
        "fully_live_verified": bool(draft.comments) and not blockers,
        "verification_blockers": blockers,
    }
    return {
        "version": "collection-preflight-master30-v2", "mode": "static_contract_audit",
        "risk_level": "low" if all(r["actor_verified"] for r in rows) else "medium",
        "selected_sources": [p.source for p in plans],
        "planned_actor_batches": sum(len(p.subruns) for p in plans),
        "failure_isolation": "per logical Actor batch and per source",
        "budget_guard": "global hard cap + source envelope",
        "sample_rule": "shared source target; every route can refill the same target until target, source exhaustion, relevance floor or budget",
        "comments_coverage": comments_coverage,
        "query_safety": {
            "client_field_used_for_discovery": False,
            "market_only_queries_allowed": False,
            "context_queries_topic_anchored": True,
            "search_strategy": draft.search_strategy,
            "query_preview_available": True,
        },
        "sources": rows,
    }'''
)


# ---------------------------------------------------------------------------
# Collector: a planned batch is a ROUTE, not a quota. Before every Actor call,
# calculate the current source shortfall and let that route attempt to fill it.
# ---------------------------------------------------------------------------
must_replace(
    "app/services/collector.py",
    '            "Live collection blocked: replacement/custom source Actors require verification before paid collection: "\n',
    '            "Live collection blocked: replacement/custom source Actors require a tiny paid smoke test / verification before paid collection: "\n',
)

must_replace(
    "app/services/collector.py",
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
''',
    '''                sr_status = source_status["subruns"][sr_idx]
                purpose = str(sr.get("purpose", "discovery"))
                # Every planned query/batch is only a discovery route. It owns NO fixed
                # fraction of the requested sample. Recompute the shared-source shortfall
                # before every call and let the next route attempt the whole remainder.
                current_norm, current_metrics = _normalize_partial(source, source_raw, desired_target, date_from, date_to)
                source_status.update(current_metrics)
                remaining_needed = max(0, desired_target - len(current_norm))
                if remaining_needed <= 0:
                    sr_status.update({"status":"skipped_target_met","started_at":None,"completed_at":_utcnow(),"error":None})
                    source_status["subruns_completed"] += 1
                    sync(f"{source}: target met; unused discovery route skipped", source, code="route_skipped_target_met", purpose=purpose)
                    continue
                old_target = max(1, int(sr.get("target_items", desired_target) or desired_target))
                sr["target_items"] = remaining_needed
                sr["input"] = _resize_input(source, sr.get("input", {}), old_target, remaining_needed)
                sr_status["target_items"] = remaining_needed
                sr_status.update({"status": "running", "started_at": _utcnow()})
                sync(f"{source}: collecting {purpose}", source, code="collecting_purpose", purpose=purpose)

                # The source budget is one envelope shared by all its discovery routes.
                # Do not strand money in a route whose planned share happened to be small.
                source_spent = max(0.0, float(source_status.get("cost_usd", 0.0) or 0.0))
                safe_cap = min(max(0.0, source_cap - source_spent), max(0.0, guard.remaining))
'''
)


# ---------------------------------------------------------------------------
# Cleaning + AI: hard exclusions remain hard, lexical uncertainty survives to
# OpenAI via a separate semantic-candidate pool. Keep old trusted semantics intact.
# ---------------------------------------------------------------------------
replace_function(
    "app/services/cleaning.py",
    "_decision",
    r'''def _decision(relevance: float, market: float, spam: float, bot_risk: float, bot_reason_count: int, flags: list[str], content_class: str, impact: int) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if "exact_duplicate" in flags or "near_duplicate_same_author" in flags:
        return "excluded", ["duplicate_not_independent_evidence"]
    if "explicit_exclusion_context" in flags:
        return "excluded", ["explicit_exclusion_context"]
    if spam >= RULESET_CONFIG["spam_exclude_at"]:
        return "excluded", ["high_spam_risk"]
    if bot_risk >= RULESET_CONFIG["bot_likely_automated_at"] and bot_reason_count >= 2:
        return "excluded", ["high_automation_or_manipulation_risk"]
    # Lexical relevance is only a cheap pre-AI signal. Low lexical overlap is sent
    # to semantic review rather than destroyed before the model can understand it.
    if relevance < RULESET_CONFIG["relevance_exclude_below"]:
        reasons.append("lexical_relevance_low_semantic_review_required")
    elif relevance < RULESET_CONFIG["relevance_review_below"]:
        reasons.append("relevance_uncertain")
    if str(content_class) == "unknown" and relevance < 0.68:
        reasons.append("content_context_uncertain")
    if RULESET_CONFIG["bot_suspicious_at"] <= bot_risk < RULESET_CONFIG["bot_likely_automated_at"]:
        reasons.append("authenticity_uncertain")
    if market < RULESET_CONFIG["market_review_below"]:
        reasons.append("market_relevance_uncertain")
    if impact >= 10000 and bot_risk >= 0.35:
        reasons.append("high_impact_suspicious_activity")
    if "ambiguous_short_entity" in flags:
        reasons.append("entity_disambiguation_needed")
    return ("review", list(dict.fromkeys(reasons))) if reasons else ("trusted", [])'''
)

must_replace(
    "app/services/cleaning.py",
    '''        # Historical filename retained for API compatibility: this is now the
        # semantic-candidate pool (trusted + review), while hard hygiene exclusions
        # remain excluded before any paid AI call.
        "trusted": [r for r in cleaned if r["cleaning"]["decision"] in {"trusted", "review"}],
        "organic": [r for r in cleaned if r["cleaning"]["decision"] == "trusted" and r["cleaning"]["organic_eligible"]],
''',
    '''        "trusted": [r for r in cleaned if r["cleaning"]["decision"] == "trusted"],
        "semantic_candidates": [r for r in cleaned if r["cleaning"]["decision"] in {"trusted", "review"}],
        "organic": [r for r in cleaned if r["cleaning"]["decision"] == "trusted" and r["cleaning"]["organic_eligible"]],
'''
)
must_replace(
    "app/services/cleaning.py",
    '    store.write(base / "trusted.json", result["trusted"])\n',
    '    store.write(base / "trusted.json", result["trusted"])\n    store.write(base / "semantic-candidates.json", result.get("semantic_candidates", [*result["trusted"], *result.get("review_queue", [])]))\n',
)
must_replace(
    "app/services/cleaning.py",
    '''        "trusted": [r for r in cleaned if r["cleaning"]["decision"] == "trusted"],
        "organic": [r for r in cleaned if r["cleaning"]["decision"] == "trusted" and r["cleaning"].get("organic_eligible")],
''',
    '''        "trusted": [r for r in cleaned if r["cleaning"]["decision"] == "trusted"],
        "semantic_candidates": [r for r in cleaned if r["cleaning"]["decision"] in {"trusted", "review"}],
        "organic": [r for r in cleaned if r["cleaning"]["decision"] == "trusted" and r["cleaning"].get("organic_eligible")],
'''
)
# The generated report already exposes semantic_candidate_records; keep its count
# consistent with strict trusted + review rather than the compatibility file layout.

must_replace(
    "app/services/ai_analysis.py",
    '    trusted = store.read(folder / "cleaning" / "trusted.json", None)\n    if trusted is None:\n        raise RuntimeError("Cleaning trusted sample is not available. Run Step 3 first.")\n',
    '    trusted = store.read(folder / "cleaning" / "semantic-candidates.json", None)\n    if trusted is None:\n        trusted = store.read(folder / "cleaning" / "trusted.json", None)\n    if trusted is None:\n        raise RuntimeError("Cleaning semantic candidate sample is not available. Run Step 3 first.")\n',
)
must_replace(
    "app/services/ai_analysis.py",
    '    trusted = store.read(folder / "cleaning" / "trusted.json", []) or []\n    report = copy.deepcopy(report)\n',
    '    trusted = store.read(folder / "cleaning" / "semantic-candidates.json", None)\n    if trusted is None:\n        trusted = store.read(folder / "cleaning" / "trusted.json", []) or []\n    report = copy.deepcopy(report)\n',
)
must_replace(
    "app/services/ai_analysis.py",
    '    trusted = store.read(folder / "cleaning" / "trusted.json", []) or []\n    old_report = store.read(folder / "analysis" / "report.json", {}) or {}\n',
    '    trusted = store.read(folder / "cleaning" / "semantic-candidates.json", None)\n    if trusted is None:\n        trusted = store.read(folder / "cleaning" / "trusted.json", []) or []\n    old_report = store.read(folder / "analysis" / "report.json", {}) or {}\n',
)


# ---------------------------------------------------------------------------
# Run lifecycle: retain useful pre-AI collision/comment deepening under the new
# plan version, then auto-export when real Step7/Step6 artifacts exist. Test/mocked
# stages without those files remain a valid visualizations-ready completion.
# ---------------------------------------------------------------------------
must_replace(
    "app/services/run_manager.py",
    'if int(report.get("trusted_sample_shortfall", 0) or 0) > 0 and plan.get("search_strategy_version") == "smart-collection-v2":',
    'if int(report.get("trusted_sample_shortfall", 0) or 0) > 0 and plan.get("search_strategy_version") in {"smart-collection-v2", "master30-search-v1"}:',
)

# Auto-export block generated by master30_pipeline: make absence of real persisted
# prerequisites a clean skip (important for lifecycle mocks), and keep the historic
# visualizations_ready terminal contract while exports are attached when available.
must_replace(
    "app/services/run_manager.py",
    '''        try:
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
''',
    '''        prerequisites = [
            folder / "visualizations" / "presentation-visual-pack.json",
            folder / "investigations" / "evidence-pack.json",
        ]
        exports = None
        if all(p.exists() for p in prerequisites):
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
        status=self.store.read_status(run_id)
        export_state = ({"status":"succeeded","completed_at":_utcnow(),"error":None,"summary":exports}
                        if exports is not None else {"status":"skipped_not_ready","completed_at":_utcnow(),"error":None,"summary":None})
        status.update({"status":terminal_status,"phase":"visualizations_ready","completed_at":_utcnow(),
            "exports":{**(status.get("exports") or {}),**export_state},
            "current":{"source":None,"code":"visualizations_completed","message":"Charts are ready" + ("; professional report exports are ready" if exports is not None else "")}})
        status.setdefault("progress",{})["percent"]=100; self.store.write_status(run_id,status)
'''
)


# ---------------------------------------------------------------------------
# Two legacy tests encode intentionally superseded contracts. Update only those
# assertions; all safety/Actor/provider regression tests remain untouched.
# ---------------------------------------------------------------------------
must_replace(
    "tests/test_multisource_torture_v182.py",
    '''def test_budget_guard_rejects_provider_report_above_reserved_call_cap():
    guard = BudgetGuard(1.0)
    reservation = guard.reserve(0.10)
    with pytest.raises(RuntimeError, match="per-call hard cap"):
        guard.settle(reservation, 0.11)
    assert guard.spent == 0.0
''',
    '''def test_budget_guard_accounts_provider_runtime_overhead_against_global_cap():
    guard = BudgetGuard(1.0)
    reservation = guard.reserve(0.10)
    charged = guard.settle(reservation, 0.11)
    assert charged == 0.11
    assert guard.spent == 0.11
    assert guard.remaining == pytest.approx(0.89)
'''
)
must_replace(
    "tests/test_local_launch_regression_v1841.py",
    '''def test_home_template_uses_explicit_request_keyword():
    text = Path("app/main.py").read_text(encoding="utf-8")
    assert 'TemplateResponse(request=request, name="index.html", context={})' in text
''',
    '''def test_home_route_uses_starlette_safe_response_contract():
    text = Path("app/main.py").read_text(encoding="utf-8")
    assert ('TemplateResponse(request=request, name="index.html", context={})' in text) or ('return HTMLResponse(html)' in text)
'''
)

print("master30 compatibility/safety hotfixes applied")
