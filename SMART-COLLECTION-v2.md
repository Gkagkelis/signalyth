# SIGNALYTH Smart Collection v2 — Candidate

Status: **candidate / not live-accepted**  
Date: 2026-09-05

## Why this exists

The Allwyn Greece X dry run exposed a real collection failure mode: a single dominant compound/property (`Allwyn Arena`) could consume most of a broad keyword sample even though the research target was the Allwyn brand/company. The old planner already had DISCOVER → FILTER → DEEPEN as a product concept, but the X discovery call could still include one broad `topic lang:el` query without per-intent caps.

Smart Collection v2 turns the requested sample into a **target for analyzable evidence**, not a promise to return N literal keyword hits.

## X discovery behavior

For Smart Search ON, the planner now builds multiple auditable intent buckets (market context, known context/entities, products/services when supplied, consumer voice, corporate/investor, sponsorship/activation, bounded language recall). It sends them in one Xquik search run using:

- `mode: search`
- `searchTerms: [...]`
- `includeSearchTerms: true`
- `queryType: Latest`
- `maxItems`: total requested target for the current discovery wave
- `maxItemsPerTarget`: fair per-query cap so one search intent cannot consume the whole sample
- exact `since`/exclusive `until` plus SIGNALYTH post-filtering
- native retweets excluded from discovery queries

## Adaptive collision rule

After a discovery wave, SIGNALYTH can detect a dominant compound/context such as `Allwyn Arena`. A collision is **not automatically deleted**. It is:

1. excluded from the other intent buckets so it cannot dominate them;
2. retained as its own separately capped property/context bucket;
3. still available for exposure/association analysis.

## Reply deepening

Smart Collection v2 includes a deterministic reply-seed planner for Xquik's current direct-reply mode:

- selects relevant seed tweets with real reply counts;
- emits `mode: replies` + `replyTweetIds`;
- caps total and per-seed results;
- treats replies as conversation evidence, not as keyword hits.

The reply planner is implemented and unit-tested, and the adaptive post-cleaning cycle is now wired into the run lifecycle. It can run a diversified refinement wave and then a direct-reply deepening wave under the same remaining budget, re-normalize, deduplicate, re-clean, and stop truthfully if the analyzable target is still unavailable. **This lifecycle has not yet been verified with a real paid Apify run**, so no claim is made that 500/1,000 analyzable items are guaranteed in a sparse period.

## Allwyn offline simulation

Using the two user-provided Xquik exports from 2026-09-05:

- broad run: 69 rows;
- top author: `sport24`, 28/69 (40.58%);
- detected dominant compound: `Allwyn arena`, 45 rows among core-term matches (~67.16%), across 12 distinct authors;
- action: `split_and_cap_not_delete`;
- Smart Collection v2 first wave for target 500: 6 intent searches, max 84 rows per search target;
- after detecting `Arena`: 7 intent searches, max 72 per target, with `Allwyn arena` retained as a separate bucket;
- refined manual run: 21 rows; only 3 collected rows report at least one direct reply, so this 15-day X window does **not** currently provide evidence that 500 direct replies are available.

See `evals/allwyn_smart_collection_v2_simulation.json`.

## Acceptance rule

Do not call this production/live accepted until:

1. tiny real paid Xquik tests verify the current Actor schema and output mapping;
2. the adaptive lifecycle is observed end-to-end on real X data (refinement → re-clean → replies → re-clean);
3. a 500-target and 1,000-target stress case is run without junk fill and with a truthful shortfall when supply is insufficient;
4. the final sample is checked for relevance, source/author concentration, duplicates, comments context, and budget accounting.
