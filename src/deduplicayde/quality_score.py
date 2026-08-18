"""quality-score: AI no-reference image-quality scoring (pyiqa/PyTorch).

A second signal alongside Round 1/2's OpenCV blur_score/edge_density — see
CLAUDE.md for why the whole-frame Laplacian-variance score can misfire on
photos with a busy background (score inflated even when the subject is out
of focus) or a plain/dark background (score deflated even when the subject
is razor sharp).

Runs either natively on the host (PyTorch `mps` backend, Apple Silicon GPU)
or in the CPU-only `quality` Docker service, selected by QUALITY_SCORING_DOCKER
in .env — see scripts/run_quality_score.sh, the only entrypoint users should
invoke directly.

Two modes:
  --sample N   Trial mode. Random sample of N eligible items, times
               inference, writes NO DB changes, produces a self-contained
               HTML report (thumbnails + scores + timing) in DATA_DIR/logs/.
               Use this before committing to a full run.
  (default)    Full mode. Iterates every eligible item without a
               quality_score yet, writes quality_score/quality_model/
               quality_scored_at to state.db, checkpointed like every
               other round.
"""
import os
import random
import signal
import sys
import time
from pathlib import Path

# Confirmed live: an uncapped CPU run (docker `quality` service, --tier
# heavyweight) let PyTorch spin up threads on every core available to it —
# sustained near-100% across all cores for the whole run — and that
# coincided with a real overheat/crash while the lid was closed. These env
# vars control the OpenMP/MKL thread pools PyTorch's CPU backend actually
# uses (torch.set_num_threads alone doesn't bound them), so they must be set
# before `import torch` happens anywhere, including transitively via pyiqa.
# QUALITY_MAX_CPU_THREADS overrides the default; default leaves 2 cores free
# so the host stays responsive and thermal load has headroom. This matters
# most for the Docker (CPU-only) path and for native runs that fall back to
# CPU — MPS runs are largely unaffected but the cap is harmless there too.
_MAX_CPU_THREADS = max(1, int(os.environ.get(
    "QUALITY_MAX_CPU_THREADS", max(1, (os.cpu_count() or 4) - 2)
)))
os.environ.setdefault("OMP_NUM_THREADS", str(_MAX_CPU_THREADS))
os.environ.setdefault("MKL_NUM_THREADS", str(_MAX_CPU_THREADS))

from PIL import Image
from pillow_heif import register_heif_opener
from tqdm import tqdm

from . import db
from .logger import log_info, log_item, log_error

register_heif_opener()  # lets PIL.Image.open() (used by both this module
                         # and pyiqa's own imread2pil) read .heic/.heif

_ROUND = "quality_score"

# Verified against pyiqa.list_models(metric_mode='NR') at implementation
# time — re-verify if upgrading pyiqa, its model-id spelling/availability
# has shifted across releases historically.
_TIER_MODELS = {
    "medium": "musiq",
    "heavyweight": "clipiqa+_vitL14_512",
}

_DEFAULT_TIER = os.environ.get("QUALITY_TIER", "medium")

_IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".tif", ".tiff", ".bmp",
}

_ELIGIBLE_WHERE_SQL = """
    WHERE local_path IS NOT NULL
      AND deletion_status IS NULL
      AND (label IS NULL OR label IN ('vague', 'ok'))
"""


def _resolve_device(requested: str = "auto"):
    import torch
    torch.set_num_threads(_MAX_CPU_THREADS)  # belt-and-suspenders alongside
    # the OMP/MKL env vars above — torch's own intra-op thread pool is a
    # separate knob from the OpenMP/MKL ones those control.
    if requested == "cpu":
        return torch.device("cpu")
    if requested == "mps":
        return torch.device("mps")
    # auto: prefer mps if genuinely available; this is simply False inside
    # the Docker `quality` service's Linux container, so no special-casing
    # is needed there — it always falls through to cpu on its own.
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def _load_metric(tier: str, device):
    import pyiqa
    model_name = _TIER_MODELS[tier]
    available = pyiqa.list_models(metric_mode="NR")
    if model_name not in available:
        raise SystemExit(
            f"Model id '{model_name}' for tier '{tier}' not found in this "
            f"pyiqa version's registry ({len(available)} NR models available). "
            f"Run pyiqa.list_models() and update _TIER_MODELS in quality_score.py."
        )
    return pyiqa.create_metric(model_name, device=device)


# Confirmed live: feeding a full-resolution 18-megapixel JPEG (5184x3456,
# nothing unusual about the file) straight into clipiqa+_vitL14_512 raised
# "Invalid buffer size: 493.81 GiB". That number lines up with a ViT
# self-attention matrix over the raw image's patch grid (patch=14 ->
# ~370x246 patches -> N^2 attention entries) rather than the model's own
# 512px working resolution — some path in pyiqa's preprocessing isn't
# downsizing before building that matrix for this model/aspect-ratio
# combination. The attempted allocation itself (not just the eventual
# exception) is what drove the multi-minute stall and system-wide swap
# thrashing/lag reported live, since by the time it fails the damage to
# memory pressure is already done. Resizing before it ever reaches pyiqa
# bounds this deterministically regardless of which model has the bug, and
# is cheap enough to apply unconditionally to every image/tier.
_MAX_IMAGE_DIM = int(os.environ.get("QUALITY_MAX_IMAGE_DIM", 1024))


def _load_image_pil(path: Path) -> Image.Image:
    """pyiqa's imread2pil (pyiqa/utils/img_util.py) accepts a path string,
    bytes, or a PIL.Image — not a raw array — and even for path strings it
    reads via PIL.Image.open() internally, not cv2.imread. So HEIC/HEIF
    support just needs pillow_heif's opener registered (done once at module
    import below, mirroring detection.py's _open_cv_image HEIC handling);
    we pass an already-opened, RGB-converted PIL Image explicitly rather
    than a path so every format goes through the exact same code path."""
    img = Image.open(path).convert("RGB")
    if max(img.size) > _MAX_IMAGE_DIM:
        img.thumbnail((_MAX_IMAGE_DIM, _MAX_IMAGE_DIM), Image.LANCZOS)
    return img


_CONTAINER_DATA_ROOT = "/data/"


def _resolve_local_path(stored_path: str) -> Path:
    """local_path is always stored /data/-prefixed — every other round
    (round0/1/2/3/4) runs inside the `cli`/`delete` Docker containers, where
    DATA_DIR=/data is the bind-mount target, so that's a valid path there.
    quality-score is the first code path that can run natively, where
    DATA_DIR is the real host path instead — /data/... doesn't exist on the
    host, so re-root onto whatever DATA_DIR actually resolves to here.
    Inside Docker this is a no-op (DATA_DIR=/data there too)."""
    data_dir = os.environ.get("DATA_DIR", "/data")
    if stored_path.startswith(_CONTAINER_DATA_ROOT) and data_dir.rstrip("/") != "/data":
        return Path(data_dir) / stored_path[len(_CONTAINER_DATA_ROOT):]
    return Path(stored_path)


def _in_docker() -> bool:
    # DATA_DIR is only ever the literal "/data" inside the `quality`
    # container (the bind-mount target); natively DATA_DIR is always a real
    # host path. Same heuristic _resolve_local_path relies on.
    return os.environ.get("DATA_DIR") == "/data"


def _stop_hint() -> str:
    return "docker compose kill quality" if _in_docker() else \
        "pkill -f 'deduplicayde.cli quality-score'"


def _confirm_cpu_run(tier: str, device, requested_device: str, assume_yes: bool) -> None:
    """Interactive gate, not just a print — the person running this needs an
    explicit chance to back out before it starts, not just a warning that
    scrolls past. Confirmed live: a sustained CPU-bound heavyweight run
    (Docker path, no GPU) coincided with a real overheat/crash while the lid
    was closed. The thread-count cap above bounds worst case, but "capped"
    still means real sustained load on however many cores are allowed —
    still bad to run enclosed or unattended without knowing that's what's
    about to happen."""
    if device.type != "cpu":
        return

    if requested_device == "cpu":
        reason = "you explicitly passed --device cpu"
    elif _in_docker():
        reason = "Docker Desktop on Apple Silicon has no Metal/GPU passthrough into containers"
    else:
        reason = "PyTorch's mps (Apple GPU) backend isn't available on this machine/build"

    free_cores = max((os.cpu_count() or 4) - _MAX_CPU_THREADS, 0)
    print(
        f"\n[!] This application cannot use your GPU because {reason}.\n"
        f"    It will run on CPU instead — capped to {_MAX_CPU_THREADS} thread(s), "
        f"leaving {free_cores} core(s) free for the terminal/other work — but it "
        "will still sustain real CPU load for the whole run.\n"
        "    Keep the lid open and don't put the laptop away until it finishes.\n"
        f"    To stop at any time: Ctrl+C in this terminal, or from another "
        f"terminal: {_stop_hint()}\n"
    )
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise SystemExit(
            "Refusing to run a CPU-bound quality-score pass non-interactively "
            "without confirmation — re-run with --yes to proceed."
        )
    answer = input("    Continue on CPU? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted — no changes made.")
        raise SystemExit(0)


def _print_stop_instructions(device) -> None:
    # CPU path already prints its own stop hint as part of the confirmation
    # prompt above; this covers the mps (GPU) path, which skips that prompt
    # entirely but the user still needs a documented way to interrupt it.
    if device.type == "cpu":
        return
    print(f"\nRunning on {device}. Press Ctrl+C anytime to stop "
          f"(or from another terminal: {_stop_hint()}).\n")


class _ScoringTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _ScoringTimeout("exceeded per-item time budget")


# Backstop, not the primary fix (that's the resize cap above) — SIGALRM only
# gets delivered when control returns to Python bytecode, so it can't
# preempt a single long C-level call (e.g. a stuck malloc) already in
# flight. It still bounds anything else that can stall a single item (slow
# disk/network read, some other model-specific pathological case) instead
# of silently eating minutes of wall time per image, as happened live.
_ITEM_TIMEOUT_SECONDS = int(os.environ.get("QUALITY_ITEM_TIMEOUT_SECONDS", 60))


def _score_one(metric, path: Path) -> float:
    img = _load_image_pil(path)
    if _ITEM_TIMEOUT_SECONDS > 0 and hasattr(signal, "SIGALRM"):
        previous = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(_ITEM_TIMEOUT_SECONDS)
        try:
            score = metric(img)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
    else:
        score = metric(img)
    return float(score.item() if hasattr(score, "item") else score)


def _eligible_unscored_rows():
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT id, local_path FROM media_items "
            + _ELIGIBLE_WHERE_SQL
            + " AND quality_score IS NULL"
        ).fetchall()


def _eligible_all_rows():
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT id, local_path FROM media_items " + _ELIGIBLE_WHERE_SQL
        ).fetchall()


def run_sample(n: int, tier: str, device_arg: str, seed: int | None, assume_yes: bool = False) -> None:
    db.init_db()
    device = _resolve_device(device_arg)
    model_name = _TIER_MODELS[tier]
    log_info(_ROUND, "Starting sample run (no DB writes)",
             tier=tier, model=model_name, device=str(device), n=n)
    _confirm_cpu_run(tier, device, device_arg, assume_yes)
    _print_stop_instructions(device)

    candidates = []
    for r in _eligible_all_rows():
        path = _resolve_local_path(r["local_path"])
        if path.suffix.lower() in _IMAGE_SUFFIXES and path.exists():
            candidates.append((r, path))
    if not candidates:
        print("No eligible items found for sampling.")
        return

    rng = random.Random(seed)
    sample = rng.sample(candidates, min(n, len(candidates)))

    metric = _load_metric(tier, device)
    results = []
    try:
        with tqdm(sample, desc=f"Sampling ({tier})", unit=" images") as bar:
            for row, path in bar:
                start = time.monotonic()
                try:
                    score = _score_one(metric, path)
                    elapsed = time.monotonic() - start
                    results.append({"id": row["id"], "path": path, "score": score, "elapsed": elapsed})
                    log_item(_ROUND, "sampled", item_id=row["id"], score=score, elapsed_s=elapsed)
                except Exception as e:
                    log_error(_ROUND, "Sample scoring failed", item_id=row["id"], path=str(path), error=str(e))
                bar.update(1)
    except KeyboardInterrupt:
        log_info(_ROUND, "Sample run stopped by user", scored=len(results))
        print(f"\nStopped by user after {len(results)} image(s). No DB changes were made either way.")
        if not results:
            return

    if not results:
        print("No images could be scored.")
        return

    total_time = sum(r["elapsed"] for r in results)
    avg_time = total_time / len(results)
    remaining = len(_eligible_unscored_rows())
    est_full_seconds = avg_time * remaining

    from .quality_report import write_sample_report
    report_path = write_sample_report(
        results=results, tier=tier, model_name=model_name, device=str(device),
        avg_time=avg_time, remaining=remaining, est_full_seconds=est_full_seconds,
    )

    print(f"\nSample done: {len(results)} images scored, avg {avg_time:.2f}s/image.")
    print(f"Estimated full run over {remaining} remaining unscored items: "
          f"~{est_full_seconds/3600:.1f}h ({est_full_seconds/60:.0f}m).")
    print(f"Report: {report_path}")
    print("No DB changes were made. Re-run without --sample to commit to a full run.")


def run_full(tier: str, device_arg: str, assume_yes: bool = False) -> None:
    db.init_db()
    device = _resolve_device(device_arg)
    model_name = _TIER_MODELS[tier]
    log_info(_ROUND, "Starting full run", tier=tier, model=model_name, device=str(device))
    _confirm_cpu_run(tier, device, device_arg, assume_yes)
    _print_stop_instructions(device)

    rows = [
        (r, _resolve_local_path(r["local_path"]))
        for r in _eligible_unscored_rows()
    ]
    rows = [(r, p) for r, p in rows if p.suffix.lower() in _IMAGE_SUFFIXES]
    log_info(_ROUND, "Scoring items", count=len(rows))

    if not rows:
        print("No unscored eligible items found.")
        return

    metric = _load_metric(tier, device)
    scored = 0
    try:
        with tqdm(rows, desc=f"quality-score ({tier})", unit=" images") as bar:
            for row, path in bar:
                if not path.exists():
                    log_item(_ROUND, "file_missing", item_id=row["id"], path=str(path))
                    bar.update(1)
                    continue

                try:
                    score = _score_one(metric, path)
                    with db.get_conn() as conn:
                        db.set_quality_score(conn, row["id"], score, model_name)
                    log_item(_ROUND, "scored", item_id=row["id"], score=score, model=model_name)
                    scored += 1
                except Exception as e:
                    log_error(_ROUND, "Scoring failed", item_id=row["id"], path=str(path), error=str(e))
                bar.update(1)
    except KeyboardInterrupt:
        # Every scored item was already committed to state.db as it was
        # scored (matches every other round's checkpoint-per-item
        # convention) — an interrupt here loses at most the one in-flight
        # image, not prior progress. Re-running the same command resumes via
        # the _eligible_unscored_rows() filter, same as any other round.
        log_info(_ROUND, "Full run stopped by user", scored=scored)
        print(f"\nStopped by user after scoring {scored} item(s) — already saved. "
              "Re-run the same command to resume where you left off.")
        return

    with db.get_conn() as conn:
        db.mark_round_complete(conn, _ROUND)

    log_info(_ROUND, "quality-score complete", scored=scored)
    print(f"\nquality-score done: {scored} items scored with '{model_name}' ({tier}).")
    print("Run 'status' to see progress.")


def run(
    tier: str | None, device: str, sample_n: int | None, seed: int | None,
    assume_yes: bool = False,
) -> None:
    try:
        # Lower scheduling priority so the OS favors interactive work (the
        # terminal, other apps) over this batch job whenever CPU is
        # contested — independent of, and in addition to, the thread cap
        # above. Doesn't affect mps/GPU compute, only how CPU time (image
        # loading/preprocessing, and all compute on the CPU fallback path)
        # is scheduled against everything else running.
        os.nice(10)
    except (AttributeError, OSError):
        pass  # not supported on this platform; not worth failing the run over

    resolved_tier = tier if tier is not None else _DEFAULT_TIER
    if resolved_tier not in _TIER_MODELS:
        raise SystemExit(f"Unknown tier '{resolved_tier}', expected one of {list(_TIER_MODELS)}")
    if sample_n:
        run_sample(sample_n, resolved_tier, device, seed, assume_yes)
    else:
        run_full(resolved_tier, device, assume_yes)
