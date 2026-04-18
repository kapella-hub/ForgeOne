"""Daily summary for the Hyperliquid paper tape. Run via cron 00:15 UTC on the VPS.

Reads `data/hyperliquid_paper_trades.jsonl` (or custom path), filters to the given UTC
day, computes metrics, and writes:
  - A human-readable summary to stdout and (optionally) to data/daily_reports/<date>.txt
  - A one-line JSON headline to data/hyperliquid_paper_daily.jsonl for dashboard ingest
  - An optional NexusRelay bulletin post (tags ["hyperliquid-paper","daily"])
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import orjson

from forgeone.config import get_settings


def _day_bounds(date: str) -> tuple[float, float]:
    """Return (start_ts, end_ts) for the UTC day YYYY-MM-DD."""
    d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    return (d.timestamp(), (d + timedelta(days=1)).timestamp())


def _load_trades(tape_path: Path) -> list[dict]:
    if not tape_path.exists():
        return []
    rows = []
    with tape_path.open("rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(orjson.loads(raw))
            except Exception:
                continue
    return rows


def _running_max_drawdown(trades: list[dict]) -> float:
    """Max drawdown in $ across the day's ordered trades (sorted by exit_ts)."""
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for t in trades:
        equity += float(t.get("net_pnl_usd", 0.0))
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def summarize(trades_all: list[dict], date: str) -> dict:
    start, end = _day_bounds(date)
    todays = sorted(
        (t for t in trades_all if start <= float(t.get("exit_ts", 0.0)) < end),
        key=lambda t: t["exit_ts"],
    )
    fires = len(todays)
    wins = sum(1 for t in todays if float(t.get("net_pnl_usd", 0.0)) > 0)
    losses = fires - wins
    net_pnls = [float(t.get("net_pnl_usd", 0.0)) for t in todays]
    gross_pnls = [float(t.get("gross_pnl_usd", 0.0)) for t in todays]
    fees = sum(float(t.get("fees_usd", 0.0)) for t in todays)
    funding = sum(float(t.get("funding_usd", 0.0)) for t in todays)

    wr = (wins / fires) if fires else 0.0
    avg = (sum(net_pnls) / fires) if fires else 0.0
    med = statistics.median(net_pnls) if net_pnls else 0.0
    exit_reasons = Counter(t.get("exit_reason", "unknown") for t in todays)

    # Rolling 7d / 30d — filter by exit_ts into [end-Nd, end).
    def _trailing_wr(days: int) -> tuple[float, int]:
        lb = end - days * 86_400
        subset = [t for t in trades_all if lb <= float(t.get("exit_ts", 0.0)) < end]
        n = len(subset)
        w = sum(1 for t in subset if float(t.get("net_pnl_usd", 0.0)) > 0)
        return ((w / n) if n else 0.0, n)

    wr7, n7 = _trailing_wr(7)
    wr30, n30 = _trailing_wr(30)

    return {
        "date": date,
        "bucket": todays[0]["bucket"] if todays else "hyperliquid_paper",
        "fires": fires,
        "wins": wins,
        "losses": losses,
        "win_rate": wr,
        "gross_pnl_usd": sum(gross_pnls),
        "net_pnl_usd": sum(net_pnls),
        "fees_usd": fees,
        "funding_usd": funding,
        "avg_trade_pnl_usd": avg,
        "median_trade_pnl_usd": med,
        "max_drawdown_usd": _running_max_drawdown(todays),
        "rolling_7d_wr": wr7,
        "rolling_7d_fires": n7,
        "rolling_30d_wr": wr30,
        "rolling_30d_fires": n30,
        "exit_reason_counts": dict(exit_reasons),
    }


def render(summary: dict) -> str:
    lines = [
        f"=== HYPERLIQUID PAPER — {summary['date']} ===",
        f"  fires:            {summary['fires']}  ({summary['wins']}W / {summary['losses']}L)",
        f"  win_rate:         {summary['win_rate']*100:.1f}%",
        f"  gross PNL:       ${summary['gross_pnl_usd']:+.2f}",
        f"  fees:            ${summary['fees_usd']:.2f}",
        f"  funding:         ${summary['funding_usd']:+.2f}",
        f"  NET PNL:         ${summary['net_pnl_usd']:+.2f}",
        f"  avg trade:       ${summary['avg_trade_pnl_usd']:+.2f}",
        f"  median trade:    ${summary['median_trade_pnl_usd']:+.2f}",
        f"  max drawdown:    ${summary['max_drawdown_usd']:.2f}",
        f"  rolling 7d WR:   {summary['rolling_7d_wr']*100:.1f}% "
        f"({summary['rolling_7d_fires']} fires)",
        f"  rolling 30d WR:  {summary['rolling_30d_wr']*100:.1f}% "
        f"({summary['rolling_30d_fires']} fires)",
        f"  exit reasons:    {summary['exit_reason_counts']}",
    ]
    return "\n".join(lines)


def _append_daily_jsonl(summary: dict, daily_path: Path) -> None:
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    with daily_path.open("ab") as f:
        f.write(orjson.dumps(summary) + b"\n")


def _post_to_relay(summary: dict, relay_url: str) -> bool:
    headline = (
        f"{summary['date']} hyperliquid_paper: "
        f"{summary['fires']} fires, "
        f"{summary['wins']}W/{summary['losses']}L "
        f"({summary['win_rate']*100:.1f}% WR), "
        f"net ${summary['net_pnl_usd']:+.2f}, "
        f"dd ${summary['max_drawdown_usd']:.2f}"
    )
    body = {"content": headline, "tags": ["hyperliquid-paper", "daily"]}
    try:
        r = httpx.post(f"{relay_url}/relay/post", json=body, timeout=5.0)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  relay post failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", default=None,
                   help="UTC date YYYY-MM-DD. Defaults to yesterday "
                        "(so 00:15 UTC cron picks up the day that just ended).")
    p.add_argument("--tape", type=Path, default=None)
    p.add_argument("--daily-jsonl", type=Path, default=None)
    p.add_argument("--reports-dir", type=Path, default=None)
    p.add_argument("--post-relay", action="store_true",
                   help="POST a one-line headline to NexusRelay.")
    args = p.parse_args(argv)

    settings = get_settings()
    data_dir = Path(settings.forge_data_dir)
    tape_path = args.tape or (data_dir / "hyperliquid_paper_trades.jsonl")
    daily_path = args.daily_jsonl or (data_dir / "hyperliquid_paper_daily.jsonl")
    reports_dir = args.reports_dir or (data_dir / "daily_reports")

    date = args.date or (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")

    trades = _load_trades(tape_path)
    summary = summarize(trades, date)
    rendered = render(summary)
    print(rendered)

    _append_daily_jsonl(summary, daily_path)

    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"{date}.txt").write_text(rendered + "\n", encoding="utf-8")

    if args.post_relay and settings.forge_nexus_post_daily:
        ok = _post_to_relay(summary, settings.forge_nexus_relay_url)
        if not ok:
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
