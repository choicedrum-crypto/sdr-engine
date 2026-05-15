"""Insert one test queue row for the end-to-end smoke test.

Sane defaults so a minimal invocation works:
    python scripts/insert_test_card.py --contact-email you@gmail.com

Customizable defaults for the smoke test:
    python scripts/insert_test_card.py \\
        --contact-email you@gmail.com \\
        --company-name "Test Co Inc" \\
        --deal-id "smoke-test-001" \\
        --hubspot-deal-id "REAL_HUBSPOT_DEAL_ID" \\
        --hubspot-contact-id "REAL_HUBSPOT_CONTACT_ID" \\
        --hubspot-company-id "REAL_HUBSPOT_COMPANY_ID"

The HubSpot IDs are what Module 7 will use for the email engagement +
associations. Use a test deal in HubSpot's Prospecting pipeline so the
email/note/task actually land somewhere safe. Without --hubspot-* args,
defaults are placeholders that WILL fail at Module 7 step time — fine
for testing the UI render path but not the full Send flow.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


DEFAULT_BODY = """Dear {first_name},

It has been a while since we last connected on trade credit options for {company}. I was reviewing some recent industry signals and your company came to mind. With Q1 sector default rates moving the way they have, I wanted to flag this as a moment worth a brief conversation.

We have access to the full panel — Allianz (formerly Euler Hermes), Atradius, Coface, FCIA, AIG, plus specialty markets. Leveraging every single carrier and AR resource means more competition and lower rates than just comparing two or three options.

We also run aggregated AR analysis monthly or quarterly for active clients to keep coverage and policy compliance up to date — happy to share what that looks like.

Happy to run a quick comparison if you ever want a second look at your current setup — no obligation.

Best regards,
Daniel Bradley"""


DEFAULT_CALL_SCRIPT = {
    "opening": "Hi {first_name}, this is Daniel Bradley from TCIA — we're a specialty brokerage focused on trade credit and AR protection.",
    "context_bridge": "I was looking at recent default-rate signals in your sector and wanted to follow up on the conversation we had a while back.",
    "observation": "Wholesale packaging default rates are up 14% YoY per Atradius's Q1 country report.",
    "open_question": "How are you thinking about carrier diversification right now versus your current setup?",
    "objection_bridges": {
        "already_covered": "Understood — keep us on your shortlist for the next renewal review.",
        "not_interested": "No worries — happy to share our quarterly AR analysis if it's ever useful.",
        "send_info": "I'll send a one-pager on our carrier panel today, focused on what's relevant to your sector.",
    },
}


DEFAULT_DEAL_NOTE = """SDR Brief — Smoke Test
Prospect Type: Former Client
Company: {company} | Industry: Manufacturing
Known Contact: {first_name} | Decision-Maker Gap: title unknown
INTEL:
  - Q1 sector default rates trending up per Atradius
  - Prior TCIA work with this company
  - Industry: flexible packaging — credit-exposed via thin-margin wholesalers
Recommended Approach: Lead with sector-stress observation, soft renewal-comparison CTA."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contact-email", required=True, help="Send target — use your personal email for smoke test")
    parser.add_argument("--first-name", default="Daniel")
    parser.add_argument("--last-name", default="Bradley")
    parser.add_argument("--company-name", default="Test Co Inc")
    parser.add_argument("--deal-id", default="smoke-test-001",
                        help="Local deal_id (internal — not a HubSpot ID)")
    parser.add_argument("--hubspot-deal-id", default="placeholder-deal",
                        help="REAL HubSpot deal ID — required for Module 7 to associate")
    parser.add_argument("--hubspot-contact-id", default="placeholder-contact",
                        help="REAL HubSpot contact ID — required for Module 7")
    parser.add_argument("--hubspot-company-id", default="placeholder-company",
                        help="REAL HubSpot company ID")
    parser.add_argument("--prospect-type", default="former_client",
                        choices=["former_client", "bor_target", "coi_referral",
                                 "lost_opportunity", "industry_trigger", "conference",
                                 "bankruptcy_trigger"])
    parser.add_argument("--send-reason", default="round-robin",
                        choices=["renewal", "round-robin"])
    parser.add_argument("--subject", default="Re-connecting: trade credit panel")
    parser.add_argument("--subject-alt", default="Manufacturing credit market update")
    args = parser.parse_args()

    db_path = Path(os.path.expanduser(os.getenv("SQLITE_PATH", "~/.sdr-engine/queue.db")))
    if not db_path.exists():
        print(f"ERROR: SQLite file not found at {db_path}. Run scripts/init_db.py first.",
              file=sys.stderr)
        return 1

    body = DEFAULT_BODY.format(first_name=args.first_name, company=args.company_name)
    call_script = json.dumps(DEFAULT_CALL_SCRIPT).replace("{first_name}", args.first_name)
    deal_note = DEFAULT_DEAL_NOTE.format(first_name=args.first_name, company=args.company_name)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "INSERT INTO queue ("
        "deal_id, company_id, company_name, contact_id, contact_email, "
        "contact_first_name, contact_last_name, "
        "send_reason, prospect_type, draft_subject, draft_subject_alt, draft_body, "
        "deal_note_body, call_script_json, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            args.deal_id,
            args.hubspot_company_id,
            args.company_name,
            args.hubspot_contact_id,
            args.contact_email,
            args.first_name,
            args.last_name,
            args.send_reason,
            args.prospect_type,
            args.subject,
            args.subject_alt,
            body,
            deal_note,
            call_script,
            "pending",
        ),
    )
    queue_id = cursor.lastrowid
    conn.commit()
    conn.close()

    print(f"OK — inserted queue row id={queue_id}")
    print(f"   Company: {args.company_name}")
    print(f"   Contact: {args.first_name} {args.last_name} <{args.contact_email}>")
    print(f"   Prospect type: {args.prospect_type} / send_reason: {args.send_reason}")
    print()
    print("Next: start the UI server and open /queue:")
    print("   python ui/server.py")
    print("   open http://127.0.0.1:5679/queue")

    if "placeholder" in (args.hubspot_deal_id, args.hubspot_contact_id, args.hubspot_company_id):
        print()
        print("⚠  HubSpot IDs are placeholders. Module 7 will fail at email-engagement time.")
        print("   For a real smoke test, pass --hubspot-deal-id / --hubspot-contact-id /")
        print("   --hubspot-company-id with real HubSpot test-record IDs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
