from __future__ import annotations
from datetime import date
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

SourceName = Literal["x", "tiktok", "instagram", "facebook", "youtube", "news"]
SampleMode = Literal["automatic", "perSource"]
SearchStrategy = Literal["topic_first", "context_first", "balanced_smart"]
KeywordRole = Literal["context", "required_context", "alias", "exclude", "watch"]

class AnalysisDraft(BaseModel):
    client: str = Field(min_length=1, max_length=120)
    topic: str = Field(min_length=1, max_length=160)
    market: str = Field(min_length=1, max_length=80)
    date_from: date
    date_to: date
    keywords: list[str] = Field(default_factory=list, max_length=50)
    sources: list[SourceName] = Field(min_length=1)
    sample_mode: SampleMode = "automatic"
    sample_target: int = Field(default=1000, ge=1, le=100000)
    per_source: dict[str, int] = Field(default_factory=dict)
    comments: bool = False
    # Comments are requested evidence with their own per-source target, exactly
    # like posts. Before this, the comment layer silently received 35% of the
    # post target, which made the real conversation a rounding error.
    per_source_comments: dict[str, int] = Field(default_factory=dict)
    # Operator-supplied parent posts whose comments must be collected first,
    # ahead of anything discovery ranked.
    comment_seed_urls: list[str] = Field(default_factory=list, max_length=100)
    # Pages/accounts the operator names per source. The conversation about a
    # brand very often lives under the brand's OWN posts, and no comment Actor
    # accepts a page — so the page is turned into its posts first, and those
    # posts feed the comment layer.
    source_pages: dict[str, list[str]] = Field(default_factory=dict)
    #: Share of each source's comment target reserved for those pages. The rest
    #: goes to open search; whatever open search cannot fill comes back here, so
    #: the layer is never left half empty.
    owned_share_pct: int = Field(default=60, ge=0, le=100)
    max_budget_usd: float = Field(default=5.0, gt=0, le=10000)
    smart_search: bool = True
    report_language: Literal["English", "Ελληνικά"] = "English"
    research_type: Literal["market", "political"] = "market"
    media_handling: Literal["blended", "separate", "exclude"] = "blended"
    owned_accounts: list[str] = Field(default_factory=list, max_length=60)
    media_accounts: list[str] = Field(default_factory=list, max_length=120)
    client_logo: str | None = Field(default=None, max_length=2_200_000)
    brand_accent: str | None = Field(default=None, max_length=16)
    additional_context: list[str] = Field(default_factory=list, max_length=50)
    exclusions: list[str] = Field(default_factory=list, max_length=50)
    search_strategy: SearchStrategy = "balanced_smart"
    keyword_roles: dict[str, KeywordRole] = Field(default_factory=dict)
    query_overrides: dict[str, list[str]] = Field(default_factory=dict)
    benchmark: dict[str, object] | None = None

    @field_validator("keywords", "additional_context", "exclusions")
    @classmethod
    def clean_terms(cls, value: list[str]) -> list[str]:
        out, seen = [], set()
        for raw in value:
            term = str(raw).strip()
            key = term.casefold()
            if term and key not in seen:
                out.append(term)
                seen.add(key)
        return out

    @model_validator(mode="after")
    def validate_scope(self):
        if self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources must be unique")
        if self.sample_mode == "perSource":
            active = {s: int(self.per_source.get(s, 0) or 0) for s in self.sources}
            if any(v < 0 for v in active.values()):
                raise ValueError("per-source targets cannot be negative")
            comment_active = {s: int(self.per_source_comments.get(s, 0) or 0) for s in self.sources}
            if any(v < 0 for v in comment_active.values()):
                raise ValueError("per-source comment targets cannot be negative")
            # A run may legitimately be comment-only: the posts it needs are just
            # the parents it must find in order to reach them.
            if sum(active.values()) + sum(comment_active.values()) <= 0:
                raise ValueError("per-source sample total must be greater than zero")
        return self

class SubRunPlan(BaseModel):
    actor_id: str
    input: dict
    target_items: int
    max_charge_usd: float
    exact_post_filter: bool = False
    post_filter_from: date | None = None
    post_filter_to: date | None = None
    purpose: str = "discovery"
    resilience_policy: str = "adaptive-isolation-v1"
    max_attempt_calls: int = Field(default=6, ge=1, le=12)

class SourcePlan(BaseModel):
    source: SourceName
    actor_id: str
    target_items: int
    estimated_cost_usd: float | None
    price_per_1000_hint: float | None = None
    date_strategy: str
    market_strategy: str
    queries: list[str]
    subruns: list[SubRunPlan]
    topup_subruns: list[SubRunPlan] = Field(default_factory=list)
    semantic_topup_subruns: list[SubRunPlan] = Field(default_factory=list)
    intent_buckets: list[dict] = Field(default_factory=list)
    query_preview: list[dict] = Field(default_factory=list)
    source_budget_usd: float = 0.0
    source_contract_version: str = "actor-contract-v3"

class CollectionPlan(BaseModel):
    client: str
    topic: str
    market: str
    date_from: date
    date_to: date
    sample_mode: SampleMode = "automatic"
    target_total: int
    estimated_cost_usd: float | None
    max_budget_usd: float
    comments_requested: bool = False
    #: Per-source comment target the operator set, in comments. This is the
    #: authoritative figure for the comment layer — no derived percentage.
    per_source_comments: dict[str, int] = Field(default_factory=dict)
    comment_target_total: int = 0
    #: Parent posts the operator supplied; collected before ranked parents.
    comment_seed_urls: list[str] = Field(default_factory=list, max_length=100)
    #: Operator-named pages per source, and how much of the comment target they own.
    source_pages: dict[str, list[str]] = Field(default_factory=dict)
    owned_share_pct: int = Field(default=60, ge=0, le=100)
    deepening_strategy: str = "important_content_only"
    budget_check: Literal["within_budget", "estimate_over_budget", "unknown"]
    rebalancing_enabled: bool = True
    rebalance_max_rounds: int = Field(default=2, ge=0, le=5)
    core_terms: list[str]
    context_terms: list[str]
    greeklish_variants: list[str]
    exclusions: list[str]
    search_strategy_version: str = "smart-collection-v2"
    target_semantics: str = "requested_analyzable_evidence"
    resilience_policy_version: str = "multisource-resilience-v1.8.4"
    preflight_forecast: dict = Field(default_factory=dict)
    search_strategy: SearchStrategy = "balanced_smart"
    keyword_roles: dict[str, str] = Field(default_factory=dict)
    query_preview: dict[str, list[dict]] = Field(default_factory=dict)
    # Advisory only: names the public may use for the subject. Shown to the
    # operator before the run; NEVER searched or measured unless they add them.
    subject_name_suggestions: list[str] = Field(default_factory=list)
    topup_policy: str = "shared_source_target_until_analyzable_or_exhausted"
    master_spec_version: str = "SIGNALYTH-master30-v1"
    benchmark: dict[str, object] | None = None
    report_language: str = "English"
    research_type: Literal["market", "political"] = "market"
    media_handling: Literal["blended", "separate", "exclude"] = "blended"
    owned_accounts: list[str] = Field(default_factory=list, max_length=60)
    media_accounts: list[str] = Field(default_factory=list, max_length=120)
    client_logo: str | None = Field(default=None, max_length=2_200_000)
    brand_accent: str | None = Field(default=None, max_length=16)
    sources: list[SourcePlan]

class SourceConfigUpdate(BaseModel):
    actor_id: str | None = Field(default=None, min_length=3, max_length=200)
    locked: bool | None = None
    enabled: bool | None = None
    price_per_1000_hint: float | None = Field(default=None, ge=0, le=10000)
    # Comment/reply collection is a second evidence layer, independently switchable
    # from the primary discovery Actor. Curated production contracts may be
    # enabled directly; the per-run Comments switch remains the paid opt-in.
    comment_enabled: bool | None = None
    # Some Actors refuse to start below a fixed minimum run charge; these floors
    # lift per-call caps to it (a cap, not a charge). Absent from this model,
    # the PATCH body silently dropped them and the registry never learned the
    # minimums — run 20260926T121905Z failed TikTok and Facebook a second time.
    price_min_charge_usd: float | None = Field(default=None, ge=0, le=100)
    comment_price_min_charge_usd: float | None = Field(default=None, ge=0, le=100)
    comment_price_per_1000_hint: float | None = Field(default=None, ge=0, le=10000)
    comment_max_per_parent: int | None = Field(default=None, ge=1, le=1000)
    comment_max_parents: int | None = Field(default=None, ge=1, le=100)
    comment_include_replies: bool | None = None


class ReviewDecision(BaseModel):
    action: Literal["keep", "exclude"]
    note: str = Field(default="", max_length=1000)
    account_type: Literal["brand_owned", "media", "organization", "person_or_creator", "unknown"] | None = None
    content_class: Literal["owned", "news", "repost", "promotional", "organic", "unknown"] | None = None

class AIReviewDecision(BaseModel):
    action: Literal["keep", "exclude"]
    note: str = Field(default="", max_length=1000)
    sentiment_label: Literal["positive", "negative", "neutral", "mixed"] | None = None
    sentiment_score: float | None = Field(default=None, ge=-1, le=1)
    primary_emotion: Literal["joy", "anger", "sadness", "fear", "disgust", "surprise", "neutral"] | None = None
    target_stance: Literal["supportive", "critical", "neutral", "mixed", "not_applicable"] | None = None
    topic: str | None = Field(default=None, min_length=1, max_length=120)
    narrative: str | None = Field(default=None, min_length=1, max_length=240)
    sarcasm: bool | None = None

class CredentialUpdate(BaseModel):
    apify_token: str | None = Field(default=None, min_length=12, max_length=500)
    openai_api_key: str | None = Field(default=None, min_length=20, max_length=500)
    clear_apify: bool = False
    clear_openai: bool = False
    ai_enabled: bool | None = None
    live_collection_enabled: bool | None = None
    bulk_model: str | None = Field(default=None, min_length=2, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    reasoning_model: str | None = Field(default=None, min_length=2, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")


class ActorLookupRequest(BaseModel):
    actor_ref: str = Field(min_length=3, max_length=500)


class ActorProbeRequest(BaseModel):
    query: str = Field(default="SIGNALYTH", min_length=1, max_length=200)
    date_from: date | None = None
    date_to: date | None = None
    country: str = Field(default="GR", min_length=1, max_length=80)
    language: str = Field(default="el", min_length=1, max_length=30)
    comments: bool = False
    max_items: int = Field(default=3, ge=1, le=10)
    max_charge_usd: float = Field(default=0.10, gt=0, le=2.0)
    run_paid_smoke_test: bool = False
    input_overrides: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_probe_dates(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        return self


class CommentRouteSmokeRequest(BaseModel):
    seed_refs: list[str] = Field(min_length=1, max_length=20)
    actor_ref: str | None = Field(default=None, min_length=3, max_length=500)
    max_items: int = Field(default=3, ge=1, le=10)
    max_charge_usd: float = Field(default=0.10, gt=0, le=2.0)
    confirm_paid_smoke_test: bool = False
    input_overrides: dict[str, object] = Field(default_factory=dict)

    @field_validator("seed_refs")
    @classmethod
    def clean_seed_refs(cls, value: list[str]) -> list[str]:
        out, seen = [], set()
        for raw in value:
            ref = str(raw or "").strip()
            if ref and ref not in seen:
                out.append(ref)
                seen.add(ref)
        if not out:
            raise ValueError("At least one non-empty seed reference is required")
        return out


class ActorMappingUpdate(BaseModel):
    output_mapping: dict[str, str] = Field(default_factory=dict)

    @field_validator("output_mapping")
    @classmethod
    def clean_output_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {"text","date","author","followers","views","likes","comments","shares","url","content_type","parent_post"}
        out: dict[str, str] = {}
        for key, path in value.items():
            if key in allowed and str(path or "").strip():
                out[key] = str(path).strip()
        return out
