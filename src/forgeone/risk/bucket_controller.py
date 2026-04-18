"""Per-bucket risk controller. One controller per bucket — no shared state.

Enforces (Phase 1):
  1. One open trade at a time (mechanical — no overlapping positions).
  2. Daily loss cap: if the bucket's daily_pnl_usd drops below -daily_loss_cap_frac of
     the *starting bankroll for that day*, block entries until next UTC day.
  3. Circuit breaker: consecutive losses trigger a period-level cooldown (see
     risk.circuit_breaker). Resumes automatically after cooldown ticks elapse.

In paper mode the controller behaves identically to live — this is deliberate so the
Phase 2 live code inherits a tested risk layer with zero surprises.
"""
from __future__ import annotations

from dataclasses import dataclass

from forgeone.risk.circuit_breaker import CircuitBreaker
from forgeone.state.bucket import BucketState


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason: str | None  # None when allowed


class BucketRiskController:
    def __init__(
        self,
        state: BucketState,
        daily_loss_cap_frac: float = 0.20,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.state = state
        self.daily_loss_cap_frac = float(daily_loss_cap_frac)
        self._circuit_breaker = circuit_breaker or CircuitBreaker.from_dict(
            state.circuit_breaker
        )
        # Track day-start bankroll (for % cap semantics).
        self._day_start_bankroll: float = state.bankroll_usd - state.daily_pnl_usd

    # ------------------------------------------------------------------ gating

    def can_enter(self, period_ts: int) -> GateDecision:
        self._roll_day_if_needed()
        self._circuit_breaker.tick(period_ts)

        if self.state.open_trade is not None:
            return GateDecision(False, "open_trade_exists")

        if self._circuit_breaker.is_cooling_down():
            return GateDecision(False, "circuit_breaker_cooldown")

        loss_cap_usd = self.daily_loss_cap_frac * self._day_start_bankroll
        if self.state.daily_pnl_usd <= -loss_cap_usd:
            return GateDecision(False, "daily_loss_cap_hit")

        return GateDecision(True, None)

    # ------------------------------------------------------------------ fills

    def record_open(self, trade: dict) -> None:
        self.state.open_trade = trade

    def record_close(self, pnl_usd: float, won: bool) -> None:
        self.state.apply_realized_pnl(pnl_usd)
        self.state.open_trade = None
        if won:
            self._circuit_breaker.record_win()
        else:
            self._circuit_breaker.record_loss()
        # Persist breaker back into state dict.
        self.state.circuit_breaker = self._circuit_breaker.to_dict()

    # ------------------------------------------------------------------ internals

    def _roll_day_if_needed(self) -> None:
        if self.state.reset_daily_if_needed():
            self._day_start_bankroll = self.state.bankroll_usd

    # ------------------------------------------------------------------ inspection

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker
