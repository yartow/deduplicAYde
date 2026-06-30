# deduplicAYde — Step-by-Step Runbook

Work through this top to bottom. Each section has a "pause and verify" checkpoint before you move on. Nothing destructive runs without an explicit flag, so you can always stop and check `status` before proceeding.

---

## Prerequisites

- **Docker Desktop** installed and running on your Mac (the only thing installed on the host).
- Your external hard drive plugged in and mounted (e.g. `/Volumes/MyDrive`).
- A Google account with the library you want to clean (`yartow@gmail.com`).

---

## Part 1 — One-time setup

### 1.1 Google Cloud credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a new project (e.g. `deduplicayde`).
2. Enable the **Photos Library API** for that project.
3. Go to **APIs & Services → Credentials** and create an **OAuth 2.0 Client ID** of type *Desktop app*.
4. Download the JSON file and save it as:
   ```
   secrets/client_secret.json
   ```
   That directory is gitignored — nothing in `secrets/` will ever be committed.

### 1.2 Configure your environment

```bash
cp .env.example .env
```

Open `.env` and set `DATA_DIR` to the absolute path of a folder on your external drive:

```
DATA_DIR=/Volumes/MyDrive/photos_cleanup
```

Create that folder if it doesn't exist yet:

```bash
mkdir -p /Volumes/MyDrive/photos_cleanup/library
mkdir -p /Volumes/MyDrive/photos_cleanup/receipts
mkdir -p /Volumes/MyDrive/photos_cleanup/logs
```

### 1.3 Build the Docker images

```bash
docker compose build
```

This takes a few minutes the first time (downloads Tesseract, OpenCV, Playwright, etc.). Nothing touches your Google account yet.

### 1.4 Authenticate with Google (Photos API — for album staging)

```bash
docker compose run --rm -p 8080:8080 cli auth
```

The terminal will print a URL. Open it in your browser, sign in with `yartow@gmail.com`, and grant the requested Photos permissions. The token is saved to `secrets/token.json` and reused automatically — you won't be asked again unless it expires.

> **Check:** You should see `Authenticated successfully. Token saved.` in the terminal.

### 1.5 Set up rclone (for library enumeration)

Google's Photos API restricts full-library listing for new projects (post-March 2024 deprecation). rclone uses its own pre-authorised credentials that bypass this restriction. This is a **separate** OAuth flow from step 1.4.

```bash
docker compose run --rm -p 53682:53682 cli rclone-setup
```

Follow the prompts:
1. Enter `n` → New remote
2. Name it `gphotos`
3. Choose `Google Photos` from the storage type list
4. Leave `client_id` and `client_secret` **blank** (press Enter) — rclone uses its own built-in credentials
5. Leave `read_only` as `false`
6. Choose `y` for auto config → rclone will print a URL; open it in your browser and sign in with `yartow@gmail.com`

The token is saved to `secrets/rclone.conf`.

> **Check:** You should see `Done. Config saved.` in the terminal and a `secrets/rclone.conf` file containing a `[gphotos]` block.

---

## Part 2 — Round 0: Build the ID mapping

Round 0 fetches your full Google Photos library via rclone and matches each item to a local file under `/data/library/`. It never stages or deletes anything.

### 2.1 Smoke test (200 items)

Before running against your full library, test with a tiny slice:

```bash
docker compose run --rm cli round0 --limit=200
docker compose run --rm cli status
```

The `status` output should show ~200 items in the DB and some fraction matched to local files. If `local_path` is always `NULL`, your `DATA_DIR/library/` may be empty or the filenames don't match what's in Takeout — check the logs in `/data/logs/`.

### 2.2 Import your first Takeout batch

Google Photos Takeout splits large libraries into multiple zip files. Download your first batch (roughly the older half of your library by date) and extract everything into:

```
/Volumes/MyDrive/photos_cleanup/library/
```

Keep the folder structure from the zip intact. Round 0 searches recursively.

### 2.3 Run Round 0 in full

```bash
docker compose run --rm cli round0
```

This fetches your entire Photos library via rclone (~453 GB = several hundred thousand items) and matches each item to a local file. Depending on your internet connection the rclone listing alone can take **5–20 minutes**, then matching runs locally. If interrupted mid-match, re-running is safe — already-matched items are skipped.

> **Check before continuing:**
> ```bash
> docker compose run --rm cli status
> ```
> You want to see a meaningful number of items with `Mapped to local` before running detection. Items that show `unmatched` are cloud-only (Takeout hasn't been imported yet) — that's expected for the second batch.

---

## Part 3 — Round 1: Detect + Stage (first Takeout batch)

Detection reads your local files with OpenCV and Tesseract. Staging pushes `mediaItemId`s into Google Photos albums via the API. **Nothing is deleted in this step.**

### 3.1 Dry run first

```bash
docker compose run --rm cli round1 --dry-run
```

This prints every file's classification (`receipt`, `vague`, `ok`) and what would be staged, without touching Google Photos. Review the log output in `/data/logs/round1_*.jsonl` to spot-check:
- Are receipts being correctly identified? (Look at a few filenames.)
- Are the `vague` hits things you'd actually want to delete?

If the thresholds feel off, adjust them in `.env` (see `.env.example` for the variable names) and re-run the dry run. The defaults work well for typical phone photos.

### 3.2 Stage items for real

Once you're satisfied with the dry-run output:

```bash
docker compose run --rm cli round1 --no-dry-run
```

This creates two albums in your Google Photos account:
- **deduplicAYde – Receipts**
- **deduplicAYde – Vague**

…and adds the flagged items to the appropriate album. You can open Google Photos right now and inspect both albums.

> **Mandatory check for Vague items:** Open the **deduplicAYde – Vague** album in Google Photos and scroll through it. These are the blurry / low-content shots the algorithm flagged. Remove anything you want to keep (just remove it from the album — don't delete). You must do this before running the deletion step for vague items.

---

## Part 4 — Delete staged items (first batch)

Deletion uses Playwright to drive `photos.google.com` in a real browser. You can watch it live.

### 4.1 Dry run

```bash
docker compose run delete delete --album=receipts --dry-run
```

Prints the list of items that would be trashed.

### 4.2 Delete receipts (auto, after dry-run confirm)

Receipts are low-risk — the OCR confidence is high and you've seen the dry-run list.

```bash
docker compose run --rm -p 6080:6080 delete delete --album=receipts --no-dry-run --confirm
```

Open **http://localhost:6080/vnc.html** in your browser to watch the Playwright browser work. If Google asks you to log in during the automation, the terminal will pause and ask you to complete the login in the noVNC window, then press Enter.

> Items are moved to **Google Photos Trash** — they are NOT permanently gone. You have a 60-day recovery window. Do not empty the Trash manually.

### 4.3 Delete vague items (after your manual review)

Only run this after you've reviewed and culled the Vague album in Google Photos (step 3.2 above).

```bash
docker compose run --rm -p 6080:6080 delete delete --album=vague --no-dry-run --confirm
```

The script will prompt you to confirm you've done the visual review before it proceeds.

---

## Part 5 — Round 2: Second Takeout batch

Import your second Takeout batch into the same `/data/library/` directory, then repeat Parts 2–4 for the second half:

```bash
# Re-map newly imported files
docker compose run --rm cli round0

# Detect + stage second batch
docker compose run --rm cli round2 --dry-run
docker compose run --rm cli round2 --no-dry-run

# Review vague album in Google Photos, then delete
docker compose run --rm -p 6080:6080 delete delete --album=receipts --no-dry-run --confirm
docker compose run --rm -p 6080:6080 delete delete --album=vague   --no-dry-run --confirm
```

---

## Part 6 — Round 3: Reconcile deletions

After the Playwright deletions have completed, Round 3 re-pulls the full library from the API, diffs it against your local DB, and deletes local copies of photos that are now gone from Google Photos. Files already in `/data/receipts/` are never touched.

```bash
# Dry run: see what would be deleted locally
docker compose run --rm cli round3 --dry-run

# Actually delete local copies
docker compose run --rm cli round3 --no-dry-run
```

---

## Part 7 — Round 4: Duplicate detection + review

Round 4 computes a perceptual hash (phash) for every remaining local file and clusters pairs that are visually similar.

### 7.1 Compute hashes and find pairs

```bash
docker compose run --rm cli round4
```

Use `--threshold=N` to change the Hamming distance cutoff (default 10; lower = stricter, fewer false positives).

### 7.2 Open the review app

```bash
docker compose up review
```

Open **http://localhost:8000** in your browser. You'll see each duplicate pair side by side at full resolution. Keyboard shortcuts:

| Key | Action |
|-----|--------|
| `A` | Keep left, mark right as duplicate |
| `D` | Keep right, mark left as duplicate |
| `S` | Keep both (not actually duplicates) |
| `Space` | Skip for now |

Decisions are saved to `state.db` immediately. You can stop and resume at any time.

---

## Checking progress at any time

```bash
docker compose run --rm cli status
```

Shows counts per round: scanned, labeled, staged, deleted, pending review.

---

## Safety summary

| Operation | Default | Override |
|-----------|---------|----------|
| Detection | Always safe (read-only) | — |
| Staging into albums | `--dry-run` | `--no-dry-run` |
| Playlist deletion (Playwright) | `--dry-run` | `--no-dry-run --confirm` |
| Local file deletion (Round 3) | `--dry-run` | `--no-dry-run` |
| Emptying Trash | **Never automatic** | Manual only, after 60 days |

---

## Troubleshooting

**`client_secret.json` not found**
→ Download OAuth credentials from Google Cloud Console and place them in `secrets/`.

**Token expired / re-auth needed**
→ Delete `secrets/token.json` and re-run `docker compose run --rm -p 8080:8080 cli auth`.

**Round 0 shows 0 matched files**
→ Check that your Takeout zips are extracted into `/data/library/`. Run `ls /data/library/` inside a container: `docker compose run --rm cli bash -c "ls /data/library/"`.

**Playwright can't find the trash button**
→ Google Photos occasionally changes its UI. Open the noVNC window (localhost:6080) and see what state the browser is in. You may need to manually complete a step, then press Enter in the terminal to continue.

**Detection is too aggressive / too conservative**
→ Tune the thresholds in `.env` and re-run the dry-run for round1/2. Items already labeled can be re-run after clearing their label: `docker compose run --rm cli bash -c "sqlite3 /data/state.db \"UPDATE media_items SET label=NULL WHERE label='vague'\""`.
