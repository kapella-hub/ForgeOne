"""Tests for P&L + exit-rule precedence. Canned price traces, no WebSocket."""
from __future__ import annotations

import pytest

from forgeone.strategies.pnl import (
    PROFIT_LOCK_STOP_PCT,
    PROFIT_LOCK_TRIGGER_PCT,
    REVERSAL_TRIGGER_PCT,
    REVERSAL_WINDOW_SEC,
    ClosedTradePnL,
    OpenPosition,
    apply_slippage,
    compute_pnl,
    should_exit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pos_up(entry_ts: float = 1000.0, entry_price: float = 100.0,
            notional_usd: float = 10_000.0, leverage: float = 3.0) -> OpenPosition:
    return OpenPosition(
        direction="up", entry_ts=entry_ts, entry_price=entry_price,
        notional_usd=notional_usd, leverage=leverage, period_ts=int(entry_ts - 400),
    )


def _pos_down(entry_ts: float = 1000.0, entry_price: float = 100.0,
              notional_usd: float = 10_000.0, leverage: float = 3.0) -> OpenPosition:
    return OpenPosition(
        direction="down", entry_ts=entry_ts, entry_price=entry_price,
        notional_usd=notional_usd, leverage=leverage, period_ts=int(entry_ts - 400),
    )


# ---------------------------------------------------------------------------
# Apply slippage
# ---------------------------------------------------------------------------


def test_apply_slippage_buy_pays_worse():
    # direction +1 (buying) => we pay higher.
    assert apply_slippage(100.0, +1, 0.00015) == pytest.approx(100.015)


def test_apply_slippage_sell_receives_worse():
    # direction -1 (selling) => we receive lower.
    assert apply_slippage(100.0, -1, 0.00015) == pytest.approx(99.985)


# ---------------------------------------------------------------------------
# Exit rule — reversal stop
# ---------------------------------------------------------------------------


def test_reversal_triggers_within_window():
    p = _pos_up()
    # Mid drops 0.2% within 60s (well past the 0.15% threshold) => reversal.
    mid = 99.8
    assert should_exit(p, now_ts=p.entry_ts + 30, mid_price=mid) == "reversal"


def test_reversal_below_threshold_does_not_trigger():
    p = _pos_up()
    mid = 100.0 * (1 - REVERSAL_TRIGGER_PCT * 0.5)  # 0.075% against — not enough
    assert should_exit(p, now_ts=p.entry_ts + 30, mid_price=mid) is None


def test_reversal_window_expires_at_60s():
    p = _pos_up()
    mid = 99.7  # 0.3% reversal — clear trigger if we were still in window
    # At t+61s, reversal window is closed; returns None (time stop far in future).
    assert should_exit(p, now_ts=p.entry_ts + REVERSAL_WINDOW_SEC + 1, mid_price=mid) is None


def test_reversal_for_short():
    p = _pos_down()
    mid = 100.2  # 0.2% up — triggers reversal for a short
    assert should_exit(p, now_ts=p.entry_ts + 30, mid_price=mid) == "reversal"


# ---------------------------------------------------------------------------
# Exit rule — profit lock
# ---------------------------------------------------------------------------


def test_profit_lock_arms_then_fires():
    p = _pos_up()
    # Push extension 0.6% (past the 0.50% arm threshold).
    mid_up = 100.6
    # Tick 1: lock arms but no exit yet.
    r1 = should_exit(p, now_ts=p.entry_ts + 120, mid_price=mid_up)
    assert r1 is None
    assert p.profit_lock_activated is True
    # Stop is 100 * (1 + 0.05%) = 100.05 (but via float math; use approx).
    assert p.profit_lock_stop_price == pytest.approx(100.05, rel=1e-9)

    # Tick 2: price drops back to the stop level => exit.
    r2 = should_exit(p, now_ts=p.entry_ts + 180, mid_price=100.05)
    assert r2 == "profit_lock"


def test_profit_lock_does_not_fire_if_price_stays_above_stop():
    p = _pos_up()
    should_exit(p, now_ts=p.entry_ts + 120, mid_price=100.6)
    # Price keeps going up — no exit.
    assert should_exit(p, now_ts=p.entry_ts + 180, mid_price=100.8) is None


def test_profit_lock_for_short_uses_inverse_levels():
    p = _pos_down()
    mid_down = 99.4  # 0.6% down
    should_exit(p, now_ts=p.entry_ts + 120, mid_price=mid_down)
    assert p.profit_lock_activated is True
    # For shorts, stop is entry * (1 - 0.05%) = 99.95.
    assert p.profit_lock_stop_price == pytest.approx(99.95, rel=1e-9)

    # Price bounces back to the stop => exit.
    assert should_exit(p, now_ts=p.entry_ts + 180, mid_price=99.95) == "profit_lock"


# ---------------------------------------------------------------------------
# Exit rule — time stop
# ---------------------------------------------------------------------------


def test_time_stop_fires_60s_after_period_end():
    p = _pos_up(entry_ts=1000.0)  # period_ts=600, period_end=1500
    # At 1560 => time stop.
    assert should_exit(p, now_ts=1560, mid_price=100.0) == "time"


def test_time_stop_not_yet_at_59s_after():
    p = _pos_up(entry_ts=1000.0)  # period_end=1500
    assert should_exit(p, now_ts=1559, mid_price=100.0) is None


# ---------------------------------------------------------------------------
# Exit rule — precedence
# ---------------------------------------------------------------------------


def test_reversal_takes_precedence_over_time():
    """Same tick: reversal is checked first, so it wins."""
    p = _pos_up(entry_ts=1440.0)  # period_ts=1040, period_end=1940
    # Reversal window closes at 1500. Pick a moment before 1500 where mid reversed.
    mid_rev = 99.7
    assert should_exit(p, now_ts=1450.0, mid_price=mid_rev) == "reversal"


def test_profit_lock_arms_even_in_reversal_window():
    """If price is up 0.6% but not reversed, profit_lock arms; reversal doesn't fire."""
    p = _pos_up()
    r = should_exit(p, now_ts=p.entry_ts + 30, mid_price=100.6)
    assert r is None
    assert p.profit_lock_activated is True


# ---------------------------------------------------------------------------
# P&L computation
# ---------------------------------------------------------------------------


def test_pnl_long_win_no_funding():
    p = _pos_up(entry_ts=1000.0, entry_price=100.0, notional_usd=10_000)
    # Entry price here is the filled price (post-slippage applied upstream).
    # Mid at exit = 100.5 (long wins 0.5%).
    exit_fill, pnl = compute_pnl(
        pos=p, exit_ts=1500.0, exit_mid=100.5,
        slippage_frac=0.00015, taker_fee_frac=0.00025, funding_rate_per_hour=0.0,
    )
    assert exit_fill == pytest.approx(100.5 * (1 - 0.00015))
    assert pnl.gross_pnl_usd > 0
    assert pnl.fees_usd > 0
    assert pnl.funding_usd == 0.0
    assert pnl.net_pnl_usd == pytest.approx(pnl.gross_pnl_usd - pnl.fees_usd)


def test_pnl_short_win():
    p = _pos_down(entry_ts=1000.0, entry_price=100.0, notional_usd=10_000)
    exit_fill, pnl = compute_pnl(
        pos=p, exit_ts=1500.0, exit_mid=99.5,
        slippage_frac=0.00015, taker_fee_frac=0.00025, funding_rate_per_hour=0.0,
    )
    # For shorts we buy back at ask => pay worse (higher).
    assert exit_fill == pytest.approx(99.5 * (1 + 0.00015))
    assert pnl.gross_pnl_usd > 0


def test_pnl_funding_charged_to_long_when_rate_positive():
    p = _pos_up(entry_ts=0.0, entry_price=100.0, notional_usd=10_000)
    # Hold 1 hour, funding rate 0.0001 (0.01%/h).
    _, pnl_fund = compute_pnl(
        pos=p, exit_ts=3600.0, exit_mid=100.0,
        slippage_frac=0.0, taker_fee_frac=0.0, funding_rate_per_hour=0.0001,
    )
    # Long pays: -1 * 0.0001 * 10_000 * 1.0 = -1.0
    assert pnl_fund.funding_usd == pytest.approx(-1.0)


def test_pnl_funding_credited_to_short_when_rate_positive():
    p = _pos_down(entry_ts=0.0, entry_price=100.0, notional_usd=10_000)
    _, pnl_fund = compute_pnl(
        pos=p, exit_ts=3600.0, exit_mid=100.0,
        slippage_frac=0.0, taker_fee_frac=0.0, funding_rate_per_hour=0.0001,
    )
    # Short receives: +1 * 0.0001 * 10_000 * 1 = +1.0
    assert pnl_fund.funding_usd == pytest.approx(+1.0)


def test_closed_trade_breakdown_is_frozen_dataclass():
    p = _pos_up()
    _, pnl = compute_pnl(p, 1100.0, 100.5, 0.0001, 0.0002, None)
    assert isinstance(pnl, ClosedTradePnL)
    with pytest.raises((AttributeError, Exception)):
        pnl.net_pnl_usd = 999  # type: ignore[misc]
