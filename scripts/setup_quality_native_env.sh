#!/usr/bin/env bash
# One-time (or re-run-to-upgrade) bootstrap for the native AI quality-scoring
# venv. This is the ONE deliberate exception to CLAUDE.md's "never installed
# on host" rule — see CLAUDE.md / README.md "AI quality scoring" for why:
# Docker Desktop on Apple Silicon runs containers in a Linux VM with no
# Metal/ANE passthrough, so `quality-score` needs to run natively to use the
# host GPU via PyTorch's `mps` backend.
#
# Usage:
#   ./scripts/setup_quality_native_env.sh
#
# Creates .venv-quality/ at the repo root (gitignored) and installs
# requirements/quality.txt into it. Safe to re-run to pick up changes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv-quality"

# Prefer python3.13 (more broadly proven against released PyTorch wheels as
# of this writing); fall back to python3.14 if that's all that's available.
# Neither is used anywhere else in this repo — this venv is fully
# self-contained and gitignored, and never affects the Dockerized Python 3.12
# services.
PYBIN=""
for candidate in python3.13 python3.14; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYBIN="$(command -v "$candidate")"
    break
  fi
done

if [ -z "$PYBIN" ]; then
  echo "Neither python3.13 nor python3.14 found on PATH." >&2
  echo "Install one (e.g. 'brew install python@3.13') and re-run." >&2
  exit 1
fi

echo "Using $("$PYBIN" --version) at $PYBIN"

if [ -d "$VENV_DIR" ]; then
  echo "Reusing existing venv at $VENV_DIR (delete it first to recreate from scratch)."
else
  "$PYBIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$REPO_ROOT/requirements/quality.txt"

echo
echo "Verifying PyTorch can see the Apple GPU (MPS backend)..."
"$VENV_DIR/bin/python" -c "
import torch
print('torch', torch.__version__)
print('MPS built:', torch.backends.mps.is_built())
print('MPS available:', torch.backends.mps.is_available())
"

echo
echo "Native quality-scoring env ready at $VENV_DIR."
echo "Try it: ./scripts/run_quality_score.sh --sample 20"
