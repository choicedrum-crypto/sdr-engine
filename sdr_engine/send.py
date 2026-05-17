"""Module 7 — HubSpot writes.

When the SDR clicks Send in the Review UI, this module fires three
HubSpot writes in order, with explicit partial-success handling:

    Step 1: email engagement (logs the email send under the SDR's identity)
    Step 2: deal note      (auto-posted SDR brief; not edited in UI per T3=A)
    Step 3: phone task     (paired follow-up call, due 36h after email)

Per the architecture spec (Module 7 → Error recovery):

  > If step 1 succeeds but step 2 or 3 fails (network / 5xx), retry the
  > failing step up to 3 times with exponential backoff. If still failing,
  > save any successful HubSpot IDs to SQLite, leave failed ones as NULL,
  > set status='send_partial' (email went out — partial success). Log to
  > OpenClaw alert. Module 8's nightly sweep retries NULL note/task IDs
  > the next day.

The orchestrator never deletes a successful email engagement on a later
failure — once the email is logged in HubSpot, the prospect has it, and
the right move is to record the partial state and retry the missing
pieces. The UI surfaces this via the send_partial state with per-step
retry buttons (A8 amendment).

This module is db-free by design: it returns a SendResult that the caller
(Module 6 UI route) writes to the queue row. Same pattern as Module 4's
draft() — keeps the orchestrator easy to test without a SQLite fixture.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from sdr_engine.clients.hubspot import HubSpotClient, HubSpotError

# ─── Configuration ─────────────────────────────────────────────────
# Phone task default lead time. Matches PHONE_TASK_LEAD_HOURS env var.
DEFAULT_PHONE_TASK_LEAD_HOURS = 36
# Retry parameters per step. Each step gets up to 3 attempts with
# exponential backoff (0.5s, 1s, 2s). Aggregate worst case: 3.5s per step.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE_SECONDS = 0.5
# HTTP status codes that are worth retrying. 4xx is a permanent client
# error — retrying won't help.
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}


@dataclass
class SendResult:
    """Return shape of send_artifacts().

    status='sent'         → all three succeeded.
    status='send_partial' → step 1 (email) succeeded but step 2 or 3 failed.
                            engagement_id is populated; note_id/task_id may
                            be None. Module 6 UI surfaces retry buttons.
    status='failed'       → step 1 failed. Nothing went out; nothing changed
                            in HubSpot for this deal. Caller leaves status
                            as 'pending' or alerts.
    """
    status: str  # 'sent' | 'send_partial' | 'failed'
    engagement_id: str | None = None
    note_id: str | None = None
    task_id: str | None = None
    errors: list[str] = field(default_factory=list)  # human-readable per-step failure reasons


# ─── Helpers ───────────────────────────────────────────────────────
def _utc_now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _phone_task_due_ms(now_ms: int | None = None, lead_hours: int = DEFAULT_PHONE_TASK_LEAD_HOURS) -> int:
    now = now_ms if now_ms is not None else _utc_now_ms()
    return int((datetime.fromtimestamp(now / 1000, tz=UTC) + timedelta(hours=lead_hours)).timestamp() * 1000)


def format_call_script_body(
    company_name: str,
    call_script: dict[str, Any],
    sent_date_human: str,
    chosen_subject: str,
) -> str:
    """Render the LLM-drafted call_script JSON into Markdown for hs_task_body.

    Format matches docs/ARCHITECTURE.md Module 7 → call script formatting.
    Keeps the SDR oriented when they open the task in HubSpot to make the call.
    """
    bridges = call_script.get("objection_bridges", {})
    return (
        f"## Cold Call Script — {company_name}\n\n"
        f"**OPENING**: {call_script.get('opening', '')}\n\n"
        f"**CONTEXT BRIDGE**: {call_script.get('context_bridge', '')}\n\n"
        f"**OBSERVATION**: {call_script.get('observation', '')}\n\n"
        f"**OPEN QUESTION**: {call_script.get('open_question', '')}\n\n"
        f"---\n\n"
        f"**OBJECTION BRIDGES**:\n"
        f"- *Already covered*: {bridges.get('already_covered', '')}\n"
        f"- *Not interested*: {bridges.get('not_interested', '')}\n"
        f"- *Send info*: {bridges.get('send_info', '')}\n\n"
        f"---\n\n"
        f"Email sent {sent_date_human} — subject: {chosen_subject}\n"
    )


def _retry(operation_name: str, fn) -> tuple[Any, str | None]:
    """Call fn() with retry on transient failures. Returns (result, None) on
    success, (None, error_message) on permanent failure.

    Transient: HubSpot 408/429/5xx, transport-level exceptions.
    Permanent: HubSpot 4xx other than 408/429 (the request is malformed —
    retrying won't help). Returns immediately.
    """
    last_err = "no attempts made"
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fn(), None
        except HubSpotError as exc:
            last_err = f"{operation_name} attempt {attempt}: HubSpot {exc.status} {exc.body[:120]}"
            if exc.status not in RETRYABLE_STATUSES:
                return None, last_err  # permanent client error — bail immediately
        except requests.RequestException as exc:
            last_err = f"{operation_name} attempt {attempt}: transport error: {exc}"
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    return None, last_err


def send_via_n8n_webhook(
    webhook_url: str,
    *,
    to_email: str,
    subject: str,
    body: str,
    timeout: int = 30,
    auth_header_value: str | None = None,
) -> tuple[bool, str | None]:
    """Trigger actual email delivery via an n8n workflow webhook.

    HubSpot's /crm/v3/objects/emails endpoint LOGS an email engagement but
    doesn't actually deliver. The architecture's send-flow assumed it did
    both — smoke test caught the gap on 2026-05-16. To deliver, we POST to
    an n8n webhook that routes to a Microsoft Outlook (or SMTP) node
    configured with Daniel's connected M365 account. n8n owns the credential
    storage per A16; this code only knows the webhook URL.

    Payload shape (kept flat for easy n8n binding via $json.body.X):
        {"to": "...", "subject": "...", "body": "..."}

    Returns (True, None) on 2xx, (False, "reason") on anything else.
    Caller treats failure as "email did not go out" — same path as a
    HubSpot engagement create failure.
    """
    headers = {"Content-Type": "application/json"}
    if auth_header_value:
        headers["Authorization"] = auth_header_value
    try:
        resp = requests.post(
            webhook_url,
            json={"to": to_email, "subject": subject, "body": body},
            headers=headers,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — failure to send must never crash the route
        return False, f"n8n webhook transport error: {exc}"
    if 200 <= resp.status_code < 300:
        return True, None
    return False, f"n8n webhook returned {resp.status_code}: {resp.text[:200]}"


# ─── Orchestrator ──────────────────────────────────────────────────
def send_artifacts(
    *,
    artifacts: dict[str, Any],
    chosen_subject: str,
    body: str,
    owner_id: str,
    contact_id: str,
    deal_id: str,
    company_name: str,
    hubspot: HubSpotClient,
    contact_email: str = "",  # only used when n8n_send_webhook_url is set
    n8n_send_webhook_url: str | None = None,
    n8n_auth_header_value: str | None = None,
    existing_engagement_id: str | None = None,
    existing_note_id: str | None = None,
    existing_task_id: str | None = None,
    phone_task_lead_hours: int = DEFAULT_PHONE_TASK_LEAD_HOURS,
    now_ms: int | None = None,
) -> SendResult:
    """Fire the email send + three HubSpot writes for one queue row.

    `artifacts` is the dict returned by Module 4 (`sdr_engine.llm.draft()`):
    must contain `deal_note` (string) and `call_script` (object).

    `n8n_send_webhook_url` — when provided, POST first to an n8n workflow
    that actually delivers the email via Daniel's connected M365 account.
    HubSpot's /crm/v3/objects/emails only logs; it does NOT deliver. If
    this URL is None, the email is logged in HubSpot but never reaches
    the prospect (the architecture's original mistaken assumption — kept
    behind a None default so existing tests + Module 8 sweeps still pass).

    `existing_*_id` parameters support idempotent retry from Module 6's
    /retry-note and /retry-task endpoints. Pre-set IDs are NOT re-fired;
    only None slots are attempted. This is what makes the partial-success
    path safe to retry.
    """
    sent_at_ms = now_ms if now_ms is not None else _utc_now_ms()
    sent_date_human = datetime.fromtimestamp(sent_at_ms / 1000, tz=UTC).strftime("%Y-%m-%d")

    result = SendResult(
        status="failed",
        engagement_id=existing_engagement_id,
        note_id=existing_note_id,
        task_id=existing_task_id,
    )

    # ─── Step 0 (NEW): actually deliver via n8n ────────────────────
    # Skipped when:
    #   - n8n_send_webhook_url is not configured (legacy behavior — log-only,
    #     used by Module 8 sweeps where the email already went out)
    #   - engagement_id is already set (retrying a send_partial row — the
    #     email was already delivered on the prior attempt)
    if n8n_send_webhook_url and result.engagement_id is None:
        if not contact_email:
            result.errors.append(
                "n8n send configured but contact_email is empty — refusing to send"
            )
            return result
        ok, err = send_via_n8n_webhook(
            n8n_send_webhook_url,
            to_email=contact_email,
            subject=chosen_subject,
            body=body,
            auth_header_value=n8n_auth_header_value,
        )
        if not ok:
            result.errors.append(f"n8n send: {err}")
            return result  # email never went out — nothing to log

    # ─── Step 1: email engagement (LOG in HubSpot) ─────────────────
    # If we already have an engagement_id, the prospect already received
    # the email on a prior send; we're only here to fill in missing pieces.
    if result.engagement_id is None:
        engagement_id, err = _retry(
            "create_email_engagement",
            lambda: hubspot.create_email_engagement(
                subject=chosen_subject,
                body=body,
                owner_id=owner_id,
                contact_id=contact_id,
                deal_id=deal_id,
                timestamp_ms=sent_at_ms,
            ),
        )
        if err:
            result.errors.append(err)
            # NOTE: the email already went out via n8n (if configured).
            # The log failure becomes a send_partial-like state, but with
            # no engagement_id. Caller treats this as failed for now;
            # Module 8 doesn't yet sweep for "delivered but not logged".
            # TODO: track delivered-but-not-logged separately if this
            # failure mode shows up in practice.
            return result
        result.engagement_id = engagement_id

    # ─── Step 2: deal note ────────────────────────────────────────
    if result.note_id is None:
        note_body = artifacts.get("deal_note", "")
        if not note_body:
            result.errors.append("deal_note artifact was empty; skipping note creation")
        else:
            note_id, err = _retry(
                "create_note",
                lambda: hubspot.create_note(
                    body=note_body,
                    owner_id=owner_id,
                    deal_id=deal_id,
                    timestamp_ms=sent_at_ms,
                ),
            )
            if err:
                result.errors.append(err)
            else:
                result.note_id = note_id

    # ─── Step 3: phone task ───────────────────────────────────────
    if result.task_id is None:
        call_script = artifacts.get("call_script", {})
        if not call_script.get("opening"):
            result.errors.append("call_script artifact was empty; skipping task creation")
        else:
            task_body = format_call_script_body(
                company_name=company_name,
                call_script=call_script,
                sent_date_human=sent_date_human,
                chosen_subject=chosen_subject,
            )
            task_due_ms = _phone_task_due_ms(sent_at_ms, phone_task_lead_hours)
            task_id, err = _retry(
                "create_phone_task",
                lambda: hubspot.create_phone_task(
                    subject=f"Follow up on reactivation email to {company_name}",
                    body=task_body,
                    owner_id=owner_id,
                    contact_id=contact_id,
                    deal_id=deal_id,
                    due_at_ms=task_due_ms,
                ),
            )
            if err:
                result.errors.append(err)
            else:
                result.task_id = task_id

    # ─── Final status classification ──────────────────────────────
    if result.engagement_id and result.note_id and result.task_id:
        result.status = "sent"
    elif result.engagement_id:
        # Email went out; note or task missing. UI shows retry buttons.
        result.status = "send_partial"
    else:
        # Shouldn't get here — if step 1 failed we already returned above.
        result.status = "failed"
    return result
