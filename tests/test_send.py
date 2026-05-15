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
