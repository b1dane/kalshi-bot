"""Pure-math helpers: a simple fair-probability model and Kalshi fee estimates.

The model is deliberately basic (zero drift, normal returns, realized vol).
Its job is to give the bot an independent reference price so it only trades
when the market's ask is meaningfully below its own estimate of the odds.
"""

import math

# Kalshi taker fee is roughly 0.07 * contracts * price * (1 - price), rounded
# up to the next cent. Verify against Kalshi's current fee schedule.
FEE_RATE = 0.07

# BTC has fat tails; a normal model is overconfident near 0/1, so clamp.
P_CLAMP = (0.03, 0.97)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(
    spot: float | None,
    strike: float | None,
    minutes_left: float,
    minute_vol: float | None,
) -> float | None:
    """P(BTC finishes above strike) given spot, time left, 1-min log-return vol."""
    if not spot or not strike or spot <= 0 or strike <= 0:
        return None
    if minute_vol is None or minute_vol <= 0:
        return None
    if minutes_left <= 0:
        return 1.0 if spot > strike else 0.0
    sigma = minute_vol * math.sqrt(minutes_left)
    p = norm_cdf(math.log(spot / strike) / sigma)
    return min(max(p, P_CLAMP[0]), P_CLAMP[1])


def fee_per_contract(price: float) -> float:
    """Unrounded per-contract fee estimate (used for edge gating)."""
    return FEE_RATE * price * (1.0 - price)


def order_fee(contracts: int, price: float) -> float:
    """Fee for a whole order, rounded up to the cent."""
    raw = FEE_RATE * contracts * price * (1.0 - price)
    return math.ceil(round(raw * 100.0, 6)) / 100.0
