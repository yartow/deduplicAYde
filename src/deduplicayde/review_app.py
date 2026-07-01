"""Round 4 review web app: side-by-side full-resolution duplicate comparison.

Start: docker compose up review
Open:  http://localhost:8000

Keyboard shortcuts:
  A     → keep left, delete right
  D     → keep right, delete left
  S     → keep both / not duplicates
  Space → skip (decide later)
"""
import os
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
import uvicorn

from . import db

_DATA_DIR = os.environ.get("DATA_DIR", "/data")

app = FastAPI(title="deduplicAYde Review", docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
def index():
    with db.get_conn() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM duplicate_pairs WHERE review_status='pending'"
        ).fetchone()[0]
        done = conn.execute(
            "SELECT COUNT(*) FROM duplicate_pairs WHERE review_status != 'pending'"
        ).fetchone()[0]

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>deduplicAYde</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 600px; margin: 80px auto; text-align: center; }}
.big {{ font-size: 4rem; }}
a.btn {{ display:inline-block; margin-top:2rem; padding:1rem 2rem; background:#1a73e8; color:white;
         text-decoration:none; border-radius:8px; font-size:1.1rem; }}
a.btn:hover {{ background:#1557b0; }}
.stats {{ margin-top: 2rem; color: #666; }}
</style></head>
<body>
<div class="big">🔍</div>
<h1>deduplicAYde</h1>
<div class="stats">
  <b>{pending}</b> pairs pending review &nbsp;|&nbsp; <b>{done}</b> reviewed
</div>
<a class="btn" href="/review">Start Reviewing →</a>
</body></html>""")


@app.get("/review", response_class=HTMLResponse)
def review_page():
    with db.get_conn() as conn:
        pair = conn.execute(
            """
            SELECT dp.id, dp.item_a_id, dp.item_b_id, dp.hamming_distance,
                   a.local_path AS path_a, a.filename AS name_a,
                   b.local_path AS path_b, b.filename AS name_b
            FROM duplicate_pairs dp
            JOIN media_items a ON a.id = dp.item_a_id
            JOIN media_items b ON b.id = dp.item_b_id
            WHERE dp.review_status = 'pending'
              AND a.deletion_status IS NULL
              AND b.deletion_status IS NULL
            ORDER BY dp.hamming_distance ASC, dp.id ASC
            LIMIT 1
            """
        ).fetchone()

        remaining = conn.execute(
            "SELECT COUNT(*) FROM duplicate_pairs WHERE review_status='pending'"
        ).fetchone()[0]

    if pair is None:
        return HTMLResponse("""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Done</title>
<style>body{font-family:system-ui;text-align:center;margin-top:10vh;}</style></head>
<body><h1>All pairs reviewed!</h1>
<p>Items marked as duplicates have <code>label='duplicate'</code> in state.db.</p>
<a href="/">← Back to dashboard</a>
</body></html>""")

    pair_id = pair["id"]
    item_a_id = pair["item_a_id"]
    item_b_id = pair["item_b_id"]
    hamming = pair["hamming_distance"]
    name_a = pair["name_a"] or str(item_a_id)
    name_b = pair["name_b"] or str(item_b_id)

    # Escape for HTML attribute use
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Review – deduplicAYde</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: system-ui, sans-serif; background: #111; color: #eee;
        height: 100vh; display: flex; flex-direction: column; overflow: hidden; }}
header {{ padding: 0.5rem 1rem; background: #222; display: flex;
          align-items: center; gap: 1rem; flex-shrink: 0; }}
header h1 {{ font-size: 1rem; }}
.meta {{ font-size: 0.8rem; color: #aaa; }}
.images {{ display: flex; flex: 1; overflow: hidden; }}
.pane {{ flex: 1; display: flex; flex-direction: column; overflow: hidden; }}
.pane-label {{ padding: 0.4rem 0.8rem; background: #1e1e1e; font-size: 0.8rem;
               color: #ccc; text-align: center; flex-shrink: 0; white-space: nowrap;
               overflow: hidden; text-overflow: ellipsis; }}
.pane img {{ flex: 1; object-fit: contain; width: 100%; min-height: 0;
             max-height: calc(100vh - 120px); }}
footer {{ padding: 0.6rem; background: #222; display: flex;
          justify-content: center; gap: 0.5rem; flex-shrink: 0; }}
button {{ padding: 0.5rem 1.2rem; border: none; border-radius: 6px;
          font-size: 0.9rem; cursor: pointer; font-weight: 600; }}
.btn-a    {{ background: #1a73e8; color: white; }}
.btn-b    {{ background: #34a853; color: white; }}
.btn-both {{ background: #fbbc04; color: black; }}
.btn-skip {{ background: #444; color: #eee; }}
button:hover {{ opacity: 0.85; }}
.key {{ font-size: 0.7rem; opacity: 0.6; display: block; }}
</style>
</head>
<body>
<header>
  <h1>deduplicAYde</h1>
  <div class="meta">
    Pair #{pair_id} &nbsp;|&nbsp; Hamming: {hamming} &nbsp;|&nbsp; {remaining} remaining
  </div>
</header>

<div class="images">
  <div class="pane">
    <div class="pane-label" title="{esc(name_a)}">{esc(name_a)}</div>
    <img src="/img/{item_a_id}" alt="{esc(name_a)}" loading="eager">
  </div>
  <div class="pane">
    <div class="pane-label" title="{esc(name_b)}">{esc(name_b)}</div>
    <img src="/img/{item_b_id}" alt="{esc(name_b)}" loading="eager">
  </div>
</div>

<footer>
  <form method="post" action="/decision/{pair_id}/keep_a" id="fa">
    <button type="submit" class="btn-a">Keep LEFT<span class="key">[A]</span></button>
  </form>
  <form method="post" action="/decision/{pair_id}/keep_both" id="fs">
    <button type="submit" class="btn-both">Keep BOTH<span class="key">[S]</span></button>
  </form>
  <form method="post" action="/decision/{pair_id}/skip" id="fk">
    <button type="submit" class="btn-skip">Skip<span class="key">[Space]</span></button>
  </form>
  <form method="post" action="/decision/{pair_id}/keep_b" id="fb">
    <button type="submit" class="btn-b">Keep RIGHT<span class="key">[D]</span></button>
  </form>
</footer>

<script>
document.addEventListener('keydown', function(e) {{
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  switch (e.key.toLowerCase()) {{
    case 'a': document.getElementById('fa').submit(); break;
    case 'd': document.getElementById('fb').submit(); break;
    case 's': document.getElementById('fs').submit(); break;
    case ' ':
    case 'k': e.preventDefault(); document.getElementById('fk').submit(); break;
  }}
}});
</script>
</body>
</html>""")


@app.post("/decision/{pair_id}/{decision}")
def record_decision(pair_id: int, decision: str):
    valid = {"keep_a", "keep_b", "keep_both", "skip"}
    if decision not in valid:
        return Response(status_code=400)

    with db.get_conn() as conn:
        pair = conn.execute(
            "SELECT item_a_id, item_b_id FROM duplicate_pairs WHERE id=?", (pair_id,)
        ).fetchone()

        if pair is None:
            return Response(status_code=404)

        conn.execute(
            "UPDATE duplicate_pairs SET review_status=?, reviewed_at=? WHERE id=?",
            (decision, db.now_iso(), pair_id),
        )

        if decision == "keep_a":
            conn.execute(
                "UPDATE media_items SET label='duplicate', updated_at=? WHERE id=?",
                (db.now_iso(), pair["item_b_id"]),
            )
        elif decision == "keep_b":
            conn.execute(
                "UPDATE media_items SET label='duplicate', updated_at=? WHERE id=?",
                (db.now_iso(), pair["item_a_id"]),
            )

    return RedirectResponse("/review", status_code=303)


@app.get("/img/{item_id:int}")
def serve_image(item_id: int):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT local_path FROM media_items WHERE id=?", (item_id,)
        ).fetchone()

    if row is None or not row["local_path"]:
        return Response(status_code=404)

    path = Path(row["local_path"])
    if not path.exists():
        return Response(status_code=404)

    return FileResponse(str(path))


if __name__ == "__main__":
    db.init_db()
    uvicorn.run(app, host="0.0.0.0", port=8000)
