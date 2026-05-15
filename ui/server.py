"""SDR Engine — Review UI server.

Single-file Flask app, ~200 lines target. Email-only card layout per T3=A:
the deal note and call script auto-post to HubSpot (Module 7); the SDR
reviews/edits only the email here, and adjusts the other artifacts in
HubSpot directly while working the deal.

Bound to 127.0.0.1:5679 only (A5). Remote access via Cloudflare Tunnel
at https://sdr.tradecredit.agency — Cloudflare Access handles auth at
the edge; this server doesn't need its own login flow.

Routes:
  GET  /queue                    — next pending card
  POST /queue/{id}/send          — send (auto-detects dirty edit, stores diff)
  POST /queue/{id}/skip          — 30-day cooldown
  POST /queue/{id}/retry-note    — for send_partial: retry HubSpot note creation
  POST /queue/{id}/retry-task    — for send_partial: retry HubSpot task creation
  POST /queue/force              — manual injection bypassing dedupe/cap
  GET  /health                   — JSON status of LLM, HubSpot, SQLite, last_run
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request
from dotenv import load_dotenv

load_dotenv()

# ─── Config ────────────────────────────────────────────────────
SQLITE_PATH = Path(os.path.expanduser(os.getenv("SQLITE_PATH", "~/.sdr-engine/queue.db")))
HOST = os.getenv("REVIEW_UI_HOST", "127.0.0.1")
PORT = int(os.getenv("REVIEW_UI_PORT", "5679"))
LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://127.0.0.1:4000/v1/chat/completions")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")

# Card is considered "stale" if loaded >2h ago (per A8 amendment)
STALE_AFTER_SECONDS = 2 * 60 * 60
# Surface staleness banner if last successful scheduler run >25h ago
STALE_RUN_HOURS = 25

app = Flask(__name__, template_folder="templates", static_folder="static")


# ─── DB helpers ────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    """Open a connection with WAL + busy_timeout already applied to the DB."""
    conn = sqlite3.connect(SQLITE_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


# ─── Routes ────────────────────────────────────────────────────
@app.get("/health")
def health():
    """Health check for post-deploy verification + UI top-of-page indicator (A7)."""
    status = {
        "llm": _probe_llm(),
        "hubspot": _probe_hubspot(),
        "sqlite": _probe_sqlite(),
        "last_run": None,
        "last_run_age_seconds": None,
    }

    try:
        with get_db() as db:
            row = db.execute(
                "SELECT started_at, status FROM runs WHERE status = 'success' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if row:
            status["last_run"] = row["started_at"]
            # Best-effort age calc; SQLite's strftime('%s') returns seconds since epoch
            with get_db() as db:
                age = db.execute(
                    "SELECT (strftime('%s','now') - strftime('%s', ?)) AS age", (row["started_at"],)
                ).fetchone()
                status["last_run_age_seconds"] = age["age"] if age else None
    except sqlite3.Error:
        pass  # already reflected in sqlite probe

    overall = "ok" if all(v == "ok" for k, v in status.items() if k in ("llm", "hubspot", "sqlite")) else "degraded"
    status["overall"] = overall
    return jsonify(status)


@app.get("/queue")
def queue_view():
    """Render the next pending card. TODO: implement state-aware rendering."""
    # TODO(module-6): implement state machine: loading → warming → caught_up_done → card → send_partial → stale
    abort(501, "Module 6 UI not yet implemented")


@app.post("/queue/<int:queue_id>/send")
def queue_send(queue_id: int):
    """Send the email. Auto-detect dirty edit by comparing posted body to draft_body."""
    # TODO(module-6): invoke Module 7 HubSpot writes; store edit_diff if dirty
    abort(501, "Module 7 send path not yet implemented")


@app.post("/queue/<int:queue_id>/skip")
def queue_skip(queue_id: int):
    """Skip — 30-day cooldown enforced by Module 1."""
    # TODO(module-6): mark skipped, actioned_at = now
    abort(501)


@app.post("/queue/<int:queue_id>/retry-note")
def queue_retry_note(queue_id: int):
    """Retry HubSpot note creation only — for send_partial state."""
    # TODO(module-7): re-call note engagement endpoint
    abort(501)


@app.post("/queue/<int:queue_id>/retry-task")
def queue_retry_task(queue_id: int):
    """Retry HubSpot phone-task creation only — for send_partial state."""
    # TODO(module-7): re-call task creation
    abort(501)


@app.post("/queue/force")
def queue_force():
    """Manual injection bypassing dedupe + rate cap.

    Compliance filter (opt-out, bounced, quarantined) is NOT bypassed —
    legal hard requirement.
    """
    # TODO(module-6): accept {deal_id}, validate in HubSpot, run Modules 2-5 inline
    abort(501)


# ─── Probes ────────────────────────────────────────────────────
def _probe_llm() -> str:
    """Return 'ok' if the LiteLLM endpoint responds, else 'down' or 'unreachable'."""
    try:
        import requests
        # LiteLLM exposes /health; if not, /v1/models is a cheap GET
        base = LLM_ENDPOINT.rsplit("/v1/", 1)[0]
        r = requests.get(f"{base}/health", timeout=3)
        return "ok" if r.status_code < 500 else "down"
    except Exception:  # noqa: BLE001 — health probe should never raise
        return "unreachable"


def _probe_hubspot() -> str:
    """Return 'ok' if HubSpot API token works, else 'unauthorized' or 'down'."""
    if not HUBSPOT_API_KEY:
        return "unconfigured"
    try:
        import requests
        r = requests.get(
            "https://api.hubapi.com/account-info/v3/details",
            headers={"Authorization": f"Bearer {HUBSPOT_API_KEY}"},
            timeout=5,
        )
        if r.status_code == 200:
            return "ok"
        if r.status_code == 401:
            return "unauthorized"
        return "down"
    except Exception:
        return "unreachable"


def _probe_sqlite() -> str:
    """Return 'ok' if the queue DB is readable, else 'missing' or 'locked'."""
    if not SQLITE_PATH.exists():
        return "missing"
    try:
        with get_db() as db:
            db.execute("SELECT 1").fetchone()
        return "ok"
    except sqlite3.OperationalError as exc:
        return "locked" if "locked" in str(exc).lower() else "error"
    except Exception:
        return "error"


# ─── Entry point ───────────────────────────────────────────────
if __name__ == "__main__":
    # Refuse to bind to 0.0.0.0 unless explicitly opted-in. The Cloudflare
    # Tunnel reaches us locally; LAN exposure is a separate decision.
    if HOST not in {"127.0.0.1", "localhost"} and os.getenv("ALLOW_LAN_BIND") != "true":
        print(
            f"REFUSING to bind to {HOST}. Use 127.0.0.1 (default) and reach "
            "the UI via Cloudflare Tunnel, OR set ALLOW_LAN_BIND=true to override.",
            file=sys.stderr,
        )
        sys.exit(1)

    app.run(host=HOST, port=PORT, debug=False)
