"""Hyperliquid feed — WebSocket BBO + funding REST poll. Read-only (Phase 1).

Hyperliquid WS docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
  Subscribe shape: {"method":"subscribe","subscription":{"type":"bbo","coin":"BTC"}}
  Ingress for BBO: {"channel":"bbo","data":{"coin":"BTC","time":ms,"bbo":[[bid_px,...],[ask_px,...]]}}

REST funding: POST {HL_REST_URL}/info  body: {"type":"metaAndAssetCtxs"}
  Returns a tuple `[meta, contexts]` where contexts[i]["funding"] is the hourly funding
  as a string (e.g. "0.00001250" → 0.00125% per hour). Hyperliquid charges hourly funding;
  we deduct it on a per-trade basis proportional to open duration.

No order placement. No wallet. Phase 2 adds those.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import orjson
import structlog
import websockets

from forgeone.buffers.rolling import RollingPriceBuffer
from forgeone.feeds.binance import backoff_delay

logger = structlog.get_logger(__name__)

OnTick = Callable[[str, float, float], Awaitable[None] | None]


class HyperliquidFeed:
    """BBO-driven mid-price feed for a single coin. Defaults to BTC."""

    def __init__(
        self,
        buffer: RollingPriceBuffer,
        coin: str = "BTC",
        ws_url: str = "wss://api.hyperliquid.xyz/ws",
        on_tick: OnTick | None = None,
    ) -> None:
        self._buffer = buffer
        self._coin = coin
        self._ws_url = ws_url
        self._on_tick = on_tick
        self._latest_bid: float | None = None
        self._latest_ask: float | None = None
        self._latest_mid: float | None = None
        self._latest_ts: float | None = None
        self._running = False
        self._connect_attempts = 0

    @property
    def mid(self) -> float | None:
        return self._latest_mid

    @property
    def bid(self) -> float | None:
        return self._latest_bid

    @property
    def ask(self) -> float | None:
        return self._latest_ask

    @property
    def latest_ts(self) -> float | None:
        return self._latest_ts

    def age_ms(self) -> float:
        if self._latest_ts is None:
            return float("inf")
        return (time.time() - self._latest_ts) * 1000.0

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._connect_and_stream()
                self._connect_attempts = 0
            except Exception as e:
                if not self._running:
                    break
                self._connect_attempts += 1
                wait = backoff_delay(self._connect_attempts)
                logger.warning("hl_ws_reconnecting", error=str(e), wait=wait,
                               attempts=self._connect_attempts, coin=self._coin)
                await asyncio.sleep(wait)

    def stop(self) -> None:
        self._running = False

    async def _connect_and_stream(self) -> None:
        async with websockets.connect(self._ws_url, ping_interval=20) as ws:
            logger.info("hl_ws_connected", coin=self._coin)
            await ws.send(orjson.dumps({
                "method": "subscribe",
                "subscription": {"type": "bbo", "coin": self._coin},
            }).decode())
            async for msg in ws:
                if not self._running:
                    break
                try:
                    data = orjson.loads(msg)
                except Exception:
                    continue
                await self._process_message(data)

    async def _process_message(self, msg: dict[str, Any]) -> None:
        if msg.get("channel") != "bbo":
            return
        mid, ts = parse_bbo(msg, expected_coin=self._coin)
        if mid is None or ts is None:
            return
        self._latest_mid = mid
        self._latest_ts = ts
        self._buffer.append(ts, mid)
        if self._on_tick is not None:
            result = self._on_tick(self._coin, mid, ts)
            if asyncio.iscoroutine(result):
                await result


def _extract_level_px(level: Any) -> float | None:
    """HL levels can be dicts {"px":"77106.0","sz":"..","n":..} or legacy lists
    [px, sz, n]. Return the px as float, or None."""
    if isinstance(level, dict):
        px = level.get("px")
    elif isinstance(level, (list, tuple)) and level:
        px = level[0]
    else:
        return None
    try:
        return float(px)
    except (TypeError, ValueError):
        return None


def parse_bbo(msg: dict[str, Any], expected_coin: str | None = None
              ) -> tuple[float | None, float | None]:
    """Parse a Hyperliquid BBO message → (mid_price, ts_seconds).

    Returns (None, None) if the message is malformed or for a different coin.

    BBO payload observed from wss://api.hyperliquid.xyz/ws (2026-04-18):
        {"channel":"bbo",
         "data":{"coin":"BTC","time":1776483677652,
                 "bbo":[{"px":"77106.0","sz":"1.98294","n":24},
                        {"px":"77107.0","sz":"1.32045","n":15}]}}
    bbo[0] is the best bid, bbo[1] is the best ask.
    """
    data = msg.get("data") or {}
    coin = data.get("coin")
    if expected_coin is not None and coin != expected_coin:
        return None, None
    bbo = data.get("bbo") or []
    if len(bbo) < 2 or not bbo[0] or not bbo[1]:
        return None, None
    bid = _extract_level_px(bbo[0])
    ask = _extract_level_px(bbo[1])
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None, None
    time_ms = data.get("time")
    ts = (float(time_ms) / 1000.0) if isinstance(time_ms, (int, float)) else time.time()
    return (bid + ask) / 2.0, ts


# ---------------------------------------------------------------------------
# Funding REST poll
# ---------------------------------------------------------------------------


async def fetch_funding_rate(client: httpx.AsyncClient, rest_url: str, coin: str = "BTC"
                             ) -> float | None:
    """Hourly funding rate as a decimal (e.g. 0.0000125 for 0.00125% per hour)."""
    try:
        r = await client.post(f"{rest_url}/info", json={"type": "metaAndAssetCtxs"},
                              timeout=10.0)
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        logger.warning("hl_funding_fetch_error", error=str(e))
        return None
    if not isinstance(body, list) or len(body) < 2:
        return None
    meta, ctxs = body[0], body[1]
    universe = (meta or {}).get("universe") or []
    for i, u in enumerate(universe):
        if (u or {}).get("name") == coin and i < len(ctxs):
            try:
                return float(ctxs[i].get("funding", 0.0))
            except (TypeError, ValueError):
                return None
    return None


class FundingPoller:
    """Polls the Hyperliquid funding rate every `interval_sec`. Exposes `latest`."""

    def __init__(self, rest_url: str, coin: str = "BTC", interval_sec: int = 60) -> None:
        self._rest_url = rest_url
        self._coin = coin
        self._interval = interval_sec
        self._latest: float | None = None
        self._latest_ts: float | None = None
        self._running = False

    @property
    def latest(self) -> float | None:
        return self._latest

    @property
    def latest_ts(self) -> float | None:
        return self._latest_ts

    async def run(self) -> None:
        self._running = True
        async with httpx.AsyncClient() as client:
            while self._running:
                rate = await fetch_funding_rate(client, self._rest_url, self._coin)
                if rate is not None:
                    self._latest = rate
                    self._latest_ts = time.time()
                    logger.debug("hl_funding_updated", coin=self._coin, rate=rate)
                await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False


__all__ = ["HyperliquidFeed", "FundingPoller", "fetch_funding_rate", "parse_bbo"]
