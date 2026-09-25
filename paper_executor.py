"""Paper trading executor — virtual balance, position tracking, settlement.

Starts with $100 virtual balance.
When Jev says trade, paper-buys at current market yes/no price.
Tracks open positions and checks settlement via Kalshi API every 60s.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from kalshi_data import get_market

logger = logging.getLogger("kalshi_bot")

# ── State file ──────────────────────────────────────────────────────────────

STATE_FILE = os.path.join(os.path.dirname(__file__), "paper_state.json")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Data classes ────────────────────────────────────────────────────────────


class PaperExecutor:
    """Virtual paper trading engine.

    State persisted to ``paper_state.json`` for survival across restarts.
    """

    INITIAL_BALANCE: float = 100.0

    def __init__(self, state_file: str | None = None):
        self.state_file = state_file or STATE_FILE
        self.balance: float = self.INITIAL_BALANCE
        self.positions: list[dict[str, Any]] = []  # open positions
        self.trade_history: list[dict[str, Any]] = []  # all trades (open + settled)
        self.total_trades: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.balance_history: list[dict[str, Any]] = []  # [{timestamp, balance}, ...]
        self._load_state()

    # ── State persistence ──────────────────────────────────────────────────

    def _load_state(self) -> None:
        """Load state from JSON file if it exists."""
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
            logger.info("Paper state loaded: balance=$%.2f, trades=%d", self.balance, self.total_trades)
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.warning("Could not load paper state (%s), starting fresh", exc)
            self._record_balance()

    def _save_state(self) -> None:
        """Persist current state to JSON file."""
        data = {
            "balance": round(self.balance, 2),
            "positions": self.positions,
            "trade_history": self.trade_history,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "balance_history": self.balance_history,
        }
        with open(self.state_file, "w") as f:
            json.dump(data, f, indent=2)

    def _record_balance(self) -> None:
        """Record current balance in history."""
        self.balance_history.append({"timestamp": _utcnow(), "balance": round(self.balance, 2)})

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
        """Execute a paper trade and record it.

        Buys one contract at the current YES price.

        Returns:
            Trade record dict, or None if balance insufficient.
        """
        entry_price = yes_price if direction == "up" else no_price

        if self.balance < entry_price:
            logger.warning(
                "Insufficient balance: $%.2f needed, $%.2f available",
                entry_price, self.balance,
            )
            return None

        # Deduct from balance
        self.balance -= entry_price
        self.balance = round(self.balance, 2)

        trade = {
            "id": f"{market_ticker}-{int(datetime.now(timezone.utc).timestamp())}",
            "event_ticker": event_ticker,
            "market_ticker": market_ticker,
            "direction": direction,
            "conviction": conviction,
            "entry_price": round(entry_price, 4),
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
            "Paper trade: %s %s @ $%.4f (balance=$%.2f)",
            direction.upper(), market_ticker, entry_price, self.balance,
        )

        return trade

    # ── Settlement ─────────────────────────────────────────────────────────

    def check_settlements(self) -> list[dict[str, Any]]:
        """Check all open positions for settlement via Kalshi API.

        Returns list of newly-settled trade dicts.
        """
        newly_settled = []
        still_open = []

        for trade in self.positions:
            market_data = get_market(trade["market_ticker"])
            if market_data is None:
                # Can't reach Kalshi — keep as open
                still_open.append(trade)
                continue

            status = market_data.get("status", "")
            if status != "settled":
                still_open.append(trade)
                continue

            # Market is settled — determine outcome
            close_price = market_data.get("close_price", None)
            if close_price is not None:
                if trade["direction"] == "up":
                    was_correct = bool(close_price == 1.0)
                else:
                    was_correct = bool(close_price == 0.0)
            else:
                # Try result field
                result = market_data.get("result", "")
                was_correct = result.lower() == "yes" if trade["direction"] == "up" else result.lower() == "no"

            # Calculate PnL
            if was_correct:
                pnl = 1.0 - trade["entry_price"]
                self.wins += 1
            else:
                pnl = -trade["entry_price"]
                self.losses += 1

            pnl = round(pnl, 4)
            self.balance += 1.0 if was_correct else 0.0
            self.balance = round(self.balance, 2)

            trade["settled"] = True
            trade["was_correct"] = was_correct
            trade["pnl"] = pnl
            trade["settled_at"] = _utcnow()

            newly_settled.append(trade)

            logger.info(
                "Settled %s: %s ($%.2f pnl, balance=$%.2f)",
                trade["market_ticker"],
                "WIN" if was_correct else "LOSS",
                pnl,
                self.balance,
            )

        self.positions = still_open
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
        """Return the N most recent trades (settled or not)."""
        return list(reversed(self.trade_history[-n:]))

    def get_status_summary(self) -> dict[str, Any]:
        """Return a dict summary for display."""
        return {
            "balance": self.balance,
            "pnl": self.pnl,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "open_positions": self.open_count,
            "initial_balance": self.INITIAL_BALANCE,
        }