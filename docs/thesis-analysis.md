# Thesis analysis vs current leader

## Collection (cloud)

Public order-book collector (no wallet / no orders):

- DB: `data/research-cloud-8h.sqlite3`
- Cadence: 1s, BTC 5m Up/Down
- Duration: 480 minutes from start

## Historical DBs required for ROI comparison

The published leader (**DOWN a=0.36 x=2.7, time_left≥270s**, worst-DB ROI +14.9%) was fit on three local DBs that are **not in git**:

| DB | File |
|----|------|
| db1 | `data/research.sqlite3` |
| db2 | `data/research-12h-0p5s.sqlite3` |
| db3 | `data/research-12h-0p5s-run3.sqlite3` |

Copy those onto this machine (or your Windows research box) under `data/`, then:

```bash
polybot-analyze-theses \
  --db data/research.sqlite3 \
  --db data/research-12h-0p5s.sqlite3 \
  --db data/research-12h-0p5s-run3.sqlite3 \
  --min-trades 20 \
  --top 30
```

## Theses encoded

- **A (race):** first of UP/DOWN with `ask ≤ a` inside first 30s/60s → one buy; hold or TP ~0.95
- **B (scale-in):** in first 30s/60s, each first touch of 0.35 / 0.30 / 0.25 buys another unit (per side or both); hold or TP ~0.95
- **Creative grid:** race a∈{0.25…0.35}, scale level sets, DOWN-only early variants near the leader

Capital ROI uses the same bankroll model as `docs/research-strategy-summary.md` (stake at ask, TP on bid, else settle).
