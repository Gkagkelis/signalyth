from __future__ import annotations
from decimal import Decimal
from app.config import settings

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

    def run(self, actor_id: str, run_input: dict, *, max_items: int, max_charge_usd: float) -> tuple[dict, list[dict]]:
        run = self.client.actor(actor_id).call(
            run_input=run_input,
            max_items=max_items,
            max_total_charge_usd=Decimal(str(max_charge_usd)),
        )
        if not run:
            raise RuntimeError(f"Actor {actor_id} returned no run object")
        meta = _run_to_dict(run)
        dataset_id = meta.get("defaultDatasetId") or meta.get("default_dataset_id")
        if not dataset_id:
            raise RuntimeError(f"Actor {actor_id} returned no dataset")
        items = self.client.dataset(dataset_id).list_items().items
        return meta, items
