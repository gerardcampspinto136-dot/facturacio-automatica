"""Shared Google OAuth2 credential management for Sheets and Gmail.

The running bot never opens a browser: if the stored token is missing or revoked it
raises, and the failure is reported (the invoice is still issued). Opening the consent
screen is authorize_google.py's job alone -- a browser popping up inside the bot's
process would block it, on a machine nobody may be watching.
"""

import os
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.send",
]

_cached_creds: Credentials | None = None


def _token_path() -> str:
    return os.getenv("GOOGLE_TOKEN_PATH", "config/credentials/google_token.json")


def load_credentials() -> Optional[Credentials]:
    """The stored token, or None if there is none. Never opens a browser."""
    path = _token_path()
    if not Path(path).exists():
        return None
    return Credentials.from_authorized_user_file(path, SCOPES)


def _save(creds: Credentials) -> None:
    path = Path(_token_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")


def get_credentials(interactive: bool = False) -> Credentials:
    """Valid credentials, refreshed if needed.

    With interactive=False (everything but authorize_google.py) a missing or revoked
    token raises instead of opening the consent screen.
    """
    global _cached_creds
    if _cached_creds and _cached_creds.valid:
        return _cached_creds

    creds = load_credentials()
    if creds and creds.valid:
        _cached_creds = creds
        return creds
    if creds and creds.expired and creds.refresh_token and not interactive:
        creds.refresh(Request())  # RefreshError if Google revoked it
        _save(creds)
        _cached_creds = creds
        return creds

    if not interactive:
        raise RuntimeError(
            "No hay una autorización de Google válida. Ejecuta `py authorize_google.py` "
            "(o usa SMTP para el correo: ver .env.example)."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH",
                           "config/credentials/google_credentials.json")
    if not Path(creds_path).exists():
        raise FileNotFoundError(
            f"Google credentials file not found at '{creds_path}'.\n"
            "Download it from Google Cloud Console and place it there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
    creds = flow.run_local_server(port=0)
    _save(creds)
    _cached_creds = creds
    return creds
