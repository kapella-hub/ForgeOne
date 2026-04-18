# ForgeOne

Barbell crypto signal execution system.

- **Phase 1 (active):** paper-trade the validated "Continuation Mode" signal on Hyperliquid BTC perps, 14-day collection window on the Malaysia VPS.
- **Phase 2+:** live Hyperliquid execution at staged capital, then Polymarket market-making with continuation-driven toxicity filter.

Design + phase plan: [`docs/superpowers/plans/hyperliquid-paper-design.md`](docs/superpowers/plans/hyperliquid-paper-design.md).

## Quickstart (local dev)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux
pip install -e .[dev]
cp .env.example .env          # edit values
pytest tests/ -v
```

Parity-gate the signal extraction before anything else:

```bash
python -m forgeone.scripts.backtest_continuation_port
# must exit 0 and print "PASS: 33W / 3L / 36 fires — parity confirmed"
```

## Layout

```
src/forgeone/
  signals/continuation.py       # pure signal, extracted from external source
  feeds/{binance,hyperliquid}.py
  buffers/rolling.py
  state/{bucket,trade_tape}.py
  risk/{bucket_controller,circuit_breaker}.py
  strategies/hyperliquid_paper.py
  scripts/{backtest_continuation_port,replay_paper_day}.py
tests/
data/                           # runtime state, gitignored
```

## Hard rules

1. No real capital until Phase 1 gate passes (WR≥75%, ≥20 fires, 14 days, median P&L ≥ +$15 at $10K notional).
2. Max 5x leverage anywhere, ever.
3. Polymarket execution stays on the Malaysia VPS; Hyperliquid runs from wherever is fastest.
4. Independent risk per bucket — a blowup in one bucket must not touch another bucket's bankroll.
5. Mechanical execution only. No discretionary overrides. Every entry/exit rule encoded.
