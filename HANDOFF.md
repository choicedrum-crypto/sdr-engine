# Session Handoff — sdr-engine

**Last updated**: 2026-05-17 by Claude Opus 4.7 session on dev machine `TCIADan`.

**Why this file**: previous session ran low on context window. Picking up on the production machine (where n8n + Cloudflare + Ollama already run) to finish the Module 1 scheduler bring-up.

## Repo state

Main branch sits at `v0.0.8.1` (PR #11 fix-scheduler-hubspot-filter merged 2026-05-17).

**Open PR #12 (awaiting merge)** — `feat/scheduler-webhook`:
- Adds `POST /scheduler/run` Flask route so n8n cron can trigger the scheduler over HTTP (replaces the broken Execute Command node).
- Updates `n8n/workflows/sdr-scheduler.json` to use HTTP Request instead of Execute Command.
- New env var: `SCHEDULER_AUTH_TOKEN` (optional Bearer token auth on the route).
- CI green; 244 tests pass; ready to merge.
- https://github.com/choicedrum-crypto/sdr-engine/pull/12

## Where you are in the build-out

| Module | Status |
|--------|--------|
| 4 — LLM drafting | ✅ Done |
| 2.5 — ZoomInfo enrichment (lib) | ✅ Done (NOT wired into scheduler yet — deferred) |
| 7 — HubSpot writes + n8n send | ✅ Done; smoke-tested end-to-end 2026-05-17 |
| 6 — Review UI | ✅ Done |
| 8 — Monitoring + sweeps | ✅ Done |
| 1 — Scheduler | ✅ MVP done; dry-run validated against real HubSpot (214 candidates examined) |
| 3 — Per-deal context (MVP) | ✅ Done; anchor extraction + OneDrive + hooks deferred to follow-ups |

System is end-to-end functional. The only thing blocking unattended daily cron is **Cloudflare Tunnel + DNS for `sdr.tradecredit.agency`** on the production machine.

## What blocked the previous session

n8n container at `n8n.tradecredit.agency` tried to POST to `sdr.tradecredit.agency/scheduler/run` and got `ENOTFOUND` — the DNS hostname doesn't exist yet. We never set up that tunnel; the smoke test only needed local browser access.

## What to do next, in order

### Step 1: Merge PR #12 (~1 min)
If not already merged:
```bash
gh pr merge 12 --merge --delete-branch
```

### Step 2: Set up Cloudflare Tunnel for sdr.tradecredit.agency on the production machine

The production machine should be the SAME box that runs n8n + Cloudflare + Ollama (per docs/ARCHITECTURE.md "Distribution Plan" section). Procedure documented in `docs/ARCHITECTURE.md` under the Cloudflare setup section + `docs/SMOKE_TEST.md` Item 6:

1. `cloudflared` should already be installed (n8n.tradecredit.agency works, so cloudflared is present)
2. **Option A**: add an ingress rule to the EXISTING tunnel's `config.yml`:
   ```yaml
   ingress:
     - hostname: n8n.tradecredit.agency
       service: http://localhost:5678
     - hostname: sdr.tradecredit.agency
       service: http://localhost:5679   # NEW LINE
     - service: http_status:404
   ```
3. Run `cloudflared tunnel route dns <existing-tunnel-name> sdr.tradecredit.agency`
4. Restart cloudflared (or hot-reload if your version supports it)
5. (Optional but recommended) Cloudflare Zero Trust → Access → Add Application for `sdr.tradecredit.agency` with email policy = `daniel@tcia.com` only

### Step 3: Deploy the Python code to the production machine

The Flask app + scheduler scripts need to run on the same box as n8n + Cloudflared so `localhost:5679` works from the tunnel.

```bash
# On the production machine:
git clone https://github.com/choicedrum-crypto/sdr-engine.git
cd sdr-engine
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python scripts/init_db.py   # creates ~/.sdr-engine/queue.db
```

### Step 4: Create `.env` on the production machine

The dev machine's `.env` has all the right values; copy or re-enter on production:

```bash
# HubSpot
HUBSPOT_API_KEY=pat-na1-...                    # private app token, 44 chars
HUBSPOT_OWNER_ID=140182322                     # OWNER ID, not user ID (different concepts)
HUBSPOT_PROSPECTING_PIPELINE_ID=883898414
HUBSPOT_PROSPECT_TYPE_PROPERTY=funnel_type

# LLM (LiteLLM router)
LLM_ENDPOINT=http://localhost:4000/v1/chat/completions
LLM_MODEL_PRIMARY=local-main                   # qwen3.5:27b
LLM_MODEL_FALLBACK=heavy-main                  # codex cloud

# Email send (n8n webhook)
N8N_SEND_EMAIL_WEBHOOK_URL=https://n8n.tradecredit.agency/webhook/sdr-send-email

# Scheduler webhook auth (n8n → Flask)
SCHEDULER_AUTH_TOKEN=<random 32+ char string — generate fresh>

# Storage
SQLITE_PATH=~/.sdr-engine/queue.db

# OpenClaw (if you have it)
OPENCLAW_WEBHOOK_URL=
```

### Step 5: Start Flask UI as a service

For production, use systemd or similar to keep `python ui/server.py` running. Quick start for validation:

```bash
python ui/server.py
# Should see: Running on http://127.0.0.1:5679
```

Verify externally:
```bash
curl https://sdr.tradecredit.agency/health
# Should return JSON with sqlite=ok, hubspot=ok
```

### Step 6: Re-import the updated n8n scheduler workflow

Delete the old broken one if present, then import `n8n/workflows/sdr-scheduler.json` (the v0.0.8.2 version with HTTP Request node).

In n8n Settings → Variables, set `SCHEDULER_AUTH_TOKEN` to the SAME value as in `.env`.

### Step 7: Manual test from n8n

Click "Execute Workflow" on the scheduler. Should see:
- "Run scheduler" node → HTTP 200 with JSON like `{"candidates_examined": 214, "enqueued": 0 or more, ...}`
- "Did it fail?" → false branch (no errors)

If `enqueued > 0`, check `/queue` in your browser at `https://sdr.tradecredit.agency/queue` to see the drafts.

### Step 8: Toggle the workflow Active

Cron will fire `0 6 * * 1-5` and the system runs unattended.

## Critical context / gotchas from this session

| Gotcha | What you'd hit if you didn't know |
|--------|-----------------------------------|
| HubSpot User ID vs Owner ID are different numbers | INVALID_OWNER_ID on email engagement creation |
| HubSpot's `notes_last_contacted` (no `_date` suffix) | HTTP 400 on deal search |
| HubSpot `LT` filter excludes NULL values | 235 of 236 deals silently filtered out — fix uses OR-semantics with NOT_HAS_PROPERTY |
| HubSpot `/crm/v3/objects/emails` LOGS only, doesn't send | Emails appear in HubSpot timeline but never reach prospects (Module 7 amendment fired n8n webhook for actual delivery) |
| Windows browser CRLF vs server LF in textarea | Status shows `edit-sent` instead of `sent` for unchanged sends (cosmetic) |
| n8n `executeCommand` node needs Python on n8n's container | Use HTTP Request to Flask `/scheduler/run` instead (PR #12) |

## What's deferred / what would I do next

Roughly in order of value-per-effort:

1. **Module 3 anchor extraction** — match deal notes against `config/tcia-brokers.json`. Biggest quality lift; turns generic drafts into "since you worked with Tom O'Connell" drafts. ~150-200 lines.
2. **Module 3 current-event hook** — M&A signals via ZoomInfo `enrich_scoops`, leadership changes via `enrich_contacts` filtered by job-start-date. Most useful for `industry_trigger` + `bankruptcy_trigger`. ~200-300 lines.
3. **Module 3 OneDrive/SharePoint policy excerpt** — Microsoft Graph fetch + .docx/.pdf parsing. Useful for `former_client` with prior policy history. ~200 lines.
4. **Module 2.5 inline in scheduler** — only matters if there are pipeline deals with no associated contacts. Defer unless you see `no_valid_contact_after_enrichment` showing up in dropped reasons frequently.
5. **`compute_edit_diff` whitespace normalization** — tiny cosmetic fix for the `edit-sent` status on Windows-browser CRLF submits.

## Files worth skimming first

- `docs/ARCHITECTURE.md` — the full spec, autoplan-reviewed
- `docs/SMOKE_TEST.md` — what works + how to validate
- `HANDOFF.md` — this file
- `sdr_engine/scheduler.py` — Module 1 main loop
- `sdr_engine/context.py` — Module 3 MVP (what's deferred is clearly noted)
- `ui/server.py` — Flask routes including `/scheduler/run`
- `n8n/workflows/sdr-send-email.json` + `sdr-scheduler.json` — the two n8n workflows

## How to verify everything still works after pickup

1. `python -m pytest -q` from the repo root → expect "244 passed" (or 243+ as future PRs add)
2. `python -m ruff check .` → "All checks passed!"
3. After Step 5 above: `curl https://sdr.tradecredit.agency/health` returns valid JSON
4. After Step 7: n8n Execute Workflow returns 200 with the ScheduleResult JSON
