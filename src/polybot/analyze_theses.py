from __future__ import annotations

import argparse
from pathlib import Path

from polybot.capital_roi import analyze_db, format_score_table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare strategy theses by capital ROI.")
    parser.add_argument(
        "--db",
        action="append",
        required=True,
        help="Research sqlite path. Repeat for multiple DBs.",
    )
    parser.add_argument("--stake", type=float, default=1.0)
    parser.add_argument("--min-trades", type=int, default=5)
    parser.add_argument("--top", type=int, default=25)
    args = parser.parse_args(argv)

    for db in args.db:
        path = Path(db)
        print(f"\n## {path}")
        if not path.exists():
            print(f"missing: {path}")
            continue
        scores = analyze_db(path, stake=args.stake)
        # mark baseline explicitly
        baseline = next((s for s in scores if s.name.startswith("baseline_")), None)
        if baseline and baseline.capital_roi is not None:
            print(
                f"leader/baseline: `{baseline.name}` ROI={baseline.capital_roi*100:+.1f}% "
                f"trades={baseline.trades}/{baseline.windows} windows"
            )
        table = format_score_table(scores, min_trades=args.min_trades)
        # keep top N rows (+ header)
        lines = table.splitlines()
        header, body = lines[:2], lines[2:]
        print("\n".join(header + body[: args.top]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
