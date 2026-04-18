"""Parity tests for src/forgeone/signals/continuation.py.

Fixtures live in tests/fixtures/signal_parity.json — hand-computed expected values from the
original polymarket-bot/run_sniper.py helpers. Any change to continuation.py that breaks these
is almost certainly signal drift and must block the release.

The 72h integration gate lives in scripts/backtest_continuation_port.py and asserts
exactly 33W/3L/36 fires.
"""
from __future__ import annotations

import json
from collections import deque

import pytest

from forgeone.signals import continuation as c


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def _hist(pairs: list[list[float]]) -> deque:
    d: deque = deque()
    for ts, p in pairs:
        d.append((float(ts), float(p)))
    return d


def _approx(expected):
    if expected is None:
        return None
    return pytest.approx(expected, rel=1e-12, abs=0.0)


@pytest.fixture(scope="module")
def parity_fixtures(fixtures_dir):
    with (fixtures_dir / "signal_parity.json").open() as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Helper-level parity
# ---------------------------------------------------------------------------


def test_get_price_at_parity(parity_fixtures):
    for case in parity_fixtures["get_price_at"]:
        got = c.get_price_at(_hist(case["history"]), case["target_ts"])
        expected = case["expected"]
        assert got == (expected if expected is not None else None), f"case={case['name']}"


def test_compute_move_pct_parity(parity_fixtures):
    for case in parity_fixtures["compute_move_pct"]:
        got = c.compute_move_pct(_hist(case["history"]), case["now_ts"], case["lookback_sec"])
        expected = case["expected"]
        if expected is None:
            assert got is None, f"case={case['name']}"
        else:
            assert got == _approx(expected), f"case={case['name']}"


def test_reversal_counter_move_parity(parity_fixtures):
    for case in parity_fixtures["reversal_counter_move"]:
        got = c.reversal_counter_move(
            _hist(case["history"]), case["now_ts"], case["window_sec"], case["direction"]
        )
        expected = case["expected"]
        assert got == _approx(expected), f"case={case['name']}"


def test_period_ts_parity(parity_fixtures):
    for case in parity_fixtures["period_ts"]:
        assert c.period_ts(case["now_ts"]) == case["expected"], f"case={case['name']}"


def test_in_active_window_parity(parity_fixtures):
    for case in parity_fixtures["in_active_window"]:
        assert c.in_active_window(case["now_ts"]) is case["expected"], f"case={case['name']}"


# ---------------------------------------------------------------------------
# Threshold constants — pinned to the source (run_sniper.py:73-79).
# ---------------------------------------------------------------------------


def test_thresholds_match_source():
    assert c.ACTIVE_START_SEC == 240
    assert c.ACTIVE_END_SEC == 600
    assert c.LOOKBACK_SEC == 300
    assert c.BTC_MIN_MOVE_PCT == 0.003
    assert c.ETH_MIN_MOVE_PCT == 0.0025
    assert c.REVERSAL_WINDOW_SEC == 60
    assert c.REVERSAL_BLOCK_PCT == 0.0012
    assert c.PERIOD_SEC == 900


# ---------------------------------------------------------------------------
# evaluate() behavioral parity
# ---------------------------------------------------------------------------


def _steady_climb_history(start_ts: float, start_price: float, per_tick_pct: float,
                          n: int, step_sec: float = 1.0) -> deque:
    """Build a history of n ticks climbing by `per_tick_pct` per step."""
    d: deque = deque()
    ts = start_ts
    price = start_price
    for _ in range(n):
        d.append((ts, price))
        ts += step_sec
        price *= (1 + per_tick_pct)
    return d


class TestEvaluate:
    """Behavioral tests mirroring each gate in run_sniper.py:503-528."""

    # Anchor the tests at a known period boundary: period_ts = 1_760_630_400 (UTC).
    # A fire inside the active window means now_ts in [period_ts + 240, period_ts + 600].
    PERIOD = 1_760_630_400
    FIRE_TS = PERIOD + 400  # middle of active window (elapsed=400)

    def test_returns_none_outside_window(self):
        # Same histories that would fire, but outside the 240-600 window.
        btc = _steady_climb_history(self.PERIOD, 100.0, 0.00001, 300)
        eth = _steady_climb_history(self.PERIOD, 100.0, 0.00001, 300)
        assert c.evaluate(btc, eth, self.PERIOD + 100) is None   # elapsed=100 (< 240)
        assert c.evaluate(btc, eth, self.PERIOD + 700) is None   # elapsed=700 (> 600)

    def test_returns_none_insufficient_history(self):
        btc = deque([(self.FIRE_TS, 100.0)])  # only 1 tick
        eth = deque([(self.FIRE_TS, 100.0)])
        assert c.evaluate(btc, eth, self.FIRE_TS) is None

    def test_fires_on_aligned_up_move(self):
        # BTC +0.4% and ETH +0.3% over the last 300s, no counter-reversal.
        btc = _hist([
            [self.FIRE_TS - 300, 100.0],
            [self.FIRE_TS - 200, 100.1],
            [self.FIRE_TS - 100, 100.2],
            [self.FIRE_TS,        100.4],
        ])
        eth = _hist([
            [self.FIRE_TS - 300, 100.0],
            [self.FIRE_TS,        100.3],
        ])
        sig = c.evaluate(btc, eth, self.FIRE_TS)
        assert sig is not None
        assert sig.direction == "up"
        assert sig.btc_move_pct == pytest.approx(0.004, abs=1e-9)
        assert sig.eth_move_pct == pytest.approx(0.003, abs=1e-9)
        assert sig.reversal_pct == pytest.approx(0.0, abs=1e-9)
        assert sig.period_ts == self.PERIOD
        assert sig.elapsed_in_period_sec == 400

    def test_fires_on_aligned_down_move(self):
        btc = _hist([
            [self.FIRE_TS - 300, 100.0],
            [self.FIRE_TS,         99.6],
        ])
        eth = _hist([
            [self.FIRE_TS - 300, 100.0],
            [self.FIRE_TS,         99.7],
        ])
        sig = c.evaluate(btc, eth, self.FIRE_TS)
        assert sig is not None
        assert sig.direction == "down"

    def test_btc_below_threshold_blocks(self):
        # BTC +0.29%, just shy of 0.30% threshold.
        btc = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.29]])
        eth = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.4]])
        assert c.evaluate(btc, eth, self.FIRE_TS) is None

    def test_btc_exactly_at_threshold_fires(self):
        # BTC exactly +0.30% — `abs(move) < THRESH` uses strict less-than, so passes.
        # Use inputs whose IEEE-754 quotient equals BTC_MIN_MOVE_PCT exactly:
        # (100300 - 100000) / 100000 == 0.003 (bit-identical to the 0.003 literal).
        btc = _hist([[self.FIRE_TS - 300, 100000.0], [self.FIRE_TS, 100300.0]])
        eth = _hist([[self.FIRE_TS - 300, 100000.0], [self.FIRE_TS, 100300.0]])
        sig = c.evaluate(btc, eth, self.FIRE_TS)
        assert sig is not None
        assert sig.btc_move_pct == 0.003  # bit-exact

    def test_eth_below_threshold_blocks(self):
        # BTC +0.4% passes; ETH +0.20% blocks.
        btc = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.4]])
        eth = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.2]])
        assert c.evaluate(btc, eth, self.FIRE_TS) is None

    def test_direction_mismatch_blocks(self):
        btc = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.4]])
        eth = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS,  99.6]])
        assert c.evaluate(btc, eth, self.FIRE_TS) is None

    def test_reversal_block_triggers(self):
        # BTC rose to 100.4 (+0.4%) but in the last 60s dropped back 0.13% from a peak.
        btc = _hist([
            [self.FIRE_TS - 300, 100.0],
            [self.FIRE_TS -  50, 100.531],   # peak
            [self.FIRE_TS,        100.4],    # counter-move (100.531-100.4)/100.4 ≈ 0.001304 > 0.0012
        ])
        eth = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.4]])
        assert c.evaluate(btc, eth, self.FIRE_TS) is None

    def test_reversal_just_below_threshold_passes(self):
        # Counter-move (100.5-100.4)/100.4 ≈ 0.000996 < 0.0012 — just passes.
        btc = _hist([
            [self.FIRE_TS - 300, 100.0],
            [self.FIRE_TS -  50, 100.5],
            [self.FIRE_TS,        100.4],
        ])
        eth = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.4]])
        sig = c.evaluate(btc, eth, self.FIRE_TS)
        assert sig is not None
        assert sig.reversal_pct < c.REVERSAL_BLOCK_PCT


class TestEvaluateWithReason:
    PERIOD = 1_760_630_400
    FIRE_TS = PERIOD + 400

    def test_outside_window_reason(self):
        sig, reason = c.evaluate_with_reason(deque(), deque(), self.PERIOD + 100)
        assert sig is None and reason == "outside_active_window"

    def test_insufficient_history_reason(self):
        btc = deque([(self.FIRE_TS, 100.0)])
        eth = deque([(self.FIRE_TS, 100.0)])
        sig, reason = c.evaluate_with_reason(btc, eth, self.FIRE_TS)
        assert sig is None and reason == "insufficient_history"

    def test_btc_too_small_reason(self):
        btc = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.1]])
        eth = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.4]])
        sig, reason = c.evaluate_with_reason(btc, eth, self.FIRE_TS)
        assert sig is None and reason == "btc_move_too_small"

    def test_eth_too_small_reason(self):
        btc = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.4]])
        eth = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.1]])
        sig, reason = c.evaluate_with_reason(btc, eth, self.FIRE_TS)
        assert sig is None and reason == "eth_move_too_small"

    def test_direction_mismatch_reason(self):
        btc = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.4]])
        eth = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 99.6]])
        sig, reason = c.evaluate_with_reason(btc, eth, self.FIRE_TS)
        assert sig is None and reason == "direction_mismatch"

    def test_reversal_block_reason(self):
        btc = _hist([
            [self.FIRE_TS - 300, 100.0],
            [self.FIRE_TS -  50, 100.6],
            [self.FIRE_TS,        100.4],
        ])
        eth = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.4]])
        sig, reason = c.evaluate_with_reason(btc, eth, self.FIRE_TS)
        assert sig is None and reason == "reversal_block"

    def test_fires_returns_none_reason(self):
        btc = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.4]])
        eth = _hist([[self.FIRE_TS - 300, 100.0], [self.FIRE_TS, 100.3]])
        sig, reason = c.evaluate_with_reason(btc, eth, self.FIRE_TS)
        assert sig is not None and reason is None
