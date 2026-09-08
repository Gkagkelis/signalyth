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
    max_budget_usd: float = Field(default=5.0, gt=0, le=10000)
    smart_search: bool = True
    report_language: Literal["English", "Ελληνικά"] = "English"
    media_handling: Literal["blended", "separate", "exclude"] = "blended"
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
            if sum(active.values()) <= 0:
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
    topup_policy: str = "shared_source_target_until_analyzable_or_exhausted"
    master_spec_version: str = "SIGNALYTH-master30-v1"
    benchmark: dict[str, object] | None = None
    report_language: str = "English"
    media_handling: Literal["blended", "separate", "exclude"] = "blended"
    sources: list[SourcePlan]

class SourceConfigUpdate(BaseModel):
    actor_id: str | None = Field(default=None, min_length=3, max_length=200)
    locked: bool | None = None
    enabled: bool | None = None
    price_per_1000_hint: float | None = Field(default=None, ge=0, le=10000)


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
