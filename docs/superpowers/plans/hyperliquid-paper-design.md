# Phase 1 — Hyperliquid Paper Trade Infrastructure: Design Doc

**Status:** APPROVED 2026-04-17 by user. Implementation authorized.
**Owner:** ForgeOne
**Scope:** Phase 1 only (paper trade Hyperliquid BTC perp using the validated Continuation signal). Phases 2–4 planned separately post-gate.

## 1. Goal

Stand up a 14-day paper-trading rig that fires the validated "Continuation Mode" signal against Hyperliquid BTC perp price data, with realistic fee/funding/slippage accounting, to answer one question: **does the signal hold off Polymarket?** Exit criteria: WR ≥ 75% over ≥ 20 fires, median trade net P&L ≥ +$15 at $10K notional, zero execution bugs.

## 2. File layout (ForgeOne, fresh)

```
src/signals/continuation.py            # pure extracted signal
src/feeds/binance.py                   # ported from polymarket-bot ws_feeds.py (BTC+ETH ticks)
src/feeds/hyperliquid.py               # new WS + REST (read-only, Phase 1)
src/buffers/rolling.py                 # (ts, price) deque + prune
src/state/bucket.py                    # per-bucket JSON state (atomic write)
src/state/trade_tape.py                # JSONL append writer
src/risk/bucket_controller.py          # daily loss cap, one-open-trade rule
src/risk/circuit_breaker.py            # generalized from run_contrarian.py
src/strategies/hyperliquid_paper.py    # the paper script + CLI entry
src/config.py                          # pydantic BaseSettings, HL_ env prefix
src/logging.py                         # structlog JSON
scripts/backtest_continuation_port.py  # re-runs 72h backtest against extracted module
scripts/replay_paper_day.py            # daily-summary roll-up from JSONL
tests/                                 # parity, exit-rules, WS parsing, risk
data/hyperliquid_paper_trades.jsonl    # append-only trade tape
data/hyperliquid_paper_daily.jsonl     # one row / UTC day
data/buckets/hyperliquid_paper.json    # live bucket state
```

## 3. Signal extraction — zero-drift procedure

**Source** (verified, do not re-derive): `polymarket-bot/run_sniper.py`
- `_get_price_at` (lines 87–94), `_compute_move_pct` (lines 97–104), `_reversal_counter_move` (lines 107–120).
- Thresholds (lines 73–79): `ACTIVE_START=240, ACTIVE_END=600, LOOKBACK=300, BTC_MIN=0.003, ETH_MIN=0.0025, REV_WIN=60, REV_BLOCK=0.0012`.
- Period boundary: `(now_ts // 900) * 900` (line 291).

**`src/signals/continuation.py` public API**:
```python
def get_price_at(history: deque, target_ts: float) -> Optional[float]
def compute_move_pct(history: deque, now_ts: float, lookback_sec: int) -> Optional[float]
def reversal_counter_move(history: deque, now_ts: float, window_sec: int, direction: str) -> float
def period_ts(now_ts: float) -> int           # (now_ts // 900) * 900
def evaluate(btc_hist, eth_hist, now_ts) -> Optional[ContinuationSignal]
```
`ContinuationSignal` = dataclass `(direction, btc_move_pct, eth_move_pct, reversal_pct, period_ts, elapsed_in_period_sec, now_ts)`.

`evaluate()` checks: (a) elapsed in [240, 600], (b) |btc_move| ≥ 0.003, (c) |eth_move| ≥ 0.0025, (d) same direction, (e) reversal < 0.0012. Returns signal or None with skip reason in logs. Does **not** check entry price cap (that's execution's job, not signal).

**Parity proof — MANDATORY before paper code runs** (ForgeOne is standalone; no polymarket-bot import anywhere):
1. `tests/fixtures/signal_parity.json` — canned `(input_history, now_ts, expected_output)` cases produced once by hand-running the original `run_sniper.py` helpers during the port (committed to ForgeOne; polymarket-bot is never imported at test time).
2. `tests/test_signals_continuation.py` — asserts the extracted module reproduces every fixture byte-identically. Boundary cases included (empty deque, single-tick history, exact threshold hits, both directions).
3. `scripts/backtest_continuation_port.py` — fetches the same 72h window used for the original backtest from Binance's public `/api/v3/klines` REST endpoint (BTC + ETH, 1m klines, no auth), replays at 1s granularity, calls `evaluate()`, and asserts **exactly 33W / 3L / 36 fires**. Any deviation → fail loudly.
4. `data/klines_cache/` — downloaded klines cached in ForgeOne so re-runs are deterministic and offline-capable.

## 4. Paper trade tape schema (`data/hyperliquid_paper_trades.jsonl`)

One line per fire:
```json
{"trade_id":"uuid","strategy_mode":"continuation","venue":"hyperliquid","bucket":"hyperliquid_paper",
 "period_ts":1760630400,"entry_ts":1760630640,"exit_ts":1760631300,"direction":"up",
 "entry_price":68420.5,"exit_price":68573.8,"notional_usd":10000,"leverage":3,
 "fees_usd":5.0,"funding_usd":0.0,"slippage_usd":3.0,
 "gross_pnl_usd":22.40,"net_pnl_usd":14.40,"exit_reason":"time",
 "signal":{"btc_move_pct":0.0037,"eth_move_pct":0.0031,"reversal_pct":0.0008,"elapsed_in_period_sec":320}}
```
Write-atomic via tmp-file + rename append-append (actually: JSONL is append-only, so fsync-and-flush per line; tolerated risk = losing the last in-flight line on crash).

## 5. Daily summary schema (`data/hyperliquid_paper_daily.jsonl`)

One line per UTC day:
`{date, bucket, fires, wins, losses, win_rate, gross_pnl, net_pnl, max_drawdown_usd, rolling_7d_wr, rolling_30d_wr, funding_total, fees_total, avg_trade_pnl, median_trade_pnl, exit_reason_counts}`.

Computed by `scripts/replay_paper_day.py`, run via cron 00:15 UTC. One-line headline also posted to NexusRelay bulletin (tags `["hyperliquid-paper", "daily"]`).

## 6. Exit-rule + P&L accounting (briefing verbatim)

Checked in order each tick after an open trade:
1. **Reversal stop:** BTC reverses ≥ 0.15% against entry within 60s of fire → close at mid.
2. **Profit lock:** BTC extends ≥ 0.50% in signal direction → tighten stop to `entry ± 0.05%`; next tick check stop hit.
3. **Time stop:** `period_end + 60s` (`period_end = period_ts(entry_ts) + 900`) → close at mid.

**Fees/slippage/funding:**
- Taker both sides: `fee = 0.00025 * (entry_notional + exit_notional)`.
- Slippage: `fill = mid * (1 + 0.00015 * sign)` in, mirror out.
- Funding: if trade spans 8h boundary (UTC 00/08/16), apply `funding_rate * notional` signed by side. Guard: if `funding > 0.001` against our direction, log warning (Phase 2 will *act* on this; Phase 1 just records).

**Paper notional:** $10,000 at 3× leverage. Rationale: we want statistical power of realistic fill sizing, not Phase 2's conservative $500 collateral cap. Phase 2 live code will override with its own cap — explicitly documented so no one copy-pastes $10K.

## 7. State, risk, circuit breaker

- `data/buckets/hyperliquid_paper.json` holds `{bankroll, daily_pnl, peak_value, consecutive_losses, last_reset_utc_day, open_trade}`; atomic write.
- `bucket_controller.can_enter()` enforces: one-open-trade rule, daily loss cap = −20% of bucket bankroll → pause 24h.
- `circuit_breaker` generalized from `run_contrarian.py:113-338`: `loss_threshold=3, cooldown_periods=4` (matches existing snipers).
- In paper, risk blocks the *fake* entry the same way it would block a real one.

## 8. Deployment

- VPS `72.62.78.141` → `/opt/forgeone` (does not touch `/opt/polymarket-bot`).
- `systemd` unit `forgeone-hyperliquid-paper.service` + `logrotate` for JSONL.
- Runbook: `git pull && pip install -e . && pytest tests/ && systemctl restart forgeone-hyperliquid-paper && journalctl -u ... -f` for 5 min of sanity.

## 9. Decisions (user-confirmed 2026-04-17)

**Overarching rule:** ForgeOne is standalone. It never imports from, installs, or PR's into `polymarket-bot`. The only thing that crosses the boundary is **ideas**, copied by hand during the port.

1. **Klines source:** fetch the 72h window fresh from Binance `/api/v3/klines` public REST (auth-less). Cache to `data/klines_cache/` for deterministic re-runs.
2. **Parity test mechanism:** canned fixtures in `tests/fixtures/signal_parity.json`, produced once by hand-running the originals during port; no runtime dep on polymarket-bot.
3. **Paper notional:** $10,000 at 3× leverage, explicitly separate from Phase 2's $500 collateral cap.
4. **14-day clock:** starts when systemd reports `active (running)` + WS connected + first period tick observed, continuously for 60 min.
5. **Extraction path:** extract directly into ForgeOne only; leave polymarket-bot frozen. Any future signal tweaks that we want mirrored to polymarket-bot become a manual port back.
6. **Hyperliquid wallet:** deferred to Phase 2. Phase 1 is read-only WS + public REST.
7. **Reporting:** daily one-line headline posted to NexusRelay bulletin (tags `["hyperliquid-paper","daily"]`). No Slack/email in Phase 1.

## 10. Verification plan (end-to-end, post-implementation)

1. `pytest tests/ -v` green.
2. `python scripts/backtest_continuation_port.py` exits 0 (exact 33W/3L/36-fires match).
3. 1h local dry run — WS connects, buffer fills, at least one period boundary observed, no exceptions.
4. VPS deploy + 6h soak → `scripts/replay_paper_day.py --date $(date -u +%F)` yields non-empty report, zero error rows.
5. Daily relay bulletin post visible via `relay_read` from another session.

---
**Approved.** Implementation proceeds in this order: scaffold → extract signal → produce fixtures → parity tests → backtest parity → feeds/buffers/state/risk → paper script + exit-rule tests → local 1h dry run → VPS deploy → 14-day clock.
