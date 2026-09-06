"""Run the checked-in Greek/Greeklish semantic eval set against the configured OpenAI models.

No request is made unless OPENAI_API_KEY is configured. This is intentionally separate from unit tests
so CI/offline validation never spends API money.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services.ai_analysis import OpenAIResponsesProvider, analyze_records

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "evals" / "semantic_cases.json").read_text(encoding="utf-8"))


def make_row(case):
    return {
        "id": case["id"], "platform": "x", "text": case["text"], "date": "2026-08-15T10:00:00+00:00",
        "author": "eval", "followers": 100, "views": 100, "likes": 1, "comments": 0, "shares": 0,
        "url": f"https://eval.invalid/{case['id']}", "content_type": "post", "parent_post": None, "raw_data": {},
        "cleaning": {"decision":"trusted", "relevance_score":0.65, "market_score":0.8, "account_type":"person_or_creator", "content_class":"organic", "origin_class":"earned_person", "organic_eligible":True},
    }


def main():
    provider = OpenAIResponsesProvider()
    plan = {"client":"OPAP", "topic":"Eurojackpot", "market":"Greece", "date_from":"2026-08-01", "date_to":"2026-08-31", "core_terms":["Eurojackpot"], "context_terms":["OPAP","ΟΠΑΠ","Greece","Ελλάδα"], "greeklish_variants":["ellada","kerdisa"], "exclusions":["KNVB"], "report_language":"Ελληνικά"}
    result = analyze_records([make_row(c) for c in CASES], plan, provider)
    actual = {r["id"]: r["ai_analysis"] for r in result["analyzed"]}
    fields = {"relevance":"semantic_relevance", "sentiment":"sentiment_label", "emotion":"primary_emotion", "sarcasm":"sarcasm", "language":"language", "stance":"target_stance"}
    totals = {k:[0,0] for k in fields}
    details=[]
    for case in CASES:
        a=actual[case["id"]]
        row={"id":case["id"],"decision":a["decision"],"model":a.get("model"),"checks":{}}
        for expected_key, actual_key in fields.items():
            allowed=case["expected"][expected_key]
            got=a[actual_key]
            ok=got in allowed
            totals[expected_key][0]+=int(ok); totals[expected_key][1]+=1
            row["checks"][expected_key]={"ok":ok,"got":got,"allowed":allowed}
        details.append(row)
    summary={k:round(ok/n,4) if n else 0 for k,(ok,n) in totals.items()}
    output={"summary":summary,"details":details,"analysis_report":result["report"]}
    out=ROOT/"evals"/"last_semantic_eval.json"
    out.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print(f"Wrote {out}")

if __name__ == "__main__":
    main()
