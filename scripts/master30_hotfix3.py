from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / "app/services/query_planner.py"
text = p.read_text(encoding="utf-8")
old = '''        primary = uniq([*primary, *(xin.get("searchTerms") or [])])

    primary = primary or [routes["topic"]]
'''
new = '''        primary = uniq([*primary, *(xin.get("searchTerms") or [])])
        # Balanced X discovery consistently excludes native retweets, including the
        # broad/topic routes added ahead of the intent catalog.
        primary = [q if "-filter:nativeretweets" in q else f"{q} -filter:nativeretweets" for q in primary]

    primary = primary or [routes["topic"]]
'''
if old not in text:
    raise RuntimeError("balanced X normalization anchor missing")
p.write_text(text.replace(old, new, 1), encoding="utf-8")
print("master30 balanced X normalization applied")
