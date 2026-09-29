"""BTC spot price and realized volatility from Coinbase public endpoints (no auth).

Note: Kalshi's KXBTC15M contracts settle on a reference index, not Coinbase
directly. Coinbase spot is a close proxy, which is fine for this purpose.
"""

import logging
import math
import time

import httpx

logger = logging.getLogger("kalshi_bot")

SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
HEADERS = {"User-Agent": "kalshi-bot/1.0"}

_VOL_TTL_SECONDS = 60
_vol_cache: tuple[float, float] | None = None  # (fetched_at, vol)


def get_spot() -> float | None:
    """Current BTC-USD spot price, or None on failure."""
    try:
        resp = httpx.get(SPOT_URL, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return float(resp.json()["data"]["amount"])
    except Exception as exc:
        logger.warning("Could not fetch BTC spot: %s", exc)
        return None


def get_minute_vol(lookback: int = 60) -> float | None:
    """Std dev of 1-minute log returns over the last ``lookback`` minutes.

    Cached for 60s so polling every 30s doesn't hammer Coinbase.
    """
    global _vol_cache
    now = time.time()
    if _vol_cache and now - _vol_cache[0] < _VOL_TTL_SECONDS:
        return _vol_cache[1]
    try:
        resp = httpx.get(
            CANDLES_URL, params={"granularity": 60}, headers=HEADERS, timeout=10
        )
        resp.raise_for_status()
        # Rows: [time, low, high, open, close, volume], newest first.
        candles = sorted(resp.json(), key=lambda c: c[0])
        closes = [float(c[4]) for c in candles][-(lookback + 1):]
        rets = [
            math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0
        ]
        if len(rets) < 10:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        vol = math.sqrt(var)
        _vol_cache = (now, vol)
        return vol
    except Exception as exc:
        logger.warning("Could not fetch BTC volatility: %s", exc)
        return None
