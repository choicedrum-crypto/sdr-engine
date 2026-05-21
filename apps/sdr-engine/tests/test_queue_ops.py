"""Unit tests for sdr_engine.queue_ops — pure SQLite, no Flask, no HTTP."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from sdr_engine.queue_ops import (
    compute_edit_diff,
    force_enqueue_not_yet_supported,
    get_by_id,
    get_last_run,
    get_next_card,
    is_scheduler_running_now,
    is_stale_after_load,
    mark_sent,
    mark_skipped,
    update_partial_ids,
)


# ─── Test helpers ──────────────────────────────────────────────────
def _insert_card(
    db: sqlite3.Connection,
    *,
    deal_id: str = "deal-1",
    status: str = "pending",
    send_reason: str = "round-robin",
    prospect_type: str = "former_client",
    enqueued_at: str | None = None,
    engagement_id: str | None = None,
    note_id: str | None = None,
    task_id: str | None = None,
    draft_body: str = "Dear Daniel,\n\nbody text\n\nBest regards,\nDaniel Bradley",
) -> int:
    """Insert a queue row, return its id. All required schema fields set with defaults."""
    cols = {
        "deal_id": deal_id,
        "company_id": "co-1",
        "company_name": "Acme",
        "contact_id": "ct-1",
        "contact_email": "test@acme.com",
        "send_reason": send_reason,
        "prospect_type": prospect_type,
        "draft_subject": "Test subject",
        "draft_body": draft_body,
        "deal_note_body": "SDR Brief — test",
        "call_script_json": json.dumps({"opening": "Hi", "context_bridge": "x", "observation": "y", "open_question": "z", "objection_bridges": {"already_covered": "a", "not_interested": "b", "send_info": "c"}}),
        "status": status,
        "hubspot_engagement_id": engagement_id,
        "hubspot_note_id": note_id,
        "hubspot_task_id": task_id,
    }
    if enqueued_at:
        cols["enqueued_at"] = enqueued_at
    columns_str = ", ".join(cols.keys())
    placeholders = ", ".join("?" * len(cols))
    cursor = db.execute(
        f"INSERT INTO queue ({columns_str}) VALUES ({placeholders})",
        tuple(cols.values()),
    )
    return cursor.lastrowid


# ─── get_next_card priority ────────────────────────────────────────
def test_get_next_card_returns_none_on_empty_queue(db_conn: sqlite3.Connection) -> None:
    assert get_next_card(db_conn) is None


def test_get_next_card_returns_pending_row(db_conn: sqlite3.Connection) -> None:
    cid = _insert_card(db_conn)
    card = get_next_card(db_conn)
    assert card is not None
    assert card.id == cid
    assert card.status == "pending"


def test_get_next_card_prioritizes_send_partial_over_pending(db_conn: sqlite3.Connection) -> None:
    """A user with an incomplete send must finish it before being handed a new card."""
    pending_id = _insert_card(db_conn, status="pending", deal_id="deal-A")
    partial_id = _insert_card(db_conn, status="send_partial", deal_id="deal-B",
                              engagement_id="eng-1", note_id=None, task_id="task-1")
    card = get_next_card(db_conn)
    assert card is not None
    assert card.id == partial_id, f"send_partial should win over pending; got id={card.id}"
    assert pending_id != partial_id  # sanity


def test_get_next_card_prioritizes_renewal_over_round_robin(db_conn: sqlite3.Connection) -> None:
    """Sharp-bucket cards take priority over round-robin within pending rows."""
    older_iso = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    rr_id = _insert_card(db_conn, send_reason="round-robin", enqueued_at=older_iso, deal_id="deal-A")
    renewal_id = _insert_card(db_conn, send_reason="renewal", deal_id="deal-B")
    card = get_next_card(db_conn)
    assert card is not None
    assert card.id == renewal_id, f"renewal should win even when round-robin enqueued earlier; got id={card.id}"
    assert rr_id != renewal_id


def test_get_next_card_oldest_first_within_same_priority(db_conn: sqlite3.Connection) -> None:
    # SQLite CURRENT_TIMESTAMP default uses "YYYY-MM-DD HH:MM:SS" (space, no TZ).
    # Use the same format so string-sort gives the expected chronological order.
    older = _insert_card(
        db_conn,
        enqueued_at=(datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        deal_id="deal-A",
    )
    _newer = _insert_card(db_conn, deal_id="deal-B")
    card = get_next_card(db_conn)
    assert card.id == older


def test_get_next_card_skips_sent_skipped_dropped(db_conn: sqlite3.Connection) -> None:
    for status in ("sent", "edit-sent", "skipped"):
        _insert_card(db_conn, status=status)
    assert get_next_card(db_conn) is None


# ─── get_by_id ─────────────────────────────────────────────────────
def test_get_by_id_returns_row(db_conn: sqlite3.Connection) -> None:
    cid = _insert_card(db_conn)
    card = get_by_id(db_conn, cid)
    assert card is not None
    assert card.id == cid


def test_get_by_id_returns_none_for_missing(db_conn: sqlite3.Connection) -> None:
    assert get_by_id(db_conn, 99999) is None


# ─── Heartbeat / scheduler state ───────────────────────────────────
def test_get_last_run_empty_returns_null_info(db_conn: sqlite3.Connection) -> None:
    info = get_last_run(db_conn)
    assert info.started_at is None
    assert info.status is None
    assert info.age_seconds is None


def test_get_last_run_returns_latest_with_age(db_conn: sqlite3.Connection) -> None:
    old = (datetime.now(UTC) - timedelta(hours=30)).isoformat(timespec="seconds")
    recent = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")
    db_conn.execute(
        "INSERT INTO runs (run_id, started_at, status, enqueued_count) VALUES (?, ?, ?, ?)",
        ("old-uuid", old, "success", 3),
    )
    db_conn.execute(
        "INSERT INTO runs (run_id, started_at, status, enqueued_count) VALUES (?, ?, ?, ?)",
        ("new-uuid", recent, "success", 5),
    )
    info = get_last_run(db_conn)
    assert info.started_at == recent
    assert info.status == "success"
    assert info.enqueued_count == 5
    assert info.age_seconds is not None
    assert 0 < info.age_seconds < 600  # within ~10min


def test_is_scheduler_running_detects_running_row(db_conn: sqlite3.Connection) -> None:
    assert is_scheduler_running_now(db_conn) is False
    db_conn.execute(
        "INSERT INTO runs (run_id, status, enqueued_count) VALUES ('x', 'running', 0)"
    )
    assert is_scheduler_running_now(db_conn) is True


# ─── Staleness ─────────────────────────────────────────────────────
def test_is_stale_after_load_fresh_returns_false() -> None:
    fresh = (datetime.now(UTC) - timedelta(minutes=10)).isoformat(timespec="seconds")
    assert is_stale_after_load(fresh) is False


def test_is_stale_after_load_old_returns_true() -> None:
    old = (datetime.now(UTC) - timedelta(hours=5)).isoformat(timespec="seconds")
    assert is_stale_after_load(old) is True


def test_is_stale_after_load_malformed_returns_true() -> None:
    """If we can't parse, treat as stale — force a refetch rather than silently sending."""
    assert is_stale_after_load("not a date") is True


# ─── mark_sent: status derivation ──────────────────────────────────
def test_mark_sent_full_success_sets_sent_status(db_conn: sqlite3.Connection) -> None:
    cid = _insert_card(db_conn)
    mark_sent(db_conn, cid, engagement_id="eng-1", note_id="note-1", task_id="task-1", edit_diff=None)
    row = get_by_id(db_conn, cid)
    assert row.status == "sent"
    assert row.actioned_at is not None
    assert row.edit_diff is None


def test_mark_sent_with_edit_diff_sets_edit_sent(db_conn: sqlite3.Connection) -> None:
    cid = _insert_card(db_conn)
    mark_sent(
        db_conn, cid,
        engagement_id="eng-1", note_id="note-1", task_id="task-1",
        edit_diff={"before": "orig", "after": "edited"},
    )
    row = get_by_id(db_conn, cid)
    assert row.status == "edit-sent"
    assert row.edit_diff is not None
    diff = json.loads(row.edit_diff)
    assert diff == {"before": "orig", "after": "edited"}


def test_mark_sent_partial_sets_send_partial_status(db_conn: sqlite3.Connection) -> None:
    cid = _insert_card(db_conn)
    mark_sent(db_conn, cid, engagement_id="eng-1", note_id=None, task_id="task-1", edit_diff=None)
    row = get_by_id(db_conn, cid)
    assert row.status == "send_partial"
    assert row.hubspot_engagement_id == "eng-1"
    assert row.hubspot_note_id is None
    assert row.hubspot_task_id == "task-1"


def test_mark_sent_without_engagement_is_noop(db_conn: sqlite3.Connection) -> None:
    """Defensive: if all 3 IDs are None, leave the row alone — caller bug."""
    cid = _insert_card(db_conn)
    mark_sent(db_conn, cid, engagement_id=None, note_id=None, task_id=None, edit_diff=None)
    row = get_by_id(db_conn, cid)
    assert row.status == "pending"  # unchanged


# ─── mark_skipped ──────────────────────────────────────────────────
def test_mark_skipped_updates_status_and_actioned_at(db_conn: sqlite3.Connection) -> None:
    cid = _insert_card(db_conn)
    mark_skipped(db_conn, cid)
    row = get_by_id(db_conn, cid)
    assert row.status == "skipped"
    assert row.actioned_at is not None


# ─── update_partial_ids ────────────────────────────────────────────
def test_update_partial_ids_fills_note_and_promotes_to_sent(db_conn: sqlite3.Connection) -> None:
    cid = _insert_card(
        db_conn,
        status="send_partial",
        engagement_id="eng-1", note_id=None, task_id="task-1",
    )
    update_partial_ids(db_conn, cid, note_id="note-NEW")
    row = get_by_id(db_conn, cid)
    assert row.hubspot_note_id == "note-NEW"
    assert row.status == "sent"  # all 3 ids now present


def test_update_partial_ids_fills_task_but_note_still_missing_stays_partial(db_conn: sqlite3.Connection) -> None:
    cid = _insert_card(
        db_conn,
        status="send_partial",
        engagement_id="eng-1", note_id=None, task_id=None,
    )
    update_partial_ids(db_conn, cid, task_id="task-NEW")
    row = get_by_id(db_conn, cid)
    assert row.hubspot_task_id == "task-NEW"
    assert row.hubspot_note_id is None
    assert row.status == "send_partial"  # still missing note


def test_update_partial_ids_no_op_when_no_args(db_conn: sqlite3.Connection) -> None:
    cid = _insert_card(db_conn, status="send_partial", engagement_id="eng-1")
    update_partial_ids(db_conn, cid)  # both kwargs None
    row = get_by_id(db_conn, cid)
    assert row.status == "send_partial"


# ─── compute_edit_diff ─────────────────────────────────────────────
def test_compute_edit_diff_returns_none_when_unchanged() -> None:
    assert compute_edit_diff("same", "same") is None


def test_compute_edit_diff_returns_none_when_only_whitespace_differs() -> None:
    """Whitespace-only changes shouldn't trigger edit_diff capture."""
    assert compute_edit_diff("hello world", "hello world\n") is None


def test_compute_edit_diff_returns_dict_on_real_change() -> None:
    diff = compute_edit_diff("original", "edited version")
    assert diff == {"before": "original", "after": "edited version"}


# ─── QueueRow helpers ──────────────────────────────────────────────
def test_queue_row_call_script_decodes_json(db_conn: sqlite3.Connection) -> None:
    cid = _insert_card(db_conn)
    row = get_by_id(db_conn, cid)
    script = row.call_script()
    assert script["opening"] == "Hi"
    assert script["objection_bridges"]["send_info"] == "c"


def test_queue_row_artifacts_for_send_returns_correct_shape(db_conn: sqlite3.Connection) -> None:
    cid = _insert_card(db_conn)
    row = get_by_id(db_conn, cid)
    artifacts = row.artifacts_for_send()
    assert "deal_note" in artifacts
    assert "call_script" in artifacts
    assert artifacts["call_script"]["opening"] == "Hi"


# ─── Force-enqueue stub ────────────────────────────────────────────
def test_force_enqueue_returns_unsupported_with_needs_list() -> None:
    result = force_enqueue_not_yet_supported()
    assert not result.success
    assert result.queue_id is None
    assert "module-1-scheduler" in result.needs
    assert "module-3-enrichment" in result.needs


# ─── Concurrency: WAL allows reads during writes ───────────────────
@pytest.mark.integration  # uses actual file (not :memory:)
def test_wal_mode_allows_concurrent_read_during_write(tmp_queue_db) -> None:
    """Pragmatic check: WAL is enabled and the BUSY_TIMEOUT actually applies.
    Not a true concurrency test — just confirms the pragmas survive schema load."""
    conn1 = sqlite3.connect(tmp_queue_db)
    journal = conn1.execute("PRAGMA journal_mode").fetchone()[0]
    busy = conn1.execute("PRAGMA busy_timeout").fetchone()[0]
    assert journal.lower() == "wal"
    assert busy == 5000
    conn1.close()
