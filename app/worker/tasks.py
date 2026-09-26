from __future__ import annotations

from app.services.run_manager import RunManager
from app.services.storage import RunNotFound, RunStore
from app.worker.celery import app


@app.task(name="app.worker.tasks.run_signalyth")
def run_signalyth(run_id: str) -> dict:
    """Run the unchanged SIGNALYTH methodology inside the durable queue worker."""
    store = RunStore()
    manager = RunManager(store=store)
    try:
        status = store.read_status(run_id)
        if status.get("status") in store.TERMINAL_STATUSES:
            return {"run_id": run_id, "status": status.get("status"), "skipped": True}
        manager.run_now(run_id)
        status = store.read_status(run_id)
        return {"run_id": run_id, "status": status.get("status")}
    except RunNotFound:
        return {"run_id": run_id, "status": "not_found"}
    finally:
        # NO unconditional checkpoint here. This finally runs for EVERY task —
        # including a redelivered stale message whose run_now stood down after
        # being displaced by a newer worker. Its unconditional checkpoint
        # archived the STALE local workspace over the active worker's fresh
        # archive; the active worker's next folder refresh then pulled that
        # clobbered (strictly newer-stamped, older-content) copy back and its
        # just-written analysis files "vanished" — the loop that stalled NBG
        # run 20260926T142313Z-d4659ef0 for two hours. _worker's own finally
        # already checkpoints, correctly guarded by lease ownership.
        manager.shutdown(wait=False)
