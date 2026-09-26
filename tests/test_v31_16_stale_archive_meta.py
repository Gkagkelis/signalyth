"""v31.16 — lagging archive metadata must never roll a live workspace back.

_refresh_if_outdated treated ANY stamp difference as "the durable archive
moved forward". archive-meta.json is read through Blob/CDN and can lag behind
the checkpoint this very worker just made, so remote(older) != local(newer)
wiped the live workspace — completed analysis files included — and restored
an OLDER archive mid-pipeline. NBG run 20260926T142313Z-d4659ef0 lost its
finished AI analysis twice this way (phase intelligence_failed: "the AI
analysis completed ... but its files are gone from this run's workspace").

Refresh now happens only when the remote stamp is strictly newer.
"""
from __future__ import annotations

from pathlib import Path

from app.services.storage import RunStore


class FakeCloud:
    enabled = True

    def __init__(self, remote_stamp):
        self.remote_stamp = remote_stamp
        self.restores = 0

    def get_archive_meta(self, run_id):
        return {"checkpointed_at": self.remote_stamp} if self.remote_stamp else None

    def restore_run_archive(self, run_id, folder):
        self.restores += 1
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / ".signalyth-archive-stamp").write_text(
            str(self.remote_stamp), encoding="utf-8")
        (Path(folder) / "marker-from-old-archive.json").write_text("[]", encoding="utf-8")
        return True


def _workspace(tmp_path, local_stamp):
    folder = tmp_path / "runs" / "R"
    folder.mkdir(parents=True)
    (folder / ".signalyth-archive-stamp").write_text(local_stamp, encoding="utf-8")
    (folder / "analysis-fresh.json").write_text("[1]", encoding="utf-8")
    return folder


def _store(cloud):
    store = RunStore()
    store.cloud = cloud
    store._refresh_checked = {}
    return store


def test_lagging_remote_meta_never_replaces_a_newer_local_workspace(tmp_path):
    cloud = FakeCloud("2026-09-26T18:00:00+00:00")     # CDN still serving the OLD meta
    store = _store(cloud)
    folder = _workspace(tmp_path, "2026-09-26T18:20:59+00:00")  # this worker's own newer checkpoint

    store._refresh_if_outdated("R", folder)

    assert cloud.restores == 0, "a lagging remote stamp must not trigger a rollback"
    assert (folder / "analysis-fresh.json").exists(), "the live workspace was destroyed"


def test_equal_stamps_do_not_refresh(tmp_path):
    cloud = FakeCloud("2026-09-26T18:20:59+00:00")
    store = _store(cloud)
    folder = _workspace(tmp_path, "2026-09-26T18:20:59+00:00")
    store._refresh_if_outdated("R", folder)
    assert cloud.restores == 0


def test_genuinely_newer_remote_archive_still_refreshes(tmp_path):
    cloud = FakeCloud("2026-09-26T19:00:00+00:00")
    store = _store(cloud)
    folder = _workspace(tmp_path, "2026-09-26T18:20:59+00:00")
    store._refresh_if_outdated("R", folder)
    assert cloud.restores == 1
    assert (folder / "marker-from-old-archive.json").exists()
    assert not (folder / "analysis-fresh.json").exists()


def test_legacy_local_marker_is_not_rolled_back_by_iso_remote(tmp_path):
    cloud = FakeCloud("2026-09-26T18:00:00+00:00")
    store = _store(cloud)
    folder = _workspace(tmp_path, "legacy-refreshed")
    store._refresh_if_outdated("R", folder)
    assert cloud.restores == 0
