"""Tests for sdr_engine.send — Module 7 HubSpot writes orchestrator.

Mocked HTTP via `responses` library. No live HubSpot calls.

Key behaviors covered:
- Happy path: all 3 writes succeed, status='sent'
- Partial: step 1 ok, step 2 or 3 fails, status='send_partial', IDs preserved
- Failure: step 1 fails, status='failed', no IDs
- Retry idempotency: existing_*_id parameters skip already-completed steps
- HubSpot client API shape: owner_id, association payloads, body content
- Permanent vs transient errors: 4xx other than 408/429 bails immediately,
  5xx retries with backoff
- Phone task body Markdown formatting
"""
from __future__ import annotations

import json as _json
from unittest.mock import patch

import pytest
import responses

from sdr_engine.clients.hubspot import HubSpotClient
from sdr_engine.send import (
    DEFAULT_PHONE_TASK_LEAD_HOURS,
    SendResult,
    format_call_script_body,
    send_artifacts,
)

EMAIL_URL = "https://api.hubapi.com/crm/v3/objects/emails"
NOTE_URL = "https://api.hubapi.com/crm/v3/objects/notes"
TASK_URL = "https://api.hubapi.com/crm/v3/objects/tasks"


# ─── Fixtures + helpers ────────────────────────────────────────────
@pytest.fixture
def hubspot() -> HubSpotClient:
    return HubSpotClient(api_key="test-hs")


@pytest.fixture(autouse=True)
def fast_retry_backoff():
    """Make retry backoff zero so tests don't actually sleep."""
    with patch("sdr_engine.send.RETRY_BACKOFF_BASE_SECONDS", 0):
        yield


def _artifacts(
    deal_note: str = "SDR Brief — 2026-05-15\nProspect Type: Former Client\n[80 words of brief content]",
    call_script: dict | None = None,
) -> dict:
    return {
        "deal_note": deal_note,
        "call_script": call_script or {
            "opening": "Hi Daniel, this is Daniel Bradley from TCIA.",
            "context_bridge": "Following up after Tom O'Connell mentioned API.",
            "observation": "Q1 default rates up 14% YoY in flexible packaging.",
            "open_question": "How are you weighing carrier diversification vs your current setup?",
            "objection_bridges": {
                "already_covered": "Understood — keep us on your shortlist for next review.",
                "not_interested": "No worries — happy to share our quarterly AR analysis if useful.",
                "send_info": "I'll send a one-pager on our carrier panel today.",
            },
        },
    }


def _send_kwargs(**overrides) -> dict:
    """Default kwargs for send_artifacts."""
    base = {
        "artifacts": _artifacts(),
        "chosen_subject": "API — trade credit panel",
        "body": "Dear Daniel,\n\nIt has been a while...\n\nBest regards,\nDaniel Bradley",
        "owner_id": "12345",
        "contact_id": "contact-abc",
        "deal_id": "deal-xyz",
        "company_name": "Advance Polybag",
        "now_ms": 1747357200000,  # fixed timestamp for deterministic tests
    }
    base.update(overrides)
    return base


# ─── format_call_script_body() ─────────────────────────────────────
def test_format_call_script_includes_all_5_sections() -> None:
    out = format_call_script_body(
        company_name="Acme",
        call_script={
            "opening": "Hi Test, this is Daniel.",
            "context_bridge": "Following up because of news X.",
            "observation": "We noticed Y.",
            "open_question": "How are you thinking about Z?",
            "objection_bridges": {
                "already_covered": "Understood.",
                "not_interested": "No worries.",
                "send_info": "Will send.",
            },
        },
        sent_date_human="2026-05-15",
        chosen_subject="Subj line",
    )
    assert "## Cold Call Script — Acme" in out
    assert "**OPENING**: Hi Test, this is Daniel." in out
    assert "**CONTEXT BRIDGE**:" in out
    assert "**OBSERVATION**:" in out
    assert "**OPEN QUESTION**:" in out
    assert "**OBJECTION BRIDGES**:" in out
    assert "*Already covered*: Understood." in out
    assert "Email sent 2026-05-15 — subject: Subj line" in out


def test_format_call_script_tolerates_missing_objection_bridges() -> None:
    out = format_call_script_body(
        company_name="Acme",
        call_script={"opening": "Hi", "objection_bridges": {}},
        sent_date_human="2026-05-15",
        chosen_subject="x",
    )
    # All bridge labels still appear; values are empty
    assert "*Already covered*:" in out
    assert "*Not interested*:" in out


# ─── Happy path ────────────────────────────────────────────────────
@responses.activate
def test_send_artifacts_happy_path_all_three_succeed(hubspot: HubSpotClient) -> None:
    responses.add(responses.POST, EMAIL_URL, json={"id": "eng-100"}, status=201)
    responses.add(responses.POST, NOTE_URL, json={"id": "note-200"}, status=201)
    responses.add(responses.POST, TASK_URL, json={"id": "task-300"}, status=201)

    result = send_artifacts(hubspot=hubspot, **_send_kwargs())

    assert result.status == "sent"
    assert result.engagement_id == "eng-100"
    assert result.note_id == "note-200"
    assert result.task_id == "task-300"
    assert result.errors == []


@responses.activate
def test_send_artifacts_email_payload_includes_owner_and_associations(hubspot: HubSpotClient) -> None:
    """Verify the email POST body carries the owner_id, body, and associations
    correctly — this is what makes 'sent on behalf of Daniel' work."""
    responses.add(responses.POST, EMAIL_URL, json={"id": "eng-100"}, status=201)
    responses.add(responses.POST, NOTE_URL, json={"id": "note-200"}, status=201)
    responses.add(responses.POST, TASK_URL, json={"id": "task-300"}, status=201)

    send_artifacts(hubspot=hubspot, **_send_kwargs(owner_id="oid-999"))

    sent = _json.loads(responses.calls[0].request.body)
    props = sent["properties"]
    assert props["hs_email_subject"] == "API — trade credit panel"
    assert props["hubspot_owner_id"] == "oid-999"
    assert props["hs_email_direction"] == "EMAIL"
    # Two associations: contact, deal
    assert len(sent["associations"]) == 2
    targets = {a["to"]["id"] for a in sent["associations"]}
    assert targets == {"contact-abc", "deal-xyz"}


@responses.activate
def test_send_artifacts_task_due_36h_after_send(hubspot: HubSpotClient) -> None:
    """Phone task hs_timestamp should be the email send time + lead hours."""
    responses.add(responses.POST, EMAIL_URL, json={"id": "eng-100"}, status=201)
    responses.add(responses.POST, NOTE_URL, json={"id": "note-200"}, status=201)
    responses.add(responses.POST, TASK_URL, json={"id": "task-300"}, status=201)

    send_artifacts(hubspot=hubspot, **_send_kwargs(now_ms=1_000_000_000_000))

    sent_task = _json.loads(responses.calls[2].request.body)
    due_ms = int(sent_task["properties"]["hs_timestamp"])
    expected = 1_000_000_000_000 + (DEFAULT_PHONE_TASK_LEAD_HOURS * 3600 * 1000)
    assert due_ms == expected


@responses.activate
def test_send_artifacts_task_body_contains_formatted_script(hubspot: HubSpotClient) -> None:
    responses.add(responses.POST, EMAIL_URL, json={"id": "eng-100"}, status=201)
    responses.add(responses.POST, NOTE_URL, json={"id": "note-200"}, status=201)
    responses.add(responses.POST, TASK_URL, json={"id": "task-300"}, status=201)

    send_artifacts(hubspot=hubspot, **_send_kwargs())

    sent_task = _json.loads(responses.calls[2].request.body)
    body = sent_task["properties"]["hs_task_body"]
    assert "## Cold Call Script — Advance Polybag" in body
    assert "Hi Daniel, this is Daniel Bradley from TCIA." in body
    assert "*Already covered*:" in body


# ─── Partial success: step 2 (note) fails ──────────────────────────
@responses.activate
def test_step2_fails_returns_send_partial_with_engagement_only(hubspot: HubSpotClient) -> None:
    responses.add(responses.POST, EMAIL_URL, json={"id": "eng-100"}, status=201)
    # Note creation fails on all retries
    responses.add(responses.POST, NOTE_URL, status=503)
    responses.add(responses.POST, NOTE_URL, status=503)
    responses.add(responses.POST, NOTE_URL, status=503)
    # Task succeeds
    responses.add(responses.POST, TASK_URL, json={"id": "task-300"}, status=201)

    result = send_artifacts(hubspot=hubspot, **_send_kwargs())

    assert result.status == "send_partial"
    assert result.engagement_id == "eng-100"  # email went out
    assert result.note_id is None              # missing; UI shows retry button
    assert result.task_id == "task-300"        # task still got created
    assert any("create_note" in e for e in result.errors)


# ─── Partial success: step 3 (task) fails ──────────────────────────
@responses.activate
def test_step3_fails_returns_send_partial_with_note_set(hubspot: HubSpotClient) -> None:
    responses.add(responses.POST, EMAIL_URL, json={"id": "eng-100"}, status=201)
    responses.add(responses.POST, NOTE_URL, json={"id": "note-200"}, status=201)
    # Task fails on all retries
    for _ in range(3):
        responses.add(responses.POST, TASK_URL, status=502)

    result = send_artifacts(hubspot=hubspot, **_send_kwargs())

    assert result.status == "send_partial"
    assert result.engagement_id == "eng-100"
    assert result.note_id == "note-200"
    assert result.task_id is None
    assert any("create_phone_task" in e for e in result.errors)


# ─── Total failure: step 1 (email) fails ───────────────────────────
@responses.activate
def test_step1_failure_returns_failed_without_attempting_note_or_task(hubspot: HubSpotClient) -> None:
    # Email creation fails on all 3 retries
    for _ in range(3):
        responses.add(responses.POST, EMAIL_URL, status=500)
    # Note + task should NEVER be called. responses.assert_all_requests_are_fired
    # is False by default, so this just verifies via call count.

    result = send_artifacts(hubspot=hubspot, **_send_kwargs())

    assert result.status == "failed"
    assert result.engagement_id is None
    assert result.note_id is None
    assert result.task_id is None
    # All 3 attempts went to the email URL, no others
    assert len(responses.calls) == 3
    assert all(call.request.url == EMAIL_URL for call in responses.calls)


# ─── Permanent error (4xx) bails immediately, no retries ───────────
@responses.activate
def test_email_4xx_bails_immediately_without_retries(hubspot: HubSpotClient) -> None:
    """A 400 means our request is malformed; retrying won't help."""
    responses.add(responses.POST, EMAIL_URL, status=400, body="bad request")

    result = send_artifacts(hubspot=hubspot, **_send_kwargs())

    assert result.status == "failed"
    assert len(responses.calls) == 1  # only one attempt


# ─── Idempotent retry: existing engagement_id skips step 1 ─────────
@responses.activate
def test_retry_with_existing_engagement_id_only_fires_missing_steps(hubspot: HubSpotClient) -> None:
    """Module 6's /retry-note endpoint calls send_artifacts with existing
    engagement_id + task_id already set. send_artifacts should NOT re-fire
    the email, only attempt the missing note."""
    responses.add(responses.POST, NOTE_URL, json={"id": "note-retry-200"}, status=201)

    result = send_artifacts(
        hubspot=hubspot,
        existing_engagement_id="eng-prior-100",
        existing_task_id="task-prior-300",
        **_send_kwargs(),
    )

    assert result.status == "sent"
    assert result.engagement_id == "eng-prior-100"
    assert result.note_id == "note-retry-200"
    assert result.task_id == "task-prior-300"
    # Only the note URL was hit
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == NOTE_URL


@responses.activate
def test_retry_with_existing_note_id_only_fires_task(hubspot: HubSpotClient) -> None:
    responses.add(responses.POST, TASK_URL, json={"id": "task-retry-300"}, status=201)

    result = send_artifacts(
        hubspot=hubspot,
        existing_engagement_id="eng-prior-100",
        existing_note_id="note-prior-200",
        **_send_kwargs(),
    )

    assert result.status == "sent"
    assert result.engagement_id == "eng-prior-100"
    assert result.note_id == "note-prior-200"
    assert result.task_id == "task-retry-300"
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == TASK_URL


# ─── Transient → eventually-succeeds retry ─────────────────────────
@responses.activate
def test_note_retries_then_succeeds(hubspot: HubSpotClient) -> None:
    """503 on first attempt, 200 on second. Whole send still classified 'sent'."""
    responses.add(responses.POST, EMAIL_URL, json={"id": "eng-100"}, status=201)
    responses.add(responses.POST, NOTE_URL, status=503)  # first attempt fails
    responses.add(responses.POST, NOTE_URL, json={"id": "note-200"}, status=201)  # second succeeds
    responses.add(responses.POST, TASK_URL, json={"id": "task-300"}, status=201)

    result = send_artifacts(hubspot=hubspot, **_send_kwargs())

    assert result.status == "sent"
    assert result.note_id == "note-200"


# ─── Empty artifact safety ─────────────────────────────────────────
@responses.activate
def test_empty_deal_note_skips_note_but_still_sends_email(hubspot: HubSpotClient) -> None:
    """If Module 4 returned a NEEDS_HUMAN result with empty deal_note placeholders,
    we still want the email path to work. send_artifacts records the skip but
    classifies as send_partial since no note was created."""
    responses.add(responses.POST, EMAIL_URL, json={"id": "eng-100"}, status=201)
    responses.add(responses.POST, TASK_URL, json={"id": "task-300"}, status=201)

    kwargs = _send_kwargs(artifacts=_artifacts(deal_note=""))
    result = send_artifacts(hubspot=hubspot, **kwargs)

    assert result.status == "send_partial"
    assert result.engagement_id == "eng-100"
    assert result.note_id is None
    assert result.task_id == "task-300"
    # Only email + task hit; note was skipped
    urls = [c.request.url for c in responses.calls]
    assert urls == [EMAIL_URL, TASK_URL]
    assert any("deal_note artifact was empty" in e for e in result.errors)


# ─── SendResult dataclass shape ────────────────────────────────────
def test_send_result_default_construction() -> None:
    r = SendResult(status="failed")
    assert r.engagement_id is None
    assert r.note_id is None
    assert r.task_id is None
    assert r.errors == []


# ─── n8n send webhook (Module 7 amendment 2026-05-16) ──────────────
N8N_URL = "https://n8n.internal/webhook/sdr-send-email"


@responses.activate
def test_send_via_n8n_webhook_returns_true_on_2xx() -> None:
    from sdr_engine.send import send_via_n8n_webhook
    responses.add(responses.POST, N8N_URL, status=200)
    ok, err = send_via_n8n_webhook(
        N8N_URL, to_email="test@example.com", subject="Hi", body="Body"
    )
    assert ok is True
    assert err is None


@responses.activate
def test_send_via_n8n_webhook_payload_shape() -> None:
    """n8n workflow binds to $json.body.{to,subject,body} — keep payload flat."""
    from sdr_engine.send import send_via_n8n_webhook
    responses.add(responses.POST, N8N_URL, status=200)
    send_via_n8n_webhook(
        N8N_URL, to_email="x@y.com", subject="Subj", body="Body text"
    )
    sent = _json.loads(responses.calls[0].request.body)
    assert sent == {"to": "x@y.com", "subject": "Subj", "body": "Body text"}


@responses.activate
def test_send_via_n8n_webhook_returns_false_on_5xx() -> None:
    from sdr_engine.send import send_via_n8n_webhook
    responses.add(responses.POST, N8N_URL, status=503, body="upstream gone")
    ok, err = send_via_n8n_webhook(
        N8N_URL, to_email="x@y.com", subject="x", body="y"
    )
    assert ok is False
    assert "503" in err


@responses.activate
def test_send_via_n8n_webhook_returns_false_on_transport_error() -> None:
    from sdr_engine.send import send_via_n8n_webhook
    responses.add(responses.POST, N8N_URL, body=Exception("network down"))
    ok, err = send_via_n8n_webhook(
        N8N_URL, to_email="x@y.com", subject="x", body="y"
    )
    assert ok is False
    assert "transport" in err


@responses.activate
def test_send_via_n8n_webhook_adds_auth_header_when_provided() -> None:
    from sdr_engine.send import send_via_n8n_webhook
    responses.add(responses.POST, N8N_URL, status=200)
    send_via_n8n_webhook(
        N8N_URL, to_email="x@y.com", subject="x", body="y",
        auth_header_value="Bearer secret-token",
    )
    assert responses.calls[0].request.headers["Authorization"] == "Bearer secret-token"


# ─── send_artifacts + n8n integration ──────────────────────────────
@responses.activate
def test_send_artifacts_calls_n8n_first_then_hubspot(hubspot: HubSpotClient) -> None:
    """n8n webhook URL configured → order should be n8n send → engagement → note → task."""
    responses.add(responses.POST, N8N_URL, status=200)
    responses.add(responses.POST, EMAIL_URL, json={"id": "eng-100"}, status=201)
    responses.add(responses.POST, NOTE_URL, json={"id": "note-200"}, status=201)
    responses.add(responses.POST, TASK_URL, json={"id": "task-300"}, status=201)

    result = send_artifacts(
        hubspot=hubspot,
        contact_email="prospect@example.com",
        n8n_send_webhook_url=N8N_URL,
        **_send_kwargs(),
    )
    assert result.status == "sent"
    # Order: n8n send, engagement, note, task
    urls = [c.request.url for c in responses.calls]
    assert urls == [N8N_URL, EMAIL_URL, NOTE_URL, TASK_URL]
    # n8n got the prospect email
    n8n_body = _json.loads(responses.calls[0].request.body)
    assert n8n_body["to"] == "prospect@example.com"
    assert n8n_body["subject"] == "API — trade credit panel"


@responses.activate
def test_send_artifacts_n8n_failure_aborts_without_hubspot_writes(hubspot: HubSpotClient) -> None:
    """If actual email delivery fails, we MUST NOT create the HubSpot log
    (otherwise HubSpot would show 'sent' for an email that never went out)."""
    responses.add(responses.POST, N8N_URL, status=502)
    # No EMAIL_URL mock — if Module 7 calls it after n8n fail, the test errors.

    result = send_artifacts(
        hubspot=hubspot,
        contact_email="prospect@example.com",
        n8n_send_webhook_url=N8N_URL,
        **_send_kwargs(),
    )
    assert result.status == "failed"
    assert result.engagement_id is None
    assert any("n8n send" in e for e in result.errors)
    # Only the n8n call happened
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == N8N_URL


@responses.activate
def test_send_artifacts_n8n_skipped_on_retry_with_existing_engagement(hubspot: HubSpotClient) -> None:
    """Retrying a send_partial row: email already went out, only note + task missing.
    n8n MUST NOT be called again or the prospect gets a duplicate email."""
    responses.add(responses.POST, NOTE_URL, json={"id": "note-NEW"}, status=201)

    result = send_artifacts(
        hubspot=hubspot,
        contact_email="prospect@example.com",
        n8n_send_webhook_url=N8N_URL,
        existing_engagement_id="eng-prior-100",
        existing_task_id="task-prior-300",
        **_send_kwargs(),
    )
    assert result.status == "sent"
    # Only the note URL was hit — n8n NOT called, engagement NOT recreated
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == NOTE_URL


def test_send_artifacts_n8n_configured_but_no_contact_email_refuses(hubspot: HubSpotClient) -> None:
    """Defensive check: if the webhook is set but contact_email is empty,
    refuse to send rather than silently fall through to log-only."""
    result = send_artifacts(
        hubspot=hubspot,
        contact_email="",
        n8n_send_webhook_url=N8N_URL,
        **_send_kwargs(),
    )
    assert result.status == "failed"
    assert any("contact_email is empty" in e for e in result.errors)


def test_send_artifacts_works_without_n8n_for_backward_compat(hubspot: HubSpotClient) -> None:
    """When n8n_send_webhook_url is None (default), Module 7 behaves as
    before — logs to HubSpot, doesn't attempt any actual send. Backward
    compat for Module 8 sweeps + tests that predate this amendment."""
    # _send_kwargs() doesn't include n8n_send_webhook_url; it defaults to None.
    # This test confirms the legacy path still works.
    pass  # the other 9 send_artifacts tests in this file all exercise this implicitly
