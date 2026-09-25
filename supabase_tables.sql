-- SQL migration: create kalshi_trades table for the BTC 15-min paper bot
-- Run: psql $SUPABASE_DB_URL < supabase_tables.sql
-- Or paste into Supabase SQL editor.

CREATE TABLE IF NOT EXISTS kalshi_trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_ticker TEXT NOT NULL,
    target_price DOUBLE PRECISION,
    prediction TEXT NOT NULL,            -- 'up', 'down', or 'pass'
    conviction_score INTEGER NOT NULL DEFAULT 0,  -- 0=none, 1=low, 2=medium, 3=high
    yes_price_at_entry DOUBLE PRECISION,
    no_price_at_entry DOUBLE PRECISION,
    market_ticker TEXT,
    settled BOOLEAN NOT NULL DEFAULT FALSE,
    was_correct BOOLEAN,                 -- NULL until settled
    pnl DOUBLE PRECISION,                -- NULL until settled
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ
);

-- Index for fast reverse-chronological queries
CREATE INDEX IF NOT EXISTS idx_kalshi_trades_created
    ON kalshi_trades(created_at DESC);

-- Index for settlement checks
CREATE INDEX IF NOT EXISTS idx_kalshi_trades_settled
    ON kalshi_trades(settled);

-- Index for win-rate queries
CREATE INDEX IF NOT EXISTS idx_kalshi_trades_was_correct
    ON kalshi_trades(was_correct)
    WHERE was_correct IS NOT NULL;