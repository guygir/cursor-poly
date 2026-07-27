# Handoff: BTC 1H Up/Down + Kronos Research Workflow

Branch: `cursor/public-price-research-772c`

Audience: next agent with working Polymarket (Gamma/CLOB) network access.

## Goal

Research-only workflow (no wallet trading):

1. Slightly before / at the start of each BTC **1H** Up/Down window, predict UP or DOWN.
2. Prefer **Kronos-mini** on CPU once per window (not every second).
3. If predicted-side ask is near **0.50** (default max entry `0.52`), log a simulated entry.
4. After the window ends, resolve winner from settlement prices and score PnL.

Trading stays disabled until a new wallet + dry-run validation. Prior wallet was compromised.

## Already built on this branch

| Piece | Location | Status |
|-------|----------|--------|
| Public 5m bid/ask sampler + double-target analyzer | `src/polybot/research.py` | Done; used for earlier 5m research |
| 5m strategy findings | `docs/research-strategy-summary.md` | Done |
| Binance OHLCV client | `src/polybot/binance.py` | Done |
| Hourly market discovery (slug + Gamma event search) | `PolymarketClient.find_active_updown_markets`, `resolve_active_hourly_market` | Code ready; needs live Polymarket verification |
| Hourly monitor CLI | `polybot-monitor-hourly-research` | Done |
| Hourly score / resolve CLI | `polybot-analyze-hourly` | Done |
| Heuristic predictor (last closed Binance 1h candle) | `LastCandleMomentumPredictor` | Done; default fallback |
| Optional Kronos wrapper | `KronosPredictor` / `--predictor kronos\|auto` | Scaffolded; **not installed / not live-tested** |
| Unit tests (no Polymarket) | `tests/test_hourly_research.py` | Done |

### CLIs

```bash
# Sample current BTC 1H book + prediction into SQLite (research.sqlite3 by default)
.venv/bin/polybot-monitor-hourly-research \
  --asset btc --timeframe 1h \
  --interval-seconds 60 --duration-minutes 180 \
  --predictor auto \
  --max-entry-price 0.52

# Manual side override (skip predictor)
.venv/bin/polybot-monitor-hourly-research --prediction-side UP --max-entry-price 0.52

# Resolve finished windows from Polymarket + print score summary
.venv/bin/polybot-analyze-hourly --resolve
```

`--predictor auto` tries Kronos, then falls back to the Binance last-closed-candle heuristic.

## What the next agent still needs to do

### 1. Verify Polymarket 1H market resolution (blocked here)

On a machine that can reach `gamma-api.polymarket.com` without SSL/Cisco resets:

```bash
.venv/bin/polybot-resolve --updown btc:1h
.venv/bin/polybot-monitor-hourly-research --duration-minutes 5 --interval-seconds 30 --predictor heuristic
```

Confirm:

- Slug / human-readable hourly event is found (`find_active_updown_markets` exists because hourly slugs are not always `btc-updown-1h-{ts}`).
- UP/DOWN token IDs and bid/ask populate.
- `start_time` / `end_time` look correct for the live hour.
- Markets with `closed` or `acceptingOrders=false` are skipped.

If Gamma event search returns noise, tighten `_matches_updown_event` (asset/timeframe/title heuristics).

### 2. Install and wire Kronos for real

Not vendored into this repo (torch + model weights are heavy). On the research machine:

1. Clone [Kronos](https://github.com/shiyu-coder/Kronos) and install its deps (torch, pandas, etc.).
2. Ensure `from model import Kronos, KronosTokenizer, KronosPredictor` works on `PYTHONPATH`.
3. First run downloads `NeoQuasar/Kronos-Tokenizer-2k` + `NeoQuasar/Kronos-mini`.
4. Run with `--predictor kronos` (or `auto` once import works).

Likely follow-ups after first live Kronos run:

- Align candle timestamp convention with Polymarket hour boundaries (UTC vs ET labels).
- Predict **before** window open; only act in the first N minutes when ask ≈ 0.50.
- Cache the model in process so each hour is one inference, not reload.
- Optionally use 5m OHLCV lookback instead of 1h if accuracy is better (Kronos is interval-agnostic).

### 3. Harden the research loop

Suggested productization (not done yet):

- Persist **one prediction per window** (currently every sample can re-predict; scoring already uses first observation per slug).
- Separate tables: `hourly_predictions`, `hourly_entries`, `hourly_resolutions`.
- launchd / cron: run once near `:59` of each hour, then sample entry prices for ~5 minutes.
- Compare Kronos vs heuristic vs always-UP baseline on the same windows.
- Do **not** enable live orders; keep `POLYBOT_ENABLE_TRADING=false`.

### 4. Network / environment notes

- Work PC may block Gamma (Cisco Umbrella / SSL resets). Prefer home PC, phone hotspot, or a free always-on host that can reach Polymarket.
- Binance public klines usually work even when Gamma does not — heuristic/Kronos features can still be unit-tested offline with fixtures.
- Never commit `.env`. Disregard any previously pasted wallet keys; treat as burned.

## SQLite schema (hourly)

Table `hourly_observations` in `data/research.sqlite3` (gitignored):

- Market: `slug`, `market_id`, `start_time`, `end_time`
- Prediction: `prediction_side`, `prediction_confidence`, `prediction_source`
- Book: `up_bid/ask`, `down_bid/ask`, `selected_*`, `entry_available`
- Spot: `latest_binance_close`
- Outcome: `resolved_winner`, `simulated_pnl` (stake=1 model: win → `1/ask - 1`, lose → `-1`)

## Related prior work

- 5m monitor: `polybot-monitor-updown`
- 5m doubles analyzer: `polybot-analyze-doubles`
- Offline 5m ROI findings: `docs/research-strategy-summary.md`

## Suggested first commands for the next agent

```bash
git checkout cursor/public-price-research-772c
git pull --ff-only
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
# With Polymarket access:
polybot-resolve --updown btc:1h
polybot-monitor-hourly-research --predictor heuristic --duration-minutes 10
# After Kronos install:
polybot-monitor-hourly-research --predictor kronos --duration-minutes 70
polybot-analyze-hourly --resolve
```
