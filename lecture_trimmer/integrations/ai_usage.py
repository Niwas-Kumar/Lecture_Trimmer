"""Job-scoped accounting from PW responses, without storing prompts or credentials."""
import threading
from decimal import Decimal, InvalidOperation


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and 0 <= amount <= Decimal("1e12") else None
    except InvalidOperation:
        return None


def _tokens(value):
    amount = _amount(value)
    return int(amount) if amount is not None and amount == amount.to_integral_value() else None


class AIUsage:
    def __init__(self):
        self._lock = threading.Lock()
        self._calls = []

    def record(self, model, response=None, *, failed=False):
        data = response if isinstance(response, dict) else {}
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        # PW owns billing. Never infer a price from token counts or missing data.
        cost = _amount(data.get("cost_inr"))
        entry = {"model": str(model), "failed": bool(failed), "cost": cost,
                 "input": _tokens(usage.get("tokens_in")),
                 "output": _tokens(usage.get("tokens_out"))}
        with self._lock:
            self._calls.append(entry)

    def snapshot(self):
        with self._lock:
            calls = list(self._calls)
        priced = [c["cost"] for c in calls if c["cost"] is not None]
        result = {"source": "pw_proxy", "currency": "INR", "requests": len(calls),
                  "failed_requests": sum(c["failed"] for c in calls),
                  "priced_requests": len(priced),
                  "reported_cost_inr": str(sum(priced, Decimal(0))) if priced or not calls else None,
                  "models": sorted({c["model"] for c in calls})}
        for name in ("input", "output"):
            values = [c[name] for c in calls if c[name] is not None]
            result[name + "_tokens"] = sum(values) if values or not calls else None
            result[name + "_reported_requests"] = len(values)
        return result
