"""Main runner loop — checks for new events, calls Jev, logs, paper trades.

Flow:
1. Every loop (30s): settle any open positions (every 60s)  <-- moved to top
2. Poll for new KXBTC15M events
3. On a new event: fetch market + book + trades, call Jev, log, paper-trade
4. Run until Ctrl+C

Patched:
- Settlement check no longer skipped by `continue` when no event is open
- Uses real ASK prices from the market (yes_ask / no_ask), never a fake 0.50
- Order book normalized ({"orderbook": {"yes": [[price, count]]}} -> dicts)
- Target price from `floor_strike` when present
"""

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

from config import config
from kalshi_data import (
    compute_target_price,
    get_current_event,
    get_order_book,
    get_recent_trades,
)
from jev_decisions import decide_trade
from paper_executor import PaperExecutor
from supabase_logger import supabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("runner")

_shutdown = False


def _handle_signal(signum: int, frame: Any) -> None:
    global _shutdown
    logger.info("Received signal %d — shutting down...", signum)
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _to_dollars(value: Any) -> float | None:
    """Convert a Kalshi price to dollars. Handles '0.56' strings and cents ints."""
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 1.0:  # cents (e.g. 56) -> dollars
        v /= 100.0
    return v if 0.0 < v < 1.0 else None


def _first_price(market: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        p = _to_dollars(market.get(key))
        if p is not None:
            return p
    return None


def get_ask_prices(market: dict[str, Any]) -> tuple[float, float] | None:
    """Return (yes_ask, no_ask) in dollars, or None if the market has no prices.

    Buying YES costs the YES ask; buying NO costs the NO ask. If one ask is
    missing, derive it from the opposite bid (yes_ask ~= 1 - no_bid).
    """
    yes_ask = _first_price(market, "yes_ask_dollars", "yes_ask")
    no_ask = _first_price(market, "no_ask_dollars", "no_ask")
    yes_bid = _first_price(market, "yes_bid_dollars", "yes_bid")
    no_bid = _first_price(market, "no_bid_dollars", "no_bid")

    if yes_ask is None and no_bid is not None:
        yes_ask = round(1.0 - no_bid, 4)
    if no_ask is None and yes_bid is not None:
        no_ask = round(1.0 - yes_bid, 4)

    if yes_ask is None or no_ask is None:
        return None
    return yes_ask, no_ask


def normalize_book(order_book: dict[str, Any] | None) -> tuple[list[dict], list[dict]]:
    """Turn Kalshi's orderbook payload into lists of {"price", "count"} dicts."""
    if not order_book:
        return [], []
    book = order_book.get("orderbook") or order_book.get("orderbook_fp") or order_book

    def _side(levels: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for lvl in levels or []:
            try:
                if isinstance(lvl, dict):
                    price, count = lvl.get("price"), lvl.get("count", 0)
                else:
                    price, count = lvl[0], lvl[1]
                p = _to_dollars(price)
                if p is not None:
                    out.append({"price": p, "count": float(count)})
            except (IndexError, TypeError, ValueError):
                continue
        # best (highest) bid first
        out.sort(key=lambda d: d["price"], reverse=True)
        return out

    return _side(book.get("yes") or book.get("yes_dollars")), _side(book.get("no") or book.get("no_dollars"))


def get_target_price(market: dict[str, Any]) -> float | None:
    strike = market.get("floor_strike")
    if strike is None:
        strike = market.get("cap_strike")
    if strike is not None:
        try:
            return float(strike)
        except (TypeError, ValueError):
            pass
    return compute_target_price(market)  # legacy fallback


def _build_decision_record(
    event: dict[str, Any],
    market_ticker: str | None,
    target_price: float | None,
    direction: str,
    conviction: int,
    yes_price: float,
    no_price: float,
    settled: bool = False,
    was_correct: bool | None = None,
    pnl: float | None = None,
    settled_at: str | None = None,
) -> dict[str, Any]:
    return {
        "event_ticker": event.get("event_ticker", "unknown"),
        "market_ticker": market_ticker or "",
        "target_price": target_price,
        "prediction": direction,
        "conviction_score": conviction,
        "yes_price_at_entry": round(yes_price, 4),
        "no_price_at_entry": round(no_price, 4),
        "settled": settled,
        "was_correct": was_correct,
        "pnl": pnl,
        "settled_at": settled_at,
    }


def _settle_open_positions(executor: PaperExecutor) -> None:
    """Check settlements and log outcomes. Safe to call on every loop."""
    settled = executor.check_settlements()
    if not settled:
        return
    for trade in settled:
        record = _build_decision_record(
            event={"event_ticker": trade["event_ticker"]},
            market_ticker=trade["market_ticker"],
            target_price=trade.get("target_price"),
            direction=trade["direction"],
            conviction=trade["conviction"],
            yes_price=trade["yes_price_at_entry"],
            no_price=trade["no_price_at_entry"],
            settled=True,
            was_correct=trade["was_correct"],
            pnl=trade["pnl"],
            settled_at=trade["settled_at"],
        )
        supabase.update_settlement(record)
    logger.info(
        "%d trade(s) settled: %d wins, %d losses (win_rate=%.1f%%)",
        len(settled),
        sum(1 for t in settled if t["was_correct"] is True),
        sum(1 for t in settled if t["was_correct"] is False),
        executor.win_rate,
    )


def _sleep_until(target_time: float) -> None:
    while not _shutdown and time.time() < target_time:
        time.sleep(1)


# ── Main loop ───────────────────────────────────────────────────────────────

def run() -> None:
    logger.info("=" * 60)
    logger.info("Kalshi BTC 15-min Paper Trading Bot starting...")
    logger.info("Config: %s", config)
    logger.info("=" * 60)

    if supabase.ready:
        supabase.ensure_table()
    else:
        logger.warning("Supabase not configured — decisions will not be persisted")

    executor = PaperExecutor()
    last_event_id: str | None = None
    last_settlement_check: float = 0.0

    logger.info(
        "Starting with balance=$%.2f, %d open positions",
        executor.balance, executor.open_count,
    )
    logger.info("Runner is live. Press Ctrl+C to stop.")

    while not _shutdown:
        loop_start = time.time()
        try:
            # ── 1. Settlement FIRST, so nothing below can skip it ──────────
            if executor.open_count > 0 and (loop_start - last_settlement_check) >= 60:
                _settle_open_positions(executor)
                last_settlement_check = loop_start

            # ── 2. Look for a new event ────────────────────────────────────
            event = get_current_event()
            if event is None:
                logger.debug("No open %s events — waiting...", config.KALSHI_SERIES)
                _sleep_until(loop_start + 30)
                continue

            event_ticker = event.get("event_ticker", "")
            event_id = event.get("id", event_ticker)

            if event_id == last_event_id:
                _sleep_until(loop_start + 30)
                continue

            logger.info("New event detected: %s", event_ticker)

            markets = event.get("markets", [])
            if not markets:
                logger.warning("Event %s has no markets — will retry", event_ticker)
                _sleep_until(loop_start + 30)
                continue  # last_event_id NOT set, so we retry next loop

            primary_market = markets[0]
            market_ticker = primary_market.get("ticker", "")

            prices = get_ask_prices(primary_market)
            if prices is None:
                logger.warning(
                    "No usable prices on %s (keys: %s) — skipping event",
                    market_ticker, sorted(primary_market.keys()),
                )
                last_event_id = event_id  # don't spam-retry a dead event
                _sleep_until(loop_start + 30)
                continue
            yes_price, no_price = prices
            last_event_id = event_id

            order_book_yes, order_book_no = normalize_book(get_order_book(market_ticker))
            trades = get_recent_trades(market_ticker)
            target_price = get_target_price(primary_market)

            price_context = {
                "yes_price": yes_price,
                "no_price": no_price,
                "order_book_yes": order_book_yes,
                "order_book_no": order_book_no,
                "recent_trades": trades,
                "market_ticker": market_ticker,
                "target_price": target_price,
            }

            # ── 3. Jev decision ────────────────────────────────────────────
            logger.info("Calling Jev for %s...", market_ticker)
            decision = decide_trade(event, price_context)

            # ── 4. Log decision ────────────────────────────────────────────
            supabase.log_decision(
                _build_decision_record(
                    event=event,
                    market_ticker=market_ticker,
                    target_price=target_price,
                    direction=decision.direction,
                    conviction=decision.conviction,
                    yes_price=yes_price,
                    no_price=no_price,
                )
            )

            # ── 5. Paper-execute ───────────────────────────────────────────
            if decision.should_trade and decision.direction in ("up", "down"):
                allowed, reason = executor.can_trade
                if not allowed:
                    logger.info("Risk gate blocked trade: %s", reason)
                else:
                    trade = executor.execute_trade(
                        event_ticker=event_ticker,
                        market_ticker=market_ticker,
                        direction=decision.direction,
                        conviction=decision.conviction,
                        yes_price=yes_price,
                        no_price=no_price,
                        target_price=target_price or 0.0,
                    )
                    if trade:
                        logger.info(
                            "Trade executed: %s %s %dx @ $%.4f balance=$%.2f",
                            decision.direction.upper(), market_ticker,
                            trade["contracts"], trade["entry_price"], executor.balance,
                        )
                    else:
                        logger.info("Trade declined (insufficient balance or bad price)")
            else:
                logger.info("No trade: %s (conviction=%d)", decision.direction, decision.conviction)

            _sleep_until(loop_start + 30)

        except Exception:
            logger.exception("Unexpected error in main loop — continuing")
            _sleep_until(time.time() + 30)

    logger.info("Shutting down.")
    status = executor.get_status_summary()
    logger.info(
        "Final: balance=$%.2f pnl=$%.2f trades=%d win_rate=%.1f%%",
        status["balance"], status["pnl"], status["total_trades"], status["win_rate"],
    )
    sys.exit(0)


if __name__ == "__main__":
    run()
