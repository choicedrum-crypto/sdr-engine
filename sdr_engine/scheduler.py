"""Module 1 — the daily scheduler loop.

Invoked once per working day by n8n's cron. Pages through the HubSpot
Prospecting pipeline, classifies + filters + buckets each deal, drafts
the artifacts via Module 4, and inserts queue rows for Module 6's UI
to surface to the SDR.

The flow per deal:

    1. Funnel-type classification → pick the warmest prospect_type
    2. Resolve contact (primary > opted-in by recency)
    3. Compliance filter (opt-out / bounced / quarantined → drop)
    4. Cooldown checks (active queue row / skip / dropped — A11)
    5. Bucket assignment:
       - Sharp: renewal_date - 60 days falls in next 7 days
       - Round-robin: md5(deal_id) % working_days_per_year matches today's index
       - Neither: skip until later in the year
    6. Daily rate cap (DAILY_SEND_CAP; sharp prioritized over round-robin
       when over cap — round-robin spillover defers to tomorrow)
    7. Gather context (Module 3 MVP — deal info + engagement excerpts)
    8. Draft via Module 4 (local LLM with cloud fallback per A15)
    9. Insert pending card into queue.db

Each step that drops a deal records a row in the `dropped` table with
a reason — both for ops visibility (Module 8 sweeps + weekly digest)
and so re-eligibility logic respects past decisions.

The whole run is wrapped in `RunHeartbeat`, which writes a `runs` row
that the UI reads to detect 'last scheduler run > 25h old' staleness.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sdr_engine.clients.hubspot import HubSpotClient, HubSpotDeal
from sdr_engine.context import gather_context_for_deal
from sdr_engine.llm import LLMResult, draft
from sdr_engine.monitoring import RunHeartbeat, notify_openclaw
from sdr_engine.queue_ops import (
    has_active_or_cooldown_row,
    insert_dropped,
    insert_pending_card,
)

# ─── Constants (operator-tunable via env at the entry point) ────────
RENEWAL_LEAD_DAYS_DEFAULT = 60        # sharp bucket: enqueue 60 days before renewal
SHARP_WINDOW_DAYS_DEFAULT = 7         # how far ahead to look for sharp-bucket cards
DAILY_SEND_CAP_DEFAULT = 3
WORKING_DAYS_PER_YEAR_DEFAULT = 250
LAST_CONTACTED_QUIET_DAYS = 90        # HubSpot-side filter

# Compliance hard-stops on contact properties — never bypassable, not even by /force
HARD_COMPLIANCE_FAILS = frozenset({
    "opted_out", "bad_email", "quarantined",
})


@dataclass
class ScheduleConfig:
    """Runtime configuration for one scheduler run."""
    pipeline_id: str
    funnel_type_property: str = "funnel_type"
    daily_send_cap: int = DAILY_SEND_CAP_DEFAULT
    working_days_per_year: int = WORKING_DAYS_PER_YEAR_DEFAULT
    renewal_lead_days: int = RENEWAL_LEAD_DAYS_DEFAULT
    sharp_window_days: int = SHARP_WINDOW_DAYS_DEFAULT
    last_contacted_quiet_days: int = LAST_CONTACTED_QUIET_DAYS
    holiday_file: Path | None = None
    today: date | None = None              # injectable for tests; defaults to now
    llm_endpoint: str | None = None
    llm_primary_model: str = "local-main"
    llm_fallback_model: str = "heavy-main"
    llm_timeout_seconds: int = 180


@dataclass
class ScheduleResult:
    """Return shape of run_scheduler() — exposes the per-bucket counts
    for the n8n workflow's logging + the OpenClaw digest."""
    candidates_examined: int = 0
    enqueued: int = 0
    enqueued_sharp: int = 0
    enqueued_round_robin: int = 0
    dropped: int = 0
    dropped_by_reason: dict[str, int] = field(default_factory=dict)
    deferred_overflow: int = 0      # round-robin slots that lost to rate cap
    llm_failures: int = 0           # NEEDS_HUMAN drafts produced
    errors: list[str] = field(default_factory=list)


# ─── Working-day index ──────────────────────────────────────────────
def _load_holidays(holiday_file: Path | None) -> set[date]:
    """Return the set of dates the scheduler treats as non-working.

    config/holidays.json shape: {"holidays": [{"date": "2026-01-01", ...}, ...]}
    """
    if holiday_file is None or not holiday_file.exists():
        return set()
    payload = json.loads(holiday_file.read_text(encoding="utf-8"))
    out: set[date] = set()
    for h in payload.get("holidays", []):
        try:
            out.add(date.fromisoformat(h["date"]))
        except (KeyError, ValueError):
            continue
    return out


def working_day_index(today_local: date, holidays: set[date]) -> int:
    """Return 0-based count of working days from Jan 1 of `today_local`'s
    year up to (and including) today, skipping weekends + holidays.

    Used as the modulo dial for round-robin slot allocation: a deal's
    md5-hashed slot in [0, working_days_per_year) is enqueued only when
    today's index matches its slot.
    """
    start = date(today_local.year, 1, 1)
    count = 0
    cursor = start
    while cursor <= today_local:
        if cursor.weekday() < 5 and cursor not in holidays:
            if cursor == today_local:
                return count
            count += 1
        cursor += timedelta(days=1)
    return count


def round_robin_slot(deal_id: str, working_days_per_year: int) -> int:
    """Deterministic slot in [0, working_days_per_year) for one deal.

    md5 + modulo gives a stable assignment so the same deal lands on the
    same working day year after year. Re-hashing would break the
    year-long pacing guarantee — see Open Q #2 (resolved) in the architecture.
    """
    h = hashlib.md5(deal_id.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % working_days_per_year


# ─── Sharp-bucket detection ─────────────────────────────────────────
def in_sharp_window(
    renewal_date_iso: str | None,
    today_local: date,
    *,
    lead_days: int,
    window_days: int,
) -> bool:
    """True if (renewal - lead_days) falls in [today, today + window_days].

    Means: this deal's "60-day renewal warning shot" target send date is
    within the next 7 working days. Enqueue now so the SDR sees it in
    time to act.
    """
    if not renewal_date_iso:
        return False
    try:
        renewal_dt = date.fromisoformat(renewal_date_iso[:10])
    except (ValueError, IndexError):
        return False
    target_send_date = renewal_dt - timedelta(days=lead_days)
    return today_local <= target_send_date <= today_local + timedelta(days=window_days)


# ─── Contact resolution + compliance ────────────────────────────────
@dataclass
class _ContactPick:
    """What scheduler needs from the chosen contact."""
    contact_id: str
    email: str
    first_name: str
    last_name: str


def _pick_contact_from_deal(
    deal: HubSpotDeal,
    hubspot: HubSpotClient,
) -> tuple[_ContactPick | None, str | None]:
    """Resolve which contact gets the email. Returns (pick, drop_reason).

    Priority order:
      1. First associated contact (HubSpot's first result usually IS the
         primary; v3 associations don't expose 'primary' flag reliably,
         so we trust position)
      2. Any associated contact with non-empty email + no opt-out flags
      3. None → drop with the most specific reason observed

    Drop reasons are constrained to the queue.dropped table's CHECK enum:
      no_valid_contact_after_enrichment (default — no contacts at all,
        or all contacts had no email)
      opted_out (at least one contact opted out and no other contact passed)
      bad_email (at least one bounced)
      quarantined (at least one quarantined)

    The most-specific applicable failure wins so the dropped row matches
    Module 8's reason-aware cooldown logic.

    MVP: fetches associated contacts one at a time. Module 1 typically
    examines 5-30 deals per run, so per-deal contact fetch is fine.
    Future optimization: batch-fetch contacts via /crm/v3/objects/contacts/batch/read.
    """
    if not deal.contact_ids:
        return None, "no_valid_contact_after_enrichment"

    saw_optout = False
    saw_bad_email = False
    saw_quarantined = False

    import requests
    for contact_id in deal.contact_ids:
        try:
            r = requests.get(
                f"{hubspot.base_url}/crm/v3/objects/contacts/{contact_id}"
                "?properties=email,firstname,lastname,hs_email_optout,"
                "hs_email_bad_address,hs_email_quarantined",
                headers=hubspot._headers(),  # noqa: SLF001 — intentional reach into client
                timeout=hubspot.timeout,
            )
        except Exception:  # noqa: BLE001 — one bad contact fetch shouldn't abort the deal
            continue
        if r.status_code != 200:
            continue
        props = r.json().get("properties", {})

        # Compliance filter: hard fails per A14
        if (props.get("hs_email_optout") or "").lower() == "true":
            saw_optout = True
            continue
        if (props.get("hs_email_bad_address") or "").lower() == "true":
            saw_bad_email = True
            continue
        if (props.get("hs_email_quarantined") or "").lower() == "true":
            saw_quarantined = True
            continue

        email = (props.get("email") or "").strip()
        if not email:
            continue

        return _ContactPick(
            contact_id=str(contact_id),
            email=email,
            first_name=props.get("firstname") or "",
            last_name=props.get("lastname") or "",
        ), None

    # Pick the most specific reason: compliance hard-fails take precedence
    # so future re-eligibility logic respects the actual compliance state.
    if saw_optout:
        return None, "opted_out"
    if saw_quarantined:
        return None, "quarantined"
    if saw_bad_email:
        return None, "bad_email"
    return None, "no_valid_contact_after_enrichment"


# ─── Main loop ──────────────────────────────────────────────────────
def run_scheduler(
    *,
    db: sqlite3.Connection,
    hubspot: HubSpotClient,
    prompt_path: Path,
    ctas_path: Path,
    config: ScheduleConfig,
    webhook_url: str | None = None,
) -> ScheduleResult:
    """Page through Prospecting deals, draft + enqueue the ones that pass
    all filters. Wraps the whole thing in a RunHeartbeat row.

    Drafting goes through sdr_engine.llm.draft() which handles the local-
    first-then-cloud-fallback retry cascade — this scheduler treats a
    NEEDS_HUMAN result as still enqueueable (the row is created with
    placeholder body + a banner-triggering marker; Module 6 surfaces
    the hand-write prompt to the SDR).
    """
    result = ScheduleResult()
    today_local = config.today or date.today()
    holidays = _load_holidays(config.holiday_file)
    today_idx = working_day_index(today_local, holidays)

    # Compute the HubSpot-side last-contacted cutoff (90-day quiet period).
    cutoff_dt = datetime.now(UTC) - timedelta(days=config.last_contacted_quiet_days)
    cutoff_ms = int(cutoff_dt.timestamp() * 1000)

    with RunHeartbeat(db) as run_record:
        # Sharp-bucket deals enqueue first, then round-robin until rate cap.
        sharp_cards_inserted = 0
        round_robin_cards_inserted = 0

        after_cursor: str | None = None
        while True:
            try:
                deals, after_cursor = hubspot.search_deals_in_pipeline(
                    config.pipeline_id,
                    funnel_type_property=config.funnel_type_property,
                    last_contacted_cutoff_ms=cutoff_ms,
                    after=after_cursor,
                )
            except Exception as exc:  # noqa: BLE001 — log error, end run
                run_record.record_error("scheduler.search", str(exc))
                result.errors.append(f"search failed: {exc}")
                notify_openclaw(webhook_url, "scheduler_search_failed", {"error": str(exc)})
                break

            for deal in deals:
                result.candidates_examined += 1
                if _process_one_deal(
                    deal=deal,
                    db=db,
                    hubspot=hubspot,
                    prompt_path=prompt_path,
                    ctas_path=ctas_path,
                    config=config,
                    today_idx=today_idx,
                    sharp_cards_inserted=sharp_cards_inserted,
                    round_robin_cards_inserted=round_robin_cards_inserted,
                    result=result,
                ):
                    # Inner function returned the bucket it inserted into;
                    # we increment counters here so the rate cap applies
                    # across the paginated loop.
                    if result.enqueued_sharp > sharp_cards_inserted:
                        sharp_cards_inserted = result.enqueued_sharp
                    if result.enqueued_round_robin > round_robin_cards_inserted:
                        round_robin_cards_inserted = result.enqueued_round_robin

            if not after_cursor:
                break

        run_record.enqueued_count = result.enqueued
        run_record.dropped_count = result.dropped

    return result


def _process_one_deal(
    *,
    deal: HubSpotDeal,
    db: sqlite3.Connection,
    hubspot: HubSpotClient,
    prompt_path: Path,
    ctas_path: Path,
    config: ScheduleConfig,
    today_idx: int,
    sharp_cards_inserted: int,
    round_robin_cards_inserted: int,
    result: ScheduleResult,
) -> bool:
    """Run one deal through every filter. Returns True if enqueued.

    Mutates `result` for counters + dropped reasons. Inserts into the
    `dropped` table for any deal that fails a filter.
    """
    # ─── Cooldown check (cheap, do first to skip expensive contact fetch)
    blocked, cooldown_reason = has_active_or_cooldown_row(db, deal.deal_id)
    if blocked:
        # Active in-flight or recent skip is normal — not a "dropped" event.
        # 90-day dropped-cooldown IS a dropped event but already in the
        # table, so no need to re-insert. Just skip silently.
        result.dropped += 1
        result.dropped_by_reason[cooldown_reason or "cooldown"] = (
            result.dropped_by_reason.get(cooldown_reason or "cooldown", 0) + 1
        )
        return False

    # ─── Bucket classification ──────────────────────────────────────
    is_sharp = in_sharp_window(
        deal.renewal_date,
        config.today or date.today(),
        lead_days=config.renewal_lead_days,
        window_days=config.sharp_window_days,
    )
    if is_sharp:
        bucket = "sharp"
    elif round_robin_slot(deal.deal_id, config.working_days_per_year) == today_idx:
        bucket = "round-robin"
    else:
        # Deal's round-robin slot isn't today's index → not this deal's day.
        # Don't increment dropped — this is expected non-action; majority of
        # deals fall here every day.
        return False

    # ─── Rate cap ────────────────────────────────────────────────────
    # Sharp always wins; round-robin overflows defer to tomorrow.
    total_inserted = sharp_cards_inserted + round_robin_cards_inserted
    if total_inserted >= config.daily_send_cap:
        if bucket == "round-robin":
            result.deferred_overflow += 1
            return False
        # Sharp bucket exceeds cap → still enqueue (sharp has priority
        # and shouldn't be deferred — renewal windows are time-sensitive).
        # Round-robin spillover happens naturally on subsequent runs.

    # ─── Contact resolution + compliance ────────────────────────────
    contact, contact_drop_reason = _pick_contact_from_deal(deal, hubspot)
    if contact is None:
        insert_dropped(db, deal.deal_id, contact_drop_reason or "no_compliant_contact")
        result.dropped += 1
        key = contact_drop_reason or "no_compliant_contact"
        result.dropped_by_reason[key] = result.dropped_by_reason.get(key, 0) + 1
        return False

    # ─── Module 3 context-gather ────────────────────────────────────
    ctx = gather_context_for_deal(
        deal,
        hubspot,
        company_name=deal.dealname,  # best available; future: fetch company.name
        contact_first_name=contact.first_name,
        contact_last_name=contact.last_name,
        contact_email=contact.email,
        contact_source="existing",   # Module 2.5 enrichment integration deferred
    )
    if ctx.drop_reason is not None:
        insert_dropped(db, deal.deal_id, ctx.drop_reason)
        result.dropped += 1
        result.dropped_by_reason[ctx.drop_reason] = (
            result.dropped_by_reason.get(ctx.drop_reason, 0) + 1
        )
        return False

    # ─── Module 4 — draft the three artifacts ───────────────────────
    inputs = ctx.inputs or {}
    try:
        draft_result: LLMResult = draft(
            inputs=inputs,
            prompt_path=prompt_path,
            ctas_path=ctas_path,
            endpoint=config.llm_endpoint or "http://127.0.0.1:4000/v1/chat/completions",
            primary_model=config.llm_primary_model,
            fallback_model=config.llm_fallback_model,
            timeout=config.llm_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — record + skip; don't crash the whole run
        result.errors.append(f"draft failed for deal {deal.deal_id}: {exc}")
        return False

    if not draft_result.success:
        # NEEDS_HUMAN — still enqueue so the SDR sees the deal in the UI
        # with the LLM-FAILURE banner. Module 6 already handles this state.
        result.llm_failures += 1

    artifacts = draft_result.artifacts
    email = artifacts.get("email") or {}
    subjects = email.get("subject_options") or ["", ""]
    if len(subjects) < 2:
        subjects = subjects + [""] * (2 - len(subjects))

    # ─── Insert the row ─────────────────────────────────────────────
    company_id = deal.company_ids[0] if deal.company_ids else ""
    insert_pending_card(
        db,
        deal_id=deal.deal_id,
        company_id=company_id,
        company_name=inputs.get("company_name") or deal.dealname,
        contact_id=contact.contact_id,
        contact_email=contact.email,
        contact_first_name=contact.first_name or None,
        contact_last_name=contact.last_name or None,
        send_reason=inputs.get("send_reason") or "round-robin",
        prospect_type=inputs.get("prospect_type") or "former_client",
        funnel_types_all=deal.funnel_types_parsed,
        contact_source=inputs.get("contact_source") or "existing",
        quote_amount=deal.amount,
        product_line=inputs.get("product_line") or None,
        quote_date_human=inputs.get("quote_date_human") or None,
        renewal_date=deal.renewal_date,
        policy_excerpt=inputs.get("policy_excerpt_or_null"),
        hook_text=inputs.get("hook_text") or None,
        hook_source=inputs.get("hook_source") or None,
        draft_subject=subjects[0] or "",
        draft_subject_alt=subjects[1] or None,
        draft_body=email.get("body", "") if draft_result.success else "[LLM format failure — please hand-write]",
        cta_chosen=email.get("cta_chosen") or None,
        deal_note_body=artifacts.get("deal_note", "") or "",
        call_script_json=json.dumps(artifacts.get("call_script", {})),
        anchor_used=(artifacts.get("metadata") or {}).get("anchor_used"),
        hook_used=(artifacts.get("metadata") or {}).get("hook_used"),
        llm_model_used=draft_result.model_used,
        llm_attempts=draft_result.attempts,
    )

    result.enqueued += 1
    if bucket == "sharp":
        result.enqueued_sharp += 1
    else:
        result.enqueued_round_robin += 1
    return True
