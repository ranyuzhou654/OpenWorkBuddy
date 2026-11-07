"""Optional price snapshots. Subscription quota is not assumed to have zero marginal cost."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def calculate(events: list[dict], price: dict | None) -> dict[str, Any]:
    unknown = {"currency": price.get("currency") if price else None, "amount": None,
               "price_snapshot": price, "complete": False}
    starts = {e["call_id"]: e for e in events if e["type"] == "model.started"}
    responses = {e["call_id"]: e for e in events if e["type"] == "model.response"}
    if (not price or not starts or starts.keys() != responses.keys()
            or any(e["type"] == "ledger.corrupt" for e in events)):
        return unknown
    total = Decimal(0)
    for call_id, start in starts.items():
        r = responses[call_id]
        if (not r.get("usage_complete") or start.get("sdk_max_retries") != 0
                or price.get("provider") != start["provider"] or price.get("model") != start["model"]):
            return unknown
        u = r["usage"]
        if not all(isinstance(u.get(k), int) and u[k] >= 0 for k in ("input", "output", "cache_read")):
            return unknown
        cache_write = u.get("cache_write", 0)
        # A tiered cache-write price cannot be derived from an aggregate cache_write count.
        if cache_write and not price.get("uniform_cache_write", False):
            return unknown
        plain_input = u["input"] - u["cache_read"] - cache_write
        if plain_input < 0:
            return unknown
        counts = {"input": plain_input, "output": u["output"], "cache_read": u["cache_read"],
                  "cache_write": cache_write}
        for k, n in counts.items():
            rate = price.get("per_million", {}).get(k)
            if rate is None and n:
                return unknown
            value = Decimal(str(rate or 0))
            if not value.is_finite() or value < 0:
                raise ValueError("price must be finite and nonnegative")
            total += Decimal(n) * value / Decimal(1_000_000)
    if not price.get("as_of") or not price.get("source") or not price.get("currency"):
        return unknown
    return {**unknown, "amount": float(total), "complete": True}
