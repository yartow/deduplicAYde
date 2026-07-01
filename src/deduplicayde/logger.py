import json
import os
import sys
from datetime import datetime, timezone


_LOG_DIR = os.path.join(os.environ.get("DATA_DIR", "/data"), "logs")


def _ensure_log_dir() -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)


def _log_file(round_name: str) -> str:
    _ensure_log_dir()
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(_LOG_DIR, f"{round_name}_{date}.jsonl")


def log_item(round_name: str, outcome: str, **fields) -> None:
    """Write one structured log line per item processed.

    File-only, not printed to the terminal — with libraries in the tens of
    thousands of files this would otherwise flood the console. tqdm shows
    live progress; use `grep <outcome> /data/logs/<round>_*.jsonl` to inspect
    per-item results after the fact (CLAUDE.md requires this to stay
    file-auditable without querying the DB).
    """
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "round": round_name,
        "outcome": outcome,
        **fields,
    }
    line = json.dumps(entry)
    with open(_log_file(round_name), "a") as f:
        f.write(line + "\n")


def log_info(round_name: str, message: str, **fields) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "round": round_name,
        "level": "INFO",
        "message": message,
        **fields,
    }
    line = json.dumps(entry)
    print(line, file=sys.stderr)
    with open(_log_file(round_name), "a") as f:
        f.write(line + "\n")


def log_error(round_name: str, message: str, **fields) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "round": round_name,
        "level": "ERROR",
        "message": message,
        **fields,
    }
    line = json.dumps(entry)
    print(line, file=sys.stderr)
    with open(_log_file(round_name), "a") as f:
        f.write(line + "\n")
