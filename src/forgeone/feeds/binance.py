"""Binance WebSocket feed (BTC + ETH miniTicker). Pushes ticks into a rolling buffer.

Pattern ported from polymarket-bot/src/crypto_arb/ws_feeds.py:19-117 — same reconnect
backoff, same combined-stream URL shape. Scope reduced to BTC + ETH (the only symbols
the continuation signal consumes).
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
import structlog
import websockets

from forgeone.buffers.rolling import RollingPriceBuffer

logger = structlog.get_logger(__name__)

SYMBOL_MAP = {
    "btcusdt": "BTC",
    "ethusdt": "ETH",
}

OnTick = Callable[[str, float, float], Awaitable[None] | None]


def backoff_delay(attempts: int) -> int:
    """Exponential backoff capped at 30s (matches polymarket-bot ws_feeds.py:67)."""
    return min(30, 2 ** min(attempts, 5))


class BinanceFeed:
    def __init__(
        self,
        buffers: dict[str, RollingPriceBuffer],
        url: str = "wss://stream.binance.com:9443/stream",
        on_tick: OnTick | None = None,
    ) -> None:
        self._buffers = buffers
        self._url = url
        self._on_tick = on_tick
        self._last_update_ts: dict[str, float] = {}
        self._running = False
        self._connect_attempts = 0

    def latest_ts(self, asset: str) -> float | None:
        return self._last_update_ts.get(asset)

    def age_ms(self, asset: str) -> float:
        last = self._last_update_ts.get(asset, 0.0)
        if not last:
            return float("inf")
        return (time.time() - last) * 1000.0

    async def run(self) -> None:
        self._running = True
        streams = [f"{sym}@miniTicker" for sym in SYMBOL_MAP]
        combined = f"{self._url}?streams={'/'.join(streams)}"
        while self._running:
            try:
                await self._connect_and_stream(combined)
                self._connect_attempts = 0
            except Exception as e:
                if not self._running:
                    break
                self._connect_attempts += 1
                wait = backoff_delay(self._connect_attempts)
                logger.warning("binance_ws_reconnecting", error=str(e), wait=wait,
                               attempts=self._connect_attempts)
                await asyncio.sleep(wait)

    def stop(self) -> None:
        self._running = False

    async def _connect_and_stream(self, url: str) -> None:
        async with websockets.connect(url, ping_interval=20) as ws:
            self._connect_attempts = 0
            logger.info("binance_ws_connected", symbols=len(SYMBOL_MAP))
            async for msg in ws:
                if not self._running:
                    break
                try:
                    data = orjson.loads(msg)
                except Exception:
                    continue
                payload = data.get("data", data)
                await self._process_ticker(payload)

    async def _process_ticker(self, payload: dict[str, Any]) -> None:
        sym = str(payload.get("s", "")).lower()
        asset = SYMBOL_MAP.get(sym)
        if not asset:
            return
        try:
            price = float(payload.get("c", 0))
        except (TypeError, ValueError):
            return
        if price <= 0:
            return

        # Binance "E" is event time in ms; fall back to wall-clock.
        event_ms = payload.get("E")
        now_ts = float(event_ms) / 1000.0 if isinstance(event_ms, (int, float)) else time.time()

        buf = self._buffers.get(asset)
        if buf is not None:
            buf.append(now_ts, price)
        self._last_update_ts[asset] = now_ts

        if self._on_tick is not None:
            result = self._on_tick(asset, price, now_ts)
            if asyncio.iscoroutine(result):
                await result


__all__ = ["BinanceFeed", "SYMBOL_MAP", "backoff_delay"]
