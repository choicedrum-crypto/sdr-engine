# End-to-End Smoke Test

This is the first real test of the system. Goal: insert a test card, open the Review UI, click Send, and verify the email + note + phone task appear in HubSpot under Daniel's owner identity.

Required pieces all merged at v0.0.5:
- Module 4 (LLM drafting) — bypassed in this test (we hand-craft the draft)
- Module 7 (HubSpot writes) — the path under test
- Module 6 (Review UI) — the surface

**Estimated time**: 15-20 minutes. Most of it is the one-time HubSpot test-record setup.

## Pre-requisites

### 1. Resolve the pre-launch checks
Before the first send, the architecture's Pre-Launch Checklist Item 2 (sender identity) needs to be in place:

- Daniel's TCIA email connected via OAuth in HubSpot Settings → General → Email → Connected emails (status should read "Connected", not "Connected via IMAP/SMTP")
- SPF/DKIM/DMARC green for `tcia.com` in HubSpot Settings → Domains & URLs → Email sending domain

### 1.5. Find the right Owner ID (NOT User ID)

HubSpot has two distinct numeric IDs per person — `HUBSPOT_OWNER_ID` requires the **Owner ID**, not the User ID. They are different numbers.

Easiest way to find it (no extra scopes needed): pick any deal in HubSpot, then run:

```bash
curl -s "https://api.hubapi.com/crm/v3/objects/deals/DEAL_ID?properties=hubspot_owner_id" \
  -H "Authorization: Bearer YOUR_PRIVATE_APP_TOKEN" | jq .properties.hubspot_owner_id
```

The value returned is your Owner ID (a 9-digit number, distinct from your 8-digit User ID).

### 1.7. Set up the n8n send webhook

HubSpot's `/crm/v3/objects/emails` endpoint **only logs an email engagement** — it doesn't actually deliver to the prospect. To deliver, Module 7 POSTs to an n8n workflow that routes to your connected M365 account.

In n8n:
1. Create a new workflow named `sdr-send-email`
2. Add a **Webhook trigger** node:
   - HTTP Method: `POST`
   - Path: `sdr-send-email`
   - Authentication: Header Auth recommended (set a secret token; you'll put the full header value like `Bearer abc123` into `N8N_AUTH_HEADER_VALUE`)
   - Save + activate, copy the production webhook URL
3. Add a **Microsoft Outlook** node (operation: Send Email):
   - To: `={{ $json.body.to }}`
   - Subject: `={{ $json.body.subject }}`
   - Body: `={{ $json.body.body }}` (set message type to HTML if you want HTML-rendered)
   - From: leave default (uses connected M365 account = your TCIA email)
   - Configure credentials: OAuth-connect Daniel's M365 account (one-time)
4. Test the workflow manually from n8n's "Execute Workflow" button with a hand-typed payload to your personal email to confirm the connected account works.
5. Put the webhook URL into `.env`: `N8N_SEND_EMAIL_WEBHOOK_URL=https://n8n.internal/webhook/sdr-send-email`

### 2. Create a HubSpot test record
Pick a target where unsolicited TCIA email is fine. Options (low → high realism):
- (Recommended) Create a brand-new test company + contact in HubSpot. Set the contact's email to **your own personal email** so you can verify what the prospect sees.
- Use a real Prospecting-pipeline deal but with the contact email rewritten to yours temporarily.

Capture three HubSpot IDs from the URL/properties of the test records:
- `--hubspot-deal-id` (deal ID, must be in the Prospecting pipeline if testing renewal/round-robin routing)
- `--hubspot-contact-id`
- `--hubspot-company-id`

### 3. Set environment

Copy `.env.example` to `.env` if you haven't:
```bash
cp .env.example .env
```

Required values for the smoke test:
- `HUBSPOT_API_KEY` — private app token (`pat-na1-...`) with the scopes from `docs/ARCHITECTURE.md` Dependencies section
- `HUBSPOT_OWNER_ID` — Daniel's **Owner** ID (per step 1.5 above — NOT the User ID, the two are different)
- `N8N_SEND_EMAIL_WEBHOOK_URL` — from step 1.7
- `N8N_AUTH_HEADER_VALUE` — if your n8n webhook is protected by Header Auth
- `SQLITE_PATH` — leave default (`~/.sdr-engine/queue.db`)

The LLM endpoint, ZoomInfo, and MS Graph values aren't needed for this smoke test (Module 6's send path doesn't call them; MS Graph credentials are reused by the n8n Outlook node, not this app).

## Steps

### Step A: install + init DB

```bash
# From repo root, with .venv activated
pip install -e ".[dev]"
python scripts/init_db.py
```

Expected output: `OK — schema applied at ...` with `Journal mode: wal`.

### Step B: insert a test card

```bash
python scripts/insert_test_card.py \
    --contact-email YOUR_PERSONAL_EMAIL@gmail.com \
    --hubspot-deal-id YOUR_HUBSPOT_DEAL_ID \
    --hubspot-contact-id YOUR_HUBSPOT_CONTACT_ID \
    --hubspot-company-id YOUR_HUBSPOT_COMPANY_ID
```

Expected output: `OK — inserted queue row id=1`.

### Step C: start the UI server

```bash
python ui/server.py
```

You should see Flask printing `Running on http://127.0.0.1:5679`. Leave this running.

### Step D: open the queue

```bash
open http://127.0.0.1:5679/queue
```

Or paste that URL into your browser. You should see:
- "Test Co Inc" company header
- The default subject (`Re-connecting: trade credit panel`) in the dropdown
- The email body pre-filled in a textarea, autofocused
- Send + Skip buttons
- Top-right status indicator (red "Last run: never (stale)" — that's expected since no scheduler has run)

### Step E: try the keyboard shortcuts (verify A9)

- Press `k` → should fire the Skip action. **Don't actually skip yet.** Click "back" instead, then reload.
- Press `e` → cursor should jump to the body textarea.

### Step F: send the test email

Click **Send** or press `s`. The UI should redirect back to `/queue`, which will show "All caught up." since there are no more pending rows.

### Step G: verify in HubSpot

In HubSpot, navigate to the test deal. You should see three new activities in the timeline:

1. **Email** — Subject matches what you sent. Body matches. Logged under Daniel's owner identity (not "noreply@hubspot.com").
2. **Note** — The SDR Brief content.
3. **Task** — "Follow up on reactivation email to Test Co Inc" — priority HIGH, type CALL, due ~36h from now. Task body contains the formatted call script.

### Step H: verify the prospect inbox (the load-bearing check)

Open your personal email (cellular network, not on TCIA Wi-Fi — best to match what a real prospect would see). Find the test email. Check:

| Check | PASS | FAIL — what to fix |
|-------|------|-------------------|
| From line shows `Daniel Bradley <daniel@tcia.com>` | ✅ | OAuth connection setup incomplete. Re-do Step 1 connected emails. |
| No "via hubspot.com" or "on behalf of" footer | ✅ | DNS/auth setup missing. Verify SPF/DKIM/DMARC. |
| Hit Reply → To: field shows `daniel@tcia.com` | ✅ | Reply-to is misconfigured. HubSpot Settings → Email → Reply-to address. |
| View original → `dkim=pass`, `spf=pass`, `dmarc=pass` | ✅ | DNS records not propagated; wait 24h after adding records and re-test. |

**Any FAIL here = do not launch.** Sender identity issues compound over time (deliverability reputation damage) and are hard to undo. Fix and re-test before adding more cards.

## Cleanup after smoke test

Drop the test row to avoid it surfacing on the next scheduler run:

```bash
sqlite3 ~/.sdr-engine/queue.db "DELETE FROM queue WHERE deal_id='smoke-test-001';"
```

In HubSpot, optionally delete the test contact + company + deal, or keep them as a known-test record for future smoke tests.

## What this test does NOT verify

- The Module 4 LLM draft path (we hand-craft the body)
- Module 2.5 ZoomInfo enrichment
- Module 1 n8n scheduler (doesn't exist yet)
- Module 3 anchor/hook extraction (doesn't exist yet)
- Cloudflare Tunnel remote access (verify separately per Pre-Launch Checklist Item 6)

These ship in later PRs. The smoke test covers the part that ALL future code will route through: the Module 4 → 7 → HubSpot final mile.
