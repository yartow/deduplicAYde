"""Self-contained HTML report for `quality-score --sample N` trial runs.

Kept separate from quality_score.py's scoring logic (same split rationale as
detection.py/thresholds.py) — this module only turns already-computed scores
into something a human can look at. Pure stdlib + Pillow, no templating
library, no web server: one .html file with base64-inlined thumbnails that
opens directly in a browser.

Sort order assumes higher-is-better polarity (true for both current tiers,
'musiq' and 'clipiqa+_vitL14_512' — see quality_score.py's _TIER_MODELS). If
a future tier uses a lower-is-better metric, this needs a polarity flag.
"""
import base64
import html
import io
import os
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

_LOG_DIR = os.path.join(os.environ.get("DATA_DIR", "/data"), "logs")
_THUMB_MAX_EDGE = 400
_HEIC_SUFFIXES = {".heic", ".heif"}


def _thumbnail_data_uri(path: Path) -> str | None:
    try:
        if path.suffix.lower() in _HEIC_SUFFIXES:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        img = Image.open(path).convert("RGB")
        img.thumbnail((_THUMB_MAX_EDGE, _THUMB_MAX_EDGE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return None


def write_sample_report(
    results: list[dict],
    tier: str,
    model_name: str,
    device: str,
    avg_time: float,
    remaining: int,
    est_full_seconds: float,
) -> str:
    """results: list of {"id", "path", "score", "elapsed"}. Returns the written
    report's path. Never touches state.db — pure read + render."""
    os.makedirs(_LOG_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(_LOG_DIR, f"quality_sample_{tier}_{timestamp}.html")

    ranked = sorted(results, key=lambda r: r["score"])  # worst (lowest) first

    def esc(s: str) -> str:
        return html.escape(str(s))

    cards = []
    for r in ranked:
        path = Path(r["path"])
        data_uri = _thumbnail_data_uri(path)
        img_tag = (
            f'<img src="{data_uri}">' if data_uri
            else '<div class="thumb-error">thumbnail failed</div>'
        )
        cards.append(f"""
      <div class="card">
        {img_tag}
        <div class="card-meta" title="{esc(path.name)}">
          {esc(path.name)}<br>score={r['score']:.3f} &middot; {r['elapsed']*1000:.0f}ms
        </div>
      </div>""")

    est_hours = est_full_seconds / 3600
    est_minutes = est_full_seconds / 60

    body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Quality-score sample — {esc(tier)}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: system-ui, sans-serif; background: #111; color: #eee; margin: 0; }}
header {{ padding: 1rem; background: #222; }}
header h1 {{ font-size: 1.1rem; margin: 0 0 0.5rem; }}
.stats {{ color: #aaa; font-size: 0.85rem; line-height: 1.6; }}
.stats b {{ color: #eee; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
         gap: 1rem; padding: 1rem; }}
.card {{ background: #1e1e1e; border-radius: 6px; overflow: hidden; }}
.card img {{ width: 100%; height: 220px; object-fit: cover; display: block; }}
.thumb-error {{ width: 100%; height: 220px; display: flex; align-items: center;
                 justify-content: center; color: #888; font-size: 0.8rem; }}
.card-meta {{ padding: 0.4rem 0.6rem; font-size: 0.7rem; color: #ccc;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
</style>
</head>
<body>
<header>
  <h1>Quality-score sample — worst (lowest score) first</h1>
  <div class="stats">
    Tier: <b>{esc(tier)}</b> &nbsp;|&nbsp; Model: <b>{esc(model_name)}</b> &nbsp;|&nbsp; Device: <b>{esc(device)}</b><br>
    Sampled: <b>{len(results)}</b> images &nbsp;|&nbsp; Avg: <b>{avg_time:.2f}s/image</b><br>
    Extrapolated full run over <b>{remaining}</b> remaining unscored items:
    <b>~{est_hours:.1f}h</b> ({est_minutes:.0f}m)<br>
    No DB changes were made by this sample run.
  </div>
</header>
<div class="grid">
  {''.join(cards) or '<p style="padding:1rem;color:#888;">No images to show.</p>'}
</div>
</body></html>"""

    with open(report_path, "w") as f:
        f.write(body)

    return report_path
