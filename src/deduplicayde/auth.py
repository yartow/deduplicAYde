"""OAuth 2.0 flow for Google Photos Library API.

First run:  docker compose run -p 8080:8080 cli auth
            Then open http://localhost:8080 in your browser to complete sign-in.
            Token is saved to /secrets/token.json for future runs.
"""
import os
import json

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.readonly",
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
]

_SECRETS_DIR = os.environ.get("SECRETS_DIR", "/secrets")
_CLIENT_SECRET = os.path.join(_SECRETS_DIR, "client_secret.json")
_TOKEN_FILE = os.path.join(_SECRETS_DIR, "token.json")


def get_credentials() -> Credentials:
    """Return valid credentials, refreshing or re-authing as needed."""
    creds = _load_token()

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
        return creds

    if not os.path.exists(_CLIENT_SECRET):
        raise FileNotFoundError(
            f"client_secret.json not found at {_CLIENT_SECRET}.\n"
            "Download it from Google Cloud Console → APIs & Services → Credentials\n"
            "and place it in the secrets/ directory."
        )

    flow = InstalledAppFlow.from_client_secrets_file(_CLIENT_SECRET, SCOPES)
    creds = flow.run_local_server(
        host="localhost",    # redirect URI Google sees (must stay 'localhost')
        bind_addr="0.0.0.0",  # actual bind address — required for Docker port mapping
        port=8080,
        open_browser=False,
        prompt="select_account",
        success_message=(
            "Authentication successful! You can close this tab and return to the terminal."
        ),
    )
    _save_token(creds)
    print(f"Token saved to {_TOKEN_FILE}")
    return creds


def _load_token() -> Credentials | None:
    if not os.path.exists(_TOKEN_FILE):
        return None
    with open(_TOKEN_FILE) as f:
        data = json.load(f)
    return Credentials.from_authorized_user_info(data, SCOPES)


def _save_token(creds: Credentials) -> None:
    os.makedirs(_SECRETS_DIR, exist_ok=True)
    with open(_TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
