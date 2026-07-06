#!/usr/bin/env bash
# Copy of extract_takeout_and_purge_videos.sh that also dedupes photos
# after each run. See extract_takeout.sh for the extraction behavior
# itself (unchanged below).
#
# After a successful run (with or without new zips to extract), invokes:
#   1. dedup_by_hash.py --delete against DATA_DIR to permanently remove
#      byte-identical duplicate photos/videos (Takeout exports a full
#      copy of every photo into each album folder it belongs to, plus
#      another copy into its "Photos from YYYY" date folder), logging
#      each deletion to DATA_DIR/logs/photo_dedup_<date>.jsonl.
#   2. remove_raw_jpg_duplicates.py --delete against DATA_DIR to
#      permanently remove RAW/DNG files that have a matching JPG,
#      logging each deletion to DATA_DIR/logs/raw_jpg_dedup_<date>.jsonl.
#   3. video_deleter.py --delete against DATA_DIR to permanently remove
#      every video over VIDEO_MIN_DURATION seconds, logging each deletion
#      to DATA_DIR/logs/video_delete_<date>.jsonl.
#   4. classify_movies.py, which appends any newly deleted full-length
#      movies to DATA_DIR/logs/deleted_movies.csv (existing rows,
#      including hand-reviewed ones, are always preserved as-is).
# This runs unattended with no review step - only use this copy once
# you're comfortable with that.
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
#   ./scripts/extract_takeout_and_purge_videos_and_photos.sh
#   TAKEOUT_SRC_DIR=/some/other/dir ./scripts/extract_takeout_and_purge_videos_and_photos.sh
#   VIDEO_DELETER_PATH=/path/to/video_deleter.py ./scripts/extract_takeout_and_purge_videos_and_photos.sh
#   PHOTO_DEDUPER_PATH=/path/to/dedup_by_hash.py ./scripts/extract_takeout_and_purge_videos_and_photos.sh
#   RAW_JPG_DEDUPER_PATH=/path/to/remove_raw_jpg_duplicates.py ./scripts/extract_takeout_and_purge_videos_and_photos.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VIDEO_DELETER="${VIDEO_DELETER_PATH:-$HOME/Documents/GitHub/HandyToolsMac/12. VideoDeleter/video_deleter.py}"
VIDEO_MIN_DURATION="${VIDEO_MIN_DURATION:-5}"
MOVIE_CLASSIFIER="${MOVIE_CLASSIFIER_PATH:-$HOME/Documents/GitHub/HandyToolsMac/12. VideoDeleter/classify_movies.py}"
PHOTO_DEDUPER="${PHOTO_DEDUPER_PATH:-$HOME/Documents/GitHub/HandyToolsMac/07. PhotoDuplicateFinder/dedup_by_hash.py}"
RAW_JPG_DEDUPER="${RAW_JPG_DEDUPER_PATH:-$HOME/Documents/GitHub/HandyToolsMac/07. PhotoDuplicateFinder/remove_raw_jpg_duplicates.py}"

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

dedupe_photos() {
  if [ ! -f "$PHOTO_DEDUPER" ]; then
    log "SKIP photo dedup - $PHOTO_DEDUPER not found"
  else
    log "DEDUP start - removing byte-identical duplicate photos/videos from $DATA_DIR"
    python3 "$PHOTO_DEDUPER" --root "$DATA_DIR" --delete 2>&1 | tee -a "$LOG"
    log "DEDUP done"
  fi

  if [ ! -f "$RAW_JPG_DEDUPER" ]; then
    log "SKIP RAW/JPG dedup - $RAW_JPG_DEDUPER not found"
  else
    log "RAW_DEDUP start - removing RAW/DNG files with a matching JPG from $DATA_DIR"
    python3 "$RAW_JPG_DEDUPER" --root "$DATA_DIR" --delete 2>&1 | tee -a "$LOG"
    log "RAW_DEDUP done"
  fi
}

purge_videos() {
  if [ ! -f "$VIDEO_DELETER" ]; then
    log "SKIP video purge - $VIDEO_DELETER not found"
    return
  fi
  log "PURGE start - deleting videos >= ${VIDEO_MIN_DURATION}s from $DATA_DIR"
  python3 "$VIDEO_DELETER" --root "$DATA_DIR" --min-duration "$VIDEO_MIN_DURATION" \
    --include-unreadable --delete 2>&1 | tee -a "$LOG"
  log "PURGE done"

  if [ ! -f "$MOVIE_CLASSIFIER" ]; then
    log "SKIP movie classification - $MOVIE_CLASSIFIER not found"
    return
  fi
  log "CLASSIFY start - updating $DATA_DIR/logs/deleted_movies.csv"
  python3 "$MOVIE_CLASSIFIER" --root "$DATA_DIR" 2>&1 | tee -a "$LOG"
  log "CLASSIFY done"
}

shopt -s nullglob
zips=("$SRC_DIR"/takeout-*.zip)
shopt -u nullglob

if [ ${#zips[@]} -eq 0 ]; then
  log "No takeout-*.zip files found in $SRC_DIR."
  dedupe_photos
  purge_videos
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
  needed=$((zip_size + zip_size /4))   # 1.5x zip size as a safety buffer
  # needed=$((zip_size + zip_size ))   # 1.5x zip size as a safety buffer

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
dedupe_photos
purge_videos
