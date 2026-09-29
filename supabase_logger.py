"""Supabase logger — log every decision and outcome to Supabase tables.

Uses httpx directly against the Supabase REST API. Table: kalshi_trades

Patched:
- update_settlement() PATCHes the original decision row instead of inserting
  a second row, so each trade appears once
- get_stats() uses Prefer: count=exact (the old select=count/limit=0 approach
  returned nothing and the bare excepts hid it)
- removed the hardcoded project URL from the warning message
"""
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from config import config

logger = logging.getLogger("kalshi_bot")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseLogger:
    """Logs trade decisions and outcomes to Supabase (service_role key)."""

    def __init__(self):
        self.base_url = config.SUPABASE_URL.rstrip("/")
        self.api_key = config.SUPABASE_SERVICE_KEY
        self._headers = {
            "apikey": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        self._table_url = f"{self.base_url}/rest/v1/kalshi_trades"
        self._configured = config.supabase_configured

    @property
    def ready(self) -> bool:
        return self._configured

    # ── Table management ───────────────────────────────────────────────────

    def ensure_table(self) -> bool:
        """Check that kalshi_trades exists; try to create it if not."""
        if not self._configured:
            logger.warning("Supabase not configured — can't ensure table")
            return False

        try:
            resp = httpx.get(f"{self._table_url}?limit=1", headers=self._headers, timeout=10)
            if resp.status_code < 400:
                logger.info("Supabase table kalshi_trades exists")
                return True
            if resp.status_code == 404:
                logger.info("Table kalshi_trades not found — creating...")
                return self._create_table()
            if resp.status_code in (401, 403):
                logger.warning("Supabase auth failed (status=%d) — check SERVICE_KEY", resp.status_code)
                return False
            logger.warning("Supabase table check returned %d: %.200s", resp.status_code, resp.text)
            return False
        except httpx.RequestError as exc:
            logger.warning("Supabase connection error: %s", exc)
            return False

    def _create_table(self) -> bool:
        sql = """
        CREATE TABLE IF NOT EXISTS kalshi_trades (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            event_ticker TEXT NOT NULL,
            target_price DOUBLE PRECISION,
            prediction TEXT NOT NULL,
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
        CREATE INDEX IF NOT EXISTS idx_kalshi_trades_created ON kalshi_trades(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_kalshi_trades_settled ON kalshi_trades(settled);
        """
        for url, kwargs in (
            (f"{self.base_url}/rest/v1/rpc/pg_query", {"json": {"query": sql}}),
            (
                f"{self.base_url}/rest/v1/sql",
                {"content": sql, "headers_extra": {"Content-Type": "text/plain"}},
            ),
        ):
            try:
                extra = kwargs.pop("headers_extra", {})
                resp = httpx.post(url, headers={**self._headers, **extra}, timeout=15, **kwargs)
                if resp.status_code < 400:
                    logger.info("Supabase table kalshi_trades created")
                    return True
            except httpx.RequestError:
                pass

        logger.warning(
            "Cannot create table via REST API (no DDL endpoint). "
            "Paste the SQL from migration.sql into the Supabase dashboard SQL Editor."
        )
        logger.warning("The bot will continue without Supabase persistence.")
        return False

    # ── Logging ────────────────────────────────────────────────────────────

    def log_decision(self, decision_data: dict[str, Any]) -> bool:
        """Insert one decision row. Returns True on success or if unconfigured."""
        if not self._configured:
            return True

        payload = {k: v for k, v in decision_data.items() if v is not None}
        try:
            resp = httpx.post(self._table_url, json=payload, headers=self._headers, timeout=10)
            if resp.status_code < 400:
                return True
            logger.warning("Supabase insert returned %d: %.200s", resp.status_code, resp.text)
            return False
        except httpx.RequestError as exc:
            logger.warning("Supabase insert error: %s", exc)
            return False

    def update_settlement(self, record: dict[str, Any]) -> bool:
        """Mark the original decision row for this market as settled.

        Matches the newest unsettled row with the record's market_ticker.
        If no such row exists (e.g. the original insert failed earlier),
        falls back to inserting the settled record so the outcome isn't lost.
        """
        if not self._configured:
            return True

        market_ticker = record.get("market_ticker")
        if not market_ticker:
            return self.log_decision(record)

        patch = {
            "settled": True,
            "was_correct": record.get("was_correct"),
            "pnl": record.get("pnl"),
            "settled_at": record.get("settled_at") or _utcnow(),
        }
        # was_correct is None for voided markets; JSON null is fine on PATCH
        params = {
            "market_ticker": f"eq.{market_ticker}",
            "settled": "eq.false",
            "prediction": "in.(up,down)",
        }
        try:
            resp = httpx.patch(
                self._table_url,
                params=params,
                json=patch,
                headers={**self._headers, "Prefer": "return=representation"},
                timeout=10,
            )
            if resp.status_code < 400:
                if resp.json():  # at least one row updated
                    return True
                logger.info("No open row for %s — inserting settled record", market_ticker)
                return self.log_decision(record)
            logger.warning("Supabase settlement update returned %d: %.200s", resp.status_code, resp.text)
            return False
        except (httpx.RequestError, ValueError) as exc:
            logger.warning("Supabase settlement update error: %s", exc)
            return False

    def log_decision_batch(self, decisions: list[dict[str, Any]]) -> bool:
        """Insert several decision rows in one request."""
        if not self._configured or not decisions:
            return True

        payload = [{k: v for k, v in d.items() if v is not None} for d in decisions]
        try:
            resp = httpx.post(self._table_url, json=payload, headers=self._headers, timeout=15)
            if resp.status_code < 400:
                logger.info("Batch logged %d decisions", len(decisions))
                return True
            logger.warning("Supabase batch insert returned %d: %.200s", resp.status_code, resp.text)
            return False
        except httpx.RequestError as exc:
            logger.warning("Supabase batch insert error: %s", exc)
            return False

    # ── Query ──────────────────────────────────────────────────────────────

    def _count(self, params: dict[str, str] | None = None) -> int | None:
        """Exact row count via Content-Range (works with any PostgREST setup)."""
        try:
            resp = httpx.get(
                self._table_url,
                headers={**self._headers, "Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
                params={"select": "id", **(params or {})},
                timeout=10,
            )
            if resp.status_code >= 400:
                logger.warning("Supabase count returned %d: %.200s", resp.status_code, resp.text)
                return None
            # Content-Range looks like "0-0/42" or "*/0"
            total = resp.headers.get("content-range", "").split("/")[-1]
            return int(total) if total.isdigit() else None
        except (httpx.RequestError, ValueError) as exc:
            logger.warning("Supabase count error: %s", exc)
            return None

    def get_stats(self) -> dict[str, Any]:
        """Aggregate stats from the table."""
        if not self._configured:
            return {"error": "Supabase not configured"}

        stats: dict[str, Any] = {
            "total_decisions": self._count(),
            "settled_trades": self._count({"settled": "eq.true"}),
            "wins": self._count({"was_correct": "eq.true"}),
        }
        try:
            resp = httpx.get(
                self._table_url,
                headers={**self._headers, "Prefer": "return=representation"},
                params={"order": "created_at.desc", "limit": 5},
                timeout=10,
            )
            if resp.status_code < 400:
                stats["recent_trades"] = resp.json()
        except (httpx.RequestError, ValueError) as exc:
            logger.warning("Supabase recent-trades error: %s", exc)
        return stats


# Singleton
supabase = SupabaseLogger()
