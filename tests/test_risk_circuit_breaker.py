"""CircuitBreaker behavioral tests."""
from __future__ import annotations

from forgeone.risk.circuit_breaker import CircuitBreaker


def test_defaults_match_contrarian_pattern():
    cb = CircuitBreaker()
    assert cb.loss_threshold == 3
    assert cb.cooldown_periods == 4


def test_single_loss_does_not_trip():
    cb = CircuitBreaker(loss_threshold=3, cooldown_periods=4)
    assert cb.record_loss() is False
    assert cb.consecutive_losses == 1
    assert cb.is_cooling_down() is False


def test_threshold_loss_trips_breaker():
    cb = CircuitBreaker(loss_threshold=3, cooldown_periods=4)
    cb.record_loss()
    cb.record_loss()
    tripped = cb.record_loss()
    assert tripped is True
    assert cb.is_cooling_down() is True
    assert cb.cooldown_remaining == 4
    # Counter reset after trip.
    assert cb.consecutive_losses == 0


def test_win_resets_streak():
    cb = CircuitBreaker(loss_threshold=3, cooldown_periods=4)
    cb.record_loss()
    cb.record_loss()
    cb.record_win()
    assert cb.consecutive_losses == 0
    tripped = cb.record_loss()
    assert tripped is False
    assert cb.is_cooling_down() is False


def test_tick_decrements_per_new_period_only():
    cb = CircuitBreaker(loss_threshold=3, cooldown_periods=4)
    cb.record_loss(); cb.record_loss(); cb.record_loss()
    assert cb.cooldown_remaining == 4
    # Multiple ticks in same period => one decrement.
    cb.tick(1000); cb.tick(1000); cb.tick(1000)
    assert cb.cooldown_remaining == 3
    cb.tick(1001)
    assert cb.cooldown_remaining == 2
    cb.tick(1002); cb.tick(1003)
    assert cb.cooldown_remaining == 0
    assert cb.is_cooling_down() is False


def test_roundtrip_serialization():
    cb = CircuitBreaker(loss_threshold=2, cooldown_periods=5, consecutive_losses=1)
    cb.record_loss()  # trips
    assert cb.is_cooling_down()
    restored = CircuitBreaker.from_dict(cb.to_dict())
    assert restored.cooldown_remaining == cb.cooldown_remaining
    assert restored.consecutive_losses == cb.consecutive_losses
    assert restored.loss_threshold == 2
    assert restored.cooldown_periods == 5


def test_second_trip_waits_for_cooldown_end():
    """While cooling down, further losses should NOT stack another cooldown."""
    cb = CircuitBreaker(loss_threshold=2, cooldown_periods=3)
    cb.record_loss(); tripped = cb.record_loss()
    assert tripped is True
    assert cb.cooldown_remaining == 3
    # Loss during cooldown: counter grows but no new cooldown.
    cb.record_loss()
    assert cb.cooldown_remaining == 3
    assert cb.consecutive_losses == 1
