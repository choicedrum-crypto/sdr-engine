# Session Handoff — sdr-engine

**Last updated**: 2026-05-20 by Claude Opus 4.7 session on dev machine `TCIADan` (Windows 11).

**Why this file**: previous session ran low on context window. Picking up on the production machine (where n8n + Cloudflare + Ollama already run) to finish the Module 1 scheduler bring-up.

## Repo state

Main branch sits at `v0.0.8.2` after PR #12 was squash-merged on 2026-05-17 (commit `bf0baf3`). What landed:

- `POST /scheduler/run` Flask route so n8n cron can trigger the scheduler over HTTP (replaces the broken Execute Command node)
- `n8n/workflows/sdr-scheduler.json` updated to HTTP Request
- New env var: `SCHEDULER_AUTH_TOKEN` (optional Bearer token auth on the route)
- CI green; 244 tests pass on the feature branch

**Note**: HANDOFF.md itself was committed to the feature branch AFTER the squash-merge, so it did NOT ride along — it's landing in a follow-up doc PR. Skip ahead to "What to do next" if you're picking this up from main.

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

> **Shell convention**: commands below assume bash/zsh (Linux/macOS production). If the production box is Windows, swap `source .venv/bin/activate` for `.venv\Scripts\Activate.ps1` and use PowerShell equivalents (`Set-Content` for `cat <<EOF`, `$env:VAR` for `export VAR`, etc.).

### Step 1: Confirm main is current (~30 sec)

PR #12 is already merged into main. Verify:
```bash
git fetch origin main
git log origin/main -1 --oneline
# Expect: bf0baf3 feat(scheduler): POST /scheduler/run webhook + cross-host n8n workflow (#12)
```

If the production checkout is behind: `git pull origin main`. If you don't have a checkout yet, see Step 3.

### Step 2: Set up Cloudflare Tunnel for sdr.tradecredit.agency on the production machine

The production machine should be the SAME box that runs n8n + Cloudflare + Ollama (per docs/ARCHITECTURE.md "Distribution Plan" section). Full procedure documented at [docs/ARCHITECTURE.md "Cloudflare setup (one-time, ~10 minutes)"](docs/ARCHITECTURE.md) and the Pre-Launch Checklist Item 6 in that same file. Quick version:

1. `cloudflared` should already be installed (n8n.tradecredit.agency works, so cloudflared is present)
2. **Option A**: add an ingress rule to the EXISTING tunnel's `config.yml` (typically at `~/.cloudflared/config.yml` on Linux or `%USERPROFILE%\.cloudflared\config.yml` on Windows):
   ```yaml
   ingress:
     - hostname: n8n.tradecredit.agency
       service: http://localhost:5678
     - hostname: sdr.tradecredit.agency
       service: http://localhost:5679   # NEW LINE
     - service: http_status:404
   ```
3. Run `cloudflared tunnel route dns <existing-tunnel-name> sdr.tradecredit.agency`. To find the tunnel name: `cloudflared tunnel list`.
4. Restart cloudflared (`sudo systemctl restart cloudflared` on Linux; restart the Windows service via `services.msc` or `Restart-Service Cloudflared`). Some versions hot-reload `config.yml` automatically.
5. (Optional but recommended) Cloudflare Zero Trust → Access → Add Application for `sdr.tradecredit.agency` with email policy locked to Daniel's TCIA email. **Confirm the exact address before applying** — a typo here will lock everyone out.

### Step 3: Deploy the Python code to the production machine

The Flask app + scheduler scripts need to run on the same box as n8n + Cloudflared so `localhost:5679` works from the tunnel.

```bash
# Linux/macOS:
git clone https://github.com/choicedrum-crypto/sdr-engine.git
cd sdr-engine
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python scripts/init_db.py   # creates ~/.sdr-engine/queue.db
```

Windows / PowerShell equivalent:
```powershell
git clone https://github.com/choicedrum-crypto/sdr-engine.git
cd sdr-engine
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python scripts\init_db.py   # creates %USERPROFILE%\.sdr-engine\queue.db
```

### Step 4: Create `.env` on the production machine

Don't hand-copy the dev `.env` block — copy the template and edit:

```bash
cp .env.example .env       # PowerShell: Copy-Item .env.example .env
```

Then fill in (these are the values that differ from the template defaults — everything else can stay as shipped):

| Var | Value / source |
|-----|----------------|
| `HUBSPOT_API_KEY` | Private app token from dev `.env` (`pat-na1-...`) |
| `HUBSPOT_OWNER_ID` | `140182322` — **Owner ID, NOT User ID** (see gotcha table below) |
| `HUBSPOT_PROSPECTING_PIPELINE_ID` | `883898414` |
| `N8N_SEND_EMAIL_WEBHOOK_URL` | `https://n8n.tradecredit.agency/webhook/sdr-send-email` |
| `N8N_AUTH_HEADER_VALUE` | The full header value if n8n's send-email webhook uses Header Auth (recommended — see docs/SMOKE_TEST.md §1.7). **Without this, Module 7 sends will 401.** |
| `SCHEDULER_AUTH_TOKEN` | Generate fresh — 32+ char random string. Same value goes into n8n's env (see Step 6). |
| `OPENCLAW_WEBHOOK_URL` | If you have OpenClaw monitoring; leave empty otherwise |

ZoomInfo / Apollo / MS Graph keys are deferred (Module 2.5 not wired into scheduler; Module 3 anchors/hooks deferred) — safe to leave blank for the daily-cron bring-up.

### Step 5: Run Flask UI as a service

For ad-hoc validation only, `python ui/server.py` works. For production, use a real WSGI server — the `/scheduler/run` endpoint runs synchronously for 30s–2min and Flask's dev server is single-threaded, so a long scheduler run will block the UI.

Linux: `gunicorn` under systemd, e.g.
```ini
# /etc/systemd/system/sdr-engine.service
[Service]
WorkingDirectory=/opt/sdr-engine
ExecStart=/opt/sdr-engine/.venv/bin/gunicorn -w 2 -b 127.0.0.1:5679 -t 300 'ui.server:create_app()'
EnvironmentFile=/opt/sdr-engine/.env
Restart=on-failure
```
(Add `[Install] WantedBy=multi-user.target` and a `[Unit]` block as needed. Note the `-t 300` worker timeout for the scheduler endpoint.)

Windows: `waitress-serve --listen=127.0.0.1:5679 --call ui.server:create_app` wrapped by NSSM or Task Scheduler. Pin `waitress` in your install if you go this route.

Verify externally once the service is up:
```bash
curl https://sdr.tradecredit.agency/health
# Returns JSON: {llm, hubspot, sqlite, last_run, last_run_age_seconds, overall}
# Expect overall=ok once HubSpot key + LiteLLM are reachable
```

### Step 6: Re-import the updated n8n scheduler workflow

Delete the old broken one if present, then import `n8n/workflows/sdr-scheduler.json` (the v0.0.8.2 version with HTTP Request node).

n8n needs four env vars set on its container (or in Settings → Variables on Cloud/desktop). Same values must be available on both sides where applicable:

| Var | Source | Why |
|-----|--------|-----|
| `SCHEDULER_AUTH_TOKEN` | Same value as Flask's `.env` | Shared secret for Flask's `/scheduler/run` auth check |
| `CF_ACCESS_CLIENT_ID` | Cloudflare → Zero Trust → Access → Service Auth → token Client ID | Lets the cron call through Cloudflare Access without a human login |
| `CF_ACCESS_CLIENT_SECRET` | Same token's Client Secret (only shown once at creation) | Same — pairs with the Client ID |
| `OPENCLAW_WEBHOOK_URL` | Your OpenClaw webhook URL, or leave unset | Powers the "Alert OpenClaw" branch on failed runs (optional) |

**Self-hosted Docker** (most likely): add these to the n8n service's env (docker-compose `environment:` block, env_file, or `docker run -e`), then restart the container so n8n picks them up. Re-importing the workflow won't re-read env vars — only a restart does.

**Cloudflare Access service-token reminder**: in Cloudflare → Zero Trust → Access → Applications → SDR Engine → Policies, the service token must be added to an Include rule (e.g. an "Allow" policy with rule: Service Auth → `n8n-scheduler`). Without that, Cloudflare doesn't recognize the token even if the headers are correct.

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

1. `python -m pytest -q` from the repo root → expect ~244 passed (the count grows as PRs land)
2. `python -m ruff check .` → "All checks passed!"
3. After Step 5 above: `curl https://sdr.tradecredit.agency/health` returns JSON with `overall: ok`
4. After Step 7: n8n Execute Workflow returns 200 with the ScheduleResult JSON
