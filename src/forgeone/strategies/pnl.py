"""Pure P&L + exit-rule helpers for the Hyperliquid paper strategy.

Separate from the async orchestrator so we can unit-test every branch without starting
WebSockets.

Exit rules (from the mission briefing, evaluated in order each tick):
  1. Reversal — if BTC reverses >= 0.15% against entry within 60s of fire, close at mid.
  2. Profit-lock — if BTC extends >= 0.50% in signal direction, tighten stop to
     entry ± 0.05%; on a subsequent tick if that stop is hit, close at the stop.
  3. Time — at period_end + 60s, close at current mid.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExitReason = Literal["reversal", "profit_lock", "time"]

# Exit thresholds (from briefing).
REVERSAL_TRIGGER_PCT: float = 0.0015      # 0.15%
REVERSAL_WINDOW_SEC: int = 60
PROFIT_LOCK_TRIGGER_PCT: float = 0.0050   # 0.50%
PROFIT_LOCK_STOP_PCT: float = 0.0005      # 0.05%
TIME_STOP_AFTER_PERIOD_END_SEC: int = 60


@dataclass
class OpenPosition:
    """Mutable position state — updated each tick until exit."""
    direction: Literal["up", "down"]
    entry_ts: float
    entry_price: float
    notional_usd: float
    leverage: float
    period_ts: int
    profit_lock_activated: bool = False
    profit_lock_stop_price: float | None = None

    @property
    def period_end_ts(self) -> int:
        return self.period_ts + 900

    @property
    def sign(self) -> int:
        return 1 if self.direction == "up" else -1


def apply_slippage(mid_price: float, direction_sign: int, slippage_frac: float) -> float:
    """Return the filled price after slippage. For a buy (sign +1), we pay a worse
    (higher) price; for a sell (sign -1), we receive a worse (lower) price."""
    return mid_price * (1.0 + direction_sign * slippage_frac)


def should_exit(pos: OpenPosition, now_ts: float, mid_price: float) -> ExitReason | None:
    """Evaluate exit conditions in order. Mutates `pos` when activating the profit-lock.

    Returns the exit reason if exit is triggered this tick, else None.
    """
    # ---- Rule 1: reversal stop (only within the first REVERSAL_WINDOW_SEC seconds).
    if now_ts - pos.entry_ts <= REVERSAL_WINDOW_SEC:
        counter_pct = _counter_move_pct(pos.entry_price, mid_price, pos.sign)
        if counter_pct >= REVERSAL_TRIGGER_PCT:
            return "reversal"

    # ---- Rule 2: profit-lock.
    extension_pct = _extension_pct(pos.entry_price, mid_price, pos.sign)
    if not pos.profit_lock_activated and extension_pct >= PROFIT_LOCK_TRIGGER_PCT:
        pos.profit_lock_activated = True
        # stop is entry + 0.05% for longs, entry - 0.05% for shorts
        pos.profit_lock_stop_price = pos.entry_price * (1 + pos.sign * PROFIT_LOCK_STOP_PCT)
        # Don't exit on the same tick the lock activates; wait for price to touch stop.
    elif pos.profit_lock_activated and pos.profit_lock_stop_price is not None:
        # For longs, exit if mid <= stop_price. For shorts, exit if mid >= stop_price.
        if pos.sign == 1 and mid_price <= pos.profit_lock_stop_price:
            return "profit_lock"
        if pos.sign == -1 and mid_price >= pos.profit_lock_stop_price:
            return "profit_lock"

    # ---- Rule 3: time stop.
    if now_ts >= pos.period_end_ts + TIME_STOP_AFTER_PERIOD_END_SEC:
        return "time"

    return None


def _counter_move_pct(entry_price: float, mid_price: float, sign: int) -> float:
    """How far has mid moved AGAINST the direction, as a positive fraction?"""
    raw_move = (mid_price - entry_price) / entry_price
    return max(0.0, -sign * raw_move)


def _extension_pct(entry_price: float, mid_price: float, sign: int) -> float:
    """How far has mid moved WITH the direction, as a positive fraction?"""
    raw_move = (mid_price - entry_price) / entry_price
    return max(0.0, sign * raw_move)


@dataclass(frozen=True, slots=True)
class ClosedTradePnL:
    gross_pnl_usd: float
    fees_usd: float
    slippage_usd: float
    funding_usd: float
    net_pnl_usd: float


def compute_pnl(
    pos: OpenPosition,
    exit_ts: float,
    exit_mid: float,
    slippage_frac: float,
    taker_fee_frac: float,
    funding_rate_per_hour: float | None,
) -> tuple[float, ClosedTradePnL]:
    """Compute exit fill price + full P&L breakdown.

    Returns (exit_fill_price, pnl_breakdown).

    - `slippage_frac` applies on the *against-us* side of the mid. Buy-to-close (short
      exit): pay higher (ask). Sell-to-close (long exit): receive lower (bid).
    - `taker_fee_frac` applied to notional on BOTH entry and exit.
    - `funding_rate_per_hour` is the HL funding rate (decimal; 0.00001 == 0.001%/h).
      Longs pay positive funding; shorts receive it. Accrued proportional to hours held.
    """
    # Exit fill: opposite side of book vs. entry => sign of slippage flips.
    exit_sign = -pos.sign
    exit_fill = apply_slippage(exit_mid, exit_sign, slippage_frac)

    # Gross P&L (perp linear): notional * sign * (exit - entry) / entry
    gross = pos.notional_usd * pos.sign * (exit_fill - pos.entry_price) / pos.entry_price

    # Fees: taker_fee_frac on entry_notional + exit_notional.
    entry_notional = pos.notional_usd
    exit_notional = pos.notional_usd * (exit_fill / pos.entry_price)
    fees = taker_fee_frac * (entry_notional + exit_notional)

    # Slippage "cost" (informational — already baked into fill prices). Compute as
    # the $ difference between entry_fill and entry_mid + same for exit.
    # entry_mid == pos.entry_price / (1 + sign * slippage_frac), but we don't have
    # the mid separately; approximate slippage = slippage_frac * notional on each side.
    slippage_cost = slippage_frac * (entry_notional + exit_notional)

    # Funding: only applied if funding rate is known; prorate over hours held.
    if funding_rate_per_hour is None:
        funding = 0.0
    else:
        hours_held = max(0.0, (exit_ts - pos.entry_ts) / 3600.0)
        # Longs pay funding when rate > 0; shorts receive it.
        funding = -pos.sign * funding_rate_per_hour * pos.notional_usd * hours_held

    net = gross - fees + funding  # slippage_cost is already in `gross` via fill prices

    return exit_fill, ClosedTradePnL(
        gross_pnl_usd=gross,
        fees_usd=fees,
        slippage_usd=slippage_cost,
        funding_usd=funding,
        net_pnl_usd=net,
    )
