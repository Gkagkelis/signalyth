# SIGNALYTH Multi-Source Hardening v1.8.2

## Goal
A user may select any subset of X, TikTok, Instagram, Facebook, YouTube and News. External Actors are unreliable dependencies, so a single upstream timeout, schema issue or empty source must not destroy the run, erase evidence, hide uncertainty or break the user budget.

## Execution contract
1. Plan conservative source-specific logical batches.
2. Never multiply a tiny requested target into extra paid calls.
3. Run each logical batch inside its own hard charge envelope.
4. Classify clean zero yield separately from infrastructure failure.
5. On transient multi-target failure, split and retry smaller units.
6. Preserve partial successful rows before retrying.
7. Checkpoint raw, normalized, metadata and diagnostics after every batch.
8. On permanent failure, stop wasting calls on that route.
9. On repeated transient source failures, open a circuit breaker and continue with other sources.
10. Rebalance only when allowed and only inside the remaining global budget.
11. Never normalize diagnostic/error rows into evidence.
12. Exact requested dates remain enforced after collection when needed.
13. If every selected source fails, the run is FAILED, not completed.
14. If sources are healthy but relevant evidence is scarce, report a shortfall; never junk-fill.

## Multi-source behavior
- 1 source: source failure is isolated and becomes a truthful failed run if no other evidence exists.
- 2–5 sources: healthy sources continue even when another source fails.
- 6 sources: same rule; source-specific evidence/checkpoints stay separate and final normalized evidence is merged only after per-source normalization.

## Comments/replies
Discovery verification and comment/reply verification are different gates. Public schema support does not authorize an automatic paid deepening call. Each comment/reply route must be independently live-smoke-verified and recorded as verified before auto-execution.

## What this does not claim
Offline tests cannot prove live Actor availability, current third-party schema compatibility, real-world yield or platform completeness. Those are explicitly reserved for tiny paid live acceptance tests.
