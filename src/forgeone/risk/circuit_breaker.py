"""Circuit breaker — generalized from polymarket-bot/run_contrarian.py:113-338.

Tracks consecutive losses; when a threshold is hit, forces the caller to skip a fixed
number of signal periods. Wins reset the counter.

Example:
    cb = CircuitBreaker(loss_threshold=3, cooldown_periods=4)
    if cb.is_cooling_down(period_ts):
        skip()
    else:
        fire(); cb.record_win() / cb.record_loss(period_ts)

The `period_ts` parameter is threaded through so `tick()` can decrement cooldown at most
once per new period (matches run_sniper.py:302-305's `last_skip_period` dedup).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CircuitBreaker:
    loss_threshold: int = 3
    cooldown_periods: int = 4
    consecutive_losses: int = 0
    cooldown_remaining: int = 0
    _last_tick_period: int | None = None

    def record_win(self) -> None:
        self.consecutive_losses = 0

    def record_loss(self) -> bool:
        """Return True iff this loss triggered the breaker (new cooldown started)."""
        self.consecutive_losses += 1
        if self.consecutive_losses >= self.loss_threshold and self.cooldown_remaining == 0:
            self.cooldown_remaining = self.cooldown_periods
            # Reset the counter so the next streak must also reach the threshold.
            self.consecutive_losses = 0
            return True
        return False

    def tick(self, period_ts: int) -> None:
        """Called once per period-start. Decrements cooldown if we're in a new period."""
        if self._last_tick_period == period_ts:
            return
        self._last_tick_period = period_ts
        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1

    def is_cooling_down(self) -> bool:
        return self.cooldown_remaining > 0

    def to_dict(self) -> dict:
        return {
            "loss_threshold": self.loss_threshold,
            "cooldown_periods": self.cooldown_periods,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_remaining": self.cooldown_remaining,
            "_last_tick_period": self._last_tick_period,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CircuitBreaker:
        return cls(
            loss_threshold=int(d.get("loss_threshold", 3)),
            cooldown_periods=int(d.get("cooldown_periods", 4)),
            consecutive_losses=int(d.get("consecutive_losses", 0)),
            cooldown_remaining=int(d.get("cooldown_remaining", 0)),
            _last_tick_period=d.get("_last_tick_period"),
        )
