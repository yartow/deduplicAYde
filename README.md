# Google Photos Cleanup Pipeline

A local, resumable, Dockerized pipeline to find and remove junk (receipts, blurry/
low-content photos, duplicates) from a large Google Photos library, while keeping a
human verification step before anything is permanently deleted.

## Why this exists

- Google Photos Library API can stage items into albums (`albums.batchAddMediaItems`)
  for **any** item in the library, but it has **no endpoint that deletes existing
  media items**. Actual deletion only happens through the web/mobile UI's trash
  action.
- So: detection and staging are done via the API; actual deletion is done via
  browser automation (Playwright) driving the real photos.google.com UI, which is
  the only thing that genuinely trashes items and frees storage.
- The library (453GB) is too large to fully download at once alongside everything
  else on a 620GB MacBook Pro, so the library is processed in two halves (by date
  range), each going through detection, staging, manual review, and deletion before
  moving to the second half.

## High-level flow

```
Round 0  Build a mapping table: Google Photos mediaItemId <-> local filename/hash
Round 1  Download first half (Takeout) -> detect receipts & vague photos locally
         -> stage matches into "Receipts" / "Vague" albums via API
         -> delete via Playwright (receipts: auto after dry-run confirm;
            vague: only after manual visual review in the album)
Round 2  Same as Round 1, for the second half of the library
Round 3  Re-pull full mediaItems list; diff against the mapping table to find what
         was actually deleted in Google Photos; mirror those deletions locally
         (except files already moved into the receipts folder)
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
5. `docker compose run cli round0` — builds the ID mapping for whatever is already
   matched/downloaded so far (re-run after each Takeout import).

## Running a round

```
docker compose run cli round1 --half=1 --dry-run     # detect + stage only, no deletion
docker compose run cli round1 --half=1 --delete-receipts
docker compose run cli review                        # open the Round 4 web app
```

Every command is safe to stop and re-run; it picks up from `state.db`.

## Status / progress

```
docker compose run cli status
```
Prints counts per round: scanned, flagged, staged, manually reviewed, deleted,
pending.
