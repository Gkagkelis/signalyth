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
        try:
            store.checkpoint_run(run_id)
        except Exception:
            pass
        manager.shutdown(wait=False)
