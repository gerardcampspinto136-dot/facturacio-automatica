"""Backups: the company's books must survive the computer they live on.

Everything is one SQLite file on one machine, and invoices must be kept for years (four
for Hacienda, six for the Código de Comercio). A dead disk, a stolen laptop or a bad
update would otherwise take all of it. So, every day:

1. **A consistent copy** of the database (SQLite's online backup, safe while the bot is
   running), with the company's settings and logo, zipped into data/backups/. The last
   `keep` are kept.
2. **Off the machine**, if configured:
   - `copy_to`: a folder that is somewhere else -- OneDrive/Google Drive synced folder,
     a USB disk, a network drive. The copy goes there, and the photographed receipts
     (the one thing that cannot be rebuilt from the database) are mirrored alongside.
   - `email`: once a week the copy is emailed. "auto" sends it to the company's own
     address once the company is configured.

Restoring: stop the bot, then `py -m src.backup --restaurar <copia.zip>` (it saves the
current database first, so a restore can itself be undone).
"""

import logging
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from src import db
from src.config_loader import get_config

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("data/backups")
RECEIPTS_DIR = Path("data/receipts")
EMAIL_EVERY = timedelta(days=7)
EMAIL_LIMIT = 18 * 1024 * 1024
PREFIX = "copia_"


def _meta_get(key: str) -> Optional[str]:
    row = db.connect().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(key: str, value: str) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def make_backup(dest_dir: Optional[Path] = None) -> Path:
    """Write one backup ZIP and return its path."""
    dest_dir = Path(dest_dir or BACKUP_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Never reuse a name: two copies in the same second (the safety copy a restore
    # takes, say) must not overwrite each other -- that once restored the wrong data.
    base = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    stamp, n = base, 1
    while (dest_dir / f"{PREFIX}{stamp}.zip").exists():
        n += 1
        stamp = f"{base}_{n}"
    snapshot = dest_dir / f"facturacio_{stamp}.db"

    target = sqlite3.connect(snapshot)
    try:
        # The online backup API copies a consistent state even mid-write.
        db.connect().backup(target)
    finally:
        target.close()

    zip_path = dest_dir / f"{PREFIX}{stamp}.zip"
    config = get_config()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as pack:
        pack.write(snapshot, "facturacio.db")
        for extra in ("config/company.yaml", config.logo_path):
            if extra and Path(extra).exists():
                pack.write(extra, extra.replace("\\", "/"))
        pack.writestr("LEEME.txt", (
            f"Copia de seguridad de {config.name} del "
            f"{datetime.now().strftime('%d/%m/%Y %H:%M')}.\r\n"
            "Para restaurarla: para el bot y ejecuta\r\n"
            f"  py -m src.backup --restaurar {zip_path.name}\r\n"))
    snapshot.unlink()
    _meta_set("backup:last", datetime.now().isoformat(timespec="seconds"))
    return zip_path


def list_backups(dest_dir: Optional[Path] = None) -> list[Path]:
    """Backups on this machine, newest first."""
    folder = Path(dest_dir or BACKUP_DIR)
    if not folder.exists():
        return []
    return sorted(folder.glob(f"{PREFIX}*.zip"), reverse=True)


def prune(keep: int, dest_dir: Optional[Path] = None) -> int:
    """Delete all but the newest `keep` backups. Returns how many were removed."""
    removed = 0
    for old in list_backups(dest_dir)[max(keep, 1):]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            logger.warning("Could not delete old backup %s", old)
    return removed


def _mirror_receipts(target: Path) -> int:
    """Copy receipt photos that are new or changed. Returns how many were copied."""
    if not RECEIPTS_DIR.exists():
        return 0
    copied = 0
    for source in RECEIPTS_DIR.rglob("*"):
        if not source.is_file():
            continue
        dest = target / "receipts" / source.relative_to(RECEIPTS_DIR)
        if dest.exists() and dest.stat().st_size == source.stat().st_size:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        copied += 1
    return copied


def email_address() -> Optional[str]:
    """Where the weekly copy goes: backup.email, or "auto" = the company's own address
    once the company is configured (never the example company's)."""
    config = get_config()
    setting = (getattr(config, "backup_email", "auto") or "").strip()
    if setting.lower() in ("", "off", "no", "false"):
        return None
    if setting.lower() == "auto":
        return None if config.is_placeholder or not config.email else config.email
    return setting


def last_backup() -> Optional[datetime]:
    value = _meta_get("backup:last")
    return datetime.fromisoformat(value) if value else None


def run_due() -> dict:
    """The daily job. Returns what it did, for the log and the tests."""
    config = get_config()
    done: dict = {}
    path = make_backup()
    done["backup"] = path
    done["pruned"] = prune(int(getattr(config, "backup_keep", 30) or 30))

    copy_to = (getattr(config, "backup_copy_to", "") or "").strip()
    if copy_to:
        target = Path(copy_to)
        try:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target / path.name)
            prune(int(getattr(config, "backup_keep", 30) or 30), target)
            done["copied_to"] = target
            done["receipts_mirrored"] = _mirror_receipts(target)
        except OSError:
            logger.exception("Could not copy the backup to %s", target)
            done["copy_error"] = str(target)

    address = email_address()
    last_email = _meta_get("backup:last_email")
    due = (last_email is None or
           datetime.now() - datetime.fromisoformat(last_email) >= EMAIL_EVERY)
    if address and due:
        from src.email_sender import send_email

        if path.stat().st_size <= EMAIL_LIMIT:
            try:
                send_email(address, f"Copia de seguridad — {config.name}",
                           "Copia de seguridad semanal de la facturación, adjunta.\n"
                           "Guárdala: si el ordenador falla, con este archivo se "
                           "recupera todo.", str(path), path.name)
                _meta_set("backup:last_email", datetime.now().isoformat(timespec="seconds"))
                done["emailed"] = address
            except Exception:
                logger.exception("Could not email the backup")
    return done


def restore(zip_path: str) -> Path:
    """Put a backup's database back. The current one is saved first. Returns that copy."""
    source = Path(zip_path)
    if not source.exists():
        candidate = BACKUP_DIR / zip_path
        if not candidate.exists():
            raise FileNotFoundError(f"No encuentro la copia {zip_path}")
        source = candidate
    safety = make_backup()
    db.close()
    live = Path(db.DB_PATH)
    with zipfile.ZipFile(source) as pack:
        with pack.open("facturacio.db") as stored, open(live, "wb") as out:
            shutil.copyfileobj(stored, out)
    for suffix in ("-wal", "-shm"):
        leftover = Path(str(live) + suffix)
        if leftover.exists():
            leftover.unlink()
    return safety


if __name__ == "__main__":  # pragma: no cover - command line
    from dotenv import load_dotenv

    load_dotenv()
    if len(sys.argv) >= 3 and sys.argv[1] in ("--restaurar", "--restore"):
        saved = restore(sys.argv[2])
        print(f"Restaurado. La base de datos anterior está guardada en {saved}")
    elif len(sys.argv) >= 2 and sys.argv[1] in ("--lista", "--list"):
        for item in list_backups():
            print(item)
    else:
        print(f"Copia hecha: {make_backup()}")
