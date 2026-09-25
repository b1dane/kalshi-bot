"""Jev decision engine — batched three-question call to typesafe.ai.

Asks ALL THREE questions in ONE API call:
  - choice: which_direction (up / down / pass)
  - score:  conviction (0=none, 1=low, 2=medium, 3=high)
  - noul:  should_trade (yes / no)

Returns a structured DecisionResult dataclass.
"""
import json
import logging
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
    should_trade: bool  # True if Jev says trade
    raw_response: str = ""
    parse_errors: list[str] = field(default_factory=list)
    reasoning: str = ""

    @property
    def valid(self) -> bool:
        return self.direction in ("up", "down", "pass") and 0 <= self.conviction <= 3

    def summary(self) -> str:
        if not self.should_trade:
            return f"PASS (conviction={self.conviction})"
        return f"{self.direction.upper()} (conviction={self.conviction})"


# ── Prompt template ─────────────────────────────────────────────────────────


def _build_prompt(
    event_data: dict[str, Any],
    price_context: dict[str, Any],
) -> str:
    """Build a focused prompt from event data and market context."""

    # Extract relevant fields
    event_ticker = event_data.get("event_ticker", "unknown")
    markets = event_data.get("markets", [])
    tick_size = event_data.get("tick_size", 0)

    yes_price = price_context.get("yes_price", 0.50)
    no_price = price_context.get("no_price", 0.50)
    order_book_yes = price_context.get("order_book_yes", [])
    order_book_no = price_context.get("order_book_no", [])
    recent_trades = price_context.get("recent_trades", [])
    mid_price = (yes_price + no_price) / 2

    # Market overview
    market_info_lines = [
        f"Event: {event_ticker}",
        f"Tick size: {tick_size}",
        f"Number of sub-markets: {len(markets)}",
    ]

    if markets:
        for i, m in enumerate(markets[:5]):  # first 5 markets max
            market_info_lines.append(
                f"  Market {i+1}: {m.get('ticker','?')} — {m.get('title','?')} "
                f"(yes={m.get('yes_price', '?')}, no={m.get('no_price', '?')})"
            )

    price_lines = [
        f"Current YES price: ${yes_price:.4f}",
        f"Current NO price:  ${no_price:.4f}",
        f"Mid price:         ${mid_price:.4f}",
    ]

    if order_book_yes:
        best_yes = order_book_yes[0]
        price_lines.append(f"Best bid (YES): {best_yes.get('count',0)} @ ${best_yes.get('price',0):.4f}")
    if order_book_no:
        best_no = order_book_no[0]
        price_lines.append(f"Best ask (NO):  {best_no.get('count',0)} @ ${best_no.get('price',0):.4f}")

    if recent_trades:
        last_trade = recent_trades[0]
        price_lines.append(
            f"Last trade: {last_trade.get('side','?')} {last_trade.get('count',0)} "
            f"@ ${last_trade.get('price',0):.4f}"
        )

    prompt = (
        "You are a BTC 15-minute binary options trader on Kalshi. "
        "Analyze the market data below and decide whether to trade.\n\n"
        "Market:\n" + "\n".join(market_info_lines) + "\n\n"
        "Prices:\n" + "\n".join(price_lines) + "\n\n"
        "Rules:\n"
        "- YES = BTC will be ABOVE the target price at settlement\n"
        "- NO  = BTC will be BELOW the target price at settlement\n"
        "- You buy at the YES price; if correct you get $1, if wrong $0\n"
        "- The YES price is the implied probability (0.50 = 50%)\n\n"
        "You must answer ALL three questions in one response.\n"
        "Response format (JSON):\n"
        "{\n"
        '  "direction": "up" | "down" | "pass",\n'
        '  "conviction": 0 | 1 | 2 | 3,\n'
        '  "should_trade": true | false,\n'
        '  "reasoning": "brief explanation"\n'
        "}"
    )
    return prompt


# ── Jev API call (typed-decision format) ────────────────────────────────────


def _call_jev(prompt: str) -> dict[str, Any] | None:
    """Make a single batched call to the Jev /systemone typed-decision API.

    Sends all three questions (direction, conviction, should_trade) as
    typed questions in a single call using the Jev typed-decision format.
    The API returns calibrated probabilities for each question.

    Returns the parsed JSON dict (the 'answers' dict) or None on failure.
    """
    if not config.jev_configured:
        logger.warning("Jev API key not configured — cannot call Jev")
        return None

    headers = {
        "Authorization": f"Bearer {config.JEV_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "jev-latest",
        "state": prompt,
        "questions": {
            "direction": {
                "type": "choice",
                "options": ["up", "down", "pass"],
                "criteria": {
                    "up": "BTC will settle ABOVE the target price at settlement",
                    "down": "BTC will settle BELOW the target price at settlement",
                    "pass": "Not enough signal, skip this event",
                },
                "instructions": "Predict the direction of the next Kalshi BTC 15-min event.",
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
        },
    }

    try:
        resp = httpx.post(config.JEV_API_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        # Jev returns {"answers": {...}}
        answers = data.get("answers")
        if not answers:
            logger.warning("Jev response has no 'answers' key: %s", data)
            return None

        return answers

    except httpx.HTTPStatusError as exc:
        logger.warning("Jev API HTTP %s: %.200s", exc.response.status_code, exc.response.text)
    except httpx.TimeoutException:
        logger.warning("Jev API timeout (30s)")
    except httpx.RequestError as exc:
        logger.warning("Jev API request error: %s", exc)
    except json.JSONDecodeError as exc:
        logger.warning("Jev API returned non-JSON: %s", exc)
    except Exception as exc:
        logger.exception("Unexpected Jev API error: %s", exc)

    return None


# ── Public interface ────────────────────────────────────────────────────────


def _parse_jev_response(raw: dict[str, Any] | None) -> DecisionResult:
    """Parse the Jev typed-decision API response into a DecisionResult.

    Jev returns answers in the format:
    - direction (choice): {"choice": "up", "probabilities": {"up": 0.6, ...}}
    - conviction (score): {"score": 1.21, "probabilities": {"0": 0.15, ...}}
    - should_trade (noul): {"noul": 0.39}
    """
    result = DecisionResult(direction="pass", conviction=0, should_trade=False)

    if raw is None:
        result.parse_errors.append("No response from Jev API")
        return result

    result.raw_response = json.dumps(raw)

    # Parse direction (choice type)
    direction_data = raw.get("direction", {})
    if isinstance(direction_data, dict):
        choice = direction_data.get("choice")
        if isinstance(choice, str):
            choice = choice.strip().lower()
            if choice in ("up", "down", "pass"):
                result.direction = choice
            else:
                result.parse_errors.append(f"Invalid direction value: {choice}")
        elif choice is not None:
            result.parse_errors.append(f"Non-string direction: {choice}")
        else:
            result.parse_errors.append("No 'choice' key in direction response")
    else:
        result.parse_errors.append(f"Unexpected direction format: {direction_data}")

    # Parse conviction (score type)
    conviction_data = raw.get("conviction", {})
    if isinstance(conviction_data, dict):
        score = conviction_data.get("score")
        if isinstance(score, (int, float)):
            # Map float score (0-3) to nearest int
            result.conviction = max(0, min(3, round(score)))
        elif score is not None:
            result.parse_errors.append(f"Non-numeric conviction score: {score}")
        else:
            result.parse_errors.append("No 'score' key in conviction response")
    else:
        result.parse_errors.append(f"Unexpected conviction format: {conviction_data}")

    # Parse should_trade (noul type — threshold the float)
    trade_data = raw.get("should_trade", {})
    if isinstance(trade_data, dict):
        noul = trade_data.get("noul")
        if isinstance(noul, (int, float)):
            # Threshold: trade if probability > 0.5
            result.should_trade = float(noul) > 0.5
        elif isinstance(noul, bool):
            result.should_trade = noul
        elif noul is not None:
            result.parse_errors.append(f"Non-float noul value: {noul}")
        else:
            result.parse_errors.append("No 'noul' key in should_trade response")
    else:
        result.parse_errors.append(f"Unexpected should_trade format: {trade_data}")

    # Extract reasoning from probabilities for logging
    parts = []
    for key in ("direction", "conviction", "should_trade"):
        entry = raw.get(key, {})
        if isinstance(entry, dict):
            if "probabilities" in entry:
                parts.append(f"{key}={entry['probabilities']}")
            elif "noul" in entry:
                parts.append(f"{key}={entry['noul']:.2f}")
    if parts:
        result.reasoning = "; ".join(parts)

    return result


def decide_trade(
    event_data: dict[str, Any],
    price_context: dict[str, Any],
) -> DecisionResult:
    """Single batched Jev call — all three questions in one request.

    Args:
        event_data: Raw event dict from get_current_event().
        price_context: Dict with yes_price, no_price, order_book_yes,
                      order_book_no, recent_trades.

    Returns:
        DecisionResult with direction, conviction, should_trade.
    """
    prompt = _build_prompt(event_data, price_context)
    raw_response = _call_jev(prompt)
    decision = _parse_jev_response(raw_response)

    logger.info(
        "Jev decision: %s (conviction=%d, trade=%s%s)",
        decision.direction,
        decision.conviction,
        decision.should_trade,
        f" — {decision.reasoning[:100]}" if decision.reasoning else "",
    )

    return decision