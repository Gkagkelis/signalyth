from __future__ import annotations

import copy
import math
import re
from datetime import date, timedelta

from app.models import AnalysisDraft, CollectionPlan, SourcePlan, SubRunPlan
from app.registry import load_registry
from app.services.smart_collection import x_search_input, canonical_topic
from app.services.source_capabilities import comments_forecast
from app.services.resilience import get_safe_batch_size

GREEK_MAP = {
    "α":"a","ά":"a","β":"v","γ":"g","δ":"d","ε":"e","έ":"e","ζ":"z","η":"i","ή":"i",
    "θ":"th","ι":"i","ί":"i","ϊ":"i","ΐ":"i","κ":"k","λ":"l","μ":"m","ν":"n","ξ":"x",
    "ο":"o","ό":"o","π":"p","ρ":"r","σ":"s","ς":"s","τ":"t","υ":"y","ύ":"y","ϋ":"y","ΰ":"y",
    "φ":"f","χ":"ch","ψ":"ps","ω":"o","ώ":"o"
}


def uniq(items):
    out, seen = [], set()
    for raw in items:
        value = str(raw or "").strip()
        key = value.casefold()
        if value and key not in seen:
            out.append(value); seen.add(key)
    return out


def greeklish(term: str) -> str:
    result = "".join(GREEK_MAP.get(ch.lower(), ch) for ch in str(term or ""))
    return result if result.casefold() != str(term or "").casefold() else ""


def hashtag(term: str) -> str:
    cleaned = re.sub(r"[^\w\u0370-\u03FF\u1F00-\u1FFF]+", "", str(term or ""), flags=re.UNICODE)
    return cleaned.strip("_")


def allocate_equal(total: int, sources: list[str]) -> dict[str, int]:
    if not sources: return {}
    base, rem = divmod(max(0, int(total)), len(sources))
    return {s: base + (1 if i < rem else 0) for i, s in enumerate(sources)}


def split_target(total: int, n: int) -> list[int]:
    total, n = max(0, int(total)), max(0, int(n))
    if total <= 0 or n <= 0: return []
    n = min(n, total); base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def batched(values: list, size: int) -> list[list]:
    size = max(1, int(size)); return [values[i:i+size] for i in range(0, len(values), size)]


def _role_map(draft: AnalysisDraft) -> dict[str, str]:
    result = {}
    for k, v in (draft.keyword_roles or {}).items():
        term = str(k or "").strip()
        if term: result[term.casefold()] = str(v)
    return result


def _role_terms(draft: AnalysisDraft, role: str) -> list[str]:
    roles = _role_map(draft)
    values = []
    for term in [*draft.keywords, *draft.additional_context]:
        default = "context"
        if roles.get(str(term).casefold(), default) == role:
            values.append(term)
    return uniq(values)


def build_terms(draft: AnalysisDraft):
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
    return [topic], context, topic_aliases

def base_queries(core: list[str], context: list[str], glish: list[str]) -> list[str]:
    if not core: return []
    primary = core[0]
    return uniq([primary, *glish, *[f"{primary} {c}" for c in context if c and c.casefold() != primary.casefold()]])[:12]


def _research_routes(draft: AnalysisDraft) -> dict:
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
        primary = [topic]
        topups = uniq([*aliases, *anchored_required, *anchored_context, *anchored_watch, *anchored_market])
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
    }

def _prioritized_source_queries(draft: AnalysisDraft, queries: list[str], limit: int) -> list[str]:
    routes = _research_routes(draft)
    return uniq([*routes["primary"], *routes["topups"], *queries])[:max(1, int(limit))]


def _facebook_safe_query(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) <= 100: return value
    clipped = value[:100].rstrip(); return clipped.rsplit(" ", 1)[0].rstrip() if " " in clipped else clipped


def _facebook_queries(draft: AnalysisDraft, queries: list[str], limit: int = 6) -> list[str]:
    return uniq([_facebook_safe_query(q) for q in _prioritized_source_queries(draft, queries, limit) if q])[:limit]


def _tiktok_date_range(draft: AnalysisDraft, today: date | None = None) -> str:
    today = today or date.today()
    if draft.date_from > today: return "ALL_TIME"
    age = max(0, (today - draft.date_from).days)
    if age <= 1: return "YESTERDAY"
    if draft.date_from.isocalendar()[:2] == today.isocalendar()[:2]: return "THIS_WEEK"
    if draft.date_from.year == today.year and draft.date_from.month == today.month: return "THIS_MONTH"
    if age <= 90: return "LAST_THREE_MONTHS"
    if age <= 180: return "LAST_SIX_MONTHS"
    return "ALL_TIME"


def _youtube_upload_date(draft: AnalysisDraft, today: date | None = None) -> str:
    today = today or date.today()
    if draft.date_from > today: return "all"
    age = max(0, (today - draft.date_from).days)
    if age <= 1: return "t"
    if age <= 7: return "w"
    if age <= 31: return "m"
    if age <= 366: return "y"
    return "all"


def news_queries_for_capacity(draft: AnalysisDraft, queries: list[str], target: int) -> list[str]:
    topic = canonical_topic(draft.topic, draft.market)
    needed = max(1, min(8, int(math.ceil(max(1, int(target)) / 500))))
    extras = [f"{topic} νέα", f"{topic} ανακοίνωση", f"{topic} συνέντευξη", f"{topic} media"] if draft.market.casefold()=="greece" else [f"{topic} news", f"{topic} announcement", f"{topic} interview", f"{topic} media"]
    return uniq([*queries, *extras])[:max(needed, min(8, len(queries)+len(extras)))]


def per_source_targets(draft: AnalysisDraft) -> dict[str, int]:
    if draft.sample_mode == "perSource":
        return {s: int(draft.per_source.get(s, 0) or 0) for s in draft.sources}
    return allocate_equal(draft.sample_target, draft.sources)


def budget_for_subrun(source_budget: float, shares: list[int], idx: int) -> float:
    total = sum(shares) or 1
    raw = max(0.0, float(source_budget)) * max(0, int(shares[idx])) / total
    return math.floor(raw * 100_000_000) / 100_000_000


def instagram_discovery_tags(draft: AnalysisDraft) -> list[str]:
    routes = _research_routes(draft); topic = routes["topic"]
    tags = [hashtag(topic)]
    for alias in routes["aliases"][:1]: tags.append(hashtag(alias))
    for term in [*routes["required"], *routes["contexts"]][:2]: tags.append(hashtag(f"{topic} {term}"))
    return uniq([t for t in tags if t])[:4]


def _sub(actor: str, inp: dict, target: int, cap: float, purpose: str, draft: AnalysisDraft, exact=True):
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
    )

def _caps(source_budget: float, n_topups: int) -> tuple[float, list[float]]:
    budget = max(0.0, float(source_budget))
    if n_topups <= 0:
        return budget, []
    primary = budget * 0.70
    remain = max(0.0, budget - primary)
    return primary, [remain / n_topups for _ in range(n_topups)]

def _query_preview(source: str, primary: list[str], topups: list[str], draft: AnalysisDraft) -> list[dict]:
    rows = []
    for i, q in enumerate(primary): rows.append({"route":"primary","query":q,"order":i+1,"editable":True})
    for i, q in enumerate(topups): rows.append({"route":"topup","query":q,"order":len(primary)+i+1,"editable":True})
    for row in rows:
        row.update({"source":source,"date_from":draft.date_from.isoformat(),"date_to":draft.date_to.isoformat(),"strategy":draft.search_strategy})
    return rows


def _apply_override(draft: AnalysisDraft, source: str, primary: list[str], topups: list[str]) -> tuple[list[str], list[str]]:
    overrides = uniq((draft.query_overrides or {}).get(source) or [])
    return (overrides[:5], overrides[5:]) if overrides else (primary, topups)


def make_generic_source_plan(source: str, target: int, draft: AnalysisDraft, queries: list[str], cfg: dict, source_budget: float) -> SourcePlan:
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
    )

def make_source_plan(source: str, target: int, draft: AnalysisDraft, queries: list[str], registry: dict, source_budget: float) -> SourcePlan:
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
        # Balanced X discovery consistently excludes native retweets, including the
        # broad/topic routes added ahead of the intent catalog.
        primary = [q if "-filter:nativeretweets" in q else f"{q} -filter:nativeretweets" for q in primary]

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
    )

def _preflight_forecast(draft: AnalysisDraft, plans: list[SourcePlan], registry: dict) -> dict:
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
    }

def build_collection_plan(draft: AnalysisDraft) -> CollectionPlan:
    registry=load_registry(); core,context,aliases=build_terms(draft); queries=base_queries(core,context,aliases) if draft.smart_search else uniq([draft.topic,*draft.keywords])
    targets=per_source_targets(draft); selected=[s for s in draft.sources if targets.get(s,0)>0]
    estimates={s:(None if registry[s].get("price_per_1000_hint") is None else targets[s]*registry[s].get("price_per_1000_hint")/1000) for s in selected}
    has_unknown=any(v is None for v in estimates.values()); known_total=sum(v or 0 for v in estimates.values())
    budget_check="unknown" if has_unknown else ("within_budget" if known_total<=draft.max_budget_usd else "estimate_over_budget")
    # Allocate the user's acquisition budget across sources. Source-specific pricing is enforced by Apify's charge cap plus the global guard.
    if selected:
        weights={s:max(0.05,float(estimates[s] or 0.05)) for s in selected}; den=sum(weights.values())
        source_budgets={s:float(draft.max_budget_usd)*weights[s]/den for s in selected}
    else: source_budgets={}
    plans=[make_source_plan(s,targets[s],draft,queries,registry,source_budgets[s]) for s in selected]
    preview={p.source:p.query_preview for p in plans}
    return CollectionPlan(client=draft.client,topic=draft.topic,market=draft.market,date_from=draft.date_from,date_to=draft.date_to,report_language=draft.report_language,media_handling=draft.media_handling,owned_accounts=list(draft.owned_accounts or []),media_accounts=list(draft.media_accounts or []),
                          sample_mode=draft.sample_mode,target_total=sum(p.target_items for p in plans),estimated_cost_usd=None if has_unknown else round(known_total,4),
                          max_budget_usd=draft.max_budget_usd,comments_requested=draft.comments,deepening_strategy="important_content_only" if draft.comments else "disabled",
                          budget_check=budget_check,rebalancing_enabled=(draft.sample_mode=="automatic"),rebalance_max_rounds=2,
                          core_terms=core,context_terms=context,greeklish_variants=aliases,exclusions=uniq([*draft.exclusions,*_role_terms(draft,"exclude")]),
                          search_strategy_version="master30-search-v1",target_semantics="requested_final_analyzable_unique_in_range",
                          resilience_policy_version="multisource-resilience-master30-v1",preflight_forecast=_preflight_forecast(draft,plans,registry),sources=plans,
                          search_strategy=draft.search_strategy,keyword_roles={str(k):str(v) for k,v in (draft.keyword_roles or {}).items()},query_preview=preview,
                          topup_policy="shared_source_target_until_analyzable_or_exhausted",master_spec_version="SIGNALYTH-master30-v1",benchmark=draft.benchmark)
