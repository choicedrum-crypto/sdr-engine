"""Flask test-client tests for the Module 6 routes.

Uses the create_app() factory pointing at a tmp SQLite db. HubSpot calls
are mocked via the `responses` library. No live HTTP, no real LLM.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
import responses

# ui/server.py imports flask + dotenv; make it importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
from server import create_app  # noqa: E402

EMAIL_URL = "https://api.hubapi.com/crm/v3/objects/emails"
NOTE_URL = "https://api.hubapi.com/crm/v3/objects/notes"
TASK_URL = "https://api.hubapi.com/crm/v3/objects/tasks"


@pytest.fixture
def app(tmp_queue_db):
    """Flask app pointing at the per-test SQLite db.

    HubSpot owner id + api key are set to non-empty so probes don't short-
    circuit on 'unconfigured'.
    """
    return create_app(
        sqlite_path=tmp_queue_db,
        hubspot_api_key="test-hs-key",
        hubspot_owner_id="owner-99",
    )


@pytest.fixture
def client(app):
    return app.test_client()


def _insert_pending_card(
    db_path,
    *,
    deal_id: str = "deal-1",
    status: str = "pending",
    body: str = "Dear Daniel,\n\nbody text " + ("word " * 145) + "Best regards,\nDaniel Bradley",
    engagement_id: str | None = None,
    note_id: str | None = None,
    task_id: str | None = None,
) -> int:
    """Insert a queue row directly via SQLite (Module 1 will do this in production)."""
    conn = sqlite3.connect(db_path)
    cs = {
        "opening": "Hi Daniel, Daniel Bradley from TCIA.",
        "context_bridge": "Following up on x.",
        "observation": "Saw y.",
        "open_question": "How are you thinking about z?",
        "objection_bridges": {
            "already_covered": "a",
            "not_interested": "b",
            "send_info": "c",
        },
    }
    cursor = conn.execute(
        "INSERT INTO queue ("
        "deal_id, company_id, company_name, contact_id, contact_email, "
        "send_reason, prospect_type, draft_subject, draft_body, "
        "deal_note_body, call_script_json, status, "
        "hubspot_engagement_id, hubspot_note_id, hubspot_task_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            deal_id, "co-1", "Acme Corp", "ct-1", "test@acme.com",
            "round-robin", "former_client", "Test subject", body,
            "SDR Brief — testing", json.dumps(cs), status,
            engagement_id, note_id, task_id,
        ),
    )
    qid = cursor.lastrowid
    conn.commit()
    conn.close()
    return qid


# ─── GET /queue: empty / caught_up ─────────────────────────────────
def test_queue_empty_renders_caught_up(client) -> None:
    resp = client.get("/queue")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "All caught up" in body


def test_queue_warming_state_when_scheduler_running(client, tmp_queue_db) -> None:
    conn = sqlite3.connect(tmp_queue_db)
    conn.execute(
        "INSERT INTO runs (run_id, status, enqueued_count) VALUES ('warming-1', 'running', 0)"
    )
    conn.commit()
    conn.close()
    resp = client.get("/queue")
    body = resp.get_data(as_text=True)
    assert "Drafting today's emails" in body


# ─── GET /queue: card render ───────────────────────────────────────
def test_queue_renders_pending_card(client, tmp_queue_db) -> None:
    _insert_pending_card(tmp_queue_db)
    resp = client.get("/queue")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Acme Corp" in body
    assert "Test subject" in body
    assert "FORMER CLIENT" in body  # provenance strip
    assert "Send" in body
    assert "Skip" in body


def test_queue_renders_send_partial_with_retry_buttons(client, tmp_queue_db) -> None:
    _insert_pending_card(
        tmp_queue_db,
        status="send_partial",
        engagement_id="eng-1",
        note_id=None,
        task_id=None,
    )
    resp = client.get("/queue")
    body = resp.get_data(as_text=True)
    assert "Partial send" in body
    assert "Retry note" in body
    assert "Retry task" in body


def test_queue_renders_llm_failure_banner(client, tmp_queue_db) -> None:
    _insert_pending_card(
        tmp_queue_db,
        body="[LLM format failure — please hand-write]",
    )
    resp = client.get("/queue")
    body = resp.get_data(as_text=True)
    assert "AUTO-DRAFT FAILED" in body


# ─── POST /send: happy path ────────────────────────────────────────
@responses.activate
def test_send_full_success_marks_row_sent(client, tmp_queue_db) -> None:
    qid = _insert_pending_card(tmp_queue_db)
    responses.add(responses.POST, EMAIL_URL, json={"id": "eng-100"}, status=201)
    responses.add(responses.POST, NOTE_URL, json={"id": "note-200"}, status=201)
    responses.add(responses.POST, TASK_URL, json={"id": "task-300"}, status=201)

    resp = client.post(
        f"/queue/{qid}/send",
        data={"subject": "Test subject", "body": "Dear Daniel,\n\nbody text " + ("word " * 145) + "Best regards,\nDaniel Bradley"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "sent"
    assert body["engagement_id"] == "eng-100"

    # Verify row is sent in DB
    conn = sqlite3.connect(tmp_queue_db)
    row = conn.execute(
        "SELECT status, hubspot_engagement_id, hubspot_note_id, hubspot_task_id, edit_diff "
        "FROM queue WHERE id = ?", (qid,),
    ).fetchone()
    conn.close()
    assert row[0] == "sent"  # no edit diff → 'sent' not 'edit-sent'
    assert row[1] == "eng-100"
    assert row[4] is None


@responses.activate
def test_send_with_edited_body_marks_row_edit_sent(client, tmp_queue_db) -> None:
    qid = _insert_pending_card(tmp_queue_db)
    responses.add(responses.POST, EMAIL_URL, json={"id": "eng-100"}, status=201)
    responses.add(responses.POST, NOTE_URL, json={"id": "note-200"}, status=201)
    responses.add(responses.POST, TASK_URL, json={"id": "task-300"}, status=201)

    edited = "I rewrote the whole thing entirely."
    resp = client.post(
        f"/queue/{qid}/send",
        data={"subject": "Test subject", "body": edited},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200

    conn = sqlite3.connect(tmp_queue_db)
    row = conn.execute(
        "SELECT status, edit_diff FROM queue WHERE id = ?", (qid,)
    ).fetchone()
    conn.close()
    assert row[0] == "edit-sent"
    diff = json.loads(row[1])
    assert diff["after"] == edited
    assert "Dear Daniel" in diff["before"]


# ─── POST /send: partial success ───────────────────────────────────
@responses.activate
def test_send_partial_note_failure_marks_send_partial(client, tmp_queue_db) -> None:
    qid = _insert_pending_card(tmp_queue_db)
    responses.add(responses.POST, EMAIL_URL, json={"id": "eng-100"}, status=201)
    # Note fails all retries
    responses.add(responses.POST, NOTE_URL, status=500)
    responses.add(responses.POST, NOTE_URL, status=500)
    responses.add(responses.POST, NOTE_URL, status=500)
    responses.add(responses.POST, TASK_URL, json={"id": "task-300"}, status=201)

    resp = client.post(
        f"/queue/{qid}/send",
        data={"subject": "x", "body": "Dear Daniel,\n\n" + ("word " * 145) + " end\n\nBest regards,\nDaniel Bradley"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "send_partial"

    conn = sqlite3.connect(tmp_queue_db)
    row = conn.execute(
        "SELECT status, hubspot_note_id, hubspot_task_id FROM queue WHERE id = ?", (qid,)
    ).fetchone()
    conn.close()
    assert row[0] == "send_partial"
    assert row[1] is None
    assert row[2] == "task-300"


# ─── POST /send: email failure ─────────────────────────────────────
@responses.activate
def test_send_email_failure_keeps_row_pending(client, tmp_queue_db) -> None:
    qid = _insert_pending_card(tmp_queue_db)
    for _ in range(3):
        responses.add(responses.POST, EMAIL_URL, status=503)

    resp = client.post(
        f"/queue/{qid}/send",
        data={"subject": "x", "body": "y"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 502  # explicit "send failed" code
    body = resp.get_json()
    assert body["status"] == "failed"
    assert body["errors"]

    conn = sqlite3.connect(tmp_queue_db)
    status = conn.execute("SELECT status FROM queue WHERE id = ?", (qid,)).fetchone()[0]
    conn.close()
    assert status == "pending"  # nothing changed


# ─── POST /send: error cases ───────────────────────────────────────
def test_send_404_for_missing_row(client) -> None:
    resp = client.post("/queue/99999/send", data={"subject": "x", "body": "y"})
    assert resp.status_code == 404


def test_send_409_for_already_sent_row(client, tmp_queue_db) -> None:
    qid = _insert_pending_card(tmp_queue_db, status="sent")
    resp = client.post(f"/queue/{qid}/send", data={"subject": "x", "body": "y"})
    assert resp.status_code == 409


# ─── POST /skip ────────────────────────────────────────────────────
def test_skip_marks_row_skipped(client, tmp_queue_db) -> None:
    qid = _insert_pending_card(tmp_queue_db)
    resp = client.post(f"/queue/{qid}/skip", headers={"Accept": "application/json"})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "skipped"

    conn = sqlite3.connect(tmp_queue_db)
    row = conn.execute("SELECT status, actioned_at FROM queue WHERE id = ?", (qid,)).fetchone()
    conn.close()
    assert row[0] == "skipped"
    assert row[1] is not None


def test_skip_404_for_missing_row(client) -> None:
    resp = client.post("/queue/99999/skip")
    assert resp.status_code == 404


# ─── POST /retry-note ──────────────────────────────────────────────
@responses.activate
def test_retry_note_fills_missing_note_and_promotes_to_sent(client, tmp_queue_db) -> None:
    qid = _insert_pending_card(
        tmp_queue_db,
        status="send_partial",
        engagement_id="eng-1",
        note_id=None,
        task_id="task-1",
    )
    responses.add(responses.POST, NOTE_URL, json={"id": "note-NEW"}, status=201)

    resp = client.post(f"/queue/{qid}/retry-note", headers={"Accept": "application/json"})
    assert resp.status_code == 200

    conn = sqlite3.connect(tmp_queue_db)
    row = conn.execute(
        "SELECT status, hubspot_note_id FROM queue WHERE id = ?", (qid,)
    ).fetchone()
    conn.close()
    assert row[0] == "sent"  # promoted from send_partial since all 3 IDs now present
    assert row[1] == "note-NEW"


def test_retry_note_409_if_note_already_exists(client, tmp_queue_db) -> None:
    qid = _insert_pending_card(
        tmp_queue_db,
        status="send_partial",
        engagement_id="eng-1",
        note_id="existing-note",
        task_id=None,
    )
    resp = client.post(f"/queue/{qid}/retry-note")
    assert resp.status_code == 409


def test_retry_note_409_for_non_send_partial_row(client, tmp_queue_db) -> None:
    qid = _insert_pending_card(tmp_queue_db, status="pending")
    resp = client.post(f"/queue/{qid}/retry-note")
    assert resp.status_code == 409


# ─── POST /retry-task ──────────────────────────────────────────────
@responses.activate
def test_retry_task_fills_missing_task(client, tmp_queue_db) -> None:
    qid = _insert_pending_card(
        tmp_queue_db,
        status="send_partial",
        engagement_id="eng-1",
        note_id="note-1",
        task_id=None,
    )
    responses.add(responses.POST, TASK_URL, json={"id": "task-NEW"}, status=201)

    resp = client.post(f"/queue/{qid}/retry-task", headers={"Accept": "application/json"})
    assert resp.status_code == 200

    conn = sqlite3.connect(tmp_queue_db)
    row = conn.execute(
        "SELECT status, hubspot_task_id FROM queue WHERE id = ?", (qid,)
    ).fetchone()
    conn.close()
    assert row[0] == "sent"
    assert row[1] == "task-NEW"


# ─── POST /force: explicit 501 ─────────────────────────────────────
def test_force_returns_501_with_explanation(client) -> None:
    resp = client.post("/queue/force", json={"deal_id": "deal-1"})
    assert resp.status_code == 501
    body = resp.get_json()
    assert "module-1-scheduler" in body["needs_modules"]
    assert "module-3-enrichment" in body["needs_modules"]


# ─── GET /health ───────────────────────────────────────────────────
def test_health_returns_json_with_status_keys(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "llm" in body
    assert "hubspot" in body
    assert "sqlite" in body
    assert "overall" in body
    # sqlite should be 'ok' (tmp file exists with schema)
    assert body["sqlite"] == "ok"
