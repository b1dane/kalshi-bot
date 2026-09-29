"""CLI status reporter — paper equity, win rate, recent trades, Supabase stats.

Usage:
    python3.11 status.py           # Full status
    python3.11 status.py --json    # JSON output (for scripting)

Layout is kept under ~44 columns so it doesn't wrap on a phone terminal.
"""
import argparse
import json

from config import config
from paper_executor import PaperExecutor
from supabase_logger import supabase

RULE = "─" * 42


def fmt_dollar(x: float) -> str:
    return f"${x:,.2f}"


def fmt_signed(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _bot_state(executor: PaperExecutor) -> str:
    """Why the bot is (or isn't) trading. Read-only: never triggers a lock."""
    if executor.bot_locked:
        return f"LOCKED — {executor.locked_reason or 'manual restart required'}"
    if executor.realized_pnl_today() <= -executor.DAILY_LOSS_LIMIT:
        return f"PAUSED — daily loss limit (${executor.DAILY_LOSS_LIMIT:.0f}) hit"
    return "ACTIVE"


def _realized(executor: PaperExecutor) -> tuple[float, int]:
    """Total realized P&L and count over settled trades."""
    pnls = [
        t["pnl"] for t in executor.trade_history
        if t.get("settled") and t.get("pnl") is not None
    ]
    return sum(pnls), len(pnls)


def show_status(json_output: bool = False) -> None:
    executor = PaperExecutor()
    stats = executor.get_status_summary()

    if json_output:
        realized, n_settled = _realized(executor)
        print(json.dumps({
            **stats,
            "state": _bot_state(executor),
            "realized_pnl": round(realized, 2),
            "realized_pnl_today": round(executor.realized_pnl_today(), 2),
            "settled_trades": n_settled,
            "config": repr(config),
            "jev_configured": config.jev_configured,
            "supabase_configured": config.supabase_configured,
        }, indent=2))
        return

    realized, n_settled = _realized(executor)
    today = executor.realized_pnl_today()

    print()
    print("  Kalshi BTC 15-min Paper Bot")
    print(f"  {RULE}")
    print(f"  Series    {config.KALSHI_SERIES}")
    print(f"  Jev       {'✓' if config.jev_configured else '✗ not configured'}")
    print(f"  Supabase  {'✓' if config.supabase_configured else '✗ not configured'}")
    print(f"  State     {_bot_state(executor)}")
    print(f"  {RULE}")

    print(f"  Equity     {fmt_dollar(stats['equity']):>11}  ({fmt_signed(stats['pnl'])})")
    print(f"  Cash       {fmt_dollar(stats['balance']):>11}")
    print(f"  Open       {stats['open_positions']:>3} pos / {fmt_dollar(executor.open_cost)}")
    print(f"  Trades     {stats['total_trades']:>3}  (W {stats['wins']} / L {stats['losses']}, "
          f"{stats['win_rate']:.1f}%)")
    if n_settled:
        print(f"  Avg P&L    {fmt_signed(realized / n_settled):>11}  per settled trade")
    print(f"  Today      {fmt_signed(today):>11}  (limit -{fmt_dollar(executor.DAILY_LOSS_LIMIT)})")
    print(f"  {RULE}")

    recent = executor.recent_trades(10)
    if recent:
        print("  Recent trades")
        for t in recent:
            when = str(t.get("entry_time", ""))[11:16]
            direction = str(t.get("direction", "?")).upper()[:4]
            n = int(t.get("contracts", 1))
            price = t.get("entry_price", 0.0)
            if t.get("settled"):
                outcome = "WIN " if t.get("was_correct") else "LOSS"
                tail = f"{outcome} {fmt_signed(t.get('pnl') or 0.0)}"
            else:
                tail = "open"
            print(f"  {when} {direction:<4} {n:>3}x @{price:.2f}  {tail}")
        print(f"  {RULE}")

    if config.supabase_configured:
        db = supabase.get_stats()
        print("  Supabase log")
        print(f"  Decisions  {str(db.get('total_decisions', '?')):>6}")
        print(f"  Settled    {str(db.get('settled_trades', '?')):>6}")
        print(f"  Wins       {str(db.get('wins', '?')):>6}")
        print(f"  {RULE}")

    print("  Start: python3.11 runner.py  (Ctrl+C to stop)")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi BTC 15-min Bot Status")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()
    show_status(json_output=args.json)


if __name__ == "__main__":
    main()
