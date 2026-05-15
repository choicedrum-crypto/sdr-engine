"""HubSpot client — contact create + associate + delete (rollback).

Only the operations Module 2.5 needs. Module 7 (HubSpot writes for email +
deal note + phone task) will live alongside this — a future PR adds the
corresponding methods on the same client.

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
