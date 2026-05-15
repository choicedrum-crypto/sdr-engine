"""Tests for sdr_engine.clients.zoominfo + sdr_engine.clients.hubspot.

Mocked HTTP with `responses`. No live ZoomInfo or HubSpot calls.
"""
from __future__ import annotations

import pytest
import responses

from sdr_engine.clients.hubspot import HubSpotClient, HubSpotError
from sdr_engine.clients.zoominfo import ZoomInfoClient


# ─── ZoomInfo client ───────────────────────────────────────────────
def test_zoominfo_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        ZoomInfoClient(api_key="")


def test_zoominfo_search_requires_company_identifier() -> None:
    client = ZoomInfoClient(api_key="test")
    with pytest.raises(ValueError, match="company_domain or company_name"):
        client.search_contacts(
            company_domain=None, company_name=None, job_titles=["CFO"]
        )


@responses.activate
def test_zoominfo_search_returns_typed_candidates() -> None:
    responses.add(
        responses.POST,
        "https://api.zoominfo.com/search/contact",
        json={
            "data": [
                {
                    "id": 100,
                    "firstName": "Jane",
                    "lastName": "Doe",
                    "title": "Chief Financial Officer",
                    "country": "United States",
                    "companyId": 999,
                    "companyName": "Acme Co",
                    "confidence": 0.95,
                }
            ]
        },
        status=200,
    )
    client = ZoomInfoClient(api_key="test")
    candidates = client.search_contacts(
        company_domain="acme.com", company_name=None, job_titles=["CFO"]
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.contact_id == "100"
    assert c.first_name == "Jane"
    assert c.title == "Chief Financial Officer"
    assert c.confidence == 0.95
    assert c.company_id == "999"


@responses.activate
def test_zoominfo_search_sends_country_and_titles() -> None:
    responses.add(
        responses.POST,
        "https://api.zoominfo.com/search/contact",
        json={"data": []},
        status=200,
    )
    client = ZoomInfoClient(api_key="test")
    client.search_contacts(
        company_domain="acme.com",
        company_name=None,
        job_titles=["CFO", "Controller"],
        country="United States",
    )
    import json as _json
    sent = _json.loads(responses.calls[0].request.body)
    assert sent["countries"] == ["United States"]
    assert sent["jobTitles"] == ["CFO", "Controller"]
    assert sent["companyDomains"] == ["acme.com"]
    assert sent["activeOnly"] is True


@responses.activate
def test_zoominfo_search_falls_back_to_company_name() -> None:
    responses.add(
        responses.POST,
        "https://api.zoominfo.com/search/contact",
        json={"data": []},
        status=200,
    )
    client = ZoomInfoClient(api_key="test")
    client.search_contacts(
        company_domain=None,
        company_name="Acme Co",
        job_titles=["CFO"],
    )
    import json as _json
    sent = _json.loads(responses.calls[0].request.body)
    assert "companyDomains" not in sent
    assert sent["companyNames"] == ["Acme Co"]


@responses.activate
def test_zoominfo_enrich_returns_email() -> None:
    responses.add(
        responses.POST,
        "https://api.zoominfo.com/enrich/contact",
        json={
            "data": [
                {
                    "id": 100,
                    "firstName": "Jane",
                    "lastName": "Doe",
                    "title": "CFO",
                    "email": "jane@acme.com",
                    "country": "United States",
                    "confidence": 0.95,
                }
            ]
        },
        status=200,
    )
    client = ZoomInfoClient(api_key="test")
    enriched = client.enrich_contact("100")
    assert enriched is not None
    assert enriched.email == "jane@acme.com"
    assert enriched.contact_id == "100"


@responses.activate
def test_zoominfo_enrich_404_returns_none() -> None:
    responses.add(
        responses.POST,
        "https://api.zoominfo.com/enrich/contact",
        json={"error": "contact not found"},
        status=404,
    )
    client = ZoomInfoClient(api_key="test")
    assert client.enrich_contact("missing") is None


# ─── HubSpot client ────────────────────────────────────────────────
def test_hubspot_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        HubSpotClient(api_key="")


@responses.activate
def test_hubspot_create_contact_returns_typed_contact() -> None:
    responses.add(
        responses.POST,
        "https://api.hubapi.com/crm/v3/objects/contacts",
        json={
            "id": "12345",
            "properties": {
                "email": "j@acme.com",
                "firstname": "Jane",
                "lastname": "Doe",
                "jobtitle": "CFO",
            },
        },
        status=201,
    )
    client = HubSpotClient(api_key="test")
    contact = client.create_contact(
        email="j@acme.com",
        first_name="Jane",
        last_name="Doe",
        job_title="CFO",
        enrichment_source="ZoomInfo 2026-05-15",
        enrichment_confidence=0.95,
    )
    assert contact.contact_id == "12345"
    assert contact.email == "j@acme.com"

    import json as _json
    sent = _json.loads(responses.calls[0].request.body)
    props = sent["properties"]
    assert props["email"] == "j@acme.com"
    assert props["enrichment_source"] == "ZoomInfo 2026-05-15"
    assert props["enrichment_confidence"] == 0.95


@responses.activate
def test_hubspot_create_contact_raises_on_400() -> None:
    responses.add(
        responses.POST,
        "https://api.hubapi.com/crm/v3/objects/contacts",
        json={"message": "duplicate email"},
        status=409,
    )
    client = HubSpotClient(api_key="test")
    with pytest.raises(HubSpotError) as exc:
        client.create_contact(
            email="dup@acme.com", first_name="J", last_name="D", job_title="CFO"
        )
    assert exc.value.status == 409
    assert exc.value.operation == "create_contact"


@responses.activate
def test_hubspot_associate_to_company_succeeds() -> None:
    responses.add(
        responses.PUT,
        "https://api.hubapi.com/crm/v3/objects/contacts/12345/associations/"
        "companies/999/contact_to_company",
        json={"id": "12345"},
        status=200,
    )
    client = HubSpotClient(api_key="test")
    client.associate_contact_to_company("12345", "999")  # no raise


@responses.activate
def test_hubspot_associate_to_deal_raises_on_5xx() -> None:
    responses.add(
        responses.PUT,
        "https://api.hubapi.com/crm/v3/objects/contacts/12345/associations/"
        "deals/777/contact_to_deal",
        body="upstream error",
        status=503,
    )
    client = HubSpotClient(api_key="test")
    with pytest.raises(HubSpotError) as exc:
        client.associate_contact_to_deal("12345", "777")
    assert exc.value.status == 503
    assert exc.value.operation == "associate_contact_to_deal"


@responses.activate
def test_hubspot_delete_contact_swallows_404() -> None:
    """404 = already gone. Treat as successful idempotent delete."""
    responses.add(
        responses.DELETE,
        "https://api.hubapi.com/crm/v3/objects/contacts/12345",
        status=404,
    )
    client = HubSpotClient(api_key="test")
    client.delete_contact("12345")  # no raise


@responses.activate
def test_hubspot_delete_contact_raises_on_500() -> None:
    """Anything else 4xx/5xx must raise so the orchestrator marks orphan."""
    responses.add(
        responses.DELETE,
        "https://api.hubapi.com/crm/v3/objects/contacts/12345",
        body="server died",
        status=500,
    )
    client = HubSpotClient(api_key="test")
    with pytest.raises(HubSpotError):
        client.delete_contact("12345")
