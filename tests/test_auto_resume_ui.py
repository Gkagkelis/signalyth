from pathlib import Path

HTML = Path("app/templates/index.html").read_text(encoding="utf-8")


def test_auto_resume_waits_for_backend_stale_threshold():
    assert "const AUTO_RESUME_STALE_MS=21*60*1000" in HTML
    assert "ageMs<AUTO_RESUME_STALE_MS" in HTML
    assert ">660000" not in HTML


def test_auto_resume_retries_quickly_after_failed_attempt():
    assert "const AUTO_RESUME_RETRY_MS=60*1000" in HTML
    assert "15*60*1000" not in HTML


def test_comment_pipeline_ignores_non_source_metadata_entries():
    assert "const commentEntries=Object.entries(cd).filter" in HTML
    assert "const commentStates=commentEntries.map" in HTML
