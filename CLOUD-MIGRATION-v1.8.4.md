# SIGNALYTH v1.8.4 — Cloud migration patch

This patch is infrastructure-only. The existing research methodology, collection planner, source strategy, cleaning, semantic analysis, intelligence, investigations, visualizations and export logic remain in place.

## Added

- Vercel Python entrypoint (`app.main:app`).
- Vercel Celery subscriber (`app.worker.run:app`) backed by Vercel Queues.
- Private Vercel Blob persistence for run metadata, full run snapshots, Actor registry changes and Actor candidates.
- `/tmp` scratch workspace when running on Vercel.
- Environment-variable-only secret handling in cloud deployments.
- Actor probe schema sanitizer that prevents malformed Actor example inputs (for example an object supplied to a field declared as `string`) from reaching Apify validation.

## Local compatibility

Outside Vercel, SIGNALYTH keeps the existing filesystem store and `ThreadPoolExecutor` behavior. The final regression run after this patch passed 509 tests.
