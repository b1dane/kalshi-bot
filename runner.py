"""Main runner loop — checks for new events, calls Jev, logs, paper trades.

Flow:
1. Every 30s: poll for new KXBTC15M events
2. When a new event is detected:
   a. Fetch event data + order book + trades
   b. Call Jev for batched decision
   c. Log decision to Supabase
   d. Paper-execute if Jev says trade
3. Every 60s: check settlement of open positions
4. Run indefinitely until Ctrl+C
"""
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

from config import config
from kalshi_data import (
    get_current_event,
    get_order_book,
    get_recent_trades,
)
from jev_decisions import decide_trade
from paper_executor import PaperExecutor
from supabase_logger import supabase

# ── Logging setup ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("runner")

# ── Shutdown handling ───────────────────────────────────────────────────────

_shutdown = False


def _handle_signal(signum: int, frame: Any) -> None:
    global _shutdown
    logger.info("Received signal %d — shutting down...", signum)
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    """Build a decision dict ready for Supabase logging."""
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


# ── Main loop ───────────────────────────────────────────────────────────────


def run() -> None:
    """Run the main trading loop indefinitely."""
    logger.info("=" * 60)
    logger.info("Kalshi BTC 15-min Paper Trading Bot starting...")
    logger.info("Config: %s", config)
    logger.info("=" * 60)

    # Ensure Supabase table exists
    if supabase.ready:
        supabase.ensure_table()
    else:
        logger.warning("Supabase not configured — decisions will not be persisted")

    # State
    executor = PaperExecutor()
    last_event_id: str | None = None
    last_settlement_check: float = 0.0
    loop_count: int = 0

    logger.info(
        "Starting with balance=$%.2f, %d open positions",
        executor.balance,
        executor.open_count,
    )

    # ── Signal readiness ───────────────────────────────────────────────────
    logger.info("Runner is live. Press Ctrl+C to stop.")

    while not _shutdown:
        loop_count += 1
        now = time.time()

        try:
            # ── 1. Check for new events (every 30s) ────────────────────────
            event = get_current_event()

            if event is None:
                logger.debug("No open %s events — waiting...", config.KALSHI_SERIES)
                _sleep_until(now + 30)
                continue

            event_ticker = event.get("event_ticker", "")
            event_id = event.get("id", event_ticker)

            if event_id == last_event_id:
                logger.debug("No new event (still: %s) — waiting...", event_ticker)
            else:
                # ── New event detected! ────────────────────────────────────
                logger.info("New event detected: %s", event_ticker)
                last_event_id = event_id

                markets = event.get("markets", [])
                if not markets:
                    logger.warning("Event %s has no markets — skipping", event_ticker)
                    _sleep_until(now + 30)
                    continue

                # Pick the first market for evaluation
                primary_market = markets[0]
                market_ticker = primary_market.get("ticker", "")

                # Get order book + recent trades
                order_book = get_order_book(market_ticker)
                trades = get_recent_trades(market_ticker)

                # Compute price context
                yes_price = float(primary_market.get("yes_price", 0.50))
                no_price = float(primary_market.get("no_price", 0.50))

                order_book_yes = []
                order_book_no = []
                if order_book:
                    order_book_yes = order_book.get("yes", [])
                    order_book_no = order_book.get("no", [])

                # Compute target price
                from kalshi_data import compute_target_price
                target_price = compute_target_price(primary_market)

                price_context = {
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "order_book_yes": order_book_yes,
                    "order_book_no": order_book_no,
                    "recent_trades": trades,
                    "market_ticker": market_ticker,
                }

                # ── 2. Call Jev for batched decision ──────────────────────
                logger.info("Calling Jev for %s...", market_ticker)
                decision = decide_trade(event, price_context)

                # ── 3. Log decision to Supabase ───────────────────────────
                decision_record = _build_decision_record(
                    event=event,
                    market_ticker=market_ticker,
                    target_price=target_price,
                    direction=decision.direction,
                    conviction=decision.conviction,
                    yes_price=yes_price,
                    no_price=no_price,
                )
                supabase.log_decision(decision_record)

                # ── 4. Paper-execute if Jev said trade ────────────────────
                if decision.should_trade and decision.direction in ("up", "down"):
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
                            "Trade executed: %s %s entry=$%.4f balance=$%.2f",
                            decision.direction.upper(),
                            market_ticker,
                            trade["entry_price"],
                            executor.balance,
                        )
                    else:
                        logger.info("Trade declined (insufficient balance or error)")
                else:
                    logger.info("No trade: %s (conviction=%d)", decision.direction, decision.conviction)

            # ── 5. Check settlement of open positions (every 60s) ────────
            if executor.open_count > 0 and (now - last_settlement_check) >= 60:
                settled = executor.check_settlements()
                last_settlement_check = now

                if settled:
                    for trade in settled:
                        # Log settlement outcome to Supabase
                        settlement_record = _build_decision_record(
                            event=event or {"event_ticker": trade["event_ticker"]},
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
                        supabase.log_decision(settlement_record)

                    logger.info(
                        "%d trade(s) settled: %d wins, %d losses (win_rate=%.1f%%)",
                        len(settled),
                        sum(1 for t in settled if t["was_correct"]),
                        sum(1 for t in settled if not t["was_correct"]),
                        executor.win_rate,
                    )

            # ── Sleep until next check ────────────────────────────────────
            _sleep_until(now + 30)

        except Exception:
            logger.exception("Unexpected error in main loop — continuing")
            _sleep_until(time.time() + 30)

    # ── Graceful shutdown ───────────────────────────────────────────────────
    logger.info("Shutting down.")
    status = executor.get_status_summary()
    logger.info(
        "Final: balance=$%.2f pnl=$%.2f trades=%d win_rate=%.1f%%",
        status["balance"],
        status["pnl"],
        status["total_trades"],
        status["win_rate"],
    )
    sys.exit(0)


def _sleep_until(target_time: float) -> None:
    """Sleep until target_time, checking for shutdown every second."""
    while not _shutdown and time.time() < target_time:
        time.sleep(1)


if __name__ == "__main__":
    run()