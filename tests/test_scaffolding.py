"""Sanity tests for the scaffolding itself — schema parses, configs are valid JSON, CTA coverage holds."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROSPECT_TYPES = {
    "former_client",
    "bor_target",
    "coi_referral",
    "lost_opportunity",
    "industry_trigger",
    "conference",
    "bankruptcy_trigger",
}


def test_schema_loads_into_empty_db(tmp_queue_db: Path) -> None:
    """sql/schema.sql is valid and creates the expected tables."""
    conn = sqlite3.connect(tmp_queue_db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    tables = {r[0] for r in rows}
    assert {"queue", "dropped", "enrichment_cache", "runs"}.issubset(tables)


def test_schema_enforces_prospect_type_enum(db_conn: sqlite3.Connection) -> None:
    """Invalid prospect_type values must be rejected by the CHECK constraint."""
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO queue (deal_id, company_id, company_name, contact_id, "
            "contact_email, send_reason, prospect_type, draft_subject, draft_body, "
            "deal_note_body, call_script_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("d1", "c1", "Co", "ct1", "x@y.com", "round-robin", "not_a_real_type",
             "subj", "body", "note", "{}"),
        )


def test_ctas_json_parses_and_covers_all_prospect_types() -> None:
    """ctas.json is valid JSON AND every prospect_type has at least one CTA (A14 invariant)."""
    payload = json.loads((ROOT / "prompts" / "ctas.json").read_text(encoding="utf-8"))
    ctas = payload["ctas"]
    assert len(ctas) >= 5, "Need at least 5 approved CTAs"
    for ptype in PROSPECT_TYPES:
        matching = [c for c in ctas if ptype in c["allowed_for"]]
        assert matching, f"No CTA has allowed_for containing {ptype!r}"


def test_holidays_json_parses() -> None:
    """holidays.json is valid JSON and contains at least 10 entries for the year."""
    payload = json.loads((ROOT / "config" / "holidays.json").read_text(encoding="utf-8"))
    assert payload["year"] == 2026
    assert len(payload["holidays"]) >= 10


def test_brokers_json_parses() -> None:
    """tcia-brokers.json is valid JSON."""
    payload = json.loads((ROOT / "config" / "tcia-brokers.json").read_text(encoding="utf-8"))
    assert "brokers" in payload


def test_sector_signals_json_parses() -> None:
    """sector-signals.json is valid JSON."""
    payload = json.loads((ROOT / "config" / "sector-signals.json").read_text(encoding="utf-8"))
    assert "signals_by_naics" in payload


def test_env_example_lists_all_required_keys() -> None:
    """.env.example references every key the codebase will read."""
    env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    required = [
        "HUBSPOT_API_KEY", "HUBSPOT_OWNER_ID", "HUBSPOT_PROSPECTING_PIPELINE_ID",
        "HUBSPOT_PROSPECT_TYPE_PROPERTY", "APOLLO_API_KEY", "ZOOMINFO_API_KEY",
        "MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET", "MS_GRAPH_TENANT_ID",
        "SHAREPOINT_SITE_ID", "SHAREPOINT_DRIVE_ID", "SHAREPOINT_POLICY_FOLDER_ID",
        "LLM_ENDPOINT", "LLM_MODEL_PRIMARY", "LLM_MODEL_FALLBACK",
        "MAX_CLOUD_FALLBACK_PER_DAY", "SQLITE_PATH", "REVIEW_UI_PORT", "REVIEW_UI_HOST",
        "DAILY_SEND_CAP", "OPENCLAW_WEBHOOK_URL",
        "CLOUDFLARE_TUNNEL_HOSTNAME", "CLOUDFLARE_TUNNEL_UUID",
    ]
    for key in required:
        assert f"{key}=" in env_text, f".env.example is missing {key}"


def test_scheduler_workflow_preflights_with_correct_bearer_expression() -> None:
    """Hostinger n8n must prove auth/reachability before the side-effectful scheduler POST."""
    payload = json.loads(
        (ROOT / "n8n" / "workflows" / "sdr-scheduler.json").read_text(encoding="utf-8")
    )
    nodes = {node["name"]: node for node in payload["nodes"]}
    expected_auth = '={{ "Bearer " + $env.SCHEDULER_AUTH_TOKEN }}'

    assert payload.get("active") is False
    assert nodes["Scheduler preflight"]["parameters"]["method"] == "GET"
    assert nodes["Scheduler preflight"]["parameters"]["url"].endswith("/scheduler/ready")
    assert nodes["Run scheduler"]["parameters"]["method"] == "POST"
    assert nodes["Run scheduler"]["parameters"]["url"].endswith("/scheduler/run")

    for node_name in ("Scheduler preflight", "Run scheduler"):
        headers = {
            h["name"]: h["value"]
            for h in nodes[node_name]["parameters"]["headerParameters"]["parameters"]
        }
        assert headers["Authorization"] == expected_auth

    trigger_next = payload["connections"]["Daily 06:00 weekdays"]["main"][0][0]["node"]
    preflight_next = payload["connections"]["Scheduler preflight"]["main"][0][0]["node"]
    assert trigger_next == "Scheduler preflight"
    assert preflight_next == "Run scheduler"
