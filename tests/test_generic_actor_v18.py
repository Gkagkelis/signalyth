from __future__ import annotations
from datetime import date

from app.models import AnalysisDraft
from app.services.query_planner import make_generic_source_plan
from app.services.normalizer import normalize_item
from app.services import collector


def draft(source="x"):
    return AnalysisDraft(client="Vodafone", topic="Vodafone Internet", market="Greece",
        date_from=date(2026,8,1), date_to=date(2026,8,31), keywords=["Vodafone Internet"],
        sources=[source], sample_target=12, comments=True, max_budget_usd=2.0, report_language="Ελληνικά")


def test_generic_array_query_is_safely_batched_and_keeps_all_mapped_fields():
    cfg={"actor_id":"owner/new", "adapter_mode":"generic", "price_per_1000_hint":1.0,
         "input_mapping":{"query":"queries","max_items":"limit","date_from":"after","date_to":"before","country":"gl","language":"hl","comments":"withComments"},
         "input_schema_fields":{"queries":{"type":"array"},"limit":{"type":"integer"},"after":{"type":"string"},"before":{"type":"string"},"gl":{"type":"string"},"hl":{"type":"string"},"withComments":{"type":"boolean"}},
         "input_template":{}}
    p=make_generic_source_plan("x",12,draft(),["q1","q2","q3"],cfg,1.0)
    assert len(p.subruns)==2
    assert sum(sr.target_items for sr in p.subruns)==12
    assert [q for sr in p.subruns for q in sr.input["queries"]] == ["q1","q2","q3"]
    assert all(len(sr.input["queries"]) <= 2 for sr in p.subruns)
    for sr in p.subruns:
        inp=sr.input
        assert inp["limit"] == sr.target_items
        assert inp["after"] == "2026-08-01" and inp["before"] == "2026-08-31"
        assert inp["gl"] == "gr" and inp["hl"] == "el" and inp["withComments"] is True
        assert sr.exact_post_filter is True


def test_generic_string_query_splits_subruns_without_overshooting_target():
    cfg={"actor_id":"owner/new", "adapter_mode":"generic", "price_per_1000_hint":1.0,
         "input_mapping":{"query":"query","max_items":"maxResults"},
         "input_schema_fields":{"query":{"type":"string"},"maxResults":{"type":"integer"}}, "input_template":{}}
    p=make_generic_source_plan("x",10,draft(),["a","b","c"],cfg,1.0)
    assert len(p.subruns)==3
    assert sum(s.target_items for s in p.subruns)==10
    assert [s.input["query"] for s in p.subruns] == ["a","b","c"]
    assert [s.input["maxResults"] for s in p.subruns] == [4,3,3]


def test_instagram_generic_url_only_actor_is_supported():
    cfg={"actor_id":"owner/ig", "adapter_mode":"generic", "price_per_1000_hint":1.0,
         "input_mapping":{"urls":"startUrls","max_items":"limit"},
         "input_schema_fields":{"startUrls":{"type":"array"},"limit":{"type":"integer"}}, "input_template":{}}
    p=make_generic_source_plan("instagram",8,draft("instagram"),["unused"],cfg,1.0)
    assert len(p.subruns)==1
    assert p.subruns[0].input["startUrls"]
    assert all("instagram.com/explore/tags" in x for x in p.subruns[0].input["startUrls"])


def test_generic_rebalancing_resizes_verified_mapped_limit(monkeypatch):
    fake_registry={"x":{"adapter_mode":"generic","input_mapping":{"max_items":"limit"},"input_schema_fields":{"limit":{"type":"integer"}}}}
    monkeypatch.setattr(collector,"load_registry",lambda:fake_registry)
    resized=collector._resize_input("x",{"query":"x","limit":3},3,11)
    assert resized["limit"]==11
    assert "maxItems" not in resized


def test_normalizer_prefers_verified_nested_mapping_but_keeps_raw():
    item={"payload":{"body":"Mapped text","published":"2026-08-03T12:00:00Z","link":"https://e/1","creator":{"handle":"bob"}},
          "text":"wrong fallback"}
    mapping={"text":"payload.body","date":"payload.published","url":"payload.link","author":"payload.creator.handle"}
    row=normalize_item("x",item,mapping)
    assert row["text"]=="Mapped text" and row["author"]=="bob"
    assert row["date"].startswith("2026-08-03") and row["url"]=="https://e/1"
    assert row["raw_data"] is item
