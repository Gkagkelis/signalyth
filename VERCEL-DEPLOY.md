# SIGNALYTH v1.8.4 — Vercel deployment

This build keeps the existing SIGNALYTH methodology and UI intact. The cloud adapter changes only infrastructure:

- FastAPI remains the web application.
- Vercel Queues + Celery replace the in-process ThreadPoolExecutor in Vercel deployments.
- `/tmp/signalyth-data` is the worker scratch workspace.
- A **private Vercel Blob** store persists `plan.json`, `status.json`, `control.json`, registry changes, and zipped run snapshots.
- `APIFY_TOKEN` and `OPENAI_API_KEY` are Vercel Environment Variables; the cloud UI is deliberately prevented from writing secrets to disk.

## Required Vercel environment variables

- `APIFY_TOKEN` — required for live collection.
- `SIGNALYTH_DRY_RUN=false` — required for live collection.
- `OPENAI_API_KEY` — required when AI Analysis is enabled.
- `SIGNALYTH_AI_ENABLED=true` — enable the AI stages if desired.
- `BLOB_READ_WRITE_TOKEN` — added automatically when a private Blob store is connected to the project.

The Vercel runtime automatically sets `VERCEL`; when present SIGNALYTH defaults to cloud storage and the Celery queue backend.

## Deployment

1. Push this directory to a **private GitHub repository**.
2. Import the repository into Vercel as a new project (do not reuse `subscription-starter`).
3. Create/connect a **Private Blob** store to the project.
4. Add the environment variables above for Production (and Preview if needed).
5. Deploy.
6. Open `/api/health`. Production is ready when `running_on_vercel=true`, `execution_backend="celery"`, and `cloud_storage_configured=true`.

## Duration note

`vercel.json` uses `maxDuration: 300` so this build is valid on Hobby. A single queued pipeline execution must finish within that limit. On Pro/Enterprise, Vercel supports longer Python Function durations; increase `maxDuration` if your real SIGNALYTH runs need it. For runs that routinely exceed one function execution window, the next hardening step is to split the pipeline into durable Workflow steps rather than replaying a paid collection.
