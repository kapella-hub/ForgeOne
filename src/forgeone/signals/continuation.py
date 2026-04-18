"""Continuation Mode signal — ported from polymarket-bot/run_sniper.py:87-120 + 503-528.

Pure functions. No venue, no execution, no I/O. The signal fires when, within minutes 4-10
of a 15-min UTC period, BTC and ETH both drift in the same direction by more than their
respective thresholds, and BTC has not counter-reverted meaningfully in the last 60s.

Byte-for-byte parity with the source helpers is enforced by tests/test_signals_continuation.py
and scripts/backtest_continuation_port.py (must reproduce 33W/3L/36 fires on the validated
72h Binance-klines window).

DO NOT change the helper math without rerunning the parity backtest and updating
tests/fixtures/signal_parity.json.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# Thresholds — verbatim from polymarket-bot/run_sniper.py:73-79.
# ---------------------------------------------------------------------------
ACTIVE_START_SEC: int = 240            # minute 4
ACTIVE_END_SEC: int = 600              # minute 10
LOOKBACK_SEC: int = 300                # 5-min rolling
BTC_MIN_MOVE_PCT: float = 0.003        # 0.30%
ETH_MIN_MOVE_PCT: float = 0.0025       # 0.25%
REVERSAL_WINDOW_SEC: int = 60
REVERSAL_BLOCK_PCT: float = 0.0012     # 0.12% peak-to-current counter-move

PERIOD_SEC: int = 900                  # 15-min period

Direction = Literal["up", "down"]

SkipReason = Literal[
    "insufficient_history",
    "btc_move_too_small",
    "eth_move_too_small",
    "direction_mismatch",
    "reversal_block",
    "outside_active_window",
]


@dataclass(frozen=True, slots=True)
class ContinuationSignal:
    """A fired continuation signal.

    `now_ts` is the float wall-clock time at which the signal fired.
    `period_ts` is the UTC-epoch-second start of the 15-min period the signal belongs to.
    `elapsed_in_period_sec` is the integer seconds since period start (matches run_sniper.py:292).
    """
    direction: Direction
    btc_move_pct: float
    eth_move_pct: float
    reversal_pct: float
    period_ts: int
    elapsed_in_period_sec: int
    now_ts: float


# ---------------------------------------------------------------------------
# Helpers — byte-for-byte ports of run_sniper.py:87-120.
# The identifiers below match the source names (with underscore prefix dropped).
# ---------------------------------------------------------------------------


def get_price_at(history: deque, target_ts: float) -> Optional[float]:
    """Return the latest price at or before `target_ts`, or None if none exists.

    Source: run_sniper.py:87-94.
    """
    best = None
    for ts, price in history:
        if ts <= target_ts:
            best = price
        else:
            break
    return best


def compute_move_pct(history: deque, now_ts: float, lookback_sec: int) -> Optional[float]:
    """Percent change over the last `lookback_sec` seconds. None if not enough data.

    Source: run_sniper.py:97-104.
    """
    if len(history) < 2:
        return None
    p_old = get_price_at(history, now_ts - lookback_sec)
    p_now = history[-1][1]
    if p_old is None or p_old <= 0:
        return None
    return (p_now - p_old) / p_old


def reversal_counter_move(
    history: deque, now_ts: float, window_sec: int, direction: str
) -> float:
    """Peak-to-current counter-move over the last `window_sec` seconds.

    For direction="up", returns max(peak_high - p_now, 0) / p_now.
    For direction="down", returns max(p_now - peak_low, 0) / p_now.

    Source: run_sniper.py:107-120.
    """
    if not history:
        return 0.0
    cutoff = now_ts - window_sec
    recent = [p for ts, p in history if ts >= cutoff]
    if not recent:
        return 0.0
    p_now = recent[-1]
    if p_now <= 0:
        return 0.0
    if direction == "up":
        return max(0.0, (max(recent) - p_now) / p_now)
    return max(0.0, (p_now - min(recent)) / p_now)


def period_ts(now_ts: float) -> int:
    """15-min UTC-aligned period start. Matches run_sniper.py:290-291 semantics
    (`int(time.time()) // 900 * 900`) — int-truncation on the now_ts before flooring."""
    return (int(now_ts) // PERIOD_SEC) * PERIOD_SEC


def in_active_window(now_ts: float) -> bool:
    """True iff `now_ts` falls within minutes 4-10 of its 15-min period."""
    elapsed = int(now_ts) - period_ts(now_ts)
    return ACTIVE_START_SEC <= elapsed <= ACTIVE_END_SEC


# ---------------------------------------------------------------------------
# High-level evaluator — mirrors the gate logic at run_sniper.py:503-528.
# Does NOT do entry-price cap, order-book depth, market lookup, or sizing;
# those are execution-layer concerns. This returns only the signal.
# ---------------------------------------------------------------------------


def evaluate(
    btc_hist: deque, eth_hist: deque, now_ts: float
) -> Optional[ContinuationSignal]:
    """Return a ContinuationSignal if the signal fires at `now_ts`, else None.

    Gating order (identical to run_sniper.py:503-528):
      1. now_ts falls inside [240, 600] seconds of its 15-min period.
      2. both BTC and ETH 5-min moves are computable.
      3. |btc_move| >= 0.30%.
      4. |eth_move| >= 0.25%.
      5. btc and eth moves are same-direction.
      6. BTC 60-sec reversal counter-move < 0.12%.

    None of these are mutated from the source — the helpers above are pure ports.
    """
    if not in_active_window(now_ts):
        return None

    btc_move = compute_move_pct(btc_hist, now_ts, LOOKBACK_SEC)
    eth_move = compute_move_pct(eth_hist, now_ts, LOOKBACK_SEC)
    if btc_move is None or eth_move is None:
        return None

    if abs(btc_move) < BTC_MIN_MOVE_PCT:
        return None
    if abs(eth_move) < ETH_MIN_MOVE_PCT:
        return None
    if (btc_move > 0) != (eth_move > 0):
        return None

    direction: Direction = "up" if btc_move > 0 else "down"
    rev = reversal_counter_move(btc_hist, now_ts, REVERSAL_WINDOW_SEC, direction)
    if rev >= REVERSAL_BLOCK_PCT:
        return None

    p_ts = period_ts(now_ts)
    elapsed = int(now_ts) - p_ts
    return ContinuationSignal(
        direction=direction,
        btc_move_pct=btc_move,
        eth_move_pct=eth_move,
        reversal_pct=rev,
        period_ts=p_ts,
        elapsed_in_period_sec=elapsed,
        now_ts=now_ts,
    )


def evaluate_with_reason(
    btc_hist: deque, eth_hist: deque, now_ts: float
) -> tuple[Optional[ContinuationSignal], Optional[SkipReason]]:
    """Same as evaluate() but also returns the first skip reason (for observability).

    Returns (signal, None) if fired, or (None, reason) if not.
    """
    if not in_active_window(now_ts):
        return None, "outside_active_window"

    btc_move = compute_move_pct(btc_hist, now_ts, LOOKBACK_SEC)
    eth_move = compute_move_pct(eth_hist, now_ts, LOOKBACK_SEC)
    if btc_move is None or eth_move is None:
        return None, "insufficient_history"

    if abs(btc_move) < BTC_MIN_MOVE_PCT:
        return None, "btc_move_too_small"
    if abs(eth_move) < ETH_MIN_MOVE_PCT:
        return None, "eth_move_too_small"
    if (btc_move > 0) != (eth_move > 0):
        return None, "direction_mismatch"

    direction: Direction = "up" if btc_move > 0 else "down"
    rev = reversal_counter_move(btc_hist, now_ts, REVERSAL_WINDOW_SEC, direction)
    if rev >= REVERSAL_BLOCK_PCT:
        return None, "reversal_block"

    p_ts = period_ts(now_ts)
    elapsed = int(now_ts) - p_ts
    return (
        ContinuationSignal(
            direction=direction,
            btc_move_pct=btc_move,
            eth_move_pct=eth_move,
            reversal_pct=rev,
            period_ts=p_ts,
            elapsed_in_period_sec=elapsed,
            now_ts=now_ts,
        ),
        None,
    )


__all__ = [
    "ACTIVE_START_SEC",
    "ACTIVE_END_SEC",
    "LOOKBACK_SEC",
    "BTC_MIN_MOVE_PCT",
    "ETH_MIN_MOVE_PCT",
    "REVERSAL_WINDOW_SEC",
    "REVERSAL_BLOCK_PCT",
    "PERIOD_SEC",
    "ContinuationSignal",
    "Direction",
    "SkipReason",
    "compute_move_pct",
    "evaluate",
    "evaluate_with_reason",
    "get_price_at",
    "in_active_window",
    "period_ts",
    "reversal_counter_move",
]
