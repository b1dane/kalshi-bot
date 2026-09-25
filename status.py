"""CLI status reporter — shows paper balance, win rate, recent trades, Jev stats.

Usage:
    python3.11 status.py           # Full status
    python3.11 status.py --json    # JSON output (for scripting)
"""
import argparse
import json
import sys

from config import config
from paper_executor import PaperExecutor
from supabase_logger import supabase


def fmt_price(p: float) -> str:
    return f"${p:.4f}"


def fmt_dollar(p: float) -> str:
    return f"${p:,.2f}"


def show_status(json_output: bool = False) -> None:
    """Display bot status."""
    executor = PaperExecutor()
    stats = executor.get_status_summary()

    if json_output:
        data = {
            **stats,
            "config": repr(config),
            "jev_configured": config.jev_configured,
            "supabase_configured": config.supabase_configured,
        }
        print(json.dumps(data, indent=2))
        return

    # ── Header ─────────────────────────────────────────────────────────────
    border = "─" * 58
    print()
    print(f"  ╔{border}╗")
    print(f"  ║  Kalshi BTC 15-min Paper Trading Bot    ║")
    print(f"  ╚{border}╝")
    print()

    # ── Configuration ──────────────────────────────────────────────────────
    print(f"  📡 Kalshi:   {config.KALSHI_BASE_URL}")
    print(f"  🧠 Jev API:  {'✓ Configured' if config.jev_configured else '✗ Not configured'}")
    print(f"  🗄️ Supabase: {'✓ Configured' if config.supabase_configured else '✗ Not configured'}")
    print(f"  📊 Series:   {config.KALSHI_SERIES}")
    print()

    # ── Account ────────────────────────────────────────────────────────────
    print(f"  ┌{' Account Status ':-^56}┐")
    print(f"  │ {'Initial Balance':<30} {fmt_dollar(stats['initial_balance']):>20} │")
    print(f"  │ {'Current Balance':<30} {fmt_dollar(stats['balance']):>20} │")
    print(f"  │ {'PnL':<30} {fmt_dollar(stats['pnl']):>20} │")
    print(f"  │ {'Total Trades':<30} {stats['total_trades']:>20} │")
    print(f"  │ {'Wins':<30} {stats['wins']:>20} │")
    print(f"  │ {'Losses':<30} {stats['losses']:>20} │")
    print(f"  │ {'Win Rate':<30} {stats['win_rate']:>19.1f}% │")

    if stats["total_trades"] > 0:
        avg_pnl = stats["pnl"] / stats["total_trades"]
        print(f"  │ {'Avg PnL / Trade':<30} {fmt_dollar(avg_pnl):>20} │")

    print(f"  │ {'Open Positions':<30} {stats['open_positions']:>20} │")
    print(f"  └{'':─^56}┘")
    print()

    # ── Recent trades ──────────────────────────────────────────────────────
    recent = executor.recent_trades(10)
    if recent:
        print(f"  ┌{' Recent Trades ':-^56}┐")
        print(f"  │ {'Time':<20} {'Market':<18} {'Dir':<5} {'Price':<8} {'Outcome':<8} │")
        print(f"  ├{'':─^20}┬{'':─^18}┬{'':─^5}┬{'':─^8}┬{'':─^8}┤")

        for trade in recent:
            t = trade["entry_time"][11:19] if len(trade["entry_time"]) > 19 else trade["entry_time"]
            market = trade["market_ticker"][-12:] if len(trade["market_ticker"]) > 12 else trade["market_ticker"]
            d = trade["direction"].upper()[:4]
            p = fmt_price(trade["entry_price"])

            if trade["settled"]:
                outcome = "✅ WIN" if trade["was_correct"] else "❌ LOSS"
            else:
                outcome = "⏳ open"

            print(f"  │ {t:<20} {market:<18} {d:<5} {p:<8} {outcome:<8} │")

        print(f"  └{'':─^20}┴{'':─^18}┴{'':─^5}┴{'':─^8}┴{'':─^8}┘")
        print()

    # ── Supabase stats ────────────────────────────────────────────────────
    if config.supabase_configured:
        db_stats = supabase.get_stats()
        db_total = db_stats.get("total_decisions", "?")
        db_settled = db_stats.get("settled_trades", "?")
        db_wins = db_stats.get("wins", "?")

        print(f"  ┌{' Supabase Stats ':-^56}┐")
        print(f"  │ {'Total Decisions Logged':<30} {str(db_total):>20} │")
        print(f"  │ {'Settled Trades':<30} {str(db_settled):>20} │")
        print(f"  │ {'Wins (from DB)':<30} {str(db_wins):>20} │")
        print(f"  └{'':─^56}┘")
        print()

    # ── Footer ─────────────────────────────────────────────────────────────
    print(f"  To start the bot: python3.11 runner.py")
    print(f"  Press Ctrl+C to stop.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi BTC 15-min Bot Status")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()
    show_status(json_output=args.json)


if __name__ == "__main__":
    main()