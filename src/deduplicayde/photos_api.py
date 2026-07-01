"""Thin wrapper around the Google Photos Library API v1.

Uses an authorized requests.Session rather than the discovery client because
the Photos API has no maintained discovery document in the Python SDK.

Only album creation lives here — Google's March 2024 policy change restricts
our OAuth scope (photoslibrary.readonly.appcreateddata) to items this app
uploaded itself, so mediaItems.list/search can never enumerate the existing
library, and albums.batchAddMediaItems can never be given a valid
mediaItemId for a pre-existing item (see CLAUDE.md). Locating and staging
items now goes through Playwright browser automation instead
(locate_stage.py) — only creating a fresh, empty, app-owned album is still
possible via this API.
"""
import time

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

_BASE = "https://photoslibrary.googleapis.com/v1"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _session(creds: Credentials) -> AuthorizedSession:
    return AuthorizedSession(creds)


def _get(session: AuthorizedSession, path: str, **params) -> dict:
    url = f"{_BASE}/{path}"
    for attempt in range(5):
        r = session.get(url, params=params, timeout=30)
        if r.status_code in _RETRY_STATUSES:
            time.sleep(2**attempt)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


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



def get_or_create_album(creds: Credentials, title: str) -> dict:
    """Return the album dict with the given title, creating it if needed."""
    session = _session(creds)

    # Search existing albums
    params: dict = {"pageSize": 50}
    while True:
        data = _get(session, "albums", **params)
        for album in data.get("albums", []):
            if album["title"] == title:
                return album
        next_token = data.get("nextPageToken")
        if not next_token:
            break
        params["pageToken"] = next_token

    # Create new
    result = _post(session, "albums", {"album": {"title": title}})
    return result
