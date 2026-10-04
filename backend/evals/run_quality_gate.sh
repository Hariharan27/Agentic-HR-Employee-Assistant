#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BACKEND_DIR=$(dirname "$SCRIPT_DIR")

cd "$BACKEND_DIR"
.venv/bin/pytest -q
.venv/bin/python evals/run_golden.py \
  --repeat 3 \
  --quality-gate \
  --fail-under 0.95 \
  "$@"
