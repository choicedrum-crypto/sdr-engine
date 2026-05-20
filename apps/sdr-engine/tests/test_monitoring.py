"""Tests for sdr_engine.monitoring — RunHeartbeat, webhook notifier, sweeps, digest.

Real SQLite (via tmp_queue_db fixture), mocked HTTP via `responses`.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import responses

from sdr_engine.clients.hubspot import HubSpotClient
from sdr_engine.monitoring import (
    RunHeartbeat,
    notify_openclaw,
    sweep_failed_artifacts,
    sweep_orphan_enrichments,
    sweep_partial_sends,
    weekly_digest,
)

EMAIL_URL = "https://api.hubapi.com/crm/v3/objects/emails"
NOTE_URL = "https://api.hubapi.com/crm/v3/objects/notes"
TASK_URL = "https://api.hubapi.com/crm/v3/objects/tasks"
WEBHOOK_URL = "https://openclaw.example.com/webhook/abc"


def _insert_partial(
    db: sqlite3.Connection,
    *,
    actioned_hours_ago: int = 25,
    note_id: str | None = None,
    task_id: str | None = None,
    deal_id: str = "deal-1",
) -> int:
    """Insert a send_partial row with an actioned_at in the past."""
    actioned_at = (datetime.now(UTC) - timedelta(hours=actioned_hours_ago)).isoformat(
        timespec="seconds"
    )
    cs = json.dumps({
        "opening": "Hi", "context_bridge": "x", "observation": "y",
        "open_question": "z", "objection_bridges": {
            "already_covered": "a", "not_interested": "b", "send_info": "c"
        },
    })
    cursor = db.execute(
        "INSERT INTO queue ("
        "deal_id, company_id, company_name, contact_id, contact_email, "
        "send_reason, prospect_type, draft_subject, draft_body, "
        "deal_note_body, call_script_json, status, actioned_at, "
        "hubspot_engagement_id, hubspot_note_id, hubspot_task_id) "
        "VALUES (?, 'co-1', 'Acme', 'ct-1', 'x@y.com', 'round-robin', 'former_client', "
        "'Test subject', 'body text', 'note text', ?, 'send_partial', ?, "
        "'eng-existing', ?, ?)",
        (deal_id, cs, actioned_at, note_id, task_id),
    )
    return cursor.lastrowid


def _insert_orphan(db: sqlite3.Connection, company_id: str = "co-orphan-1") -> None:
    db.execute(
        "INSERT INTO enrichment_cache (company_id, contact_id, status, confidence, "
        "matched_title, enriched_at, updated_at) VALUES (?, ?, 'orphan', 0.9, 'CFO', "
        "datetime('now', '-2 days'), datetime('now', '-2 days'))",
        (company_id, "dangling-contact-id"),
    )


# ─── RunHeartbeat ──────────────────────────────────────────────────
def test_heartbeat_writes_running_on_enter(db_conn: sqlite3.Connection) -> None:
    with RunHeartbeat(db_conn) as run:
        # While inside the context, the row should exist with status='running'
        row = db_conn.execute(
            "SELECT status FROM runs WHERE run_id = ?", (run.run_id,)
        ).fetchone()
        assert row[0] == "running"


def test_heartbeat_finalizes_to_success_on_clean_exit(db_conn: sqlite3.Connection) -> None:
    with RunHeartbeat(db_conn) as run:
        run.enqueued_count = 5
        run.dropped_count = 2
    row = db_conn.execute(
        "SELECT status, enqueued_count, dropped_count, ended_at FROM runs WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()
    assert row[0] == "success"
    assert row[1] == 5
    assert row[2] == 2
    assert row[3] is not None


def test_heartbeat_finalizes_to_partial_when_errors_recorded(db_conn: sqlite3.Connection) -> None:
    with RunHeartbeat(db_conn) as run:
        run.enqueued_count = 3
        run.record_error("module-1", "deal-42 had no contact", deal_id="42")
    row = db_conn.execute(
        "SELECT status, errors_jsonl FROM runs WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    assert row[0] == "partial"
    assert "deal-42" in row[1]


def test_heartbeat_finalizes_to_failed_on_exception(db_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="boom"):
        with RunHeartbeat(db_conn) as run:
            run.enqueued_count = 1
            raise ValueError("boom")
    row = db_conn.execute(
        "SELECT status, errors_jsonl FROM runs WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    assert row[0] == "failed"
    assert "boom" in row[1]


# ─── notify_openclaw ───────────────────────────────────────────────
@responses.activate
def test_notify_openclaw_returns_true_on_2xx() -> None:
    responses.add(responses.POST, WEBHOOK_URL, status=200)
    assert notify_openclaw(WEBHOOK_URL, "test_event", {"x": 1}) is True


@responses.activate
def test_notify_openclaw_returns_false_on_5xx() -> None:
    responses.add(responses.POST, WEBHOOK_URL, status=500)
    assert notify_openclaw(WEBHOOK_URL, "test_event", {"x": 1}) is False


@responses.activate
def test_notify_openclaw_returns_false_on_timeout() -> None:
    responses.add(responses.POST, WEBHOOK_URL, body=Exception("timeout"))
    assert notify_openclaw(WEBHOOK_URL, "test_event", {"x": 1}) is False


def test_notify_openclaw_no_op_on_empty_url() -> None:
    """No webhook configured → silently return False, no HTTP call."""
    assert notify_openclaw(None, "test_event", {"x": 1}) is False
    assert notify_openclaw("", "test_event", {"x": 1}) is False


@responses.activate
def test_notify_openclaw_sends_event_type_and_payload() -> None:
    responses.add(responses.POST, WEBHOOK_URL, status=200)
    notify_openclaw(WEBHOOK_URL, "partial_send_still_failing", {"queue_id": 42, "errors": ["x"]})
    sent = json.loads(responses.calls[0].request.body)
    assert sent["event_type"] == "partial_send_still_failing"
    assert sent["payload"]["queue_id"] == 42
    assert "timestamp" in sent


# ─── sweep_partial_sends ───────────────────────────────────────────
@responses.activate
def test_sweep_partial_heals_row_by_retrying_missing_note(db_conn: sqlite3.Connection) -> None:
    qid = _insert_partial(db_conn, note_id=None, task_id="task-existing")
    responses.add(responses.POST, NOTE_URL, json={"id": "note-NEW"}, status=201)

    hubspot = HubSpotClient(api_key="test")
    result = sweep_partial_sends(db_conn, hubspot, owner_id="owner-1")

    assert result.checked == 1
    assert result.healed == 1
    assert result.still_failing == 0

    row = db_conn.execute(
        "SELECT status, hubspot_note_id FROM queue WHERE id = ?", (qid,)
    ).fetchone()
    assert row[0] == "sent"  # promoted
    assert row[1] == "note-NEW"


@responses.activate
def test_sweep_partial_skips_rows_younger_than_cutoff(db_conn: sqlite3.Connection) -> None:
    """A row that became partial 1 hour ago shouldn't be swept yet (default 24h)."""
    qid = _insert_partial(db_conn, actioned_hours_ago=1, note_id=None, task_id="task-1")
    # No responses.add — if sweep tried to call HubSpot it would error.

    result = sweep_partial_sends(db_conn, HubSpotClient(api_key="test"), owner_id="owner-1")
    assert result.checked == 0
    row = db_conn.execute("SELECT status FROM queue WHERE id = ?", (qid,)).fetchone()
    assert row[0] == "send_partial"


@responses.activate
def test_sweep_partial_records_still_failing_when_retry_fails(db_conn: sqlite3.Connection) -> None:
    _insert_partial(db_conn, note_id=None, task_id="task-1")
    # Note retry fails on all 3 attempts inside send_artifacts
    for _ in range(3):
        responses.add(responses.POST, NOTE_URL, status=503)

    hubspot = HubSpotClient(api_key="test")
    result = sweep_partial_sends(db_conn, hubspot, owner_id="owner-1")

    assert result.checked == 1
    assert result.healed == 0
    assert result.still_failing == 1


@responses.activate
def test_sweep_partial_alerts_on_still_failing(db_conn: sqlite3.Connection) -> None:
    _insert_partial(db_conn, note_id=None, task_id="task-1", deal_id="deal-X")
    for _ in range(3):
        responses.add(responses.POST, NOTE_URL, status=500)
    responses.add(responses.POST, WEBHOOK_URL, status=200)

    sweep_partial_sends(
        db_conn, HubSpotClient(api_key="test"), owner_id="owner-1", webhook_url=WEBHOOK_URL,
    )
    # The webhook call is the 4th call (after 3 note retries)
    webhook_call = next(c for c in responses.calls if c.request.url == WEBHOOK_URL)
    sent = json.loads(webhook_call.request.body)
    assert sent["event_type"] == "partial_send_still_failing"
    assert sent["payload"]["deal_id"] == "deal-X"


@responses.activate
def test_sweep_partial_respects_max_per_run(db_conn: sqlite3.Connection) -> None:
    """When 5 partial rows exist but max_per_run=2, only 2 should be touched."""
    for i in range(5):
        _insert_partial(db_conn, note_id=None, task_id="task-1", deal_id=f"d-{i}")
    # Mock enough responses for max_per_run × the worst case (3 attempts each)
    for _ in range(2):
        responses.add(responses.POST, NOTE_URL, json={"id": "n-x"}, status=201)

    result = sweep_partial_sends(
        db_conn, HubSpotClient(api_key="test"), owner_id="owner-1", max_per_run=2,
    )
    assert result.checked == 2  # capped


# ─── sweep_orphan_enrichments ──────────────────────────────────────
@responses.activate
def test_sweep_orphan_fires_alert_per_orphan_row(db_conn: sqlite3.Connection) -> None:
    _insert_orphan(db_conn, company_id="co-orph-1")
    _insert_orphan(db_conn, company_id="co-orph-2")
    responses.add(responses.POST, WEBHOOK_URL, status=200)
    responses.add(responses.POST, WEBHOOK_URL, status=200)

    result = sweep_orphan_enrichments(db_conn, webhook_url=WEBHOOK_URL)

    assert result.checked == 2
    assert result.alerted == 2
    assert len(responses.calls) == 2


def test_sweep_orphan_no_op_when_no_orphans(db_conn: sqlite3.Connection) -> None:
    """Most days there should be zero orphans. Don't fire spurious alerts."""
    result = sweep_orphan_enrichments(db_conn, webhook_url=WEBHOOK_URL)
    assert result.checked == 0
    assert result.alerted == 0


# ─── sweep_failed_artifacts ────────────────────────────────────────
@responses.activate
def test_sweep_failed_artifacts_retries_sent_rows_with_null_note(db_conn: sqlite3.Connection) -> None:
    """A 'sent' row with NULL note id (e.g., legacy migration data) should
    be re-attempted and promoted properly."""
    actioned_at = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    cs = json.dumps({"opening": "Hi", "context_bridge": "x", "observation": "y",
                     "open_question": "z", "objection_bridges": {
                         "already_covered": "a", "not_interested": "b", "send_info": "c"
                     }})
    cursor = db_conn.execute(
        "INSERT INTO queue (deal_id, company_id, company_name, contact_id, contact_email, "
        "send_reason, prospect_type, draft_subject, draft_body, deal_note_body, "
        "call_script_json, status, actioned_at, hubspot_engagement_id, hubspot_note_id, "
        "hubspot_task_id) VALUES (?, 'co-1', 'Acme', 'ct-1', 'x@y.com', 'round-robin', "
        "'former_client', 'Subject', 'body', 'note text', ?, 'sent', ?, 'eng-1', NULL, 'task-1')",
        ("deal-Y", cs, actioned_at),
    )
    qid = cursor.lastrowid

    responses.add(responses.POST, NOTE_URL, json={"id": "note-LATER"}, status=201)

    result = sweep_failed_artifacts(db_conn, HubSpotClient(api_key="test"), owner_id="owner-1")
    assert result.checked == 1
    assert result.healed == 1

    row = db_conn.execute(
        "SELECT hubspot_note_id FROM queue WHERE id = ?", (qid,)
    ).fetchone()
    assert row[0] == "note-LATER"


# ─── weekly_digest ─────────────────────────────────────────────────
def test_weekly_digest_empty_db_returns_zeros(db_conn: sqlite3.Connection) -> None:
    report = weekly_digest(db_conn)
    assert report["sent"] == 0
    assert report["skipped"] == 0
    assert report["skip_rate"] == 0.0
    assert report["edit_ratio"] == 0.0


def test_weekly_digest_counts_sent_and_skipped(db_conn: sqlite3.Connection) -> None:
    """Insert 3 sent, 1 skipped — verify counts + skip_rate."""
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    cs = json.dumps({"opening": "x"})
    for status in ("sent", "sent", "edit-sent", "skipped"):
        db_conn.execute(
            "INSERT INTO queue (deal_id, company_id, company_name, contact_id, "
            "contact_email, send_reason, prospect_type, draft_subject, draft_body, "
            "deal_note_body, call_script_json, status, actioned_at) "
            "VALUES (?, 'co', 'A', 'ct', 'x@y.com', 'round-robin', 'former_client', "
            "'s', 'b', 'n', ?, ?, ?)",
            (f"deal-{status}", cs, status, recent),
        )

    report = weekly_digest(db_conn)
    assert report["sent"] == 3  # sent + edit-sent count as sent
    assert report["edit_sent"] == 1
    assert report["skipped"] == 1
    assert report["skip_rate"] == round(1 / 4, 3)
    assert report["edit_ratio"] == round(1 / 3, 3)


def test_weekly_digest_window_excludes_old_rows(db_conn: sqlite3.Connection) -> None:
    """A row actioned 8 days ago shouldn't appear in the 7-day window."""
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    cs = json.dumps({"opening": "x"})
    db_conn.execute(
        "INSERT INTO queue (deal_id, company_id, company_name, contact_id, "
        "contact_email, send_reason, prospect_type, draft_subject, draft_body, "
        "deal_note_body, call_script_json, status, actioned_at) "
        "VALUES ('old-deal', 'co', 'A', 'ct', 'x@y.com', 'round-robin', 'former_client', "
        "'s', 'b', 'n', ?, 'sent', ?)",
        (cs, old),
    )

    report = weekly_digest(db_conn)
    assert report["sent"] == 0


def test_weekly_digest_per_prospect_type_breakdown(db_conn: sqlite3.Connection) -> None:
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    cs = json.dumps({"opening": "x"})
    types = ["former_client", "former_client", "bor_target", "coi_referral"]
    for i, ptype in enumerate(types):
        db_conn.execute(
            "INSERT INTO queue (deal_id, company_id, company_name, contact_id, "
            "contact_email, send_reason, prospect_type, draft_subject, draft_body, "
            "deal_note_body, call_script_json, status, actioned_at) "
            "VALUES (?, 'co', 'A', 'ct', 'x@y.com', 'round-robin', ?, 's', 'b', 'n', ?, 'sent', ?)",
            (f"deal-{i}", ptype, cs, recent),
        )

    report = weekly_digest(db_conn)
    assert report["per_prospect_type"]["former_client"] == 2
    assert report["per_prospect_type"]["bor_target"] == 1
    assert report["per_prospect_type"]["coi_referral"] == 1


def test_weekly_digest_includes_orphan_and_partial_counts(db_conn: sqlite3.Connection) -> None:
    _insert_orphan(db_conn, company_id="co-x")
    _insert_partial(db_conn, note_id=None, task_id=None, deal_id="d-pending")

    report = weekly_digest(db_conn)
    assert report["orphan_enrichment_count"] == 1
    assert report["send_partial_count"] == 1


def test_weekly_digest_enrichment_success_rate(db_conn: sqlite3.Connection) -> None:
    recent = (datetime.now(UTC) - timedelta(days=5)).isoformat(timespec="seconds")
    # 2 success, 1 no_match, 1 error → 50% success rate
    for company_id, status in [
        ("co-1", "success"), ("co-2", "success"), ("co-3", "no_match"), ("co-4", "error")
    ]:
        db_conn.execute(
            "INSERT INTO enrichment_cache (company_id, status, enriched_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (company_id, status, recent, recent),
        )
    report = weekly_digest(db_conn)
    assert report["enrichment_success_rate_30d"] == 0.5
