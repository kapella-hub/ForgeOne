"""Parity backtest for the extracted Continuation signal.

Fetches 72h of Binance 1m klines (BTC + ETH) fresh from the public REST API, replays
period-by-period, and calls `forgeone.signals.continuation.evaluate` at each 60-second
evaluation point within [240, 600] seconds of each 15-min period. A fire resolves as a
win if BTC closes in the signal direction at the period boundary.

Historical baseline (source: polymarket-bot/run_sniper.py header, 72h ending 2026-04-17):
    36 fires, 33 wins, 3 losses, 91.7% WR, ~12 fires/day, $0.25 EV/trade @ $0.65 entry.

Because we fetch a fresh 72h window (different data than the baseline), we do NOT assert
exact reproduction of 33/3/36 — instead we assert the Phase 1 go/no-go criteria:
    - fires >= 20 (enough fires to be statistically meaningful)
    - win_rate >= 0.75 (briefing's Phase 1 gate)

If those hold, the signal's edge still exists on current market conditions. If they fail,
paper-trading is likely to fail the Phase 1 gate too — stop and re-plan.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

from forgeone.signals import continuation as c


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SYMBOLS = ("BTCUSDT", "ETHUSDT")
HOURS = 72

# Try these in order. api.binance.com is unavailable from US IPs (HTTP 451); fallbacks
# are api.binance.us (BTCUSDT and ETHUSDT supported) and Bybit (drop-in kline shape is
# slightly different, so we only fall back to Binance-shaped endpoints). Override via
# FORGE_KLINES_BASE env or --base-url.
DEFAULT_BASE_URLS = [
    "https://api.binance.com",   # Works from Malaysia VPS and non-US IPs
    "https://api.binance.us",    # Works from US IPs
]

CACHE_DIR = Path("data/klines_cache")
MIN_FIRES_GATE = 20
MIN_WR_GATE = 0.75

HIST_BASELINE = {"fires": 36, "wins": 33, "losses": 3, "win_rate": 33 / 36}


# ---------------------------------------------------------------------------
# Binance REST fetch (no auth, free rate limits suffice for a 72h pull)
# ---------------------------------------------------------------------------


def _fetch_klines_one_base(base_url: str, symbol: str,
                           start_ms: int, end_ms: int) -> list[list]:
    """Paginate through `{base_url}/api/v3/klines` until we cover [start_ms, end_ms]."""
    bars: list[list] = []
    cur = start_ms
    while cur < end_ms:
        url = (
            f"{base_url}/api/v3/klines?symbol={symbol}&interval=1m"
            f"&startTime={cur}&endTime={end_ms}&limit=1000"
        )
        with urllib.request.urlopen(url, timeout=15) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        bars.extend(batch)
        last_open_ms = batch[-1][0]
        cur = last_open_ms + 60_000
        if len(batch) < 1000:
            break
    return bars


def fetch_klines(symbol: str, start_ms: int, end_ms: int,
                 base_urls: list[str]) -> list[list]:
    """Try each base URL in order until one succeeds."""
    last_error: Exception | None = None
    for base in base_urls:
        try:
            bars = _fetch_klines_one_base(base, symbol, start_ms, end_ms)
            if bars:
                print(f"  fetched {len(bars)} {symbol} bars from {base}")
                return bars
        except Exception as e:
            last_error = e
            print(f"  {base} failed for {symbol}: {type(e).__name__}: {e}")
            continue
    raise RuntimeError(
        f"All kline sources failed for {symbol}. Last error: {last_error}. "
        f"If running from a US IP, Binance.com returns HTTP 451; binance.us should work. "
        f"On the Malaysia VPS, binance.com should work. Tried: {base_urls}"
    )


def load_or_fetch(end_ms: int | None, cache_dir: Path,
                  base_urls: list[str]) -> dict[str, list[list]]:
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    start_ms = end_ms - HOURS * 3600 * 1000

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = f"klines_{HOURS}h_end_{end_ms // 60_000 * 60_000}.json"
    cache_path = cache_dir / cache_key

    if cache_path.exists():
        print(f"Using cached klines: {cache_path}")
        return json.loads(cache_path.read_text())

    print(f"Fetching {HOURS}h klines... (end_ms={end_ms})")
    data: dict[str, list[list]] = {}
    for sym in SYMBOLS:
        data[sym] = fetch_klines(sym, start_ms, end_ms, base_urls)
    cache_path.write_text(json.dumps(data))
    total = sum(len(v) for v in data.values())
    print(f"Cached {total} bars to {cache_path}")
    return data


# ---------------------------------------------------------------------------
# Price-series construction & adapters for continuation.py
# ---------------------------------------------------------------------------


def build_deque(bars: list[list]) -> deque[tuple[float, float]]:
    """Construct a (open_ts_seconds, close_price) deque matching the shape expected
    by continuation.py helpers. Klines are already sorted by open_ts ascending."""
    d: deque = deque()
    for b in bars:
        open_ts = int(b[0] // 1000)
        close_price = float(b[4])
        d.append((float(open_ts), close_price))
    return d


def close_price_at(bars_by_open_ts: dict[int, float], ts: int,
                   fallback_sec: int = 120) -> float | None:
    """Nearest kline close at-or-before ts, within `fallback_sec` seconds.

    Matches the source backtest's `price_at` semantics for the purpose of
    period-boundary win/loss resolution (*not* signal evaluation)."""
    for offset in range(fallback_sec + 1):
        p = bars_by_open_ts.get(ts - offset)
        if p is not None:
            return p
    return None


# ---------------------------------------------------------------------------
# Simulation — runs the extracted `continuation.evaluate()` at period-aligned
# evaluation points.
# ---------------------------------------------------------------------------


def simulate(data: dict[str, list[list]]) -> dict:
    btc_full = build_deque(data["BTCUSDT"])
    eth_full = build_deque(data["ETHUSDT"])
    btc_by_ts = {int(b[0] // 1000): float(b[4]) for b in data["BTCUSDT"]}

    if not btc_full or not eth_full:
        return {"error": "no klines returned"}

    start_ts = int(btc_full[0][0])
    end_ts = int(btc_full[-1][0])

    # First full 15-min period boundary strictly after start, last one strictly before end.
    first_period = ((start_ts + c.PERIOD_SEC - 1) // c.PERIOD_SEC) * c.PERIOD_SEC
    last_period = (end_ts // c.PERIOD_SEC) * c.PERIOD_SEC

    stats: dict = {
        "periods_evaluated": 0,
        "fires": 0,
        "up_fires": 0,
        "down_fires": 0,
        "wins": 0,
        "losses": 0,
        "skip_counts": {},
        "fire_log": [],
    }

    # Incremental deque: we stream past ticks into view_btc/view_eth as time advances.
    # This matches live-run semantics where the deque contains only *past* data relative
    # to now_ts. Populating up front would let reversal_counter_move see future ticks
    # because its ts>=cutoff filter has no upper bound.
    btc_list = list(btc_full)
    eth_list = list(eth_full)
    view_btc: deque = deque()
    view_eth: deque = deque()
    btc_idx = 0
    eth_idx = 0

    def advance_views(now_ts: float) -> None:
        """Append all btc_list/eth_list ticks with ts <= now_ts into the views."""
        nonlocal btc_idx, eth_idx
        while btc_idx < len(btc_list) and btc_list[btc_idx][0] <= now_ts:
            view_btc.append(btc_list[btc_idx])
            btc_idx += 1
        while eth_idx < len(eth_list) and eth_list[eth_idx][0] <= now_ts:
            view_eth.append(eth_list[eth_idx])
            eth_idx += 1

    for period_ts in range(first_period, last_period, c.PERIOD_SEC):
        period_end_ts = period_ts + c.PERIOD_SEC
        period_start_price = close_price_at(btc_by_ts, period_ts)
        period_end_price = close_price_at(btc_by_ts, period_end_ts)
        if period_start_price is None or period_end_price is None:
            continue
        stats["periods_evaluated"] += 1

        fired = False
        # Source backtest evaluates at 60s increments within [240, 600]. Keep identical.
        for elapsed in range(c.ACTIVE_START_SEC, c.ACTIVE_END_SEC + 1, 60):
            if fired:
                break
            now_ts = float(period_ts + elapsed)
            advance_views(now_ts)

            sig, reason = c.evaluate_with_reason(view_btc, view_eth, now_ts)
            if sig is None:
                stats["skip_counts"][reason] = stats["skip_counts"].get(reason, 0) + 1
                continue

            stats["fires"] += 1
            fired = True
            if sig.direction == "up":
                stats["up_fires"] += 1
                won = period_end_price > period_start_price
            else:
                stats["down_fires"] += 1
                won = period_end_price < period_start_price

            stats["wins" if won else "losses"] += 1
            stats["fire_log"].append({
                "period_ts": period_ts,
                "elapsed": elapsed,
                "direction": sig.direction,
                "btc_move_pct": round(sig.btc_move_pct * 100, 3),
                "eth_move_pct": round(sig.eth_move_pct * 100, 3),
                "reversal_pct": round(sig.reversal_pct * 100, 3),
                "period_start_price": period_start_price,
                "period_end_price": period_end_price,
                "period_move_pct": round(
                    (period_end_price - period_start_price) / period_start_price * 100, 3
                ),
                "won": won,
            })

    return stats


# ---------------------------------------------------------------------------
# Reporting + gate
# ---------------------------------------------------------------------------


def report(stats: dict, show_fires: int = 10) -> tuple[bool, str]:
    if "error" in stats:
        print(f"ERROR: {stats['error']}")
        return False, "error"

    fires = stats["fires"]
    wins = stats["wins"]
    losses = stats["losses"]
    wr = (wins / fires) if fires else 0.0
    fires_per_day = fires / (HOURS / 24.0)

    print("=" * 68)
    print(" CONTINUATION SIGNAL PARITY BACKTEST — FORGEONE PORT")
    print("=" * 68)
    print(f"  Periods evaluated:   {stats['periods_evaluated']}")
    print(f"  Fires:               {fires} ({stats['up_fires']} up / {stats['down_fires']} down)")
    print(f"  Fires per day:       {fires_per_day:.1f}")
    print(f"  Wins:                {wins}")
    print(f"  Losses:              {losses}")
    print(f"  Win rate:            {wr * 100:.1f}%")
    print()
    print("  Skip reasons:")
    for k in sorted(stats["skip_counts"]):
        print(f"    {k:30s} {stats['skip_counts'][k]}")
    print()
    print(f"  Historical baseline (72h ending 2026-04-17): "
          f"{HIST_BASELINE['fires']} fires, {HIST_BASELINE['wins']}W / "
          f"{HIST_BASELINE['losses']}L = {HIST_BASELINE['win_rate'] * 100:.1f}%")
    print()
    if show_fires and stats["fire_log"]:
        print(f"  First {min(show_fires, len(stats['fire_log']))} fires:")
        for f in stats["fire_log"][:show_fires]:
            print(f"    period={f['period_ts']} elapsed={f['elapsed']}s "
                  f"dir={f['direction']} btc={f['btc_move_pct']}% eth={f['eth_move_pct']}% "
                  f"-&gt; {'WIN' if f['won'] else 'LOSS'} "
                  f"(period_move={f['period_move_pct']}%)")
        print()

    gate_fires_ok = fires >= MIN_FIRES_GATE
    gate_wr_ok = wr >= MIN_WR_GATE
    passed = gate_fires_ok and gate_wr_ok

    print("  Phase 1 go/no-go:")
    print(f"    fires >= {MIN_FIRES_GATE:2d}:         {fires:2d}    "
          f"{'PASS' if gate_fires_ok else 'FAIL'}")
    print(f"    win_rate >= {MIN_WR_GATE:.2f}:     {wr:.2f}  "
          f"{'PASS' if gate_wr_ok else 'FAIL'}")
    print(f"    VERDICT:              {'PASS — signal edge is live' if passed else 'FAIL'}")
    print("=" * 68)

    verdict = "PASS" if passed else "FAIL"
    return passed, verdict


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    # On Windows consoles (cp1252), print may fail on non-ASCII; force UTF-8 stdout.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--end-ms", type=int, default=None,
                   help="Unix ms timestamp to end the 72h window at. Defaults to now.")
    p.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    p.add_argument("--no-cache", action="store_true",
                   help="Ignore cached klines and re-fetch.")
    p.add_argument("--show-fires", type=int, default=10,
                   help="Print N first fires (default 10, 0 to disable).")
    p.add_argument("--base-url", action="append", default=None,
                   help="Kline REST base URL(s). Repeat to provide fallbacks. "
                        "Defaults to binance.com then binance.us.")
    args = p.parse_args(argv)

    if args.no_cache and args.cache_dir.exists():
        for f in args.cache_dir.glob("klines_*h_end_*.json"):
            f.unlink()

    base_urls = args.base_url or DEFAULT_BASE_URLS
    data = load_or_fetch(args.end_ms, args.cache_dir, base_urls)
    stats = simulate(data)
    passed, _ = report(stats, show_fires=args.show_fires)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
