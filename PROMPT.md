# Project kickoff prompt for Claude Code

Paste everything below into Claude Code in a fresh, empty project directory.

---

I want to build a tool that helps me clean up my Google Photos library (~453GB) by
detecting and removing junk photos (receipts, blurry/low-content "vague" shots, and
duplicates), while keeping verified deletion in my control. Read README.md and
CLAUDE.md in this repo first — I've already written up the full design and
constraints there. Use them as the source of truth for scope and architecture.

Build this as a set of Dockerized, resumable Python scripts/CLI commands, one per
round, that I run manually from the terminal. I do not want a always-on service or
daemon — I want to invoke each step, let it run (possibly for hours, possibly
overnight, possibly across multiple sessions), and be able to pause/stop it safely
(Ctrl+C or container stop) and resume later without redoing completed work or
corrupting state.

Please:

1. Set up the project structure described in CLAUDE.md (Docker Compose service(s),
   Python package layout, a SQLite database for state/checkpointing).
2. Implement Round 0 (the ID-mapping builder) first, since every later round depends
   on it, and confirm it works against my real Google account before moving on.
3. Implement Round 1/2 (detection: receipts via OCR + vague via blur/edge scoring,
   staging into Google Photos albums via the API).
4. Implement the Round 1/2 deletion step (Playwright browser automation against
   photos.google.com, driven from the staged albums).
5. Implement Round 3 (offline/cloud ID reconciliation and local deletion sync).
6. Implement Round 4 (perceptual-hash duplicate detection plus the local side-by-side
   review web app).
7. At each step, pause and let me test against a small subset (e.g. one album or a
   few hundred photos) before running it against the full library.

Ask me before doing anything that touches my actual Google account credentials,
before any step that performs a real (non-dry-run) deletion, and before installing
anything outside the Docker containers. Default every destructive operation to a
`--dry-run` mode that only prints/logs what it would do.
