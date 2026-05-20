"""Nightly sweep — retry missing note/task slots on send_partial rows >24h old.

Run via cron at e.g. 02:00 local:
    0 2 * * *  cd /path/to/sdr-engine && python scripts/sweep_partial_sends.py

Reads env (HUBSPOT_API_KEY, HUBSPOT_OWNER_ID, SQLITE_PATH,
OPENCLAW_WEBHOOK_URL) so it works from cron with a clean environment
when invoked from the project directory.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

from sdr_engine.clients.hubspot import HubSpotClient
from sdr_engine.monitoring import (
    notify_openclaw,
    sweep_failed_artifacts,
    sweep_orphan_enrichments,
    sweep_partial_sends,
)

load_dotenv()


def main() -> int:
    db_path = Path(os.path.expanduser(os.getenv("SQLITE_PATH", "~/.sdr-engine/queue.db")))
    if not db_path.exists():
        print(f"ERROR: SQLite file not found at {db_path}", file=sys.stderr)
        return 1

    hubspot_key = os.getenv("HUBSPOT_API_KEY", "")
    owner_id = os.getenv("HUBSPOT_OWNER_ID", "")
    webhook = os.getenv("OPENCLAW_WEBHOOK_URL", "")

    if not hubspot_key or not owner_id:
        print("ERROR: HUBSPOT_API_KEY and HUBSPOT_OWNER_ID required", file=sys.stderr)
        return 1

    hubspot = HubSpotClient(api_key=hubspot_key)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        partial = sweep_partial_sends(conn, hubspot, owner_id, webhook_url=webhook)
        failed_artifacts = sweep_failed_artifacts(conn, hubspot, owner_id, webhook_url=webhook)
        orphans = sweep_orphan_enrichments(conn, webhook_url=webhook)
    finally:
        conn.close()

    summary = {
        "partial_sends": {
            "checked": partial.checked,
            "healed": partial.healed,
            "still_failing": partial.still_failing,
        },
        "failed_artifacts": {
            "checked": failed_artifacts.checked,
            "healed": failed_artifacts.healed,
            "still_failing": failed_artifacts.still_failing,
        },
        "orphan_enrichments": {
            "checked": orphans.checked,
            "alerted": orphans.alerted,
        },
    }
    print(json.dumps(summary, indent=2))

    # Fire an aggregate alert if anything is still in a bad state — gives the
    # operator one ping per night summarizing what needs attention.
    if partial.still_failing or failed_artifacts.still_failing or orphans.checked:
        notify_openclaw(webhook, "nightly_sweep_summary", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
