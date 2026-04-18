"""JSONL trade-tape append writer. Schema locked by the design doc.

One JSON line per fired trade. Append-only. Each write is flushed + fsync'd before the
call returns so crash-loss is bounded to any in-flight line on the OS side.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import orjson


@dataclass(frozen=True)
class PaperTrade:
    """Fields match docs/superpowers/plans/hyperliquid-paper-design.md §4."""
    trade_id: str
    strategy_mode: str          # "continuation"
    venue: str                  # "hyperliquid"
    bucket: str                 # "hyperliquid_paper"
    period_ts: int
    entry_ts: float
    exit_ts: float
    direction: str              # "up" | "down"
    entry_price: float
    exit_price: float
    notional_usd: float
    leverage: float
    fees_usd: float
    funding_usd: float
    slippage_usd: float
    gross_pnl_usd: float
    net_pnl_usd: float
    exit_reason: str            # "reversal" | "profit_lock" | "time"
    signal: dict = field(default_factory=dict)

    def to_json_bytes(self) -> bytes:
        return orjson.dumps(asdict(self), option=orjson.OPT_SERIALIZE_NUMPY)


class TradeTape:
    """Append-only JSONL writer with per-line flush + fsync."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, trade: PaperTrade) -> None:
        line = trade.to_json_bytes() + b"\n"
        # Open/write/fsync/close per line — safe across crashes; fine for our volume.
        fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        with self.path.open("rb") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                out.append(orjson.loads(raw))
        return out
