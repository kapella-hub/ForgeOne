"""Rolling price buffer — thin wrapper around `collections.deque` with auto-prune.

Matches `run_sniper.py:493-496` semantics verbatim:
    rolling_prices[asset].append((now_f, p))
    cutoff = now_f - _roll_max_age
    while rolling_prices[asset] and rolling_prices[asset][0][0] < cutoff:
        rolling_prices[asset].popleft()

Default `max_age_sec` is 390s (= LOOKBACK_SEC 300 + REVERSAL_WINDOW_SEC 60 + 30s slack),
which is what run_sniper.py uses. Same slack ensures the reversal lookup at the trailing
edge still has history to work with.
"""
from __future__ import annotations

from collections import deque
from typing import Iterable

from forgeone.signals import continuation as c


DEFAULT_MAX_AGE_SEC = c.LOOKBACK_SEC + c.REVERSAL_WINDOW_SEC + 30  # 390


class RollingPriceBuffer:
    """Append (ts, price) ticks; older-than-max_age ticks are auto-pruned on append."""

    __slots__ = ("_d", "_max_age_sec")

    def __init__(self, max_age_sec: int = DEFAULT_MAX_AGE_SEC) -> None:
        self._d: deque[tuple[float, float]] = deque()
        self._max_age_sec = int(max_age_sec)

    def append(self, ts: float, price: float) -> None:
        self._d.append((float(ts), float(price)))
        cutoff = float(ts) - self._max_age_sec
        while self._d and self._d[0][0] < cutoff:
            self._d.popleft()

    def view(self) -> deque[tuple[float, float]]:
        """Return the underlying deque. Intended for read-only signal evaluation."""
        return self._d

    def latest_price(self) -> float | None:
        if not self._d:
            return None
        return self._d[-1][1]

    def latest_ts(self) -> float | None:
        if not self._d:
            return None
        return self._d[-1][0]

    def __len__(self) -> int:
        return len(self._d)

    def __iter__(self) -> Iterable[tuple[float, float]]:
        return iter(self._d)

    def clear(self) -> None:
        self._d.clear()
