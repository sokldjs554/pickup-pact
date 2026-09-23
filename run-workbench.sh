#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH=".:services/reconciler${PYTHONPATH:+:$PYTHONPATH}"
exec python -m app.workbench --db .repair-review/review.sqlite --port 8765
