"""Module 1 entry point — daily scheduler invocation (CLI flavor).

Run via n8n cron at 06:00 local OR directly during dry-runs:
    0 6 * * 1-5  cd /path/to/sdr-engine && python scripts/run_scheduler.py

For cross-host n8n triggering (n8n in Docker on a separate machine
than where the Python code lives), use the POST /scheduler/run webhook
endpoint on the Flask UI instead — same scheduler invocation, reachable
via the Cloudflare Tunnel.

Reads env (HUBSPOT_API_KEY, HUBSPOT_PROSPECTING_PIPELINE_ID,
HUBSPOT_PROSPECT_TYPE_PROPERTY, SQLITE_PATH, LLM_ENDPOINT,
LLM_MODEL_PRIMARY, LLM_MODEL_FALLBACK, OPENCLAW_WEBHOOK_URL,
DAILY_SEND_CAP, WORKING_DAYS_PER_YEAR, RENEWAL_LEAD_DAYS).

Exits 0 on a clean run (even if zero deals enqueued — empty queue is
normal on non-allocated days). Exits 1 on a fatal error so n8n / cron
can mark the workflow execution as failed and OpenClaw alerts.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

from sdr_engine.clients.hubspot import HubSpotClient
from sdr_engine.monitoring import notify_openclaw
from sdr_engine.scheduler import config_from_env, run_scheduler

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    sqlite_path = Path(os.path.expanduser(os.getenv("SQLITE_PATH", "~/.sdr-engine/queue.db")))
    if not sqlite_path.exists():
        print(f"ERROR: SQLite file not found at {sqlite_path}. "
              "Run scripts/init_db.py first.", file=sys.stderr)
        return 1

    hubspot_key = os.getenv("HUBSPOT_API_KEY", "")
    if not hubspot_key:
        print("ERROR: HUBSPOT_API_KEY is required", file=sys.stderr)
        return 1

    config, err = config_from_env(ROOT)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    webhook = os.getenv("OPENCLAW_WEBHOOK_URL", "")
    hubspot = HubSpotClient(api_key=hubspot_key)
    conn = sqlite3.connect(sqlite_path, isolation_level=None)
    try:
        result = run_scheduler(
            db=conn,
            hubspot=hubspot,
            prompt_path=ROOT / "prompts" / "draft-pipeline.txt",
            ctas_path=ROOT / "prompts" / "ctas.json",
            config=config,
            webhook_url=webhook or None,
        )
    finally:
        conn.close()

    summary = result.to_dict()
    print(json.dumps(summary, indent=2))

    if result.errors or result.llm_failures or result.enqueued:
        notify_openclaw(webhook, "scheduler_run_summary", summary)

    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
