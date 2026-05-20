"""Module 2.5 — ZoomInfo contact enrichment with cache-first atomicity.

The flow (per autoplan amendments A2 + A11 + the search-first economics):

    1. Cache pre-check (queue.db.enrichment_cache):
       - status='success' within TTL → reuse contact_id, 0 credits used
       - status='no_match' or 'error' within TTL → skip (don't requery)
       - status='in_progress' > 1h → assume previous run crashed, retry
       - status='orphan' → human cleanup needed; skip until resolved

    2. Search (CHEAP, no enrich credits):
       zoominfo.search_contacts(domain | name, country=US, titles=...)

    3. Local qualification gate:
       - Drop non-US (defense in depth — search already filters)
       - Drop non-target titles
       - Rank by title priority (CFO > VP Finance > Director > Controller > Risk)
       - Pick top-1 by (priority, confidence) tie-break

    4. Enrich (CREDIT-BURNING, one call):
       zoominfo.enrich_contact(top_1.contact_id) → email + full details

    5. Cache-first HubSpot writes (per A2 — write order matters):
       (a) UPSERT enrichment_cache row, status='in_progress'.
           This is the load-bearing step — if anything below crashes
           mid-way, the orphan finder can still locate the in-flight
           contact via this row.
       (b) hubspot.create_contact → contact_id
       (c) hubspot.associate_contact_to_company
       (d) hubspot.associate_contact_to_deal
       (e) UPDATE enrichment_cache → status='success'

    6. On failure at (b)-(e): rollback.
       - Attempt hubspot.delete_contact(contact_id) if (b) succeeded.
       - If delete succeeds: UPDATE cache → status='error'.
       - If delete FAILS (5xx, timeout): UPDATE cache → status='orphan'.
         Module 8's nightly sweep finds these and pages a human.

The orchestrator never silently swallows errors. Every drop reason is
recorded in the queue's `dropped` table or the cache's status field.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from sdr_engine.clients.hubspot import HubSpotClient, HubSpotError
from sdr_engine.clients.zoominfo import ContactCandidate, EnrichedContact, ZoomInfoClient

# ─── Configuration (operator-tunable) ──────────────────────────────
# Target titles by priority. First in the list = highest priority.
# Matches docs/ARCHITECTURE.md Module 2.5 spec; also configurable via env
# ZOOMINFO_TARGET_TITLES at the orchestrator boundary.
DEFAULT_TARGET_TITLES = [
    "Chief Financial Officer",
    "CFO",
    "VP Finance",
    "Director of Finance",
    "Controller",
    "Credit Manager",
    "VP Risk Management",
    "VP, Risk Management",
    "Risk Management",
]

# Title priority for tie-break (lower number = higher priority).
# Substring match against candidate.title (case-insensitive).
TITLE_PRIORITY = {
    "chief financial officer": 0,
    "cfo": 0,  # CFO and "Chief Financial Officer" tied at 0
    "vp finance": 1,
    "vp, finance": 1,
    "director of finance": 2,
    "controller": 3,
    "credit manager": 4,
    "vp risk management": 5,
    "vp, risk management": 5,
    "risk management": 5,
}

# Cache TTL in days. Matches ENRICHMENT_CACHE_TTL_DAYS env var.
DEFAULT_CACHE_TTL_DAYS = 90

# Pending-row staleness — if an 'in_progress' row is older than this,
# assume the previous run crashed and retry rather than blocking forever.
PENDING_RETRY_AFTER_HOURS = 1


@dataclass
class EnrichmentConfig:
    """Runtime configuration. Passed once at orchestrator construction."""
    target_titles: list[str]
    cache_ttl_days: int
    country_filter: str = "United States"
    enrichment_source_label_prefix: str = "ZoomInfo"

    @classmethod
    def default(cls) -> EnrichmentConfig:
        return cls(
            target_titles=list(DEFAULT_TARGET_TITLES),
            cache_ttl_days=DEFAULT_CACHE_TTL_DAYS,
        )


@dataclass
class EnrichmentResult:
    """Return shape of enrich_for_deal()."""
    success: bool
    contact_id: str | None              # HubSpot contact id on success
    cache_status: str                   # 'success' | 'no_match' | 'error' | 'orphan' | 'cached_hit' | 'cached_miss'
    reason: str | None                  # populated on non-success
    credits_used: int                   # 1 if we called enrich_contact, 0 if cache hit
    contact_email: str | None = None    # populated on success


# ─── Cache helpers ──────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(UTC)


def _read_cache(db: sqlite3.Connection, company_id: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT company_id, contact_id, source, status, confidence, matched_title, "
        "enriched_at, updated_at FROM enrichment_cache WHERE company_id = ?",
        (company_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "company_id": row[0],
        "contact_id": row[1],
        "source": row[2],
        "status": row[3],
        "confidence": row[4],
        "matched_title": row[5],
        "enriched_at": row[6],
        "updated_at": row[7],
    }


def _is_fresh(timestamp_iso: str | None, ttl_days: int) -> bool:
    """True if `timestamp_iso` is within ttl_days of now. False on null/parse error."""
    if not timestamp_iso:
        return False
    try:
        # SQLite stores in 'YYYY-MM-DD HH:MM:SS' or ISO. tolerate both.
        ts = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (ValueError, AttributeError):
        return False
    return (_utcnow() - ts) < timedelta(days=ttl_days)


def _is_stale_in_progress(timestamp_iso: str | None) -> bool:
    """True if an in_progress row is old enough to assume crash + retry."""
    if not timestamp_iso:
        return True
    try:
        ts = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (ValueError, AttributeError):
        return True
    return (_utcnow() - ts) > timedelta(hours=PENDING_RETRY_AFTER_HOURS)


def _upsert_cache(
    db: sqlite3.Connection,
    company_id: str,
    contact_id: str | None,
    status: str,
    confidence: float | None,
    matched_title: str | None,
) -> None:
    """Insert or update enrichment_cache row. Always bumps updated_at."""
    now_iso = _utcnow().isoformat(timespec="seconds")
    db.execute(
        """
        INSERT INTO enrichment_cache (company_id, contact_id, source, status, confidence,
                                       matched_title, enriched_at, updated_at)
        VALUES (?, ?, 'zoominfo', ?, ?, ?, ?, ?)
        ON CONFLICT(company_id) DO UPDATE SET
            contact_id = excluded.contact_id,
            status = excluded.status,
            confidence = excluded.confidence,
            matched_title = excluded.matched_title,
            updated_at = excluded.updated_at
        """,
        (company_id, contact_id, status, confidence, matched_title, now_iso, now_iso),
    )


# ─── Qualification gate ─────────────────────────────────────────────
def _title_priority(title: str, target_titles: list[str]) -> int | None:
    """Return tie-break priority (0 = highest, 5 = lowest) if title matches a
    target. None means the title isn't in the target list at all.
    """
    title_lower = title.lower()
    # First check the explicit priority map (substring match)
    for key, priority in TITLE_PRIORITY.items():
        if key in title_lower:
            return priority
    # Otherwise check if it loosely matches any target title
    for tgt in target_titles:
        if tgt.lower() in title_lower or title_lower in tgt.lower():
            return 6  # matches a target but isn't in the priority map; lowest rank
    return None


def qualify_candidates(
    candidates: list[ContactCandidate],
    config: EnrichmentConfig,
) -> ContactCandidate | None:
    """Apply the qualification gate. Returns the chosen candidate or None."""
    qualified: list[tuple[int, float, ContactCandidate]] = []
    for c in candidates:
        # Defense in depth: search should already filter by country, but
        # if it doesn't (e.g., legacy data quality), drop here.
        if c.country and c.country.lower() != config.country_filter.lower():
            continue
        priority = _title_priority(c.title, config.target_titles)
        if priority is None:
            continue
        qualified.append((priority, -c.confidence, c))  # negate confidence for asc sort
    if not qualified:
        return None
    qualified.sort()  # priority asc, then confidence desc (via negation)
    return qualified[0][2]


# ─── Orchestrator ───────────────────────────────────────────────────
def enrich_for_deal(
    *,
    company_id: str,
    company_domain: str | None,
    company_name: str | None,
    deal_id: str,
    db: sqlite3.Connection,
    zoominfo: ZoomInfoClient,
    hubspot: HubSpotClient,
    config: EnrichmentConfig | None = None,
) -> EnrichmentResult:
    """Run the full Module 2.5 pipeline for one deal lacking a contact.

    On success, the contact exists in HubSpot, is associated to both the
    company and the deal, and the enrichment_cache row is 'success'.

    On any failure during the HubSpot writes, the orchestrator attempts
    rollback (delete the contact). If rollback fails too, the cache row
    is marked 'orphan' so Module 8's nightly sweep can find it.
    """
    config = config or EnrichmentConfig.default()

    # ─── Step 1: cache pre-check ──────────────────────────────────
    cached = _read_cache(db, company_id)
    if cached:
        status = cached["status"]
        if status == "success" and _is_fresh(cached["updated_at"], config.cache_ttl_days):
            return EnrichmentResult(
                success=True,
                contact_id=cached["contact_id"],
                cache_status="cached_hit",
                reason=None,
                credits_used=0,
            )
        if status in {"no_match", "error"} and _is_fresh(cached["updated_at"], config.cache_ttl_days):
            return EnrichmentResult(
                success=False,
                contact_id=None,
                cache_status="cached_miss",
                reason=f"cache says prior {status} within TTL — not re-querying",
                credits_used=0,
            )
        if status == "in_progress" and not _is_stale_in_progress(cached["updated_at"]):
            return EnrichmentResult(
                success=False,
                contact_id=None,
                cache_status="in_progress",
                reason="another process is already enriching this company (in_progress < 1h)",
                credits_used=0,
            )
        if status == "orphan":
            return EnrichmentResult(
                success=False,
                contact_id=None,
                cache_status="orphan",
                reason="cache marked orphan — human cleanup required before retry",
                credits_used=0,
            )
        # status='in_progress' AND stale → fall through and retry

    # ─── Step 2: search (cheap, no credits burned) ────────────────
    try:
        candidates = zoominfo.search_contacts(
            company_domain=company_domain,
            company_name=company_name,
            job_titles=config.target_titles,
            country=config.country_filter,
        )
    except requests.RequestException as exc:
        _upsert_cache(db, company_id, None, "error", None, None)
        return EnrichmentResult(
            success=False,
            contact_id=None,
            cache_status="error",
            reason=f"zoominfo search failed: {exc}",
            credits_used=0,
        )

    # ─── Step 3: local qualification gate ──────────────────────────
    chosen = qualify_candidates(candidates, config)
    if chosen is None:
        _upsert_cache(db, company_id, None, "no_match", None, None)
        return EnrichmentResult(
            success=False,
            contact_id=None,
            cache_status="no_match",
            reason="no_qualified_contact_at_company",
            credits_used=0,
        )

    # ─── Step 4: enrich the chosen candidate (BURNS A CREDIT) ─────
    try:
        enriched: EnrichedContact | None = zoominfo.enrich_contact(chosen.contact_id)
    except requests.RequestException as exc:
        _upsert_cache(db, company_id, None, "error", chosen.confidence, chosen.title)
        return EnrichmentResult(
            success=False,
            contact_id=None,
            cache_status="error",
            reason=f"zoominfo enrich failed: {exc}",
            credits_used=1,  # we attempted the enrich; ZoomInfo may or may not have charged
        )
    if enriched is None or not enriched.email:
        _upsert_cache(db, company_id, None, "no_match", chosen.confidence, chosen.title)
        return EnrichmentResult(
            success=False,
            contact_id=None,
            cache_status="no_match",
            reason="enrich returned no email for chosen candidate",
            credits_used=1,
        )

    # ─── Step 5: cache-first atomic HubSpot writes (A2) ────────────
    # (a) Mark cache as in_progress BEFORE any HubSpot writes.
    _upsert_cache(db, company_id, None, "in_progress", enriched.confidence, enriched.title)

    enrichment_label = (
        f"{config.enrichment_source_label_prefix} {_utcnow().date().isoformat()}"
    )
    created_contact_id: str | None = None

    try:
        # (b) create contact
        contact = hubspot.create_contact(
            email=enriched.email,
            first_name=enriched.first_name,
            last_name=enriched.last_name,
            job_title=enriched.title,
            enrichment_source=enrichment_label,
            enrichment_confidence=enriched.confidence,
        )
        created_contact_id = contact.contact_id

        # (c) associate contact ↔ company
        hubspot.associate_contact_to_company(created_contact_id, company_id)

        # (d) associate contact ↔ deal
        hubspot.associate_contact_to_deal(created_contact_id, deal_id)

        # (e) finalize cache
        _upsert_cache(db, company_id, created_contact_id, "success", enriched.confidence, enriched.title)
        return EnrichmentResult(
            success=True,
            contact_id=created_contact_id,
            cache_status="success",
            reason=None,
            credits_used=1,
            contact_email=enriched.email,
        )
    except (HubSpotError, requests.RequestException) as exc:
        # Rollback path. The cache row is currently in_progress (or has
        # a stale contact_id from a prior crash). Try to delete the
        # contact if we created it; mark cache 'error' on success or
        # 'orphan' if the delete itself fails.
        rollback_status = "error"
        if created_contact_id is not None:
            try:
                hubspot.delete_contact(created_contact_id)
            except (HubSpotError, requests.RequestException):
                rollback_status = "orphan"
                # Keep the contact_id in the orphan row so the sweep can find it.
                _upsert_cache(
                    db, company_id, created_contact_id, "orphan",
                    enriched.confidence, enriched.title,
                )
                return EnrichmentResult(
                    success=False,
                    contact_id=created_contact_id,
                    cache_status="orphan",
                    reason=f"rollback failed; orphan contact in HubSpot: {exc}",
                    credits_used=1,
                )
        _upsert_cache(db, company_id, None, rollback_status, enriched.confidence, enriched.title)
        return EnrichmentResult(
            success=False,
            contact_id=None,
            cache_status=rollback_status,
            reason=f"hubspot write failed: {exc}",
            credits_used=1,
        )
