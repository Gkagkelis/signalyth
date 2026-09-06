# Smart Collection v2 — Test Report

Status: **candidate / not production-approved**
Date: 2026-09-05

## Current Actor contract checked

The Xquik X Tweet Scraper public input/output documentation was checked on 2026-09-05 before implementing the candidate fields. The relevant documented inputs include `searchTerms`, `maxItems`, `maxItemsPerTarget`, `includeSearchTerms`, `queryType`, `since`, `until`, `mode: replies`, and `replyTweetIds`.

## Automated tests

The suite was run in three non-overlapping groups after the Smart Collection v2 changes:

- lifecycle / collection / cleaning / planner / v1.8 integrations: **146 passed**;
- AI / intelligence / investigations: **126 passed**;
- presentation / visualization / gold benchmark / stress / UI shell: **134 passed**.

Total: **406 passed / 0 failed**.

New permanent tests cover:

- removing a redundant market suffix (`Allwyn Greece` → `Allwyn` for query planning);
- current Xquik search fields and `Latest` mode;
- bounded multi-intent search instead of one dominant broad query;
- dominant context collision detection (`Allwyn Arena`-like case);
- split-and-cap behavior that retains the property as a separate bucket;
- X direct-reply deepening input and per-seed caps;
- adaptive post-cleaning refinement + reply deepening lifecycle with re-cleaning;
- remaining-budget enforcement during adaptive calls;
- real-style Xquik RFC timestamp parsing and flat author normalization.

Python compilation for the changed modules: **PASS**.

## Deliberate limitation

The automatic adaptive expansion loop is now connected after Step 3 cleaning and tested with fake Actor responses. It still must not be described as fully live/accepted until a tiny real paid Xquik run verifies the lifecycle, current output mapping, cost accounting, and real-world yield.
