"""Run the pasted KalshiPanicFadeBot strategy against Kalshi Demo.

The strategy module is used unchanged. This driver supplies BRTI spot,
rolling minute volatility, current KXBTC15M YES quotes, and a signed Demo V2
order sink. Demo only; production hosts are rejected.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import config
from kalshi_brti_feed import stream_brti
from kalshi_data import get_current_event, get_order_book
from jev_decisions import decide_trade
from kxbtc15m_adapter import kxbtc15m_market_data, panic_order_to_demo_v2
from panic_fade_strategy import KalshiPanicFadeBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("panic_fade_demo")

DEMO_REST = "https://external-api.demo.kalshi.co/trade-api/v2"
SIGN_PATH = "/trade-api/v2/portfolio/events/orders"


class BrtiVolatility:
    """Rolling one-minute log-return volatility in per-minute units."""

    def __init__(self, max_points: int = 600):
        self.points: deque[tuple[float, float]] = deque(maxlen=max_points)

    def add(self, timestamp_ms: int | None, value: float) -> None:
        t = (timestamp_ms or int(time.time() * 1000)) / 1000.0
        self.points.append((t, value))
        cutoff = t - 300.0
        while self.points and self.points[0][0] < cutoff:
            self.points.popleft()

    def value(self) -> float | None:
        if len(self.points) < 10:
            return None
        returns: list[float] = []
        previous = self.points[0][1]
        for _, value in list(self.points)[1:]:
            if previous > 0 and value > 0:
                returns.append(math.log(value / previous))
            previous = value
        if len(returns) < 5:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((x - mean) ** 2 for x in returns) / (len(returns) - 1)
        return max(math.sqrt(variance) * math.sqrt(60.0), 1e-9)


class DemoOrderSink:
    def __init__(self) -> None:
        if config.KALSHI_ENV != "demo":
            raise RuntimeError("panic_fade_demo refuses non-demo KALSHI_ENV")
        if config.KALSHI_BASE_URL != DEMO_REST:
            raise RuntimeError("panic_fade_demo resolved an unexpected REST host")

    def _key(self) -> Any:
        path = Path(config.KALSHI_PRIVATE_KEY_PATH)
        return serialization.load_pem_private_key(path.read_bytes(), password=None)

    def _headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path}".encode()
        signature = self._key().sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "Content-Type": "application/json",
        }

    def __call__(self, order: dict[str, Any]) -> None:
        payload = panic_order_to_demo_v2(order)
        payload["client_order_id"] = str(uuid4())
        response = httpx.post(
            DEMO_REST + "/portfolio/events/orders",
            json=payload,
            headers=self._headers("POST", SIGN_PATH),
            timeout=15,
        )
        logger.info(
            "DEMO PANIC-FADE ORDER status=%s ticker=%s side=%s price=%s count=%s",
            response.status_code,
            payload["ticker"],
            payload["side"],
            payload["price"],
            payload["count"],
        )
        response.raise_for_status()
        result = response.json()
        logger.info(
            "DEMO ORDER RESULT order_id=%s fill_count=%s remaining_count=%s",
            result.get("order_id"),
            result.get("fill_count"),
            result.get("remaining_count"),
        )


class PanicFadeDemo:
    def __init__(self) -> None:
        self.volatility = BrtiVolatility()
        self.spot: float | None = None
        self.candidate_order: dict[str, Any] | None = None
        self.demo_sink = DemoOrderSink()
        self.bot = KalshiPanicFadeBot(
            vol_provider=self.volatility.value,
            order_sink=self._capture_candidate,
        )

    def _capture_candidate(self, order: dict[str, Any]) -> None:
        """Collect the literal strategy candidate; JEV decides before submission."""
        self.candidate_order = order

    async def brti_loop(self) -> None:
        async for reading in stream_brti():
            self.spot = reading.value
            self.volatility.add(reading.source_ts_ms, reading.value)
            self.bot.on_btc_spot_update(reading.value)

    async def market_loop(self) -> None:
        while True:
            try:
                event = await asyncio.to_thread(get_current_event)
                markets = (event or {}).get("markets", [])
                for market in markets:
                    ticker = market.get("ticker")
                    if not ticker:
                        continue
                    book = await asyncio.to_thread(get_order_book, ticker)
                    yes = (book or {}).get("yes", [])
                    best_bid = yes[0]["price"] if yes else None
                    try:
                        md = kxbtc15m_market_data(
                            market,
                            best_ask=float(market["yes_ask_dollars"]),
                            best_bid=best_bid,
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        logger.info("PANIC-FADE skip ticker=%s reason=%s", ticker, exc)
                        continue
                    if self.spot is not None:
                        jctx = {
                                    "yes_price": float(market.get("yes_ask_dollars", 0.5)),
                                    "no_price": float(market.get("no_ask_dollars", 0.5)),
                                    "market_ticker": ticker,
                                    "target_price": md["strike_price"],
                                    "seconds_to_close": max(0, int(md["minutes_left"] * 60)),
                                    "brti_price": self.spot,
                                    "brti_avg_60s": self.spot,
                                    "brti_final_minute_avg_15m": None,
                                    "brti_final_minute_window_size": 0,
                                    "order_book_yes": [],
                                    "order_book_no": [],
                                    "recent_trades": [],
                                }
                        event_for_jev = {
                                    "event_ticker": event.get("event_ticker", ticker),
                                    "markets": [market],
                                }
                        verdict = decide_trade(event_for_jev, jctx)
                        expected = "down" if md["direction"] == "above" else "up"
                        trigger = verdict.should_trade and verdict.direction == expected
                        logger.info(
                            "PANIC-FADE JEV trigger=%s direction=%s expected=%s conviction=%d",
                            trigger, verdict.direction, expected, verdict.conviction,
                        )
                        if trigger:
                            self.candidate_order = None
                            st = self.bot.markets.setdefault(ticker, __import__("panic_fade_strategy").MarketState())
                            result = self.bot._evaluate(md, st, md["best_ask"], self.bot.clock())
                            if result.get("fade") and self.candidate_order:
                                self.demo_sink(self.candidate_order)
                expired = self.bot.expire_orders()
                for order in expired:
                    logger.info("PANIC-FADE order TTL expired client_order_id=%s", order["client_order_id"])
            except Exception:
                logger.exception("PANIC-FADE market loop error")
            await asyncio.sleep(2)

    async def run(self) -> None:
        logger.info("PANIC-FADE DEMO starting env=%s rest=%s", config.KALSHI_ENV, config.KALSHI_BASE_URL)
        await asyncio.gather(self.brti_loop(), self.market_loop())


if __name__ == "__main__":
    asyncio.run(PanicFadeDemo().run())
