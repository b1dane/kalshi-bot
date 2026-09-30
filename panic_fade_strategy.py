"""Panic-fade strategy (dry run by default: orders go to an injectable sink).

Idea: when a contract's ask spikes with no move in BTC to justify it, sell that
YES at the spike (i.e. buy NO at 1 - ask, post-only) and let it revert.

Fixes vs. the original:
- Direction: the original only checked |spot - strike|, so an ABOVE contract
  spiking while spot was already ABOVE the strike (a justified spike) still
  passed and got "faded". Now fair value is computed per contract direction.
- Fair-value edge instead of a fixed $ distance: edge = ask - fair - fee, using
  the same vol model as pricing.py (accounts for time left and volatility).
- Rolling window with a carried-forward baseline. The original only compared
  two points >= 5s apart, so a 60s-quiet market diluted velocity 12x and
  spike-and-revert inside the window was missed.
- Spot feed timestamp + max age; spread and time-left guards; per-market
  cooldown; exposure cap; monotonic clock; no logging.basicConfig at import.
- Order is the NO side of the same market (price = 1 - ask, cents). The
  original bought YES on a separate "opposing" ticker at 1 - ask, which is a
  different order book with its own spread.

VERIFY the order payload against Kalshi's current create-order schema before
wiring a real sink; field names below are my best understanding.
"""
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from pricing import fee_per_contract, prob_above

logger = logging.getLogger("kalshi_bot")


@dataclass
class MarketState:
    history: deque = field(default_factory=deque)  # (t, best_ask); [0] is carried baseline
    last_fade_at: float = float("-inf")


def _direction(market_data: dict[str, Any]) -> str | None:
    d = str(market_data.get("direction") or market_data.get("contract_type") or "").lower()
    if d.startswith("above"):
        return "above"
    if d.startswith("below"):
        return "below"
    return None


class KalshiPanicFadeBot:
    """Expected market_data keys: ticker, strike_price, direction|contract_type
    ("above*"/"below*"), best_ask, best_bid, minutes_left."""

    def __init__(
        self,
        vol_provider: Callable[[], float | None],
        order_sink: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        *,
        window: float = 5.0,
        min_move: float = 0.20,        # ask rise within the window that counts as a spike
        max_spread: float = 0.15,      # skip gapped/thin books
        min_edge: float = 0.08,        # $/contract over fair value, after fee
        min_minutes_left: float = 2.0,
        max_spot_age: float = 2.0,
        cooldown: float = 120.0,       # seconds between fades on one market
        order_size: int = 10,
        max_open_contracts: int = 30,
        order_ttl: float = 30.0,       # seconds before an unfilled order should be cancelled
    ):
        self.vol_provider = vol_provider
        self.order_sink = order_sink or (lambda o: logger.critical("DRY-RUN ORDER %s", o))
        self.clock = clock
        self.window = window
        self.min_move = min_move
        self.max_spread = max_spread
        self.min_edge = min_edge
        self.min_minutes_left = min_minutes_left
        self.max_spot_age = max_spot_age
        self.cooldown = cooldown
        self.order_size = order_size
        self.max_open_contracts = max_open_contracts
        self.order_ttl = order_ttl

        self.spot: float | None = None
        self.spot_ts: float | None = None
        self.markets: dict[str, MarketState] = {}
        self.pending: dict[str, dict[str, Any]] = {}  # client_order_id -> order + expires_at

    # ── Feeds ──────────────────────────────────────────────────────────────

    def on_btc_spot_update(self, spot_price: float) -> None:
        self.spot = spot_price
        self.spot_ts = self.clock()

    def process_order_book_update(self, market_data: dict[str, Any]) -> dict[str, Any] | None:
        """Returns None if no spike, else a decision dict {fade, reason, order}."""
        now = self.clock()
        ticker = market_data.get("ticker")
        ask = market_data.get("best_ask")
        if not ticker or ask is None or not (0.0 < ask < 1.0):
            return None

        st = self.markets.setdefault(ticker, MarketState())
        st.history.append((now, ask))
        # Keep exactly one point at/before the window start: on an event-driven
        # stream a quiet market held that price until now.
        cutoff = now - self.window
        while len(st.history) >= 2 and st.history[1][0] <= cutoff:
            st.history.popleft()

        move = ask - min(p for _, p in st.history)
        if move < self.min_move:
            return None

        logger.warning(
            "Panic on %s: ask=%.2f rose %.2f within %.1fs (%.4f/sec)",
            ticker, ask, move, self.window, move / self.window,
        )
        return self._evaluate(market_data, st, ask, now)

    # ── Decision ───────────────────────────────────────────────────────────

    def _reject(self, ticker: str, reason: str) -> dict[str, Any]:
        logger.info("[%s] fade rejected: %s", ticker, reason)
        return {"fade": False, "reason": reason, "order": None}

    def _evaluate(
        self, md: dict[str, Any], st: MarketState, ask: float, now: float
    ) -> dict[str, Any]:
        ticker = md["ticker"]

        if now - st.last_fade_at < self.cooldown:
            return self._reject(ticker, "cooldown")

        direction = _direction(md)
        if direction is None:
            return self._reject(ticker, "unknown contract direction")

        if self.spot is None or self.spot_ts is None or now - self.spot_ts > self.max_spot_age:
            return self._reject(ticker, "spot feed missing or stale")

        mins = md.get("minutes_left")
        if mins is None or mins < self.min_minutes_left:
            return self._reject(ticker, f"minutes_left={mins} below {self.min_minutes_left}")

        bid = md.get("best_bid")
        if bid is None or ask - bid > self.max_spread:
            return self._reject(ticker, f"spread too wide or no bid (bid={bid}, ask={ask:.2f})")

        p_above = prob_above(self.spot, md.get("strike_price"), mins, self.vol_provider())
        if p_above is None:
            return self._reject(ticker, "no fair-value inputs (vol/strike)")
        fair = p_above if direction == "above" else 1.0 - p_above

        no_price = 1.0 - ask
        edge = ask - fair - fee_per_contract(no_price)
        logger.info(
            "[%s] %s contract | spot=%.1f strike=%s | ask=%.2f fair=%.3f edge=%.3f",
            ticker, direction, self.spot, md.get("strike_price"), ask, fair, edge,
        )
        if edge < self.min_edge:
            return self._reject(ticker, f"edge {edge:.3f} < {self.min_edge} (spike looks justified)")

        open_contracts = sum(o["count"] for o in self.pending.values())
        if open_contracts + self.order_size > self.max_open_contracts:
            return self._reject(ticker, f"exposure cap ({open_contracts} open)")

        price_cents = max(1, min(99, round(no_price * 100)))
        order = {
            "ticker": ticker,
            "action": "buy",
            "side": "no",
            "type": "limit",
            "count": self.order_size,
            "no_price": price_cents,
            "post_only": True,
            "client_order_id": uuid.uuid4().hex,
        }
        self.pending[order["client_order_id"]] = {**order, "expires_at": now + self.order_ttl}
        st.last_fade_at = now
        self.order_sink(order)
        return {"fade": True, "reason": f"edge {edge:.3f}", "order": order}

    # ── Order lifecycle (caller wires these to the exchange) ───────────────

    def expire_orders(self) -> list[dict[str, Any]]:
        """Orders past their TTL, removed from pending. The caller must cancel them."""
        now = self.clock()
        expired = [o for o in self.pending.values() if o["expires_at"] <= now]
        for o in expired:
            del self.pending[o["client_order_id"]]
        return expired

    def release(self, client_order_id: str) -> None:
        """Call on fill or cancel confirmation."""
        self.pending.pop(client_order_id, None)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    t = [0.0]
    bot = KalshiPanicFadeBot(vol_provider=lambda: 0.0004, clock=lambda: t[0])
    md = {"ticker":"BTC-65k-ABOVE","strike_price":65000.0,"direction":"above","best_bid":0.80,"minutes_left":10.0}
    bot.on_btc_spot_update(64820.0)
    bot.process_order_book_update({**md, "best_ask": 0.30, "best_bid": 0.25})
    t[0] = 5.1
    bot.on_btc_spot_update(64820.0)
    print(bot.process_order_book_update({**md, "best_ask": 0.85}))
