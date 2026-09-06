from __future__ import annotations
import math
import re
import copy
from datetime import timedelta
from app.models import AnalysisDraft, CollectionPlan, SourcePlan, SubRunPlan
from app.registry import load_registry
from app.services.smart_collection import x_search_input, canonical_topic
from app.services.resilience import get_safe_batch_size
from app.services.source_capabilities import comments_forecast

GREEK_MAP = {
    "α":"a","ά":"a","β":"v","γ":"g","δ":"d","ε":"e","έ":"e","ζ":"z","η":"i","ή":"i",
    "θ":"th","ι":"i","ί":"i","ϊ":"i","ΐ":"i","κ":"k","λ":"l","μ":"m","ν":"n","ξ":"x",
    "ο":"o","ό":"o","π":"p","ρ":"r","σ":"s","ς":"s","τ":"t","υ":"y","ύ":"y","ϋ":"y","ΰ":"y",
    "φ":"f","χ":"ch","ψ":"ps","ω":"o","ώ":"o"
}

def uniq(items):
    out, seen = [], set()
    for raw in items:
        value = str(raw).strip()
        key = value.casefold()
        if value and key not in seen:
            out.append(value)
            seen.add(key)
    return out

def greeklish(term: str) -> str:
    result = "".join(GREEK_MAP.get(ch.lower(), ch) for ch in term)
    return result if result.casefold() != term.casefold() else ""

def hashtag(term: str) -> str:
    cleaned = re.sub(r"[^\w\u0370-\u03FF\u1F00-\u1FFF]+", "", term, flags=re.UNICODE)
    return cleaned.strip("_")

def allocate_equal(total: int, sources: list[str]) -> dict[str, int]:
    base, rem = divmod(total, len(sources))
    return {s: base + (1 if i < rem else 0) for i, s in enumerate(sources)}

def split_target(total: int, n: int) -> list[int]:
    """Split an integer target without creating zero-sized logical batches.

    A target of 1 across three query batches must remain one paid batch, not three
    one-item calls. This keeps requested sample semantics and budget envelopes exact.
    """
    total = max(0, int(total))
    n = max(0, int(n))
    if total <= 0 or n <= 0:
        return []
    n = min(n, total)
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]



def batched(values: list, size: int) -> list[list]:
    size = max(1, int(size))
    return [values[i:i+size] for i in range(0, len(values), size)]


def _preflight_forecast(draft: AnalysisDraft, plans: list[SourcePlan], registry: dict) -> dict:
    unverified = []
    source_rows = []
    for sp in plans:
        cfg = registry.get(sp.source, {})
        verified = cfg.get("actor_status") == "verified"
        if not verified:
            unverified.append(sp.source)
        source_rows.append({
            "source": sp.source,
            "actor_id": sp.actor_id,
            "actor_verified": verified,
            "planned_logical_batches": len(sp.subruns),
            "safe_failure_isolation": True,
            "exact_date_post_filter": any(bool(sr.exact_post_filter) for sr in sp.subruns),
            "comments": comments_forecast(sp.source, bool(draft.comments), cfg),
        })
    risk = "medium" if unverified else "low"
    if len(plans) >= 4 and unverified:
        risk = "medium-high"
    comment_rows = [row["comments"] for row in source_rows]
    comment_applicable_sources = [
        row["source"] for row in source_rows
        if row["comments"].get("status") != "not_applicable"
    ]
    comment_blockers = [
        row["source"] for row in source_rows
        if bool(draft.comments) and row["comments"].get("status") not in {"verified_available", "not_applicable"}
    ]
    return {
        "version": "collection-preflight-v1.8.4",
        "mode": "static_before_live_probe",
        "risk_level": risk,
        "selected_sources": [p.source for p in plans],
        "unverified_default_actors": unverified,
        "planned_actor_batches": sum(len(p.subruns) for p in plans),
        "failure_isolation": "per logical Actor batch and per source",
        "transient_recovery": "split multi-target batch -> retry smaller batch -> preserve successful partials",
        "budget_guard": "hard global cap plus per-logical-batch envelope",
        "sample_rule": "never fill unavailable relevance with junk; report shortfall",
        "query_safety": {
            "client_field_used_for_discovery": False,
            "market_only_queries_allowed": False,
            "context_expansions_must_be_topic_anchored": True,
            "adaptive_collision_detection": True,
            "live_probe_required_for_yield_forecast": True,
        },
        "sources": source_rows,
        "comments_coverage": {
            "requested": bool(draft.comments),
            "fully_live_verified": bool(draft.comments) and bool(comment_applicable_sources) and not comment_blockers,
            "applicable_sources": comment_applicable_sources,
            "verification_blockers": comment_blockers,
            "rule": "No source is allowed to claim comment coverage until its deepening path is live-smoke-verified.",
        },
        "live_yield_forecast_available": False,
        "note": "Actual yield and Actor stability require the later tiny paid smoke/preflight with verified credentials.",
    }

def build_terms(draft: AnalysisDraft):
    """Build auditable search terms without leaking administrative fields into discovery.

    ``client`` identifies who the analysis is for; it is never a search keyword.  The topic is the
    only unanchored discovery seed. User keywords and explicit additional context are treated as
    scoped context and are always joined back to the topic. This prevents a broad helper keyword
    such as ``OPAP`` or a market alias such as ``Ellada`` from swallowing a 500/1,000-item sample.

    A Greeklish form of the *topic itself* may be searched as a standalone alias. Greeklish forms
    of contextual Greek terms are added to context and remain topic-anchored.
    """
    topic = canonical_topic(str(draft.topic or "").strip(), str(draft.market or ""))
    core = uniq([topic])
    core_keys = {x.casefold() for x in core}

    if draft.market.casefold() == "greece":
        market_context = ["Greece", "Ελλάδα", "Ellada"]
    else:
        market_context = [draft.market]

    explicit_context = [*draft.keywords, *draft.additional_context]
    topic_fold = topic.casefold()
    original_topic_fold = str(draft.topic or "").strip().casefold()
    explicit_context = [
        x for x in explicit_context
        if str(x).strip().casefold() not in core_keys
        and str(x).strip().casefold() != original_topic_fold
        and str(x).strip().casefold() not in topic_fold
    ]
    context_variants = []
    for term in explicit_context:
        variant = greeklish(term)
        if variant:
            context_variants.append(variant)
    context = uniq([*market_context, *explicit_context, *context_variants])

    topic_glish = greeklish(topic)
    glish = uniq([topic_glish] if topic_glish else [])
    return core, context, glish

def base_queries(core: list[str], context: list[str], glish: list[str]) -> list[str]:
    """Return high-recall queries while keeping contextual expansions topic-anchored.

    Standalone queries are limited to the topic and its own spelling/transliteration aliases.
    Everything else is joined to the primary topic.
    """
    core = uniq(core)
    if not core:
        return []
    primary = core[0]
    out = [primary]
    out += glish[:3]
    out += [f"{primary} {c}" for c in context[:8] if c and c.casefold() != primary.casefold()]
    return uniq(out)[:12]


def news_queries_for_capacity(draft: AnalysisDraft, queries: list[str], target: int) -> list[str]:
    """Add only topic-anchored media-context variants when Google News needs more query capacity.

    The current public Actor schema limits ``maxArticles`` to 500 per query. For the
    normal SIGNALYTH presets (up to 3,000), we generate enough distinct, still
    topic-anchored query routes so no logical call asks one query for >500 rows.
    Cleaning/relevance still decides what is usable; these are discovery candidates,
    never a promise that the target exists.
    """
    primary = canonical_topic(str(draft.topic or "").strip(), str(draft.market or ""))
    needed = max(1, min(8, int(math.ceil(max(1, int(target)) / 500))))
    if len(queries) >= needed:
        return uniq(queries)[:8]
    extras = (
        [f"{primary} νέα", f"{primary} ανακοίνωση", f"{primary} συνέντευξη", f"{primary} συνεργασία",
         f"{primary} παρουσίαση", f"{primary} Ελλάδα", f"{primary} Greece", f"{primary} media"]
        if draft.market.casefold() == "greece" else
        [f"{primary} news", f"{primary} announcement", f"{primary} interview", f"{primary} partnership",
         f"{primary} launch", f"{primary} report", f"{primary} media", f"{primary} update"]
    )
    return uniq([*queries, *extras])[:max(needed, min(8, len(queries) + len(extras)))]


def per_source_targets(draft: AnalysisDraft) -> dict[str, int]:
    if draft.sample_mode == "perSource":
        return {s: int(draft.per_source.get(s, 0) or 0) for s in draft.sources}
    return allocate_equal(draft.sample_target, draft.sources)

def budget_for_subrun(source_budget: float, shares: list[int], idx: int) -> float:
    """Allocate a logical-batch cap without ever exceeding the source budget.

    We deliberately floor rather than round upward: a tiny user budget must not be
    silently inflated just because a source was split into several resilience batches.
    """
    total = sum(shares) or 1
    raw = max(0.0, float(source_budget)) * max(0, int(shares[idx])) / total
    return math.floor(raw * 100_000_000) / 100_000_000



def _generic_field_type(cfg: dict, field: str | None) -> str | None:
    if not field:
        return None
    spec = (cfg.get("input_schema_fields") or {}).get(field)
    if isinstance(spec, dict):
        return spec.get("type")
    if isinstance(spec, str):
        return spec
    existing = (cfg.get("input_template") or {}).get(field)
    if isinstance(existing, list):
        return "array"
    if isinstance(existing, bool):
        return "boolean"
    if isinstance(existing, int):
        return "integer"
    if isinstance(existing, float):
        return "number"
    if isinstance(existing, str):
        return "string"
    return None


def _set_generic_value(inp: dict, cfg: dict, semantic: str, value):
    mapping = cfg.get("input_mapping") or {}
    field = mapping.get(semantic)
    if not field or value is None:
        return
    typ = _generic_field_type(cfg, field)
    if typ == "array" and not isinstance(value, list):
        value = [value]
    elif typ == "string" and isinstance(value, list):
        value = value[0] if value else ""
    elif typ in {"integer", "number"}:
        try:
            value = int(value) if typ == "integer" else float(value)
        except Exception:
            return
    elif typ == "boolean":
        value = bool(value)
    inp[field] = value


def _generic_country_value(field: str, draft: AnalysisDraft):
    if draft.market.casefold() != "greece":
        return draft.market
    norm = re.sub(r"[^a-z]", "", field.casefold())
    if norm in {"gl", "countrycode"}:
        return "gr"
    if "location" in norm:
        return "Greece"
    return "GR"


def _generic_language_value(field: str, draft: AnalysisDraft):
    if draft.market.casefold() != "greece":
        return "en"
    norm = re.sub(r"[^a-z]", "", field.casefold())
    return "el" if norm in {"hl", "lang", "language", "languagecode"} else "el"



def instagram_discovery_tags(draft: AnalysisDraft) -> list[str]:
    """Return only topic-anchored hashtag candidates.

    Instagram hashtag routes cannot express boolean topic+context queries safely.
    A standalone secondary keyword (for example ``#fiber`` or ``#opap``) can swamp
    a brand analysis with unrelated content, so we never emit it by itself.
    """
    topic = canonical_topic(str(draft.topic or "").strip(), str(draft.market or ""))
    base = hashtag(topic)
    tags = [base] if base else []

    # A topic transliteration is a valid alias of the same subject, not a context leak.
    topic_glish = greeklish(topic)
    if topic_glish:
        glish_tag = hashtag(topic_glish)
        if glish_tag:
            tags.append(glish_tag)

    # At most one compound topic+focus hashtag. It remains explicitly anchored.
    focus = []
    base_fold = (base or "").casefold()
    for value in [*draft.keywords, *draft.additional_context]:
        raw = str(value or "").strip()
        if not raw:
            continue
        vf = raw.casefold()
        tf = topic.casefold()
        raw_tag = hashtag(raw).casefold()
        # Treat punctuation/spacing variants of the topic as aliases, not focus terms.
        if vf == tf or vf in tf or (base_fold and raw_tag == base_fold):
            continue
        compound = hashtag(f"{topic} {raw}")
        if compound and compound.casefold() != base_fold:
            focus.append(compound)
            break
    tags.extend(focus)
    return uniq(tags)[:2]

def make_generic_source_plan(source: str, target: int, draft: AnalysisDraft, queries: list[str], cfg: dict, source_budget: float) -> SourcePlan:
    """Build a verified replacement-Actor plan with conservative batch isolation."""
    mapping = cfg.get("input_mapping") or {}
    query_field = mapping.get("query")
    urls_field = mapping.get("urls")
    if not query_field and not (source == "instagram" and urls_field):
        raise ValueError(f"Verified Actor for {source} has no usable discovery input mapping.")

    base = copy.deepcopy(cfg.get("input_template") or {})
    rate = cfg.get("price_per_1000_hint")
    est = round(target * rate / 1000, 4) if rate is not None else None
    discovery_values = queries[:8] or [draft.topic]
    semantic = "query"
    if not query_field and source == "instagram" and urls_field:
        semantic = "urls"
        tags = instagram_discovery_tags(draft)
        discovery_values = [f"https://www.instagram.com/explore/tags/{tag.lower()}/" for tag in tags] or []

    discovery_field = mapping.get(semantic)
    field_type = _generic_field_type(cfg, discovery_field)
    safe_batch = get_safe_batch_size(source, cfg["actor_id"])
    if field_type == "array":
        chunks = batched(discovery_values, safe_batch)
    else:
        chunks = [[v] for v in discovery_values[:6]]
    chunks = chunks or [[draft.topic]]

    shares = split_target(target, len(chunks))
    subruns: list[SubRunPlan] = []
    for i, (chunk, share) in enumerate(zip(chunks, shares)):
        inp = copy.deepcopy(base)
        value = chunk if field_type == "array" else (chunk[0] if chunk else draft.topic)
        _set_generic_value(inp, cfg, semantic, value)
        _set_generic_value(inp, cfg, "max_items", max(1, share))
        _set_generic_value(inp, cfg, "date_from", draft.date_from.isoformat())
        _set_generic_value(inp, cfg, "date_to", draft.date_to.isoformat())
        country_field = mapping.get("country")
        if country_field:
            _set_generic_value(inp, cfg, "country", _generic_country_value(country_field, draft))
        language_field = mapping.get("language")
        if language_field:
            _set_generic_value(inp, cfg, "language", _generic_language_value(language_field, draft))
        _set_generic_value(inp, cfg, "comments", draft.comments)
        subruns.append(SubRunPlan(
            actor_id=cfg["actor_id"], input=inp, target_items=max(1, share),
            max_charge_usd=budget_for_subrun(source_budget, shares, i),
            exact_post_filter=True, post_filter_from=draft.date_from, post_filter_to=draft.date_to,
            purpose=f"generic_discovery_batch_{i+1}",
        ))

    return SourcePlan(
        source=source, actor_id=cfg["actor_id"], target_items=target, estimated_cost_usd=est,
        price_per_1000_hint=rate,
        date_strategy="mapped_native_plus_exact_post_filter" if mapping.get("date_from") and mapping.get("date_to") else "exact_post_filter",
        market_strategy="mapped_or_relevance_filter", queries=queries, subruns=subruns,
    )

def make_source_plan(source: str, target: int, draft: AnalysisDraft, queries: list[str], registry: dict, source_budget: float) -> SourcePlan:
    cfg = registry[source]
    if cfg.get("adapter_mode") == "generic":
        return make_generic_source_plan(source, target, draft, queries, cfg, source_budget)
    rate = cfg.get("price_per_1000_hint")
    est = round(target * rate / 1000, 4) if rate is not None else None
    until_exclusive = draft.date_to + timedelta(days=1)
    subruns: list[SubRunPlan] = []
    safe_batch = get_safe_batch_size(source, cfg["actor_id"])

    intent_buckets: list[dict] = []
    if source == "x":
        # Xquik supports multi-search input, but real-world upstream timeouts showed why
        # one large fan-out must never be a single point of failure. Plan small logical batches.
        full_input, intent_buckets = x_search_input(draft, target)
        query_batches = batched(list(full_input.get("searchTerms") or []), safe_batch)
        shares = split_target(target, max(1, len(query_batches)))
        for i, (batch, share) in enumerate(zip(query_batches, shares)):
            inp = {
                "mode": "search", "searchTerms": batch, "maxItems": max(1, share),
                "maxItemsPerTarget": max(1, math.ceil(max(1, share) / max(1, len(batch)))),
                "includeSearchTerms": True, "queryType": "Latest",
                "since": f"{draft.date_from.isoformat()}_00:00:00_UTC",
                "until": f"{until_exclusive.isoformat()}_00:00:00_UTC",
            }
            subruns.append(SubRunPlan(
                actor_id=cfg["actor_id"], input=inp, target_items=max(1, share),
                max_charge_usd=budget_for_subrun(source_budget, shares, i), exact_post_filter=True,
                post_filter_from=draft.date_from, post_filter_to=draft.date_to,
                purpose=f"balanced_intent_discovery_batch_{i+1}",
            ))

    elif source == "tiktok":
        batches = batched(queries[:8] or [draft.topic], safe_batch)
        shares = split_target(target, len(batches))
        for i, (batch, share) in enumerate(zip(batches, shares)):
            inp = {"search": batch, "maxItems": max(1, share), "location": "GR" if draft.market.casefold()=="greece" else None,
                   "dateRange": "ALL_TIME", "sortType": "RELEVANCE"}
            inp = {k:v for k,v in inp.items() if v is not None}
            subruns.append(SubRunPlan(
                actor_id=cfg["actor_id"], input=inp, target_items=max(1, share),
                max_charge_usd=budget_for_subrun(source_budget, shares, i), exact_post_filter=True,
                post_filter_from=draft.date_from, post_filter_to=draft.date_to, purpose=f"search_batch_{i+1}",
            ))

    elif source == "instagram":
        tags = instagram_discovery_tags(draft) or [hashtag(canonical_topic(draft.topic, draft.market))]
        jobs = [(tag, mode) for tag in tags for mode in ("posts", "reels")]
        shares = split_target(target, len(jobs))
        for i, ((tag, mode), share) in enumerate(zip(jobs, shares)):
            inp = {"directUrls": [f"https://www.instagram.com/explore/tags/{tag.lower()}/"], "resultsType": mode,
                   "resultsLimit": max(1, share), "onlyPostsNewerThan": draft.date_from.isoformat(), "addParentData": True}
            subruns.append(SubRunPlan(actor_id=cfg["actor_id"], input=inp, target_items=max(1, share),
                                      max_charge_usd=budget_for_subrun(source_budget, shares, i), exact_post_filter=True,
                                      post_filter_from=draft.date_from, post_filter_to=draft.date_to, purpose=mode))

    elif source == "facebook":
        q = queries[:4] or [draft.topic]
        shares = split_target(target, len(q))
        for i, (query, share) in enumerate(zip(q, shares)):
            inp = {"query": query, "resultsCount": max(1, share), "searchType": "latest",
                   "startDate": draft.date_from.isoformat(), "endDate": draft.date_to.isoformat()}
            subruns.append(SubRunPlan(actor_id=cfg["actor_id"], input=inp, target_items=max(1, share),
                                      max_charge_usd=budget_for_subrun(source_budget, shares, i), purpose=f"search_query_{i+1}"))

    elif source == "youtube":
        batches = batched(queries[:10] or [draft.topic], safe_batch)
        shares = split_target(target, len(batches))
        for i, (batch, share) in enumerate(zip(batches, shares)):
            inp = {"keywords": batch, "gl": "gr" if draft.market.casefold()=="greece" else "us",
                   "hl": "el" if draft.market.casefold()=="greece" else "en", "uploadDate": "all", "sort": "r", "maxItems": max(1, share)}
            subruns.append(SubRunPlan(actor_id=cfg["actor_id"], input=inp, target_items=max(1, share),
                                      max_charge_usd=budget_for_subrun(source_budget, shares, i), exact_post_filter=True,
                                      post_filter_from=draft.date_from, post_filter_to=draft.date_to, purpose=f"keyword_batch_{i+1}"))

    elif source == "news":
        news_queries = news_queries_for_capacity(draft, queries, target)
        batches = batched(news_queries[:8] or [draft.topic], safe_batch)
        shares = split_target(target, len(batches))
        for i, (batch, share) in enumerate(zip(batches, shares)):
            per_query = min(500, max(1, math.ceil(max(1, share) / max(len(batch), 1))))
            inp = {"queries": batch, "language": "el" if draft.market.casefold()=="greece" else "en-US",
                   "country": "GR" if draft.market.casefold()=="greece" else "US", "maxArticles": per_query,
                   "fromDate": draft.date_from.isoformat(), "toDate": draft.date_to.isoformat(), "resolveUrls": True}
            subruns.append(SubRunPlan(actor_id=cfg["actor_id"], input=inp, target_items=max(1, share),
                                      max_charge_usd=budget_for_subrun(source_budget, shares, i), purpose=f"news_batch_{i+1}"))

    return SourcePlan(source=source, actor_id=cfg["actor_id"], target_items=target, estimated_cost_usd=est,
                      price_per_1000_hint=rate, date_strategy=cfg["date_support"],
                      market_strategy=cfg["market_support"], queries=queries, subruns=subruns,
                      intent_buckets=intent_buckets if source == "x" else [])

def build_collection_plan(draft: AnalysisDraft) -> CollectionPlan:
    registry = load_registry()
    core, context, glish = build_terms(draft)
    queries = base_queries(core, context, glish) if draft.smart_search else uniq([draft.topic, *draft.keywords])
    targets = per_source_targets(draft)

    selected = [s for s in draft.sources if targets.get(s, 0) > 0]
    estimates = {}
    for source in selected:
        rate = registry[source].get("price_per_1000_hint")
        estimates[source] = None if rate is None else targets[source] * rate / 1000
    has_unknown = any(v is None for v in estimates.values())
    known_total = sum(v or 0 for v in estimates.values())
    budget_check = "unknown" if has_unknown else ("within_budget" if known_total <= draft.max_budget_usd else "estimate_over_budget")

    # Run caps are a second guard, independent of the estimate. Their total never exceeds the analysis budget.
    # Apify's run total can include platform/runtime usage in addition to Actor event charges,
    # so tiny samples need operational headroom instead of micro-cent source envelopes.
    if known_total > 0:
        budget = max(0.0, float(draft.max_budget_usd))
        if budget <= known_total:
            source_budgets = {s: budget * ((estimates[s] or 0) / known_total) for s in selected}
        else:
            buffered_total = min(budget, known_total * 1.25)
            source_budgets = {s: buffered_total * ((estimates[s] or 0) / known_total) for s in selected}
            spare = max(0.0, budget - sum(source_budgets.values()))
            if selected and spare > 0:
                overhead_each = spare / len(selected)
                source_budgets = {s: source_budgets[s] + overhead_each for s in selected}
    else:
        source_budgets = allocate_equal(int(round(draft.max_budget_usd * 10000)), selected)
        source_budgets = {s: v / 10000 for s, v in source_budgets.items()}

    plans = [make_source_plan(s, targets[s], draft, queries, registry, max(0.0, source_budgets[s])) for s in selected]
    estimated = None if has_unknown else round(known_total, 4)
    preflight = _preflight_forecast(draft, plans, registry)
    return CollectionPlan(client=draft.client, topic=draft.topic, market=draft.market, date_from=draft.date_from, date_to=draft.date_to,
                          sample_mode=draft.sample_mode, target_total=sum(p.target_items for p in plans), estimated_cost_usd=estimated,
                          max_budget_usd=draft.max_budget_usd, comments_requested=draft.comments,
                          deepening_strategy="important_content_only" if draft.comments else "disabled", budget_check=budget_check,
                          rebalancing_enabled=(draft.sample_mode == "automatic"), rebalance_max_rounds=2,
                          core_terms=core, context_terms=context, greeklish_variants=glish, exclusions=draft.exclusions,
                          resilience_policy_version="multisource-resilience-v1.8.4", preflight_forecast=preflight, sources=plans)
