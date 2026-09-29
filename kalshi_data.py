"""Kalshi public API client (no authentication needed).

Fixes vs. the original:
- Kalshi now returns prices as dollar strings (``yes_ask_dollars: "0.5600"``);
  the old ``yes_price`` / ``no_price`` fields don't exist, so the bot silently
  used its 0.50 default for every price.
- ``GET /markets/{ticker}`` wraps the payload as ``{"market": {...}}`` and the
  orderbook as ``{"orderbook_fp": {...}}``; the original read the wrapper, so
  settlement status and order books were never actually found.
- The target price comes from ``floor_strike`` instead of regexing the title.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from config import config

logger = logging.getLogger("kalshi_bot")

KALSHI_EVENTS_URL = f"{config.KALSHI_BASE_URL}/events"
KALSHI_MARKETS_URL = f"{config.KALSHI_BASE_URL}/markets"


# ── Helpers ─────────────────────────────────────────────────────────────────

def _request(url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Make a GET request; return parsed JSON or None on failure."""
    try:
        resp = httpx.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("HTTP %s from %s params=%s", exc.response.status_code, url, params)
    except httpx.TimeoutException:
        logger.warning("Timeout fetching %s", url)
    except httpx.RequestError as exc:
        logger.warning("Request error fetching %s: %s", url, exc)
    except Exception as exc:
        logger.exception("Unexpected error fetching %s: %s", url, exc)
    return None


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price(market: dict[str, Any], name: str) -> float | None:
    """Read a price in dollars. Prefers ``<name>_dollars``; falls back to the
    legacy integer-cents ``<name>`` field."""
    v = _f(market.get(f"{name}_dollars"))
    if v is not None:
        return v
    v = _f(market.get(name))
    return v / 100.0 if v is not None else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_quote(market: dict[str, Any]) -> dict[str, float | None]:
    """Normalized prices (dollars) from a market object."""
    return {
        "yes_bid": _price(market, "yes_bid"),
        "yes_ask": _price(market, "yes_ask"),
        "no_bid": _price(market, "no_bid"),
        "no_ask": _price(market, "no_ask"),
        "last": _price(market, "last_price"),
    }


def valid_ask(price: float | None) -> bool:
    return price is not None and 0.0 < price < 1.0


def minutes_left(market: dict[str, Any]) -> float | None:
    """Minutes until the market closes (settlement window end)."""
    close = parse_iso(market.get("close_time"))
    if close is None:
        return None
    return (close - datetime.now(timezone.utc)).total_seconds() / 60.0


# ── Public API ──────────────────────────────────────────────────────────────

def get_current_event() -> dict[str, Any] | None:
    """Fetch the currently-live open KXBTC15M event with nested markets.

    If several events are open, pick the one that closes soonest (the live
    window), not the most recently created.
    """
    params: dict[str, Any] = {
        "series_ticker": config.KALSHI_SERIES,
        "status": "open",
        "with_nested_markets": True,
    }
    data = _request(KALSHI_EVENTS_URL, params=params)
    if data is None:
        return None
    events = data.get("events", [])
    if not events:
        logger.info("No open %s events found", config.KALSHI_SERIES)
        return None

    now = datetime.now(timezone.utc)
    far_future = datetime.max.replace(tzinfo=timezone.utc)

    def next_close(event: dict[str, Any]) -> datetime:
        closes = [
            c for c in (parse_iso(m.get("close_time")) for m in event.get("markets", []))
            if c is not None and c > now
        ]
        return min(closes) if closes else far_future

    events.sort(key=next_close)
    return events[0]


def _levels(raw: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for lvl in raw or []:
        try:
            out.append((float(lvl[0]), float(lvl[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def get_order_book(ticker: str, depth: int = 10) -> dict[str, list[tuple[float, float]]] | None:
    """Order book as ``{"yes": [(price, qty)...], "no": [...]}`` in dollars.

    Kalshi returns BIDS only, ascending. Levels are sorted best (highest) first.
    """
    data = _request(f"{KALSHI_MARKETS_URL}/{ticker}/orderbook", params={"depth": depth})
    if not data:
        return None
    book = data.get("orderbook_fp") or data.get("orderbook") or {}
    if "yes_dollars" in book or "no_dollars" in book:
        yes = _levels(book.get("yes_dollars"))
        no = _levels(book.get("no_dollars"))
    else:  # legacy cents format
        yes = [(p / 100.0, q) for p, q in _levels(book.get("yes"))]
        no = [(p / 100.0, q) for p, q in _levels(book.get("no"))]
    yes.sort(reverse=True)
    no.sort(reverse=True)
    return {"yes": yes, "no": no}


def get_recent_trades(ticker: str, limit: int = 10) -> list[dict[str, Any]]:
    """Recent trades for a market ticker (raw dicts)."""
    data = _request(f"{KALSHI_MARKETS_URL}/trades", params={"ticker": ticker, "limit": limit})
    if data is None:
        return []
    return data.get("trades", [])


def get_market(ticker: str) -> dict[str, Any] | None:
    """Fetch a single market by ticker. Returns the unwrapped market object."""
    data = _request(f"{KALSHI_MARKETS_URL}/{ticker}")
    if data is None:
        return None
    return data.get("market", data)


def compute_target_price(market: dict[str, Any]) -> float | None:
    """Strike BTC must finish above for YES, from ``floor_strike``."""
    for key in ("floor_strike", "cap_strike"):
        v = _f(market.get(key))
        if v is not None and v > 0:
            return v
    return None


def parse_market_id(ticker: str) -> dict[str, str] | None:
    """Split a KXBTC15M ticker like ``KXBTC15M-26SEP291545-45`` into parts."""
    m = re.match(r"^([A-Z0-9]+)-(\d{2}[A-Z]{3}\d{2})(\d{4})-(\w+)$", ticker)
    if not m:
        return None
    return {"series": m.group(1), "date": m.group(2), "hhmm": m.group(3), "suffix": m.group(4)}


def interpret_yes_price(price: float) -> str:
    """Interpret the YES price as a prediction."""
    if price >= 0.90:
        return "very_likely_up"
    elif price >= 0.70:
        return "likely_up"
    elif price >= 0.55:
        return "leaning_up"
    elif price <= 0.10:
        return "very_likely_down"
    elif price <= 0.30:
        return "likely_down"
    elif price <= 0.45:
        return "leaning_down"
    return "uncertain"
