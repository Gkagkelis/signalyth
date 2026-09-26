"""v31.8 — the runs list must never disagree with the run about being finished.

Seen in production (2026-09-26): a run detail modal said "Ολοκληρώθηκε ·
τελευταίο σήμα 40s πριν" while the list behind it still showed yesterday's
"Εκτελείται". Three holes, all in the list's polling:

1. One failed fetch (laptop asleep overnight, brief offline) killed the whole
   renderRuns() setTimeout chain — nothing ever re-armed it.
2. A background tab throttles timers; returning to it kept showing the frozen
   DOM until some unrelated navigation.
3. The detail modal keeps its own live poll (it re-arms even on errors), so it
   learns the truth first — but never told the stale list behind it.
"""
from pathlib import Path

HTML = Path("app/templates/index.html").read_text(encoding="utf-8")


def test_list_poll_re_arms_after_a_failed_fetch():
    assert "runsPollTimer=setTimeout(()=>renderRuns(),5000)" in HTML


def test_returning_to_the_tab_refreshes_the_list():
    assert "document.addEventListener('visibilitychange'" in HTML
    assert "window.addEventListener('focus'" in HTML
    # The wake-up refresh must bypass the no-flicker cache, or it repaints nothing.
    assert "lastRunsHtml='';renderRuns()" in HTML


def test_a_terminal_run_detail_repaints_the_list_behind_it():
    assert "lastRunsHtml='';if(current==='runs')renderRuns();" in HTML
