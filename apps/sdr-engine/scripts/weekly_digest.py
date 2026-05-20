"""Weekly digest — KPI snapshot. Prints to stdout or fires a webhook.

Run via cron Sunday evenings:
    0 18 * * 0  cd /path/to/sdr-engine && python scripts/weekly_digest.py

Reads SQLITE_PATH from env. If OPENCLAW_WEBHOOK_URL is set, fires the
digest as a 'weekly_digest' event payload AS WELL AS printing to stdout
(so it works equally well from cron + via manual run).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

from sdr_engine.monitoring import notify_openclaw, weekly_digest

load_dotenv()


def _format_human(report: dict) -> str:
    """Pretty-print the dict — easier than reading raw JSON in a terminal."""
    lines = [
        "═══════════════════════════════════════════════════════",
        "  SDR ENGINE — WEEKLY DIGEST",
        "═══════════════════════════════════════════════════════",
        f"  Window: {report['window']}",
        "",
        f"  Sent           : {report['sent']:>4}",
        f"  Skipped        : {report['skipped']:>4}",
        f"  Edit-sent      : {report['edit_sent']:>4}",
        f"  Dropped        : {report['dropped']:>4}",
        "",
        f"  Skip rate      : {report['skip_rate']:>6.1%}",
        f"  Edit ratio     : {report['edit_ratio']:>6.1%}",
        "",
        "  Per prospect_type:",
    ]
    for ptype, count in sorted(report["per_prospect_type"].items(), key=lambda x: -x[1]):
        lines.append(f"    {ptype:<22}: {count}")

    lines.extend([
        "",
        "  Enrichment (last 30d):",
        f"    Success rate         : {report['enrichment_success_rate_30d']:>6.1%}",
        f"    Orphan count         : {report['orphan_enrichment_count']}",
        "",
        f"  Send-partial backlog : {report['send_partial_count']}",
        "═══════════════════════════════════════════════════════",
    ])
    return "\n".join(lines)


def main() -> int:
    db_path = Path(os.path.expanduser(os.getenv("SQLITE_PATH", "~/.sdr-engine/queue.db")))
    if not db_path.exists():
        print(f"ERROR: SQLite file not found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    try:
        report = weekly_digest(conn)
    finally:
        conn.close()

    print(_format_human(report))
    print()
    print(json.dumps(report, indent=2))

    webhook = os.getenv("OPENCLAW_WEBHOOK_URL", "")
    if webhook:
        notify_openclaw(webhook, "weekly_digest", report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
