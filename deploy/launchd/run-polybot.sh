#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

cd "$PROJECT_DIR"

if [[ ! -x "$PROJECT_DIR/.venv/bin/polybot" ]]; then
  echo "polybot executable not found. Run: python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'" >&2
  exit 1
fi

exec "$PROJECT_DIR/.venv/bin/polybot"
