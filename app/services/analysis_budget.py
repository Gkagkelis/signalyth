"""Thread-safe accounting for bounded, independent provider operations.

This is an accounting component, not a provider-enforced spending limit.
Callers must supply a defensible maximum charge before dispatch. An estimate
alone cannot guarantee a hard cap. Unknown billing outcomes retain their
reservation and require reconciliation rather than an automatic retry.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from threading import RLock


class BudgetError(RuntimeError):
    pass


class BudgetExceeded(BudgetError):
    pass


class BudgetLedger:
    def __init__(self, limit):
        self.limit = self._money(limit)
        if self.limit <= 0:
            raise ValueError("A positive, explicit budget is required")
        self._lock = RLock()
        self._operations = {}
        self._spent = Decimal("0")

    @staticmethod
    def _money(value):
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Invalid monetary amount") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError("Money must be finite and nonnegative")
        return amount

    def _reserved(self):
        return sum((r["reserved"] for r in self._operations.values()
                    if r["state"] in {"reserved", "dispatched", "unknown"}), Decimal("0"))

    def reserve(self, key: str, maximum_cost):
        """Atomically admit one operation, counting every in-flight reservation."""
        if not key:
            raise ValueError("A stable operation key is required")
        amount = self._money(maximum_cost)
        if amount <= 0:
            raise ValueError("A positive maximum charge is required")
        with self._lock:
            if key in self._operations:
                return False  # Never dispatch an existing/unknown operation twice.
            if self._spent + self._reserved() + amount > self.limit:
                raise BudgetExceeded("Insufficient unreserved budget")
            self._operations[key] = {"state": "reserved", "reserved": amount,
                                     "actual": None, "provider_id": None}
            return True

    def mark_dispatched(self, key: str, *, provider_id=None):
        """Mark the operation as sent before making its external call."""
        with self._lock:
            op = self._operations[key]
            if op["state"] != "reserved":
                raise BudgetError("Operation is not awaiting dispatch")
            op["state"] = "dispatched"
            op["provider_id"] = provider_id

    def settle(self, key: str, actual_cost, *, provider_id=None):
        """Record known usage; unexpected overage blocks subsequent admissions."""
        amount = self._money(actual_cost)
        with self._lock:
            op = self._operations[key]
            if op["state"] == "settled":
                if op["actual"] != amount:
                    raise BudgetError("Conflicting settlement for the same operation")
                return False
            if op["state"] not in {"reserved", "dispatched", "unknown"}:
                raise BudgetError("Operation is not awaiting settlement")
            op.update(state="settled", actual=amount, provider_id=provider_id)
            self._spent += amount
            return True

    def mark_unknown(self, key: str, *, provider_id=None):
        """Retain the full reservation when a request may have been billed."""
        with self._lock:
            op = self._operations[key]
            if op["state"] == "settled":
                raise BudgetError("A settled operation cannot become unknown")
            op["state"] = "unknown"
            if provider_id is not None:
                op["provider_id"] = provider_id

    def cancel_before_dispatch(self, key: str):
        """Release only work whose provider dispatch is known not to have begun."""
        with self._lock:
            op = self._operations[key]
            if op["state"] != "reserved":
                raise BudgetError("Cannot release dispatched or unknown work")
            op["state"] = "cancelled_before_dispatch"

    def snapshot(self):
        with self._lock:
            reserved = self._reserved()
            return {"limit": str(self.limit), "spent": str(self._spent),
                    "reserved": str(reserved),
                    "available": str(max(Decimal("0"), self.limit-self._spent-reserved)),
                    "over_limit": self._spent+reserved > self.limit,
                    "operations": {k: {a: str(b) if isinstance(b, Decimal) else b
                                        for a, b in v.items()}
                                   for k, v in self._operations.items()}}
