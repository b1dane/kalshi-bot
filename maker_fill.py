"""Deterministic paper predicate for resting-maker fills."""
from __future__ import annotations


def would_fill(
    *,
    side: str,
    limit_price: float,
    yes_trade_price: float | None,
    no_trade_price: float | None,
) -> bool:
    if side == "yes":
        return yes_trade_price is not None and yes_trade_price <= limit_price
    if side == "no":
        return no_trade_price is not None and no_trade_price <= limit_price
    return False
