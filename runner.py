"""Main runner loop — settle, evaluate the live event, ask Jev, paper-trade.

Flow (every POLL_SECONDS):
1. Settle any open positions (always, even between events).
2. Fetch the live KXBTC15M event and its market.
3. Compute a volatility-model fair price from BTC spot, strike, time left.
4. If the model shows an edge over the ask (after fees), ask Jev.
5. Trade only if Jev agrees on direction with conviction >= MIN_CONVICTION.
   One trade per event, only until MIN_MINUTES_LEFT before close.

Why re-evaluate every poll instead of once when the event opens: the strike is
set at the open, so odds start near 50/50 with no edge. Edge shows up later in
the window, when spot has moved and the market's price hasn't caught up.
"""

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

from btc_data import get_minute_vol, get_spot
from config import config
from jev_decisions import decide_trade
from kalshi_data import (
    compute_target_price,
    get_current_event,
    get_order_book,
    get_quote,
    minutes_left,
    valid_ask,
)
from paper_executor import PaperExecutor
from pricing import fee_per_contract, prob_above
from supabase_logger import supabase

# ── Tunables ────────────────────────────────────────────────────────────────
POLL_SECONDS = 30
SETTLE_CHECK_SECONDS = 60
MIN_MINUTES_LEFT = 1.5  # don't open trades this close to settlement
MIN_EDGE = 0.04  # model P minus ask minus fee, in dollars per contract
MIN_CONVICTION = 1  # Jev conviction floor (0-3)
JEV_RECHECK_SECONDS = 120  # min gap between Jev calls for the same event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kalshi_bot")

_shutdown = False


def _handle_signal(signum: int, frame: Any) -> None:
    global _shutdown
    logger.info("Received signal %d — shutting down...", signum)
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _sleep_until(target_time: float) -> None:
    while not _shutdown and time.time() < target_time:
        time.sleep(1)


def _record(
    event_ticker: str,
    market_ticker: str,
    target_price: float | None,
    direction: str,
    conviction: int,
    yes_price: float | None,
    no_price: float | None,
    settled: bool = False,
    was_correct: bool | None = None,
    pnl: float | None = None,
    settled_at: str | None = None,
) -> dict[str, Any]:
    """Row for Supabase logging."""
    return {
        "event_ticker": event_ticker or "unknown",
        "market_ticker": market_ticker or "",
        "target_price": target_price,
        "prediction": direction,
        "conviction_score": conviction,
        "yes_price_at_entry": round(yes_price, 4) if yes_price is not None else None,
        "no_price_at_entry": round(no_price, 4) if no_price is not None else None,
        "settled": settled,
        "was_correct": was_correct,
        "pnl": pnl,
        "settled_at": settled_at,
    }


def _settle(executor: PaperExecutor) -> None:
    settled = executor.check_settlements()
    if not settled:
        return
    for trade in settled:
        # Use the settled trade's OWN event ticker (the original used the
        # current event's, so settlement rows were tagged with the wrong event).
        supabase.update_settlement(
            _record(
                event_ticker=trade["event_ticker"],
                market_ticker=trade["market_ticker"],
                target_price=trade.get("target_price"),
                direction=trade["direction"],
                conviction=trade["conviction"],
                yes_price=trade.get("yes_price_at_entry"),
                no_price=trade.get("no_price_at_entry"),
                settled=True,
                was_correct=trade["was_correct"],
                pnl=trade["pnl"],
                settled_at=trade["settled_at"],
            )
        )
    logger.info(
        "%d trade(s) settled: %d wins, %d losses | overall win_rate=%.1f%%",
        len(settled),
        sum(1 for t in settled if t["was_correct"]),
        sum(1 for t in settled if not t["was_correct"]),
        executor.win_rate,
    )


def _evaluate(
    event: dict[str, Any],
    executor: PaperExecutor,
    state: dict[str, Any],
) -> None:
    """Evaluate the live event once; maybe trade."""
    event_ticker = event.get("event_ticker", "")
    markets = event.get("markets", [])
    if not event_ticker or not markets:
        return
    if executor.has_traded_event(event_ticker):
        return

    allowed, reason = executor.can_trade
    if not allowed:
        if state.get("last_block") != reason:
            logger.info("Risk gate: %s", reason)
            state["last_block"] = reason
        return

    market = markets[0]
    market_ticker = market.get("ticker", "")
    strike = compute_target_price(market)
    mins = minutes_left(market)
    if strike is None or mins is None:
        if state.get("warned_event") != event_ticker:
            logger.warning("%s: missing strike/close_time — skipping", event_ticker)
            state["warned_event"] = event_ticker
        return
    if mins < MIN_MINUTES_LEFT:
        return

    quote = get_quote(market)
    yes_ask, no_ask = quote["yes_ask"], quote["no_ask"]
    if not (valid_ask(yes_ask) and valid_ask(no_ask)):
        logger.debug("%s: no valid asks yet", market_ticker)
        return

    spot = get_spot()
    vol = get_minute_vol()
    p_yes = prob_above(spot, strike, mins, vol)
    if p_yes is None:
        logger.debug("%s: model inputs unavailable", market_ticker)
        return

    edge_yes = p_yes - yes_ask - fee_per_contract(yes_ask)
    edge_no = (1.0 - p_yes) - no_ask - fee_per_contract(no_ask)
    if edge_yes >= edge_no:
        direction, edge, ask = "up", edge_yes, yes_ask
    else:
        direction, edge, ask = "down", edge_no, no_ask

    if edge < MIN_EDGE:
        logger.debug(
            "%s: no edge (best=%s %.3f, p_yes=%.3f, yes_ask=%.2f no_ask=%.2f, %.1f min left)",
            market_ticker, direction, edge, p_yes, yes_ask, no_ask, mins,
        )
        return

    # Candidate found — rate-limit Jev calls for this event.
    last_call = state.setdefault("jev_calls", {}).get(event_ticker, 0.0)
    if time.time() - last_call < JEV_RECHECK_SECONDS:
        return
    state["jev_calls"][event_ticker] = time.time()

    book = get_order_book(market_ticker) or {"yes": [], "no": []}
    ctx = {
        "spot": spot,
        "strike": strike,
        "minutes_left": mins,
        "minute_vol": vol,
        "p_yes_model": p_yes,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "order_book_yes": book["yes"],
        "order_book_no": book["no"],
    }
    logger.info(
        "%s: model edge %.3f on %s (p_yes=%.3f, ask=%.2f, %.1f min left) — asking Jev",
        market_ticker, edge, direction.upper(), p_yes, ask, mins,
    )
    decision = decide_trade(event, ctx)

    def log_row(prediction: str) -> None:
        supabase.log_decision(
            _record(
                event_ticker, market_ticker, strike, prediction,
                decision.conviction, yes_ask, no_ask,
            )
        )

    # Rows with prediction up/down represent ACTUAL paper trades (one per event).
    # Everything else is logged as "pass" so update_settlement never matches it.
    if not decision.should_trade or decision.direction != direction:
        log_row("pass")
        logger.info(
            "No trade: Jev said %s (trade=%s) vs model %s",
            decision.direction, decision.should_trade, direction,
        )
        return
    if decision.conviction < MIN_CONVICTION:
        log_row("pass")
        logger.info("No trade: conviction %d below floor %d", decision.conviction, MIN_CONVICTION)
        return

    trade = executor.execute_trade(
        event_ticker=event_ticker,
        market_ticker=market_ticker,
        direction=direction,
        conviction=decision.conviction,
        entry_price=ask,
        target_price=strike,
        yes_price=yes_ask,
        no_price=no_ask,
        p_model=p_yes,
        edge=edge,
    )
    if trade:
        log_row(direction)
        logger.info(
            "Trade executed: %s %d x %s @ $%.2f balance=$%.2f",
            direction.upper(), trade["contracts"], market_ticker,
            trade["entry_price"], executor.balance,
        )
    else:
        log_row("pass")


# ── Main loop ───────────────────────────────────────────────────────────────

def run() -> None:
    logger.info("=" * 60)
    logger.info("Kalshi BTC 15-min Paper Trading Bot starting...")
    logger.info("=" * 60)

    for notice in getattr(config, "notices", []):
        logger.warning(notice)

    if not config.jev_configured:
        logger.error("Jev API key is not configured (set TYPESAFE_API_KEY). Exiting.")
        sys.exit(1)

    if supabase.ready:
        supabase.ensure_table()
    else:
        logger.warning("Supabase not configured — decisions will not be persisted")

    executor = PaperExecutor()
    state: dict[str, Any] = {}
    last_settle_check = 0.0
    logger.info(
        "Starting with balance=$%.2f, %d open positions",
        executor.balance, executor.open_count,
    )
    logger.info("Runner is live. Press Ctrl+C to stop.")

    while not _shutdown:
        now = time.time()
        try:
            # 1. Settlement runs every cycle, regardless of whether an event is live.
            if executor.open_count > 0 and now - last_settle_check >= SETTLE_CHECK_SECONDS:
                _settle(executor)
                last_settle_check = now

            # 2. Evaluate the live event.
            event = get_current_event()
            if event is not None:
                if state.get("event") != event.get("event_ticker"):
                    state["event"] = event.get("event_ticker")
                    logger.info("Live event: %s", state["event"])
                _evaluate(event, executor, state)
        except Exception:
            logger.exception("Unexpected error in main loop — continuing")
        _sleep_until(now + POLL_SECONDS)

    logger.info("Shutting down.")
    s = executor.get_status_summary()
    logger.info(
        "Final: balance=$%.2f equity=$%.2f pnl=$%.2f trades=%d win_rate=%.1f%%",
        s["balance"], s["equity"], s["pnl"], s["total_trades"], s["win_rate"],
    )
    sys.exit(0)


if __name__ == "__main__":
    run()
