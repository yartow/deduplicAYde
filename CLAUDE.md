# CLAUDE.md

Guidance for Claude Code working in this repository. Read README.md first for the
overall design; this file covers implementation conventions and guardrails.

## Tech stack

- **Python 3.12**, run only inside Docker containers (never installed on the
  host) — **except the `quality-score` AI image-quality step**, which may
  optionally run natively in a host venv (`.venv-quality/`, Python 3.13 or
  3.14 — see `scripts/setup_quality_native_env.sh`). Reason: Docker Desktop
  on Apple Silicon runs containers inside a Linux VM with no Metal/Neural-
  Engine passthrough, so a GPU-accelerated model inside a container silently
  falls back to CPU. Controlled by `QUALITY_SCORING_DOCKER` in `.env`
  (default `true` = Docker, CPU-only, matches every other step's behavior);
  set to `false` to opt into native execution via PyTorch's `mps` backend on
  the host GPU. This is the only step allowed to touch host Python or write
  outside `DATA_DIR`/the repo (pretrained weights cache to the host's
  `~/.cache/torch`, same directory PyTorch always uses) — don't extend either
  exception to any other command without discussing it first.
  **Confirmed live, a real gotcha for this step specifically**: `local_path`
  in `media_items` is always stored `/data/`-prefixed, because every other
  round (0-4) runs inside a Docker container where `DATA_DIR=/data` is the
  bind-mount target — that prefix is only ever valid inside a container.
  `quality-score` is the first code path that can run natively, where
  `DATA_DIR` is the *real* host path instead, so `/data/...` doesn't exist
  there at all — every eligible-file lookup silently returned zero results
  until `quality_score.py::_resolve_local_path` was added to re-root the
  stored path onto the real `DATA_DIR` when the two differ (a no-op inside
  Docker, where they're the same). Any future native code path reading
  `local_path` needs this same re-rooting — it's not specific to
  quality-score's logic, it's a property of how every path in the DB was
  written.
- **OpenCV** (`opencv-python-headless`) for blur/edge detection.
- **Tesseract OCR** (`pytesseract` + the `tesseract-ocr` apt package) for receipt
  text-density detection.
- **imagehash** (phash) for duplicate detection.
- **google-api-python-client** + **google-auth-oauthlib** for the Photos Library API
  — **album creation only**. Google's March 2024 policy change restricts OAuth
  tokens (even from rclone's pre-registered client) to `photoslibrary.readonly
  .appcreateddata`: an app can only see/touch media items it uploaded itself, so
  `mediaItems.list/search` can never enumerate an existing library and
  `albums.batchAddMediaItems` can never be given a valid `mediaItemId` for a
  pre-existing item. Creating a fresh, empty, app-owned album still works — that's
  the only thing this API is used for now. See rule 1 below for what replaced the
  rest.
- **Playwright (Python)** for everything that needs to see or act on existing
  library items: locating a flagged local file on photos.google.com, adding it to
  a review album, and trashing staged items — all driven as the logged-in human
  user, which isn't subject to the OAuth scope restriction above. Confirmed live
  that Google's login flow blocks sign-ins performed inside a CDP-attached
  (Playwright-controlled) browser ("this browser or app may not be secure"),
  independent of DOM selectors. Fix: `docker compose run -p 6080:6080 delete
  login` launches the bundled Chromium binary directly via `subprocess`
  (bypassing Playwright/CDP entirely, so no automation fingerprint) against a
  persistent profile at `secrets/chrome-profile/`; the user signs into Google
  there via noVNC, once. Every later `stage`/`delete` run reuses that
  already-authenticated profile through `launch_persistent_context` — Playwright
  never performs the sign-in handshake itself. Real Chrome (`channel="chrome"`)
  isn't an option: no Linux arm64 build exists, and this runs on Apple Silicon;
  the same bundled Chromium binary is used for both the manual login and the
  automated runs, so there's no cross-browser profile-format risk either. See
  `browser.py`.
- **SQLite** (stdlib `sqlite3`, or `sqlmodel`/`peewee` if a lightweight ORM helps)
  for all checkpoint/state tracking.
- **FastAPI + a minimal HTML/JS frontend** (or Flask, whichever is faster to ship)
  for the Round 4 side-by-side duplicate review app. Keep this simple — no frontend
  build step, no framework beyond what's needed for two images and four buttons.
- **Docker Compose** with at least two services: `cli` (batch rounds) and `review`
  (the Round 4 web app). Decide during Round 0 whether the Playwright deletion step
  needs its own service with a noVNC port exposed, or can share the `cli` service.
- **pyiqa (IQA-PyTorch) + PyTorch** for the optional `quality-score` step's
  AI no-reference image-quality scoring — a second signal alongside
  `blur_score`/`edge_density` (see architecture rule 11), run either in the
  CPU-only `quality` Docker service or natively via PyTorch's `mps` backend
  (see tech-stack bullet 1 above). Two tiers, chosen via `--tier`/
  `QUALITY_TIER`: `medium` (`musiq`) and `heavyweight`
  (`clipiqa+_vitL14_512`, a full CLIP ViT-L/14 backbone at 512px — much
  slower, especially on CPU). Verify exact pyiqa model-id strings against
  `pyiqa.list_models(metric_mode='NR')` before relying on them; pyiqa's
  registry has changed spelling/availability across releases. Confirmed live:
  pyiqa's `imread2pil` only accepts a path string, bytes, or a `PIL.Image`
  (not a raw array) and reads via `PIL.Image.open()` internally even for
  path strings — so every image is pre-loaded as a PIL Image and passed in
  directly (`quality_score.py::_load_image_pil`), with `pillow_heif.
  register_heif_opener()` called once at module import so `.heic`/`.heif`
  files open correctly (mirroring why `detection.py`'s `_open_cv_image`
  needs the same registration for its own separate cv2-based path).

## Architecture rules

1. **Every round must be idempotent and resumable.** Before processing any item,
   check `state.db` for whether it's already been handled; never reprocess
   completed work. Use a single source of truth schema (a `media_items` table
   keyed by an internal row id, identity anchored to the local file path — not a
   Google `mediaItemId`, since one can no longer be obtained for pre-existing
   items — with columns for detection results, staging status, review status,
   deletion status, timestamps).
2. **No deletion via the Photos API.** The only API call used against the Photos
   Library API is album creation (`albums.insert`); it has no delete endpoint
   and, per rule 1's OAuth restriction, can't add pre-existing items to an
   album either. Locating items and adding them to a review album, as well as
   actually trashing staged items, are both Playwright automation against the
   real web UI — the only code paths that touch existing library items at all.
   Note: `albums.list` (find-by-title) also 403s under the restricted scope —
   confirmed live on the first real `stage` run — even though Google's docs
   list `photoslibrary.readonly.appcreateddata` as valid for it. Don't add a
   list/search-based lookup back to `photos_api.py`; cross-run album
   idempotency is tracked locally in the `albums` table instead (see
   `staging.get_or_create_album`, which every caller needing an album must go
   through).
3. **Dry-run by default.** Any command with a destructive effect (staging into an
   album that will later be auto-deleted, triggering the Playwright delete flow,
   deleting local files) must require an explicit flag to actually execute, and
   must default to printing/logging what it would do.
4. **Receipts vs. vague items are handled differently.** Receipts can go through
   automated staging + automated deletion after a dry-run confirmation, since
   they're low-risk and you've described high confidence in the OCR/contour
   heuristic. Vague/blurry items must always pause for manual visual review inside
   the staged Google Photos album before the deletion step runs against them.
5. **Round 3 reconciliation must never delete the receipts folder.** When a
   `label='receipt'` row is confirmed deleted from Google Photos, Round 3 moves
   its local file out of `library/` into `/data/receipts/` instead of deleting
   it — receipts are kept locally forever even though the cloud copy is gone.
   Round 3 is the only code path allowed to write into `/data/receipts/`; once a
   file lives there it's permanent and excluded from the offline-deletion sync
   on every subsequent run.
6. **Round 4 duplicate confirmation requires full-resolution side-by-side display**,
   not thumbnails — load both full images in the browser at a size that fits the
   viewport, not cropped/scaled-down previews.
7. **Checkpoint frequently** (e.g. after each batch of N items, not just at the end
   of a round) so a Ctrl+C or container stop loses at most a small amount of
   progress.
8. **Logging**: write structured logs (one line per item processed, with outcome)
   to `/data/logs/`, in addition to the SQLite state, so progress can be audited
   without querying the database.
9. **Why there's no `mediaItemId` in the schema**: confirmed live against a real
   account (traced raw HTTP request/response bodies to
   `photoslibrary.googleapis.com` — every page came back with a valid
   `nextPageToken` but zero `mediaItems`, with and without
   `includeArchivedMedia`) that the OAuth scope granted for this app —
   `photoslibrary.readonly.appcreateddata` — only exposes items the app itself
   uploaded. This is Google's March 2024 API policy change, not an rclone/config
   bug, and it applies regardless of which OAuth client is used (rclone's
   pre-registered client is not exempt for fresh authorizations). Re-requesting
   the broader `photoslibrary.readonly` scope is not a viable fix — Google isn't
   granting it to personal-use OAuth consents. Don't attempt to re-add
   cloud-side enumeration or ID-based staging without first re-verifying this
   restriction still holds.
10. **`local_timestamp` is always naive local time in `ACCOUNT_TIMEZONE`, never
    UTC.** Confirmed live: locate_stage.py matches local files to Google Photos
    UI items by comparing `local_timestamp` against a timestamp parsed out of
    each grid tile's aria-label, which Google Photos always renders in the
    account's local display timezone with no UTC/offset indicator. Takeout
    sidecar timestamps are natively a UTC epoch; storing them as UTC (with a
    trailing "Z") broke every single sidecar-sourced match — both because the
    trailing "Z" made exact-string comparison against the UI's un-suffixed
    timestamps always fail, and because for capture times close to local
    midnight, the UTC value could land on the wrong calendar day entirely once
    converted. `round0.py`'s `_read_sidecar_ts` now converts to
    `ACCOUNT_TIMEZONE` (env var, default `Europe/Amsterdam`) and stores a naive
    string, matching EXIF's format. Don't reintroduce UTC storage for sidecar
    timestamps without re-verifying the UI still shows local time with no
    offset marker.
11. **`quality-score --sample N` never writes to the DB.** Unlike
    `round0 --limit` (which processes real, DB-persisted rows), `--sample`
    draws a *random* subset (`random.sample`, not first-N) purely to preview
    timing and output quality before committing to a full run over the whole
    library. It writes only a self-contained HTML report to `DATA_DIR/logs/`
    (matches rule 3's dry-run-by-default spirit even though this command
    isn't destructive). Only the full (`--sample`-less) run writes
    `quality_score`/`quality_model`/`quality_scored_at` to `media_items`.

## Things to ask the user about before proceeding

- Before first OAuth login / before any code touches real Google account
  credentials.
- Before running the Playwright locate/stage flow (`stage`) or deletion flow
  (`delete`) for the first time, and before pointing either at anything larger
  than a small test album — their DOM selectors are best-effort and unverified
  against the live site until run once and watched via noVNC. Also before the
  very first Playwright run of any kind: `docker compose run -p 6080:6080
  delete login` must be run once so the persistent profile at
  `secrets/chrome-profile/` exists and is signed into Google — see
  `browser.py`'s `manual_login()`.
- Before running `--delete-receipts` or any other non-dry-run flag against the
  full library for the first time.
- Before installing any new system dependency that isn't already covered by the
  Docker images.
- Before running `quality-score --tier heavyweight` for the first time: it
  downloads a large pretrained checkpoint (CLIP ViT-L/14, order of 1-2GB) —
  confirm there's room and patience before running it unattended over the
  full library.
- Before any `quality-score` run that will execute on CPU (the Docker path,
  `QUALITY_SCORING_DOCKER=true`, always; or the native path if `mps` isn't
  available): tell the user explicitly not to close the lid or put the
  laptop away while it runs. **Confirmed live**: an uncapped `--tier
  heavyweight` run through the Docker `quality` service let PyTorch pin
  every core Docker Desktop's Linux VM had access to (up to 10 on an M1 Max
  — `com.docker.virtualization --cpus 10`), sustained for the whole run,
  and that coincided with a real overheat/crash while the lid was closed.
  `quality` now has a hard `cpus`/`mem_limit` cap in `docker-compose.yml`
  and `quality_score.py` caps `OMP_NUM_THREADS`/`MKL_NUM_THREADS`/
  `torch.set_num_threads` (`QUALITY_MAX_CPU_THREADS` env var to override,
  default cpu_count-2) and prints a warning before any CPU-bound run — but
  those bound the worst case, they don't make sustained load safe to leave
  enclosed. Don't remove or loosen these caps without an equivalent
  safeguard, and always surface the warning to the user rather than
  suppressing it.
- **A second, distinct incident, confirmed live**: even on the `mps` (GPU)
  path — no CPU pinning, exactly the "safe" path above — a `--tier
  heavyweight` (`clipiqa+_vitL14_512`) run raised `Invalid buffer size:
  493.81 GiB` on an ordinary 18-megapixel JPEG (5184x3456, nothing wrong
  with the file). That number matches a ViT self-attention matrix sized off
  the raw image's patch grid rather than the model's 512px working
  resolution — some path in pyiqa's preprocessing wasn't downsizing first.
  The *attempted* allocation (not just the eventual exception) is what
  caused a 13-minute stall and system-wide swap-thrashing/keystroke lag
  reported live — by the time the exception is caught, the memory-pressure
  damage is already done, so the existing per-item try/except couldn't have
  prevented it. Fixed by capping every image's longest side to
  `QUALITY_MAX_IMAGE_DIM` (default 1024px, via `PIL.Image.thumbnail`) in
  `_load_image_pil` *before* it ever reaches pyiqa, applied unconditionally
  to every tier/model — plus a `SIGALRM`-based per-item timeout
  (`QUALITY_ITEM_TIMEOUT_SECONDS`, default 60s) in `_score_one` as a
  backstop for any other stall this specific fix doesn't cover (the alarm
  can't preempt a single already-in-flight C-level call like a stuck
  malloc, so it's a backstop, not the primary fix). Don't remove the resize
  cap without re-verifying this specific model/pyiqa-version combination no
  longer needs it.

## Things NOT to do

- Don't attempt to find or use an undocumented/private Google Photos delete
  endpoint — it doesn't exist for arbitrary library items; don't go looking for
  workarounds that violate Google's ToS (e.g. reverse-engineered internal APIs).
- Don't write anything to the host filesystem outside the mounted `DATA_DIR` and
  the repo itself — except `quality-score`'s native path, which writes
  `.venv-quality/` inside the repo and caches pretrained weights to the
  host's `~/.cache/torch` (PyTorch's own standard cache location); see
  tech-stack bullet 1.
- Don't auto-empty the Trash without an explicit separate command/flag — the
  60-day recovery window is intentional safety margin.
- Don't build this as a long-running background daemon; it should be explicit,
  user-invoked commands per round.

## Suggested build order

1. Repo scaffolding, Docker Compose, `.env.example`, `secrets/` gitignored.
2. SQLite schema + a `cli status` command (even before other rounds exist).
3. Round 0: local-only cataloging of everything under `DATA_DIR/library/`
   (identity is the file path itself — no cloud enumeration is possible, see
   tech-stack and rule 1 above). Resolve each file's true timestamp: (a) EXIF
   `DateTimeOriginal` via `exifread`, (b) `photoTakenTime` from the Takeout JSON
   sidecar. **Never use filesystem mtime/ctime** — Takeout extraction corrupts
   these. Files with neither source are flagged `no_timestamp` in the logs for
   manual review. -> populate `media_items` table.
4. Round 1/2 detection (OpenCV blur/edge + Tesseract OCR) -> classification only,
   no cloud interaction.
5. Playwright locate + stage flow: given a flagged local file, find it on
   photos.google.com (date + filename) and add it to a review album (created via
   the API). Tested first against a small manually-created test album before
   being wired into the receipts/vague flow, same as the deletion flow below.
6. Playwright deletion flow, tested first against a small manually-created test
   album before being wired into the receipts/vague flow.
7. Round 3 local cleanup (mirror confirmed cloud deletions to local files) +
   Round 4 phash clustering + FastAPI/Flask review app.

Confirm Round 0 works end-to-end against real local files before writing any
code that stages or deletes anything.
