# SIGNALYTH v1.8.4 — Collection Orchestrator Hardening Candidate

This package preserves the v1.7.1 functional shell, the v1.8 integration control plane and the v1.8.2 multi-source resilience layer, then hardens **query safety and orchestration** for real multi-source work. It is a **candidate**, not live/production-approved. Real paid Apify/OpenAI smoke tests and an accepted end-to-end pilot remain mandatory.

## What v1.8.4 adds

- The UI `Client` field is now strictly administrative and can never leak into discovery queries.
- Market aliases such as `Greece`, `Ελλάδα` and `Ellada` can never become naked broad queries.
- Secondary keywords such as `OPAP` are no longer allowed to become unanchored high-volume searches; they stay joined to the research topic.
- The topic itself remains the primary discovery anchor; a Greeklish form of the topic may act as a legitimate alias, while Greeklish context terms stay topic-anchored.
- X keeps balanced intent buckets and conservative two-query logical batches; a large 12-query fan-out like the failed manual Allwyn run is never issued as one Actor call by the application.
- Every selected source is protected by the same resilience layer: bounded retries, split-on-transient-failure, checkpointing, source isolation, circuit breaking, diagnostic-row quarantine and hard budget envelopes.
- All 63 non-empty combinations of X, TikTok, Instagram, Facebook, YouTube and News are planned and tested.
- Comment/reply deepening remains fail-closed until each specific paid route is independently live-smoke-verified.
- A full live RUN is blocked until every selected primary Actor has itself passed a tiny paid smoke and been committed as verified.
- Provider-reported spend above a per-call hard cap triggers a global paid-collection stop; preserved evidence remains available but the run is failed visibly.
- Raw rows that no longer satisfy the normalized data/date contract are flagged as possible schema/mapping drift, not misreported as clean scarcity.

## Collection contract

A requested sample of 500/1,000 means **up to that many analyzable evidence units**, not a command to fill a quota with noise. SIGNALYTH may collect a larger candidate pool, clean/filter/dedupe it, deepen useful conversations when a verified route exists, rebalance healthy sources, and stop with a truthful shortfall if relevant public evidence is exhausted.

The application must never require the user to understand Actor-specific syntax, batching, retry semantics or date quirks. Those belong in the server-side planner/orchestrator.

## Failure behavior

- 504/503/502, 429, timeouts and provider partial-failure diagnostics are treated as infrastructure/transient failures, not clean zero-yield.
- A provider UI saying `Succeeded` cannot override acquisition diagnostics showing retry exhaustion/partial failure.
- Partial successful evidence is saved immediately and survives later source failure.
- One source may fail while the others continue.
- If every selected source fails and no evidence exists, the run is **failed**, never green/completed.
- Clean zero-yield is reported as scarcity, not infrastructure failure.
- Repeated transient failures open a source circuit breaker so cost is not wasted.

## Validation in this package

See `COLLECTION-ORCHESTRATOR-v1.8.4.md` and `TEST_REPORT.md`.

Offline/local validation includes:

- 497 automated tests;
- all 63 non-empty source combinations;
- 384 exhaustive single-source failure placements across those combinations (every source position tested once with 504 and once with 403);
- 1,575 planner matrix variants (63 combinations × 5 target sizes × 5 budgets);
- 1,000 deterministic randomized planner fuzz cases;
- 50,000 randomized resilience scenarios;
- 368,900 invariant checks in the standalone torture harness;
- query-safety regression tests that forbid client-name leakage, naked market queries and unanchored broad secondary keywords;
- deliberate 504/429/403, false-green provider status, zero-output diagnostics, malformed rows, missing dates, overdelivery, budget exhaustion, multi-source failures, circuit breakers, restart/cancellation and cross-source evidence handling.

These are offline/local tests. They **cannot prove** current third-party Actor health, current live schemas, real-world yield, platform completeness or real billing behavior.

## Start locally

Double-click `START-SIGNALYTH.command` on Mac or run the backend normally in a controlled server environment. The backend binds to localhost by default. For visual-only preview, `SIGNALYTH-v1.8.4-preview.html` is included; `file://` mode cannot safely use live integrations.

## Security and acceptance boundary

Never put API keys in chat, frontend HTML or browser localStorage. Credentials belong server-side. Default Actors and all comment-deepening routes remain unapproved until the Actor Connection Manager performs explicit tiny paid smoke tests with user-held credentials.

Required final gate:

**BUILD → TEST → BREAK DELIBERATELY → FIX → RE-TEST → tiny real paid source smoke tests → small real multi-source run → Allwyn/Vodafone end-to-end pilot → APPROVE.**

### macOS relocation-safe launcher hotfix
`START-SIGNALYTH.command` now invokes `.venv/bin/python3` directly instead of activating the virtual environment. This prevents macOS from falling back to the system Python 2.7 after the application folder is renamed or moved.
