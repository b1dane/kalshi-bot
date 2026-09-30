"""KXBTC15M adapter for the literal panic-fade strategy.

The imported strategy remains unchanged. This boundary translates the current
Kalshi Up/Down market schema into its expected above/below input shape.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def minutes_left(market: dict[str, Any], now: datetime | None = None) -> float | None:
    raw = market.get("close_time")
    if not raw:
        return None
    try:
        close = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        return (close - current).total_seconds() / 60.0
    except ValueError:
        return None


def kxbtc15m_market_data(
    market: dict[str, Any],
    *,
    best_ask: float,
    best_bid: float | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Translate a live KXBTC15M market into panic-fade input fields."""
    result = str(market.get("result", "")).lower()
    status = str(market.get("status", "")).lower()
    if status not in {"active", "open", "initialized"} and not result:
        raise ValueError(f"market is not open: status={status!r}")
    strike = market.get("floor_strike")
    if strike is None:
        raise ValueError("KXBTC15M market has no floor_strike reference")
    remaining = minutes_left(market, now=now)
    if remaining is None:
        raise ValueError("KXBTC15M market has no valid close_time")
    return {
        "ticker": market.get("ticker"),
        "strike_price": float(strike),
        "direction": "above",
        "contract_type": "above",
        "best_ask": float(best_ask),
        "best_bid": float(best_bid) if best_bid is not None else None,
        "minutes_left": remaining,
        "settlement_source": "CF Benchmarks BRTI 60-second final-minute average",
        "settlement_rule": market.get("rules_primary", ""),
    }


def panic_order_to_demo_v2(order: dict[str, Any]) -> dict[str, Any]:
    """Translate the supplied strategy's dry-run order to documented V2 shape.

    This function does not submit anything. It only builds a payload.
    """
    if order.get("side") != "no" or order.get("action") != "buy":
        raise ValueError("panic-fade adapter only accepts buy-NO orders")
    price_cents = int(order["no_price"])
    if not 1 <= price_cents <= 99:
        raise ValueError("NO price must be 1..99 cents")
    return {
        "ticker": str(order["ticker"]),
        "client_order_id": str(order["client_order_id"]),
        "side": "ask",
        "count": f"{float(order['count']):.2f}",
        "price": f"{price_cents / 100.0:.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "maker",
        "post_only": True,
        "cancel_order_on_pause": True,
        "reduce_only": False,
    }
