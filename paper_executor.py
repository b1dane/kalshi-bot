"""Paper trading executor — virtual balance, position tracking, settlement.

Starts with $100 virtual balance.
When Jev says trade, paper-buys contracts at the current ask price.
Tracks open positions and checks settlement via Kalshi API every 60s.

Patched:
- Settlement reads the {"market": {...}} wrapper and the `result` field
- MAX_BET now really caps the dollar cost of a trade (sizes contracts)
- Voided/unresolvable finalized markets are refunded instead of hanging forever
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from kalshi_data import get_market

logger = logging.getLogger("kalshi_bot")

STATE_FILE = os.path.join(os.path.dirname(__file__), "paper_state.json")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperExecutor:
    """Virtual paper trading engine. State persisted to paper_state.json."""

    INITIAL_BALANCE: float = 100.0
    MAX_BET: float = 5.0  # max $ cost per trade entry
    STOP_THRESHOLD: float = 0.75  # +75% PnL locks the bot
    MIN_TRADE_THRESHOLD: float = 0.30  # (unused, kept for compatibility)

    def __init__(self, state_file: str | None = None):
        self.state_file = state_file or STATE_FILE
        self.balance: float = self.INITIAL_BALANCE
        self.positions: list[dict[str, Any]] = []
        self.trade_history: list[dict[str, Any]] = []
        self.total_trades: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.balance_history: list[dict[str, Any]] = []
        self.bot_locked: bool = False
        self.locked_reason: str = ""
        self._load_state()

    # ── State persistence ──────────────────────────────────────────────────

    def _load_state(self) -> None:
        if not os.path.isfile(self.state_file):
            self._record_balance()
            self._save_state()
            return
        try:
            with open(self.state_file) as f:
                data = json.load(f)
            self.balance = data.get("balance", self.INITIAL_BALANCE)
            self.positions = data.get("positions", [])
            self.trade_history = data.get("trade_history", [])
            self.total_trades = data.get("total_trades", 0)
            self.wins = data.get("wins", 0)
            self.losses = data.get("losses", 0)
            self.balance_history = data.get("balance_history", [])
            self.bot_locked = data.get("bot_locked", False)
            self.locked_reason = data.get("locked_reason", "")
            logger.info(
                "Paper state loaded: balance=$%.2f, trades=%d, locked=%s",
                self.balance, self.total_trades, self.bot_locked,
            )
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.warning("Could not load paper state (%s), starting fresh", exc)
            self._record_balance()

    def _save_state(self) -> None:
        data = {
            "balance": round(self.balance, 2),
            "positions": self.positions,
            "trade_history": self.trade_history,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "balance_history": self.balance_history,
            "bot_locked": self.bot_locked,
            "locked_reason": self.locked_reason,
        }
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.state_file)  # atomic: no half-written state on crash

    def _record_balance(self) -> None:
        self.balance_history.append({"timestamp": _utcnow(), "balance": round(self.balance, 2)})

    # ── Risk gates ─────────────────────────────────────────────────────────

    @property
    def pnl_ratio(self) -> float:
        return round((self.balance - self.INITIAL_BALANCE) / self.INITIAL_BALANCE, 4)

    @property
    def can_trade(self) -> tuple[bool, str]:
        if self.bot_locked:
            return False, self.locked_reason or "Bot locked — manual restart required"
        if self.balance <= 0:
            return False, "Balance is $0 or less — cannot trade"
        pnl = self.pnl_ratio
        if pnl >= self.STOP_THRESHOLD:
            self.bot_locked = True
            self.locked_reason = f"+{pnl*100:.0f}% PnL reached — bot stopped per risk rule"
            self._save_state()
            return False, self.locked_reason
        return True, "ok"

    # ── Trading ────────────────────────────────────────────────────────────

    def execute_trade(
        self,
        event_ticker: str,
        market_ticker: str,
        direction: str,
        conviction: int,
        yes_price: float,
        no_price: float,
        target_price: float,
    ) -> dict[str, Any] | None:
        """Paper-buy contracts on the chosen side.

        yes_price / no_price are the ASK prices in dollars (0-1).
        Contract count is sized so total cost never exceeds MAX_BET.
        """
        entry_price = yes_price if direction == "up" else no_price

        if not (0.0 < entry_price < 1.0):
            logger.warning("Invalid entry price %.4f — not trading", entry_price)
            return None

        allowed, reason = self.can_trade
        if not allowed:
            logger.info("Trade blocked: %s", reason)
            return None

        # Size: cap by MAX_BET and by what we can actually afford
        contracts = int(min(self.MAX_BET, self.balance) // entry_price)
        if contracts < 1:
            logger.warning(
                "Insufficient balance: $%.2f available, $%.2f needed for 1 contract",
                self.balance, entry_price,
            )
            return None

        cost = round(contracts * entry_price, 4)
        self.balance = round(self.balance - cost, 2)

        trade = {
            "id": f"{market_ticker}-{int(datetime.now(timezone.utc).timestamp())}",
            "event_ticker": event_ticker,
            "market_ticker": market_ticker,
            "direction": direction,
            "conviction": conviction,
            "entry_price": round(entry_price, 4),
            "contracts": contracts,
            "cost": cost,
            "yes_price_at_entry": round(yes_price, 4),
            "no_price_at_entry": round(no_price, 4),
            "target_price": target_price,
            "entry_time": _utcnow(),
            "settled": False,
            "was_correct": None,
            "pnl": None,
            "settled_at": None,
        }
        self.positions.append(trade)
        self.trade_history.append(trade)
        self.total_trades += 1
        self._record_balance()
        self._save_state()

        logger.info(
            "Paper trade: %s %s %dx @ $%.4f cost=$%.2f (balance=$%.2f)",
            direction.upper(), market_ticker, contracts, entry_price, cost, self.balance,
        )
        return trade

    # ── Settlement ─────────────────────────────────────────────────────────

    def check_settlements(self) -> list[dict[str, Any]]:
        """Check open positions against Kalshi; return newly settled trades."""
        newly_settled: list[dict[str, Any]] = []
        still_open: list[dict[str, Any]] = []

        for trade in self.positions:
            raw = get_market(trade["market_ticker"])
            if raw is None:
                still_open.append(trade)
                continue

            # Single-market endpoint wraps the payload in {"market": {...}}
            market = raw.get("market", raw)
            status = str(market.get("status") or "").lower()
            result = str(market.get("result") or "").lower()

            contracts = trade.get("contracts", 1)  # old state files had 1 contract
            cost = trade.get("cost", trade["entry_price"] * contracts)

            if result not in ("yes", "no"):
                if status in ("finalized", "settled"):
                    # Finalized with no yes/no result (void/scalar): refund the cost
                    self.balance = round(self.balance + cost, 2)
                    trade.update(
                        settled=True, was_correct=None, pnl=0.0, settled_at=_utcnow()
                    )
                    self.total_trades = max(0, self.total_trades - 1)
                    logger.warning("Market %s finalized without yes/no — refunded", trade["market_ticker"])
                    newly_settled.append(trade)
                else:
                    still_open.append(trade)
                continue

            was_correct = (result == "yes") == (trade["direction"] == "up")

            if was_correct:
                payout = float(contracts)  # $1 per contract
                self.wins += 1
            else:
                payout = 0.0
                self.losses += 1

            pnl = round(payout - cost, 4)
            self.balance = round(self.balance + payout, 2)

            trade["settled"] = True
            trade["was_correct"] = was_correct
            trade["pnl"] = pnl
            trade["settled_at"] = _utcnow()
            newly_settled.append(trade)

            logger.info(
                "Settled %s: %s ($%.2f pnl, balance=$%.2f)",
                trade["market_ticker"], "WIN" if was_correct else "LOSS", pnl, self.balance,
            )

        self.positions = still_open
        if newly_settled:
            self._record_balance()
        self._save_state()
        return newly_settled

    # ── Stats ──────────────────────────────────────────────────────────────

    @property
    def win_rate(self) -> float:
        settled = self.wins + self.losses
        if settled == 0:
            return 0.0
        return round(self.wins / settled * 100, 1)

    @property
    def pnl(self) -> float:
        return round(self.balance - self.INITIAL_BALANCE, 2)

    @property
    def open_count(self) -> int:
        return len(self.positions)

    def recent_trades(self, n: int = 10) -> list[dict[str, Any]]:
        return list(reversed(self.trade_history[-n:]))

    def get_status_summary(self) -> dict[str, Any]:
        return {
            "balance": self.balance,
            "pnl": self.pnl,
            "pnl_ratio": self.pnl_ratio,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "open_positions": self.open_count,
            "initial_balance": self.INITIAL_BALANCE,
            "bot_locked": self.bot_locked,
            "locked_reason": self.locked_reason,
            "max_bet": self.MAX_BET,
            "stop_threshold_pct": int(self.STOP_THRESHOLD * 100),
        }
