"""
Interactive setup wizard for the Invoice Bot.
Run with:  py setup_wizard.py
"""

import os
import sys
import subprocess
from pathlib import Path

# ── helpers ───────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    display = f"{prompt}" + (f" [{default}]" if default else "") + ": "
    if secret:
        import getpass
        val = getpass.getpass(display)
    else:
        val = input(display).strip()
    return val or default


def section(title: str) -> None:
    print(f"\n{'─' * 55}")
    print(f"  {title}")
    print(f"{'─' * 55}")


def ok(msg: str) -> None:
    print(f"  ✅  {msg}")


def info(msg: str) -> None:
    print(f"  ℹ️   {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠️   {msg}")

# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    print("╔═══════════════════════════════════════════════════════╗")
    print("║         INVOICE BOT — Setup Wizard                   ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print()
    print("This wizard will create your .env file step by step.")
    print("Press Ctrl+C at any time to cancel.")

    env: dict[str, str] = {}

    # ── 1. Telegram ───────────────────────────────────────────────────────────
    section("STEP 1 — Telegram Bot Token")
    info("Open Telegram and find the bot called @BotFather")
    info("Send /newbot and follow the instructions to create a bot")
    info("Copy the token it gives you (looks like: 1234567890:ABCdef...)")
    print()
    token = ask("  Paste your Telegram bot token", secret=True)
    if not token:
        warn("Skipped — you must add TELEGRAM_BOT_TOKEN to .env manually")
    env["TELEGRAM_BOT_TOKEN"] = token

    # ── 2. Speech-to-text ─────────────────────────────────────────────────────
    section("STEP 2 — Speech-to-Text API  (Groq is FREE, recommended)")
    print("  Option A → Groq (free, fast)")
    print("    1. Go to https://console.groq.com")
    print("    2. Sign up / log in")
    print("    3. Click API Keys → Create API Key")
    print()
    print("  Option B → OpenAI (paid)")
    print("    1. Go to https://platform.openai.com/api-keys")
    print("    2. Create a new secret key")
    print()

    choice = ask("  Which do you want?  A (Groq/free) or B (OpenAI)", "A").upper()

    if choice == "A":
        key = ask("  Paste your Groq API key", secret=True)
        env["GROQ_API_KEY"] = key
        env["OPENAI_API_KEY"] = ""
        if key:
            ok("Groq API key saved")
    else:
        key = ask("  Paste your OpenAI API key", secret=True)
        env["OPENAI_API_KEY"] = key
        env["GROQ_API_KEY"] = ""
        if key:
            ok("OpenAI API key saved")

    # ── 3. Anthropic ──────────────────────────────────────────────────────────
    section("STEP 3 — Anthropic API Key  (for invoice data extraction)")
    info("Go to https://console.anthropic.com → API Keys → Create Key")
    print()
    key = ask("  Paste your Anthropic API key", secret=True)
    env["ANTHROPIC_API_KEY"] = key
    if key:
        ok("Anthropic API key saved")

    # ── 4. Google Cloud credentials ───────────────────────────────────────────
    section("STEP 4 — Google Cloud Credentials  (Sheets + Gmail)")
    print("  Follow these steps carefully:")
    print()
    print("  a) Go to https://console.cloud.google.com")
    print("  b) Create a new project (or select an existing one)")
    print("  c) Search for 'Google Sheets API' → Enable it")
    print("  d) Search for 'Gmail API'          → Enable it")
    print("  e) Go to APIs & Services → Credentials")
    print("  f) Click Create Credentials → OAuth 2.0 Client IDs")
    print("  g) Application type: Desktop application → Create")
    print("  h) Click the download (⬇) icon → save the JSON file")
    print("  i) Move/copy that file to:")
    print("       config/credentials/google_credentials.json")
    print()
    input("  Press Enter once the file is in place...")

    creds_path = "config/credentials/google_credentials.json"
    if Path(creds_path).exists():
        ok("google_credentials.json found")
    else:
        warn(f"File not found at '{creds_path}' — add it before running the bot")

    env["GOOGLE_CREDENTIALS_PATH"] = creds_path
    env["GOOGLE_TOKEN_PATH"] = "config/credentials/google_token.json"

    # ── 5. Google Spreadsheet ─────────────────────────────────────────────────
    section("STEP 5 — Google Spreadsheet ID")
    print("  Open your Google Sheet in the browser. The URL looks like:")
    print("  https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit")
    print()
    print("  Copy the long ID between /d/ and /edit")
    print()
    sid = ask("  Paste the Spreadsheet ID")
    env["SPREADSHEET_ID"] = sid
    env["SPREADSHEET_NAME"] = "Facturas"
    if sid:
        ok("Spreadsheet ID saved")

    # ── Write .env ────────────────────────────────────────────────────────────
    section("Writing .env file")
    with open(".env", "w", encoding="utf-8") as f:
        for key, val in env.items():
            f.write(f"{key}={val}\n")

    ok(".env created successfully")

    # ── 6. Install dependencies ───────────────────────────────────────────────
    section("STEP 6 — Install Python packages")
    do_install = ask("  Install packages now? (recommended)", "y").lower()
    if do_install in ("y", "yes", ""):
        print()
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], check=False)
        ok("Packages installed")

    # ── 7. Generate test logo ─────────────────────────────────────────────────
    section("STEP 7 — Generate placeholder logo")
    do_logo = ask("  Generate a placeholder logo for testing?", "y").lower()
    if do_logo in ("y", "yes", ""):
        try:
            subprocess.run([sys.executable, "generate_logo.py"], check=True)
            ok("Logo generated at config/logo.png")
        except Exception as e:
            warn(f"Could not generate logo: {e}")

    # ── Done ──────────────────────────────────────────────────────────────────
    print()
    print("╔═══════════════════════════════════════════════════════╗")
    print("║  Setup complete! Start the bot with:                 ║")
    print("║                                                       ║")
    print("║     py main.py                                        ║")
    print("║                                                       ║")
    print("║  On first run, a browser will open so you can        ║")
    print("║  authorise access to Google Sheets + Gmail.          ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nCancelled.")
