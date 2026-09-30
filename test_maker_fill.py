from maker_fill import would_fill


def test_fill_when_trade_crosses_resting_yes_bid():
    assert would_fill(side="yes", limit_price=0.92, yes_trade_price=0.92, no_trade_price=None)


def test_no_fill_when_market_does_not_reach_resting_bid():
    assert not would_fill(side="yes", limit_price=0.92, yes_trade_price=0.94, no_trade_price=None)


def test_no_fill_when_opposite_side_is_used():
    assert not would_fill(side="yes", limit_price=0.92, yes_trade_price=None, no_trade_price=0.90)
