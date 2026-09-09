import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import db  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    """Give every test its own empty database file."""
    db.set_db_path(tmp_path / "test.db")
    db.connect()
    yield
    db.close()
