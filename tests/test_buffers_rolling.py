"""Tests for RollingPriceBuffer."""
from __future__ import annotations

from forgeone.buffers.rolling import DEFAULT_MAX_AGE_SEC, RollingPriceBuffer


def test_default_max_age_is_390():
    """Matches run_sniper.py's _roll_max_age = LOOKBACK (300) + REV_WIN (60) + 30."""
    assert DEFAULT_MAX_AGE_SEC == 390


def test_append_and_iter():
    b = RollingPriceBuffer(max_age_sec=100)
    b.append(1000.0, 50.0)
    b.append(1050.0, 51.0)
    assert list(b) == [(1000.0, 50.0), (1050.0, 51.0)]
    assert len(b) == 2
    assert b.latest_price() == 51.0
    assert b.latest_ts() == 1050.0


def test_prune_on_append():
    b = RollingPriceBuffer(max_age_sec=60)
    b.append(1000.0, 50.0)
    b.append(1030.0, 51.0)
    b.append(1100.0, 52.0)  # cutoff = 1100 - 60 = 1040, so 1000 and 1030 get dropped.
    assert list(b) == [(1100.0, 52.0)]


def test_prune_keeps_ties_at_cutoff():
    """cutoff check uses strict `< cutoff`, so tick AT cutoff survives."""
    b = RollingPriceBuffer(max_age_sec=60)
    b.append(1000.0, 50.0)
    b.append(1060.0, 51.0)  # cutoff = 1000, tick at 1000 kept (not <).
    assert list(b) == [(1000.0, 50.0), (1060.0, 51.0)]


def test_latest_on_empty():
    b = RollingPriceBuffer()
    assert b.latest_price() is None
    assert b.latest_ts() is None
    assert len(b) == 0


def test_clear():
    b = RollingPriceBuffer()
    b.append(1.0, 2.0)
    b.clear()
    assert len(b) == 0


def test_view_shared_with_signal_deque():
    """The .view() deque should be the exact same object used internally,
    which is what continuation.evaluate() expects to walk."""
    b = RollingPriceBuffer()
    b.append(100.0, 50.0)
    assert b.view() is b._d  # noqa: SLF001
