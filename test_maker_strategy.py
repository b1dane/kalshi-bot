from maker_strategy import evaluate_maker_entry


def test_selects_leading_side_at_bid_under_inclusive_ceiling():
    result = evaluate_maker_entry(
        yes_bid=0.92,
        yes_ask=0.95,
        no_bid=0.05,
        no_ask=0.08,
        price_ceiling=0.95,
    )
    assert result.eligible is True
    assert result.side == "yes"
    assert result.limit_price == 0.92
    assert result.quantity == 1


def test_skips_when_leading_side_ask_exceeds_ceiling():
    result = evaluate_maker_entry(
        yes_bid=0.94,
        yes_ask=0.96,
        no_bid=0.04,
        no_ask=0.06,
        price_ceiling=0.95,
    )
    assert result.eligible is False
    assert result.reason == "leading_side_ask_above_ceiling"


def test_skips_degenerate_or_missing_quotes():
    result = evaluate_maker_entry(
        yes_bid=0.0,
        yes_ask=1.0,
        no_bid=0.0,
        no_ask=1.0,
        price_ceiling=0.95,
    )
    assert result.eligible is False
    assert result.reason == "invalid_two_sided_quotes"
