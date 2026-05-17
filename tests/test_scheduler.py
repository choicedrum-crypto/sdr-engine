"""Tests for sdr_engine.scheduler — Module 1 main loop.

Covers:
  - Working-day index (holidays + weekends)
  - Round-robin slot determinism
  - Sharp-window detection
  - End-to-end loop with mocked HubSpot + mocked LLM
  - Cooldown integration
  - Compliance filter (opt-out, bounced, quarantined)
  - Rate cap (sharp priority, round-robin overflow defer)
  - LLM failure handling (NEEDS_HUMAN still enqueued)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import responses

from sdr_engine.clients.hubspot import HubSpotClient
from sdr_engine.scheduler import (
    ScheduleConfig,
    in_sharp_window,
    round_robin_slot,
    run_scheduler,
    working_day_index,
)

ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "prompts" / "draft-pipeline.txt"
CTAS_PATH = ROOT / "prompts" / "ctas.json"

SEARCH_URL = "https://api.hubapi.com/crm/v3/objects/deals/search"
LLM_URL = "http://127.0.0.1:4000/v1/chat/completions"


# ─── working_day_index ─────────────────────────────────────────────
def test_working_day_index_jan_1_is_zero_on_weekday() -> None:
    # 2026-01-01 is a Thursday → working day 0 (start of year), and it's a US holiday
    # so weekday=0 but holiday filter skips it. Test on a non-holiday weekday instead.
    assert working_day_index(date(2026, 1, 2), holidays=set()) == 1  # Friday Jan 2


def test_working_day_index_skips_weekends() -> None:
    # 2026-01-05 is a Monday — should be working day 3 (Jan 2 Fri = 1, Jan 5 Mon = 3
    # because Jan 1 Thu is included as day 0)
    # Actually count: Jan 1 Thu = 0, Jan 2 Fri = 1, Jan 5 Mon = 2
    assert working_day_index(date(2026, 1, 5), holidays=set()) == 2


def test_working_day_index_skips_holidays() -> None:
    holidays = {date(2026, 1, 1)}  # New Year's Day skipped
    # Jan 2 Fri is now working day 0 since Jan 1 was skipped
    assert working_day_index(date(2026, 1, 2), holidays=holidays) == 0


def test_working_day_index_returns_zero_for_first_working_day_of_year() -> None:
    # If Jan 1 is a Saturday (e.g., 2022), the first weekday Jan 3 Mon is index 0
    # Test with a year where Jan 1 is a weekend
    holidays = set()
    assert working_day_index(date(2022, 1, 3), holidays=holidays) == 0


# ─── round_robin_slot — determinism ────────────────────────────────
def test_round_robin_slot_is_deterministic() -> None:
    """Same deal_id always lands on the same slot."""
    s1 = round_robin_slot("deal-123", 250)
    s2 = round_robin_slot("deal-123", 250)
    assert s1 == s2
    assert 0 <= s1 < 250


def test_round_robin_slot_differs_per_deal() -> None:
    """Different deals should mostly land on different slots (probabilistic)."""
    slots = {round_robin_slot(f"deal-{i}", 250) for i in range(100)}
    # 100 random hashes into 250 slots should give us ~80+ unique values
    assert len(slots) > 50


# ─── in_sharp_window ───────────────────────────────────────────────
def test_sharp_window_fires_when_renewal_minus_60_is_in_next_7_days() -> None:
    today = date(2026, 6, 1)
    # renewal 2026-08-01 → target send 2026-06-02 → within today+7 days
    assert in_sharp_window("2026-08-01", today, lead_days=60, window_days=7)


def test_sharp_window_does_not_fire_for_distant_renewal() -> None:
    today = date(2026, 6, 1)
    # renewal 2026-12-01 → target send 2026-10-02 → way past +7 days
    assert not in_sharp_window("2026-12-01", today, lead_days=60, window_days=7)


def test_sharp_window_does_not_fire_for_past_renewal() -> None:
    today = date(2026, 6, 1)
    assert not in_sharp_window("2026-04-01", today, lead_days=60, window_days=7)


def test_sharp_window_returns_false_for_null_renewal() -> None:
    today = date(2026, 6, 1)
    assert not in_sharp_window(None, today, lead_days=60, window_days=7)


def test_sharp_window_handles_iso_with_time_suffix() -> None:
    today = date(2026, 6, 1)
    assert in_sharp_window(
        "2026-08-01T00:00:00.000Z", today, lead_days=60, window_days=7,
    )


# ─── End-to-end (mocked HubSpot + mocked LLM) ──────────────────────
def _hs_deal_json(
    deal_id: str = "60298035984",
    funnel_type: str = "former_client",
    contact_id: str = "95001773907",
    company_id: str = "54969818907",
    renewal_date: str | None = None,
    dealname: str = "Test Deal",
) -> dict:
    """Build a search-results-style deal dict for mocking."""
    return {
        "id": deal_id,
        "properties": {
            "dealname": dealname,
            "amount": "500000",
            "createdate": "2024-03-15T10:00:00Z",
            "closedate": None,
            "pipeline": "prospecting-pipeline-id",
            "dealstage": "appointmentscheduled",
            "funnel_type": funnel_type,
            "renewal_date": renewal_date,
            "notes_last_contacted_date": "2024-01-01T00:00:00Z",
        },
        "associations": {
            "contacts": {"results": [{"id": contact_id, "type": "deal_to_contact"}]},
            "companies": {"results": [{"id": company_id, "type": "deal_to_company"}]},
        },
    }


def _hs_contact_json(
    contact_id: str = "95001773907",
    email: str = "test@example.com",
    optout: str | None = None,
) -> dict:
    return {
        "id": contact_id,
        "properties": {
            "email": email,
            "firstname": "Daniel",
            "lastname": "Bradley",
            "hs_email_optout": optout,
            "hs_email_bad_address": None,
            "hs_email_quarantined": None,
        },
    }


def _llm_response(cta: str) -> dict:
    """Build a valid Module 4 response."""
    body = "Dear Daniel,\n\n" + (" ".join(["word"] * 145)) + " " + cta + "\n\nBest regards,\nDaniel Bradley"
    return {
        "choices": [{"message": {"content": json.dumps({
            "deal_note": " ".join(["w"] * 80),
            "email": {
                "subject_options": ["subject one", "subject two"],
                "body": body,
                "word_count": len(body.split()),
                "cta_chosen": cta,
            },
            "call_script": {
                "opening": "Hi Daniel, this is Daniel Bradley from TCIA.",
                "context_bridge": "Following up on industry news.",
                "observation": "Saw your sector under pressure.",
                "open_question": "How are you thinking about diversification?",
                "objection_bridges": {
                    "already_covered": "Understood — circle back later.",
                    "not_interested": "No worries — keep us in mind.",
                    "send_info": "Will send a one-pager today.",
                },
            },
            "metadata": {"anchor_used": "none", "hook_used": "none"},
        })}}],
    }


def _first_cta_for(prospect_type: str) -> str:
    from sdr_engine.llm import filter_ctas
    return filter_ctas(CTAS_PATH, prospect_type)[0]


@responses.activate
def test_scheduler_enqueues_qualified_deal(db_conn: sqlite3.Connection) -> None:
    """End-to-end: a former_client deal with valid contact + funnel_type
    matching today's round-robin slot → enqueued."""
    deal_id = "deal-rr-1"
    # Mock search results
    responses.add(responses.POST, SEARCH_URL,
                  json={"results": [_hs_deal_json(deal_id=deal_id)], "paging": {}}, status=200)
    # Mock contact fetch
    responses.add(
        responses.GET,
        "https://api.hubapi.com/crm/v3/objects/contacts/95001773907?properties=email,firstname,lastname,hs_email_optout,hs_email_bad_address,hs_email_quarantined",
        json=_hs_contact_json(),
        status=200,
        match_querystring=False,
    )
    # Mock engagement-association fetch (empty)
    responses.add(
        responses.GET,
        f"https://api.hubapi.com/crm/v4/objects/deals/{deal_id}/associations/notes",
        json={"results": []}, status=200,
    )
    # Mock LLM
    cta = _first_cta_for("former_client")
    responses.add(responses.POST, LLM_URL, json=_llm_response(cta), status=200)

    # Force today's working-day index to match the deal's round-robin slot
    target_slot = round_robin_slot(deal_id, 250)
    with patch("sdr_engine.scheduler.working_day_index", return_value=target_slot):
        config = ScheduleConfig(
            pipeline_id="prospecting-pipeline-id",
            llm_endpoint=LLM_URL,
        )
        result = run_scheduler(
            db=db_conn,
            hubspot=HubSpotClient(api_key="test"),
            prompt_path=PROMPT_PATH,
            ctas_path=CTAS_PATH,
            config=config,
        )

    assert result.candidates_examined == 1
    assert result.enqueued == 1
    assert result.enqueued_round_robin == 1
    # Verify the queue row was actually inserted
    row = db_conn.execute(
        "SELECT deal_id, status, prospect_type FROM queue WHERE deal_id = ?",
        (deal_id,),
    ).fetchone()
    assert row is not None
    assert row[1] == "pending"
    assert row[2] == "former_client"


@responses.activate
def test_scheduler_skips_deal_not_in_todays_slot(db_conn: sqlite3.Connection) -> None:
    """Deal's round-robin slot doesn't match today's index → silently skipped
    (not counted as 'dropped' — this is expected non-action for most deals)."""
    deal_id = "deal-not-today"
    responses.add(responses.POST, SEARCH_URL,
                  json={"results": [_hs_deal_json(deal_id=deal_id)], "paging": {}}, status=200)
    # No contact fetch mock — if scheduler tried to fetch, would error

    target_slot = round_robin_slot(deal_id, 250)
    different_slot = (target_slot + 1) % 250

    with patch("sdr_engine.scheduler.working_day_index", return_value=different_slot):
        result = run_scheduler(
            db=db_conn,
            hubspot=HubSpotClient(api_key="test"),
            prompt_path=PROMPT_PATH,
            ctas_path=CTAS_PATH,
            config=ScheduleConfig(pipeline_id="ppid"),
        )

    assert result.candidates_examined == 1
    assert result.enqueued == 0
    # Not dropped — just not this deal's day. dropped_by_reason stays empty.
    assert result.dropped == 0


@responses.activate
def test_scheduler_drops_deal_without_known_funnel_type(db_conn: sqlite3.Connection) -> None:
    """A deal tagged with only unknown funnel_type values → drop with
    missing_prospect_type. Recorded in the dropped table."""
    deal_id = "deal-unk-funnel"
    responses.add(
        responses.POST, SEARCH_URL,
        json={"results": [_hs_deal_json(deal_id=deal_id, funnel_type="banker_abl_referral")],
              "paging": {}},
        status=200,
    )
    # Mock contact fetch (still happens before context-gather realizes type is bad)
    responses.add(
        responses.GET,
        "https://api.hubapi.com/crm/v3/objects/contacts/95001773907?properties=email,firstname,lastname,hs_email_optout,hs_email_bad_address,hs_email_quarantined",
        json=_hs_contact_json(),
        status=200,
        match_querystring=False,
    )

    target_slot = round_robin_slot(deal_id, 250)
    with patch("sdr_engine.scheduler.working_day_index", return_value=target_slot):
        result = run_scheduler(
            db=db_conn, hubspot=HubSpotClient(api_key="test"),
            prompt_path=PROMPT_PATH, ctas_path=CTAS_PATH,
            config=ScheduleConfig(pipeline_id="ppid"),
        )

    assert result.dropped == 1
    assert "missing_prospect_type" in result.dropped_by_reason
    # Dropped table has the record
    row = db_conn.execute(
        "SELECT reason FROM dropped WHERE deal_id = ?", (deal_id,),
    ).fetchone()
    assert row[0] == "missing_prospect_type"


@responses.activate
def test_scheduler_drops_opted_out_contact(db_conn: sqlite3.Connection) -> None:
    """Contact with hs_email_optout=true → drop with no_compliant_contact."""
    deal_id = "deal-optout"
    responses.add(responses.POST, SEARCH_URL,
                  json={"results": [_hs_deal_json(deal_id=deal_id)], "paging": {}}, status=200)
    # Contact returns opted-out
    responses.add(
        responses.GET,
        "https://api.hubapi.com/crm/v3/objects/contacts/95001773907?properties=email,firstname,lastname,hs_email_optout,hs_email_bad_address,hs_email_quarantined",
        json=_hs_contact_json(optout="true"),
        status=200,
        match_querystring=False,
    )

    target_slot = round_robin_slot(deal_id, 250)
    with patch("sdr_engine.scheduler.working_day_index", return_value=target_slot):
        result = run_scheduler(
            db=db_conn, hubspot=HubSpotClient(api_key="test"),
            prompt_path=PROMPT_PATH, ctas_path=CTAS_PATH,
            config=ScheduleConfig(pipeline_id="ppid"),
        )

    assert result.dropped == 1
    assert "opted_out" in result.dropped_by_reason


@responses.activate
def test_scheduler_respects_active_queue_row_cooldown(db_conn: sqlite3.Connection) -> None:
    """Deal with an existing 'pending' queue row → skip on this run."""
    deal_id = "deal-already-pending"
    # Pre-insert a pending row for the same deal_id
    db_conn.execute(
        "INSERT INTO queue (deal_id, company_id, company_name, contact_id, contact_email, "
        "send_reason, prospect_type, draft_subject, draft_body, deal_note_body, "
        "call_script_json, status) VALUES (?, 'co', 'A', 'ct', 'x@y.com', 'round-robin', "
        "'former_client', 's', 'b', 'n', '{}', 'pending')",
        (deal_id,),
    )

    responses.add(responses.POST, SEARCH_URL,
                  json={"results": [_hs_deal_json(deal_id=deal_id)], "paging": {}}, status=200)
    # No contact fetch mock — cooldown check fires before contact resolution

    target_slot = round_robin_slot(deal_id, 250)
    with patch("sdr_engine.scheduler.working_day_index", return_value=target_slot):
        result = run_scheduler(
            db=db_conn, hubspot=HubSpotClient(api_key="test"),
            prompt_path=PROMPT_PATH, ctas_path=CTAS_PATH,
            config=ScheduleConfig(pipeline_id="ppid"),
        )

    assert result.dropped == 1  # incremented by cooldown branch
    # Cooldown reason key starts with "already_in_flight"
    assert any(k.startswith("already_in_flight") for k in result.dropped_by_reason)


@responses.activate
def test_scheduler_writes_runs_heartbeat_row(db_conn: sqlite3.Connection) -> None:
    """Every run writes a 'runs' row that the UI's staleness banner reads."""
    responses.add(responses.POST, SEARCH_URL, json={"results": [], "paging": {}}, status=200)

    run_scheduler(
        db=db_conn, hubspot=HubSpotClient(api_key="test"),
        prompt_path=PROMPT_PATH, ctas_path=CTAS_PATH,
        config=ScheduleConfig(pipeline_id="ppid"),
    )
    row = db_conn.execute(
        "SELECT status, enqueued_count, dropped_count FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "success"
    assert row[1] == 0
    assert row[2] == 0


@responses.activate
def test_scheduler_handles_search_failure_gracefully(db_conn: sqlite3.Connection) -> None:
    """HubSpot search 500 → run ends with errors recorded, doesn't crash."""
    responses.add(responses.POST, SEARCH_URL, status=500)

    result = run_scheduler(
        db=db_conn, hubspot=HubSpotClient(api_key="test"),
        prompt_path=PROMPT_PATH, ctas_path=CTAS_PATH,
        config=ScheduleConfig(pipeline_id="ppid"),
    )
    assert result.errors
    assert "search failed" in result.errors[0]
    # Heartbeat row finalized to 'failed' or 'partial' (depending on if error was recorded)
    row = db_conn.execute(
        "SELECT status FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert row[0] in ("partial", "failed")


@responses.activate
def test_scheduler_paginates_through_multiple_pages(db_conn: sqlite3.Connection) -> None:
    """Search returns paging.next.after → scheduler fetches next page."""
    # First page: 1 deal with after-cursor
    deal_1 = "deal-page1"
    deal_2 = "deal-page2"
    responses.add(
        responses.POST, SEARCH_URL,
        json={
            "results": [_hs_deal_json(deal_id=deal_1)],
            "paging": {"next": {"after": "cursor-2"}},
        },
        status=200,
    )
    responses.add(
        responses.POST, SEARCH_URL,
        json={"results": [_hs_deal_json(deal_id=deal_2)], "paging": {}},
        status=200,
    )

    # Make both deals NOT in today's slot — focus on pagination, not enqueue logic
    with patch("sdr_engine.scheduler.working_day_index", return_value=-1):
        result = run_scheduler(
            db=db_conn, hubspot=HubSpotClient(api_key="test"),
            prompt_path=PROMPT_PATH, ctas_path=CTAS_PATH,
            config=ScheduleConfig(pipeline_id="ppid"),
        )
    assert result.candidates_examined == 2  # both pages processed


@responses.activate
def test_scheduler_sharp_bucket_overrides_rate_cap(db_conn: sqlite3.Connection) -> None:
    """Sharp bucket has priority and should enqueue even when over cap.
    Round-robin overflows defer; sharp does not."""
    deal_id = "deal-sharp"
    # Renewal 65 days out → sharp window (60 lead + 5 days)
    today = date.today()
    renewal_iso = (today + timedelta(days=65)).isoformat()

    responses.add(
        responses.POST, SEARCH_URL,
        json={"results": [_hs_deal_json(deal_id=deal_id, renewal_date=renewal_iso)],
              "paging": {}},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.hubapi.com/crm/v3/objects/contacts/95001773907?properties=email,firstname,lastname,hs_email_optout,hs_email_bad_address,hs_email_quarantined",
        json=_hs_contact_json(),
        status=200,
        match_querystring=False,
    )
    responses.add(
        responses.GET,
        f"https://api.hubapi.com/crm/v4/objects/deals/{deal_id}/associations/notes",
        json={"results": []}, status=200,
    )
    cta = _first_cta_for("former_client")
    responses.add(responses.POST, LLM_URL, json=_llm_response(cta), status=200)

    # daily_send_cap = 0 — every round-robin would defer. Sharp should still enqueue.
    config = ScheduleConfig(
        pipeline_id="ppid", daily_send_cap=0, llm_endpoint=LLM_URL,
    )
    result = run_scheduler(
        db=db_conn, hubspot=HubSpotClient(api_key="test"),
        prompt_path=PROMPT_PATH, ctas_path=CTAS_PATH,
        config=config,
    )
    assert result.enqueued == 1
    assert result.enqueued_sharp == 1
    assert result.deferred_overflow == 0


@responses.activate
def test_scheduler_round_robin_defers_when_over_cap(db_conn: sqlite3.Connection) -> None:
    """Round-robin overflow should bump deferred_overflow counter, NOT enqueue."""
    deal_id = "deal-rr-defer"
    responses.add(responses.POST, SEARCH_URL,
                  json={"results": [_hs_deal_json(deal_id=deal_id)], "paging": {}},
                  status=200)
    # No further mocks — over-cap deals bail before contact fetch is needed

    target_slot = round_robin_slot(deal_id, 250)
    with patch("sdr_engine.scheduler.working_day_index", return_value=target_slot):
        # Pre-fill the scheduler's notion of inserted counter via a queue row
        # to push us over cap. But the actual rate check is against the scheduler's
        # per-run counter, not the db. Set cap = 0 to force overflow on first deal.
        config = ScheduleConfig(pipeline_id="ppid", daily_send_cap=0)
        result = run_scheduler(
            db=db_conn, hubspot=HubSpotClient(api_key="test"),
            prompt_path=PROMPT_PATH, ctas_path=CTAS_PATH,
            config=config,
        )
    assert result.deferred_overflow == 1
    assert result.enqueued == 0


@responses.activate
def test_scheduler_enqueues_needs_human_card_on_llm_failure(db_conn: sqlite3.Connection) -> None:
    """Module 4's NEEDS_HUMAN result → row still inserted, but flagged so
    Module 6's UI shows the AUTO-DRAFT FAILED banner."""
    deal_id = "deal-llm-fail"
    responses.add(responses.POST, SEARCH_URL,
                  json={"results": [_hs_deal_json(deal_id=deal_id)], "paging": {}}, status=200)
    responses.add(
        responses.GET,
        "https://api.hubapi.com/crm/v3/objects/contacts/95001773907?properties=email,firstname,lastname,hs_email_optout,hs_email_bad_address,hs_email_quarantined",
        json=_hs_contact_json(),
        status=200,
        match_querystring=False,
    )
    responses.add(
        responses.GET,
        f"https://api.hubapi.com/crm/v4/objects/deals/{deal_id}/associations/notes",
        json={"results": []}, status=200,
    )

    # LLM returns bad JSON every time — all 3 attempts fail
    for _ in range(3):
        responses.add(responses.POST, LLM_URL,
                      json={"choices": [{"message": {"content": "not json"}}]}, status=200)

    target_slot = round_robin_slot(deal_id, 250)
    with patch("sdr_engine.scheduler.working_day_index", return_value=target_slot):
        result = run_scheduler(
            db=db_conn, hubspot=HubSpotClient(api_key="test"),
            prompt_path=PROMPT_PATH, ctas_path=CTAS_PATH,
            config=ScheduleConfig(pipeline_id="ppid", llm_endpoint=LLM_URL),
        )

    assert result.enqueued == 1
    assert result.llm_failures == 1
    # Row is enqueued with NEEDS_HUMAN body
    row = db_conn.execute(
        "SELECT draft_body FROM queue WHERE deal_id = ?", (deal_id,),
    ).fetchone()
    assert "LLM format failure" in row[0]
