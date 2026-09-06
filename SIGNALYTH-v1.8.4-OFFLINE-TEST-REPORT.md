# SIGNALYTH v1.8.4 — Collection Orchestrator Validation Report

Date: 2026-09-05
Status: **OFFLINE / LOCAL HARDENING PASS — LIVE ACCEPTANCE STILL PENDING**

## Final automated suite

**497 tests passed / 0 failed** in three complete, non-overlapping test groups after the final safety fixes.

- Hardening / multi-source / resilience group: **124 passed**.
- Core analysis / lifecycle / integration group: **202 passed**.
- API / UI / presentation / visualization group: **171 passed**.
- Pytest collection check: **497 tests collected**.

Coverage includes collection, lifecycle, cleaning, AI semantics, deterministic intelligence, investigations, visualization, presentation/export, integrations, Actor replacement/rollback, UI regression, Smart Collection v2, multi-source matrix behavior, resilience, fuzzing, query-safety regressions, live-verification gating, provider cost-cap violations and schema-drift fail-closed behavior.

## Exhaustive multi-source matrix

- Sources: X, TikTok, Instagram, Facebook, YouTube, News.
- Non-empty source combinations: **63**.
- Planner variants: **1,575** (63 combinations × 5 sample targets × 5 budgets).
- Healthy execution: all **63** combinations execute in the mocked offline harness while preserving each source.
- Exhaustive single-source failure placement: **384** placements — every one of the **192 source positions** across the 63 combinations is deliberately failed once as transient HTTP 504 and once as permanent HTTP 403.
- One source can fail without erasing evidence from healthy sources.
- Simultaneous multi-source failure, repeated transient failure, circuit breaking and all-selected-sources failure are also covered.

## Deterministic fuzz / torture

- Random planner fuzz: **1,000** plans.
- Random resilience torture: **50,000** logical Actor acquisitions.
- Standalone invariant checks: **368,900**.
- Torture failures: **0**.

The torture space includes success, true empty yield, HTTP 504, HTTP 429, HTTP 403, provider false-green/diagnostic failure, partial success plus transient failure, and provider-reported cost above the hard attempt cap.

Validated invariants include:
- all 63 source combinations plan successfully;
- no planned logical-call caps exceed the user budget;
- source targets sum exactly to the requested sample;
- fan-out stays within conservative source batch limits;
- retries are bounded and remain inside their logical cost envelope;
- diagnostics never become analysis evidence;
- client/agency names never leak into discovery;
- market-only queries are forbidden;
- non-X Smart Search queries remain topic-anchored;
- comment coverage is never falsely presented as live-verified;
- provider-reported cost above a hard per-call cap is never silently clamped or treated as success.

## Real Allwyn/X failure regression

The manual Allwyn run exposed an upstream X search pattern in which multiple searches ended in repeated HTTP 504/retry exhaustion while the provider container still appeared technically successful. v1.8.4 protects against that class of failure by:
- never planning the large 12-term X fan-out as one logical Actor call;
- using conservative source-specific logical batches;
- classifying partial-failure/504 diagnostics as failure rather than clean zero-yield;
- splitting transient multi-target failures before retrying;
- checkpointing successful evidence after every logical batch;
- opening a source circuit breaker after repeated transient failure;
- continuing healthy sources while isolating the failed one.

## Final safety fixes added after adversarial review

1. **Live Actor verification gate.** A full paid RUN is now blocked before the first Actor call unless every selected primary Actor ID exactly matches a registry entry with `actor_status=verified`. Actor verification must happen first through the tiny paid Connection Manager smoke/commit flow.
2. **Provider cost-cap violation.** If the provider reports a charge above the hard per-attempt cap, SIGNALYTH does not silently clamp it. It records the anomaly, checkpoints any returned evidence, stops all later paid source calls, marks remaining sources `skipped_budget_safety`, and fails the collection visibly.
3. **Schema/mapping drift guard.** If real provider data rows arrive but none can satisfy the normalized contract, or all normalized rows lack usable dates, the source is failed as possible schema/mapping drift instead of being mislabeled as ordinary scarcity.
4. **Exact evidence preservation.** Diagnostic/error rows remain in raw/audit storage but never enter normalized analysis evidence.

## Local server smoke

A real local FastAPI process was started and stopped successfully. Checks passed:
- `GET /api/health` → HTTP 200, app version **1.8.4**, dry-run mode confirmed.
- `POST /api/plan` with **Allwyn · Greece · X + Facebook + TikTok + YouTube + News · target 1,000 · Comments ON** → HTTP 200.
- Target distributed to 1,000 total items; source logical batches were conservative.
- Planned charge caps stayed below the supplied global budget.
- Comment coverage correctly reported **not fully live-verified** and listed verification blockers.
- Query-safety flags confirmed no client-field discovery, no market-only queries, topic anchoring and adaptive collision detection.

## Syntax / static security checks

Passed:
- Python `compileall` for app/tools/tests;
- HTML parser validation;
- JavaScript syntax (`node --check`) for the inline app script;
- Mac launcher shell syntax (`bash -n`);
- runtime/package scan found no pasted Apify UI/API token or OpenAI key patterns outside intentional test fixtures;
- scan found no `eval`, `exec`, `shell=True`, `os.system` or wildcard-CORS pattern in application code.

## What this PASS does **not** mean

This is a strong offline/local hardening pass. It does **not** prove current third-party Actor availability, current live schemas, real-world sample yield, platform completeness, comment-route compatibility or real billing behavior. Default Actors and comment routes remain unverified until paid smoke-tested with user-held credentials.

## Remaining real acceptance gates

1. Enter credentials only in the server-side Connections flow.
2. Tiny paid discovery smoke + real sample/mapping commit for each source Actor that will be used.
3. Tiny paid comment/reply smoke for each Comments route that will be enabled.
4. OpenAI connection/smoke.
5. Small real 2-source run.
6. Small real 3–6-source run while observing failures, cost and exact fields.
7. Full Allwyn 1,000 or Vodafone end-to-end pilot with the underlying evidence/numbers checked.
8. Only then may SIGNALYTH be called live/production-accepted.

**Acceptance sequence: BUILD → TEST → BREAK DELIBERATELY → FIX → RE-TEST → REAL SMALL PAID TEST → MULTI-SOURCE TEST → FULL PILOT → APPROVE.**
