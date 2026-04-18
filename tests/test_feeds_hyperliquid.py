"""Hyperliquid BBO parsing + backoff tests (no live WS dependency)."""
from __future__ import annotations

from forgeone.feeds.binance import backoff_delay
from forgeone.feeds.hyperliquid import parse_bbo


def test_backoff_matches_polymarket_bot_shape():
    # Exponential until cap; capped at 30s.
    assert backoff_delay(0) == 1
    assert backoff_delay(1) == 2
    assert backoff_delay(2) == 4
    assert backoff_delay(3) == 8
    assert backoff_delay(4) == 16
    assert backoff_delay(5) == 30  # 2^5 == 32 → capped at 30
    assert backoff_delay(10) == 30
    assert backoff_delay(100) == 30


def test_parse_bbo_happy_path_dict_shape():
    # Actual HL format observed on wss://api.hyperliquid.xyz/ws 2026-04-18.
    msg = {
        "channel": "bbo",
        "data": {
            "coin": "BTC",
            "time": 1_760_630_400_000,
            "bbo": [
                {"px": "68420.0", "sz": "0.1", "n": 1},
                {"px": "68421.0", "sz": "0.2", "n": 1},
            ],
        },
    }
    mid, ts = parse_bbo(msg, expected_coin="BTC")
    assert mid == (68420.0 + 68421.0) / 2
    assert ts == 1_760_630_400.0


def test_parse_bbo_legacy_list_shape():
    # Tolerate list-of-lists shape defensively.
    msg = {
        "channel": "bbo",
        "data": {
            "coin": "BTC",
            "time": 1_760_630_400_000,
            "bbo": [["68420.0", "0.1", 1], ["68421.0", "0.2", 1]],
        },
    }
    mid, ts = parse_bbo(msg, expected_coin="BTC")
    assert mid == (68420.0 + 68421.0) / 2
    assert ts == 1_760_630_400.0


def test_parse_bbo_wrong_coin():
    msg = {"channel": "bbo",
           "data": {"coin": "ETH", "time": 1,
                    "bbo": [{"px": "1"}, {"px": "2"}]}}
    assert parse_bbo(msg, expected_coin="BTC") == (None, None)


def test_parse_bbo_missing_side():
    msg = {"channel": "bbo", "data": {"coin": "BTC", "time": 1, "bbo": [{"px": "1"}]}}
    assert parse_bbo(msg, expected_coin="BTC") == (None, None)


def test_parse_bbo_crossed_book_rejected():
    msg = {"channel": "bbo",
           "data": {"coin": "BTC", "time": 1,
                    "bbo": [{"px": "100"}, {"px": "99"}]}}
    assert parse_bbo(msg, expected_coin="BTC") == (None, None)


def test_parse_bbo_missing_time_uses_wallclock():
    msg = {"channel": "bbo",
           "data": {"coin": "BTC",
                    "bbo": [{"px": "100"}, {"px": "101"}]}}
    mid, ts = parse_bbo(msg, expected_coin="BTC")
    assert mid == 100.5
    assert ts is not None and ts > 1_700_000_000


def test_parse_bbo_non_bbo_channel_returns_none():
    msg = {"channel": "trades", "data": {"coin": "BTC"}}
    assert parse_bbo(msg, expected_coin="BTC") == (None, None)


def test_parse_bbo_negative_prices():
    msg = {"channel": "bbo",
           "data": {"coin": "BTC", "time": 1,
                    "bbo": [{"px": "-1"}, {"px": "1"}]}}
    assert parse_bbo(msg, expected_coin="BTC") == (None, None)
