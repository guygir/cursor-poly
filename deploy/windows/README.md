# Windows 8-hour local run

This Cursor cloud VM is often **geoblocked** for Polymarket CLOB orders. Run live trading on your **Windows PC**.

## One-time setup on Windows

```powershell
cd path\to\cursor-poly
git pull
python -m venv .venv
.\.venv\Scripts\pip install -e .
```

Copy secrets into a local `.env` (gitignored). Use the same values as the agent workspace `/workspace/.env`:

- `POLYMARKET_USER_ADDRESS`
- `POLYMARKET_FUNDER_ADDRESS`
- `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_SIGNATURE_TYPE=3` (deposit-wallet / POLY_1271; `1` is proxy/Magic)
- `POLYBOT_ENABLE_TRADING=false` (the script flips this on for the run)

`config.yaml` should have `mode: first_minute` and `buy_usd: 2` (keeps share size above CLOB min sell size of 5 at entry ≤ 0.36).

## Start an 8-hour run (keeps PC awake)

In **PowerShell as your user** (lid can close on some setups only if sleep is inhibited; keep AC power preferred):

```powershell
cd path\to\cursor-poly
powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-8h.ps1 -Hours 8 -BuyUsd 2
```

What it does:

- Sets `dry_run: false` and `POLYBOT_ENABLE_TRADING=true`
- Calls Win32 `SetThreadExecutionState` so Windows should **not sleep** while the script runs
- Runs `polybot` for N hours (DOWN ask ≤ 0.36 → buy → sell-limit 0.97)
- On exit/timeout: restores `dry_run: true` and `POLYBOT_ENABLE_TRADING=false`

Logs: `logs/polybot-8h.out.log`, `logs/polybot-8h.err.log`

## PnL watcher (optional)

Tracks matched buys from the out-log and prints WON/LOST + cumulative P&L after each window settles. Writes `logs/pnl-ledger.json` (gitignored under `logs/`). Does not load or print private keys.

```powershell
.\.venv\Scripts\python.exe .\deploy\windows\pnl-watcher.py
```

## Also recommended in Windows Settings

- Power → Screen and sleep → **Sleep: Never** (while plugged in) for the session
- Keep the laptop plugged in

Closing the laptop lid may still sleep on some OEM policies; if so, leave the lid open or change the lid-close action to “Do nothing”.
