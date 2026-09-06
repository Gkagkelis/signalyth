from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app import main as M
from app.services import integrations as I


def _isolate(monkeypatch, tmp_path):
    secret_file = tmp_path / "user" / "secrets.env"
    monkeypatch.setattr(I, "SECRETS_FILE", secret_file)
    old = (
        I.settings.apify_token,
        I.settings.openai_api_key,
        I.settings.signalyth_ai_enabled,
        I.settings.signalyth_dry_run,
    )
    I.settings.apify_token = ""
    I.settings.openai_api_key = ""
    I.settings.signalyth_ai_enabled = False
    I.settings.signalyth_dry_run = True
    return secret_file, old


def _restore(old):
    (
        I.settings.apify_token,
        I.settings.openai_api_key,
        I.settings.signalyth_ai_enabled,
        I.settings.signalyth_dry_run,
    ) = old


def test_sequential_provider_saves_never_replace_each_other(monkeypatch, tmp_path):
    secret_file, old = _isolate(monkeypatch, tmp_path)
    try:
        client = TestClient(M.app)
        a = "apify_token_123456789012345"
        o = "sk-test-abcdefghijklmnopqrstuvwxyz123456"
        r1 = client.post("/api/integrations/apify/credential", json={"apify_token": a})
        assert r1.status_code == 200
        assert r1.json()["apify"]["configured"] is True
        assert r1.json()["openai"]["configured"] is False

        r2 = client.post("/api/integrations/openai/credential", json={"openai_api_key": o})
        assert r2.status_code == 200
        status = r2.json()
        assert status["apify"]["configured"] is True
        assert status["openai"]["configured"] is True
        assert a not in r2.text and o not in r2.text

        text = secret_file.read_text(encoding="utf-8")
        assert f"APIFY_TOKEN={a}" in text
        assert f"OPENAI_API_KEY={o}" in text
    finally:
        _restore(old)


def test_reverse_sequential_provider_saves_never_replace_each_other(monkeypatch, tmp_path):
    secret_file, old = _isolate(monkeypatch, tmp_path)
    try:
        client = TestClient(M.app)
        a = "apify_token_123456789012345"
        o = "sk-test-abcdefghijklmnopqrstuvwxyz123456"
        assert client.post("/api/integrations/openai/credential", json={"openai_api_key": o}).status_code == 200
        r = client.post("/api/integrations/apify/credential", json={"apify_token": a})
        assert r.status_code == 200
        assert r.json()["apify"]["configured"] is True
        assert r.json()["openai"]["configured"] is True
        text = secret_file.read_text(encoding="utf-8")
        assert f"APIFY_TOKEN={a}" in text
        assert f"OPENAI_API_KEY={o}" in text
    finally:
        _restore(old)


def test_near_simultaneous_provider_saves_are_atomic_merge(monkeypatch, tmp_path):
    secret_file, old = _isolate(monkeypatch, tmp_path)
    try:
        a = "apify_token_123456789012345"
        o = "sk-test-abcdefghijklmnopqrstuvwxyz123456"
        # Repeated paired writes make the old read-modify-write race easy to expose.
        for _ in range(50):
            secret_file.unlink(missing_ok=True)
            I.settings.apify_token = ""
            I.settings.openai_api_key = ""
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(I.write_server_secrets, apify_token=a)
                f2 = ex.submit(I.write_server_secrets, openai_api_key=o)
                f1.result()
                f2.result()
            text = secret_file.read_text(encoding="utf-8")
            assert f"APIFY_TOKEN={a}" in text
            assert f"OPENAI_API_KEY={o}" in text
    finally:
        _restore(old)


def test_provider_save_preserves_runtime_flags_and_model_settings(monkeypatch, tmp_path):
    secret_file, old = _isolate(monkeypatch, tmp_path)
    old_b, old_r = I.settings.signalyth_ai_bulk_model, I.settings.signalyth_ai_reasoning_model
    try:
        I.write_server_secrets(
            live_collection_enabled=True,
            ai_enabled=True,
            bulk_model="model-bulk",
            reasoning_model="model-reason",
        )
        I.write_server_secrets(apify_token="apify_token_123456789012345")
        I.write_server_secrets(openai_api_key="sk-test-abcdefghijklmnopqrstuvwxyz123456")
        text = secret_file.read_text(encoding="utf-8")
        assert "SIGNALYTH_DRY_RUN=false" in text
        assert "SIGNALYTH_AI_ENABLED=true" in text
        assert "SIGNALYTH_AI_BULK_MODEL=model-bulk" in text
        assert "SIGNALYTH_AI_REASONING_MODEL=model-reason" in text
        assert "APIFY_TOKEN=" in text and "OPENAI_API_KEY=" in text
    finally:
        I.settings.signalyth_ai_bulk_model, I.settings.signalyth_ai_reasoning_model = old_b, old_r
        _restore(old)


def test_default_secret_location_is_outside_versioned_app_folder():
    # This is the upgrade-safety invariant: moving/replacing the app directory
    # must not move/delete the user's secret store.
    from app.config import BASE_DIR, PERSISTENT_SECRETS_FILE
    assert PERSISTENT_SECRETS_FILE.name == "secrets.env"
    assert BASE_DIR not in PERSISTENT_SECRETS_FILE.parents
