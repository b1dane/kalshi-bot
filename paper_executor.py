"""Paper trading executor — virtual balance, position tracking, settlement.

Fixes vs. the original:
- MAX_BET is now a real dollar cap on order cost (it used to cap a 0-1 price,
  which did nothing, and every trade bought exactly one contract).
- Entry is at the ask, and Kalshi fees are charged.
- Settlement reads the market's ``result`` field from the unwrapped market
  object (the original never found ``status`` and so never settled anything).
- P&L accounts for contract count; equity includes open positions.
- Loss controls: total drawdown lock and a daily realized-loss limit.
- Same-event dedupe (survives restarts) and atomic state-file writes.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from kalshi_data import get_market
from pricing import order_fee

logger = logging.getLogger("kalshi_bot")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_state.json")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperExecutor:
    """Virtual paper trading engine. State persists to ``paper_state.json``."""

    INITIAL_BALANCE: float = 100.0
    MAX_BET: float = 5.0  # max $ cost (incl. fees) per trade, at conviction 3
    STOP_THRESHOLD: float = 0.75  # +75% equity locks the bot (manual restart)
    MAX_LOSS_THRESHOLD: float = 0.30  # -30% equity locks the bot
    DAILY_LOSS_LIMIT: float = 10.0  # realized $ loss per UTC day pauses trading

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
                "Paper state loaded: balance=$%.2f trades=%d locked=%s",
                self.balance, self.total_trades, self.bot_locked,
            )
        except (json.JSONDecodeError, OSError) as exc:
            backup = f"{self.state_file}.corrupt-{int(time.time())}"
            try:
                os.replace(self.state_file, backup)
                logger.error("Could not load paper state (%s); moved it to %s", exc, backup)
            except OSError:
                logger.error("Could not load paper state (%s); starting fresh", exc)
            self._record_balance()
            self._save_state()

    def _save_state(self) -> None:
        """Atomic write: temp file then rename, so a crash can't corrupt state."""
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
        tmp = f"{self.state_file}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_file)

    def _record_balance(self) -> None:
        self.balance_history.append({"timestamp": _utcnow(), "balance": round(self.balance, 2)})

    # ── Risk ───────────────────────────────────────────────────────────────

    @staticmethod
    def _cost(trade: dict[str, Any]) -> float:
        # Old-format trades (pre-fix) have no "cost": they were 1 contract.
        return float(trade.get("cost", trade.get("entry_price", 0.0)))

    @property
    def open_cost(self) -> float:
        return sum(self._cost(t) for t in self.positions)

    @property
    def equity(self) -> float:
        """Balance plus open positions valued at cost."""
        return self.balance + self.open_cost

    @property
    def pnl_ratio(self) -> float:
        return round((self.equity - self.INITIAL_BALANCE) / self.INITIAL_BALANCE, 4)

    def realized_pnl_today(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        return sum(
            t["pnl"]
            for t in self.trade_history
            if t.get("settled") and t.get("pnl") is not None
            and str(t.get("settled_at", "")).startswith(today)
        )

    def has_traded_event(self, event_ticker: str) -> bool:
        return any(t.get("event_ticker") == event_ticker for t in self.trade_history)

    def _lock(self, reason: str) -> None:
        self.bot_locked = True
        self.locked_reason = reason
        self._save_state()

    @property
    def can_trade(self) -> tuple[bool, str]:
        if self.bot_locked:
            return False, self.locked_reason or "Bot locked — manual restart required"
        if self.balance <= 0:
            return False, "Balance is $0 or less — cannot trade"
        pnl = self.pnl_ratio
        if pnl >= self.STOP_THRESHOLD:
            self._lock(f"+{pnl * 100:.0f}% equity reached — bot stopped per profit rule")
            return False, self.locked_reason
        if pnl <= -self.MAX_LOSS_THRESHOLD:
            self._lock(f"{pnl * 100:.0f}% equity — bot stopped per max-drawdown rule")
            return False, self.locked_reason
        if self.realized_pnl_today() <= -self.DAILY_LOSS_LIMIT:
            return False, f"Daily loss limit (${self.DAILY_LOSS_LIMIT:.0f}) reached"
        return True, "ok"

    # ── Trading ────────────────────────────────────────────────────────────

    def _size(self, price: float, conviction: int) -> tuple[int, float]:
        """Largest whole-contract order whose cost+fees fit the budget."""
        budget = min(self.MAX_BET * max(1, min(conviction, 3)) / 3.0, self.balance)
        n = int(budget // price)
        while n >= 1:
            cost = n * price + order_fee(n, price)
            if cost <= budget + 1e-9:
                return n, round(cost, 2)
            n -= 1
        return 0, 0.0

    def execute_trade(
        self,
        event_ticker: str,
        market_ticker: str,
        direction: str,
        conviction: int,
        entry_price: float,
        target_price: float,
        yes_price: float | None = None,
        no_price: float | None = None,
        p_model: float | None = None,
        edge: float | None = None,
    ) -> dict[str, Any] | None:
        """Paper-buy at ``entry_price`` (the ask). Returns the trade or None."""
        if not (0.0 < entry_price < 1.0):
            logger.warning("Invalid entry price %r — not trading", entry_price)
            return None
        if self.has_traded_event(event_ticker):
            logger.info("Already traded %s — skipping", event_ticker)
            return None
        allowed, reason = self.can_trade
        if not allowed:
            logger.info("Trade blocked: %s", reason)
            return None

        contracts, cost = self._size(entry_price, conviction)
        if contracts < 1:
            logger.warning("Budget too small for 1 contract @ $%.2f", entry_price)
            return None
        fee = round(cost - contracts * entry_price, 2)

        self.balance = round(self.balance - cost, 2)
        trade = {
            "id": f"{market_ticker}-{int(datetime.now(timezone.utc).timestamp())}",
            "event_ticker": event_ticker,
            "market_ticker": market_ticker,
            "direction": direction,
            "conviction": conviction,
            "contracts": contracts,
            "entry_price": round(entry_price, 4),
            "fee": fee,
            "cost": cost,
            "yes_price_at_entry": round(yes_price, 4) if yes_price is not None else None,
            "no_price_at_entry": round(no_price, 4) if no_price is not None else None,
            "target_price": target_price,
            "p_model": round(p_model, 4) if p_model is not None else None,
            "edge": round(edge, 4) if edge is not None else None,
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
            "Paper trade: %s %d x %s @ $%.2f fee=$%.2f (balance=$%.2f)",
            direction.upper(), contracts, market_ticker, entry_price, fee, self.balance,
        )
        return trade

    # ── Settlement ─────────────────────────────────────────────────────────

    def check_settlements(self) -> list[dict[str, Any]]:
        """Settle positions whose market has a yes/no ``result``."""
        newly_settled: list[dict[str, Any]] = []
        still_open: list[dict[str, Any]] = []

        for trade in self.positions:
            market = get_market(trade["market_ticker"])
            result = str((market or {}).get("result") or "").strip().lower()
            if result not in ("yes", "no"):
                still_open.append(trade)
                continue

            was_correct = (result == "yes") == (trade["direction"] == "up")
            contracts = int(trade.get("contracts", 1))
            cost = self._cost(trade)
            payout = float(contracts) if was_correct else 0.0
            pnl = round(payout - cost, 4)

            self.balance = round(self.balance + payout, 2)
            if was_correct:
                self.wins += 1
            else:
                self.losses += 1

            trade["settled"] = True
            trade["was_correct"] = was_correct
            trade["pnl"] = pnl
            trade["settled_at"] = _utcnow()
            newly_settled.append(trade)
            logger.info(
                "Settled %s: %s (pnl=$%.2f, balance=$%.2f)",
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
        return round(self.wins / settled * 100, 1) if settled else 0.0

    @property
    def pnl(self) -> float:
        return round(self.equity - self.INITIAL_BALANCE, 2)

    @property
    def open_count(self) -> int:
        return len(self.positions)

    def recent_trades(self, n: int = 10) -> list[dict[str, Any]]:
        return list(reversed(self.trade_history[-n:]))

    def get_status_summary(self) -> dict[str, Any]:
        return {
            "balance": self.balance,
            "equity": round(self.equity, 2),
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
            "max_loss_threshold_pct": int(self.MAX_LOSS_THRESHOLD * 100),
            "daily_loss_limit": self.DAILY_LOSS_LIMIT,
        }
