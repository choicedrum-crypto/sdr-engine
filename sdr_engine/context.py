"""Module 3 — gather per-deal context for the LLM drafter.

The scheduler (Module 1) calls gather_context_for_deal() once per deal
after compliance + cooldown checks pass. The returned dict is then
passed verbatim to sdr_engine.llm.draft() as the `inputs` arg.

MVP scope (this file):
  - Pick the right prospect_type from funnel_type via priority order
  - Pick the contact to send to (primary > opted-in > zoominfo if missing)
  - Truncate engagement bodies for prompt-budget control
  - Render the year/word-formatted quote_date
  - Compose anchor + hook placeholders that mean "we don't know yet"

Deferred to follow-up PRs (architecture's full Module 3 spec):
  - Relationship anchor extraction from broker rosters (config/tcia-brokers.json)
  - OneDrive / SharePoint policy excerpt fetch + .docx/.pdf parsing
  - Current-event hook detection (M&A signals, leadership changes, sector stress)

When the deferred items land, this module's gather_context_for_deal()
signature stays the same — only the implementation gets richer. The LLM
template already tolerates anchor_type='none' / hook_source='none' by
dropping the relationship paragraph and falling back to a generic value
touch, so the MVP doesn't produce broken drafts; it produces less-anchored
drafts that the SDR can polish in Module 6's textarea.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sdr_engine.clients.hubspot import HubSpotClient, HubSpotDeal, HubSpotEngagement

# Priority order matching the operational template's intent — the warmest
# relationship framing wins on multi-tagged deals. Matches docs/ARCHITECTURE.md
# Module 1 → "Prospect-type classification" section.
TYPE_PRIORITY = [
    "former_client",
    "lost_opportunity",
    "bor_target",
    "coi_referral",
    "bankruptcy_trigger",
    "industry_trigger",
    "conference",
]
KNOWN_TYPES = set(TYPE_PRIORITY)


@dataclass
class ContextResult:
    """What scheduler gets back. `drop_reason` populated when this deal
    can't be enqueued; `inputs` populated otherwise."""
    inputs: dict[str, Any] | None
    drop_reason: str | None
    contact_id: str | None = None
    company_id: str | None = None


def pick_prospect_type(funnel_types_parsed: list[str]) -> str | None:
    """Pick the warmest known type from a multi-checkbox value.

    Returns None if no value is in the known taxonomy — caller drops the
    deal as `missing_prospect_type` per A11 (recoverable: re-eligible
    next run if user fixes the property).
    """
    known = [t for t in funnel_types_parsed if t in KNOWN_TYPES]
    if not known:
        return None
    # Sort by priority (lower index = higher priority); ties impossible
    # because TYPE_PRIORITY is a list with unique values.
    return min(known, key=TYPE_PRIORITY.index)


def humanize_quote_date(createdate_iso: str | None) -> str:
    """Convert a HubSpot createdate ISO to 'March 2024'-style format
    for the LLM. The operational prompt expects month-year, not full ISO.
    """
    if not createdate_iso:
        return ""
    try:
        ts = datetime.fromisoformat(createdate_iso.replace("Z", "+00:00"))
        return ts.strftime("%B %Y")
    except ValueError:
        return ""


def derive_company_acronym(company_name: str) -> str:
    """Heuristic company acronym for the email body's natural-language
    shorthand. 'Advance Polybag' → 'AP'. 'TCIA' (already acronym-like) → 'TCIA'.

    Skip generic suffixes like 'Inc', 'LLC', 'Corp'. Skip articles
    ('The', 'A'). 2-4 letters typical.
    """
    SUFFIX_DROPS = {
        "inc", "inc.", "llc", "llc.", "corp", "corp.", "corporation",
        "co", "co.", "company", "ltd", "ltd.", "plc", "limited",
    }
    ARTICLE_DROPS = {"the", "a", "an"}

    if not company_name:
        return ""
    # If already short + uppercase-ish, treat as already acronym
    if len(company_name) <= 5 and company_name.isupper():
        return company_name

    words = [w for w in company_name.split() if w.lower() not in ARTICLE_DROPS]
    words = [w for w in words if w.lower().rstrip(",.") not in SUFFIX_DROPS]
    if not words:
        return company_name[:4]
    return "".join(w[0].upper() for w in words)


def gather_context_for_deal(
    deal: HubSpotDeal,
    hubspot: HubSpotClient,
    *,
    company_name: str = "",
    company_industry: str = "",
    contact_first_name: str = "",
    contact_last_name: str = "",
    contact_email: str = "",
    contact_source: str = "existing",
    product_line: str = "",
    fetch_engagements: bool = True,
) -> ContextResult:
    """Build the `inputs` dict that sdr_engine.llm.draft() expects.

    The scheduler resolves the contact first (associations → primary > opt-in)
    and the company first (associations[0]) before calling here. This
    function then:
      - Maps funnel_type → prospect_type via priority
      - Sets send_reason from renewal_date presence
      - Pulls last engagement bodies (one HubSpot API call per deal)
      - Returns the inputs dict OR drops the deal with a reason
    """
    prospect_type = pick_prospect_type(deal.funnel_types_parsed)
    if prospect_type is None:
        return ContextResult(inputs=None, drop_reason="missing_prospect_type")

    if not contact_email:
        return ContextResult(inputs=None, drop_reason="no_valid_contact_after_enrichment")

    # send_reason: 'renewal' if we know the renewal date (sharp bucket), else round-robin
    send_reason = "renewal" if deal.renewal_date else "round-robin"

    # Engagement bodies for downstream anchor/hook detection (deferred to
    # follow-up PRs). Currently passed as raw text on the inputs dict only
    # for diagnostics — the operational prompt doesn't reference them.
    engagement_excerpts: list[str] = []
    if fetch_engagements:
        try:
            engagements: list[HubSpotEngagement] = hubspot.fetch_deal_engagements(
                deal.deal_id, limit=3, body_char_cap=200,
            )
            engagement_excerpts = [e.body for e in engagements if e.body.strip()]
        except Exception:  # noqa: BLE001 — context-gather must not fail the run
            # Failing to fetch engagements is non-fatal; the LLM tolerates
            # missing anchor/hook by falling back to generic value-touch.
            engagement_excerpts = []

    return ContextResult(
        inputs={
            "prospect_type": prospect_type,
            "send_reason": send_reason,
            "company_name": company_name or deal.dealname,
            "company_acronym": derive_company_acronym(company_name or deal.dealname),
            "company_industry": company_industry,
            "contact_first_name": contact_first_name,
            "contact_last_name": contact_last_name,
            "contact_source": contact_source,
            "quote_amount": deal.amount or "",
            "product_line": product_line,
            "quote_date_human": humanize_quote_date(deal.createdate),
            "renewal_date_or_null": deal.renewal_date,
            "policy_excerpt_or_null": None,    # MVP — OneDrive integration deferred
            "hook_text": "",                    # MVP — current-event hook deferred
            "hook_source": "none",
            "anchor_type": "none",              # MVP — broker-roster matching deferred
            "anchor_name": "",
            "anchor_context": "\n\n".join(engagement_excerpts) if engagement_excerpts else None,
        },
        drop_reason=None,
        contact_id=None,  # scheduler tracks these separately
        company_id=None,
    )
