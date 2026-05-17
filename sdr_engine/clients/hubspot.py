"""HubSpot client — covers Module 1 (deal search + engagement fetch),
Module 2.5 (contact CRUD + association), and Module 7 (email engagement,
deal note, phone task creation).

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


@dataclass
class HubSpotDeal:
    """Module 1's typed view of a Prospecting-pipeline deal.

    Fields are intentionally narrow — these are exactly what the
    scheduler classification + Module 4 drafting need. Raw payload
    preserved for diagnostics + future enrichment.
    """
    deal_id: str
    dealname: str
    pipeline: str
    dealstage: str
    funnel_type: str            # raw multi-checkbox value, semicolon-separated
    funnel_types_parsed: list[str]  # split + cleaned
    amount: float | None
    createdate: str | None
    closedate: str | None
    renewal_date: str | None    # may be empty/null
    notes_last_contacted_date: str | None
    contact_ids: list[str]      # primary + secondary associated contacts
    company_ids: list[str]
    raw: dict[str, Any]


@dataclass
class HubSpotEngagement:
    """One engagement record (email, note, call, meeting...). Module 3
    fetches the last 3 per deal to seed the LLM's context."""
    engagement_id: str
    engagement_type: str         # EMAIL | NOTE | CALL | MEETING | TASK
    body: str                    # truncated to ~500 chars upstream
    created_at: str
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

    # ─── Module 1 — deal search + engagement fetch ─────────────────
    def search_deals_in_pipeline(
        self,
        pipeline_id: str,
        *,
        funnel_type_property: str = "funnel_type",
        last_contacted_cutoff_ms: int | None = None,
        limit: int = 100,
        after: str | None = None,
    ) -> tuple[list[HubSpotDeal], str | None]:
        """POST /crm/v3/objects/deals/search filtered to a pipeline.

        Returns (deals, next_after_cursor). Caller paginates by passing
        the cursor back in for subsequent calls until next_after is None.

        Filters applied:
          - pipeline == pipeline_id (the Prospecting pipeline)
          - notes_last_contacted_date < last_contacted_cutoff_ms IF provided
            (the architecture's 90-day quiet-period rule)

        Pulls properties needed for classification + Module 4 substitution +
        Module 1 cooldown/bucket logic. Associations: contacts + companies.
        """
        filters: list[dict[str, Any]] = [
            {"propertyName": "pipeline", "operator": "EQ", "value": pipeline_id},
        ]
        if last_contacted_cutoff_ms is not None:
            filters.append({
                "propertyName": "notes_last_contacted_date",
                "operator": "LT",
                "value": str(last_contacted_cutoff_ms),
            })

        body: dict[str, Any] = {
            "filterGroups": [{"filters": filters}],
            "properties": [
                "dealname", "amount", "createdate", "closedate",
                "dealstage", "pipeline", "renewal_date",
                "notes_last_contacted_date", "hs_lastmodifieddate",
                funnel_type_property,
            ],
            "limit": limit,
            "associations": ["contacts", "companies"],
        }
        if after:
            body["after"] = after

        resp = requests.post(
            f"{self.base_url}/crm/v3/objects/deals/search",
            headers=self._headers(),
            json=body,
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise HubSpotError(resp.status_code, resp.text, operation="search_deals_in_pipeline")
        data = resp.json()
        deals = [_deal_from_raw(r, funnel_type_property) for r in data.get("results", [])]
        next_after = data.get("paging", {}).get("next", {}).get("after")
        return deals, next_after

    def fetch_deal_engagements(
        self,
        deal_id: str,
        *,
        limit: int = 3,
        body_char_cap: int = 500,
    ) -> list[HubSpotEngagement]:
        """Return the last `limit` engagements on a deal, sorted by created_at desc.

        Module 3 uses these as anchor + hook hints for the LLM prompt.
        Each body is truncated to `body_char_cap` chars to keep the
        prompt under budget — full bodies are not needed for context.

        Two API calls:
          1. GET associations contact_to_engagement / deal_to_engagement
             (deal → engagement_ids)
          2. POST batch read /crm/v3/objects/notes (or unified
             /crm/v3/objects/engagements) for body content

        HubSpot's engagement model splits across notes/emails/calls/tasks/
        meetings — to keep things simple this implementation fetches notes
        only (the most common engagement type containing prose). Other
        types add complexity for marginal LLM context value at MVP.
        """
        # Step 1: get associated notes for the deal.
        assoc_resp = requests.get(
            f"{self.base_url}/crm/v4/objects/deals/{deal_id}/associations/notes",
            headers=self._headers(),
            timeout=self.timeout,
        )
        if assoc_resp.status_code == 404:
            return []
        if assoc_resp.status_code >= 400:
            raise HubSpotError(
                assoc_resp.status_code, assoc_resp.text,
                operation="fetch_deal_engagements_associations",
            )
        assoc_ids = [str(r["toObjectId"]) for r in assoc_resp.json().get("results", [])]
        if not assoc_ids:
            return []

        # Step 2: batch-fetch the note bodies. Sort by createdate desc client-side.
        batch_body = {
            "properties": ["hs_note_body", "hs_createdate"],
            "inputs": [{"id": nid} for nid in assoc_ids[:50]],  # batch cap
        }
        batch_resp = requests.post(
            f"{self.base_url}/crm/v3/objects/notes/batch/read",
            headers=self._headers(),
            json=batch_body,
            timeout=self.timeout,
        )
        if batch_resp.status_code >= 400:
            raise HubSpotError(
                batch_resp.status_code, batch_resp.text,
                operation="fetch_deal_engagements_batch",
            )
        notes = batch_resp.json().get("results", [])
        notes.sort(
            key=lambda n: n.get("properties", {}).get("hs_createdate", ""),
            reverse=True,
        )
        return [
            HubSpotEngagement(
                engagement_id=str(n["id"]),
                engagement_type="NOTE",
                body=(n.get("properties", {}).get("hs_note_body", "") or "")[:body_char_cap],
                created_at=n.get("properties", {}).get("hs_createdate", ""),
                raw=n,
            )
            for n in notes[:limit]
        ]


def _deal_from_raw(raw: dict[str, Any], funnel_type_property: str) -> HubSpotDeal:
    """Convert a HubSpot search result row into a typed HubSpotDeal."""
    props = raw.get("properties", {})
    funnel_raw = props.get(funnel_type_property, "") or ""
    funnel_parsed = [v.strip() for v in funnel_raw.split(";") if v.strip()]

    # Associations come back nested. Extract contact + company IDs robustly
    # across the v3 and v4 response shapes.
    contact_ids: list[str] = []
    company_ids: list[str] = []
    associations = raw.get("associations") or {}
    contacts_block = associations.get("contacts") or {}
    companies_block = associations.get("companies") or {}
    for r in contacts_block.get("results", []):
        cid = r.get("id") or r.get("toObjectId")
        if cid:
            contact_ids.append(str(cid))
    for r in companies_block.get("results", []):
        coid = r.get("id") or r.get("toObjectId")
        if coid:
            company_ids.append(str(coid))

    amount_str = props.get("amount")
    try:
        amount = float(amount_str) if amount_str else None
    except (TypeError, ValueError):
        amount = None

    return HubSpotDeal(
        deal_id=str(raw["id"]),
        dealname=props.get("dealname", "") or "",
        pipeline=props.get("pipeline", "") or "",
        dealstage=props.get("dealstage", "") or "",
        funnel_type=funnel_raw,
        funnel_types_parsed=funnel_parsed,
        amount=amount,
        createdate=props.get("createdate") or None,
        closedate=props.get("closedate") or None,
        renewal_date=props.get("renewal_date") or None,
        notes_last_contacted_date=props.get("notes_last_contacted_date") or None,
        contact_ids=contact_ids,
        company_ids=company_ids,
        raw=raw,
    )
