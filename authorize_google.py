"""One-off Google authorisation.

Opens the consent screen in your browser and caches the resulting token in
config/credentials/google_token.json, which is what Sheets and Gmail then use. Run it
once with `py authorize_google.py`; after that the bot never asks again.

Kept separate from main.py so authorising does not require the rest of the bot's
configuration to be complete.
"""

from dotenv import load_dotenv

load_dotenv()

from src.google_auth import get_credentials  # noqa: E402


def main() -> None:
    print("Opening the Google consent screen in your browser...")
    print("Google will warn that the app is not verified — that is expected for your own")
    print("app. Choose 'Advanced' (Configuración avanzada), then 'Go to gestoria (unsafe)'.")
    print()

    creds = get_credentials()

    print("Authorised. Token saved to config/credentials/google_token.json")
    print("Scopes granted:")
    for scope in creds.scopes or []:
        print("  -", scope)

    # Prove the token actually works against the user's real spreadsheet.
    import os

    sheet_id = os.getenv("SPREADSHEET_ID", "")
    if sheet_id and not sheet_id.startswith("your_"):
        import gspread

        sheet = gspread.authorize(creds).open_by_key(sheet_id)
        print()
        print(f"Spreadsheet reachable: {sheet.title}")
        print("Tabs:", ", ".join(ws.title for ws in sheet.worksheets()))


if __name__ == "__main__":
    main()
