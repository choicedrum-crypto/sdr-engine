"""ZoomInfo client — search + enrich.

TCIA's convention (per autoplan 2026-05-14): search_contacts is the cheap
candidate-finder (low/no credit cost); enrich_contacts is the credit-burning
detail-fetcher. The orchestrator calls search first, locally filters the
candidates, then enriches only the chosen one. This matches Daniel's
existing operational discipline with ZoomInfo across other workflows.

The exact ZoomInfo API endpoints depend on subscription tier; this module
takes the base URL as configuration. Default targets the Enterprise API
shape (api.zoominfo.com). For tier-specific quirks, override
ZOOMINFO_SEARCH_PATH and ZOOMINFO_ENRICH_PATH env vars.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

# Default Enterprise API paths. Overridable via env if a different tier.
DEFAULT_SEARCH_PATH = "/search/contact"
DEFAULT_ENRICH_PATH = "/enrich/contact"


@dataclass
class ContactCandidate:
    """One result row from ZoomInfo search_contacts. No email yet — that
    requires the credit-burning enrich call."""
    contact_id: str
    first_name: str
    last_name: str
    title: str
    country: str
    company_id: str | None
    company_name: str
    confidence: float
    # Raw payload preserved for enrichment lookup + audit trail
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EnrichedContact:
    """Full contact record after enrich_contacts. Email is the load-bearing field."""
    contact_id: str
    first_name: str
    last_name: str
    title: str
    email: str
    country: str
    confidence: float
    raw: dict[str, Any] = field(default_factory=dict)


class ZoomInfoClient:
    """Thin wrapper over ZoomInfo's REST API.

    Args:
      api_key: bearer token for the Authorization header.
      base_url: e.g. 'https://api.zoominfo.com'.
      timeout: per-request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.zoominfo.com",
        search_path: str = DEFAULT_SEARCH_PATH,
        enrich_path: str = DEFAULT_ENRICH_PATH,
        timeout: int = 30,
    ) -> None:
        if not api_key:
            raise ValueError("ZoomInfoClient requires an api_key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.search_path = search_path
        self.enrich_path = enrich_path
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def search_contacts(
        self,
        company_domain: str | None,
        company_name: str | None,
        job_titles: list[str],
        country: str = "United States",
        limit: int = 3,
    ) -> list[ContactCandidate]:
        """Find candidates at a company. CHEAP — does not burn enrich credits.

        At least one of company_domain or company_name must be provided.
        country defaults to USA (TCIA's compliance-locked outreach scope).
        Returns up to `limit` candidates sorted by ZoomInfo confidence.
        """
        if not company_domain and not company_name:
            raise ValueError("search_contacts requires company_domain or company_name")

        body: dict[str, Any] = {
            "jobTitles": job_titles,
            "countries": [country],
            "limit": limit,
            "activeOnly": True,
        }
        if company_domain:
            body["companyDomains"] = [company_domain]
        if company_name and not company_domain:
            body["companyNames"] = [company_name]

        resp = requests.post(
            self.base_url + self.search_path,
            headers=self._headers(),
            json=body,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return [_candidate_from_raw(r) for r in resp.json().get("data", [])]

    def enrich_contact(self, contact_id: str) -> EnrichedContact | None:
        """Fetch the email + full record for one specific contact_id.

        BURNS A CREDIT — call only after qualification filtering selects
        the winning candidate. Returns None on 404 (contact no longer
        available); raises requests.RequestException on transport failure.
        """
        if not contact_id:
            raise ValueError("enrich_contact requires a contact_id")
        resp = requests.post(
            self.base_url + self.enrich_path,
            headers=self._headers(),
            json={"contactIds": [contact_id]},
            timeout=self.timeout,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return None
        return _enriched_from_raw(data[0])


def _candidate_from_raw(raw: dict[str, Any]) -> ContactCandidate:
    """Map a raw ZoomInfo search row into our typed candidate.

    Tolerant of missing fields — search results are partial by design.
    """
    return ContactCandidate(
        contact_id=str(raw.get("id", "")),
        first_name=raw.get("firstName", "") or "",
        last_name=raw.get("lastName", "") or "",
        title=raw.get("title", "") or raw.get("jobTitle", "") or "",
        country=raw.get("country", "") or "",
        company_id=str(raw["companyId"]) if raw.get("companyId") else None,
        company_name=raw.get("companyName", "") or "",
        confidence=float(raw.get("confidence", 0.0)),
        raw=raw,
    )


def _enriched_from_raw(raw: dict[str, Any]) -> EnrichedContact:
    """Map a raw enrich response row into our typed enriched contact."""
    return EnrichedContact(
        contact_id=str(raw.get("id", "")),
        first_name=raw.get("firstName", "") or "",
        last_name=raw.get("lastName", "") or "",
        title=raw.get("title", "") or raw.get("jobTitle", "") or "",
        email=raw.get("email", "") or "",
        country=raw.get("country", "") or "",
        confidence=float(raw.get("confidence", 0.0)),
        raw=raw,
    )
