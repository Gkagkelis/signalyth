from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch(rel: str, old: str, new: str, count: int = 1):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"final hotfix anchor missing in {rel}: {old[:140]!r}")
    p.write_text(text.replace(old, new, count), encoding="utf-8")


# Topic-first means exactly what the research specification says: exhaust the
# canonical topic first. Aliases and contexts are fallback/top-up routes.
patch(
    "app/services/query_planner.py",
    '''    if draft.search_strategy == "topic_first":
        primary = uniq([topic, *aliases])
        topups = uniq([*anchored_required, *anchored_context, *anchored_watch, *anchored_market])
''',
    '''    if draft.search_strategy == "topic_first":
        primary = [topic]
        topups = uniq([*aliases, *anchored_required, *anchored_context, *anchored_watch, *anchored_market])
''',
)

# A provider runtime total above a tiny pay-per-event attempt cap is not itself
# a SIGNALYTH budget violation. This integration test now simulates a true
# logical-envelope violation, which must still stop all later paid sources.
patch(
    "tests/test_v184_safety_final.py",
    'return {"status": "SUCCEEDED", "usageTotalUsd": max_charge_usd + 0.25}, [{',
    'return {"status": "SUCCEEDED", "usageTotalUsd": max_charge_usd + 5.0}, [{',
)

print("master30 final regression hotfixes applied")
