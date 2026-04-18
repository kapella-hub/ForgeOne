# ForgeOne VPS Deployment — Phase 1 (Malaysia VPS `72.62.78.141`)

This installs the Hyperliquid paper trader alongside the existing polymarket-bot at
`/opt/polymarket-bot`. It does **not** touch the existing bot.

## Pre-flight

- [ ] You have SSH access to `root@72.62.78.141`
- [ ] `/opt/polymarket-bot/` is healthy (check its own status endpoint)
- [ ] `/opt` has ≥200 MB free (ForgeOne venv + 60 days of JSONL)

## 1 — Clone the repo

```bash
ssh root@72.62.78.141
cd /opt
git clone https://github.com/kapella-hub/ForgeOne.git forgeone
cd forgeone
```

## 2 — Create venv + install

```bash
python3.10 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -e .[dev]
```

## 3 — Env + state

```bash
install -m 0600 deploy/.env.vps .env
install -d -m 0755 data data/buckets data/daily_reports data/klines_cache
```

Edit `.env` if you need to change bankroll, leverage, or other defaults.

## 4 — Verify locally before daemonizing

```bash
./.venv/bin/python -m pytest tests/ -v             # must be all green
./.venv/bin/python -m forgeone.scripts.backtest_continuation_port  # must exit 0
```

Run a short dry-run to confirm feeds work from the VPS (binance.com not 451 here):

```bash
./.venv/bin/python -m forgeone.strategies.hyperliquid_paper --duration 120
```

Look for both `binance_ws_connected` and `hl_ws_connected` lines, no reconnect storms.

## 5 — Install systemd service

```bash
install -m 0644 deploy/forgeone-hyperliquid-paper.service \
    /etc/systemd/system/forgeone-hyperliquid-paper.service
systemctl daemon-reload
systemctl enable --now forgeone-hyperliquid-paper
systemctl status forgeone-hyperliquid-paper
journalctl -u forgeone-hyperliquid-paper -f            # watch for ~5 min
```

Expect:
- `paper_strategy_starting`
- `binance_ws_connected`
- `hl_ws_connected`
- `first_period_observed` within ~1s of start
- No repeated `*_ws_reconnecting` events

## 6 — Logrotate

```bash
install -m 0644 deploy/forgeone.logrotate /etc/logrotate.d/forgeone
logrotate --debug /etc/logrotate.d/forgeone        # dry-run
```

## 7 — Daily summary cron (runs 00:15 UTC)

```bash
install -m 0644 deploy/cron.daily /etc/cron.d/forgeone-daily
systemctl restart cron
```

## 8 — Start the 14-day clock

Once the service has been `active (running)` continuously for 60 minutes AND
`first_period_observed` has been logged AND `binance_ws_connected` + `hl_ws_connected`
are both stable, record the UTC start time somewhere (e.g., a relay post) and begin
counting. The Phase 1 gate requires **14 calendar days** of continuous paper data.

## Daily checks

- `journalctl -u forgeone-hyperliquid-paper --since "1 hour ago"` — look for
  unexpected warnings or reconnect storms
- `cat /opt/forgeone/data/daily_reports/$(date -u -d yesterday +%F).txt` — yesterday's
  summary
- `wc -l /opt/forgeone/data/hyperliquid_paper_trades.jsonl` — running fire count

## Phase 1 go/no-go criteria (re-check at day 14)

| Check | Requirement |
|---|---|
| Continuous collection | ≥14 calendar days |
| Fires | ≥20 |
| Paper WR | ≥75% |
| Median trade P&L | ≥ +$15 at $10K notional |
| Execution bugs | 0 unresolved |
| Parity backtest | still green on most recent 72h |

All checks must pass before writing Phase 2 live-execution code.

**Abort trigger:** any 7-day rolling WR drops below 60% → pause, re-plan.

## Rollback

The paper service never touches real capital, so rollback is just stopping + removing:

```bash
systemctl disable --now forgeone-hyperliquid-paper
rm /etc/systemd/system/forgeone-hyperliquid-paper.service
rm /etc/logrotate.d/forgeone
rm /etc/cron.d/forgeone-daily
# keep /opt/forgeone/ for log analysis
```

The polymarket-bot at `/opt/polymarket-bot` is unaffected.
