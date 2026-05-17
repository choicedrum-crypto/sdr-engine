"""Module 1 entry point — daily scheduler invocation.

Run via n8n cron at 06:00 local:
    0 6 * * 1-5  cd /path/to/sdr-engine && python scripts/run_scheduler.py

Reads env (HUBSPOT_API_KEY, HUBSPOT_PROSPECTING_PIPELINE_ID,
HUBSPOT_PROSPECT_TYPE_PROPERTY, SQLITE_PATH, LLM_ENDPOINT,
LLM_MODEL_PRIMARY, LLM_MODEL_FALLBACK, OPENCLAW_WEBHOOK_URL,
DAILY_SEND_CAP, WORKING_DAYS_PER_YEAR, RENEWAL_LEAD_DAYS).

Exits 0 on a clean run (even if zero deals enqueued — empty queue is
normal on non-allocated days). Exits 1 on a fatal error so n8n can
mark the workflow execution as failed and OpenClaw alerts.
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
from sdr_engine.scheduler import ScheduleConfig, run_scheduler

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    sqlite_path = Path(os.path.expanduser(os.getenv("SQLITE_PATH", "~/.sdr-engine/queue.db")))
    if not sqlite_path.exists():
        print(f"ERROR: SQLite file not found at {sqlite_path}. "
              "Run scripts/init_db.py first.", file=sys.stderr)
        return 1

    hubspot_key = os.getenv("HUBSPOT_API_KEY", "")
    pipeline_id = os.getenv("HUBSPOT_PROSPECTING_PIPELINE_ID", "")
    if not hubspot_key or not pipeline_id:
        print("ERROR: HUBSPOT_API_KEY and HUBSPOT_PROSPECTING_PIPELINE_ID required",
              file=sys.stderr)
        return 1

    config = ScheduleConfig(
        pipeline_id=pipeline_id,
        funnel_type_property=os.getenv("HUBSPOT_PROSPECT_TYPE_PROPERTY", "funnel_type"),
        daily_send_cap=int(os.getenv("DAILY_SEND_CAP", "3")),
        working_days_per_year=int(os.getenv("WORKING_DAYS_PER_YEAR", "250")),
        renewal_lead_days=int(os.getenv("RENEWAL_LEAD_DAYS", "60")),
        holiday_file=ROOT / "config" / "holidays.json",
        llm_endpoint=os.getenv("LLM_ENDPOINT", "http://127.0.0.1:4000/v1/chat/completions"),
        llm_primary_model=os.getenv("LLM_MODEL_PRIMARY", "local-main"),
        llm_fallback_model=os.getenv("LLM_MODEL_FALLBACK", "heavy-main"),
        llm_timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
    )
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

    summary = {
        "candidates_examined": result.candidates_examined,
        "enqueued": result.enqueued,
        "enqueued_sharp": result.enqueued_sharp,
        "enqueued_round_robin": result.enqueued_round_robin,
        "dropped": result.dropped,
        "dropped_by_reason": result.dropped_by_reason,
        "deferred_overflow": result.deferred_overflow,
        "llm_failures": result.llm_failures,
        "errors": result.errors,
    }
    print(json.dumps(summary, indent=2))

    # Fire summary alert if anything notable happened
    if result.errors or result.llm_failures or result.enqueued:
        notify_openclaw(webhook, "scheduler_run_summary", summary)

    # Exit 1 only on outright failure; LLM failures alone don't fail the run
    # (Module 6 renders them with the AUTO-DRAFT FAILED banner).
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
