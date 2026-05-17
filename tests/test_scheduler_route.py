"""Tests for the POST /scheduler/run Flask route.

Flask test-client + mocked HubSpot. Same code path as
scripts/run_scheduler.py so this validates the webhook-flavored
invocation works identically.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
from server import create_app  # noqa: E402

SEARCH_URL = "https://api.hubapi.com/crm/v3/objects/deals/search"


@pytest.fixture
def app_no_auth(tmp_queue_db):
    """Flask app without scheduler auth token — open route."""
    return create_app(
        sqlite_path=tmp_queue_db,
        hubspot_api_key="test-hs-key",
        hubspot_owner_id="owner-99",
        n8n_send_webhook_url="",
        scheduler_auth_token="",  # explicit empty = no auth
    )


@pytest.fixture
def app_with_auth(tmp_queue_db):
    """Flask app with a scheduler auth token configured."""
    # Need a HUBSPOT_PROSPECTING_PIPELINE_ID env var for config_from_env;
    # the test patches via responses below.
    import os
    os.environ["HUBSPOT_PROSPECTING_PIPELINE_ID"] = "test-pipeline-id"
    return create_app(
        sqlite_path=tmp_queue_db,
        hubspot_api_key="test-hs-key",
        hubspot_owner_id="owner-99",
        n8n_send_webhook_url="",
        scheduler_auth_token="secret-token-123",
    )


@pytest.fixture(autouse=True)
def set_pipeline_env(monkeypatch):
    """All scheduler route tests need a pipeline ID in env (config_from_env reads it)."""
    monkeypatch.setenv("HUBSPOT_PROSPECTING_PIPELINE_ID", "test-pipeline-id")


# ─── Auth ──────────────────────────────────────────────────────────
def test_scheduler_route_open_when_no_token_configured(app_no_auth) -> None:
    """When SCHEDULER_AUTH_TOKEN is empty, the route accepts requests
    without an Authorization header. Acceptable behind Cloudflare Access."""
    client = app_no_auth.test_client()
    # Mock HubSpot search to return empty so the run completes cleanly
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, SEARCH_URL, json={"results": [], "paging": {}}, status=200)
        resp = client.post("/scheduler/run")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["candidates_examined"] == 0


def test_scheduler_route_rejects_missing_auth_when_configured(app_with_auth) -> None:
    """When token is set, requests without Authorization header → 401."""
    client = app_with_auth.test_client()
    resp = client.post("/scheduler/run")
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


def test_scheduler_route_rejects_wrong_token(app_with_auth) -> None:
    client = app_with_auth.test_client()
    resp = client.post(
        "/scheduler/run",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_scheduler_route_accepts_matching_token(app_with_auth) -> None:
    client = app_with_auth.test_client()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, SEARCH_URL, json={"results": [], "paging": {}}, status=200)
        resp = client.post(
            "/scheduler/run",
            headers={"Authorization": "Bearer secret-token-123"},
        )
    assert resp.status_code == 200


def test_scheduler_route_rejects_token_without_bearer_prefix(app_with_auth) -> None:
    """The literal string check means 'secret-token-123' alone is rejected;
    must be 'Bearer secret-token-123'."""
    client = app_with_auth.test_client()
    resp = client.post(
        "/scheduler/run",
        headers={"Authorization": "secret-token-123"},
    )
    assert resp.status_code == 401


# ─── Returned shape ────────────────────────────────────────────────
def test_scheduler_route_returns_full_result_dict(app_no_auth) -> None:
    client = app_no_auth.test_client()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, SEARCH_URL, json={"results": [], "paging": {}}, status=200)
        resp = client.post("/scheduler/run")
    body = resp.get_json()
    # All ScheduleResult fields surface as JSON keys
    for key in (
        "candidates_examined", "enqueued", "enqueued_sharp", "enqueued_round_robin",
        "dropped", "dropped_by_reason", "deferred_overflow", "llm_failures", "errors",
    ):
        assert key in body, f"missing field in response: {key}"


def test_scheduler_route_returns_500_on_search_failure(app_no_auth) -> None:
    """HubSpot search 500 → route returns 500 so n8n's IF-node fires alert."""
    client = app_no_auth.test_client()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, SEARCH_URL, status=500)
        resp = client.post("/scheduler/run")
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["errors"]
    assert "search failed" in body["errors"][0]


# ─── Missing config ────────────────────────────────────────────────
def test_scheduler_route_returns_500_when_pipeline_id_missing(
    tmp_queue_db, monkeypatch,
) -> None:
    """No HUBSPOT_PROSPECTING_PIPELINE_ID → 500 with explanatory error."""
    monkeypatch.delenv("HUBSPOT_PROSPECTING_PIPELINE_ID", raising=False)
    app = create_app(
        sqlite_path=tmp_queue_db,
        hubspot_api_key="test-hs-key",
        hubspot_owner_id="owner-99",
        n8n_send_webhook_url="",
        scheduler_auth_token="",
    )
    resp = app.test_client().post("/scheduler/run")
    assert resp.status_code == 500
    assert "HUBSPOT_PROSPECTING_PIPELINE_ID" in resp.get_json()["error"]


def test_scheduler_route_returns_500_when_hubspot_key_missing(
    tmp_queue_db, monkeypatch,
) -> None:
    """No HUBSPOT_API_KEY → 500 with explanatory error."""
    app = create_app(
        sqlite_path=tmp_queue_db,
        hubspot_api_key="",  # explicit empty
        hubspot_owner_id="owner-99",
        n8n_send_webhook_url="",
        scheduler_auth_token="",
    )
    resp = app.test_client().post("/scheduler/run")
    assert resp.status_code == 500
    assert "HUBSPOT_API_KEY" in resp.get_json()["error"]


# ─── Heartbeat row written ─────────────────────────────────────────
def test_scheduler_route_writes_heartbeat_row(app_no_auth, tmp_queue_db) -> None:
    """Webhook-flavored runs should write the same runs row as CLI runs —
    both share the RunHeartbeat context manager via run_scheduler()."""
    client = app_no_auth.test_client()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, SEARCH_URL, json={"results": [], "paging": {}}, status=200)
        client.post("/scheduler/run")
    conn = sqlite3.connect(tmp_queue_db)
    row = conn.execute(
        "SELECT status, enqueued_count FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "success"
    assert row[1] == 0
