# Prompt: retry the `heavyweight` quality-score sample

Use this as a Claude Code prompt when ready to retry — not something to run
unattended without reading it first.

## Context

Two real incidents happened testing `quality-score --tier heavyweight`
(`clipiqa+_vitL14_512`) on this machine (M1 Max, native `mps` execution
unless otherwise noted):

1. **Docker CPU-pinning overheat/crash.** An uncapped run through the
   Docker `quality` service (CPU-only path) let PyTorch pin every core
   Docker Desktop's Linux VM had access to, sustained for the whole run,
   and coincided with a real overheat/crash while the lid was closed.
2. **Memory-blowup / swap-thrashing lag, even on `mps` (GPU).** A
   full-resolution 18-megapixel JPEG (5184x3456, nothing wrong with the
   file) made `clipiqa+_vitL14_512` raise `Invalid buffer size: 493.81
   GiB`. The *attempted* allocation — not just the eventual exception —
   caused a 13-minute stall on that one image and system-wide swap
   thrashing/keystroke lag, forcing a hard kill of the process.

Both are described in detail in `CLAUDE.md`'s "Things to ask the user about
before proceeding" section — read that section's two `heavyweight`-related
bullets before doing anything else here.

## Precondition — do this first, don't skip it

**Verify the memory/resource guard is actually in place and correct before
running anything, not just that it once was.** As of this writing,
`src/deduplicayde/quality_score.py` should have:

- `_load_image_pil` capping every image's longest side to
  `QUALITY_MAX_IMAGE_DIM` (default 1024px) via `PIL.Image.thumbnail` before
  the image ever reaches `pyiqa` — this is the actual fix for incident #2
  above, applied unconditionally regardless of tier/model.
- `_score_one` wrapping the `metric()` call in a `SIGALRM`-based timeout
  (`QUALITY_ITEM_TIMEOUT_SECONDS`, default 60s) as a backstop for any other
  stall — note this can't preempt an already-in-flight C-level call (e.g. a
  stuck malloc), so it's a backstop, not the primary fix.
- The Docker `quality` service capped (`cpus: 4`, `mem_limit: 6g` in
  `docker-compose.yml`) and CPU thread caps
  (`OMP_NUM_THREADS`/`MKL_NUM_THREADS`/`torch.set_num_threads`, via
  `QUALITY_MAX_CPU_THREADS`) from incident #1.
- The CPU-unavailable confirmation prompt (`_confirm_cpu_run`) and the
  Ctrl+C / kill-command messaging (`_print_stop_instructions`).

If any of this is missing, weakened, or was reverted, **build/restore it
before running heavyweight again** — don't just retry and hope. Re-read the
git history / `CLAUDE.md` gotcha entries for the exact reasoning if
anything here looks stale.

## What to actually do

1. Confirm the precondition above.
2. Run the same 12-image sample used for the medium-tier comparison, same
   seed, so results are directly comparable:
   ```
   ./scripts/run_quality_score.sh --sample 12 --tier heavyweight --seed 42
   ```
3. **Report per-photo timing, not just an aggregate average.** For each of
   the 12 images: filename, resolution (original, before the resize cap),
   elapsed time, and score. The HTML report
   (`quality_report.py::write_sample_report`) already includes per-image
   elapsed time (`{elapsed*1000:.0f}ms`) in each thumbnail's caption — check
   it's still there and pull the numbers out into a plain table in the
   response too, don't make the user open the HTML file just to get timing.
   Flag any image whose elapsed time is a clear outlier vs. the rest (that
   was the leading indicator of the stall in incident #2 — image 5 jumped
   from ~13-25s to 207s/image average right before the crash-inducing
   image showed up).
4. Compare against the medium-tier (`musiq`) run's timings and scores for
   the same 12 images (same seed) if that data is still available in
   `DATA_DIR/logs/quality_score_*.jsonl` or a prior HTML report.
5. Watch system load/memory (`uptime`, `vm_stat`) during the run this time,
   not just after — if free pages start dropping sharply or load average
   spikes well beyond baseline mid-run, stop and investigate before letting
   it continue, rather than waiting for a crash or a user complaint about
   lag to notice.
