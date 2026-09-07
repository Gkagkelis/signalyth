from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import pytest
from app.services.analysis_budget import BudgetLedger, BudgetExceeded, BudgetError


def test_concurrent_reservations_do_not_spend_the_same_budget_twice():
    ledger = BudgetLedger("1.00")
    def attempt(i):
        try:
            return ledger.reserve(str(i), ".30")
        except BudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=16) as pool:
        accepted = list(pool.map(attempt, range(100)))
    assert sum(accepted) == 3
    assert Decimal(ledger.snapshot()["reserved"]) == Decimal(".90")


def test_duplicate_and_unknown_operations_are_not_retried_or_released():
    ledger = BudgetLedger("1")
    assert ledger.reserve("batch-1", ".8")
    ledger.mark_unknown("batch-1", provider_id="resp-1")
    assert not ledger.reserve("batch-1", ".8")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("batch-2", ".3")
    with pytest.raises(BudgetError):
        ledger.cancel_before_dispatch("batch-1")
    assert ledger.settle("batch-1", ".5", provider_id="resp-1")
    assert ledger.reserve("batch-2", ".3")
    assert ledger.snapshot()["spent"] == "0.5"


def test_actual_overage_is_visible_and_blocks_new_work():
    ledger = BudgetLedger("1")
    ledger.reserve("a", ".5")
    ledger.settle("a", "1.2")
    assert ledger.snapshot()["over_limit"]
    assert ledger.snapshot()["spent"] == "1.2"
    with pytest.raises(BudgetExceeded):
        ledger.reserve("b", ".01")


def test_only_undispatched_work_can_release_a_reservation():
    ledger = BudgetLedger("1")
    ledger.reserve("a", ".9")
    ledger.cancel_before_dispatch("a")
    assert ledger.reserve("b", "1")
    assert ledger.snapshot()["available"] == "0"


def test_invalid_amounts_and_conflicting_settlements_fail_closed():
    with pytest.raises(ValueError):
        BudgetLedger(0)
    ledger = BudgetLedger("1")
    for amount in [float("nan"), float("inf"), -1, 0]:
        with pytest.raises(ValueError):
            ledger.reserve("invalid", amount)
    ledger.reserve("a", ".2")
    ledger.settle("a", ".1")
    assert not ledger.settle("a", ".1")
    with pytest.raises(BudgetError):
        ledger.settle("a", ".2")


def test_dispatched_work_cannot_be_cancelled_as_unspent():
    ledger = BudgetLedger("1")
    ledger.reserve("a", ".9")
    ledger.mark_dispatched("a")
    with pytest.raises(BudgetError):
        ledger.cancel_before_dispatch("a")
    ledger.mark_unknown("a")
    assert ledger.snapshot()["reserved"] == "0.9"
