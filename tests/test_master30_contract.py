from datetime import date

from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.normalizer import normalize_item, classify_source_row
from app.services.intelligence import _impact_components


def draft(**kw):
    base=dict(client="test",topic="Αλέξης Τσίπρας",market="Greece",date_from=date(2026,9,2),date_to=date(2026,9,4),keywords=[],sources=["x","tiktok","instagram","facebook","youtube","news"],sample_mode="perSource",per_source={s:50 for s in ["x","tiktok","instagram","facebook","youtube","news"]},max_budget_usd=2.0,smart_search=True,report_language="Ελληνικά",search_strategy="topic_first")
    base.update(kw);return AnalysisDraft(**base)


# v30: a topic-only brief is still valid, but one route may no longer swallow
# the whole target — the first wave is DIVERSIFIED across several routes that
# together cover the target. These pins now guard that contract instead of the
# old single-route one.
def test_topic_only_is_valid_and_facebook_uses_whole_target():
    plan=build_collection_plan(draft())
    fb=next(x for x in plan.sources if x.source=="facebook")
    assert len(fb.subruns)>=2, "topic-only facebook lost its diversified first wave"
    assert sum(sr.target_items for sr in fb.subruns)==50
    for sr in fb.subruns:
        assert sr.input["query"].strip() and sr.input["resultsCount"]==sr.target_items


def test_x_has_global_target_with_per_query_coverage_guard():
    plan=build_collection_plan(draft())
    x=next(x for x in plan.sources if x.source=="x")
    assert x.subruns, "topic-only x planned no routes at all"
    assert sum(sr.input["maxItems"] for sr in x.subruns)==50
    assert all(sr.input.get("maxItemsPerTarget", 0) >= 1 for sr in x.subruns)


def test_instagram_uses_direct_hashtag_content_route_and_metadata_is_not_evidence():
    plan=build_collection_plan(draft())
    ig=next(x for x in plan.sources if x.source=="instagram")
    assert ig.subruns[0].input.get("directUrls") and ig.subruns[0].input.get("resultsType")=="posts"
    assert "searchType" not in ig.subruns[0].input
    assert classify_source_row("instagram",{"hashtag":"tsipras","postsCount":123,"url":"https://instagram.com/explore/tags/tsipras/"})=="metadata"


def test_tiktok_real_nested_output_normalizes():
    row=normalize_item("tiktok",{"id":"v1","desc":"Τσίπρας στη ΔΕΘ","createTime":1788372000,"webVideoUrl":"https://tiktok/v1","author":{"uniqueId":"u"},"authorStats":{"followerCount":321},"stats":{"playCount":1000,"diggCount":80,"commentCount":9,"shareCount":4}})
    assert row["text"].startswith("Τσίπρας") and row["author"]=="u" and row["views"]==1000 and row["likes"]==80 and row["comments"]==9 and row["shares"]==4
    assert row["metric_availability"]["views_known"] is True and row["date"]


def test_facebook_nested_output_normalizes():
    row=normalize_item("facebook",{"postId":"p1","postText":"κείμενο","timestamp":"2026-09-03T10:00:00Z","url":"https://fb/p1","author":{"name":"Page"},"reactionsCount":55,"commentsCount":7,"sharesCount":3})
    assert row["author"]=="Page" and row["likes"]==55 and row["comments"]==7 and row["shares"]==3


def test_youtube_publish_date_and_channel_name_normalize():
    row=normalize_item("youtube",{"videoId":"y1","title":"Video","publishDate":"Sep 03, 2026","url":"https://youtube/y1","channel":{"name":"Channel"},"viewCount":120})
    assert row["author"]=="Channel" and row["date"].startswith("2026-09-03") and row["views"]==120


def test_missing_metrics_are_unknown_not_zero_impact():
    row=normalize_item("news",{"title":"Article","publishedAt":"2026-09-03T10:00:00Z","link":"https://news/a"})
    assert row["views"]==0 and row["metric_availability"]["views_known"] is False
    impact=_impact_components(row,{"news":{},"__global__":{}})
    assert impact["impact_score"]==0.5 and impact["impact_confidence"]==0.0


def test_context_first_uses_context_as_primary_not_broad_topic():
    d=draft(keywords=["ΔΕΘ"],keyword_roles={"ΔΕΘ":"required_context"},search_strategy="context_first")
    plan=build_collection_plan(d);fb=next(x for x in plan.sources if x.source=="facebook")
    assert fb.subruns[0].input["query"]=="Αλέξης Τσίπρας ΔΕΘ"
