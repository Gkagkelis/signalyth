from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import integrations as I


class FakeBuild:
    def __init__(self, openapi=None, data=None):
        self.openapi = openapi or {}
        self.data = data or {}
    def get_open_api_definition(self):
        return self.openapi
    def get(self):
        return self.data


class FakeActor:
    def __init__(self, actor=None, build=None, valid=True, run=None):
        self.actor = actor or {}
        self.build = build or FakeBuild()
        self.valid = valid
        self.run = run
        self.validated_inputs = []
        self.calls = []
    def get(self):
        return self.actor
    def default_build(self):
        return self.build
    def validate_input(self, run_input):
        self.validated_inputs.append(run_input)
        if self.valid is Exception:
            raise RuntimeError("bad input")
        return bool(self.valid)
    def call(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.run, Exception):
            raise self.run
        return self.run


class FakeDataset:
    def __init__(self, items): self.items = items
    def list_items(self, limit=None):
        return SimpleNamespace(items=list(self.items)[:limit])


class FakeClient:
    def __init__(self, actor: FakeActor | None = None, items=None, user=None):
        self.actor_obj = actor or FakeActor()
        self.items = items or []
        self.user_obj = user or {"username": "sig-test"}
        self.actor_refs = []
        self.dataset_refs = []
    def actor(self, ref):
        self.actor_refs.append(ref)
        return self.actor_obj
    def dataset(self, ref):
        self.dataset_refs.append(ref)
        return FakeDataset(self.items)
    def user(self):
        outer = self
        class U:
            def get(self): return outer.user_obj
        return U()


def test_parse_actor_reference_supported_shapes():
    assert I.parse_actor_reference("apify/instagram-scraper").normalized == "apify/instagram-scraper"
    assert I.parse_actor_reference("apify~instagram-scraper").normalized == "apify/instagram-scraper"
    assert I.parse_actor_reference("https://apify.com/apify/instagram-scraper").api_id == "apify~instagram-scraper"
    assert I.parse_actor_reference("https://console.apify.com/actors/AbCdEf123").normalized == "AbCdEf123"
    with pytest.raises(ValueError):
        I.parse_actor_reference("https://example.com/not-apify")
    with pytest.raises(ValueError):
        I.parse_actor_reference("too/many/path/parts")


def test_openapi_schema_refs_and_mapping():
    openapi = {
        "paths": {"/v2/acts/x/runs": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Input"}}}}}}},
        "components": {"schemas": {"Input": {"type": "object", "required": ["queries"], "properties": {
            "queries": {"type": "array", "items": {"type": "string"}},
            "maxItems": {"type": "integer"},
            "startDate": {"type": "string"},
            "endDate": {"type": "string"},
            "includeComments": {"type": "boolean"},
        }}}},
    }
    schema = I._json_schema_from_openapi(openapi)
    mapping = I.suggest_input_mapping(schema)
    assert mapping["query"] == "queries"
    assert mapping["max_items"] == "maxItems"
    assert mapping["date_from"] == "startDate"
    assert mapping["date_to"] == "endDate"
    assert mapping["comments"] == "includeComments"
    probe, unresolved, _ = I.build_probe_input(schema, {"query": "Vodafone", "max_items": 3, "comments": True})
    assert probe["queries"] == ["Vodafone"]
    assert probe["maxItems"] == 3
    assert probe["includeComments"] is True
    assert unresolved == []


def test_example_input_fallback_enables_mapping_when_schema_missing():
    actor = {
        "id": "id1", "username": "owner", "name": "searcher", "title": "Searcher",
        "exampleRunInput": {"body": '{"searchQuery":"hello","limit":5,"includeComments":false}'},
    }
    fake = FakeClient(FakeActor(actor=actor, build=FakeBuild(openapi={})))
    result = I.lookup_actor("owner/searcher", client=fake)
    assert result["schema_found"] is True
    assert result["input_mapping_suggestion"]["query"] == "searchQuery"
    assert result["input_mapping_suggestion"]["max_items"] == "limit"
    assert result["input_mapping_suggestion"]["comments"] == "includeComments"


def test_required_unknown_field_is_not_invented():
    schema = {"type": "object", "required": ["query", "secretMode"], "properties": {
        "query": {"type": "string"}, "secretMode": {"type": "object"}
    }}
    probe, unresolved, mapping = I.build_probe_input(schema, {"query": "test"})
    assert probe["query"] == "test"
    assert "secretMode" in unresolved
    assert mapping["query"] == "query"


def test_input_compatibility_is_conservative():
    assert I.actor_input_compatibility("x", {"query": "q"})["compatible"] is True
    assert I.actor_input_compatibility("instagram", {"urls": "directUrls"})["compatible"] is True
    bad = I.actor_input_compatibility("youtube", {"urls": "startUrls"})
    assert bad["compatible"] is False
    assert any("discovery" in x for x in bad["warnings"])


def test_output_mapping_handles_nested_paths_and_core_fields():
    items = [{
        "post": {"caption": "hello", "permalink": "https://x/1"},
        "createdAt": "2026-09-01T10:00:00Z",
        "author": {"username": "alice"},
        "metrics": {"viewCount": 200, "likeCount": 5},
    }]
    inferred = I.infer_output_mapping(items)
    assert inferred["mapping"]["text"] == "post.caption"
    assert inferred["mapping"]["url"] == "post.permalink"
    assert inferred["mapping"]["date"] == "createdAt"
    assert inferred["mapping"]["author"] == "author.username"
    assert "post.caption" in inferred["available_paths"]


def test_apify_connection_uses_account_endpoint_abstraction():
    result = I.test_apify_connection(client=FakeClient(user={"username":"team"}))
    assert result["ok"] is True
    assert result["username"] == "team"


def test_actor_lookup_reads_metadata_and_openapi_without_running_actor():
    schema = {"type":"object","properties":{"query":{"type":"string"},"maxItems":{"type":"integer"}}}
    openapi = {"paths":{"/run":{"post":{"requestBody":{"content":{"application/json":{"schema":schema}}}}}}}
    fa = FakeActor(actor={"id":"a1","username":"owner","name":"actor","title":"Actor X","isPublic":True}, build=FakeBuild(openapi=openapi))
    client = FakeClient(fa)
    result = I.lookup_actor("owner/actor", client=client)
    assert result["actor"]["actor_id"] == "owner/actor"
    assert result["actor"]["title"] == "Actor X"
    assert result["input_mapping_suggestion"]["query"] == "query"
    assert fa.calls == []


def test_actor_smoke_limits_items_and_infers_mapping():
    run = SimpleNamespace(id="r1", default_dataset_id="d1", status="SUCCEEDED", usage_total_usd=0.01)
    fa = FakeActor(run=run)
    client = FakeClient(fa, items=[
        {"text":"one","date":"2026-09-01T00:00:00Z","url":"https://e/1"},
        {"text":"two","date":"2026-09-02T00:00:00Z","url":"https://e/2"},
        {"text":"three","date":"2026-09-03T00:00:00Z","url":"https://e/3"},
        {"text":"four","date":"2026-09-04T00:00:00Z","url":"https://e/4"},
    ])
    result = I.smoke_test_actor("owner/actor", {"query":"x"}, max_items=3, max_charge_usd=0.10, client=client)
    assert result["ok"] is True
    assert result["sample_count"] == 3
    assert result["output_mapping"]["mapping"]["text"] == "text"
    assert fa.calls[0]["max_items"] == 3
    assert float(fa.calls[0]["max_total_charge_usd"]) == pytest.approx(0.10)


def test_actor_smoke_empty_dataset_is_not_verified():
    run = SimpleNamespace(id="r-empty", default_dataset_id="d-empty", status="SUCCEEDED", usage_total_usd=0.0)
    client = FakeClient(FakeActor(run=run), items=[])
    with pytest.raises(I.IntegrationError, match="empty dataset"):
        I.smoke_test_actor("owner/actor", {"query":"x"}, max_items=3, max_charge_usd=0.10, client=client)


def test_openai_model_settings_are_replaceable_and_persisted(tmp_path, monkeypatch):
    secret_file = tmp_path / "config" / "secrets.env"
    monkeypatch.setattr(I, "SECRETS_FILE", secret_file)
    old_b, old_r = I.settings.signalyth_ai_bulk_model, I.settings.signalyth_ai_reasoning_model
    try:
        status = I.write_server_secrets(bulk_model="gpt-5.6-luna", reasoning_model="gpt-5.6-terra")
        assert status["openai"]["bulk_model"] == "gpt-5.6-luna"
        assert status["openai"]["reasoning_model"] == "gpt-5.6-terra"
        text = secret_file.read_text()
        assert "SIGNALYTH_AI_BULK_MODEL=gpt-5.6-luna" in text
        assert "SIGNALYTH_AI_REASONING_MODEL=gpt-5.6-terra" in text
        with pytest.raises(ValueError):
            I.write_server_secrets(bulk_model="bad model\nINJECT=1")
    finally:
        I.settings.signalyth_ai_bulk_model, I.settings.signalyth_ai_reasoning_model = old_b, old_r


def test_openai_connection_can_report_partial_model_access():
    class Models:
        def retrieve(self, model):
            if model == "good": return SimpleNamespace(id="good")
            raise RuntimeError("missing")
    client = SimpleNamespace(models=Models())
    result = I.test_openai_connection(client=client, models=["good","missing"])
    assert result["ok"] is False
    assert result["accessible_models"] == ["good"]
    assert result["unavailable_models"] == ["missing"]


def test_openai_smoke_uses_structured_output_without_exposing_content():
    class Responses:
        def __init__(self): self.kwargs = None
        def create(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                output_text='{"status":"ok","language":"en"}',
                usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
                model="fake", id="resp1",
            )
    responses = Responses()
    client = SimpleNamespace(responses=responses)
    result = I.smoke_test_openai(client=client, model="fake")
    assert result["ok"] is True
    fmt = responses.kwargs["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert responses.kwargs["store"] is False


def test_secret_storage_never_returns_secret_and_uses_private_mode(tmp_path, monkeypatch):
    secret_file = tmp_path / "config" / "secrets.env"
    monkeypatch.setattr(I, "SECRETS_FILE", secret_file)
    old_a, old_o, old_ai, old_dry = I.settings.apify_token, I.settings.openai_api_key, I.settings.signalyth_ai_enabled, I.settings.signalyth_dry_run
    try:
        status = I.write_server_secrets(apify_token="apify_token_123456789", openai_api_key="sk-test-abcdefghijklmnopqrstuvwxyz", ai_enabled=True, live_collection_enabled=True)
        assert status["apify"]["configured"] is True
        assert status["openai"]["configured"] is True
        assert status["apify"]["live_collection_enabled"] is True
        assert I.settings.signalyth_dry_run is False
        assert "apify_token_123456789" not in repr(status)
        assert "sk-test-abcdefghijklmnopqrstuvwxyz" not in repr(status)
        text = secret_file.read_text()
        assert "APIFY_TOKEN=" in text and "OPENAI_API_KEY=" in text
        if hasattr(secret_file.stat(), "st_mode"):
            assert (secret_file.stat().st_mode & 0o777) == 0o600
        with pytest.raises(ValueError):
            I.write_server_secrets(apify_token="validlength123\nINJECT=1")
    finally:
        I.settings.apify_token, I.settings.openai_api_key, I.settings.signalyth_ai_enabled, I.settings.signalyth_dry_run = old_a, old_o, old_ai, old_dry

def test_actor_smoke_diagnostic_only_dataset_cannot_verify():
    run = SimpleNamespace(id="r-diag", default_dataset_id="d-diag", status="SUCCEEDED", usage_total_usd=0.001)
    client = FakeClient(FakeActor(run=run), items=[
        {"id":"diag:1", "resultType":"diagnostic", "status":"zero-output", "message":"Fetch failed after HTTP 504"}
    ])
    with pytest.raises(I.IntegrationError, match="no real data rows"):
        I.smoke_test_actor("owner/actor", {"query":"x"}, max_items=3, max_charge_usd=0.10, client=client)


def test_actor_smoke_partial_failure_with_real_row_cannot_verify():
    run = {
        "id":"r-partial", "defaultDatasetId":"d-partial", "status":"SUCCEEDED", "usageTotalUsd":0.001,
        "statusMessage":"Partial result. Fetch failed after retry-exhausted HTTP 504",
    }
    client = FakeClient(FakeActor(run=run), items=[
        {"text":"real row", "date":"2026-09-01T00:00:00Z", "url":"https://e/1"},
        {"id":"diag:1", "resultType":"diagnostic", "status":"zero-output", "message":"retry-exhausted 504"},
    ])
    with pytest.raises(I.IntegrationError, match="not a clean acquisition"):
        I.smoke_test_actor("owner/actor", {"query":"x"}, max_items=3, max_charge_usd=0.10, client=client)
