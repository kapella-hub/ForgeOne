"""Per-bucket persistent state. Atomic-write JSON.

State tracks per-bucket bankroll, daily P&L, peak value (for drawdown), circuit breaker
restoration, and the single currently-open trade (Phase 1 enforces one open at a time).
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import orjson

from forgeone.risk.circuit_breaker import CircuitBreaker


def _today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


@dataclass
class BucketState:
    bucket_id: str
    bankroll_usd: float
    daily_pnl_usd: float = 0.0
    peak_value_usd: float = 0.0
    last_reset_utc_day: str = field(default_factory=_today_utc)
    open_trade: dict | None = None
    circuit_breaker: dict = field(default_factory=lambda: CircuitBreaker().to_dict())

    def reset_daily_if_needed(self, today: str | None = None) -> bool:
        today = today or _today_utc()
        if today != self.last_reset_utc_day:
            self.daily_pnl_usd = 0.0
            self.last_reset_utc_day = today
            return True
        return False

    def apply_realized_pnl(self, pnl_usd: float) -> None:
        self.bankroll_usd += pnl_usd
        self.daily_pnl_usd += pnl_usd
        if self.bankroll_usd > self.peak_value_usd:
            self.peak_value_usd = self.bankroll_usd

    @property
    def drawdown_pct(self) -> float:
        if self.peak_value_usd <= 0:
            return 0.0
        return max(0.0, (self.peak_value_usd - self.bankroll_usd) / self.peak_value_usd)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> BucketState:
        return cls(**d)


class BucketStateStore:
    """Atomic-write JSON persistence for a single BucketState."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self, default_factory) -> BucketState:
        if not self.path.exists():
            state = default_factory()
            self.save(state)
            return state
        raw = self.path.read_bytes()
        return BucketState.from_dict(orjson.loads(raw))

    def save(self, state: BucketState) -> None:
        payload = orjson.dumps(state.to_dict(), option=orjson.OPT_INDENT_2)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
