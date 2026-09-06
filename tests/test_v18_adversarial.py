from __future__ import annotations

from types import SimpleNamespace
import pytest

from app.services import integrations as I


class _Build:
    def __init__(self, openapi=None, data=None):
        self.openapi = openapi or {}
        self.data = data or {}
    def get_open_api_definition(self):
        return self.openapi
    def get(self):
        return self.data


class _Actor:
    def __init__(self, data=None, *, build=None, validation=True, run=None):
        self.data = data
        self.build = build or _Build()
        self.validation = validation
        self.run = run
    def get(self):
        return self.data
    def default_build(self):
        return self.build
    def validate_input(self, run_input):
        if isinstance(self.validation, Exception):
            raise self.validation
        return self.validation
    def call(self, **kwargs):
        if isinstance(self.run, Exception):
            raise self.run
        return self.run


class _Dataset:
    def __init__(self, items):
        self.items = items
    def list_items(self, limit=None):
        return SimpleNamespace(items=list(self.items)[:limit])


class _Client:
    def __init__(self, actor, items=None):
        self.actor_obj = actor
        self.items = items or []
    def actor(self, ref):
        return self.actor_obj
    def dataset(self, ref):
        return _Dataset(self.items)


def test_invalid_non_apify_url_is_rejected_before_network_use():
    with pytest.raises(ValueError):
        I.parse_actor_reference("https://evil.example/owner/actor")


def test_private_or_not_found_actor_is_not_staged_as_valid():
    with pytest.raises(I.IntegrationError, match="not found|not accessible"):
        I.lookup_actor("owner/missing", client=_Client(_Actor(data=None)))


def test_schema_unavailable_is_explicit_not_silently_invented():
    actor = {"id":"a1", "username":"owner", "name":"bare", "title":"Bare"}
    result = I.lookup_actor("owner/bare", client=_Client(_Actor(data=actor, build=_Build())))
    assert result["schema_found"] is False
    assert result["input_mapping_suggestion"] == {}


def test_malformed_actor_input_validation_fails_safely():
    actor = _Actor(data={"id":"a1"}, validation=RuntimeError("bad input"))
    with pytest.raises(I.IntegrationError, match="validation failed"):
        I.validate_actor_input("owner/actor", {"query": {"wrong":"shape"}}, client=_Client(actor))


def test_paid_actor_run_failure_does_not_become_verified():
    actor = _Actor(data={"id":"a1"}, run=RuntimeError("provider failed"))
    with pytest.raises(I.IntegrationError, match="failed safely"):
        I.smoke_test_actor("owner/actor", {"query":"x"}, client=_Client(actor))


def test_missing_date_is_reported_as_mapping_warning():
    inferred = I.infer_output_mapping([{"text":"hello", "url":"https://example.test/1"}])
    assert inferred["mapping"]["text"] == "text"
    assert inferred["mapping"]["url"] == "url"
    assert "date" not in inferred["mapping"]
    assert any("date" in w for w in inferred["warnings"])


def test_smoke_cost_cap_rejects_unsafe_values_before_paid_call():
    actor = _Actor(data={"id":"a1"})
    client = _Client(actor)
    with pytest.raises(ValueError, match="at most 2 USD"):
        I.smoke_test_actor("owner/actor", {"query":"x"}, max_charge_usd=2.01, client=client)


def test_no_token_behavior_is_safe(monkeypatch):
    old_a, old_o = I.settings.apify_token, I.settings.openai_api_key
    monkeypatch.setattr(I.settings, "apify_token", "")
    monkeypatch.setattr(I.settings, "openai_api_key", "")
    try:
        with pytest.raises(I.IntegrationNotConfigured, match="Apify is not connected"):
            I.test_apify_connection()
        with pytest.raises(I.IntegrationNotConfigured, match="OpenAI is not connected"):
            I.test_openai_connection()
    finally:
        I.settings.apify_token, I.settings.openai_api_key = old_a, old_o
