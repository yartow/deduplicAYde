# Idea: Burst photo detection + keeper selection

## Problem

Burst shots (several near-identical frames taken in rapid succession) bloat
the library, but which frame to keep is subjective — sharpness alone doesn't
capture "best expression" or "best composition." Auto-deleting siblings is
too risky to do unattended.

## Proposed approach

1. **Cluster bursts**, not individual near-dupes. Group photos by:
   - `local_timestamp` proximity (e.g. within ~1-2 seconds), and
   - phash similarity (reuse the Round 4 perceptual-hash infrastructure —
     see `round4` / `--threshold` in `cli.py`) to avoid lumping in
     coincidental same-second-but-unrelated shots.
   - Optionally cross-check burst IDs already present in filenames/EXIF from
     the source camera, if available, as a stronger signal than
     timestamp+phash alone.
2. **Rank within each cluster** using the existing `blur_score` (Laplacian
   variance, already computed in `detection.py`) as a first-pass "sharpest
   frame" signal — sort the cluster by this so the review UI can present a
   sensible default ordering, not as an auto-delete criterion.
3. **Never auto-delete burst siblings.** Surface each cluster in the Round 4
   review app (already built for full-resolution side-by-side duplicate
   confirmation, per CLAUDE.md rule 6) and let the user manually pick the
   keeper(s) per cluster. This is a different risk profile than deleting a
   single blurry photo — you'd be destroying alternate frames the user might
   actually have preferred, so it stays a manual-confirm flow like vague
   items, not an automated one like receipts.

## Why not fully automate keeper selection

Blur/sharpness is necessary but not sufficient — closed eyes, awkward
expression, better background/composition in a slightly-less-sharp frame are
all things a heuristic won't reliably capture. Treat sharpness as a sort key
for the review UI, not a decision rule.

## Open questions for implementation

- What burst-window (seconds) and phash Hamming-distance threshold produce
  good clusters without over- or under-grouping on this actual library?
- Should burst clusters reuse the existing `duplicate_pairs` table, or need
  their own schema (a cluster is an N-way group, not a pair)?
- Does the Round 4 review app's UI extend cleanly to N-way clusters, or does
  it need a "pick 1+ of N" pattern instead of its current pairwise
  accept/reject?
