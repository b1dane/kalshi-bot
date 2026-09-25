-- migration.sql — Create kalshi_trades table for Kalshi BTC 15-min paper bot
-- Run via: psql $SUPABASE_DB_URL < migration.sql
-- Or paste directly into Supabase SQL editor.
--
-- Table used by: supabase_logger.py

CREATE TABLE IF NOT EXISTS kalshi_trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_ticker TEXT NOT NULL,
    target_price DOUBLE PRECISION,
    prediction TEXT NOT NULL,            -- 'up', 'down', or 'pass'
    conviction_score INTEGER NOT NULL DEFAULT 0,
    yes_price_at_entry DOUBLE PRECISION,
    no_price_at_entry DOUBLE PRECISION,
    market_ticker TEXT,
    settled BOOLEAN NOT NULL DEFAULT FALSE,
    was_correct BOOLEAN,
    pnl DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_kalshi_trades_created
    ON kalshi_trades(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_kalshi_trades_settled
    ON kalshi_trades(settled);

CREATE INDEX IF NOT EXISTS idx_kalshi_trades_was_correct
    ON kalshi_trades(was_correct)
    WHERE was_correct IS NOT NULL;