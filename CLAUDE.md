# CLAUDE.md

Guidance for Claude Code working in this repository. Read README.md first for the
overall design; this file covers implementation conventions and guardrails.

## Tech stack

- **Python 3.12**, run only inside Docker containers (never installed on the host).
- **OpenCV** (`opencv-python-headless`) for blur/edge detection.
- **Tesseract OCR** (`pytesseract` + the `tesseract-ocr` apt package) for receipt
  text-density detection.
- **imagehash** (phash) for duplicate detection.
- **google-api-python-client** + **google-auth-oauthlib** for the Photos Library API
  (mapping, search, album staging — never for deletion, since no delete endpoint
  exists).
- **Playwright (Python)** for the one piece that needs real browser automation:
  actually trashing items via photos.google.com.
- **SQLite** (stdlib `sqlite3`, or `sqlmodel`/`peewee` if a lightweight ORM helps)
  for all checkpoint/state tracking.
- **FastAPI + a minimal HTML/JS frontend** (or Flask, whichever is faster to ship)
  for the Round 4 side-by-side duplicate review app. Keep this simple — no frontend
  build step, no framework beyond what's needed for two images and four buttons.
- **Docker Compose** with at least two services: `cli` (batch rounds) and `review`
  (the Round 4 web app). Decide during Round 0 whether the Playwright deletion step
  needs its own service with a noVNC port exposed, or can share the `cli` service.

## Architecture rules

1. **Every round must be idempotent and resumable.** Before processing any item,
   check `state.db` for whether it's already been handled; never reprocess
   completed work. Use a single source of truth schema (e.g. a `media_items` table
   keyed by Google `mediaItemId`, with columns for local path, detection results,
   staging status, review status, deletion status, timestamps).
2. **No deletion via the Photos API.** Only `albums.batchAddMediaItems` (staging)
   and read endpoints (`mediaItems.list/search`, `albums.get`) are used against the
   API. The only code path that performs real deletion is the Playwright automation
   against the actual web UI.
3. **Dry-run by default.** Any command with a destructive effect (staging into an
   album that will later be auto-deleted, triggering the Playwright delete flow,
   deleting local files) must require an explicit flag to actually execute, and
   must default to printing/logging what it would do.
4. **Receipts vs. vague items are handled differently.** Receipts can go through
   automated staging + automated deletion after a dry-run confirmation, since
   they're low-risk and you've described high confidence in the OCR/contour
   heuristic. Vague/blurry items must always pause for manual visual review inside
   the staged Google Photos album before the deletion step runs against them.
5. **Round 3 reconciliation must never delete the receipts folder.** Local files
   under `/data/receipts/` are permanent and excluded from the offline-deletion
   sync, even though their corresponding cloud items were deleted.
6. **Round 4 duplicate confirmation requires full-resolution side-by-side display**,
   not thumbnails — load both full images in the browser at a size that fits the
   viewport, not cropped/scaled-down previews.
7. **Checkpoint frequently** (e.g. after each batch of N items, not just at the end
   of a round) so a Ctrl+C or container stop loses at most a small amount of
   progress.
8. **Logging**: write structured logs (one line per item processed, with outcome)
   to `/data/logs/`, in addition to the SQLite state, so progress can be audited
   without querying the database.

## Things to ask the user about before proceeding

- Before first OAuth login / before any code touches real Google account
  credentials.
- Before running the Playwright deletion flow for the first time, and before
  pointing it at anything larger than a small test album.
- Before running `--delete-receipts` or any other non-dry-run flag against the
  full library for the first time.
- Before installing any new system dependency that isn't already covered by the
  Docker images.

## Things NOT to do

- Don't attempt to find or use an undocumented/private Google Photos delete
  endpoint — it doesn't exist for arbitrary library items; don't go looking for
  workarounds that violate Google's ToS (e.g. reverse-engineered internal APIs).
- Don't write anything to the host filesystem outside the mounted `DATA_DIR` and
  the repo itself.
- Don't auto-empty the Trash without an explicit separate command/flag — the
  60-day recovery window is intentional safety margin.
- Don't build this as a long-running background daemon; it should be explicit,
  user-invoked commands per round.

## Suggested build order

1. Repo scaffolding, Docker Compose, `.env.example`, `secrets/` gitignored.
2. SQLite schema + a `cli status` command (even before other rounds exist).
3. Round 0: OAuth flow + `mediaItems.list` pagination + local file matching
   (filename as the primary key; timestamp disambiguation when multiple local files
   share the same name; phash as a final tiebreaker). **Never use filesystem
   mtime/ctime** — Takeout extraction corrupts these. True timestamp priority:
   (a) EXIF `DateTimeOriginal` via `exifread`, (b) `photoTakenTime` from the
   Takeout JSON sidecar. Files with neither source are flagged `no_timestamp` in
   the logs for manual review. -> populate `media_items` table.
4. Round 1/2 detection (OpenCV blur/edge + Tesseract OCR) -> classification ->
   staging via `albums.batchAddMediaItems`.
5. Playwright deletion flow, tested first against a small manually-created test
   album before being wired into the receipts/vague flow.
6. Round 3 reconciliation diff + local deletion sync.
7. Round 4 phash clustering + FastAPI/Flask review app.

Confirm Round 0 works end-to-end against the real account (read-only) before
writing any code that stages or deletes anything.
