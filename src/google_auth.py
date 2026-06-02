"""Shared Google OAuth2 credential management for Sheets and Gmail."""

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.send",
]

_cached_creds: Credentials | None = None


def get_credentials() -> Credentials:
    global _cached_creds
    if _cached_creds and _cached_creds.valid:
        return _cached_creds

    token_path = os.getenv("GOOGLE_TOKEN_PATH", "config/credentials/google_token.json")
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "config/credentials/google_credentials.json")

    creds: Credentials | None = None

    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(creds_path).exists():
                raise FileNotFoundError(
                    f"Google credentials file not found at '{creds_path}'.\n"
                    "Download it from Google Cloud Console and place it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)

        Path(token_path).parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    _cached_creds = creds
    return creds
