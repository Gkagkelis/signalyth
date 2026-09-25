from __future__ import annotations
from datetime import timedelta
from decimal import Decimal
from app.config import settings
from app.services.resilience import actor_run_timeout_seconds

class CollectionNotConfigured(RuntimeError):
    pass


def _run_to_dict(run) -> dict:
    if isinstance(run, dict):
        return dict(run)
    if hasattr(run, "model_dump"):
        return run.model_dump(mode="json", by_alias=True)
    data = {}
    for attr in ("id", "default_dataset_id", "usage_total_usd", "status", "status_message", "started_at", "finished_at"):
        if hasattr(run, attr):
            value = getattr(run, attr)
            key = {
                "default_dataset_id": "defaultDatasetId",
                "usage_total_usd": "usageTotalUsd",
                "status_message": "statusMessage",
                "started_at": "startedAt",
                "finished_at": "finishedAt",
            }.get(attr, attr)
            data[key] = value
    return data


class ApifyRunner:
    def __init__(self):
        if not settings.apify_token:
            raise CollectionNotConfigured("Apify is not connected. Configure APIFY_TOKEN securely on the server.")
        try:
            from apify_client import ApifyClient
        except ImportError as exc:
            raise CollectionNotConfigured("apify-client is not installed.") from exc
        self.client = ApifyClient(settings.apify_token)

    #: Extra seconds we wait for the Actor to be SCHEDULED and finish on top
    #: of its own run timeout. The run timeout only starts once the Actor is
    #: running; a run parked in READY (no free memory on the account because
    #: other Actors of ours are still running) never reaches it, and without a
    #: client-side wait our worker blocked forever — run 20260925T094032Z
    #: stopped writing status at 09:45 UTC inside exactly such a call.
    WAIT_MARGIN_SECONDS = 90.0

    def run(self, actor_id: str, run_input: dict, *, max_items: int, max_charge_usd: float) -> tuple[dict, list[dict]]:
        timeout_s = float(actor_run_timeout_seconds(actor_id))
        run = self.client.actor(actor_id).call(
            run_input=run_input,
            max_items=max_items,
            max_total_charge_usd=Decimal(str(max_charge_usd)),
            # A single slow provider must not hold the whole SIGNALYTH pipeline forever.
            # Apify terminates the Actor run itself at this limit; resilience logic can
            # then isolate the failure and continue with later batches/sources.
            run_timeout=timedelta(seconds=timeout_s),
            # ...and WE stop waiting shortly after that limit, whatever state
            # the run is in. call() otherwise waits indefinitely.
            wait_duration=timedelta(seconds=timeout_s + self.WAIT_MARGIN_SECONDS),
        )
        if not run:
            raise RuntimeError(f"Actor {actor_id} returned no run object")
        meta = _run_to_dict(run)
        state = str(meta.get("status") or "").upper()
        if state and state not in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMING-OUT", "ABORTING"}:
            # Still READY/RUNNING after our wait window: give the slot back and
            # report it as a provider timeout so the pipeline isolates it.
            run_id = meta.get("id")
            if run_id:
                try:
                    self.client.run(str(run_id)).abort()
                except Exception:
                    pass
            raise RuntimeError(
                f"Actor {actor_id} run did not finish within {int(timeout_s + self.WAIT_MARGIN_SECONDS)}s "
                f"(status={state or 'unknown'}) — timed out and aborted"
            )
        dataset_id = meta.get("defaultDatasetId") or meta.get("default_dataset_id")
        if not dataset_id:
            raise RuntimeError(f"Actor {actor_id} returned no dataset")
        items = self.client.dataset(dataset_id).list_items().items
        return meta, items
