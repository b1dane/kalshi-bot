# Kalshi BTC Bot

Paper trading bot for Kalshi BTC 15-minute up/down contracts, powered by **Jev** — TypeSafe's hosted "System One" typed-decision AI.

The bot polls Kalshi's public API every 15 minutes for the latest BTC up/down event, asks Jev for a calibrated decision (direction + conviction + trade gate) in a single call, simulates a paper trade, and logs every decision and outcome to Supabase for later analysis.

## Architecture

```
kalshi_bot/
├── config.py              # Env-based config (Kalshi, Jev, Supabase)
├── kalshi_data.py         # Kalshi public API client (no auth)
├── jev_decisions.py       # Jev decision engine (batched 3-question call)
├── paper_executor.py      # Paper trading simulation ($100 virtual bankroll)
├── supabase_logger.py     # Logs decisions + outcomes to Supabase
├── runner.py              # Main loop: poll → Jev → trade → settle
├── status.py              # CLI dashboard
├── migration.sql          # Creates the kalshi_trades logging table
├── run_bot.sh             # One-shot launcher (exports the Jev key)
└── app/                   # Mobile-first PWA dashboard (FastAPI)
```

## How Jev makes decisions

Jev answers **three questions in one ~70-500ms call** per market:

| Question | Type | Output |
|----------|------|--------|
| Direction | `choice` | `up` / `down` / `pass` |
| Conviction | `score` | 0 (none) to 3 (high) |
| Trade gate | `noul` | Should we enter (probability) |

All three are batched into a single request to `api.typesafe.ai/v1/systemone`.

## Setup

```bash
cd ~/workspace/kalshi-bot

# 1. Configure .env (or set env vars)
cp .env.example .env   # SUPABASE_URL, SUPABASE_SERVICE_KEY, TYPESAFE_API_KEY

# 2. Run the Supabase migration (SQL Editor → run migration.sql)
#    creates the kalshi_trades logging table

# 3. Start the bot (runs until Ctrl+C)
python3.11 runner.py

# 4. Check status
python3.11 status.py
```

## Supabase logging table

```sql
kalshi_trades (
  id UUID PK,
  event_ticker TEXT,
  market_ticker TEXT,
  target_price DOUBLE PRECISION,
  prediction TEXT,            -- up / down / pass
  conviction_score INTEGER,   -- 0-3
  yes_price_at_entry, no_price_at_entry DOUBLE PRECISION,
  settled BOOLEAN,
  was_correct BOOLEAN,
  pnl DOUBLE PRECISION,
  created_at, settled_at TIMESTAMPTZ
)
```

## Mobile PWA dashboard

A mobile-first, installable PWA (in `app/`) served by FastAPI on `http://localhost:8788`:

- **Dashboard** — live balance, P&L, win rate, balance sparkline
- **Live Feed** — rolling Jev decisions in real time
- **Trades** — full trade history with green/red outcomes
- **Settings** — paper/live mode, position size, conviction floor

```bash
cd app
python3.11 server.py    # → http://localhost:8788
```

On Android, open the URL and "Add to Home Screen" for a native-like app.

## Note on live trading

The bot runs in **paper mode** by default (no real money, no orders placed). The app's Settings screen exposes a paper/live toggle, but **live order execution is not wired up yet** — by design. This is for paper-data gathering and validation before any real trading.

Kalshi is a CFTC-regulated US exchange; its public market data API requires no authentication.

## License

Apache-2.0
