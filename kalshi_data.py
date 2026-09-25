"""Kalshi public API client (no authentication needed).

Functions to fetch the latest KXBTC15M event, order books, and recent trades
from the Kalshi public trade API.
"""
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from config import config

logger = logging.getLogger("kalshi_bot")

KALSHI_EVENTS_URL = f"{config.KALSHI_BASE_URL}/events"
KALSHI_MARKETS_URL = f"{config.KALSHI_BASE_URL}/markets"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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


# ── Public API ──────────────────────────────────────────────────────────────


def get_current_event() -> dict[str, Any] | None:
    """Fetch the latest open KXBTC15M event with nested markets.

    Returns the raw event dict (first/most recent open event) or None.
    The event contains a ``markets`` list of nested market dicts.
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

    # Sort by created_at descending, take the most recent
    events.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    event = events[0]
    logger.debug(
        "Found event %s (tick_size=%s) with %d nested markets",
        event.get("event_ticker"),
        event.get("tick_size"),
        len(event.get("markets", [])),
    )
    return event


def get_order_book(ticker: str, depth: int = 10) -> dict[str, Any] | None:
    """Fetch order book depth for a market ticker.

    Returns dict with ``yes`` and ``no`` sides, each containing
    lists of {price, count}.
    """
    url = f"{KALSHI_MARKETS_URL}/{ticker}/orderbook"
    data = _request(url, params={"depth": depth})
    return data


def get_recent_trades(ticker: str, limit: int = 10) -> list[dict[str, Any]]:
    """Fetch recent trades for a market ticker.

    Returns list of trade dicts (each with price, count, side, etc.).
    """
    url = f"{KALSHI_MARKETS_URL}/trades"
    data = _request(url, params={"ticker": ticker, "limit": limit})
    if data is None:
        return []
    return data.get("trades", [])


def get_market(ticker: str) -> dict[str, Any] | None:
    """Fetch a single market by its ticker (for settlement status, etc.)."""
    url = f"{KALSHI_MARKETS_URL}/{ticker}"
    return _request(url)


def parse_market_id(event_ticker: str) -> tuple[str, int] | None:
    """Extract the target price and expiry minute from a KXBTC15M ticker.

    Ticker format: KXBTC15M-26APR100545-45
    → target price: 1005.45  (divide by 100)
    → expiry minute offset: 45 (minutes past the hour)
    """
    # Format: KXBTC15M-DDMMMHHMMSS-XX
    import re

    m = re.match(r"KXBTC15M-(\d{2}[A-Z]{3}\d{2})(\d{2})(\d{2})-(\d+)", event_ticker)
    if m:
        day_mon_year = m.group(1)  # e.g. 26APR25
        hour = int(m.group(2))  # e.g. 10
        minute = int(m.group(3))  # e.g. 05
        offset = int(m.group(4))  # e.g. 45
        # target_price in cents from event; we return raw cents
        return offset, hour, minute, day_mon_year
    return None


def compute_target_price(market: dict[str, Any]) -> float | None:
    """Extract the target BTC price from a market object.

    The market's title or strike/display fields encode the target.
    e.g. "Will BTC be below $969.30 at settlement?"
    """
    title = market.get("title", "")
    import re

    # Look for $XXX or XXXXXX patterns
    m = re.search(r"\$?(\d+[,.]?\d*)", title)
    if m:
        raw = m.group(1).replace(",", "")
        return float(raw)

    # Fallback: from ticker encoding
    ticker = market.get("ticker", "")
    try:
        # e.g. KXBTC15M-26APR100545-45 → 100545 → 1005.45
        parts = ticker.split("-")
        if len(parts) >= 3:
            raw_price = parts[1][-6:]  # last 6 chars before the second dash... wrong logic
            # Actually format: KXBTC15M-26APR100545-45
            # Between first and second dash: 26APR100545
            dash2 = ticker.find("-", ticker.find("-") + 1)
            if dash2 > 0:
                code = ticker[len(config.KALSHI_SERIES) + 1 : dash2]
                # code = 26APR100545
                # strip the date prefix letters
                price_chars = ""
                for ch in code:
                    if ch.isdigit():
                        price_chars += ch
                # price_chars should be like 100545 (6 digits) or 1005455 (7)
                if len(price_chars) >= 4:
                    val = float(price_chars) / 100.0
                    return val
    except (ValueError, IndexError):
        pass

    return None


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
    else:
        return "uncertain"