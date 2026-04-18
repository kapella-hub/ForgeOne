"""BucketRiskController tests."""
from __future__ import annotations

import pytest

from forgeone.risk.bucket_controller import BucketRiskController
from forgeone.risk.circuit_breaker import CircuitBreaker
from forgeone.state.bucket import BucketState


def _state(bankroll: float = 10_000.0, open_trade: dict | None = None,
           daily_pnl: float = 0.0) -> BucketState:
    return BucketState(
        bucket_id="hyperliquid_paper",
        bankroll_usd=bankroll,
        daily_pnl_usd=daily_pnl,
        peak_value_usd=bankroll,
        open_trade=open_trade,
    )


def test_empty_bucket_allows_entry():
    ctl = BucketRiskController(_state())
    d = ctl.can_enter(period_ts=1_000_000)
    assert d.allowed is True
    assert d.reason is None


def test_open_trade_blocks_entry():
    ctl = BucketRiskController(_state(open_trade={"id": "x"}))
    d = ctl.can_enter(period_ts=1_000_000)
    assert d.allowed is False
    assert d.reason == "open_trade_exists"


def test_daily_loss_cap_blocks():
    # Start day at $10k; lose $2k => 20% = cap hit.
    s = _state(bankroll=8_000.0, daily_pnl=-2_000.0)
    ctl = BucketRiskController(s, daily_loss_cap_frac=0.20)
    d = ctl.can_enter(period_ts=1_000_000)
    assert d.allowed is False
    assert d.reason == "daily_loss_cap_hit"


def test_daily_loss_cap_just_under_threshold_allows():
    s = _state(bankroll=8_001.0, daily_pnl=-1_999.0)
    ctl = BucketRiskController(s, daily_loss_cap_frac=0.20)
    d = ctl.can_enter(period_ts=1_000_000)
    assert d.allowed is True


def test_circuit_breaker_blocks_entry():
    cb = CircuitBreaker(loss_threshold=2, cooldown_periods=3)
    cb.record_loss(); cb.record_loss()  # trips cooldown=3
    ctl = BucketRiskController(_state(), circuit_breaker=cb)
    d = ctl.can_enter(period_ts=1_000_000)
    assert d.allowed is False
    assert d.reason == "circuit_breaker_cooldown"


def test_circuit_breaker_ticks_across_periods():
    # cooldown_periods=3 means "block can_enter for 3 distinct periods post-trip";
    # tick decrements exactly once per distinct period_ts the controller observes.
    cb = CircuitBreaker(loss_threshold=2, cooldown_periods=3)
    cb.record_loss(); cb.record_loss()  # trip: cooldown_remaining=3
    ctl = BucketRiskController(_state(), circuit_breaker=cb)

    # Period 0: tick 3->2, still cooling
    assert ctl.can_enter(period_ts=1_000_000).reason == "circuit_breaker_cooldown"
    # Period 1: tick 2->1, still cooling
    assert ctl.can_enter(period_ts=1_000_900).reason == "circuit_breaker_cooldown"
    # Period 2: tick 1->0, cooldown JUST exhausted at the start of this check -> allowed
    assert ctl.can_enter(period_ts=1_001_800).allowed is True


def test_record_close_updates_state_and_breaker():
    ctl = BucketRiskController(_state(open_trade={"id": "x"}))
    ctl.record_close(pnl_usd=-50.0, won=False)
    assert ctl.state.open_trade is None
    assert ctl.state.bankroll_usd == pytest.approx(9950.0)
    assert ctl.state.daily_pnl_usd == pytest.approx(-50.0)
    assert ctl.circuit_breaker.consecutive_losses == 1


def test_day_rollover_resets_daily_pnl_and_day_start():
    s = _state(bankroll=8_000.0, daily_pnl=-2_000.0)
    s.last_reset_utc_day = "1970-01-01"  # force a rollover on next check
    ctl = BucketRiskController(s, daily_loss_cap_frac=0.20)
    d = ctl.can_enter(period_ts=1_000_000)
    assert d.allowed is True   # new day, cap reset
    assert s.daily_pnl_usd == 0.0


def test_win_then_loss_does_not_trip_breaker():
    cb = CircuitBreaker(loss_threshold=3, cooldown_periods=4)
    ctl = BucketRiskController(_state(open_trade={"id": "x"}), circuit_breaker=cb)
    ctl.record_close(pnl_usd=10.0, won=True)
    assert cb.consecutive_losses == 0
    ctl.state.open_trade = {"id": "y"}
    ctl.record_close(pnl_usd=-5.0, won=False)
    assert cb.consecutive_losses == 1
    assert cb.is_cooling_down() is False
