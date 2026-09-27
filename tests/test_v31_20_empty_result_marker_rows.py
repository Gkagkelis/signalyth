"""v31.20 — "nobody posted that" must not be reported as a broken integration.

The Christodoulakis run (20260927T013214Z-ee62fa8a) showed Instagram as
FAILED with "Actor returned data, but required normalized/date fields were
unusable. Possible schema or mapping drift." The truth was gentler: the
hashtags of a regional MP's name hold no posts, so apify/instagram-scraper
answered each query with a marker row — {"error": "no_items", ...}, no post
fields, $0.00 charged. Counted as data, four dateless rows tripped the
data-contract heuristic and the whole source was called broken.

A marker row is a diagnostic, not evidence. Then data_items is empty, the
heuristic cannot fire, and the source reads "succeeded, nothing found" —
which is what a client should be told.
"""
from __future__ import annotations

from app.services.resilience import (
    classify_actor_outcome,
    is_diagnostic_row,
    split_diagnostic_rows,
)

INSTAGRAM_NO_ITEMS = {
    "error": "no_items",
    "errorDescription": "We detected that this page does not contain any posts.",
    "url": "https://www.instagram.com/explore/tags/manolischristodoulakis/",
}


def test_an_empty_result_marker_is_a_diagnostic_not_evidence():
    assert is_diagnostic_row(INSTAGRAM_NO_ITEMS) is True
    data, diagnostics = split_diagnostic_rows([INSTAGRAM_NO_ITEMS] * 4)
    assert data == []
    assert len(diagnostics) == 4


def test_a_source_that_only_returned_markers_reads_as_empty_not_failed():
    _data, diagnostics = split_diagnostic_rows([INSTAGRAM_NO_ITEMS] * 4)
    assert classify_actor_outcome({"status": "SUCCEEDED"}, [], diagnostics) == "empty"


def test_a_real_post_carrying_an_error_field_is_still_evidence():
    """Never lose a paid post to an over-eager marker rule."""
    row = {
        "error": "partial_media",
        "caption": "Ο Μανώλης Χριστοδουλάκης στη Βουλή σήμερα",
        "timestamp": "2026-09-12T10:00:00Z",
        "url": "https://www.instagram.com/p/abc/",
    }
    assert is_diagnostic_row(row) is False
    data, diagnostics = split_diagnostic_rows([row])
    assert data == [row] and diagnostics == []


def test_an_ordinary_post_without_any_error_field_is_untouched():
    row = {"caption": "καλημέρα", "timestamp": "2026-09-12T10:00:00Z"}
    assert is_diagnostic_row(row) is False


def test_a_dateless_contentless_row_without_an_error_stays_data():
    """The rule keys on the Actor SAYING it found nothing, not on emptiness."""
    assert is_diagnostic_row({"url": "https://example.com/x"}) is False
