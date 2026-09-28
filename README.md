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

The plist ships with `/path/to/cursor-poly` placeholders. Replace those paths with this repo’s absolute path before loading it. `.env` stays local; do not commit keys.

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

## Real Trading

Real orders require two explicit switches:

```yaml
dry_run: false
```

```bash
POLYBOT_ENABLE_TRADING=true
```

Keep dry-run enabled until logs show the exact markets, prices, holdings, and actions you expect.

## Sell Limits

Polymarket supports GTC limit orders through the CLOB API. The bot uses that for exits: after it sees that you hold shares, it places a resting sell-limit order at `max_price`. If that order is already open, it does not place another one.

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
