"""Kalshi Bot mobile PWA server.

Serves a mobile-first SPA + JSON REST API for monitoring and configuring the
Kalshi BTC paper trading bot.

Runs with FastAPI + uvicorn (both installed in the python3.11 env).

Start:
    cd ~/workspace/kalshi-bot/app
    python3.11 server.py

The server needs the bot's working directory on sys.path (so it can import
paper_executor, supabase_logger, config). We add the sibling bot directory for
that reason, then bind paper_state.json paths relative to it.
"""

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

# ── ensure we can import bot modules ──────────────────────────────────────────
BOT_DIR = Path(__file__).resolve().parent.parent  # ~/workspace/kalshi-bot
APP_DIR = Path(__file__).resolve().parent          # ~/workspace/kalshi-bot/app
sys.path.insert(0, str(BOT_DIR))

try:
    from paper_executor import PaperExecutor
    from supabase_logger import supabase
    from config import config
    _BOT_IMPORTS_OK = True
except Exception as exc:  # pragma: no cover
    PaperExecutor = None
    supabase = None
    config = None
    _BOT_IMPORTS_OK = False
    _IMPORT_ERROR = exc

# ── settings persistence ─────────────────────────────────────────────────────
SETTINGS_FILE = APP_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "mode": "paper",          # "paper" | "live"  (live not wired to execution yet)
    "position_size": 1,       # contracts per trade
    "conviction_floor": 1,    # min conviction (0-3) required to place a trade
}


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    # sanity clamp
    merged["conviction_floor"] = max(0, min(3, int(merged.get("conviction_floor", 1))))
    try:
        merged["position_size"] = max(1, int(merged.get("position_size", 1)))
    except (TypeError, ValueError):
        merged["position_size"] = 1
    return merged


def save_settings(new: dict) -> dict:
    merged = load_settings()
    for k in ("mode", "position_size", "conviction_floor"):
        if k in new:
            merged[k] = new[k]
    merged["conviction_floor"] = max(0, min(3, int(merged["conviction_floor"])))
    merged["position_size"] = max(1, int(merged["position_size"]))
    with open(SETTINGS_FILE, "w") as f:
        json.dump(merged, f, indent=2)
    return merged


# ── bot state helpers ──────────────────────────────────────────────────────────

def get_executor() -> "PaperExecutor | None":
    if PaperExecutor is None:
        return None
    try:
        return PaperExecutor()  # reads current paper_state.json from disk
    except Exception:
        return None


def read_raw_state() -> dict:
    """Read paper_state.json directly as a dict (single source of truth on disk)."""
    state_file = BOT_DIR / "paper_state.json"
    try:
        with open(state_file) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def balance_history() -> list:
    raw = read_raw_state()
    bh = raw.get("balance_history", []) or []
    # return just [timestamp, balance] pairs for the sparkline
    return [{"t": e.get("timestamp"), "b": e.get("balance")} for e in bh]


def get_status() -> dict:
    raw = read_raw_state()
    initial = raw.get("initial_balance", 100.0) if "initial_balance" in raw else 100.0
    balance = raw.get("balance", 100.0)
    wins = raw.get("wins", 0)
    losses = raw.get("losses", 0)
    settled = wins + losses
    win_rate = round(wins / settled * 100, 1) if settled else 0.0
    total = raw.get("total_trades", 0)
    return {
        "balance": balance,
        "pnl": round(balance - initial, 2),
        "initial_balance": initial,
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "open_positions": len(raw.get("positions", [])),
        "balance_history": balance_history(),
        "bot_running": _bot_running(),
    }


def _bot_running() -> bool:
    """Cheap heuristic: does the runner process appear to be alive?"""
    try:
        out = os.popen("pgrep -f 'runner.py' | wc -l").read().strip()
        return int(out) > 0
    except Exception:
        return False


def get_decisions(limit: int = 50) -> dict:
    """Merge Supabase kalshi_trades rows with local paper trade_history.

    Returns a normalized list, newest first. Falls back to paper_state.json
    trade_history when Supabase is unreachable.
    """
    rows = _fetch_supabase_trades(limit)
    source = "supabase"
    if not rows:
        rows = _local_trades()
        source = "local"

    return {"source": source, "decisions": rows[:limit]}


def _normalize(row: dict) -> dict:
    """Normalize a Supabase row or paper trade into a common shape."""
    pred = row.get("prediction") or row.get("direction") or "pass"
    conv = row.get("conviction_score")
    if conv is None:
        conv = row.get("conviction", 0)
    yes_price = row.get("yes_price_at_entry")
    no_price = row.get("no_price_at_entry")
    entry_price = row.get("entry_price")
    if entry_price is None:
        entry_price = yes_price if pred == "up" else (no_price if pred == "down" else None)
    settled = bool(row.get("settled", False))
    was_correct = row.get("was_correct")
    if was_correct is None:
        was_correct = row.get("was_correct")
    return {
        "id": row.get("id") or row.get("market_ticker"),
        "prediction": pred.upper(),
        "conviction": int(conv or 0),
        "entry_price": entry_price,
        "yes_price_at_entry": yes_price,
        "no_price_at_entry": no_price,
        "status": "WIN" if (settled and was_correct) else ("LOSS" if (settled and not was_correct) else "OPEN"),
        "pnl": row.get("pnl"),
        "was_correct": was_correct,
        "settled": settled,
        "event_ticker": row.get("event_ticker"),
        "market_ticker": row.get("market_ticker"),
        "entry_time": row.get("entry_time") or row.get("created_at"),
        "settled_at": row.get("settled_at"),
    }


def _local_trades() -> list:
    raw = read_raw_state()
    trades = raw.get("trade_history", []) or []
    normalized = [_normalize(t) for t in trades]
    normalized.sort(key=lambda t: t.get("entry_time") or "", reverse=True)
    return normalized


def _fetch_supabase_trades(limit: int) -> list:
    if supabase is None or not supabase.ready:
        return []
    import httpx
    base = config.SUPABASE_URL.rstrip("/")
    key = config.SUPABASE_SERVICE_KEY
    if not base or not key:
        return []
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    url = f"{base}/rest/v1/kalshi_trades"
    params = {"order": "created_at.desc", "limit": limit}
    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=8)
        if resp.status_code < 400:
            rows = resp.json()
        else:
            return []
    except Exception:
        return []
    out = []
    for r in rows:
        n = _normalize(r)
        out.append(n)
    out.sort(key=lambda t: t.get("entry_time") or "", reverse=True)
    return out


# ── FastAPI app ──────────────────────────────────────────────────────────────

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Kalshi Bot Monitor")

# Serve static frontend
STATIC_DIR = APP_DIR / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class SettingsPayload(BaseModel):
    mode: Optional[str] = None
    position_size: Optional[int] = None
    conviction_floor: Optional[int] = None


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/status")
def api_status():
    if not _BOT_IMPORTS_OK:
        return JSONResponse({"error": "bot modules import failed", "detail": str(_IMPORT_ERROR)}, status_code=500)
    return get_status()


@app.get("/api/decisions")
def api_decisions(limit: int = 50):
    return get_decisions(min(max(limit, 1), 200))


@app.get("/api/settings")
def api_settings_get():
    return load_settings()


@app.post("/api/settings")
def api_settings_post(payload: SettingsPayload):
    changes = {}
    if payload.mode is not None:
        if payload.mode not in ("paper", "live"):
            return JSONResponse({"error": "mode must be 'paper' or 'live'"}, status_code=400)
        changes["mode"] = payload.mode
    if payload.position_size is not None:
        changes["position_size"] = payload.position_size
    if payload.conviction_floor is not None:
        changes["conviction_floor"] = payload.conviction_floor
    saved = save_settings(changes)
    return saved


@app.get("/api/health")
def api_health():
    return {
        "ok": True,
        "bot_imports": _BOT_IMPORTS_OK,
        "supabase_ready": bool(supabase and supabase.ready) if supabase else False,
        "settings_file": str(SETTINGS_FILE),
    }


# ── manual server boot (also callable via uvicorn) ───────────────────────────

def main():
    import uvicorn
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 8788))
    print(f"Kalshi Bot Monitor server -> http://{host}:{port}")
    print(f"  bot imports OK={_BOT_IMPORTS_OK}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
