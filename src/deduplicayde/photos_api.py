"""Thin wrapper around the Google Photos Library API v1.

Uses an authorized requests.Session rather than the discovery client because
the Photos API has no maintained discovery document in the Python SDK.

Only album *creation* lives here — Google's March 2024 policy change restricts
our OAuth scope (photoslibrary.readonly.appcreateddata) to items this app
uploaded itself, so mediaItems.list/search can never enumerate the existing
library, and albums.batchAddMediaItems can never be given a valid
mediaItemId for a pre-existing item (see CLAUDE.md). Locating and staging
items now goes through Playwright browser automation instead
(locate_stage.py) — only creating a fresh, empty, app-owned album is still
possible via this API.

Confirmed live (first real `stage --no-dry-run` run): `albums.list` also 403s
under this restricted scope, even though Google's docs list
photoslibrary.readonly.appcreateddata as a valid scope for it. So this module
never searches for an existing album by title — it only ever calls
`albums.create`. Cross-run idempotency (don't create the same album twice) is
handled locally instead, via the `albums` table in state.db — see
staging.get_or_create_album, the caller every other module should go through.
"""
import time

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

_BASE = "https://photoslibrary.googleapis.com/v1"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _session(creds: Credentials) -> AuthorizedSession:
    return AuthorizedSession(creds)


def _post(session: AuthorizedSession, path: str, body: dict) -> dict:
    url = f"{_BASE}/{path}"
    for attempt in range(5):
        r = session.post(url, json=body, timeout=30)
        if r.status_code in _RETRY_STATUSES:
            time.sleep(2**attempt)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def create_album(creds: Credentials, title: str) -> dict:
    """Create a new album via the API. No lookup-by-title (see module docstring
    for why) — callers must track album_id -> purpose locally instead."""
    session = _session(creds)
    return _post(session, "albums", {"album": {"title": title}})
