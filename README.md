# Google Photos Cleanup Pipeline

A local, resumable, Dockerized pipeline to find and remove junk (receipts, blurry/
low-content photos, duplicates) from a large Google Photos library, while keeping a
human verification step before anything is permanently deleted.

## Why this exists

- Google Photos Library API can stage items into albums (`albums.batchAddMediaItems`)
  and enumerate the library (`mediaItems.list/search`) — but only for items **the
  requesting app itself uploaded**. This is a Google policy change (March 2024);
  it applies regardless of which OAuth client is used, and there's no way to get a
  broader grant for personal-use projects. Since this app has never uploaded
  anything, the API can neither list the existing library nor stage pre-existing
  items into an album. The only thing it can still do is create a fresh, empty
  album. It also has **no endpoint that deletes existing media items** — that only
  happens through the web/mobile UI's trash action.
- So: local cataloging and detection are entirely local (no network); *locating* a
  flagged item on photos.google.com, *staging* it into a review album, and
  *deleting* staged items are all done via the same mechanism — browser automation
  (Playwright) driving the real photos.google.com UI as the logged-in human user,
  which isn't subject to the API's OAuth scope restriction at all.
- The library (453GB) is too large to fully download at once alongside everything
  else on a 620GB MacBook Pro, so the library is processed in two halves (by date
  range), each going through detection, staging, manual review, and deletion before
  moving to the second half.

## High-level flow

```
Round 0  Catalog local files under DATA_DIR/library/: filename, resolved EXIF/
         sidecar timestamp, path. No cloud interaction (see "Why this exists").
Round 1  Download first half (Takeout) -> detect receipts & vague photos locally
Round 2  Same as Round 1, for the second half of the library
Stage    Locate each detected item on photos.google.com (via Playwright) and add
         it to a "Receipts" / "Vague" review album (created via the API)
Delete   Trash staged items via Playwright (receipts: auto after dry-run confirm;
         vague: only after manual visual review in the album)
Round 3  Local-only sweep: delete local copies of anything already confirmed
         trashed by the Delete step (except files already moved into receipts/)
Round 4  Perceptual-hash dedup pass across remaining local files; side-by-side
         full-resolution review web app to confirm and act on duplicate pairs
```

Every round is checkpointed in a local SQLite database so it can be interrupted
(Ctrl+C, container stop, closing the laptop) and resumed without reprocessing
completed items or losing track of what's been staged/deleted/reviewed.

## Why Docker

Keeps Tesseract, OpenCV system dependencies, Playwright's browser binaries, and
Python dependencies off the host Mac entirely. Two options, pick in CLAUDE.md:

- A single `docker compose run` service for the CLI/batch rounds (0-3 detection +
  staging), mounting the external hard drive as a volume.
- A second service for the Round 4 review web app, exposing a local port (e.g.
  `localhost:8000`) so it can be opened in a normal browser on the host while the
  actual image files stay on the mounted external drive.

Playwright's browser automation (the deletion step) needs to either run headed
inside the container with a VNC/noVNC viewer exposed on a local port (so you can
watch and intervene), or run headed via X11 passthrough if preferred — pick whichever
Claude Code finds simpler to set up reliably on macOS.

## Storage layout (external hard drive, mounted into containers)

```
/data/
  takeout_half1/        # raw Takeout export, half 1 (deleted after extraction)
  takeout_half2/
  library/              # extracted, organized photos (by original Takeout folder)
  receipts/             # confirmed receipt images moved out, kept permanently
  to_review/            # flagged "vague" candidates pending visual confirmation
  state.db              # SQLite checkpoint/mapping database
  logs/
```

## Setup

1. Install Docker Desktop on the Mac (the only thing installed on the host).
2. Create a Google Cloud project, enable the Photos Library API, create OAuth 2.0
   desktop credentials, download `client_secret.json` into `secrets/` (gitignored).
3. Plug in the external hard drive; set `DATA_DIR` in `.env` to its mount path.
4. `docker compose build`
5. As Google Takeout zip parts finish downloading into `~/Downloads`, run
   `./scripts/extract_takeout.sh` to extract them into `DATA_DIR/library/` and
   delete each zip after a verified successful extraction. Safe to re-run any
   time — only processes zips still sitting in `~/Downloads`. Uses `ditto`
   (not `unzip`) since Takeout's non-UTF8-flagged accented filenames trip up
   Apple's `unzip`.
6. `docker compose run cli round0` — catalogs whatever is already extracted into
   `library/` so far (re-run after each Takeout import).
7. Before the first `stage` or `delete` run (once, not per-round):
   ```
   docker compose run -p 6080:6080 delete login
   ```
   Opens a plain, non-automated browser window (watch at
   `http://localhost:6080/vnc.html`) — log into your Google account there, then
   just close the window. This exists because Google blocks sign-ins performed
   inside an automation-controlled (Playwright/CDP) browser; `login` launches
   the browser binary directly, bypassing Playwright, so the sign-in itself
   never touches CDP. Every later `stage`/`delete` run reuses that
   already-authenticated profile instead of signing in itself. See `browser.py`
   and `CLAUDE.md` for why.

## Running a round

```
docker compose run cli round1                                     # detect (first half)
docker compose run -p 6080:6080 delete stage --purpose=receipt --dry-run   # preview staging
docker compose run -p 6080:6080 delete stage --purpose=receipt --no-dry-run
docker compose run -p 6080:6080 delete delete --album=receipts --no-dry-run --confirm
docker compose up review                                          # open the Round 4 web app
```

Every command is safe to stop and re-run; it picks up from `state.db`. The `stage`
and `delete` commands require the Playwright-capable `delete` service (watch
progress at `http://localhost:6080/vnc.html`) — their DOM selectors are
best-effort and should be validated against a small test album first.

## Status / progress

```
docker compose run cli status
```
Prints counts per round: scanned, flagged, staged, manually reviewed, deleted,
pending.
