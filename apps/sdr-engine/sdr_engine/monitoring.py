"""Module 8 — monitoring + sweeps + heartbeat.

Three responsibilities:

1. RunHeartbeat — context manager that writes a `runs` row when a
   scheduler invocation starts and finalizes it when the run ends.
   This is what feeds the UI staleness banner (>25h since last
   successful run) per A6.

2. notify_openclaw() — non-blocking webhook fire for real-time alerts
   on LLM 5xx, HubSpot 4xx/5xx, orphan enrichments, and other "wake
   somebody up" events. Best-effort: failure to alert never raises.

3. Sweep functions — nightly scripts call these to find rows that
   need attention:
   - sweep_partial_sends: queue WHERE status='send_partial' AND
     actioned_at < now-24h. Retry the missing note/task slots.
   - sweep_orphan_enrichments: enrichment_cache WHERE status='orphan'.
     Fire one alert per row so a human can clean up the dangling
     HubSpot contact.
   - weekly_digest: aggregate counts for the digest email.

Module 8 doesn't run on its own — it provides the library that future
Module 1 (scheduler) + cron scripts use. The cron scripts are at
scripts/sweep_partial_sends.py + scripts/weekly_digest.py.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from sdr_engine.clients.hubspot import HubSpotClient
from sdr_engine.queue_ops import update_partial_ids
from sdr_engine.send import send_artifacts

# ─── Constants ──────────────────────────────────────────────────────
# Default cutoff for the partial-send sweep: row must be ≥24h old.
# Module 7's per-step retries are immediate; if a row is still partial
# after 24h, it's persistently failing — worth a fresh attempt.
DEFAULT_PARTIAL_SWEEP_AGE_HOURS = 24

# Per-run safety cap on retry attempts (prevents runaway cost if there's
# a HubSpot outage and everything fails again).
DEFAULT_SWEEP_MAX_PER_RUN = 20

# Webhook timeout. Short — alerts shouldn't block the workflow.
WEBHOOK_TIMEOUT_SECONDS = 5


# ─── Dataclasses ────────────────────────────────────────────────────
@dataclass
class RunRecord:
    """Mutable handle returned by RunHeartbeat. Module 1 mutates the
    enqueued_count + errors fields during the run, then __exit__ writes
    the final state.
    """
    run_id: str
    started_at: str
    ended_at: str | None = None
    status: str = "running"
    enqueued_count: int = 0
    dropped_count: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def record_error(self, source: str, message: str, **extra: Any) -> None:
        """Append one error record. The run finalizes to 'partial' on exit
        if any errors are recorded but no exception escaped."""
        self.errors.append({"source": source, "message": message, **extra})


@dataclass
class SweepResult:
    """Return shape of the sweep functions."""
    checked: int
    healed: int = 0          # rows that moved out of partial / orphan state
    still_failing: int = 0   # rows that we retried but couldn't resolve
    alerted: int = 0         # rows where we fired an alert (for orphans)
    errors: list[str] = field(default_factory=list)


# ─── Helpers ────────────────────────────────────────────────────────
def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _cutoff_iso(age_hours: int) -> str:
    """Return the ISO timestamp `age_hours` ago — rows older than this are eligible."""
    return (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat(timespec="seconds")


# ─── Heartbeat ──────────────────────────────────────────────────────
class RunHeartbeat:
    """Context manager wrapping a scheduler run.

    Usage (from future Module 1):

        with RunHeartbeat(db) as run:
            for deal in pipeline:
                try:
                    enqueue(deal)
                    run.enqueued_count += 1
                except DropException as exc:
                    run.dropped_count += 1
                    run.record_error("module-1", str(exc), deal_id=deal.id)

    On exit:
    - No exception, no errors → status='success'
    - No exception, errors recorded → status='partial'
    - Exception escaped → status='failed' (exception re-raises)
    """
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.record = RunRecord(run_id=str(uuid.uuid4()), started_at=_utcnow_iso())

    def __enter__(self) -> RunRecord:
        self.db.execute(
            "INSERT INTO runs (run_id, started_at, status, enqueued_count, dropped_count) "
            "VALUES (?, ?, 'running', 0, 0)",
            (self.record.run_id, self.record.started_at),
        )
        return self.record

    def __exit__(self, exc_type, exc_val, _exc_tb) -> bool:
        if exc_type is not None:
            self.record.status = "failed"
            self.record.record_error("uncaught", str(exc_val), exception_type=exc_type.__name__)
        elif self.record.errors:
            self.record.status = "partial"
        else:
            self.record.status = "success"

        errors_jsonl = "\n".join(json.dumps(e) for e in self.record.errors) or None
        self.db.execute(
            "UPDATE runs SET ended_at = ?, status = ?, enqueued_count = ?, "
            "dropped_count = ?, errors_jsonl = ? WHERE run_id = ?",
            (
                _utcnow_iso(),
                self.record.status,
                self.record.enqueued_count,
                self.record.dropped_count,
                errors_jsonl,
                self.record.run_id,
            ),
        )
        # Return False so any exception still propagates to the caller.
        return False


# ─── Webhook notifier ───────────────────────────────────────────────
def notify_openclaw(
    webhook_url: str | None,
    event_type: str,
    payload: dict[str, Any],
    *,
    timeout: int = WEBHOOK_TIMEOUT_SECONDS,
) -> bool:
    """Fire a webhook alert to OpenClaw. Returns True on 2xx, False otherwise.

    NEVER raises — alert failure must not cascade into the original failure
    (we already had one bad thing happen; making it worse by failing the
    alert handler is the opposite of what monitoring is for).

    Pass webhook_url=None or empty to no-op (e.g., during tests).
    """
    if not webhook_url:
        return False
    try:
        resp = requests.post(
            webhook_url,
            json={
                "event_type": event_type,
                "timestamp": _utcnow_iso(),
                "payload": payload,
            },
            timeout=timeout,
        )
        return 200 <= resp.status_code < 300
    except Exception:  # noqa: BLE001 — alerting must never escalate
        return False


# ─── Sweeps ─────────────────────────────────────────────────────────
def sweep_partial_sends(
    db: sqlite3.Connection,
    hubspot: HubSpotClient,
    owner_id: str,
    *,
    older_than_hours: int = DEFAULT_PARTIAL_SWEEP_AGE_HOURS,
    max_per_run: int = DEFAULT_SWEEP_MAX_PER_RUN,
    webhook_url: str | None = None,
) -> SweepResult:
    """Retry missing note/task slots on send_partial rows older than the cutoff.

    Rows that succeed move to status='sent' via update_partial_ids().
    Rows that still fail stay 'send_partial' for the next nightly sweep.
    """
    rows = db.execute(
        "SELECT id, deal_id, company_id, company_name, contact_id, "
        "draft_subject, draft_body, deal_note_body, call_script_json, "
        "hubspot_engagement_id, hubspot_note_id, hubspot_task_id "
        "FROM queue WHERE status = 'send_partial' AND actioned_at < ? "
        "ORDER BY actioned_at ASC LIMIT ?",
        (_cutoff_iso(older_than_hours), max_per_run),
    ).fetchall()

    result = SweepResult(checked=len(rows))
    for row in rows:
        try:
            send_result = send_artifacts(
                artifacts={
                    "deal_note": row[7],
                    "call_script": json.loads(row[8]) if row[8] else {},
                },
                chosen_subject=row[5],
                body=row[6],
                owner_id=owner_id,
                contact_id=row[4],
                deal_id=row[1],
                company_name=row[3],
                hubspot=hubspot,
                existing_engagement_id=row[9],
                existing_note_id=row[10],
                existing_task_id=row[11],
            )
        except Exception as exc:  # noqa: BLE001 — sweep must continue on row failures
            result.errors.append(f"row {row[0]}: {exc}")
            result.still_failing += 1
            continue

        update_partial_ids(db, row[0], note_id=send_result.note_id, task_id=send_result.task_id)
        if send_result.status == "sent":
            result.healed += 1
        else:
            result.still_failing += 1
            notify_openclaw(
                webhook_url,
                "partial_send_still_failing",
                {
                    "queue_id": row[0],
                    "deal_id": row[1],
                    "company_name": row[3],
                    "engagement_id": row[9],
                    "missing_note": row[10] is None and send_result.note_id is None,
                    "missing_task": row[11] is None and send_result.task_id is None,
                    "errors": send_result.errors,
                },
            )
    return result


def sweep_orphan_enrichments(
    db: sqlite3.Connection,
    webhook_url: str | None = None,
) -> SweepResult:
    """Fire one alert per orphan enrichment_cache row.

    These rows happen when Module 2.5's rollback delete failed — there's
    a dangling HubSpot contact and the cache row preserves its id. A
    human needs to either delete the contact in HubSpot manually or
    mark it intentional. This sweep doesn't auto-clean; it just makes
    the orphans visible.
    """
    rows = db.execute(
        "SELECT company_id, contact_id, matched_title, enriched_at FROM enrichment_cache "
        "WHERE status = 'orphan' ORDER BY enriched_at ASC"
    ).fetchall()

    result = SweepResult(checked=len(rows))
    for row in rows:
        notify_openclaw(
            webhook_url,
            "enrichment_orphan",
            {
                "company_id": row[0],
                "contact_id": row[1],
                "title_at_enrichment": row[2],
                "orphaned_since": row[3],
            },
        )
        result.alerted += 1
    return result


def sweep_failed_artifacts(
    db: sqlite3.Connection,
    hubspot: HubSpotClient,
    owner_id: str,
    *,
    max_per_run: int = DEFAULT_SWEEP_MAX_PER_RUN,
    webhook_url: str | None = None,
) -> SweepResult:
    """Retry NULL note/task ids on rows that are otherwise 'sent' or 'edit-sent'.

    The status='sent' branch handles a different failure mode than
    send_partial: this is for rows where Module 7 succeeded fully but a
    subsequent system action (e.g., manual SQL update) zeroed out an id,
    OR rows from before the send_partial state existed in the schema.
    Belt-and-suspenders for Module 8 spec compliance.
    """
    rows = db.execute(
        "SELECT id, deal_id, company_id, company_name, contact_id, "
        "draft_subject, draft_body, deal_note_body, call_script_json, "
        "hubspot_engagement_id, hubspot_note_id, hubspot_task_id "
        "FROM queue WHERE status IN ('sent', 'edit-sent') "
        "AND (hubspot_note_id IS NULL OR hubspot_task_id IS NULL) "
        "ORDER BY actioned_at ASC LIMIT ?",
        (max_per_run,),
    ).fetchall()
    # Reuse the partial-send sweep retry logic but on a different row set.
    # No duplication — same SQL columns + same send_artifacts call pattern.
    result = SweepResult(checked=len(rows))
    for row in rows:
        try:
            send_result = send_artifacts(
                artifacts={
                    "deal_note": row[7],
                    "call_script": json.loads(row[8]) if row[8] else {},
                },
                chosen_subject=row[5],
                body=row[6],
                owner_id=owner_id,
                contact_id=row[4],
                deal_id=row[1],
                company_name=row[3],
                hubspot=hubspot,
                existing_engagement_id=row[9],
                existing_note_id=row[10],
                existing_task_id=row[11],
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"row {row[0]}: {exc}")
            result.still_failing += 1
            continue
        update_partial_ids(db, row[0], note_id=send_result.note_id, task_id=send_result.task_id)
        if send_result.note_id and send_result.task_id:
            result.healed += 1
        else:
            result.still_failing += 1
            notify_openclaw(webhook_url, "sent_row_still_missing_artifacts", {
                "queue_id": row[0], "deal_id": row[1],
                "missing_note": send_result.note_id is None,
                "missing_task": send_result.task_id is None,
            })
    return result


# ─── Weekly digest ──────────────────────────────────────────────────
def weekly_digest(db: sqlite3.Connection) -> dict[str, Any]:
    """Aggregate KPIs from the queue and enrichment_cache for the weekly
    digest (matches docs/ARCHITECTURE.md Module 8 spec).

    All counts scoped to the last 7 days unless noted. Returns dict —
    caller decides how to format (email, Slack message, JSON dump).
    """
    cutoff_7d = _cutoff_iso(24 * 7)
    cutoff_30d = _cutoff_iso(24 * 30)

    def _count(query: str, *params: Any) -> int:
        return db.execute(query, params).fetchone()[0] or 0

    sent_last_7d = _count(
        "SELECT COUNT(*) FROM queue WHERE status IN ('sent', 'edit-sent') AND actioned_at > ?",
        cutoff_7d,
    )
    skipped_last_7d = _count(
        "SELECT COUNT(*) FROM queue WHERE status = 'skipped' AND actioned_at > ?",
        cutoff_7d,
    )
    edit_sent_last_7d = _count(
        "SELECT COUNT(*) FROM queue WHERE status = 'edit-sent' AND actioned_at > ?",
        cutoff_7d,
    )
    dropped_last_7d = _count(
        "SELECT COUNT(*) FROM dropped WHERE dropped_at > ?", cutoff_7d,
    )

    actioned_last_7d = sent_last_7d + skipped_last_7d
    skip_rate = (skipped_last_7d / actioned_last_7d) if actioned_last_7d else 0.0
    edit_ratio = (edit_sent_last_7d / sent_last_7d) if sent_last_7d else 0.0

    # Per-prospect-type touches in the last 7 days
    per_type_rows = db.execute(
        "SELECT prospect_type, COUNT(*) FROM queue "
        "WHERE status IN ('sent', 'edit-sent') AND actioned_at > ? "
        "GROUP BY prospect_type",
        (cutoff_7d,),
    ).fetchall()
    per_type = {r[0]: r[1] for r in per_type_rows}

    # Per contact_source conversion view (last 30d for signal)
    per_contact_source_rows = db.execute(
        "SELECT contact_source, status, COUNT(*) FROM queue "
        "WHERE actioned_at > ? GROUP BY contact_source, status",
        (cutoff_30d,),
    ).fetchall()
    per_contact_source: dict[str, dict[str, int]] = {}
    for source, status, count in per_contact_source_rows:
        per_contact_source.setdefault(source, {})[status] = count

    # Enrichment success rate over last 30d
    enrich_total = _count(
        "SELECT COUNT(*) FROM enrichment_cache WHERE enriched_at > ?", cutoff_30d,
    )
    enrich_success = _count(
        "SELECT COUNT(*) FROM enrichment_cache WHERE status = 'success' AND enriched_at > ?",
        cutoff_30d,
    )
    enrichment_success_rate = (enrich_success / enrich_total) if enrich_total else 0.0

    # Orphan + error counts (point-in-time)
    orphan_count = _count("SELECT COUNT(*) FROM enrichment_cache WHERE status = 'orphan'")
    partial_count = _count("SELECT COUNT(*) FROM queue WHERE status = 'send_partial'")

    return {
        "window": "last_7_days",
        "sent": sent_last_7d,
        "skipped": skipped_last_7d,
        "edit_sent": edit_sent_last_7d,
        "dropped": dropped_last_7d,
        "skip_rate": round(skip_rate, 3),
        "edit_ratio": round(edit_ratio, 3),
        "per_prospect_type": per_type,
        "per_contact_source_30d": per_contact_source,
        "enrichment_success_rate_30d": round(enrichment_success_rate, 3),
        "orphan_enrichment_count": orphan_count,
        "send_partial_count": partial_count,
    }
