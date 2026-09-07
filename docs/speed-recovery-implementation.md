# SIGNALYTH — Speed and recovery implementation

Status: implementation in progress; not production-validated. The existing live run must not be restarted or overwritten as part of this work.

## Acceptance contract

1. Preserve the existing scientific methodology, source contracts, all 25 indicator families, and evidence-linked exports. Speed must not be obtained by reducing the requested sample, skipping difficult evidence, suppressing uncertainty, or silently changing models or classification rules.
2. A long run must survive the execution limit of a single web/serverless request. Persist source/provider run identifiers, completed data, analysis batch results, cost reservations, and stage state in durable storage. A lost worker must be distinguishable from a slow but live worker.
3. Collection, technical cleaning, and independent semantic batches may overlap after a safe per-batch input snapshot. Final aggregation and reporting must use a finalized, versioned evidence snapshot. Semantic refill must be bounded and must not trigger unnecessary re-analysis of unchanged evidence.
4. Parallelism must have shared, atomic concurrency and cost controls. Reservations cover all in-flight work, not just completed work. Unknown provider outcomes are not automatically retried as new paid requests. Provider run IDs and response IDs, when available, must be retained for reconciliation.
5. Every expensive completed unit is checkpointed before scheduling dependent work. Recovery reuses completed units and explicitly reports unknown outcomes. Cancellation preserves evidence and stops new work without silently discarding completed results.
6. Status must expose stage, completed/total units, last heartbeat, current provider operation, and actionable failure or recovery state. Progress must not imply completion merely because collection ended or a fixed percentage was reached.
7. The proposed sentiment display band of ±0.10 is provisional and must be versioned, calibrated, and kept separate from the original target-aware classification. Mixed/unclear and genuine neutral evidence must not be forced into polarity. Existing results are not silently rewritten.
8. Emotion time series must preserve multi-label intensities, use an explicit denominator, show daily sample size and numeric values, and share one data contract across dashboard, PDF, DOCX, and PPTX. A top-three-emotions shortcut is not a complete seven-family trend.
9. Offline tests must include concurrent budget races, provider timeout after billing, crash/restart between provider completion and checkpoint, duplicate delivery, cancellation, partial source failure, all-source completion, no-AI fallback, and deterministic report/export consistency. A green test count alone is not production acceptance.
10. A live acceptance run requires explicit approval of the collection and AI cost ceilings. Verify a complete report and every export, a bounded interrupted-run recovery, and actual wall-clock measurements before declaring production ready.

## Rollout

Use a separate GitHub branch and dry-run/offline validation first. Do not apply the uploaded three-file patch unchanged: its parallel cost accounting, unversioned sentiment override, and dashboard-only emotion renderer require correction. Do not change production credentials, deploy a new worker, start paid Actors, or invoke paid OpenAI calls as part of this preparation. Preserve the existing production branch and saved runs until the new execution path has been validated.
