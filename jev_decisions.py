"""Jev decision engine — batched three-question call to typesafe.ai.

Asks ALL THREE questions in ONE API call:
- choice: which_direction (up / down / pass)
- score: conviction (0=none, 1=low, 2=medium, 3=high)
- noul: should_trade (probability -> thresholded)

Fixes vs. the original: the prompt now includes BTC spot, the strike, distance,
time left, realized volatility and the real ask prices. Before, Jev only saw
Kalshi's own prices (which were also wrong), so it had no information to
predict anything with.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import config

logger = logging.getLogger("kalshi_bot")


@dataclass
class DecisionResult:
    """Structured decision output from a single batched Jev call."""

    direction: str  # "up" | "down" | "pass"
    conviction: int  # 0-3
    should_trade: bool
    raw_response: str = ""
    parse_errors: list[str] = field(default_factory=list)
    reasoning: str = ""
    p_up: float | None = None  # Jev's P(up) among up/down, if provided

    @property
    def valid(self) -> bool:
        return self.direction in ("up", "down", "pass") and 0 <= self.conviction <= 3

    def summary(self) -> str:
        if not self.should_trade:
            return f"PASS (conviction={self.conviction})"
        return f"{self.direction.upper()} (conviction={self.conviction})"


# ── Prompt ──────────────────────────────────────────────────────────────────

def _n(value: float | None, fmt: str) -> str:
    return format(value, fmt) if value is not None else "n/a"


def _best_bid(levels: list[tuple[float, float]]) -> str:
    if not levels:
        return "none"
    price, qty = levels[0]
    return f"{qty:g} @ ${price:.2f}"


def _build_prompt(event_data: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Build a focused prompt from real market + BTC context."""
    spot = ctx.get("spot")
    strike = ctx.get("strike")
    dist_usd = (spot - strike) if spot is not None and strike is not None else None
    dist_pct = (dist_usd / strike * 100.0) if dist_usd is not None and strike else None
    vol = ctx.get("minute_vol")

    lines = [
        f"Event: {event_data.get('event_ticker', 'unknown')} (BTC 15-minute up/down)",
        f"BTC spot (Coinbase): ${_n(spot, ',.2f')}",
        f"Strike: ${_n(strike, ',.2f')} (YES wins if BTC settles ABOVE this)",
        f"Spot vs strike: {_n(dist_usd, '+,.2f')} USD ({_n(dist_pct, '+.3f')}%)",
        f"Minutes to settlement: {_n(ctx.get('minutes_left'), '.1f')}",
        f"Recent 1-min realized volatility: {_n(vol * 100 if vol else None, '.4f')}%",
        f"Volatility-model P(BTC above strike): {_n(ctx.get('p_yes_model'), '.3f')}",
        f"YES ask: ${_n(ctx.get('yes_ask'), '.2f')}   NO ask: ${_n(ctx.get('no_ask'), '.2f')}",
        f"Best YES bid: {_best_bid(ctx.get('order_book_yes', []))}",
        f"Best NO bid: {_best_bid(ctx.get('order_book_no', []))}",
    ]

    return (
        "You are evaluating one Kalshi BTC 15-minute binary contract.\n\n"
        + "\n".join(lines)
        + "\n\nRules:\n"
        "- Buying YES costs the YES ask and pays $1 if BTC settles above the strike.\n"
        "- Buying NO costs the NO ask and pays $1 if BTC settles below the strike.\n"
        "- Kalshi charges a taker fee of roughly 7% x price x (1 - price) per contract.\n"
        "- Only pick a direction if you believe its true probability exceeds its ask "
        "by more than fees. If unsure, answer pass.\n"
    )


# ── Jev API call (typed-decision format) ────────────────────────────────────

_QUESTIONS: dict[str, Any] = {
    "direction": {
        "type": "choice",
        "options": ["up", "down", "pass"],
        "criteria": {
            "up": "BTC will settle ABOVE the strike and YES is underpriced after fees",
            "down": "BTC will settle BELOW the strike and NO is underpriced after fees",
            "pass": "No clear edge after fees, skip this market",
        },
        "instructions": "Which side, if any, has positive expected value?",
    },
    "conviction": {
        "type": "score",
        "levels": ["none", "low", "medium", "high"],
        "criteria": [
            "No signal or conflicting indicators",
            "Weak directional bias, low confidence",
            "Moderate confidence in predicted direction",
            "Strong conviction in predicted direction",
        ],
        "instructions": "How confident are you in this prediction?",
    },
    "should_trade": {
        "type": "noul",
        "instructions": "Should we execute a paper trade based on this prediction?",
    },
}


def _call_jev(prompt: str, attempts: int = 2) -> dict[str, Any] | None:
    """Single batched call to the Jev /systemone typed-decision API.

    Retries once on timeouts/network errors. Returns the 'answers' dict or None.
    """
    if not config.jev_configured:
        logger.error("Jev API key not configured — cannot call Jev")
        return None

    headers = {
        "Authorization": f"Bearer {config.JEV_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": "jev-latest", "state": prompt, "questions": _QUESTIONS}

    for attempt in range(1, attempts + 1):
        try:
            resp = httpx.post(config.JEV_API_URL, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            answers = resp.json().get("answers")
            if not answers:
                logger.warning("Jev response has no 'answers' key")
                return None
            return answers
        except httpx.HTTPStatusError as exc:
            logger.warning("Jev API HTTP %s: %.200s", exc.response.status_code, exc.response.text)
            return None  # 4xx/5xx: don't hammer
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning("Jev API network error (attempt %d/%d): %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(2)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Jev API returned non-JSON: %s", exc)
            return None
        except Exception:
            logger.exception("Unexpected Jev API error")
            return None
    return None


# ── Parsing ─────────────────────────────────────────────────────────────────

def _parse_jev_response(raw: dict[str, Any] | None) -> DecisionResult:
    """Parse the typed-decision response into a DecisionResult."""
    result = DecisionResult(direction="pass", conviction=0, should_trade=False)
    if raw is None:
        result.parse_errors.append("No response from Jev API")
        return result

    result.raw_response = json.dumps(raw)

    # direction (choice)
    d = raw.get("direction", {})
    if isinstance(d, dict):
        choice = d.get("choice")
        if isinstance(choice, str) and choice.strip().lower() in ("up", "down", "pass"):
            result.direction = choice.strip().lower()
        else:
            result.parse_errors.append(f"Invalid direction: {choice!r}")
        probs = d.get("probabilities")
        if isinstance(probs, dict):
            up, down = probs.get("up"), probs.get("down")
            if isinstance(up, (int, float)) and isinstance(down, (int, float)) and (up + down) > 0:
                result.p_up = up / (up + down)
    else:
        result.parse_errors.append(f"Unexpected direction format: {d!r}")

    # conviction (score)
    c = raw.get("conviction", {})
    score = c.get("score") if isinstance(c, dict) else None
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        result.conviction = max(0, min(3, round(score)))
    else:
        result.parse_errors.append(f"Bad conviction score: {score!r}")

    # should_trade (noul: probability -> threshold at 0.5)
    t = raw.get("should_trade", {})
    noul = t.get("noul") if isinstance(t, dict) else None
    if isinstance(noul, bool):
        result.should_trade = noul
    elif isinstance(noul, (int, float)):
        result.should_trade = float(noul) > 0.5
    else:
        result.parse_errors.append(f"Bad noul value: {noul!r}")

    parts = []
    for key in ("direction", "conviction"):
        entry = raw.get(key, {})
        if isinstance(entry, dict) and "probabilities" in entry:
            parts.append(f"{key}={entry['probabilities']}")
    if isinstance(noul, (int, float)) and not isinstance(noul, bool):
        parts.append(f"should_trade={noul:.2f}")
    result.reasoning = "; ".join(parts)
    return result


def decide_trade(event_data: dict[str, Any], ctx: dict[str, Any]) -> DecisionResult:
    """Single batched Jev call — all three questions in one request.

    ``ctx`` keys: spot, strike, minutes_left, minute_vol, p_yes_model,
    yes_ask, no_ask, order_book_yes, order_book_no.
    """
    prompt = _build_prompt(event_data, ctx)
    decision = _parse_jev_response(_call_jev(prompt))
    if decision.parse_errors:
        logger.warning("Jev parse issues: %s", decision.parse_errors)
    logger.info(
        "Jev decision: %s (conviction=%d, trade=%s%s)",
        decision.direction,
        decision.conviction,
        decision.should_trade,
        f" — {decision.reasoning[:100]}" if decision.reasoning else "",
    )
    return decision
