# SIGNALYTH Collection Orchestrator v1.8.4

## Purpose
The orchestration layer exists so a non-technical user can request `Allwyn · Greece · X + Facebook + TikTok · 1,000 · Comments ON` without knowing Actor syntax, source-specific limits, timeout behavior, comment routes or query operators.

## Query-safety rules
1. `Client` is metadata, never a discovery term.
2. The research topic is the only default unanchored discovery seed.
3. Market aliases are context only and must stay topic-anchored.
4. Secondary keywords/additional context stay topic-anchored; broad helper terms are never allowed to become standalone quota-filling searches.
5. A Greeklish form of the topic may be used as a standalone alias; Greeklish context terms remain anchored.
6. User exclusions remain explicit and auditable.
7. X broad recall is one bounded intent bucket, never the entire strategy.
8. Dominant entity/property collisions are split and capped, not silently deleted.

## Multi-source execution rules
1. Plan conservative source-specific logical batches.
2. Never send a large fan-out as one fragile Actor call when the source has a safer batch profile.
3. Keep a hard global budget and a hard logical-call envelope.
4. Save raw/normalized/audit state after every logical batch.
5. Split multi-target transient failures before retrying.
6. Preserve successful partial evidence.
7. Stop wasting calls on a permanently broken route.
8. Open a circuit breaker after repeated source-level transient failure.
9. Continue healthy sources even when another source fails.
10. Never pass diagnostic/error rows into analysis.
11. Enforce exact requested dates after collection when the Actor cannot guarantee them natively.
12. Never junk-fill a requested sample.

## Comment/reply contract
Comment discovery is source-specific and is a separate live acceptance gate from post discovery. Public schema knowledge is not enough. A route can auto-execute only after a tiny paid smoke has verified the exact Actor, input route, output mapping and cost behavior.

## What offline hardening can and cannot prove
Offline tests can prove planner invariants, cost envelopes, failure isolation, checkpointing, query anchoring, deterministic retry logic and truthful terminal states. They cannot prove live Actor availability, current schema compatibility, current platform coverage, real-world yield or current billing. Those remain explicit live gates.

## Final v1.8.4 safety additions
- Full live collection is blocked until every selected primary Actor is individually verified and committed after a tiny paid smoke test.
- A provider-reported charge above a hard per-call cap is treated as a budget-safety violation: evidence is checkpointed, later paid source calls are skipped, and the run fails visibly.
- Raw provider rows that cannot satisfy the normalized data contract (including all-date-missing output) are treated as possible schema/mapping drift, never as clean scarcity.
- Exhaustive failure-position testing covers all 192 source positions across the 63 non-empty source combinations for both transient 504 and permanent 403 failures (384 source-failure placements).
