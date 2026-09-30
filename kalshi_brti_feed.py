"""Authenticated Kalshi CF Benchmarks BRTI feed.

Read-only market-data client. This module never creates, amends, cancels,
or fills orders and never accesses Supabase or another repository.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import config

logger = logging.getLogger("kalshi_bot.brti")

WS_PATH = "/trade-api/ws/v2"


@dataclass(frozen=True)
class BrtiReading:
    value: float
    source_ts_ms: int | None
    received_at_ms: int
    avg_60s: float | None = None
    avg_60s_window_size: int = 0
    final_minute_avg_15m: float | None = None
    final_minute_window_size: int = 0
    seq: int | None = None


@dataclass
class BrtiState:
    latest: BrtiReading | None = None
    last_message_ms: int = 0
    sequence_gaps: int = 0
    available_indices: set[str] = field(default_factory=set)

    @property
    def stale(self) -> bool:
        return not self.latest or (int(time.time() * 1000) - self.last_message_ms) > 5000


class KalshiAuthError(RuntimeError):
    pass


def _load_private_key() -> Any:
    path = Path(config.KALSHI_PRIVATE_KEY_PATH).expanduser()
    bot_dir = Path(__file__).resolve().parent
    try:
        path.relative_to(bot_dir)
    except ValueError as exc:
        raise KalshiAuthError("KALSHI_PRIVATE_KEY_PATH must remain inside the bot directory") from exc
    if not path.is_file():
        raise KalshiAuthError(f"Kalshi private key not found: {path}")
    if path.stat().st_mode & 0o077:
        raise KalshiAuthError(f"Kalshi private key permissions are too broad: {oct(path.stat().st_mode & 0o777)}")
    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    except Exception as exc:
        raise KalshiAuthError("Kalshi private key is not a readable PEM private key") from exc


def _signature(timestamp_ms: str, method: str, path: str) -> str:
    key = _load_private_key()
    message = f"{timestamp_ms}{method.upper()}{path.split('?', 1)[0]}".encode()
    signature = key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def websocket_headers() -> dict[str, str]:
    if not config.KALSHI_API_KEY_ID:
        raise KalshiAuthError("KALSHI_API_KEY_ID is not configured")
    timestamp = str(int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": _signature(timestamp, "GET", WS_PATH),
    }


def parse_brti_message(message: dict[str, Any], state: BrtiState) -> BrtiReading | None:
    if message.get("type") == "cfbenchmarks_value_indexlist":
        state.available_indices.update(message.get("msg", {}).get("index_ids", []))
        return None
    if message.get("type") != "cfbenchmarks_value":
        return None
    msg = message.get("msg") or {}
    if msg.get("index_id") != "BRTI":
        return None
    try:
        raw = json.loads(msg.get("data", "{}"))
        value = float(raw.get("value"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    avg = msg.get("avg_60s_data") or {}
    final = msg.get("last_60s_windowed_average_15min") or {}
    source_ts = raw.get("time")
    reading = BrtiReading(
        value=value,
        source_ts_ms=int(source_ts) if source_ts is not None else None,
        received_at_ms=int(msg.get("received_at", time.time() * 1000)),
        avg_60s=float(avg["value"]) if avg.get("value") is not None else None,
        avg_60s_window_size=int(avg.get("window_size", 0)),
        final_minute_avg_15m=float(final["value"]) if final.get("value") is not None else None,
        final_minute_window_size=int(final.get("window_size", 0)),
        seq=int(message["seq"]) if message.get("seq") is not None else None,
    )
    if reading.seq is not None and state.latest and state.latest.seq is not None:
        expected = state.latest.seq + 1
        if reading.seq > expected:
            state.sequence_gaps += reading.seq - expected
            logger.warning("BRTI sequence gap: expected=%d received=%d", expected, reading.seq)
    state.latest = reading
    state.last_message_ms = int(time.time() * 1000)
    return reading


async def stream_brti() -> AsyncIterator[BrtiReading]:
    """Yield authoritative BRTI readings from Kalshi's authenticated feed."""
    state = BrtiState()
    async with websockets.connect(config.KALSHI_WS_URL, additional_headers=websocket_headers(), ping_interval=20, ping_timeout=10) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "cmd": "subscribe",
            "params": {"channels": ["cfbenchmarks_value"], "index_ids": ["BRTI"]},
        }))
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode()
            message = json.loads(raw)
            if message.get("type") == "error":
                raise KalshiAuthError(str(message.get("msg", "Kalshi WebSocket error")))
            reading = parse_brti_message(message, state)
            if reading:
                yield reading


async def first_reading(timeout: float = 15.0) -> BrtiReading:
    async def get_one() -> BrtiReading:
        async for reading in stream_brti():
            return reading
        raise KalshiAuthError("BRTI stream ended before a reading arrived")
    return await asyncio.wait_for(get_one(), timeout=timeout)
