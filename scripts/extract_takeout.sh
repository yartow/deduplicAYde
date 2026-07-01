#!/usr/bin/env bash
# Extract Google Takeout zip parts into DATA_DIR/library, one at a time,
# deleting each zip only after it has been verified to extract cleanly.
#
# Uses `ditto` rather than `unzip`: Apple's bundled unzip mis-decodes
# accented filenames that Takeout doesn't flag as UTF-8 (e.g. "Liberté"),
# producing an invalid path and failing with a misleading "disk full?"
# error. `ditto` is the same engine Finder/Archive Utility use and handles
# this correctly, and it merges into an existing destination instead of
# creating numbered "Takeout (1)" folders on conflict.
#
# Safe to re-run any time new parts land in Downloads — already-extracted
# zips are gone (deleted after success), so it only processes what's left.
# Skips any zip whose size is still changing (still downloading), and
# refuses to start a zip if there isn't at least 1.5x its size free on disk.
#
# Usage:
#   ./scripts/extract_takeout.sh
#   TAKEOUT_SRC_DIR=/some/other/dir ./scripts/extract_takeout.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -z "${DATA_DIR:-}" ] && [ -f "$REPO_ROOT/.env" ]; then
  DATA_DIR=$(grep -E '^DATA_DIR=' "$REPO_ROOT/.env" | tail -1 | cut -d= -f2-)
fi
if [ -z "${DATA_DIR:-}" ]; then
  echo "DATA_DIR is not set (checked \$DATA_DIR and $REPO_ROOT/.env)." >&2
  exit 1
fi

SRC_DIR="${TAKEOUT_SRC_DIR:-$HOME/Downloads}"
DEST_DIR="$DATA_DIR/library"
LOG_DIR="$DATA_DIR/logs"
mkdir -p "$LOG_DIR" "$DEST_DIR"
LOG="$LOG_DIR/extract_takeout.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

shopt -s nullglob
zips=("$SRC_DIR"/takeout-*.zip)
shopt -u nullglob

if [ ${#zips[@]} -eq 0 ]; then
  log "No takeout-*.zip files found in $SRC_DIR."
  exit 0
fi

processed=0
for zip_path in "${zips[@]}"; do
  z=$(basename "$zip_path")

  size1=$(stat -f%z "$zip_path")
  sleep 2
  size2=$(stat -f%z "$zip_path" 2>/dev/null || echo -1)
  if [ "$size1" != "$size2" ]; then
    log "SKIP $z - still being written (size changed from $size1 to $size2 bytes)"
    continue
  fi

  log "START $z"

  zip_size=$size2
  avail_kb=$(df -k "$DEST_DIR" | tail -1 | awk '{print $4}')
  avail_bytes=$((avail_kb * 1024))
  needed=$((zip_size + zip_size / 2))   # 1.5x zip size as a safety buffer

  if [ "$avail_bytes" -lt "$needed" ]; then
    log "ABORT $z - not enough free space (avail=$avail_bytes needed=$needed). Free up space and re-run; earlier zips already extracted are untouched."
    exit 1
  fi

  if ditto -x -k "$zip_path" "$DEST_DIR" >> "$LOG" 2>&1; then
    log "OK $z - deleting zip"
    rm -f "$zip_path"
    processed=$((processed + 1))
  else
    log "FAIL $z (ditto failed, see $LOG) - NOT deleting, stopping"
    exit 1
  fi
done

log "DONE - extracted $processed zip(s)"
