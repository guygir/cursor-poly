# BTC 5m Up/Down — Strategy Research Summary

Offline price research on Polymarket BTC 5-minute Up/Down markets (public books only,
no wallet/orders). We sampled order books into SQLite, then evaluated entry threshold
`a`, exit multiplier `x` (or hold to settlement), side (UP/DOWN), and time remaining.

## Scoring model

Polymarket bankroll model (not naive `$/share`):

- Stake `X` at ask `p`. Win at settle → receive `X / p`. Lose → `0`.
- Take-profit at target `t = x·a`: if bid reaches `t` after entry, receive `X · (t / p)`; else settle.
- **`x = null` means hold to settlement.**
- **Capital ROI = E[payout] / stake − 1**, per entry, at most one entry per window.
- `n` = number of opportunities (entries), **not** wins. Low win% at low `a` can still yield
  high ROI because the payoff is ≈ `1/a`.
- Entry rule: first sample with `ask ≤ a`, optionally only if `time_left ≥ bucket`
  (30s steps: 270…0). Fill uses the actual ask (avg entry can be `< a` if price gaps).
- Window winner inferred from final mids (UP → ~1, DOWN → ~0).

## Datasets

Three independent collection runs, no shared windows (slug overlap = 0), so pooling all
three is equivalent to one larger sample.

| DB | Cadence | Rows | Windows | File |
|----|---------|------|---------|------|
| db1 | ~1s (mixed) | 54,592 | 135 | `data/research.sqlite3` |
| db2 | 0.5s, 12h | 170,096 | 145 | `data/research-12h-0p5s.sqlite3` |
| db3 | 0.5s, 12h | 167,958 | 145 | `data/research-12h-0p5s-run3.sqlite3` |
| **Pooled** | — | — | **425** | densest-per-slug union |

Robust filters applied everywhere: `n ≥ 20`, `|avg_entry − a| ≤ 0.03`, and TP cells that
never actually hit (identical to hold) are dropped.

## Key scientific findings

- The unconstrained "best `a`" is **regime-dependent**: db1 was more UP-heavy, db2 more
  DOWN-heavy. Pooled winners: UP=216, DOWN=208 (1 unknown).
- Cheap **UP `a≈0.05–0.16` hold** looks excellent on some runs but is largely a
  **late-window dump artifact**: when DOWN wins, UP ask drops ≤0.13 in ~93–100% of windows,
  with almost no such entries when ≥4.5m remain. It flips negative on the run where the
  regime differs (see 2/3 section), so it is not robust.
- With a **time filter** (enter only with meaningful time left), early/mid-window mid-`a`
  DOWN rules with a take-profit are the most consistent across runs.
- For DOWN TP rules, `a·x` for top cells is often ~0.70–0.91 (not ≈1): TP beats pure hold
  when some winners later flip (bank the bounce, sell before reverse).

## Results by robustness criterion

### Single DB — top capital ROI per DB (robust)

| DB | Best rule | ROI | n | win% |
|----|-----------|-----|---|------|
| db1 | UP a=0.05 ≥90s null | +81.8% | 22 | 9.1% |
| db2 | DOWN a=0.26 ≥240s x=2.8 | +75.8% | 20 | 40.0% |
| db3 | UP a=0.07 ≥0s null | +110.6% | 83 | 12.0% |

These per-DB winners disagree on side and structure — each overfits its own regime.

### 3/3 — positive on all three DBs (most robust)

Best cell by the maximum of its worst-DB ROI (40 cells positive in all three):

**DOWN · a=0.36 · x=2.7 · time_left ≥ 270s**

| DB | ROI | n | win% | TP hit% |
|----|-----|---|------|---------|
| db1 | +14.9% | 22 | 36.4% | 40.9% |
| db2 | +29.5% | 36 | 47.2% | 47.2% |
| db3 | +24.5% | 34 | 41.2% | 38.2% |

- Worst-case (min across DBs): **+14.9%**
- Simple average across DBs: **≈ +23.0%**
- Same cell held to settle (null) is weaker: min +5.6%.

Other strong 3/3 cells clustered around **DOWN a=0.35–0.38, ≥270s, x=2.3–2.8**.

### 2/3 — positive on at least two DBs (higher headline, less safe)

Top cells look stronger but each **flips negative on the third DB**:

| Rule | min+ (positive DBs) | Failing DB |
|------|--------------------|------------|
| UP a=0.13 ≥0s null | +42.2% (db1 +68.7, db3 +42.2) | db2 −24.3% |
| UP a=0.12 ≥0s null | +39.5% (db1 +46.7, db3 +39.5) | db2 −27.8% |
| UP a=0.11 ≥0s null | +38.9% (db1 +38.9, db3 +51.6) | db2 −22.1% |
| DOWN a=0.20 ≥210s x=3.6 | +34.4% (db2 +56.5, db3 +34.4) | db1 −6.1% |
| DOWN a=0.12 ≥120s null | +33.6% (db2 +37.1, db3 +33.6) | db1 −23.8% |

Takeaway: allowing 2/3 mostly resurfaces the cheap late-dump UP-hold artifact and other
regime-specific cells. Higher on paper, but not dependable.

### All DBs pooled (treated as one large 425-window DB)

Ranked by pooled average capital ROI, no cross-DB veto:

| Rule | ROI | n | win% | note |
|------|-----|---|------|------|
| UP a=0.07 ≥0s null | +37.4% | 218 | 7.8% | cheap late-dump; any time left |
| DOWN a=0.19 ≥210s x=3.8 | +36.2% | 57 | 15.8% | early/mid, aggressive TP |
| DOWN a=0.32 ≥270s x=3.0 | +27.3% | 55 | 40.0% | early-window peak |
| DOWN a=0.32 ≥270s null | +26.7% | 55 | 40.0% | hold variant |
| DOWN a=0.36 ≥270s x=2.7 | +24.1% | 92 | 42.4% | the 3/3 pick, pooled |

Best pooled cell per time filter:

- `left ≥ 0s`: UP a=0.07 null → +37.4%
- `left ≥ 180s`: DOWN a=0.19 x=3.8 → +18.7% (n=88)
- `left ≥ 270s`: DOWN a=0.32 x=3.0 → +27.3%

## Recommendation

- **Most robust / deployable (survives every run):**
  **DOWN, a = 0.36, x = 2.7 (sell if bid ≈ 0.97, else settle), enter only with time_left ≥ 270s.**
  Worst-run ROI +14.9%, average ≈ +23%, and it holds a large sample when pooled
  (n=92, +24.1%).

- **If optimizing pooled average and you accept an early-window constraint:**
  **DOWN, a = 0.32, x = 3.0, time_left ≥ 270s** (≈ +27% pooled).

- **Avoid** the headline cheap **UP a≈0.05–0.16 hold** rules: highest raw ROI but they are
  a late-window dump artifact and flip negative whenever the regime differs (db2). Not
  robust despite the big numbers.

### One-line deployable

`side=DOWN  a=0.36  x=2.7  time_left>=270s`  (hold-to-settle fallback if no TP hit)

## Reproduce

```bash
python data/run_pick_best_strategy.py        # 3/3 cross-DB pick
python data/run_capital_roi_3d.py            # per-DB 3D (a x exit x min_time_left)
python data/run_deployable_rules.py          # robust cross-DB summary
```

Outputs: `data/research-best-single-strategy.txt`, `data/research-deployable-rules.txt`,
`data/research-*-capital-roi-3d.txt`.
