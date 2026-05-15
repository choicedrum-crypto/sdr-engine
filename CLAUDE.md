# CLAUDE.md

Project guidance for AI assistants working in this repo.

## Project overview

SDR Reactivation Engine — internal automation for TCIA (trade credit + AR protection brokerage). Surfaces re-engageable prospects from HubSpot's "Prospecting" pipeline with renewal-triggered + round-robin cadence, drafts 3 artifacts (deal note + email + cold-call script) via local LLM, paired phone-call task auto-created on send. Single user (Daniel, the SDR), self-hosted on internal server. See `docs/ARCHITECTURE.md` for the full v1 spec.

## Stack

- **Workflow engine**: n8n (existing self-hosted instance)
- **Local LLM**: Ollama via LiteLLM routing (`local-main` = qwen3.5:27b, `heavy-main` = codex cloud fallback)
- **State store**: SQLite (`~/.sdr-engine/queue.db`, WAL mode)
- **UI**: Flask, single-file `ui/server.py`, bound `127.0.0.1:5679`
- **Remote access**: Cloudflare Tunnel + Access at `https://sdr.tradecredit.agency`
- **External APIs**: HubSpot (CRM of record), Apollo (company URL verification), ZoomInfo (contact enrichment), Microsoft Graph (SharePoint policy summaries)
- **Monitoring**: OpenClaw (existing)
- **Tests**: pytest

## Testing

Run tests: `pytest`
Test directory: `tests/`
Framework: pytest with `responses` for HTTP mocking.
See `pyproject.toml` for the full dev dependencies.

Expectations:
- 100% test coverage is the goal — tests make this codebase safe to evolve under time pressure
- When writing a new function, write a corresponding test
- When fixing a bug, write a regression test
- When adding a conditional, write tests for BOTH paths
- Never commit code that makes existing tests fail
- Integration tests (real external APIs) live under `@pytest.mark.integration` — skipped in CI without secrets

## Test Coverage

Minimum: 70%
Target: 85%

These thresholds gate `/ship`. AI-assessed coverage diagrams treat error paths and edge cases as first-class — happy-path-only tests are ★, not ★★★.

## Architecture decisions (locked)

- **Single LLM call returns 3 artifacts** (P22) — deal note + email + call script in one JSON response. Validation is structured (A3): on 2nd failure, fall back to `heavy-main` (cloud) before NEEDS_HUMAN.
- **Prompt injection wrapping** (A1) — all user-controlled inputs (deal notes, policy excerpts, anchors) wrapped in `<untrusted_input>` tags with system instruction to never follow instructions inside them.
- **M2.5 cache-first atomicity** (A2) — enrichment_cache row written BEFORE HubSpot writes; on rollback failure, status becomes `'orphan'` (manual cleanup).
- **USA-only outreach** — ZoomInfo `country: "United States"` hard filter. No EU/CAN scope.
- **Email-only Review UI** (T3=A) — UI shows email card; deal note + call script auto-post to HubSpot via Module 7. SDR edits those in HubSpot directly.

## Prompt/LLM changes

These files affect LLM output and require eval verification on change:
- `prompts/draft-pipeline.txt` (canonical voice source)
- `prompts/ctas.json` (CTAs filtered by `allowed_for` per prospect type)
- `config/tcia-brokers.json` (relationship-anchor extraction)
- `config/sector-signals.json` (current-event hook source)
- Any change to Module 4's substitution logic

When changing these, run the eval suite (added in a future PR) before merging.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
