"""Tests for sdr_engine.enrichment.enrich_for_deal — the Module 2.5 orchestrator.

Real SQLite (via the tmp_queue_db fixture), mocked HTTP for ZoomInfo + HubSpot.
"""
from __future__ import annotations

import json as _json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import responses

from sdr_engine.clients.hubspot import HubSpotClient
from sdr_engine.clients.zoominfo import ContactCandidate, ZoomInfoClient
from sdr_engine.enrichment import (
    DEFAULT_TARGET_TITLES,
    EnrichmentConfig,
    enrich_for_deal,
    qualify_candidates,
)

ZI_SEARCH = "https://api.zoominfo.com/search/contact"
ZI_ENRICH = "https://api.zoominfo.com/enrich/contact"
HS_CREATE = "https://api.hubapi.com/crm/v3/objects/contacts"


# ─── Fixtures ──────────────────────────────────────────────────────
@pytest.fixture
def clients() -> tuple[ZoomInfoClient, HubSpotClient]:
    return ZoomInfoClient(api_key="test-zi"), HubSpotClient(api_key="test-hs")


@pytest.fixture
def config() -> EnrichmentConfig:
    return EnrichmentConfig.default()


def _candidate(
    contact_id: str = "100",
    title: str = "Chief Financial Officer",
    country: str = "United States",
    confidence: float = 0.95,
) -> ContactCandidate:
    return ContactCandidate(
        contact_id=contact_id,
        first_name="Jane",
        last_name="Doe",
        title=title,
        country=country,
        company_id="999",
        company_name="Acme",
        confidence=confidence,
    )


def _wrap_zi_search(candidates: list[dict]) -> dict:
    return {"data": candidates}


def _zi_candidate_json(
    cid: int = 100, title: str = "CFO", country: str = "United States", confidence: float = 0.9
) -> dict:
    return {
        "id": cid,
        "firstName": "Jane",
        "lastName": "Doe",
        "title": title,
        "country": country,
        "companyId": 999,
        "companyName": "Acme",
        "confidence": confidence,
    }


def _zi_enriched_json(
    cid: int = 100, email: str = "jane@acme.com", title: str = "CFO", confidence: float = 0.9
) -> dict:
    return {
        "id": cid,
        "firstName": "Jane",
        "lastName": "Doe",
        "title": title,
        "email": email,
        "country": "United States",
        "confidence": confidence,
    }


def _hs_contact_json(cid: str = "abc-123", email: str = "jane@acme.com") -> dict:
    return {"id": cid, "properties": {"email": email, "firstname": "Jane", "lastname": "Doe", "jobtitle": "CFO"}}


# ─── qualify_candidates() unit tests ───────────────────────────────
def test_qualify_picks_cfo_over_controller(config: EnrichmentConfig) -> None:
    chosen = qualify_candidates(
        [
            _candidate(contact_id="A", title="Controller", confidence=0.99),
            _candidate(contact_id="B", title="Chief Financial Officer", confidence=0.80),
        ],
        config,
    )
    assert chosen is not None
    assert chosen.contact_id == "B"  # CFO wins despite lower confidence


def test_qualify_tie_breaks_on_confidence_within_same_priority(config: EnrichmentConfig) -> None:
    chosen = qualify_candidates(
        [
            _candidate(contact_id="A", title="CFO", confidence=0.80),
            _candidate(contact_id="B", title="CFO", confidence=0.95),
        ],
        config,
    )
    assert chosen is not None
    assert chosen.contact_id == "B"


def test_qualify_drops_non_us_candidates(config: EnrichmentConfig) -> None:
    chosen = qualify_candidates(
        [
            _candidate(contact_id="A", title="CFO", country="Canada", confidence=0.95),
            _candidate(contact_id="B", title="Controller", country="United States", confidence=0.7),
        ],
        config,
    )
    assert chosen is not None
    assert chosen.contact_id == "B"


def test_qualify_drops_non_target_titles(config: EnrichmentConfig) -> None:
    chosen = qualify_candidates(
        [
            _candidate(contact_id="A", title="Marketing Coordinator", confidence=0.99),
            _candidate(contact_id="B", title="VP Finance", confidence=0.6),
        ],
        config,
    )
    assert chosen is not None
    assert chosen.contact_id == "B"


def test_qualify_returns_none_when_no_match(config: EnrichmentConfig) -> None:
    chosen = qualify_candidates(
        [_candidate(contact_id="A", title="Sales Manager", country="United States")],
        config,
    )
    assert chosen is None


def test_qualify_returns_none_on_empty_list(config: EnrichmentConfig) -> None:
    assert qualify_candidates([], config) is None


# ─── Orchestrator: cache pre-check ─────────────────────────────────
def _seed_cache(db: sqlite3.Connection, **fields) -> None:
    """Manually insert a row for cache-precheck tests. Defaults given for required cols."""
    base = {
        "company_id": "999",
        "contact_id": "cached-contact-id",
        "source": "zoominfo",
        "status": "success",
        "confidence": 0.9,
        "matched_title": "CFO",
        "enriched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    base.update(fields)
    db.execute(
        "INSERT INTO enrichment_cache (company_id, contact_id, source, status, confidence, "
        "matched_title, enriched_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(base.values()),
    )


def test_orchestrator_returns_cached_hit_when_fresh_success(
    db_conn: sqlite3.Connection, clients, config
) -> None:
    _seed_cache(db_conn, status="success")
    zi, hs = clients
    # No responses.add — if the orchestrator called the network, the test
    # would error since `responses` patches requests when activated; without
    # @responses.activate, requests actually fires (which would fail because
    # the connection refuses). The cache hit short-circuits everything.
    result = enrich_for_deal(
        company_id="999",
        company_domain="acme.com",
        company_name="Acme",
        deal_id="deal-1",
        db=db_conn,
        zoominfo=zi,
        hubspot=hs,
        config=config,
    )
    assert result.success
    assert result.cache_status == "cached_hit"
    assert result.contact_id == "cached-contact-id"
    assert result.credits_used == 0


def test_orchestrator_skips_when_cached_no_match_within_ttl(
    db_conn: sqlite3.Connection, clients, config
) -> None:
    _seed_cache(db_conn, status="no_match", contact_id=None)
    zi, hs = clients
    result = enrich_for_deal(
        company_id="999",
        company_domain="acme.com",
        company_name="Acme",
        deal_id="deal-1",
        db=db_conn,
        zoominfo=zi,
        hubspot=hs,
        config=config,
    )
    assert not result.success
    assert result.cache_status == "cached_miss"
    assert result.credits_used == 0
    assert "no_match within TTL" in result.reason


def test_orchestrator_refuses_when_orphan(
    db_conn: sqlite3.Connection, clients, config
) -> None:
    _seed_cache(db_conn, status="orphan", contact_id="dangling-id")
    zi, hs = clients
    result = enrich_for_deal(
        company_id="999",
        company_domain="acme.com",
        company_name="Acme",
        deal_id="deal-1",
        db=db_conn,
        zoominfo=zi,
        hubspot=hs,
        config=config,
    )
    assert not result.success
    assert result.cache_status == "orphan"


def test_orchestrator_blocks_on_recent_in_progress(
    db_conn: sqlite3.Connection, clients, config
) -> None:
    """If another process is mid-enrich (started < 1h ago), we don't double up."""
    _seed_cache(db_conn, status="in_progress")
    zi, hs = clients
    result = enrich_for_deal(
        company_id="999",
        company_domain="acme.com",
        company_name="Acme",
        deal_id="deal-1",
        db=db_conn,
        zoominfo=zi,
        hubspot=hs,
        config=config,
    )
    assert not result.success
    assert result.cache_status == "in_progress"


@responses.activate
def test_orchestrator_retries_stale_in_progress(
    db_conn: sqlite3.Connection, clients, config
) -> None:
    """If 'in_progress' is >1h old, assume crash and re-enrich."""
    stale_iso = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    _seed_cache(db_conn, status="in_progress", updated_at=stale_iso, enriched_at=stale_iso)
    responses.add(responses.POST, ZI_SEARCH, json=_wrap_zi_search([_zi_candidate_json()]), status=200)
    responses.add(responses.POST, ZI_ENRICH, json={"data": [_zi_enriched_json()]}, status=200)
    responses.add(responses.POST, HS_CREATE, json=_hs_contact_json(), status=201)
    responses.add(
        responses.PUT,
        "https://api.hubapi.com/crm/v3/objects/contacts/abc-123/associations/"
        "companies/999/contact_to_company",
        json={}, status=200,
    )
    responses.add(
        responses.PUT,
        "https://api.hubapi.com/crm/v3/objects/contacts/abc-123/associations/"
        "deals/deal-1/contact_to_deal",
        json={}, status=200,
    )

    zi, hs = clients
    result = enrich_for_deal(
        company_id="999",
        company_domain="acme.com",
        company_name="Acme",
        deal_id="deal-1",
        db=db_conn,
        zoominfo=zi,
        hubspot=hs,
        config=config,
    )
    assert result.success
    assert result.cache_status == "success"
    assert result.credits_used == 1


# ─── Orchestrator: search → qualify → enrich happy path ────────────
@responses.activate
def test_orchestrator_happy_path_end_to_end(
    db_conn: sqlite3.Connection, clients, config
) -> None:
    responses.add(responses.POST, ZI_SEARCH, json=_wrap_zi_search([
        _zi_candidate_json(cid=100, title="CFO", confidence=0.95),
        _zi_candidate_json(cid=101, title="Controller", confidence=0.7),
    ]), status=200)
    responses.add(responses.POST, ZI_ENRICH, json={"data": [_zi_enriched_json(cid=100)]}, status=200)
    responses.add(responses.POST, HS_CREATE, json=_hs_contact_json(cid="hs-abc"), status=201)
    responses.add(
        responses.PUT,
        "https://api.hubapi.com/crm/v3/objects/contacts/hs-abc/associations/"
        "companies/999/contact_to_company",
        json={}, status=200,
    )
    responses.add(
        responses.PUT,
        "https://api.hubapi.com/crm/v3/objects/contacts/hs-abc/associations/"
        "deals/deal-1/contact_to_deal",
        json={}, status=200,
    )

    zi, hs = clients
    result = enrich_for_deal(
        company_id="999",
        company_domain="acme.com",
        company_name="Acme",
        deal_id="deal-1",
        db=db_conn,
        zoominfo=zi,
        hubspot=hs,
        config=config,
    )
    assert result.success
    assert result.contact_id == "hs-abc"
    assert result.contact_email == "jane@acme.com"
    assert result.credits_used == 1

    # Verify enrich was called with the CFO id (100), not the controller (101)
    enrich_body = _json.loads(responses.calls[1].request.body)
    assert enrich_body["contactIds"] == ["100"]

    # Verify cache row is success + carries contact id
    row = db_conn.execute(
        "SELECT contact_id, status FROM enrichment_cache WHERE company_id = ?", ("999",)
    ).fetchone()
    assert row[0] == "hs-abc"
    assert row[1] == "success"


# ─── Orchestrator: qualification gate filters out unqualified ──────
@responses.activate
def test_orchestrator_no_qualified_contact_records_no_match(
    db_conn: sqlite3.Connection, clients, config
) -> None:
    """Search returns rows but none match the title list — should record
    no_match in cache and NOT call enrich (no credit burn)."""
    responses.add(responses.POST, ZI_SEARCH, json=_wrap_zi_search([
        _zi_candidate_json(cid=100, title="Marketing Director"),
        _zi_candidate_json(cid=101, title="Sales Manager"),
    ]), status=200)
    # No enrich call expected — if orchestrator calls it, responses raises ConnectionError.

    zi, hs = clients
    result = enrich_for_deal(
        company_id="999",
        company_domain="acme.com",
        company_name="Acme",
        deal_id="deal-1",
        db=db_conn,
        zoominfo=zi,
        hubspot=hs,
        config=config,
    )
    assert not result.success
    assert result.cache_status == "no_match"
    assert result.reason == "no_qualified_contact_at_company"
    assert result.credits_used == 0

    row = db_conn.execute(
        "SELECT status FROM enrichment_cache WHERE company_id = ?", ("999",)
    ).fetchone()
    assert row[0] == "no_match"


# ─── Orchestrator: partial failures + rollback ─────────────────────
@responses.activate
def test_orchestrator_rolls_back_on_associate_company_failure(
    db_conn: sqlite3.Connection, clients, config
) -> None:
    """Contact is created, but contact↔company association fails. Orchestrator
    must delete the contact AND mark cache as 'error' (since delete succeeded)."""
    responses.add(responses.POST, ZI_SEARCH, json=_wrap_zi_search([_zi_candidate_json()]), status=200)
    responses.add(responses.POST, ZI_ENRICH, json={"data": [_zi_enriched_json()]}, status=200)
    responses.add(responses.POST, HS_CREATE, json=_hs_contact_json(cid="hs-abc"), status=201)
    # Associate company FAILS
    responses.add(
        responses.PUT,
        "https://api.hubapi.com/crm/v3/objects/contacts/hs-abc/associations/"
        "companies/999/contact_to_company",
        body="oops", status=503,
    )
    # Rollback DELETE succeeds
    responses.add(
        responses.DELETE,
        "https://api.hubapi.com/crm/v3/objects/contacts/hs-abc",
        status=204,
    )

    zi, hs = clients
    result = enrich_for_deal(
        company_id="999",
        company_domain="acme.com",
        company_name="Acme",
        deal_id="deal-1",
        db=db_conn,
        zoominfo=zi,
        hubspot=hs,
        config=config,
    )
    assert not result.success
    assert result.cache_status == "error"
    assert result.contact_id is None  # rollback succeeded; no orphan
    assert result.credits_used == 1

    row = db_conn.execute(
        "SELECT status, contact_id FROM enrichment_cache WHERE company_id = ?", ("999",)
    ).fetchone()
    assert row[0] == "error"
    assert row[1] is None


@responses.activate
def test_orchestrator_marks_orphan_when_rollback_delete_also_fails(
    db_conn: sqlite3.Connection, clients, config
) -> None:
    """Worst case: associate fails AND the rollback delete also fails.
    Module 8's nightly sweep needs the orphan row to find this contact."""
    responses.add(responses.POST, ZI_SEARCH, json=_wrap_zi_search([_zi_candidate_json()]), status=200)
    responses.add(responses.POST, ZI_ENRICH, json={"data": [_zi_enriched_json()]}, status=200)
    responses.add(responses.POST, HS_CREATE, json=_hs_contact_json(cid="hs-abc"), status=201)
    responses.add(
        responses.PUT,
        "https://api.hubapi.com/crm/v3/objects/contacts/hs-abc/associations/"
        "deals/deal-1/contact_to_deal",
        status=503,
    )
    responses.add(
        responses.PUT,
        "https://api.hubapi.com/crm/v3/objects/contacts/hs-abc/associations/"
        "companies/999/contact_to_company",
        json={}, status=200,  # company succeeds
    )
    # Rollback DELETE fails
    responses.add(
        responses.DELETE,
        "https://api.hubapi.com/crm/v3/objects/contacts/hs-abc",
        status=500,
    )

    zi, hs = clients
    result = enrich_for_deal(
        company_id="999",
        company_domain="acme.com",
        company_name="Acme",
        deal_id="deal-1",
        db=db_conn,
        zoominfo=zi,
        hubspot=hs,
        config=config,
    )
    assert not result.success
    assert result.cache_status == "orphan"
    assert result.contact_id == "hs-abc"  # preserved so sweep can find it

    row = db_conn.execute(
        "SELECT status, contact_id FROM enrichment_cache WHERE company_id = ?", ("999",)
    ).fetchone()
    assert row[0] == "orphan"
    assert row[1] == "hs-abc"


# ─── Orchestrator: enrich failure paths ────────────────────────────
@responses.activate
def test_orchestrator_marks_error_on_search_failure(
    db_conn: sqlite3.Connection, clients, config
) -> None:
    responses.add(responses.POST, ZI_SEARCH, status=500)
    zi, hs = clients
    result = enrich_for_deal(
        company_id="999",
        company_domain="acme.com",
        company_name="Acme",
        deal_id="deal-1",
        db=db_conn,
        zoominfo=zi,
        hubspot=hs,
        config=config,
    )
    assert not result.success
    assert result.cache_status == "error"
    assert result.credits_used == 0
    assert "zoominfo search failed" in result.reason


@responses.activate
def test_orchestrator_no_email_in_enrich_response_marks_no_match(
    db_conn: sqlite3.Connection, clients, config
) -> None:
    """Enrich returned a contact but no email field — same outcome as no_match."""
    responses.add(responses.POST, ZI_SEARCH, json=_wrap_zi_search([_zi_candidate_json()]), status=200)
    responses.add(
        responses.POST, ZI_ENRICH,
        json={"data": [_zi_enriched_json(email="")]}, status=200,
    )
    zi, hs = clients
    result = enrich_for_deal(
        company_id="999",
        company_domain="acme.com",
        company_name="Acme",
        deal_id="deal-1",
        db=db_conn,
        zoominfo=zi,
        hubspot=hs,
        config=config,
    )
    assert not result.success
    assert result.cache_status == "no_match"
    assert result.credits_used == 1  # we did burn a credit on the enrich call


# ─── Default config ────────────────────────────────────────────────
def test_default_config_includes_all_expected_titles() -> None:
    config = EnrichmentConfig.default()
    assert "CFO" in config.target_titles
    assert "Credit Manager" in config.target_titles
    assert config.country_filter == "United States"
    assert config.cache_ttl_days == 90
    assert config.target_titles == DEFAULT_TARGET_TITLES
