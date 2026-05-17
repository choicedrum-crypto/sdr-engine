"""Tests for sdr_engine.context — Module 3 MVP.

Pure helpers + the orchestrator wired against mocked HubSpot.
"""
from __future__ import annotations

import pytest
import responses

from sdr_engine.clients.hubspot import HubSpotClient, HubSpotDeal
from sdr_engine.context import (
    KNOWN_TYPES,
    derive_company_acronym,
    gather_context_for_deal,
    humanize_quote_date,
    pick_prospect_type,
)


# ─── pick_prospect_type — priority order ───────────────────────────
def test_pick_returns_warmest_when_multi_tagged() -> None:
    assert pick_prospect_type(["bor_target", "former_client"]) == "former_client"


def test_pick_picks_lost_opportunity_over_bor_target() -> None:
    """lost_opportunity is warmer than bor_target per priority order."""
    assert pick_prospect_type(["bor_target", "lost_opportunity"]) == "lost_opportunity"


def test_pick_picks_coi_referral_over_industry_trigger() -> None:
    assert pick_prospect_type(["industry_trigger", "coi_referral"]) == "coi_referral"


def test_pick_returns_none_for_empty() -> None:
    assert pick_prospect_type([]) is None


def test_pick_returns_none_for_unknown_only() -> None:
    """A deal tagged with only 'banker_abl_referral' (which doesn't exist in
    TCIA's pipeline) shouldn't get classified."""
    assert pick_prospect_type(["banker_abl_referral", "cold_new_prospect"]) is None


def test_pick_ignores_unknown_alongside_known() -> None:
    """Unknown values are silently filtered; known ones still classified."""
    assert pick_prospect_type(["banker_abl_referral", "former_client"]) == "former_client"


@pytest.mark.parametrize("ptype", sorted(KNOWN_TYPES))
def test_pick_single_known_type_returns_itself(ptype: str) -> None:
    assert pick_prospect_type([ptype]) == ptype


# ─── humanize_quote_date ───────────────────────────────────────────
def test_humanize_handles_iso_with_z() -> None:
    assert humanize_quote_date("2024-03-15T10:30:00Z") == "March 2024"


def test_humanize_handles_iso_with_offset() -> None:
    assert humanize_quote_date("2024-09-01T00:00:00+00:00") == "September 2024"


def test_humanize_handles_empty() -> None:
    assert humanize_quote_date(None) == ""
    assert humanize_quote_date("") == ""


def test_humanize_handles_malformed() -> None:
    assert humanize_quote_date("not a date") == ""


# ─── derive_company_acronym ────────────────────────────────────────
def test_acronym_drops_common_suffixes() -> None:
    assert derive_company_acronym("Advance Polybag Inc") == "AP"
    assert derive_company_acronym("Acme Corp") == "A"


def test_acronym_drops_articles() -> None:
    assert derive_company_acronym("The Tideline Group LLC") == "TG"


def test_acronym_preserves_short_uppercase() -> None:
    assert derive_company_acronym("API") == "API"
    assert derive_company_acronym("TCIA") == "TCIA"


def test_acronym_falls_back_to_prefix_when_all_dropped() -> None:
    """All-suffix name (rare but possible) — fall back to first 4 chars."""
    assert derive_company_acronym("Corp Inc LLC") == "Corp"


def test_acronym_handles_empty() -> None:
    assert derive_company_acronym("") == ""


# ─── gather_context_for_deal — happy path ──────────────────────────
def _make_deal(
    funnel_types: list[str] | None = None,
    renewal_date: str | None = None,
    deal_id: str = "deal-test-1",
    contact_ids: list[str] | None = None,
) -> HubSpotDeal:
    return HubSpotDeal(
        deal_id=deal_id,
        dealname="Acme Test",
        pipeline="prospecting-pipeline-id",
        dealstage="appointmentscheduled",
        funnel_type=";".join(funnel_types or ["former_client"]),
        funnel_types_parsed=funnel_types or ["former_client"],
        amount=500000.0,
        createdate="2024-03-15T10:00:00Z",
        closedate=None,
        renewal_date=renewal_date,
        notes_last_contacted_date=None,
        contact_ids=contact_ids or ["ct-1"],
        company_ids=["co-1"],
        raw={},
    )


@responses.activate
def test_gather_context_produces_module4_compatible_inputs() -> None:
    """Output should be ready to pass directly to sdr_engine.llm.draft()."""
    # Mock the engagement-association fetch (returns empty for MVP)
    responses.add(
        responses.GET,
        "https://api.hubapi.com/crm/v4/objects/deals/deal-test-1/associations/notes",
        json={"results": []},
        status=200,
    )

    hubspot = HubSpotClient(api_key="test")
    deal = _make_deal(funnel_types=["former_client"], renewal_date=None)
    result = gather_context_for_deal(
        deal,
        hubspot,
        company_name="Acme Test Co",
        contact_first_name="Daniel",
        contact_last_name="Bradley",
        contact_email="d@y.com",
    )
    assert result.drop_reason is None
    assert result.inputs is not None
    inputs = result.inputs
    assert inputs["prospect_type"] == "former_client"
    assert inputs["send_reason"] == "round-robin"  # no renewal_date → round-robin
    assert inputs["company_name"] == "Acme Test Co"
    assert inputs["contact_first_name"] == "Daniel"
    assert inputs["quote_date_human"] == "March 2024"
    assert inputs["anchor_type"] == "none"   # MVP — anchor extraction deferred
    assert inputs["hook_source"] == "none"


@responses.activate
def test_gather_context_picks_sharp_send_reason_when_renewal_known() -> None:
    responses.add(
        responses.GET,
        "https://api.hubapi.com/crm/v4/objects/deals/deal-test-1/associations/notes",
        json={"results": []},
        status=200,
    )
    hubspot = HubSpotClient(api_key="test")
    deal = _make_deal(renewal_date="2026-08-15")
    result = gather_context_for_deal(
        deal, hubspot,
        contact_first_name="D", contact_email="d@y.com",
    )
    assert result.inputs["send_reason"] == "renewal"


def test_gather_context_drops_when_no_known_prospect_type() -> None:
    hubspot = HubSpotClient(api_key="test")
    deal = _make_deal(funnel_types=["unknown_value"])
    result = gather_context_for_deal(deal, hubspot, contact_email="d@y.com")
    assert result.inputs is None
    assert result.drop_reason == "missing_prospect_type"


def test_gather_context_drops_when_no_contact_email() -> None:
    hubspot = HubSpotClient(api_key="test")
    deal = _make_deal()
    result = gather_context_for_deal(deal, hubspot, contact_email="")
    assert result.inputs is None
    assert result.drop_reason == "no_valid_contact_after_enrichment"


@responses.activate
def test_gather_context_tolerates_engagement_fetch_failure() -> None:
    """Engagement fetch failure shouldn't block context-gather; LLM
    tolerates missing anchor/hook by falling back to generic value-touch."""
    responses.add(
        responses.GET,
        "https://api.hubapi.com/crm/v4/objects/deals/deal-test-1/associations/notes",
        status=500,
    )
    hubspot = HubSpotClient(api_key="test")
    deal = _make_deal()
    result = gather_context_for_deal(
        deal, hubspot, contact_email="d@y.com", contact_first_name="D",
    )
    # Still produces inputs, just with empty engagement context
    assert result.drop_reason is None
    assert result.inputs is not None
    assert result.inputs["anchor_context"] is None
