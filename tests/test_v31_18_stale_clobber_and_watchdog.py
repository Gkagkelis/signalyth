"""v31.18 — stale queue tasks can no longer clobber the archive; dead workers
are detected by the run screen itself.

1. app/worker/tasks.py ran an UNCONDITIONAL checkpoint in its finally — for
   every task, including a redelivered stale message whose run_now stood down
   after being displaced. That checkpoint archived the stale local workspace
   over the active worker's fresh archive; the active worker's next folder
   refresh pulled the clobbered (newer-stamped, older-content) copy back and
   its just-written analysis files "vanished". NBG run
   20260926T142313Z-d4659ef0 looped on this for two hours. Durability belongs
   to RunManager._worker's finally, which checks lease ownership.

2. A dead worker used to cost 1200s of dead time before anyone could resume,
   although live workers heartbeat every ≤20s. The stale window is now 300s
   and GET /api/runs/{run_id} — polled by the run screen — auto-resumes a
   stale running run, so recovery needs no human and no external watchdog.
"""
from __future__ import annotations

import inspect

from fastapi.testclient import TestClient


def test_worker_task_finally_never_checkpoints_unconditionally():
    import app.worker.tasks as tasks
    src = inspect.getsource(tasks.run_signalyth)
    assert "checkpoint_run" not in src, (
        "run_signalyth must not checkpoint outside the lease-guarded worker path")


def test_stale_window_matches_the_heartbeat_era():
    from app.config import settings
    assert int(settings.signalyth_stale_running_after_seconds) <= 300


def test_get_run_auto_resumes_a_stale_running_run(monkeypatch):
    import app.main as main

    stale = {"run_id": "R1", "status": "running",
             "updated_at": "2020-01-01T00:00:00+00:00"}
    calls = []
    monkeypatch.setattr(main.store, "get_run", lambda run_id: dict(stale))
    monkeypatch.setattr(main.manager, "enqueue",
                        lambda run_id: calls.append(run_id))

    client = TestClient(main.app)
    body = client.get("/api/runs/R1").json()
    assert calls == ["R1"], "a stale running run must be auto-resumed"
    assert body.get("auto_resumed") is True


def test_get_run_leaves_a_live_run_alone(monkeypatch):
    import app.main as main
    from datetime import datetime, timezone

    fresh = {"run_id": "R2", "status": "running",
             "updated_at": datetime.now(timezone.utc).isoformat()}
    calls = []
    monkeypatch.setattr(main.store, "get_run", lambda run_id: dict(fresh))
    monkeypatch.setattr(main.manager, "enqueue",
                        lambda run_id: calls.append(run_id))

    client = TestClient(main.app)
    body = client.get("/api/runs/R2").json()
    assert calls == [], "a heartbeating worker must not be displaced"
    assert "auto_resumed" not in body
