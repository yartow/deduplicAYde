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

## AI quality scoring (optional)

Round 1/2's `blur_score` (OpenCV Laplacian variance) is computed over the
*whole frame*, not the subject — a genuinely sharp close-up against a plain
background can score as blurry (a big flat region drags the frame-average
down), and a genuinely blurry photo against a busy background can score as
sharp (background clutter inflates the average). Threshold-tuning
(`vague-threshold`, the `/vague` review page) can't fix this — it's a
metric-shape problem, not a cutoff problem.

`quality-score` runs a real no-reference AI image-quality model
([pyiqa](https://github.com/chaofengc/IQA-PyTorch)) as a second signal,
stored alongside `blur_score` rather than replacing it. Two tiers:

| Tier | Model | Notes |
|---|---|---|
| `medium` | `musiq` | Fast, moderate size, good default. |
| `heavyweight` | `clipiqa+_vitL14_512` | Full CLIP ViT-L/14 backbone, slower, first run downloads a ~1-2GB checkpoint. |

**Docker vs. native.** Set in `.env`:
```
QUALITY_SCORING_DOCKER=true    # default: CPU-only, works everywhere
QUALITY_SCORING_DOCKER=false   # native on this Mac, uses PyTorch's `mps`
                                # backend (Apple GPU) — much faster
```
Docker Desktop on Apple Silicon runs containers in a Linux VM with no
Metal/Neural-Engine passthrough, so the Docker path is always CPU-only
regardless of model choice. Native execution is the only way to get real GPU
throughput on an M1/M2/M3/M4 Mac — see `CLAUDE.md` for why this is the one
deliberate exception to this project's otherwise-strict "nothing on the
host" rule.

**⚠️ Any CPU-bound run (the Docker path, or native without `mps`) sustains
real load across multiple cores for the whole run.** Keep the lid open and
the laptop on a hard, ventilated surface until it finishes — don't put it in
a bag or close the lid. The `quality` Docker service is capped (`cpus: 4`,
`mem_limit: 6g` in `docker-compose.yml`) and thread counts are capped in
code (`QUALITY_MAX_CPU_THREADS`, default: all cores minus 2) after a run
without those caps coincided with a real overheat/crash — but capped still
means sustained real load, not safe to leave unattended and enclosed.

**Confirmation prompt.** Any run that can't use the GPU (the Docker path,
`--device cpu`, or native without `mps` available) prints exactly why and
asks you to confirm before it starts:
```
[!] This application cannot use your GPU because <reason>.
    It will run on CPU instead — capped to N thread(s), leaving M core(s)
    free for the terminal/other work — but it will still sustain real CPU
    load for the whole run.
    Keep the lid open and don't put the laptop away until it finishes.
    To stop at any time: Ctrl+C in this terminal, or from another
    terminal: <kill command>
    Continue on CPU? [y/N]
```
Pass `--yes`/`-y` to skip the prompt for scripted/unattended use — it's
otherwise required whenever stdin isn't an interactive terminal. Runs on
`mps` (GPU) skip the prompt (no CPU-pinning risk) but still print the Ctrl+C/
kill-command reminder.

**Stopping a run.** Ctrl+C in the terminal always works and is caught
cleanly: full (non-`--sample`) runs checkpoint each item as it's scored, so
an interrupt loses at most the one in-flight image — just re-run the same
command to resume. Sample runs write no DB changes regardless. If the
terminal itself is unresponsive, kill it from another terminal:
```
docker compose kill quality                        # Docker path
pkill -f "deduplicayde.cli quality-score"           # native path
```
The process also runs at lowered scheduling priority (`nice`d) so the OS
favors the terminal and other apps over it whenever CPU is contested.

**One-time native setup** (skip if staying on the Docker default):
```
./scripts/setup_quality_native_env.sh
```
Creates `.venv-quality/` (gitignored) and installs PyTorch + pyiqa there —
fully isolated from the Dockerized services, targets whatever Python 3.13/
3.14 is available on the host since the container's Python 3.12 image isn't
used for this step.

**Try before you commit.** Always invoke via the dispatcher script, which
reads `QUALITY_SCORING_DOCKER` and routes to the right place automatically:
```
./scripts/run_quality_score.sh --sample 20 --tier medium
```
Scores a random sample of 20 unscored images, writes **zero** database
changes, and opens a self-contained HTML report (thumbnails, scores, timing,
an extrapolated full-run estimate) so you can eyeball whether the tier's
results look right before spending real time on the full library. Try
`--tier heavyweight` the same way and compare. Once you're happy:
```
./scripts/run_quality_score.sh --tier heavyweight
```
runs the full pass (no `--sample`), checkpointed/resumable like every other
round, writing `quality_score`/`quality_model`/`quality_scored_at` per item.

## Status / progress

```
docker compose run cli status
```
Prints counts per round: scanned, flagged, staged, manually reviewed, deleted,
pending.
