from kxbtc15m_adapter import kxbtc15m_market_data, panic_order_to_demo_v2


def test_kxbtc15m_maps_to_above_reference():
    md = kxbtc15m_market_data(
        {
            "ticker": "KXBTC15M-TEST-00",
            "status": "active",
            "floor_strike": 83296.48,
            "close_time": "2026-09-30T03:30:00Z",
            "rules_primary": "BRTI average rule",
        },
        best_ask=0.80,
        best_bid=0.75,
    )
    assert md["direction"] == "above"
    assert md["strike_price"] == 83296.48
    assert md["best_ask"] == 0.80
    assert md["best_bid"] == 0.75
    assert md["settlement_source"].startswith("CF Benchmarks BRTI")


def test_panic_no_order_maps_to_demo_v2_buy_no():
    payload = panic_order_to_demo_v2({
        "ticker": "KXBTC15M-TEST-00",
        "action": "buy",
        "side": "no",
        "type": "limit",
        "count": 10,
        "no_price": 35,
        "post_only": True,
        "client_order_id": "abc",
    })
    assert payload["side"] == "ask"
    assert payload["price"] == "0.3500"
    assert payload["count"] == "10.00"
    assert payload["post_only"] is True
    assert payload["self_trade_prevention_type"] == "maker"
    assert payload["cancel_order_on_pause"] is True
