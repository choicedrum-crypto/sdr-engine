"""Shared pytest fixtures."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def tmp_queue_db(tmp_path: Path) -> Path:
    """Create a fresh SQLite queue DB from sql/schema.sql for tests that need real storage."""
    db_path = tmp_path / "queue.db"
    schema = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema.read_text(encoding="utf-8"))
    finally:
        conn.close()
    return db_path


@pytest.fixture
def db_conn(tmp_queue_db: Path) -> sqlite3.Connection:
    """Open a connection against the freshly-initialized test DB."""
    conn = sqlite3.connect(tmp_queue_db)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()
