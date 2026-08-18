#!/usr/bin/env bash
# Dispatches `quality-score` to either the native venv (.venv-quality/) or the
# Dockerized `quality` service, based on QUALITY_SCORING_DOCKER in .env
# (default: true = Docker, CPU-only). Set QUALITY_SCORING_DOCKER=false to run
# natively on the host and use the Apple GPU — see CLAUDE.md / README.md "AI
# quality scoring". This is the only entrypoint users should invoke; don't
# call `docker compose run quality ...` or the venv python directly, or the
# toggle stops being a single source of truth.
#
# Usage (identical flags either way):
#   ./scripts/run_quality_score.sh --sample 20 --tier medium
#   ./scripts/run_quality_score.sh --tier heavyweight        # full run
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DATA_DIR_VAL=""
DOCKER_TOGGLE=""
if [ -f "$REPO_ROOT/.env" ]; then
  DATA_DIR_VAL=$(grep -E '^DATA_DIR=' "$REPO_ROOT/.env" | tail -1 | cut -d= -f2-)
  DOCKER_TOGGLE=$(grep -E '^QUALITY_SCORING_DOCKER=' "$REPO_ROOT/.env" | tail -1 | cut -d= -f2-)
fi
DATA_DIR_VAL="${DATA_DIR:-$DATA_DIR_VAL}"
DOCKER_TOGGLE="${QUALITY_SCORING_DOCKER:-${DOCKER_TOGGLE:-true}}"

if [ -z "$DATA_DIR_VAL" ]; then
  echo "DATA_DIR is not set (checked \$DATA_DIR and $REPO_ROOT/.env)." >&2
  exit 1
fi

if [ "$DOCKER_TOGGLE" = "false" ]; then
  VENV_DIR="$REPO_ROOT/.venv-quality"
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "Native venv not found at $VENV_DIR." >&2
    echo "Run ./scripts/setup_quality_native_env.sh first." >&2
    exit 1
  fi
  echo "Running quality-score natively (.venv-quality, QUALITY_SCORING_DOCKER=false)..."
  export DATA_DIR="$DATA_DIR_VAL"
  export PYTHONPATH="$REPO_ROOT/src"
  export PYTHONUNBUFFERED=1
  "$VENV_DIR/bin/python" -m deduplicayde.cli quality-score "$@"
  status=$?

  # Best-effort UX: auto-open the sample HTML report, natively only (a Docker
  # Linux VM has no host GUI to open a browser in).
  if [ $status -eq 0 ] && command -v open >/dev/null 2>&1; then
    latest_report=$(ls -t "$DATA_DIR_VAL"/logs/quality_sample_*.html 2>/dev/null | head -1)
    [ -n "$latest_report" ] && open "$latest_report"
  fi
  exit $status
else
  echo "Running quality-score in Docker (QUALITY_SCORING_DOCKER=true, default; CPU-only)..."
  exec docker compose run --rm quality quality-score "$@"
fi
