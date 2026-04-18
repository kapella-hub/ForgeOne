"""Hyperliquid paper-trade strategy.

Phase 1: fire the Continuation signal using Binance BTC+ETH ticks; simulate entry/exit
against Hyperliquid BTC mid-price. Every fired trade is appended to a JSONL trade tape
and reflected in per-bucket state. No real orders, no wallet.

Invoke:
    python -m forgeone.strategies.hyperliquid_paper [--duration SECONDS]

The Phase 1 14-day collection clock starts once this service reports WS-connected for
60 continuous minutes and has logged at least one period boundary.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
import uuid
from pathlib import Path

import structlog

from forgeone.buffers.rolling import RollingPriceBuffer
from forgeone.config import get_settings
from forgeone.feeds.binance import BinanceFeed
from forgeone.feeds.hyperliquid import FundingPoller, HyperliquidFeed
from forgeone.logging import configure as configure_logging
from forgeone.logging import get_logger
from forgeone.risk.bucket_controller import BucketRiskController
from forgeone.signals import continuation as c
from forgeone.state.bucket import BucketState, BucketStateStore
from forgeone.state.trade_tape import PaperTrade, TradeTape
from forgeone.strategies.pnl import OpenPosition, apply_slippage, compute_pnl, should_exit


class HyperliquidPaperStrategy:
    """The paper strategy orchestrator. Wires feeds + state + risk + signal + tape."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.logger: structlog.stdlib.BoundLogger = get_logger("hyperliquid_paper")
        self._stopping = False

        data_dir = Path(self.settings.forge_data_dir)
        tape_path = data_dir / "hyperliquid_paper_trades.jsonl"
        state_path = data_dir / "buckets" / f"{self.settings.hl_bucket_id}.json"

        self.btc_buf = RollingPriceBuffer()
        self.eth_buf = RollingPriceBuffer()
        self.hl_buf = RollingPriceBuffer()

        self.tape = TradeTape(tape_path)
        self.state_store = BucketStateStore(state_path)
        self.state = self.state_store.load(
            lambda: BucketState(
                bucket_id=self.settings.hl_bucket_id,
                bankroll_usd=self.settings.hl_starting_bankroll_usd,
                peak_value_usd=self.settings.hl_starting_bankroll_usd,
            )
        )
        self.risk = BucketRiskController(
            self.state,
            daily_loss_cap_frac=self.settings.hl_daily_loss_cap_pct,
        )

        self.binance = BinanceFeed(
            buffers={"BTC": self.btc_buf, "ETH": self.eth_buf},
            url=self.settings.forge_binance_ws_url,
        )
        self.hl = HyperliquidFeed(
            buffer=self.hl_buf,
            coin="BTC",
            ws_url=self.settings.hl_ws_url,
        )
        self.funding = FundingPoller(
            rest_url=self.settings.hl_rest_url,
            coin="BTC",
            interval_sec=60,
        )

        # Logged once per UTC day so the first-period-ever entry is visible in journal.
        self._first_period_logged: int | None = None

    # ------------------------------------------------------------------ loops

    async def run(self, duration_sec: float | None = None) -> int:
        deadline = time.time() + duration_sec if duration_sec is not None else None
        self.logger.info(
            "paper_strategy_starting",
            bucket=self.settings.hl_bucket_id,
            bankroll=self.state.bankroll_usd,
            open_trade=bool(self.state.open_trade),
            paper_only=self.settings.hl_paper_only,
        )

        tasks = [
            asyncio.create_task(self.binance.run(), name="binance_ws"),
            asyncio.create_task(self.hl.run(), name="hl_ws"),
            asyncio.create_task(self.funding.run(), name="hl_funding"),
            asyncio.create_task(self._strategy_loop(deadline), name="strategy_loop"),
        ]

        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            pass
        finally:
            self._stopping = True
            self.binance.stop()
            self.hl.stop()
            self.funding.stop()
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.state_store.save(self.state)
            self.logger.info("paper_strategy_stopped",
                             bankroll=self.state.bankroll_usd,
                             daily_pnl=self.state.daily_pnl_usd)
        return 0

    async def _strategy_loop(self, deadline: float | None) -> None:
        """1-second tick loop. Mechanical — no branches outside encoded rules."""
        while not self._stopping:
            try:
                await self._tick()
            except Exception as e:
                self.logger.error("strategy_tick_error", error=str(e), exc_info=True)
            if deadline is not None and time.time() >= deadline:
                self.logger.info("strategy_duration_reached")
                return
            await asyncio.sleep(1.0)

    async def _tick(self) -> None:
        now = time.time()
        period = c.period_ts(now)

        if self._first_period_logged is None:
            self._first_period_logged = period
            self.logger.info("first_period_observed", period=period,
                             btc_age_ms=self.binance.age_ms("BTC"),
                             eth_age_ms=self.binance.age_ms("ETH"),
                             hl_age_ms=self.hl.age_ms())

        if self.state.open_trade is not None:
            self._maybe_close(now)
        else:
            self._maybe_enter(now, period)

    # ------------------------------------------------------------------ entry

    def _maybe_enter(self, now: float, period: int) -> None:
        if not c.in_active_window(now):
            return

        decision = self.risk.can_enter(period_ts=period)
        if not decision.allowed:
            # log once per period to keep the journal terse
            if period != getattr(self, "_last_block_period", None):
                self._last_block_period = period
                self.logger.debug("entry_blocked", period=period, reason=decision.reason)
            return

        hl_mid = self.hl_buf.latest_price()
        if hl_mid is None:
            return  # HL feed not yet warm

        sig, reason = c.evaluate_with_reason(self.btc_buf.view(), self.eth_buf.view(), now)
        if sig is None:
            return

        direction_sign = 1 if sig.direction == "up" else -1
        entry_fill = apply_slippage(hl_mid, direction_sign, self.settings.slippage_frac)

        pos = OpenPosition(
            direction=sig.direction,
            entry_ts=now,
            entry_price=entry_fill,
            notional_usd=self.settings.hl_notional_usd,
            leverage=self.settings.hl_leverage,
            period_ts=sig.period_ts,
        )
        # State persistence uses a dict.
        trade_id = str(uuid.uuid4())
        self.state.open_trade = {
            "trade_id": trade_id,
            "strategy_mode": "continuation",
            "venue": "hyperliquid",
            "bucket": self.settings.hl_bucket_id,
            "period_ts": sig.period_ts,
            "entry_ts": now,
            "direction": sig.direction,
            "entry_price": entry_fill,
            "entry_mid": hl_mid,
            "notional_usd": pos.notional_usd,
            "leverage": pos.leverage,
            "profit_lock_activated": False,
            "profit_lock_stop_price": None,
            "signal": {
                "btc_move_pct": sig.btc_move_pct,
                "eth_move_pct": sig.eth_move_pct,
                "reversal_pct": sig.reversal_pct,
                "elapsed_in_period_sec": sig.elapsed_in_period_sec,
            },
        }
        self.risk.record_open(self.state.open_trade)
        self.state_store.save(self.state)
        self.logger.info(
            "CONT_PAPER_FIRE",
            trade_id=trade_id,
            period=sig.period_ts,
            elapsed=sig.elapsed_in_period_sec,
            direction=sig.direction,
            btc_move_pct=round(sig.btc_move_pct * 100, 3),
            eth_move_pct=round(sig.eth_move_pct * 100, 3),
            reversal_pct=round(sig.reversal_pct * 100, 3),
            entry_fill=round(entry_fill, 4),
            hl_mid=round(hl_mid, 4),
            notional_usd=pos.notional_usd,
        )

    # ------------------------------------------------------------------ exit

    def _maybe_close(self, now: float) -> None:
        ot = self.state.open_trade
        assert ot is not None  # caller guarantees

        hl_mid = self.hl_buf.latest_price()
        if hl_mid is None:
            return

        pos = OpenPosition(
            direction=ot["direction"],
            entry_ts=float(ot["entry_ts"]),
            entry_price=float(ot["entry_price"]),
            notional_usd=float(ot["notional_usd"]),
            leverage=float(ot["leverage"]),
            period_ts=int(ot["period_ts"]),
            profit_lock_activated=bool(ot.get("profit_lock_activated", False)),
            profit_lock_stop_price=ot.get("profit_lock_stop_price"),
        )

        reason = should_exit(pos, now, hl_mid)
        # Reflect any profit-lock activation back into state (so we don't lose it across restarts).
        ot["profit_lock_activated"] = pos.profit_lock_activated
        ot["profit_lock_stop_price"] = pos.profit_lock_stop_price

        if reason is None:
            # persist incremental update (lock arming) — cheap atomic write
            self.state_store.save(self.state)
            return

        # Close the trade.
        exit_fill, pnl = compute_pnl(
            pos=pos,
            exit_ts=now,
            exit_mid=hl_mid,
            slippage_frac=self.settings.slippage_frac,
            taker_fee_frac=self.settings.taker_fee_frac,
            funding_rate_per_hour=self.funding.latest,
        )

        trade = PaperTrade(
            trade_id=str(ot["trade_id"]),
            strategy_mode="continuation",
            venue="hyperliquid",
            bucket=self.settings.hl_bucket_id,
            period_ts=int(ot["period_ts"]),
            entry_ts=float(ot["entry_ts"]),
            exit_ts=now,
            direction=str(ot["direction"]),
            entry_price=float(ot["entry_price"]),
            exit_price=float(exit_fill),
            notional_usd=float(ot["notional_usd"]),
            leverage=float(ot["leverage"]),
            fees_usd=pnl.fees_usd,
            funding_usd=pnl.funding_usd,
            slippage_usd=pnl.slippage_usd,
            gross_pnl_usd=pnl.gross_pnl_usd,
            net_pnl_usd=pnl.net_pnl_usd,
            exit_reason=reason,
            signal=ot.get("signal", {}),
        )
        self.tape.append(trade)
        won = pnl.net_pnl_usd > 0
        self.risk.record_close(pnl_usd=pnl.net_pnl_usd, won=won)
        self.state_store.save(self.state)
        self.logger.info(
            "CONT_PAPER_CLOSE",
            trade_id=trade.trade_id,
            period=trade.period_ts,
            direction=trade.direction,
            exit_reason=reason,
            entry_price=round(trade.entry_price, 4),
            exit_price=round(trade.exit_price, 4),
            gross_pnl=round(pnl.gross_pnl_usd, 4),
            fees=round(pnl.fees_usd, 4),
            funding=round(pnl.funding_usd, 4),
            net_pnl=round(pnl.net_pnl_usd, 4),
            won=won,
            bankroll=round(self.state.bankroll_usd, 2),
            daily_pnl=round(self.state.daily_pnl_usd, 2),
        )


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--duration", type=float, default=None,
                   help="Stop after N seconds (local dry-run). Omit to run indefinitely.")
    p.add_argument("--log-level", default=None)
    args = p.parse_args(argv)

    settings = get_settings()
    configure_logging(
        level=args.log_level or settings.forge_log_level,
        bucket=settings.hl_bucket_id,
        strategy_mode="continuation",
    )

    strategy = HyperliquidPaperStrategy()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal(_signum, _frame):  # type: ignore[no-untyped-def]
        strategy._stopping = True  # noqa: SLF001

    # Windows SIGTERM doesn't exist; handle whatever the platform gives us.
    for sig_name in ("SIGTERM", "SIGINT"):
        sig_const = getattr(signal, sig_name, None)
        if sig_const is not None:
            try:
                signal.signal(sig_const, _handle_signal)
            except (OSError, ValueError):
                pass

    try:
        return loop.run_until_complete(strategy.run(args.duration))
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
