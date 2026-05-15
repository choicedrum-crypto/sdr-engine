# sdr-engine

Internal SDR reactivation engine for TCIA (trade credit + AR protection brokerage).

Surfaces re-engageable prospects from HubSpot's "Prospecting" pipeline at the right moment (60 days before renewal when known; round-robin year-long fallback otherwise), drafts three artifacts per send (deal note + email + cold-call script) via local LLM, and auto-creates a paired phone-call task in HubSpot.

**Status**: pre-build. Architecture locked, scaffolding in progress.

## Spec

See `docs/ARCHITECTURE.md` for the v1 spec, including all modules, schema, env vars, and pre-launch checklist. The spec went through `/autoplan` review on 2026-05-14 and has 16 mechanical amendments + locked taste decisions baked in.

## Quick start (for the maintainer)

1. **Clone + install**:
   ```bash
   git clone https://github.com/choicedrum-crypto/sdr-engine.git
   cd sdr-engine
   python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
   pip install -e ".[dev]"
   ```

2. **Configure**: `cp .env.example .env` and fill in the secrets (HubSpot, ZoomInfo, MS Graph, OpenClaw webhook). Apollo and LiteLLM endpoints should already be running on the internal server.

3. **Initialize the queue DB**:
   ```bash
   mkdir -p ~/.sdr-engine
   sqlite3 ~/.sdr-engine/queue.db < sql/schema.sql
   ```

4. **Run tests**: `pytest`

5. **Run the Review UI** (when implemented): `python ui/server.py` — accessible at `http://127.0.0.1:5679` locally or via Cloudflare Tunnel at `https://sdr.tradecredit.agency`.

## Architecture at a glance

```
HubSpot Prospecting pipeline (245 deals across 7 funnel types)
       │
       ▼
[Module 1] n8n cron daily 06:00 → filter + classify + dedupe + bucket
       │
       ▼
[Module 2] Apollo search (URL verification, no enrich credit burn)
       │
       ▼
[Module 2.5] ZoomInfo search-first → qualify → enrich only the chosen contact
       │
       ▼
[Module 3] Relationship anchor + current-event hook + policy excerpt
       │
       ▼
[Module 4] LiteLLM routing — local-main qwen3.5:27b → heavy-main codex fallback
       │
       ▼
[Module 5] SQLite queue (WAL mode, runs heartbeat, dropped log, enrichment cache)
       │
       ▼
[Module 6] Flask UI (127.0.0.1:5679 + Cloudflare Tunnel) — email-only review
       │
       ▼
[Module 7] HubSpot writes — email engagement + deal note + phone task with script
       │
       ▼
[Module 8] OpenClaw monitoring — sync webhook on errors, nightly retry sweep
```

## Tech stack

- **Workflow**: n8n
- **Local LLM**: Ollama via LiteLLM (`local-main` qwen3.5:27b primary, `heavy-main` codex cloud fallback)
- **State**: SQLite WAL
- **UI**: Flask single-file
- **External**: HubSpot, Apollo (URL only), ZoomInfo (search + qualified enrich), Microsoft Graph (SharePoint policy summaries)
- **Remote access**: Cloudflare Tunnel + Access at `sdr.tradecredit.agency`
- **Monitoring**: OpenClaw
- **Tests**: pytest

## Status of pre-launch checks

See `docs/ARCHITECTURE.md` → "Pre-Launch Checklist" for the full list and procedures.

| # | Check | Status |
|---|-------|--------|
| 1 | `HUBSPOT_PROSPECT_TYPE_PROPERTY` = `funnel_type` | RESOLVED — verify exact value strings via API |
| 2 | HubSpot sender identity test | PENDING — procedure documented |
| 3 | Outreach compliance (USA-only) | RESOLVED |
| 4 | LiteLLM smoke test | PENDING — procedure documented |
| 5 | `prompts/ctas.json` validation | RESOLVED — A14 startup check + test_scaffolding test |
| 6 | Cloudflare Tunnel + Access setup | PENDING — procedure documented |
| 7 | Day-1 dry run | PENDING — runs after code lands |

## Contributing

This is a private internal tool. Direct commits to `main` are disabled — all changes go through PRs. Run `/ship` from a feature branch to land code.
