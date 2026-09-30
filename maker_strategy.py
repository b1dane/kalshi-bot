"""Fee-aware paper maker-entry policy for KXBTC15M research."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MakerEntry:
    eligible: bool
    side: str | None = None
    limit_price: float | None = None
    quantity: int = 0
    reason: str = ""


def evaluate_maker_entry(
    *,
    yes_bid: float | None,
    yes_ask: float | None,
    no_bid: float | None,
    no_ask: float | None,
    price_ceiling: float = 0.95,
) -> MakerEntry:
    quotes = (yes_bid, yes_ask, no_bid, no_ask)
    if any(v is None or not 0.0 < float(v) < 1.0 for v in quotes):
        return MakerEntry(False, reason="invalid_two_sided_quotes")

    yb, ya, nb, na = map(float, quotes)
    side = "yes" if ya >= na else "no"
    ask = ya if side == "yes" else na
    bid = yb if side == "yes" else nb
    if ask > price_ceiling:
        return MakerEntry(False, reason="leading_side_ask_above_ceiling")
    if bid <= 0.0 or bid >= 1.0 or bid >= ask:
        return MakerEntry(False, reason="invalid_leading_side_book")
    return MakerEntry(True, side=side, limit_price=bid, quantity=1, reason="eligible_resting_maker")
