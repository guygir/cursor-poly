"""
Watch polybot logs for matched buys; at each window end report WON/LOST + cumulative P&L.

Pings only for windows where a buy filled - not on holds/skips.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "logs" / "polybot-8h.out.log"
LEDGER = ROOT / "logs" / "pnl-ledger.json"
STATUS = ROOT / "logs" / "pnl-watcher-status.txt"

ENTRY_RE = re.compile(
    r"entry_check slug=(?P<slug>\S+) outcome=(?P<outcome>\S+) "
    r"ask=(?P<ask>[0-9.]+) bid=(?P<bid>[0-9.]+) .* action=buy"
)
BUY_RE = re.compile(
    r"Submitted buy token=(?P<token>\d+) status=(?P<status>\S+) order_id=(?P<order_id>\S+)"
)
SELL_LIMIT_RE = re.compile(
    r"Submitted place_sell_limit token=(?P<token>\d+) status=(?P<status>\S+) order_id=(?P<order_id>\S+)"
)
SELL_RE = re.compile(
    r"Submitted sell token=(?P<token>\d+) status=(?P<status>\S+) order_id=(?P<order_id>\S+)"
)
EXIT_HOLD_RE = re.compile(
    r"exit_check outcome=(?P<outcome>\S+) holdings=(?P<holdings>[0-9.]+) "
    r"exit_bid=(?P<exit_bid>[0-9.]+) action=(?P<action>\S+)"
)
NEW_WINDOW_RE = re.compile(r"New window slug=(?P<slug>\S+) start=(?P<start>\d+)")
BUY_USD_RE = re.compile(r"buy_usd=(?P<buy_usd>[0-9.]+)")


@dataclass
class OpenTrade:
    slug: str
    outcome: str
    token_id: str
    entry_ask: float
    buy_usd: float
    shares: float
    order_id: str
    exit_bid: float | None
    window_end: int
    buy_ts: str


def log_status(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    with STATUS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def load_ledger() -> dict:
    for path in (LEDGER, LEDGER.with_name("pnl-ledger.local.json")):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                # Migrate single open -> open_trades queue
                if "open_trades" not in data:
                    data["open_trades"] = []
                    if data.get("open"):
                        data["open_trades"].append(data["open"])
                        data["open"] = None
                return data
            except Exception:
                continue
    return {
        "cumulative_pnl_usd": 0.0,
        "trades": [],
        "open": None,
        "open_trades": [],
        "seen_order_ids": [],
    }


def save_ledger(ledger: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(ledger, indent=2)
    tmp = LEDGER.with_suffix(".json.tmp")
    last_err: Exception | None = None
    for attempt in range(8):
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, LEDGER)
            return
        except PermissionError as exc:
            last_err = exc
            time.sleep(0.25 * (attempt + 1))
        except OSError as exc:
            last_err = exc
            time.sleep(0.25 * (attempt + 1))
    # Last resort: sidestep a locked OneDrive file
    fallback = LEDGER.with_name("pnl-ledger.local.json")
    fallback.write_text(payload, encoding="utf-8")
    log_status(f"LEDGER_FALLBACK wrote {fallback.name} after lock: {last_err}")


def slug_window_end(slug: str) -> int | None:
    # btc-updown-5m-<unix_start>
    parts = slug.rsplit("-", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    return int(parts[1]) + 300


def fetch_gamma_event(slug: str) -> dict | None:
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"slug": slug},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
    except Exception as exc:
        log_status(f"GAMMA_ERR {exc}")
    return None


def fetch_position(user: str, token_id: str) -> dict | None:
    try:
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": user, "sizeThreshold": 0},
            timeout=20,
        )
        r.raise_for_status()
        for p in r.json() or []:
            if str(p.get("asset")) == token_id:
                return p
    except Exception as exc:
        log_status(f"POS_ERR {exc}")
    return None


def fetch_activity_for_asset(user: str, token_id: str, start_ts: int) -> list[dict]:
    try:
        r = requests.get(
            "https://data-api.polymarket.com/activity",
            params={
                "user": user,
                "type": "TRADE,REDEEM",
                "start": start_ts,
                "limit": 100,
            },
            timeout=20,
        )
        r.raise_for_status()
        rows = r.json() or []
        return [a for a in rows if str(a.get("asset")) == token_id]
    except Exception as exc:
        log_status(f"ACT_ERR {exc}")
        return []


def parse_outcome_prices(raw) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if isinstance(raw, list) and raw:
        try:
            return [float(x) for x in raw]
        except Exception:
            return None
    return None


def resolve_trade(trade: OpenTrade, user: str) -> tuple[str, float, str]:
    """
    Returns (result, pnl_usd, detail).
    result in {WON, LOST, UNKNOWN}
    """
    cost = trade.buy_usd
    window_start = trade.window_end - 300

    # 1) Sell fill via activity
    acts = fetch_activity_for_asset(user, trade.token_id, window_start - 30)
    sell_usdc = 0.0
    for a in acts:
        if str(a.get("type")).upper() == "TRADE" and str(a.get("side")).upper() == "SELL":
            sell_usdc += float(a.get("usdcSize") or 0)
    if sell_usdc > 0:
        pnl = sell_usdc - cost
        return ("WON" if pnl > 0 else "LOST" if pnl < 0 else "FLAT"), pnl, f"sold_for=${sell_usdc:.4f}"

    # 2) Redeem activity
    redeem_usdc = 0.0
    for a in acts:
        if str(a.get("type")).upper() == "REDEEM":
            redeem_usdc += float(a.get("usdcSize") or 0)
    if redeem_usdc > 0:
        pnl = redeem_usdc - cost
        return ("WON" if pnl > 0 else "LOST"), pnl, f"redeemed=${redeem_usdc:.4f}"

    # 3) Gamma resolution
    event = fetch_gamma_event(trade.slug)
    if event:
        markets = event.get("markets") or []
        market = markets[0] if markets else {}
        closed = bool(market.get("closed") or event.get("closed"))
        prices = parse_outcome_prices(market.get("outcomePrices"))
        outcomes = market.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = None
        if closed and prices and outcomes:
            # Map our outcome label to price
            idx = None
            for i, name in enumerate(outcomes):
                if str(name).upper() == trade.outcome.upper():
                    idx = i
                    break
            if idx is not None and idx < len(prices):
                payout = prices[idx]  # 1.0 win, 0.0 lose typically
                proceeds = trade.shares * payout
                pnl = proceeds - cost
                label = "WON" if payout >= 0.99 else "LOST" if payout <= 0.01 else "PARTIAL"
                return label, pnl, f"resolved payout={payout:.2f} proceeds=${proceeds:.4f}"

    # 4) Position mark after window
    pos = fetch_position(user, trade.token_id)
    if pos is not None:
        size = float(pos.get("size") or 0)
        cur = pos.get("curPrice")
        if size <= 0.01:
            # Position gone — likely sold; fall back to cashPnl/realized if present
            realized = pos.get("realizedPnl")
            if realized is not None:
                pnl = float(realized)
                return ("WON" if pnl > 0 else "LOST" if pnl < 0 else "FLAT"), pnl, "position_closed realizedPnl"
            return "UNKNOWN", 0.0, "position_closed_no_pnl"
        if cur is not None:
            cur_f = float(cur)
            if cur_f >= 0.99:
                proceeds = size * 1.0
                pnl = proceeds - cost
                return "WON", pnl, f"curPrice=1 size={size:.4f}"
            if cur_f <= 0.01:
                return "LOST", -cost, f"curPrice=0 size={size:.4f}"

    return "UNKNOWN", 0.0, "not_resolved_yet"


def close_trade(ledger: dict, trade: OpenTrade, result: str, pnl: float, detail: str) -> None:
    ledger["cumulative_pnl_usd"] = round(float(ledger.get("cumulative_pnl_usd", 0.0)) + pnl, 4)
    cum = ledger["cumulative_pnl_usd"]
    record = {
        **asdict(trade),
        "result": result,
        "pnl_usd": round(pnl, 4),
        "detail": detail,
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }
    ledger.setdefault("trades", []).append(record)
    # Drop from queue
    ledger["open_trades"] = [
        t for t in ledger.get("open_trades", []) if t.get("order_id") != trade.order_id
    ]
    if ledger.get("open") and ledger["open"].get("order_id") == trade.order_id:
        ledger["open"] = None
    save_ledger(ledger)
    sign = "+" if cum >= 0 else ""
    log_status(
        f"SETTLEMENT result={result} slug={trade.slug} outcome={trade.outcome} "
        f"entry_ask={trade.entry_ask} buy_usd={trade.buy_usd} shares={trade.shares:.4f} "
        f"pnl=${pnl:+.4f} cumulative=${sign}{cum:.4f} ({detail})"
    )


def maybe_close_open(ledger: dict, user: str, force: bool = False) -> None:
    now = int(time.time())
    opens = list(ledger.get("open_trades") or [])
    if not opens and ledger.get("open"):
        opens = [ledger["open"]]
    for open_raw in opens:
        trade = OpenTrade(**open_raw)
        if not force and now < trade.window_end + 15:
            continue
        result, pnl, detail = resolve_trade(trade, user)
        # Never finalize UNKNOWN — keep retrying (resolution/API can lag a long time)
        if result == "UNKNOWN":
            # Log at most once every ~5 minutes to avoid chat spam
            if now > trade.window_end + 60 and ((now - trade.window_end) % 300) < 2:
                log_status(
                    f"PENDING_SETTLEMENT slug={trade.slug} outcome={trade.outcome} "
                    f"waited={now - trade.window_end}s ({detail})"
                )
            continue
        close_trade(ledger, trade, result, pnl, detail)


def register_buy(
    ledger: dict,
    *,
    slug: str,
    outcome: str,
    ask: float,
    token_id: str,
    order_id: str,
    buy_usd: float,
    exit_bid: float | None,
    shares_hint: float | None,
) -> None:
    if order_id in ledger.get("seen_order_ids", []):
        return
    # Attempt to settle any due opens, but never drop unresolved ones
    maybe_close_open(ledger, os.getenv("POLYMARKET_USER_ADDRESS", ""), force=False)

    window_end = slug_window_end(slug) or (int(time.time()) + 300)
    shares = shares_hint if shares_hint and shares_hint > 0 else (buy_usd / ask if ask else 0.0)
    trade = OpenTrade(
        slug=slug,
        outcome=outcome,
        token_id=token_id,
        entry_ask=ask,
        buy_usd=buy_usd,
        shares=shares,
        order_id=order_id,
        exit_bid=exit_bid,
        window_end=window_end,
        buy_ts=datetime.now(timezone.utc).isoformat(),
    )
    ledger.setdefault("seen_order_ids", []).append(order_id)
    ledger.setdefault("open_trades", []).append(asdict(trade))
    ledger["open"] = asdict(trade)  # latest pointer for compatibility
    save_ledger(ledger)
    log_status(
        f"TRACK_BUY slug={slug} outcome={outcome} ask={ask} buy_usd={buy_usd} "
        f"shares~={shares:.4f} window_end={window_end} order={order_id[:12]}..."
    )


def follow_log(user: str) -> None:
    ledger = load_ledger()
    buy_usd = 2.0
    last_entry: dict | None = None
    last_exit_bid: float | None = None
    last_holdings: float | None = None
    current_slug: str | None = None

    # Seed buy_usd from latest Starting line if present
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            m = BUY_USD_RE.search(line)
            if m and "Starting polybot" in line:
                buy_usd = float(m.group("buy_usd"))

    log_status(f"PNL_WATCHER_START user={user[:8]}... buy_usd_default={buy_usd} ledger={LEDGER}")

    # Catch up: scan existing log for unmatched buys in current run
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            m = BUY_USD_RE.search(line)
            if m and "Starting polybot" in line:
                buy_usd = float(m.group("buy_usd"))
            m = NEW_WINDOW_RE.search(line)
            if m:
                current_slug = m.group("slug")
            m = ENTRY_RE.search(line)
            if m:
                last_entry = m.groupdict()
            m = EXIT_HOLD_RE.search(line)
            if m:
                last_exit_bid = float(m.group("exit_bid"))
                last_holdings = float(m.group("holdings"))
            m = BUY_RE.search(line)
            if m and m.group("status") == "matched":
                if last_entry:
                    register_buy(
                        ledger,
                        slug=last_entry["slug"],
                        outcome=last_entry["outcome"],
                        ask=float(last_entry["ask"]),
                        token_id=m.group("token"),
                        order_id=m.group("order_id"),
                        buy_usd=buy_usd,
                        exit_bid=last_exit_bid,
                        shares_hint=last_holdings,
                    )
                    ledger = load_ledger()

    # Open file at end for live tail
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.touch(exist_ok=True)
    with LOG.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                maybe_close_open(ledger, user)
                ledger = load_ledger()
                time.sleep(1.0)
                continue

            m = BUY_USD_RE.search(line)
            if m and "Starting polybot" in line:
                buy_usd = float(m.group("buy_usd"))

            m = NEW_WINDOW_RE.search(line)
            if m:
                current_slug = m.group("slug")

            m = ENTRY_RE.search(line)
            if m:
                last_entry = m.groupdict()

            m = EXIT_HOLD_RE.search(line)
            if m:
                last_exit_bid = float(m.group("exit_bid"))
                last_holdings = float(m.group("holdings"))
                # Update matching open trade shares if we have a better reading
                if last_holdings > 0 and ledger.get("open_trades"):
                    changed = False
                    for t in ledger["open_trades"]:
                        # Prefer updating the latest open with same outcome/exit context
                        t["shares"] = last_holdings
                        if last_exit_bid:
                            t["exit_bid"] = last_exit_bid
                        changed = True
                    if ledger.get("open"):
                        ledger["open"]["shares"] = last_holdings
                        if last_exit_bid:
                            ledger["open"]["exit_bid"] = last_exit_bid
                    if changed:
                        save_ledger(ledger)

            m = BUY_RE.search(line)
            if m and m.group("status") == "matched" and last_entry:
                register_buy(
                    ledger,
                    slug=last_entry["slug"],
                    outcome=last_entry["outcome"],
                    ask=float(last_entry["ask"]),
                    token_id=m.group("token"),
                    order_id=m.group("order_id"),
                    buy_usd=buy_usd,
                    exit_bid=last_exit_bid,
                    shares_hint=last_holdings,
                )
                ledger = load_ledger()

            maybe_close_open(ledger, user)
            ledger = load_ledger()


def main() -> int:
    load_dotenv(ROOT / ".env")
    user = os.getenv("POLYMARKET_USER_ADDRESS") or os.getenv("POLYMARKET_FUNDER_ADDRESS")
    if not user:
        raise SystemExit("POLYMARKET_USER_ADDRESS missing in .env")
    follow_log(user)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
