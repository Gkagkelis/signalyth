from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def must_replace(rel: str, old: str, new: str, count: int = 1):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"Hotfix anchor not found in {rel}: {old[:140]!r}")
    p.write_text(text.replace(old, new, count), encoding="utf-8")


# _gold_standard_audit has slide_ids but not visual_pack; applicability is derived
# from the generated slide inventory rather than an out-of-scope variable.
must_replace(
    "app/services/presentation.py",
    '        "analyst_findings": bool((visual_pack.get("analyst_synthesis") or {}).get("findings")),\n        "recommendations": bool((visual_pack.get("analyst_synthesis") or {}).get("recommendations")),\n',
    '        "analyst_findings": "analyst_findings" in slide_ids,\n        "recommendations": "recommendations" in slide_ids,\n',
)

print("master30 hotfixes applied")
