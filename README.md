# Polybot

A small Polymarket threshold bot. It polls configured market outcomes every `t` minutes, buys `d` dollars when price is below `min_price`, avoids buying again while already holding, and places a resting sell-limit order at `max_price` once you hold shares.

The bot defaults to dry-run mode. It will not submit real orders unless both `dry_run: false` and `POLYBOT_ENABLE_TRADING=true` are set.

## Free Local Mac Deployment

This workload does not need a GPU. It only performs API polling, threshold checks, and optional order placement.

The free production path is to run the bot on your Mac with `launchd`, macOS's built-in service manager. This costs nothing, but the bot only runs while the Mac is powered on, connected to the internet, and not asleep.

Free serverless schedulers are a poor fit for frequent market monitoring: GitHub Actions supports cron syntax as frequent as every 5 minutes, but timing is not guaranteed and free minutes are capped; Vercel Hobby cron/action limits are also restrictive.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
cp config.example.yaml config.yaml
```

Edit `.env` with your wallet/proxy values:

```bash
POLYMARKET_PRIVATE_KEY=...
POLYMARKET_USER_ADDRESS=...
POLYMARKET_FUNDER_ADDRESS=...
POLYMARKET_SIGNATURE_TYPE=
POLYBOT_ENABLE_TRADING=false
```

If you already have CLOB API credentials, you can also set `POLYMARKET_CLOB_API_KEY`, `POLYMARKET_CLOB_API_SECRET`, and `POLYMARKET_CLOB_API_PASSPHRASE`. Otherwise the bot will ask the CLOB client to create or derive them from the private key when real trading starts.

Edit `config.yaml` with the markets you want to monitor:

```yaml
poll_interval_minutes: 5
dry_run: true

markets:
  - id: "0xconditionid"
    token_id: "1234567890"
    outcome: "YES"
    end_time: "2026-12-31T23:59:00+00:00"
    min_price: 0.35
    max_price: 0.55
    buy_usd: 10
    avoid_buy_within_end_minutes: 1
    sell_shares: "all"
    enabled: true

recurring_markets:
  - asset: "btc"
    timeframe: "5m"
    outcome: "UP"
    min_price: 0.35
    max_price: 0.55
    buy_usd: 10
    avoid_buy_within_end_minutes: 1
    sell_shares: "all"
    enabled: false
```

Use the Polymarket CLOB token ID for the specific outcome you want to trade. `avoid_buy_within_end_minutes` is optional; when set to `1`, the bot buys only when there is more than 1 minute left, avoiding the final minute.

## Finding Market Values

You do not need credentials to find `id`, `token_id`, `outcome`, or `end_time`. They are public market metadata.

Use the helper with a Polymarket URL:

```bash
polybot-resolve --url "https://polymarket.com/event/example-slug"
```

Or with a slug:

```bash
polybot-resolve --slug "example-slug"
```

For recurring crypto Up/Down markets, do not hardcode the timestamped slug. Use the series form:

```bash
polybot-resolve --updown btc:5m
```

The bot uses the same rule internally for `recurring_markets`. You configure only `asset: "btc"` and `timeframe: "5m"`; you do not enter `1781606700`. For `btc-updown-5m-1781606700`, `1781606700` is the Unix timestamp for the 5-minute window start. For a 5m market, the next slug is 300 seconds later. Each polling cycle computes the current window from the current Unix time, resolves that event through Gamma, and trades the configured outcome.

Gamma also returns series metadata like `seriesSlug: btc-up-or-down-5m` and `recurrence: 5m`, plus per-window fields such as `closed`, `acceptingOrders`, `eventStartTime`, and `endDate`. The bot skips a recurring window if Gamma says it is closed or not accepting orders.

The helper prints YAML entries you can copy into `config.yaml`.

Where values come from:

- `id`: Polymarket `conditionId`. Used for market metadata and positions filtering.
- `token_id`: CLOB token ID for the exact outcome you trade, such as `YES` or `NO`. Used for price checks and orders.
- `outcome`: Human label paired with the `token_id`.
- `end_time`: Market end time from Gamma market metadata.
- `min_price`, `max_price`, `buy_usd`, `avoid_buy_within_end_minutes`: Your strategy choices.
- `recurring_markets.asset`: The crypto prefix in the Polymarket slug, for example `btc`, `eth`, or `sol`.
- `recurring_markets.timeframe`: The window size in the slug, for example `5m` or `15m`.
- Wallet/private-key values in `.env`: only needed for checking your positions, checking open orders, and submitting real orders.

Where credential values come from:

- `POLYMARKET_USER_ADDRESS`: Your Polymarket proxy wallet/user address. This is the address used by the public Data API to fetch positions.
- `POLYMARKET_FUNDER_ADDRESS`: The wallet/proxy address that funds CLOB orders. For many Polymarket accounts this is the same proxy wallet address.
- `POLYMARKET_PRIVATE_KEY`: The private key for the wallet authorized to trade. Keep it only in local `.env`.
- `POLYMARKET_SIGNATURE_TYPE`: Polymarket CLOB signature type for your account/wallet setup: `0` for standalone EOA, `1` for Polymarket proxy/Magic email flow, `2` for Gnosis Safe, `3` for the newer deposit-wallet/POLY_1271 flow.
- `POLYMARKET_CLOB_API_KEY`, `POLYMARKET_CLOB_API_SECRET`, `POLYMARKET_CLOB_API_PASSPHRASE`: Optional CLOB API credentials. If omitted, the bot asks the CLOB client to create or derive them from the private key when real trading starts.

## Run Locally

Run one cycle:

```bash
polybot --once
```

Run continuously:

```bash
polybot
```

Run continuously in the background with `launchd`:

```bash
mkdir -p ~/Library/LaunchAgents
cp deploy/launchd/com.guygirmonsky.polybot.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.guygirmonsky.polybot.plist
launchctl enable "gui/$(id -u)/com.guygirmonsky.polybot"
launchctl kickstart -k "gui/$(id -u)/com.guygirmonsky.polybot"
```

Check logs:

```bash
tail -f logs/polybot.out.log logs/polybot.err.log
```

Stop the background service:

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.guygirmonsky.polybot.plist
```

Run tests:

```bash
pytest
```

## Keep The Mac Awake

For reliable scanning, disable sleep while the bot is running. You can use macOS System Settings, or keep the machine awake from a terminal:

```bash
caffeinate -dimsu
```

Leave that terminal open. Closing it allows normal sleep behavior again.

## First-Minute Strategy (default config)

`mode: first_minute` in `config.yaml` implements the research deployable rule:

- Poll BTC 5m Up/Down every **1 second** during the first **60 seconds** of each window (`minutes_left >= 4`).
- Evaluate enabled side rules (ask ≤ `entry_ask`). Take **at most one** trade per window, or do nothing.
- If a buy happens, place a resting sell-limit at `entry_ask * exit_multiplier` (hold to settle if it never fills).
- Current research mapping: **DOWN a=0.36 x=2.7 enabled**; **UP disabled** (no ROI>10% early-window UP rule).

Dry-run (no wallet required):

```bash
polybot --once
# or continuously:
polybot
```

## Real Trading

Real orders require two explicit switches:

```yaml
dry_run: false
```

```bash
POLYBOT_ENABLE_TRADING=true
```

Keep dry-run enabled until logs show the exact markets, prices, holdings, and actions you expect.

**Never commit `.env` or private keys.** If a key was ever pushed to a public repo, treat that wallet as compromised and use a new account.

## Sell Limits

Polymarket supports GTC limit orders through the CLOB API. The bot uses that for exits: after it sees that you hold shares, it places a resting sell-limit order at `max_price`. If that order is already open, it does not place another one.

## Public Price Research

You can monitor BTC 5m Up/Down prices without wallet credentials. This records public order-book prices into SQLite so you can later test entry thresholds.

No wallet, no positions, and no orders are involved. The monitor only reads public Gamma and CLOB order-book data.

### Run On A Standalone PC

Run this workflow on a machine that can reach Polymarket APIs. Cloud agent and many datacenter networks are often blocked by Polymarket, so use a home PC, laptop, or VPS with normal residential or non-blocked egress.

1. Clone or copy this repo onto the machine.
2. Install the project:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

3. Confirm Polymarket is reachable before starting a long collection:

```bash
.venv/bin/python -c "
from polybot.config import load_env
from polybot.polymarket_client import PolymarketClient
from polybot.research import collect_updown_once

samples = collect_updown_once(PolymarketClient(load_env(None)), 'btc', '5m')
print(len(samples), samples[0].slug if samples else 'no samples')
"
```

You should see `2` and a slug like `btc-updown-5m-...`. If you get a connection error, switch to a different network or machine before collecting data.

4. Collect BTC 5m samples for 3 hours:

```bash
.venv/bin/polybot-monitor-updown --asset btc --timeframe 5m --interval-seconds 1 --duration-minutes 180
```

Recommended sampling is `1s`. `0.5s` is possible, but usually not worth the extra API load for a multi-hour run.

Samples are stored in `data/research.sqlite3`, which is intentionally ignored by git.

5. After collection finishes, generate the `a -> 2a` table:

```bash
.venv/bin/polybot-analyze-doubles --asset btc --timeframe 5m --min-threshold 0.05 --max-threshold 0.50 --step 0.01
```

### Interpretation

The analyzer uses realistic side prices:

- Entry opportunity: first time best ask is `<= a`.
- Double-target success: after entry, best bid becomes `>= 2a` before the window ends.
- Results are grouped separately for `UP` and `DOWN`.

### Optional Long-Running Collection

For unattended collection on macOS or Linux, run the monitor in `tmux`, `screen`, or `nohup`:

```bash
nohup .venv/bin/polybot-monitor-updown \
  --asset btc \
  --timeframe 5m \
  --interval-seconds 1 \
  --duration-minutes 180 \
  > data/research-monitor.log 2>&1 &
```

Copy `data/research.sqlite3` back to your analysis machine if collection and analysis happen on different computers.

## Optional Docker

```bash
docker compose up --build
```

Docker is still available if you later move to a VPS:

1. Install Docker and Docker Compose.
2. Copy this project to the VPS.
3. Create `.env` and `config.yaml` on the VPS.
4. Start the service with `docker compose up -d --build`.
5. Watch the first session with `docker compose logs -f`.

The compose file persists SQLite state under `./data`.

## How Decisions Work

- If current price is below `min_price` and holdings are zero, buy `buy_usd`.
- If current price is below `min_price`, holdings are zero, and the optional time window matches, buy `buy_usd`.
- If holdings are already positive, place a resting sell-limit order at `max_price`.
- If a sell-limit order at `max_price` is already open, hold.
- Otherwise, hold.

Positions are fetched from Polymarket's Data API. Orders are submitted through the Polymarket CLOB v2 Python client.
