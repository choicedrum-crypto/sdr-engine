"""SDR Engine — Review UI server.

Single-file Flask app. Email-only card layout per T3=A: deal note and call
script auto-post to HubSpot via Module 7; the SDR reviews/edits only the
email here and adjusts the other artifacts in HubSpot directly.

Bound to 127.0.0.1:5679 only (A5). Remote access via Cloudflare Tunnel at
https://sdr.tradecredit.agency — Cloudflare Access handles auth at the
edge; this server doesn't need its own login flow.

Routes:
  GET  /queue                    — next card (state-aware render)
  POST /queue/<id>/send          — call Module 7; auto-detect dirty edit
  POST /queue/<id>/skip          — 30-day cooldown
  POST /queue/<id>/retry-note    — fill missing note on send_partial row
  POST /queue/<id>/retry-task    — fill missing task on send_partial row
  POST /queue/force              — 501 until Modules 1 + 3 land
  GET  /health                   — JSON: LLM/HubSpot/SQLite/last_run

Configurable via the function `create_app()` so tests can pass an
isolated SQLite path. Production entry point reads env vars and starts
the dev server at 127.0.0.1:5679.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for

from sdr_engine.clients.hubspot import HubSpotClient
from sdr_engine.queue_ops import (
    compute_edit_diff,
    force_enqueue_not_yet_supported,
    get_by_id,
    get_last_run,
    get_next_card,
    is_scheduler_running_now,
    mark_sent,
    mark_skipped,
    update_partial_ids,
)
from sdr_engine.send import send_artifacts

load_dotenv()

# Surface staleness banner if last successful scheduler run is older than this.
STALE_RUN_HOURS = 25


def create_app(
    sqlite_path: Path | None = None,
    hubspot_api_key: str | None = None,
    hubspot_owner_id: str | None = None,
    hubspot_base_url: str = "https://api.hubapi.com",
    llm_endpoint: str | None = None,
    n8n_send_webhook_url: str | None = None,
    n8n_auth_header_value: str | None = None,
) -> Flask:
    """Build the Flask app with explicit configuration.

    Defaults come from environment so production just runs `python ui/server.py`;
    tests construct an isolated app pointing at a tmp SQLite file via this factory.
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SQLITE_PATH"] = sqlite_path or Path(
        os.path.expanduser(os.getenv("SQLITE_PATH", "~/.sdr-engine/queue.db"))
    )
    app.config["HUBSPOT_API_KEY"] = hubspot_api_key or os.getenv("HUBSPOT_API_KEY", "")
    app.config["HUBSPOT_OWNER_ID"] = hubspot_owner_id or os.getenv("HUBSPOT_OWNER_ID", "")
    app.config["HUBSPOT_BASE_URL"] = hubspot_base_url
    app.config["LLM_ENDPOINT"] = llm_endpoint or os.getenv(
        "LLM_ENDPOINT", "http://127.0.0.1:4000/v1/chat/completions"
    )
    # Actual email delivery webhook (n8n workflow with Outlook/SMTP node).
    # Without this, HubSpot logs the engagement but no email is delivered.
    app.config["N8N_SEND_EMAIL_WEBHOOK_URL"] = (
        n8n_send_webhook_url or os.getenv("N8N_SEND_EMAIL_WEBHOOK_URL", "")
    )
    app.config["N8N_AUTH_HEADER_VALUE"] = (
        n8n_auth_header_value or os.getenv("N8N_AUTH_HEADER_VALUE", "")
    )

    # ─── DB connection lifecycle (per-request) ─────────────────────
    def _get_db() -> sqlite3.Connection:
        if "db" not in g:
            conn = sqlite3.connect(app.config["SQLITE_PATH"], isolation_level=None)
            g.db = conn
        return g.db

    @app.teardown_appcontext
    def _close_db(_exc) -> None:
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def _hubspot_client() -> HubSpotClient:
        return HubSpotClient(
            api_key=app.config["HUBSPOT_API_KEY"],
            base_url=app.config["HUBSPOT_BASE_URL"],
        )

    # ─── GET /queue — state-aware card render ─────────────────────
    @app.get("/queue")
    def queue_view():
        db = _get_db()
        last_run = get_last_run(db)
        card = get_next_card(db)

        # Staleness check: was the last run successful within 25h?
        scheduler_stale = (
            last_run.age_seconds is not None
            and last_run.age_seconds > STALE_RUN_HOURS * 3600
        ) or last_run.started_at is None

        if card is None:
            # No pending cards. Distinguish caught_up_done from warming.
            if is_scheduler_running_now(db):
                state = "warming"
            else:
                state = "caught_up_done"
            return render_template(
                "queue.html",
                state=state,
                card=None,
                last_run=last_run,
                scheduler_stale=scheduler_stale,
            )

        # We have a card. State depends on its status.
        state = "send_partial" if card.status == "send_partial" else "card"
        # LLM-failure banner: draft_body starts with [LLM format failure
        llm_failed = card.draft_body.startswith("[LLM format failure")
        return render_template(
            "queue.html",
            state=state,
            card=card,
            last_run=last_run,
            scheduler_stale=scheduler_stale,
            llm_failed=llm_failed,
        )

    # ─── POST /queue/<id>/send ────────────────────────────────────
    @app.post("/queue/<int:queue_id>/send")
    def queue_send(queue_id: int):
        db = _get_db()
        card = get_by_id(db, queue_id)
        if card is None:
            abort(404, f"queue row {queue_id} not found")
        if card.status not in ("pending", "send_partial"):
            abort(409, f"row {queue_id} is {card.status}; cannot send")

        # Posted form fields:
        #   subject (chosen subject — may be the alt or freeform)
        #   body    (may be edited; auto-diff if differs from draft_body)
        chosen_subject = request.form.get("subject", card.draft_subject)
        posted_body = request.form.get("body", card.draft_body)
        edit_diff = compute_edit_diff(card.draft_body, posted_body)

        result = send_artifacts(
            artifacts=card.artifacts_for_send(),
            chosen_subject=chosen_subject,
            body=posted_body,
            owner_id=app.config["HUBSPOT_OWNER_ID"],
            contact_id=card.contact_id,
            deal_id=card.deal_id,
            company_name=card.company_name,
            contact_email=card.contact_email,
            hubspot=_hubspot_client(),
            n8n_send_webhook_url=app.config["N8N_SEND_EMAIL_WEBHOOK_URL"] or None,
            n8n_auth_header_value=app.config["N8N_AUTH_HEADER_VALUE"] or None,
            existing_engagement_id=card.hubspot_engagement_id,
            existing_note_id=card.hubspot_note_id,
            existing_task_id=card.hubspot_task_id,
        )

        if result.status == "failed":
            # Email never went out — leave the row pending so user can retry.
            return jsonify({
                "status": "failed",
                "errors": result.errors,
            }), 502

        mark_sent(
            db,
            queue_id,
            engagement_id=result.engagement_id,
            note_id=result.note_id,
            task_id=result.task_id,
            edit_diff=edit_diff,
        )

        if request.headers.get("Accept") == "application/json" or request.is_json:
            return jsonify({
                "status": result.status,
                "engagement_id": result.engagement_id,
                "note_id": result.note_id,
                "task_id": result.task_id,
                "errors": result.errors,
            })
        return redirect(url_for("queue_view"))

    # ─── POST /queue/<id>/skip ────────────────────────────────────
    @app.post("/queue/<int:queue_id>/skip")
    def queue_skip(queue_id: int):
        db = _get_db()
        card = get_by_id(db, queue_id)
        if card is None:
            abort(404, f"queue row {queue_id} not found")
        if card.status not in ("pending", "send_partial"):
            abort(409, f"row {queue_id} is {card.status}; cannot skip")
        mark_skipped(db, queue_id)
        if request.headers.get("Accept") == "application/json" or request.is_json:
            return jsonify({"status": "skipped"})
        return redirect(url_for("queue_view"))

    # ─── POST /queue/<id>/retry-note ──────────────────────────────
    @app.post("/queue/<int:queue_id>/retry-note")
    def queue_retry_note(queue_id: int):
        return _retry_step(queue_id, missing="note")

    # ─── POST /queue/<id>/retry-task ──────────────────────────────
    @app.post("/queue/<int:queue_id>/retry-task")
    def queue_retry_task(queue_id: int):
        return _retry_step(queue_id, missing="task")

    def _retry_step(queue_id: int, *, missing: str):
        """Shared helper for the two retry routes.

        Passes the existing engagement_id (and the OTHER completed id) back
        into send_artifacts, which then only fires the still-missing step.
        """
        db = _get_db()
        card = get_by_id(db, queue_id)
        if card is None:
            abort(404, f"queue row {queue_id} not found")
        if card.status != "send_partial":
            abort(409, f"row {queue_id} is {card.status}; retry only valid for send_partial")
        if missing == "note" and card.hubspot_note_id:
            abort(409, "note already exists; nothing to retry")
        if missing == "task" and card.hubspot_task_id:
            abort(409, "task already exists; nothing to retry")

        result = send_artifacts(
            artifacts=card.artifacts_for_send(),
            chosen_subject=card.draft_subject,
            body=card.draft_body,
            owner_id=app.config["HUBSPOT_OWNER_ID"],
            contact_id=card.contact_id,
            deal_id=card.deal_id,
            company_name=card.company_name,
            contact_email=card.contact_email,
            hubspot=_hubspot_client(),
            # Retry path: engagement already exists → send_artifacts skips
            # the n8n send (which already delivered) and only fires the
            # missing HubSpot writes. Still pass the webhook URL for
            # consistency in case of an edge case where engagement_id is None.
            n8n_send_webhook_url=app.config["N8N_SEND_EMAIL_WEBHOOK_URL"] or None,
            n8n_auth_header_value=app.config["N8N_AUTH_HEADER_VALUE"] or None,
            existing_engagement_id=card.hubspot_engagement_id,
            existing_note_id=card.hubspot_note_id,
            existing_task_id=card.hubspot_task_id,
        )

        update_partial_ids(db, queue_id, note_id=result.note_id, task_id=result.task_id)

        if request.headers.get("Accept") == "application/json" or request.is_json:
            return jsonify({
                "status": result.status,
                "note_id": result.note_id,
                "task_id": result.task_id,
                "errors": result.errors,
            })
        return redirect(url_for("queue_view"))

    # ─── POST /queue/force — stub until Modules 1 + 3 ─────────────
    @app.post("/queue/force")
    def queue_force():
        result = force_enqueue_not_yet_supported()
        return jsonify({
            "error": result.error,
            "needs_modules": result.needs,
        }), 501

    # ─── GET /health ──────────────────────────────────────────────
    @app.get("/health")
    def health():
        status = {
            "llm": _probe_llm(app.config["LLM_ENDPOINT"]),
            "hubspot": _probe_hubspot(app.config["HUBSPOT_API_KEY"]),
            "sqlite": _probe_sqlite(app.config["SQLITE_PATH"]),
            "last_run": None,
            "last_run_age_seconds": None,
        }
        try:
            last_run = get_last_run(_get_db())
            status["last_run"] = last_run.started_at
            status["last_run_age_seconds"] = last_run.age_seconds
        except sqlite3.Error:
            pass
        overall = "ok" if all(
            status[k] == "ok" for k in ("llm", "hubspot", "sqlite")
        ) else "degraded"
        status["overall"] = overall
        return jsonify(status)

    return app


# ─── Probes (module-level so tests can patch) ──────────────────────
def _probe_llm(endpoint: str) -> str:
    try:
        base = endpoint.rsplit("/v1/", 1)[0]
        r = requests.get(f"{base}/health", timeout=3)
        return "ok" if r.status_code < 500 else "down"
    except Exception:  # noqa: BLE001 — health probe never raises
        return "unreachable"


def _probe_hubspot(api_key: str) -> str:
    if not api_key:
        return "unconfigured"
    try:
        r = requests.get(
            "https://api.hubapi.com/account-info/v3/details",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        if r.status_code == 200:
            return "ok"
        if r.status_code == 401:
            return "unauthorized"
        return "down"
    except Exception:  # noqa: BLE001
        return "unreachable"


def _probe_sqlite(path: Path) -> str:
    if not Path(path).exists():
        return "missing"
    try:
        conn = sqlite3.connect(path)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return "ok"
    except sqlite3.OperationalError as exc:
        return "locked" if "locked" in str(exc).lower() else "error"
    except Exception:  # noqa: BLE001
        return "error"


# ─── Entry point ───────────────────────────────────────────────────
if __name__ == "__main__":
    host = os.getenv("REVIEW_UI_HOST", "127.0.0.1")
    port = int(os.getenv("REVIEW_UI_PORT", "5679"))
    if host not in {"127.0.0.1", "localhost"} and os.getenv("ALLOW_LAN_BIND") != "true":
        print(
            f"REFUSING to bind to {host}. Use 127.0.0.1 (default) and reach the UI via "
            "Cloudflare Tunnel, OR set ALLOW_LAN_BIND=true to override.",
            file=sys.stderr,
        )
        sys.exit(1)
    create_app().run(host=host, port=port, debug=False)
