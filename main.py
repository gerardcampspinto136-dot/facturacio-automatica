"""Entry point for the Invoice Bot."""

from dotenv import load_dotenv

load_dotenv()

from src.bot import run_bot  # noqa: E402 — must load .env first

if __name__ == "__main__":
    run_bot()
