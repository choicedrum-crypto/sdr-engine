"""Apply sql/schema.sql to the SQLite queue database.

Idempotent — every CREATE in schema.sql uses IF NOT EXISTS. Safe to re-run.

Usage:
    python scripts/init_db.py
    SQLITE_PATH=/custom/path/queue.db python scripts/init_db.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "sql" / "schema.sql"


def main() -> int:
    db_path = Path(os.path.expanduser(os.getenv("SQLITE_PATH", "~/.sdr-engine/queue.db")))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not SCHEMA_PATH.exists():
        print(f"ERROR: schema not found at {SCHEMA_PATH}", file=sys.stderr)
        return 1

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        # Verify the four expected tables exist
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('queue', 'dropped', 'enrichment_cache', 'runs')"
        ).fetchall()
        tables = sorted(r[0] for r in rows)
    finally:
        conn.close()

    expected = ["dropped", "enrichment_cache", "queue", "runs"]
    if tables != expected:
        print(f"ERROR: expected tables {expected}, got {tables}", file=sys.stderr)
        return 1

    journal = sqlite3.connect(db_path).execute("PRAGMA journal_mode").fetchone()[0]
    print(f"OK — schema applied at {db_path}")
    print(f"   Tables: {', '.join(tables)}")
    print(f"   Journal mode: {journal}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
