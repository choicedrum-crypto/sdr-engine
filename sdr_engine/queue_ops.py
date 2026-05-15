"""Queue row read/write helpers.

The route handlers in ui/server.py are thin glue: parse request → call
an orchestrator (Module 4's draft, Module 7's send_artifacts) → call one
of these helpers to read/write the SQLite queue row. Everything db-touching
lives here so the routes stay testable without Flask test-client setup
and the helpers stay testable without HTTP.

All functions take an open sqlite3.Connection. The caller is responsible
for connection lifecycle (the Flask app reuses one connection per
request via Flask's g object).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass
class QueueRow:
    """Typed view of a queue row. Mirrors sql/schema.sql column-for-column;
    Module 6 routes pass this around instead of raw sqlite3.Row tuples.
    """
    id: int
    deal_id: str
    company_id: str
    company_name: str
    contact_id: str
    contact_email: str
    contact_first_name: str | None
    contact_last_name: str | None
    quote_amount: float | None
    product_line: str | None
    quote_date_human: str | None
    renewal_date: str | None
    policy_excerpt: str | None
    hook_text: str | None
    hook_source: str | None
    send_reason: str
    prospect_type: str
    funnel_types_all: str | None
    contact_source: str
    draft_subject: str
    draft_subject_alt: str | None
    draft_body: str
    cta_chosen: str | None
    deal_note_body: str
    call_script_json: str
    anchor_used: str | None
    hook_used: str | None
    llm_model_used: str | None
    llm_attempts: int
    status: str
    enqueued_at: str
    actioned_at: str | None
    edit_diff: str | None
    hubspot_engagement_id: str | None
    hubspot_note_id: str | None
    hubspot_task_id: str | None

    def call_script(self) -> dict[str, Any]:
        """Decode call_script_json into a dict for downstream rendering."""
        return json.loads(self.call_script_json) if self.call_script_json else {}

    def artifacts_for_send(self) -> dict[str, Any]:
        """Shape that send_artifacts() expects: {'deal_note': str, 'call_script': dict}."""
        return {"deal_note": self.deal_note_body, "call_script": self.call_script()}


@dataclass
class LastRunInfo:
    """Heartbeat info from the runs table (per A6) — drives UI staleness banner."""
    started_at: str | None
    status: str | None  # 'running' | 'success' | 'partial' | 'failed' | None
    enqueued_count: int
    age_seconds: int | None = None  # populated by get_last_run()


# Columns selected for QueueRow. Order matches the dataclass exactly so we
# can zip the SELECT result into a QueueRow without named-column lookup.
QUEUE_COLUMNS = (
    "id, deal_id, company_id, company_name, contact_id, contact_email, "
    "contact_first_name, contact_last_name, quote_amount, product_line, "
    "quote_date_human, renewal_date, policy_excerpt, hook_text, hook_source, "
    "send_reason, prospect_type, funnel_types_all, contact_source, "
    "draft_subject, draft_subject_alt, draft_body, cta_chosen, "
    "deal_note_body, call_script_json, anchor_used, hook_used, "
    "llm_model_used, llm_attempts, status, enqueued_at, actioned_at, "
    "edit_diff, hubspot_engagement_id, hubspot_note_id, hubspot_task_id"
)


def _row_to_queue(row: tuple) -> QueueRow:
    """Build a QueueRow from a SELECT row matching QUEUE_COLUMNS order."""
    return QueueRow(*row)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ─── Reads ─────────────────────────────────────────────────────────
def get_next_card(db: sqlite3.Connection) -> QueueRow | None:
    """Fetch the next card the SDR should see in /queue.

    Priority:
      1. send_partial rows (oldest first) — user needs to finish a partial send
         before being handed a new card; otherwise partial state piles up.
      2. pending rows with send_reason='renewal' (sharp bucket — highest leverage),
         oldest enqueued_at first.
      3. pending rows with send_reason='round-robin', oldest enqueued_at first.

    Returns None if no pending/send_partial rows exist (UI shows caught_up).
    """
    row = db.execute(
        f"SELECT {QUEUE_COLUMNS} FROM queue "
        "WHERE status IN ('pending', 'send_partial') "
        "ORDER BY "
        "  CASE status WHEN 'send_partial' THEN 0 ELSE 1 END, "
        "  CASE send_reason WHEN 'renewal' THEN 0 ELSE 1 END, "
        "  enqueued_at ASC "
        "LIMIT 1"
    ).fetchone()
    return _row_to_queue(row) if row else None


def get_by_id(db: sqlite3.Connection, queue_id: int) -> QueueRow | None:
    """Fetch a specific queue row by id. Returns None if not found."""
    row = db.execute(
        f"SELECT {QUEUE_COLUMNS} FROM queue WHERE id = ?", (queue_id,)
    ).fetchone()
    return _row_to_queue(row) if row else None


def get_last_run(db: sqlite3.Connection) -> LastRunInfo:
    """Most recent runs row. Drives UI staleness banner (>25h = warn)."""
    row = db.execute(
        "SELECT started_at, status, enqueued_count FROM runs "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return LastRunInfo(started_at=None, status=None, enqueued_count=0, age_seconds=None)

    started_at_iso = row[0]
    age = None
    try:
        ts = datetime.fromisoformat(started_at_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age = int((datetime.now(UTC) - ts).total_seconds())
    except (ValueError, AttributeError):
        pass
    return LastRunInfo(started_at=started_at_iso, status=row[1], enqueued_count=row[2], age_seconds=age)


def is_scheduler_running_now(db: sqlite3.Connection) -> bool:
    """True if any runs row has status='running' (UI shows warming state)."""
    row = db.execute(
        "SELECT 1 FROM runs WHERE status = 'running' LIMIT 1"
    ).fetchone()
    return row is not None


def is_stale_after_load(enqueued_at_iso: str, stale_after_seconds: int = 2 * 60 * 60) -> bool:
    """True if the row was loaded long enough ago that we should re-fetch
    before allowing send (the row might have been actioned in another tab,
    or the scheduler may have skipped it). The UI passes the load-time
    enqueued_at back via a hidden field; this checks the threshold.
    """
    try:
        ts = datetime.fromisoformat(enqueued_at_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (ValueError, AttributeError):
        return True  # malformed → treat as stale, force a re-fetch
    return (datetime.now(UTC) - ts) > timedelta(seconds=stale_after_seconds)


# ─── Writes ────────────────────────────────────────────────────────
def mark_sent(
    db: sqlite3.Connection,
    queue_id: int,
    *,
    engagement_id: str | None,
    note_id: str | None,
    task_id: str | None,
    edit_diff: dict[str, str] | None,
    llm_model_used: str | None = None,
) -> None:
    """Persist a successful (or partially-successful) send.

    If `edit_diff` is provided (SDR edited the body before send), store as
    JSON in the edit_diff column for prompt-tuning analysis.
    If all three IDs are populated: status='sent' (or 'edit-sent' if edited).
    If engagement_id but not all three: status='send_partial'.
    Caller decides which state to write via the explicit status param? No —
    derive here to avoid divergence.
    """
    if engagement_id and note_id and task_id:
        status = "edit-sent" if edit_diff else "sent"
    elif engagement_id:
        status = "send_partial"
    else:
        # Shouldn't normally happen — caller should not call mark_sent without
        # at least an engagement_id. Preserve 'pending' so the user can retry.
        return
    db.execute(
        "UPDATE queue SET status = ?, actioned_at = ?, "
        "hubspot_engagement_id = ?, hubspot_note_id = ?, hubspot_task_id = ?, "
        "edit_diff = ?, llm_model_used = COALESCE(?, llm_model_used) "
        "WHERE id = ?",
        (
            status,
            _utcnow_iso(),
            engagement_id,
            note_id,
            task_id,
            json.dumps(edit_diff) if edit_diff else None,
            llm_model_used,
            queue_id,
        ),
    )


def mark_skipped(db: sqlite3.Connection, queue_id: int) -> None:
    """SDR clicked Skip — cools off this card for 30 days (Module 1 enforces)."""
    db.execute(
        "UPDATE queue SET status = 'skipped', actioned_at = ? WHERE id = ?",
        (_utcnow_iso(), queue_id),
    )


def update_partial_ids(
    db: sqlite3.Connection,
    queue_id: int,
    *,
    note_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Fill in a previously-NULL note_id or task_id from a successful retry.

    If both columns are now non-null after the update, promote status from
    'send_partial' to 'sent'.
    """
    sets: list[str] = []
    args: list[Any] = []
    if note_id is not None:
        sets.append("hubspot_note_id = ?")
        args.append(note_id)
    if task_id is not None:
        sets.append("hubspot_task_id = ?")
        args.append(task_id)
    if not sets:
        return  # nothing to update
    args.append(queue_id)
    db.execute(f"UPDATE queue SET {', '.join(sets)} WHERE id = ?", args)

    # Re-check: if all three IDs are now populated, promote to 'sent'.
    row = db.execute(
        "SELECT hubspot_engagement_id, hubspot_note_id, hubspot_task_id FROM queue WHERE id = ?",
        (queue_id,),
    ).fetchone()
    if row and all(row):
        db.execute(
            "UPDATE queue SET status = 'sent' WHERE id = ? AND status = 'send_partial'",
            (queue_id,),
        )


# ─── Diff detection ────────────────────────────────────────────────
def compute_edit_diff(original_body: str, posted_body: str) -> dict[str, str] | None:
    """Return {'before': original, 'after': posted} if they differ; None if not.

    Whitespace-trimmed comparison so cosmetic whitespace doesn't trigger
    a false edit. The stored values are the EXACT bodies (no trimming) —
    only the equality check is normalized.
    """
    if original_body.strip() == posted_body.strip():
        return None
    return {"before": original_body, "after": posted_body}


# ─── Force-enqueue (stub, completed when Modules 1 + 3 land) ───────
@dataclass
class ForceResult:
    success: bool
    queue_id: int | None
    error: str | None = None
    needs: list[str] = field(default_factory=list)  # what modules are still missing


def force_enqueue_not_yet_supported() -> ForceResult:
    """Force-enqueue requires Module 1 (scheduler classifier) and Module 3
    (anchor + hook extraction) to fully implement the architecture's
    'runs Modules 2-5 inline' spec. Module 6 returns 501 from /queue/force
    with this explanation until those land.

    Stub returns a typed result so the route + tests can assert on its shape.
    """
    return ForceResult(
        success=False,
        queue_id=None,
        error="force-enqueue requires Module 1 (classifier) and Module 3 (enrichment) "
              "to wire together with the existing Module 2.5 + Module 4 paths.",
        needs=["module-1-scheduler", "module-3-enrichment"],
    )
