# n8n workflows

This folder contains exported n8n workflow JSON for the Hostinger VPS n8n instance:

- `sdr-scheduler.json` — Module 1 + 2 + 2.5 + 3 + 4 + 5 (daily cron at 06:00)
- `sdr-send.json` — Module 7 (triggered by Review UI on Send)

## Importing a workflow

1. In n8n UI: Workflows -> import from file -> pick the `.json`
2. Set credentials (HubSpot, Apollo, ZoomInfo, Microsoft Graph) via n8n's encrypted credentials store - NOT via `.env`. Per A16 amendment, secrets for n8n nodes live in n8n, not the project `.env`.
3. Set n8n variable `SCHEDULER_AUTH_TOKEN` to match dbhub's Flask `.env`.
4. Keep the scheduler workflow inactive until the `Scheduler preflight` node returns HTTP 200 from `https://sdr.tradecredit.agency/scheduler/ready`.
5. Activate the workflow only after a manually approved scheduler run succeeds.

## Exporting after edits

1. Workflow → menu → "Download" → JSON
2. Save to this folder, overwriting the existing file.
3. Commit the diff.

## Workflows depend on these env vars

The Hostinger n8n service must have access to:
- `SCHEDULER_AUTH_TOKEN` for `GET /scheduler/ready` and `POST /scheduler/run`
- `OPENCLAW_WEBHOOK_URL` for scheduler failure alerts

The Hostinger n8n service must NOT access the SQLite file or local scripts directly. Those live on dbhub and are reached through Cloudflare at `https://sdr.tradecredit.agency`.

Credentials (API keys) live in n8n's credentials store, NOT in env vars accessible to the workflow nodes.

Cloudflare Access should protect human UI routes (`/`, `/queue`, `/health`) and bypass only scheduler machine routes (`/scheduler/ready`, `/scheduler/run`), which are protected by the Flask Bearer token.
