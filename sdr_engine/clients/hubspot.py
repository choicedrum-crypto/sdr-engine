"""HubSpot client — covers Module 2.5 (contact CRUD + association) and
Module 7 (email engagement, deal note, phone task creation).

All requests target the v3 CRM endpoints. Bearer-token auth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_BASE_URL = "https://api.hubapi.com"


class HubSpotError(Exception):
    """Raised on any non-success HTTP response. Carries status + body."""

    def __init__(self, status: int, body: str, *, operation: str) -> None:
        super().__init__(f"HubSpot {operation} failed: status={status} body={body[:200]}")
        self.status = status
        self.body = body
        self.operation = operation


@dataclass
class HubSpotContact:
    """Just enough of the contact record for downstream use."""
    contact_id: str
    email: str
    first_name: str
    last_name: str
    job_title: str
    raw: dict[str, Any]


class HubSpotClient:
    """Thin wrapper. Each method either returns a typed result or raises
    HubSpotError. No retry — orchestrator owns retry policy.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 30,
    ) -> None:
        if not api_key:
            raise ValueError("HubSpotClient requires an api_key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def create_contact(
        self,
        email: str,
        first_name: str,
        last_name: str,
        job_title: str,
        enrichment_source: str | None = None,
        enrichment_confidence: float | None = None,
    ) -> HubSpotContact:
        """POST /crm/v3/objects/contacts.

        Sets custom properties enrichment_source + enrichment_confidence
        so analytics can later separate enriched-then-converted from
        existing-contact-then-converted (per the contact_source queue column).
        """
        properties: dict[str, Any] = {
            "email": email,
            "firstname": first_name,
            "lastname": last_name,
            "jobtitle": job_title,
        }
        if enrichment_source:
            properties["enrichment_source"] = enrichment_source
        if enrichment_confidence is not None:
            properties["enrichment_confidence"] = enrichment_confidence

        resp = requests.post(
            f"{self.base_url}/crm/v3/objects/contacts",
            headers=self._headers(),
            json={"properties": properties},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise HubSpotError(resp.status_code, resp.text, operation="create_contact")
        data = resp.json()
        props = data.get("properties", {})
        return HubSpotContact(
            contact_id=str(data["id"]),
            email=props.get("email", email),
            first_name=props.get("firstname", first_name),
            last_name=props.get("lastname", last_name),
            job_title=props.get("jobtitle", job_title),
            raw=data,
        )

    def associate_contact_to_company(self, contact_id: str, company_id: str) -> None:
        """PUT /crm/v3/objects/contacts/{id}/associations/companies/{co_id}/contact_to_company."""
        resp = requests.put(
            f"{self.base_url}/crm/v3/objects/contacts/{contact_id}/associations/companies/"
            f"{company_id}/contact_to_company",
            headers=self._headers(),
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise HubSpotError(resp.status_code, resp.text, operation="associate_contact_to_company")

    def associate_contact_to_deal(self, contact_id: str, deal_id: str) -> None:
        """PUT /crm/v3/objects/contacts/{id}/associations/deals/{deal_id}/contact_to_deal."""
        resp = requests.put(
            f"{self.base_url}/crm/v3/objects/contacts/{contact_id}/associations/deals/"
            f"{deal_id}/contact_to_deal",
            headers=self._headers(),
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise HubSpotError(resp.status_code, resp.text, operation="associate_contact_to_deal")

    def delete_contact(self, contact_id: str) -> None:
        """DELETE /crm/v3/objects/contacts/{id} — used in Module 2.5 rollback.

        Raises HubSpotError on any failure (the orchestrator catches this
        and marks the enrichment_cache row as 'orphan' so manual cleanup
        can find it later).
        """
        resp = requests.delete(
            f"{self.base_url}/crm/v3/objects/contacts/{contact_id}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        # 404 is OK — contact may have been deleted by another path. Treat as success.
        if resp.status_code == 404:
            return
        if resp.status_code >= 400:
            raise HubSpotError(resp.status_code, resp.text, operation="delete_contact")

    # ─── Module 7 — engagement / note / task writes ────────────────
    def create_email_engagement(
        self,
        *,
        subject: str,
        body: str,
        owner_id: str,
        contact_id: str,
        deal_id: str,
        timestamp_ms: int,
    ) -> str:
        """POST /crm/v3/objects/emails — log the email send under the SDR's
        owner identity. Returns the engagement_id.

        Per Open Q #7's resolution: setting hubspot_owner_id correctly is what
        renders 'sent on behalf of Daniel' in the prospect's inbox rather than
        a noreply@hubspot.com fallback. Verify via the pre-launch sender test
        in docs/ARCHITECTURE.md.
        """
        body_doc = {
            "properties": {
                "hs_email_subject": subject,
                "hs_email_text": body,
                "hs_email_direction": "EMAIL",
                "hs_timestamp": str(timestamp_ms),
                "hubspot_owner_id": owner_id,
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 198}],
                },
                {
                    "to": {"id": deal_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 210}],
                },
            ],
        }
        resp = requests.post(
            f"{self.base_url}/crm/v3/objects/emails",
            headers=self._headers(),
            json=body_doc,
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise HubSpotError(resp.status_code, resp.text, operation="create_email_engagement")
        return str(resp.json()["id"])

    def create_note(
        self,
        *,
        body: str,
        owner_id: str,
        deal_id: str,
        timestamp_ms: int,
    ) -> str:
        """POST /crm/v3/objects/notes — create the internal SDR-brief note
        associated to the deal. Owner is the SDR so the note is attributable
        in HubSpot's activity feed.
        """
        body_doc = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(timestamp_ms),
                "hubspot_owner_id": owner_id,
            },
            "associations": [
                {
                    "to": {"id": deal_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}],
                },
            ],
        }
        resp = requests.post(
            f"{self.base_url}/crm/v3/objects/notes",
            headers=self._headers(),
            json=body_doc,
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise HubSpotError(resp.status_code, resp.text, operation="create_note")
        return str(resp.json()["id"])

    def create_phone_task(
        self,
        *,
        subject: str,
        body: str,
        owner_id: str,
        contact_id: str,
        deal_id: str,
        due_at_ms: int,
        priority: str = "HIGH",
        status: str = "NOT_STARTED",
    ) -> str:
        """POST /crm/v3/objects/tasks — schedule the paired follow-up call.

        The body is the LLM-drafted call script formatted as Markdown by
        sdr_engine.send.format_call_script_body() before being passed in.
        Default due 36h after the email per PHONE_TASK_LEAD_HOURS env var.
        """
        body_doc = {
            "properties": {
                "hs_task_subject": subject,
                "hs_task_body": body,
                "hs_task_priority": priority,
                "hs_task_status": status,
                "hs_task_type": "CALL",
                "hs_timestamp": str(due_at_ms),
                "hubspot_owner_id": owner_id,
            },
            "associations": [
                {
                    "to": {"id": deal_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 216}],
                },
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 204}],
                },
            ],
        }
        resp = requests.post(
            f"{self.base_url}/crm/v3/objects/tasks",
            headers=self._headers(),
            json=body_doc,
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise HubSpotError(resp.status_code, resp.text, operation="create_phone_task")
        return str(resp.json()["id"])
