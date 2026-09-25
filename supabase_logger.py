"""Supabase logger — log every decision and outcome to Supabase tables.

Uses httpx directly to POST to the Supabase REST API.
Table: kalshi_trades

Auto-creates the table on first use via the Supabase management API.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from config import config

logger = logging.getLogger("kalshi_bot")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseLogger:
    """Logs trade decisions and outcomes to Supabase.

    Uses the Supabase REST API with service_role key for table operations.
    """

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
        """Ensure the kalshi_trades table exists.

        Attempts to insert a test row; if the table doesn't exist the
        API returns a 404-like error. We then try to create it via
        the Supabase management SQL endpoint.

        Returns True if table exists or was created.
        """
        if not self._configured:
            logger.warning("Supabase not configured — can't ensure table")
            return False

        # Test: try to query the table (single row)
        test_url = f"{self._table_url}?limit=1"
        try:
            resp = httpx.get(test_url, headers=self._headers, timeout=10)
            if resp.status_code < 400:
                logger.info("Supabase table kalshi_trades exists")
                return True
            if resp.status_code == 404:
                logger.info("Table kalshi_trades not found — creating...")
                return self._create_table()
            # 401/403 means bad key — just log and continue
            if resp.status_code in (401, 403):
                logger.warning("Supabase auth failed (status=%d) — check SERVICE_KEY", resp.status_code)
                return False
            logger.warning("Supabase table check returned %d: %.200s", resp.status_code, resp.text)
            return False
        except httpx.RequestError as exc:
            logger.warning("Supabase connection error: %s", exc)
            return False

    def _create_table(self) -> bool:
        """Create the kalshi_trades table via Supabase SQL endpoint.

        The Supabase REST API does NOT expose DDL (CREATE TABLE) endpoints
        by default. This method attempts the known approaches and provides
        clear instructions if they fail.
        """
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

        # Attempt 1: rpc/pg_query (custom function, rarely exists by default)
        query_url = f"{self.base_url}/rest/v1/rpc/pg_query"
        try:
            resp = httpx.post(query_url, json={"query": sql}, headers=self._headers, timeout=15)
            if resp.status_code < 400:
                logger.info("Supabase table kalshi_trades created via pg_query")
                return True
        except httpx.RequestError:
            pass

        # Attempt 2: /rest/v1/sql with content-type text/plain
        try:
            resp = httpx.post(
                f"{self.base_url}/rest/v1/sql",
                content=sql,
                headers={**self._headers, "Content-Type": "text/plain"},
                timeout=15,
            )
            if resp.status_code < 400:
                logger.info("Supabase table created via /rest/v1/sql")
                return True
        except httpx.RequestError:
            pass

        # Neither approach works — the Supabase REST API does not expose DDL.
        logger.warning(
            "Cannot create table via REST API. "
            "The Supabase REST API does not expose DDL endpoints. "
            "To create the table, paste the SQL from migration.sql "
            "into the Supabase dashboard SQL Editor "
            "(https://supabase.com/dashboard/project/tkssicfcpqwoccidunkv/sql/new)"
        )
        logger.warning(
            "The bot will continue without Supabase persistence — "
            "Jev decisions and Kalshi data fetching still work."
        )
        return False

    # ── Logging ────────────────────────────────────────────────────────────

    def log_decision(self, decision_data: dict[str, Any]) -> bool:
        """Log a single decision/outcome row to Supabase.

        Args:
            decision_data: Dict with keys matching the table schema:
                - event_ticker (str)
                - target_price (float, optional)
                - prediction (str: up/down/pass)
                - conviction_score (int: 0-3)
                - yes_price_at_entry (float, optional)
                - no_price_at_entry (float, optional)
                - market_ticker (str, optional)
                - settled (bool)
                - was_correct (bool, optional)
                - pnl (float, optional)
                - settled_at (str, optional)

        Returns:
            True if logged successfully or supabase not configured.
        """
        if not self._configured:
            logger.debug("Supabase not configured — skipping log")
            return True  # Not an error, just not configured

        # Strip None values (let Supabase use defaults)
        payload = {k: v for k, v in decision_data.items() if v is not None}

        try:
            resp = httpx.post(self._table_url, json=payload, headers=self._headers, timeout=10)
            if resp.status_code < 400:
                return True
            logger.warning(
                "Supabase insert returned %d: %.200s",
                resp.status_code, resp.text,
            )
            return False
        except httpx.RequestError as exc:
            logger.warning("Supabase insert error: %s", exc)
            return False

    def log_decision_batch(self, decisions: list[dict[str, Any]]) -> bool:
        """Log multiple decisions in one batch request.

        Args:
            decisions: List of decision data dicts.

        Returns:
            True if all logged successfully.
        """
        if not self._configured:
            return True
        if not decisions:
            return True

        # Strip None values from each
        payload = [{k: v for k, v in d.items() if v is not None} for d in decisions]

        try:
            resp = httpx.post(self._table_url, json=payload, headers={
                **self._headers,
                "Prefer": "return=minimal",
            }, timeout=15)
            if resp.status_code < 400:
                logger.info("Batch logged %d decisions", len(decisions))
                return True
            logger.warning("Supabase batch insert returned %d: %.200s", resp.status_code, resp.text)
            return False
        except httpx.RequestError as exc:
            logger.warning("Supabase batch insert error: %s", exc)
            return False

    # ── Query ──────────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        """Fetch aggregate stats from the Supabase table."""
        if not self._configured:
            return {"error": "Supabase not configured"}

        stats = {}

        try:
            # Total decisions
            resp = httpx.get(
                self._table_url,
                headers={**self._headers, "Prefer": "return=representation"},
                params={"select": "count", "limit": 0},
                timeout=10,
            )
            if resp.status_code < 400:
                data = resp.json()
                stats["total_decisions"] = data[0].get("count", 0) if isinstance(data, list) else 0
        except Exception:
            pass

        try:
            # Settled trades
            resp = httpx.get(
                self._table_url,
                headers={**self._headers, "Prefer": "return=representation"},
                params={"settled": "eq.true", "select": "count", "limit": 0},
                timeout=10,
            )
            if resp.status_code < 400:
                data = resp.json()
                stats["settled_trades"] = data[0].get("count", 0) if isinstance(data, list) else 0
        except Exception:
            pass

        try:
            # Wins
            resp = httpx.get(
                self._table_url,
                headers={**self._headers, "Prefer": "return=representation"},
                params={"was_correct": "eq.true", "select": "count", "limit": 0},
                timeout=10,
            )
            if resp.status_code < 400:
                data = resp.json()
                stats["wins"] = data[0].get("count", 0) if isinstance(data, list) else 0
        except Exception:
            pass

        try:
            # Recent trades
            resp = httpx.get(
                self._table_url,
                headers={**self._headers, "Prefer": "return=representation"},
                params={"order": "created_at.desc", "limit": 5},
                timeout=10,
            )
            if resp.status_code < 400:
                stats["recent_trades"] = resp.json()
        except Exception:
            pass

        return stats


# Singleton
supabase = SupabaseLogger()