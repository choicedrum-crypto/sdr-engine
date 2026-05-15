# n8n workflows

This folder will contain exported n8n workflow JSON for:

- `sdr-scheduler.json` — Module 1 + 2 + 2.5 + 3 + 4 + 5 (daily cron at 06:00)
- `sdr-send.json` — Module 7 (triggered by Review UI on Send)

## Importing a workflow

1. In n8n UI: Workflows → import from file → pick the `.json`
2. Set credentials (HubSpot, Apollo, ZoomInfo, Microsoft Graph) via n8n's encrypted credentials store — NOT via `.env`. Per A16 amendment, secrets for n8n nodes live in n8n, not the project `.env`.
3. Activate the workflow.

## Exporting after edits

1. Workflow → menu → "Download" → JSON
2. Save to this folder, overwriting the existing file.
3. Commit the diff.

## Workflows depend on these env vars

The n8n container/service that runs these workflows must have access to:
- `LLM_ENDPOINT`, `LLM_MODEL_PRIMARY`, `LLM_MODEL_FALLBACK` (for Module 4)
- `SQLITE_PATH` (for Module 5)
- Static config files at `config/holidays.json`, `config/tcia-brokers.json`, `config/sector-signals.json`
- `prompts/draft-pipeline.txt`, `prompts/ctas.json`

Credentials (API keys) live in n8n's credentials store, NOT in env vars accessible to the workflow nodes.

(Workflow JSON files added in follow-up PRs as each module is built.)
