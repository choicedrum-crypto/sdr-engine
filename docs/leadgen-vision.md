<!-- /autoplan restore point: ~/.gstack/projects/choicedrum-crypto-sdr-engine/claude-busy-benz-4f55c5-autoplan-restore-20260520-145737.md -->
# TCIA Lead-Gen Pipeline — Vision

> **Scope note (updated 2026-05-20):** the lead-gen pipeline (bankruptcy triggers, COI lanes, weekly scored prospect lists, Apollo-ready CSVs) is a **distinct workflow** from the SDR Reactivation Engine, which handles per-deal cadence inside HubSpot's "Prospecting" pipeline.
>
> **Repo shape:** **monorepo** with `apps/sdr-engine/`, `apps/tcia-leadgen/`, and `packages/tcia-core/` (shared Cloudflare middleware, JWT verify, SQLite helpers, alembic, smoke-test scripts). The two apps ship independently from one repo and share locked architectural decisions (A1 prompt-injection wrap, A2 cache-first atomicity, A3-equivalent retry-fallback) via `tcia-core`. *(Decision #1 in DECISIONS APPLIED below.)*

## Problem

Lead-gen currently lives largely on SharePoint with assorted scripts and Excel mirrors. Each weekly run subtly tweaks scoring criteria or brief structure with no diff and no audit trail. The drift isn't really file drift — it's **prompt and logic drift**.

## Architecture

### Where each thing lives

**Linux server + GitHub (canonical):**

- All Python scripts, prompts, schemas, alembic migrations, gstack review configs, Hermes workflow definitions
- `~/.tcia-leadgen/leadgen.db` SQLite (WAL) — single writer, system of record for prospect state, run history, scores
- The actual weekly run executes here (cron triggers `POST /scheduler/run`; Hermes orchestrates)

**SharePoint (consumption + handoff only):**

- Weekly Excel mirrors of the SQLite tables (auto-generated, never hand-edited)
- Per-run output folders: scored CSVs, Apollo-ready uploads, markdown briefs
- SOPs and reference docs the team reads but doesn't execute against
- `_shared/` prompt copies for team visibility (read-only mirror of the repo)

### The sync rule that kills drift

**SharePoint is downstream. Nothing on SharePoint feeds back into the pipeline.** Per policy: **Rob (the SDR operator) does not edit the gathered data in SharePoint.** SharePoint is purely an output surface — Excel mirrors, CSVs, briefs land there for reading. Any signal Rob needs to communicate back into scoring goes through the dashboard or a documented feedback channel, never through Excel edits. This is the single change that eliminates ~90% of drift risk in the current setup. *(See SharePoint inventory audit in Concrete next steps — week-0.)*

### Employee access (the Cloudflare answer)

No one needs to remote in. Stand up:

- **Cloudflare Tunnel** from the Linux box exposing a small internal Flask dashboard (`ui/server.py` bound to `127.0.0.1`)
- **Cloudflare Access** in front of the tunnel, with email-based auth restricted to the `tcia-tn.com` domain

Rob hits `https://leadgen.tcia-tn.com`, logs in with his work email, sees the latest BK run, can trigger a re-score from a button, can download the Apollo CSV. Zero VPN, zero RDP, zero server literacy. Free at this scale.

**Identity contract (locked):** Flask binds `127.0.0.1` only and verifies the `Cf-Access-Jwt-Assertion` JWT against the team JWKS on every request. The `cf-access-authenticated-user-email` convenience header is NOT trusted as an identity source. Every dashboard mutation records `triggered_by_email` from the verified JWT claim into the `audit_log` table. *(Decision #3.)*

### LLM stack and orchestration (updated 2026-05-20)

LiteLLM is removed from the stack. The new layout:

- **Codex (cloud)** — default reasoning model for all LLM calls in leadgen: CourtListener alert filtering, creditor PDF field extraction, debtor name normalization, prospect scoring rationale, SDR brief drafting + polishing, COI email personalization. Everything routes through Codex unless explicitly offloaded to OpenClaw.
- **OpenClaw + 3 local models (on-box)** — agent execution layer. Local models are NOT directly callable from leadgen code; they power OpenClaw's internal tool-using sub-agents. When leadgen needs an agentic sub-task (e.g., "find this debtor's parent company across 3 sources"), it calls OpenClaw; OpenClaw routes internally.
- **Hermes** — workflow orchestrator (replaces what n8n would have done). Hermes receives the cron trigger via `POST /scheduler/run`, runs the multi-step BK lane (alert → filter → enrich → score → brief → upload), calls Codex for reasoning and OpenClaw for agentic sub-tasks, writes outcomes to SQLite. *(Decision #2 — n8n is dropped from leadgen v1.)*

**Cost ceiling (required, per Eng P2):** per-run LLM spend has a hard circuit-breaker. If the BK lane exceeds the ceiling mid-run, remaining prospects write to a `deferred` table with `reason=budget_exceeded` for next-run pickup. High-water-mark alert at 2× rolling 4-week average prospect count fires before auto-run; Daniel approves over-budget runs manually.

**Codex auth + retry contract:** Codex CLI auth (API key or session login) is the single LLM credential in the stack. On Codex call failure, distinguish two failure classes:

- **Per-call failure** (timeout, 5xx, malformed JSON, model hiccup) — retry once with exponential backoff; on 2nd failure, write `(prospect_id, status='NEEDS_HUMAN', last_error)` and continue the run. **Never halt the whole run** for one prospect.
- **Auth-class failure** (401, 403, quota exhausted) — if 2 consecutive Codex calls share an auth-class error, **trip the circuit breaker immediately**, halt the run, fire the webhook alert. Do not write 500 NEEDS_HUMAN rows from a single expired session.

**Hermes orchestration contract:** Hermes workflows are defined as **Python decorators** in `apps/tcia-leadgen/workflows/*.py` — diffable, testable, refactorable, reviewed by `/review` like any code. No JSON DSL, no DB-stored binary format. The `workflow_registry` decorator helpers live in `packages/tcia-core`.

**Hermes crash-resume contract:** each step writes a checkpoint row `(prospect_id, run_id, phase, status, output_hash)` to SQLite **before** the next step begins. On Hermes restart, resume = `SELECT prospects WHERE phase < target_phase AND run_id = current_run`. No re-paying Codex tokens for completed phases; no duplicate Apollo uploads. Checkpoint helpers live in `packages/tcia-core`.

## gstack's role — compounding value

Once code is in git, gstack provides:

- **`/review` on every PR** — same pattern as the existing Claude Code engineering reviews, pointed at scoring logic, prompt changes, and schema migrations
- **Weekly post-run `/audit` slash command** *(net-new skill to author)* — compares this week's prompts, schemas, and output structure against last week's, flags unintended deltas, writes an audit note to `run-history/`
- **`/calibrate` slash command** *(net-new skill to author)* — pulls the last 4–8 weeks of scored prospects, asks: which ones converted in Apollo, which didn't, are the scoring weights still defensible? This is the compounding loop — each week's outcomes tune next week's scoring

`/audit` and `/calibrate` are not existing gstack skills. They'd be authored via `anthropic-skills:skill-creator` after the first PR or two land and the inputs they need are concrete.

## Concrete next steps

### Week 0 — pre-code prep (decisions confirmed)

0a. **Verify `dig NS tcia-tn.com`** — confirm Cloudflare nameservers (or budget for migration). 5 min.
0b. **SharePoint inventory audit** — 90 min producing `inventory.md`: every `_shared/` and `bankruptcy-trigger/` file, purpose, language, classification (`port-verbatim` / `port-with-rewrite` / `deprecate` / `human-only-SOP`). Three-week estimate only valid after this lands.
0c. **Apollo API spike** — 1–2 hr proof-of-concept: can `prospect_id` round-trip through CSV upload → sequence-stats poll → conversion-status retrieval? If yes, design the loop. If no, **kill `/calibrate` from the plan** and document the manual quarterly-review fallback.
0d. **Monorepo migration prep** — convert `sdr-engine` repo to monorepo shape (`apps/sdr-engine/`, add `apps/tcia-leadgen/`, extract `packages/tcia-core/`).

### Week 1 — foundational mechanics

1. **Move to monorepo shape.** Extract `packages/tcia-core/` with:
   - **Cloudflare middleware + JWT verify** — Flask middleware that validates `Cf-Access-Jwt-Assertion` against team JWKS and exposes `request.user_email` to handlers
   - **SQLite + alembic helpers** — generic `init_db(db_path, migrations_path)`; per-app smoke scripts (`insert_test_card.py`, `insert_test_prospect.py`) stay in each app's `scripts/`
   - **Prompt-injection `<untrusted_input>` wrap (A1)** — port from sdr-engine; mandatory for all attacker-controllable text (CourtListener docket text, ZoomInfo enrichment fields, SharePoint document content)
   - **Cache-first atomicity (A2)** — port from sdr-engine Module 2.5; enrichment_cache row written BEFORE downstream writes; `'orphan'` status on rollback failure
   - **Codex retry-fallback pattern** — per-call retry-once + auth-class circuit-break (per LLM stack section above)
   - **OpenClaw client** — thin wrapper exposing `openclaw.call(sub_agent, payload)` with health-check method
   - **Hermes `workflow_registry` decorators** — Python decorators that register a function as a step in a named workflow; supports `@step(name='enrich', after=['filter'])`
   - **Hermes checkpoint helpers** — `checkpoint(prospect_id, run_id, phase, status, output_hash)` + `resume_from(run_id) → list[(prospect_id, next_phase)]`
   - **`do_not_contact` table primitives** — schema + `is_blocked(email_or_phone) → bool` + `add_to_dnc(email_or_phone, source, reason)`
   - **`audit_log` table primitives** — schema + `log_action(user_email, action, target, metadata)` called from JWT-verified mutations
   - **Cost-ceiling / circuit-breaker primitive** — `with_budget(run_id, phase, cap_dollars) as ctx` context manager; tracks spend per phase; trips per-phase, not aggregate
2. **Stand up SQLite + alembic** on Linux server at `~/.tcia-leadgen/leadgen.db`. Alembic configured from day 1, even with one table. Weekly snapshot to `~/.tcia-leadgen/_backups/` retained 8 weeks; restore-from-backup runbook documented.
3. **Stand up Cloudflare Tunnel + Access** on `leadgen.tcia-tn.com`. Flask binds 127.0.0.1; JWT verify against team JWKS on every request; `triggered_by_email` audit column.
4. **`leadgen` CLI scaffold** with verbs: `run`, `runs`, `prompts`, `db`, `ui`. `--dry-run` default for interactive use; cron passes `--apply`. Escape hatches: `--skip-sharepoint`, `--skip-apollo`, `--force-model`, `--prompts-at <sha>`, `--lane bk`, `--prospect-id`.

### Week 2 — port BK lane with rails

5. **Port BK pipeline** behind `leadgen run --lane bk`. Hermes orchestrates; Codex handles reasoning; OpenClaw handles agentic sub-tasks. Reuse `tcia-core` A1 wrap (CourtListener text is attacker-controllable), A2 atomicity, Codex retry-fallback.
6. **Frozen eval suite** under `tests/eval/test_bk_score.py` with N=20 golden prospects. **Pre-merge CI gate** on every PR touching `prompts/` — gate is prophylactic, `/audit` is forensic.
7. **Observability** (port sdr-engine Module 8): heartbeat ping, webhook alerts on run failure, structured JSON-lines logs to `~/.tcia-leadgen/logs/run-<ts>.jsonl`, `runs` table with `(run_id, lane, started_at, finished_at, status, prompt_git_sha, prospect_count_in, prospect_count_out, errors_count, triggered_by_email)`. Per-run telemetry also writes `openclaw_calls_failed` count and OpenClaw self-health (memory, queue depth, last-local-model-error) into a sibling `openclaw_health` table — silent enrichment degradation must be detectable from the dashboard.

### Week 3 — cutover prep

8. **Shadow-run** new BK alongside existing SharePoint pipeline for 2 weeks. Diff outputs to `run-history/parity-<date>.md`. Cutover only after 2 consecutive weeks <5% diff.
9. **Flask dashboard** with "Last 10 runs" panel (read-only first; re-score button comes after observability is real). Panel surfaces per-run `NEEDS_HUMAN` and `budget_exceeded` counts with drill-down to `prospect_id` + `phase` + `last_error`. Status displays distinguish `completed`, `completed_with_failures`, `circuit_broken` (auth-class halt), and `crashed_resumed`. SharePoint mirror writer with `_attic/<ts>/` backup + atomic rename.
10. **CourtListener weekly reconciliation:** PACER-direct count vs SQLite-ingested count; alert if delta >5%.

### Week 4+ — compounding layer

11. **gstack `/review`** wired on PRs (existing).
12. **Author `/audit`** via `anthropic-skills:skill-creator` — operates on `runs` table + git history.
13. **Implement Apollo conversion write-back** (per 0c spike result).
14. **Author `/calibrate`** only if Apollo loop is real. Operates on aggregates (scoring-bin × conversion-rate × week ≈ 200 rows) not raw rows — never bust the context budget.

## Break-glass requirement

Keep a **documented manual-run path** where the pipeline can execute from the server even if Cloudflare or Hermes is down. The orchestration cannot become the bottleneck for Monday-morning deliverables.

Concrete procedure:

```bash
# SSH to the Linux box as the service user (key-based + MFA-wrapped)
ssh leadgen-svc@<box>

# Run the BK lane directly, bypassing Hermes
cd /opt/tcia-monorepo/apps/tcia-leadgen
./bin/leadgen run --lane bk --apply --no-hermes --output-to ~/breakglass/$(date +%Y%m%d)/
```

If **Codex auth is the failure** (auth-class circuit-break tripped), the break-glass run still fails — there is no offline reasoning fallback for the scoring step. Recovery in that case is: refresh Codex session, re-run. If Codex is unavailable for >24 hrs, ship the prior week's scored prospects from `~/.tcia-leadgen/_backups/` as a stopgap — `leadgen runs export --run-id <last-known-good> --to-csv` produces an Apollo-ready CSV from frozen state.

Every break-glass invocation writes a row to `breakglass_log(ts, user_email, command, exit_code)` for compliance.

## Out of scope (for v1)

- Anything that writes back from SharePoint to SQLite
- Per-user permissioning beyond domain-restricted Cloudflare Access
- COI lanes (port BK first; revisit COI-first sequencing only if BK port reveals it was actually mature)
- Per-environment dev/prod profiles (single-server reality, single-operator)
- Pre-commit hook for n8n credential stripping (n8n is removed from leadgen v1; not needed)

## Success Metrics

**Baseline (per Rob, 2026-05-20):**

- 100% of BK prospects in the weekly list are called
- 0% convert to HubSpot opportunities
- 0% close

**Interpretation:** the current pipeline is 100% effort, 0% return. The disease isn't (only) drift — it's that the qualification signal is bad enough that every called prospect is a wasted dial. Drift is a symptom of weak scoring; the real fix is better scoring evidence-backed by Apollo conversion data.

**v1 ship criteria:**

- **Primary:** at least 1 BK prospect converts to a HubSpot opportunity within the first 4 weeks of the new pipeline. Any non-zero opportunity rate is an improvement over the 0% baseline.
- **Secondary:** reduce called-prospect volume by ≥30% by filtering more aggressively. (Fewer calls + first-non-zero opportunity = directional win.)
- **Tertiary (compounding):** by week 12, `/calibrate` identifies the top-bin and bottom-bin features predicting opportunity conversion; Daniel updates scoring weights based on that signal. Requires Apollo write-back loop (week-0c spike).

**Numeric target lock:** the actual numbers above can sharpen once the Apollo write-back spike (0c) confirms automatic conversion detection. If Apollo's API can't attribute conversions to prospect_ids cleanly, the success criteria reverts to manual quarterly review and `/calibrate` is dropped.

## Data Source SLAs

| Source | Cadence + free-tier limits | Failure detection | Backfill path |
|---|---|---|---|
| **CourtListener** | Free tier 5,000 queries/day. Alerts have variable lag and occasional RSS drops; party-name normalization has edge cases | Weekly reconciliation: count of filings in target chapters/districts vs SQLite-ingested count; alert if delta >5% | Direct PACER query for missing range; document gap in `run-history/` |
| **Apollo** | Sequence-stats API; upload-and-poll vs webhook unknown until week-0c spike | Per-prospect `upload_result(prospect_id, apollo_id, status, error)` row; daily SLA: pending → succeeded within 24 hrs | Manual quarterly review if API path proves infeasible |
| **HubSpot** | Opportunity creation webhook (reused from sdr-engine) | Existing sdr-engine monitor (Module 8 patterns) | Existing |
| **ZoomInfo** | On-demand contact enrichment, US-only | Cache-first atomicity (A2 from `tcia-core`); cache row written BEFORE HubSpot writes, status `'orphan'` on rollback failure | Manual cleanup of `'orphan'` rows |
| **Microsoft Graph (SharePoint)** | Chunked upload session with explicit commit; xlsx written to `.tmp` then atomic rename | mtime/etag check before clobber; existing → copy to `_attic/<ts>/`; last 4 versions retained | `_attic/` rollback |

## v1 Compliance & Privacy

- **Named compliance reviewer:** [NAME or by end of week 2 — `leadgen run --apply --send-outbound` runtime check refuses to send if this slot is empty; dashboard surfaces "compliance owner not assigned" warning until filled]
- **Outreach scope:** business email at **non-debtor entities only** (creditors, suppliers, vendors of the bankrupt entity); never the debtor directly
- **Opt-out path:** documented at `https://leadgen.tcia-tn.com/optout`; honored within 10 business days
- **`do_not_contact` table:** SQLite table with write-only `(email_or_phone, added_at, source, reason)`; queried before every enrichment AND every CSV export
- **Audit trail:** every dashboard mutation writes `triggered_by_email` (from verified Cloudflare Access JWT claim), `action`, `target`, `timestamp` to `audit_log` table
- **Break-glass invocations:** server-side script runs write a row to `breakglass_log(ts, user, command, exit_code)`; SSH access is key-based + MFA-wrapped; off-box log forwarding (so a compromised box can't wipe its own trail)

## Documentation contract

These ship in v1 (mirroring the sdr-engine pattern):

- `README.md` — what it is, who it's for, link to SETUP
- `docs/SETUP.md` — credential walk (Apollo + ZoomInfo + HubSpot + Graph + CourtListener + Cloudflare + Codex auth) with copy-paste-complete blocks
- `docs/RUNBOOK.md` — "if you see X in the logs, run Y" — the 2 AM document
- `docs/ARCHITECTURE.md` — system topology, integration points, decision log
- `CLAUDE.md` + `AGENTS.md` — content-identical except title line, mirroring sdr-engine sync rule. Reflect locked architectural decisions: A1 prompt-injection wrap, A2 cache-first atomicity, Codex auth-class circuit-break, JWT-verified Cloudflare identity contract, Hermes Python-decorator workflow definition, Hermes checkpoint/resume contract
- **CLAUDE.md / AGENTS.md placement (monorepo):** both files live at **repo root** (shared monorepo conventions, the locked architectural decisions, build/test/CI commands) AND **per-app** at `apps/sdr-engine/CLAUDE.md` + `apps/tcia-leadgen/CLAUDE.md` (app-specific orchestrator choice, app-specific dependencies, app-specific deploy targets). Sync rule applies per file: edit root pair together, edit per-app pair together
- **Dual-orchestrator monorepo (v1 intentional):** `apps/sdr-engine` uses n8n + `/scheduler/run`; `apps/tcia-leadgen` uses Hermes + `/scheduler/run`. Cron-slot allocation tracked in `infra/cron-map.md`. Convergence to a single orchestrator is post-v1 work; explicitly accepted as a v1 inconsistency
- `docs/PROMPT_VERSIONING.md` — prompts live in `prompts/*.md` as flat files in git; recover historical via `git show <sha>:prompts/<file>.md`; `leadgen prompts show <name> --at <sha>` is the CLI surface
- `docs/TROUBLESHOOTING.md` — common errors and resolutions
- `docs/DEV.md` — Windows ↔ Linux dev parity (WSL2 / devcontainer / VSCode Remote-SSH — decision deferred to week-0 but documented before week-1 code)

---

# GSTACK REVIEW REPORT

> Generated by `/autoplan` on 2026-05-20.
> Codex CLI unavailable on this host → dual voices degraded to **subagent-only**.
> Source tag: `[subagent-only]` applies to every consensus table below.

## Phase 1 — CEO Review

### 1.1 Premise Audit

| # | Premise (where in plan) | Status | Challenge |
|---|---|---|---|
| P1 | "Drift is prompt-and-logic drift, not file drift" (Problem) | **UNVERIFIED** | No concrete diff examples cited from the last 90 days. Could be a hypothesis dressed as a finding. The real drift may live in human judgment (Will rescoring on Friday), not files. |
| P2 | "SharePoint-downstream eliminates ~90% of drift risk" (sync rule) | **ASSUMED — number is invented** | 90% has no source. If Will's Excel edits today inform next week's prompts via Slack DMs, the artifact moved but the human loop didn't. Drift reappears as `_shared/feedback-this-week.md`. |
| P3 | "Cloudflare Tunnel + Access is free at this scale" + "one-evening job" (employee access) | **PARTLY WRONG** | Free tier is fine ≤50 users. But Access on a custom domain requires Cloudflare nameservers. If `tcia-tn.com` isn't already on CF DNS, this is a week (NS migration + propagation + corporate-domain approval), not an evening. |
| P4 | "Hermes is almost certainly enough" (cloud vs local) | **HEDGED** | "Almost certainly" punts the architectural fork. BK trigger likely needs PDF extraction → debtor norm → creditor lookup → company match → score — a real multi-step chain. Hermes vs OpenClaw is undecided. |
| P5 | "BK trigger is most mature, port first to prove the loop" (next step #4) | **CONTRADICTS ITSELF** | Porting the most-mature lane proves the *least*. Brittle lanes (COI) stay unproven. You burn weeks on the easy one, then hit unknowns with deadline pressure. |
| P6 | "/audit and /calibrate provide compounding value" (gstack role) | **ASSUMED** | Skills don't exist yet. Audit value depends on someone *reading* audit notes — has Will or anyone shown they will? And /audit equivalent is already `git log -p prompts/`. |
| P7 | "Lead-gen should be a separate repo from sdr-engine" (scope note) | **ASSUMED** | Justified only by "may share patterns" — that's an argument FOR a shared repo, not against. Same operator, same server, same domain. Likely diverges in 3 months. |
| P8 | "Apollo conversion data flows back to /calibrate" (gstack role) | **NO MECHANISM** | Apollo API → SQLite write path is undescribed. Without it, /calibrate operates on its own scoring rationale — a hallucination loop. |
| P9 | "Single-user (Daniel) assumptions from sdr-engine transfer" (implicit) | **WRONG** | Lead-gen is multi-stakeholder (Daniel + Will + others). Flask on 127.0.0.1 with no per-user state, single-writer SQLite, no audit-log doesn't fit. |
| P10 | "Will WILL use the dashboard" (employee access) | **WISHFUL** | Will is named once and never characterized. He currently uses Excel. Telling him "your edits don't take" is a *feature removal* without a substitute. |
| P11 | "n8n stays" (out of scope) | **DIVERGENCE RISK** | sdr-engine commit `bf0baf3` just added `/scheduler/run` — moving away from n8n. Locking lead-gen into n8n forks the platform decision. |
| P12 | "Mirror the current SharePoint code" (next step #1) | **AMBIGUOUS** | "Mirror" could mean `git init && cp -r` or a multi-week refactor of Excel-formula logic into Python. Different scope, different risk. |

### 1.2 Strategic Blind Spots (severity-ranked)

| # | Blind spot | Severity | Fix |
|---|---|---|---|
| B1 | **No business metric, no baseline.** Drift framing is a symptom; the disease is unmeasured. What % of weekly list does Will convert to Apollo sequences today? What's the target? | CRITICAL | Before any code, fill: "Today, Will converts __% to Apollo. v1 ships when __% or [measurable]." If you can't fill the first blank, project is premature. |
| B2 | **Will is uncharacterized.** Whole consumption design hinges on a user named once. The Excel edits are signal — half are scoring inputs you're dismissing as drift. | CRITICAL | 30-min walkthrough with Will. Inventory every Excel column he edits and every reason. Classify each: (a) real scoring signal, (b) UI complaint, (c) noise. Build a deliberate channel for (a) before locking SharePoint as read-only. |
| B3 | **`/calibrate`'s Apollo→SQLite write path is undescribed.** Load-bearing for "compounding value." Apollo IDs must be written back to SQLite at upload, then status polled. Neither in plan. | CRITICAL | Design the data path FIRST. If Apollo's API can't support it cleanly (sequence stats tying back to your prospect IDs), kill /calibrate now. |
| B4 | **Single-user assumptions inherited.** Flask 127.0.0.1, single-writer SQLite, no audit log for "who triggered re-score." | HIGH | Add `triggered_by_email` from Cloudflare Access `cf-access-authenticated-user-email` header to every dashboard action. 1 hour; prevents forensic mystery. |
| B5 | **n8n in lead-gen vs sdr-engine migrating off.** Two scheduler stacks, zero leverage between them. | MEDIUM | Either keep n8n in sdr-engine too, or kill n8n in lead-gen v1. Don't fork. |
| B6 | **BK-first sequencing is backwards.** Mature lane = lowest information. COI is where drift actually lives. | MEDIUM | Either port one COI lane first, OR hard-time-box BK port at 1 week and pivot to COI immediately. |
| B7 | **CourtListener fragility unmentioned.** RECAP gaps, party-name normalization disasters, alert lag, 5,000-query/day free tier. | MEDIUM | Add a "data source SLA" section. Backfill strategy when CourtListener misses a filing. |
| B8 | **Compliance owner unnamed.** Enriching bankrupt debtors with personal contact data for outbound is a complaint magnet (CCPA/state laws). | MEDIUM | Name a compliance reviewer. Document opt-out path. |

### 1.3 Existing Code Leverage Map (`sdr-engine` → potential reuse)

| Sub-problem | Existing in `sdr-engine` | Reusable? |
|---|---|---|
| Cloudflare Tunnel + Access setup | `sdr.tradecredit.agency` already live (per CLAUDE.md) | YES — same auth model, same nameserver requirement applies |
| Single-writer SQLite + WAL | `~/.sdr-engine/queue.db` pattern | YES — same library; rename path to `~/.tcia-leadgen/leadgen.db` |
| LiteLLM routing (`local-main`, `heavy-main`) | Already wired | YES — reuse the alias surface, possibly the config file |
| Flask dashboard bound to 127.0.0.1 | `ui/server.py` single-file pattern | YES — same single-file pattern works for the leadgen dashboard |
| Cloud LLM fallback on validation failure | sdr-engine Module 4 (A3 pattern) | YES — port the validate-retry-fallback machinery |
| Cron-driven weekly trigger | sdr-engine scheduler module (commit d42b438, bf0baf3) | YES — `/scheduler/run` webhook is the exact pattern leadgen needs |
| n8n workflow templates | sdr-engine has exported workflows | PARTIAL — orchestrator choice should be reconsidered (see B5) |
| Smoke-test scripts (`init_db.py`, `insert_test_card.py`) | Per commit 670badd, exists in sdr-engine | YES — same pattern for leadgen DB bootstrap |

This map is the strongest argument for **monorepo or shared-library approach** vs separate repo (re: P7).

### 1.4 Dream State Delta

```
CURRENT (today, ~May 2026)
  ├── Lead-gen lives on SharePoint
  ├── Excel + scripts, no diff, no audit
  ├── Will edits Excel; edits inform next week informally
  ├── Apollo conversion data lives in Apollo, never touches scoring
  └── Drift exists; magnitude unmeasured

THIS PLAN (v1, ~3 months)
  ├── Code in `tcia-leadgen` (separate repo) — DECISION PENDING (P7)
  ├── SQLite system of record on Linux server
  ├── Cloudflare Tunnel + Access for Will at leadgen.tcia-tn.com
  ├── BK lane ported; COI lanes deferred
  ├── /review on PRs (gstack)
  ├── /audit, /calibrate proposed but not built
  └── Apollo → scoring loop NOT closed

12-MONTH IDEAL (~May 2027)
  ├── All lanes ported (BK, COI, anything else)
  ├── Apollo conversion data flows back; /calibrate actually tunes weights
  ├── Will's manual Excel edits replaced by structured feedback channel
  ├── Cross-lane lift measurable (weekly converted-to-sequenced %)
  ├── Compliance opt-out path + audit log per outreach
  └── Drift detected pre-merge, not post-run (PR-time /audit)
```

**Gap from THIS PLAN to IDEAL:** Apollo feedback loop, structured Will-input channel, COI lanes, PR-time auditing. The plan doesn't have a story for any of these — they're all "later."

### 1.5 Implementation Alternatives Table

| # | Approach | CC effort | Human effort | Risk | Pros | Cons |
|---|---|---|---|---|---|---|
| A | **As-stated** — separate repo, BK first, n8n stays, dashboard, /audit + /calibrate later | ~30 hrs | ~3 weeks | Moderate | Clean separation; quick first ship | Compounds drift between repos; BK-first proves least; /audit & /calibrate may never ship |
| B | **Monorepo + library** — leadgen as `tcia-leadgen/` directory inside an expanded sdr-engine repo (or extracted shared `tcia-core` lib) | ~25 hrs | ~3 weeks | Lower | Shared LiteLLM, Cloudflare, SQLite patterns; one CLAUDE.md; no divergence | Couples deploy cadence; requires upfront refactor of sdr-engine into modules |
| C | **Minimum-viable, no dashboard** — SQLite → weekly CSV → emailed to Will. No Flask, no Cloudflare for leadgen. Time-box BK port to 1 week, immediately COI. | ~12 hrs | ~1.5 weeks | Lowest | Forces business-metric definition before infra; reveals if Will actually engages | No UI fanciness; harder to justify dashboard later if Will is happy with email |
| D | **Apollo-loop-first** — build the Apollo→SQLite conversion write path before anything else. Only then port BK or build /calibrate. | ~20 hrs | ~3 weeks | Moderate | Validates the load-bearing premise (P8) before depending on it | Postpones visible progress; may surface that the loop is infeasible (which is itself valuable) |

### 1.6 Mode Selection (SELECTIVE EXPANSION)

Per `/autoplan` defaults, mode = SELECTIVE EXPANSION: hold scope on the in-blast-radius items, cherry-pick a few high-leverage expansions.

**Held in scope (don't expand):**
- BK as the first ported lane (but see C7 sequencing — time-box it)
- SQLite as system of record
- gstack `/review` per PR

**Cherry-picked expansions (in blast radius, < 1 day CC effort):**
- **E1:** Add a one-paragraph "v1 success metric" section to the plan (B1 fix). No code. ~10 min.
- **E2:** Add `triggered_by_email` audit-log column to every dashboard action (B4 fix). ~1 hr code.
- **E3:** Add a "data source SLA" subsection for CourtListener (B7 fix). ~15 min plan-only.
- **E4:** Add a "compliance owner + opt-out path" line (B8 fix). ~15 min plan-only.

**Deferred (not now, write to TODOS.md):**
- Apollo→SQLite write path design (B3 fix) — DEFER but block /calibrate on it
- Will walkthrough (B2 fix) — DEFER as week-0 activity, not code
- COI lanes — DEFER per current sequencing
- Replacing n8n — DEFER but flag the divergence risk

### 1.7 NOT in Scope (for v1, with rationale)

| Item | Why deferred |
|---|---|
| Apollo→SQLite conversion-write path (B3) | Load-bearing for /calibrate, but /calibrate itself is week-2 — design path before building /calibrate, not before BK port |
| Will-input structured channel (B2) | Requires 30-min walkthrough first; out of scope for plan, in scope for week-0 prep |
| Replacing n8n (P11) | Acknowledged divergence risk; revisit after first BK port lands |
| COI lanes (B6) | Pending B6 sequencing decision at gate |
| Per-user roles beyond Cloudflare Access | Genuinely fine for v1 — only Daniel and Will, both fully trusted |
| Migrating `tcia-tn.com` nameservers if not already on CF (P3) | Verify state first; if already on CF, the "one evening" claim stands |

### 1.8 Error & Rescue Registry (CEO scope)

| Failure | Detect | Rescue | Owner |
|---|---|---|---|
| BK port misses an undocumented filter from current SharePoint pipeline | Will reports false positives in first 2 weeks | Diff old SharePoint logic against new, restore missing filters | Daniel |
| `tcia-tn.com` not on CF nameservers → "one evening" balloons | Domain registrar lookup | Either migrate NS or use a sub-zone CF DNS; widen timeline | Daniel |
| Will doesn't use dashboard | Weekly access-log check first month | Pivot to emailed CSV; kill dashboard build | Daniel |
| SharePoint mirror sync breaks silently | Weekly check of last-write timestamp | Add liveness monitor; alert if stale > 8 days | Daniel |
| CourtListener misses a filing | Cross-check against PACER weekly | Backfill query; document the gap | Daniel |
| Apollo API doesn't support clean prospect-ID write-back | Spike before /calibrate build | If infeasible, kill /calibrate; pivot to manual quarterly review | Daniel |
| BK debtor data CCPA complaint | Inbound from contacted person | Have opt-out path documented and runnable | Daniel + named compliance owner |

### 1.9 Failure Modes Registry (CEO scope)

| Mode | Likelihood (subagent estimate) | Plan addresses? |
|---|---|---|
| Will doesn't use dashboard | 60% | NO |
| SharePoint mirror goes stale, nobody owns it | 70% | NO |
| Apollo conversion data never flows back | 75% | NO (deferred) |
| BK pipeline more brittle than "mature" implies | 50% | NO |
| Two repos diverge in patterns | 80% | NO (assumed away) |
| /audit and /calibrate never get written | 65% | NO (week-2 = never) |

Five of six unaddressed. This is the strongest case for the **plan needs revision**, not just "approve as-is."

### 1.10 CEO Dual-Voice Consensus Table `[subagent-only]`

```
CEO DUAL VOICES — CONSENSUS TABLE  [codex unavailable; single voice]
═══════════════════════════════════════════════════════════════
  Dimension                            Subagent  Consensus
  ──────────────────────────────────── ────────  ─────────
  1. Premises valid?                   NO        NO (12 challenges)
  2. Right problem to solve?           NO        NO (B1 critical)
  3. Scope calibration correct?        NO        NO (B6 sequencing)
  4. Alternatives sufficiently explored? NO      NO (5 alternatives dismissed)
  5. Competitive/market risks covered? NO        NO (B7, B8, Apollo risk)
  6. 6-month trajectory sound?         NO        NO (5/6 failure modes unaddressed)
═══════════════════════════════════════════════════════════════
Source: subagent-only (codex CLI not installed on this host)
```

### 1.11 CEO Phase Completion Summary

- **Mode:** SELECTIVE EXPANSION
- **Premises challenged:** 12 (3 wrong/contradictory, 7 assumed-without-evidence, 2 hedged)
- **Critical blind spots:** 3 (no business metric, Will uncharacterized, /calibrate data path missing)
- **Cherry-picked expansions accepted:** E1, E2, E3, E4 (all < 1 day CC effort)
- **Deferred to TODOS.md:** Apollo loop design, Will walkthrough, COI lanes, n8n divergence
- **Verdict:** **PLAN NEEDS REVISION before Eng review can be meaningful.** Three critical premise fixes (metric, Will, Apollo loop) must be answered or explicitly accepted-as-risk before Phase 3 architecture work makes sense.

---

## Phase 2 — Design Review

**SKIPPED.** UI scope detection returned 2 keyword matches but no actual UI design content (the vision only mentions "Flask, or even a static page reading from SQLite" — that's an implementation hint, not a design decision). Defer design review until the dashboard scope is real, then invoke `/plan-design-review` directly.

---

## Phase 3 — Eng Review

### 3.1 Architecture Diagram (ASCII)

```
                       ┌─────────────────────────────────────────┐
                       │  Cloudflare Edge (leadgen.tcia-tn.com)  │
                       │  Tunnel terminator + Access (Zero Trust)│
                       │  Injects cf-access-jwt-assertion header │
                       └────────────────┬────────────────────────┘
                                        │ cloudflared tunnel
                                        │ (TLS, no public ingress)
       ╔════════════════════════════════▼══════════════════════════════════╗
       ║         LINUX SERVER  (shared host with sdr-engine)               ║
       ║                                                                   ║
       ║   ┌──────────────┐         ┌─────────────────────────────┐        ║
       ║   │ cloudflared  │────────►│ Flask dashboard              │        ║
       ║   │ (sidecar,    │         │ ui/server.py @ 127.0.0.1:??? │        ║
       ║   │  shared with │         │ JWT verify MISSING [X-JWT]   │        ║
       ║   │  sdr-engine) │         └─────┬──────────────┬─────────┘        ║
       ║   └──────────────┘               │              │ "re-score"      ║
       ║          │                       ▼              ▼ button          ║
       ║          ▼                ┌──────────────┐  ┌──────────────┐      ║
       ║   ┌─────────────────┐     │ SQLite WAL   │  │ trigger      │      ║
       ║   │ n8n web UI      │     │ ~/.tcia-     │◄─┤ /scheduler/  │      ║
       ║   │ (tunneled, L37) │     │  leadgen/    │  │   run ???    │      ║
       ║   └────────┬────────┘     │  leadgen.db  │  └──────┬───────┘      ║
       ║            │              │ SINGLE-WRITER│         │              ║
       ║            ▼              └─▲────▲────┬──┘         │              ║
       ║   ┌─────────────────────────┴────┴────┴────────────┴─────────┐    ║
       ║   │           n8n WORKFLOW LAYER  (or replaced — see D1)     │    ║
       ║   │           Cron + per-lane workflows                      │    ║
       ║   │           JSON exports in git (LOSSY round-trip)         │    ║
       ║   └─┬────────────────┬────────────────┬─────────────┬────────┘    ║
       ║     ▼                ▼                ▼             ▼              ║
       ║  ┌──────┐   ┌─────────────────┐   ┌────────┐  ┌──────────────┐    ║
       ║  │Court-│   │  LiteLLM router │   │ Apollo │  │ SharePoint   │    ║
       ║  │Listr │   │ ┌─────────────┐ │   │ upload │  │ mirror writer│    ║
       ║  │alerts│   │ │qwen2.5:14b  │ │   │ CSV    │  │ (server→SP)  │    ║
       ║  └──────┘   │ │ local       │ │   └────┬───┘  └──────┬───────┘    ║
       ║             │ ├─────────────┤ │        │             │            ║
       ║             │ │Claude cloud │ │        │             ▼            ║
       ║             │ │ FALLBACK    │ │        │      ┌──────────────┐    ║
       ║             │ │ UNDEFINED   │ │        │      │ SharePoint   │    ║
       ║             │ │ [X-FALLBACK]│ │        │      │ Excel mirror │    ║
       ║             │ └─────────────┘ │        │      └──────────────┘    ║
       ║             └─────────────────┘        ▼                          ║
       ║                                  ┌─────────────┐                  ║
       ║                                  │ Apollo cloud│                  ║
       ║                                  │  ??? POLL?  │                  ║
       ║                                  │  WEBHOOK?   │                  ║
       ║                                  │  [X-LOOP]   │                  ║
       ║                                  └──────┬──────┘                  ║
       ║                                         │ no described            ║
       ║                                         ▼ write-back path         ║
       ║                                  ┌─────────────────────────┐      ║
       ║                                  │ /calibrate (not yet     │      ║
       ║                                  │   authored as a skill)  │      ║
       ║                                  └─────────────────────────┘      ║
       ╚═══════════════════════════════════════════════════════════════════╝

   LOAD-BEARING-BUT-VAGUE:
   [X-JWT]      JWT verification on Flask side — security-critical, unspecified
   [X-FALLBACK] LiteLLM fallback contract for leadgen — sdr-engine has A3, leadgen silent
   [X-LOOP]     Apollo → SQLite conversion write-back — entire /calibrate value hinges here

   COUPLING CONCERNS:
   [C1] Flask re-score button shares writer with cron → contention (EC1)
   [C2] n8n cron + sdr-engine's /scheduler/run on same box → no slot-allocation model (EC5)
   [C3] LiteLLM shared with sdr-engine → outage cascades (S3, P4)
   [C4] cloudflared tunnel shared → one cert + config blast radius
   [C5] SharePoint writer can race itself if weekly run overlaps prior sync (EP5)
```

### 3.2 Test Diagram

Plan is vision-stage (no code yet). Minimum test surface required **before week-2** (= before BK lane is "stable enough that diffs are meaningful," per next-steps §5):

| # | Data flow segment | Test type | Status |
|---|---|---|---|
| T1 | CourtListener alert → BK candidate parse | Contract test against fixture payloads + golden-file regression | **GAP** |
| T2 | BK candidate → SQLite insert (idempotent) | Schema migration + upsert idempotency | **GAP** |
| T3 | qwen2.5:14b filtering call | Mocked-LLM unit + JSON schema validation + timeout test | **GAP** |
| T4 | Claude scoring/personalization call | Same as T3 + prompt-injection wrap (per sdr-engine A1) | **CRITICAL GAP** — plan never mentions injection wrapping; CourtListener text is attacker-controllable |
| T5 | Scoring → SQLite write | Property test: every prospect → exactly one row, no orphans (sdr-engine A2 pattern) | **GAP** |
| T6 | SQLite → SharePoint Excel mirror | Integration with Graph API sandbox + diff snapshot | **GAP** |
| T7 | SQLite → Apollo-ready CSV | Golden CSV diff + Apollo schema validation | **GAP** |
| T8 | Apollo upload → conversion data → SQLite | E2E with Apollo sandbox; webhook OR poll test | **CRITICAL GAP** — write-back path doesn't exist in plan |
| T9 | Conversion data → /calibrate | LLM eval suite with frozen prompts + drift assertion | **GAP** (skill not authored) |
| T10 | Cloudflare Access JWT → Flask identity | JWT signature verify + spoofing-rejection test | **CRITICAL GAP** — plan says "email-based auth"; never describes JWT verify |
| T11 | Manual break-glass invocation | Smoke test of `make run-bk` or equivalent | **GAP** (interface unspecified) |
| T12 | Re-score button + cron concurrency | Simultaneous-trigger test | **CRITICAL GAP** — write contention story absent |

**Test artifact** would normally be written to `~/.gstack/projects/$SLUG/leadgen-test-plan-{datetime}.md`. Since lead-gen is a separate repo not yet created, this test plan stays embedded here as Section 3.2.

### 3.3 Critical Edge Cases (Section 3 — full list in audit trail)

| # | Edge case | Severity | Fix summary |
|---|---|---|---|
| EC1 | SQLite single-writer + cron + Will's re-score button → contention | **CRITICAL** | `runs(run_id, status, lock_held)` table; row-level lock; UI returns 409 if run in flight |
| EC2 | "Auto-generated, never hand-edited" SharePoint mirror — enforcement absent | **HIGH** | Set library read-only for non-service users; copy existing to `_attic/<ts>/` before clobber; banner Will if his edits were preserved |
| EC3 | Cloudflare Access header trust on Flask | **CRITICAL** | (1) Bind to 127.0.0.1 only. (2) **Verify `Cf-Access-Jwt-Assertion` JWT** against team JWKS every request. `cf-access-authenticated-user-email` is a convenience, not an identity source. |
| EC4 | LiteLLM fallback for leadgen unspecified (sdr-engine has A3, leadgen silent) | **HIGH** | Reuse sdr-engine Module 4 validate-retry-fallback; `litellm-router-config.yaml` with `leadgen-bulk → [qwen2.5:14b, claude-haiku]`, timeout 30s, 2 retries |
| EC5 | n8n vs `/scheduler/run` ownership collision | **HIGH** | Pick one orchestrator; if keeping both, `infra/cron-map.md` + LiteLLM semaphore |
| EC6 | `_databases/` path ambiguity (repo? home? root?) | **MEDIUM** | Canonical: `~/.tcia-leadgen/leadgen.db`. `.gitignore`. `scripts/init_db.py` |

### 3.4 Security Threat Registry

| # | Threat | Severity | Fix summary |
|---|---|---|---|
| S1 | Cloudflare origin exposure if Flask binds anything other than 127.0.0.1 | **CRITICAL** | 127.0.0.1 bind + host firewall reject non-CF + JWT verify (overlaps EC3) |
| S2 | BK enrichment + CCPA / state law exposure | **HIGH** | Compliance owner named pre-week-1; opt-out path; hard filter to business email at non-debtor entities; `do_not_contact` table |
| S3 | Secret sprawl (Apollo + ZoomInfo + HubSpot + Graph + CourtListener + LiteLLM + Cloudflare) | **HIGH** | `.env` chmod 600 service-user owned; n8n credentials store; CI grep for secret patterns; quarterly rotation runbook |
| S4 | SQLite file = unprotected PII for anyone with shell | **HIGH** | Service-user ownership + chmod 600 + volume-at-rest encryption |
| S5 | Break-glass = SSH? Access matrix undocumented | **HIGH** | Key-based + MFA SSH; off-box audit log; `breakglass_log(ts, user, command, exit_code)` table |
| S6 | Prompt injection via CourtListener public text | **HIGH** | Reuse sdr-engine A1 `<untrusted_input>` wrap pattern; injection-payload regression test |
| S7 | n8n web UI exposed via tunnel — workflow editing + credential rotation surface | **MEDIUM** | Scope CF Access to specific email allowlist (not `@tcia-tn.com` wildcard); hardware-key MFA on n8n admin route |

### 3.5 Performance / Scale Findings

| # | Concern | Severity | Fix summary |
|---|---|---|---|
| P1 | `/calibrate` context budget: 4–8 weeks × 500 prospects × ~400 tokens ≈ 1.6M tokens at $24/run × 52 weeks = $1,250/yr just for one skill, AND may bust the 1M context cap | **HIGH** | `/calibrate` operates on **aggregates** (scoring-bin × conversion-rate × week ≈ 200 rows, <50k tokens, <$1/run). Include only top/bottom-decile sample rows. Lock this in plan. |
| P2 | BK candidate explosion bound (5–10× wave possible) | **MEDIUM** | Hard ceiling per-run LLM spend with circuit-breaker; high-water-mark alert at 2× rolling 4-week avg |
| P3 | qwen2.5:14b throughput unspecified — hardware not in plan | **MEDIUM** | Add "hardware baseline" line: GPU model, VRAM, target tok/s, items/run budget. If no GPU, this whole local-LLM premise collapses |
| P4 | Same box: 2× n8n + LiteLLM + Ollama + 2× Flask + SharePoint sync + Apollo upload | **MEDIUM** | RAM/CPU footprint documented; OpenClaw alert at >80% memory |

### 3.6 Deployment Risk Registry

| # | Risk | Severity | Fix summary |
|---|---|---|---|
| D1 | "Mirror current SharePoint code" is a black box (could be Python / Excel formulas / Power Automate / VBA / PDFs) | **CRITICAL** | Week-0 90-min audit producing `inventory.md` (every file, purpose, language, last-modified, classification: port-verbatim / port-with-rewrite / deprecate / human-only-SOP) |
| D2 | No rollback story | **HIGH** | Shadow-run new pipeline alongside SharePoint for 2 weeks; diff to `run-history/parity-<date>.md`; cutover only after 2 consecutive weeks <5% diff |
| D3 | n8n workflow JSON in git is a known smell (UUIDs change, credentials inline, expression noise, lossy round-trip) | **HIGH** | Either: (a) accept as build artifacts with a stable export tool, or (b) replace n8n with `/scheduler/run`-style code-first (consistent with EC5). At minimum: pre-commit hook strips credentials + normalizes IDs. |
| D4 | "Cloudflare = one evening" — claim untested re: nameservers | **MEDIUM** | Verify `dig NS tcia-tn.com` before scoping. If not on CF, this is a week with corporate-domain approval, not an evening |
| D5 | Two-repo divergence guaranteed (same operator, box, domain, primitives) | **MEDIUM** | Shared `tcia-core` library (Cloudflare middleware + JWT verify + LiteLLM client + SQLite helpers + alembic) OR monorepo with `apps/sdr-engine` + `apps/tcia-leadgen` |

### 3.7 Error Path Registry

| # | Failure | Severity | Detect | Rescue |
|---|---|---|---|---|
| EP1 | CourtListener alert miss (RSS drop, rate limit, party-name filter eats it) | **HIGH** | Weekly reconciliation: PACER-direct count vs SQLite-ingested count; alert if delta >5% | Backfill via direct PACER query; document gap |
| EP2 | Apollo upload silent failure (200-but-empty, row-level 4xx, duplicate 5xx retry) | **HIGH** | Per-prospect `upload_result(prospect_id, apollo_id, status, error)` row | Dashboard surfaces failed/pending; daily SLA on pending→succeeded |
| EP3 | LLM malformed JSON | **HIGH** | sdr-engine A3 pattern: 2 validation failures → cloud LLM; 2 cloud failures → `NEEDS_HUMAN` row | Dashboard surfaces NEEDS_HUMAN count; **never halt the whole run** |
| EP4 | SQLite WAL grows unbounded → disk fills → silent write failures | **MEDIUM** | Monitor `leadgen.db-wal` size; alert at >100MB | `VACUUM` + `PRAGMA wal_checkpoint(TRUNCATE)` weekly; backup snapshots in `_backups/` |
| EP5 | SharePoint mirror partial-write on network drop | **MEDIUM** | xlsx open fails Monday morning | Write to `.tmp` + atomic rename; chunked Graph upload session with explicit commit; keep last 4 versions in `_attic/` |

### 3.8 NOT in Scope (Eng-phase additions)

| Item | Why deferred (with explicit risk owner) |
|---|---|
| n8n workflow export schema-normalization tooling | Deferred — pending D3 decision on n8n vs code-first |
| `tcia-core` shared library | Deferred — pending D5 monorepo-vs-separate-repo decision |
| Per-environment (dev/prod) deploy profiles | Deferred — single-server reality, single-operator. Risk accepted; revisit when 2nd operator joins |
| Backups for SQLite | **NOT acceptable to defer.** Must be in v1. (filed as item in 3.10) |
| OpenClaw integration (memory + CPU + WAL-size alerts) | Deferred — existing OpenClaw infra reusable; not a v1 blocker |

### 3.9 What Already Exists in sdr-engine (Eng-phase reuse map)

| Need in leadgen | sdr-engine source | Reuse path |
|---|---|---|
| Cloudflare Tunnel + Access pattern | sdr.tradecredit.agency live | Replicate cloudflared config; same JWKS team URL |
| SQLite WAL + path convention | `~/.sdr-engine/queue.db`, `init_db.py` (commit 670badd) | Same pattern; rename to `~/.tcia-leadgen/leadgen.db` |
| LiteLLM A3 validate-retry-fallback | Module 4 (commit 9d8c190) | Direct port; rename `local-main` alias → `leadgen-bulk` if needed |
| Prompt injection `<untrusted_input>` wrap (A1) | CLAUDE.md A1 | Direct reuse |
| Cache-first atomicity (A2) | Module 2.5 (commit d88a86f) | Apply same to BK candidate writes |
| `/scheduler/run` webhook | commits d42b438, bf0baf3 | Direct reuse → kills the n8n-vs-code-first question (D1, EC5) if chosen |
| Smoke-test scripts (`init_db.py`, `insert_test_card.py`) | sdr-engine (commit 670badd) | Same pattern for leadgen DB bootstrap |
| OpenClaw monitoring | Existing | Add leadgen alerts to existing dashboard |

This map is **the strongest single argument for sharing primitives** (shared lib OR monorepo) — see D5.

### 3.10 Failure Modes Registry with Critical-Gap Assessment

| Failure mode | Likelihood | Critical gap in plan? |
|---|---|---|
| JWT not verified → Cloudflare bypass via origin reach | Critical-gap-if-shipped | **YES** (T10 / EC3 / S1) |
| Apollo write-back impossible → /calibrate vaporware | Critical-gap | **YES** (T8 / EP2) |
| Prompt injection via CourtListener text | Critical-gap-if-shipped | **YES** (T4 / S6) |
| SharePoint hand-edits get clobbered (no `_attic` backup) | High | **YES** (EC2 / EP5) |
| Re-score race with cron | High | **YES** (EC1 / T12) |
| BK port misses an undocumented filter | High | **YES** (D1 / D2) |
| WAL fills disk silently | Medium | YES (EP4) — no DB backups in plan |
| n8n JSON in git becomes maintenance disaster | Medium | YES (D3) |

**Eight critical or high gaps in the plan, all unaddressed.**

### 3.11 Eng Dual-Voice Consensus Table `[subagent-only]`

```
ENG DUAL VOICES — CONSENSUS TABLE  [codex unavailable; single voice]
═══════════════════════════════════════════════════════════════
  Dimension                            Subagent  Consensus
  ──────────────────────────────────── ────────  ─────────
  1. Architecture sound?               PARTIAL   PARTIAL (3 load-bearing vague)
  2. Test coverage sufficient?         NO        NO (3 critical gaps T4, T8, T10, T12)
  3. Performance risks addressed?      NO        NO (/calibrate budget, qwen hardware)
  4. Security threats covered?         NO        NO (7 threats, 1 critical, 5 high)
  5. Error paths handled?              NO        NO (5 unaddressed, 3 high)
  6. Deployment risk manageable?       NO        NO (D1 SharePoint black box = critical)
═══════════════════════════════════════════════════════════════
Source: subagent-only (codex CLI not installed on this host)
```

### 3.12 Top 5 Architecture Decisions That Must Land Before Code

| # | Decision | Why blocking | Cost to defer |
|---|---|---|---|
| **D1** | **Orchestrator: n8n or `/scheduler/run` code-first** | Picks deployment shape, cron model, workflow versioning, divergence with sdr-engine | Two orchestrators on one box → nobody owns either → 6-month rewrite |
| **D2** | **Repo shape: separate / shared lib / monorepo** | Determines reuse path for Cloudflare middleware, JWT verify, LiteLLM client, SQLite helpers, migration tooling | Two divergent copies of 5+ primitives by month 3 |
| **D3** | **Cloudflare Access identity contract (JWT verify on origin)** | Every audit row, every "Will pressed re-score" trust depends on it. Bolted on later = retroactively untrustworthy audit log | Security incident; rebuilding audit trail from log timestamps |
| **D4** | **Apollo → SQLite write-back path** | `/calibrate` (and the entire compounding-value premise) hinges here. If Apollo API can't cleanly attribute conversions to prospect_ids, /calibrate is hallucination | Build whole pipeline, discover loop can't close, /calibrate dies, drift returns unsolved |
| **D5** | **SharePoint inventory + rollback model** | "Mirror current SharePoint code" (step #1) is undefined work. 3-week estimate unfounded; rollback path doesn't exist | Week-3 cutover misses a SharePoint-only filter → false-negative leads → no revert path → 3 weeks burned |

### 3.13 Eng Phase Completion Summary

- **Architecture verdict:** Directionally correct (SharePoint-downstream, SQLite-truth, Cloudflare Access). But **three load-bearing components are specified as wishes**: JWT verify on Flask, Apollo→SQLite loop, contents of "the SharePoint folder."
- **Test coverage:** Vision-stage doc; zero scaffolding. 4 critical test gaps (T4 injection wrap, T8 Apollo loop, T10 JWT verify, T12 write contention) must land before week-2.
- **Security verdict:** 7 threats identified; 1 critical (JWT/origin), 5 high. None addressed in plan.
- **Performance verdict:** /calibrate's context budget (P1) is a hidden blocker that becomes obvious in week-3.
- **Deployment verdict:** D1 (SharePoint black box) and D2 (no rollback) are the highest unforced errors.
- **Verdict:** **No code should start until the 5 architecture decisions (3.12) are recorded.** Each is 1–2 hours of investigation; collectively they save weeks of recoverable mistakes.

---

## Phase 3.5 — DX Review

> Reviewer framing: this is **internal tooling for a 2-person team (Daniel + Will)**. TTHW < 5 min isn't the metric; what matters is whether **future-Daniel** can pick up the codebase in 6 months without the week-0 setup burning out the operator.

### 3.5.1 Developer Journey Map (9 stages)

| Stage | What Persona A (Daniel, operator) actually does | Plan supports? | Friction (1=low, 5=high) |
|---|---|---|---|
| 1. **Clone** | `git clone tcia-leadgen` | Implied | 1 |
| 2. **Setup** | Provision 13 secrets, install n8n, configure Cloudflare, pull qwen, init SQLite, register Graph app | **NO** | 5 |
| 3. **Run** | Trigger first BK end-to-end run | **NO** | 5 |
| 4. **Debug** | Inspect why prospect X scored low | **NO** | 5 |
| 5. **Modify** | Change a scoring prompt, see diff, re-run on one prospect | PARTIAL (`/review` works; rest doesn't) | 4 |
| 6. **Ship** | PR → `/review` → merge → cron picks up next Monday | PARTIAL | 3 |
| 7. **Observe** | Did Sunday's cron succeed? Any NEEDS_HUMAN? | **NO** | 5 |
| 8. **Upgrade** | Bump qwen, change schema, swap Apollo API version | **NO** | 5 |
| 9. **Handoff** | Hand keys to Persona B, go on leave for 3 weeks | **NO** | 5 |

**Five of nine stages at friction 5/5.**

### 3.5.2 Developer Empathy Narrative — Persona A, day 1

> Monday 9 AM. Clone the new `tcia-leadgen` repo. README is empty — I'm the one writing it. Scroll past the autoplan review and start porting BK code from SharePoint. By 10 I realize I need to decide n8n vs `/scheduler/run`. Park it. By 11 I'm in Cloudflare Zero Trust provisioning `leadgen.tcia-tn.com` and discovering `tcia-tn.com` nameservers aren't on Cloudflare. So much for "one-evening job." Afternoon disappears to NS migration paperwork. By 4 PM I have a SQLite file with one table, no migration tool, and a half-typed `bootstrap.sh`. I haven't pulled qwen. I haven't touched a prompt. Will pings me asking when his Monday list lands. I tell him "next week." I'm not sure that's true.

### 3.5.3 The Setup Cliff (13 implicit credentials, zero documented)

| # | Step | Plan? | Realistic time |
|---|---|---|---|
| 1–2 | Clone + Python env + `pip install -e .` | NO | 5 min |
| 3 | `.env` (Apollo + ZoomInfo + HubSpot + Graph + CourtListener + Cloudflare + LiteLLM + n8n basic-auth) | **NO** | 30–90 min in vendor consoles |
| 4 | SQLite init | NO | 5 min if `init_db.py` ported, 1 hr if not |
| 5 | LiteLLM aliases (`leadgen-bulk`, `leadgen-heavy` or reuse `local-main`?) | **NO** | 30 min — decision pending |
| 6 | `ollama pull qwen2.5:14b` + GPU sanity | NO | 15 min download + 1 hr GPU debug |
| 7 | n8n install OR `/scheduler/run` (D1 unresolved) | **NO** | 30 min if shared with sdr-engine, multi-day if fresh |
| 8 | n8n workflow JSON re-credential (lossy round-trip) | NO | 30 min/workflow |
| 9 | cloudflared cred + Access policy | NO | 1 hr if `tcia-tn.com` already on CF; **5–7 days** if not |
| 10 | Microsoft Graph app: register, scope, cert auth, drive ID | **NO** | 2–4 hr + IT approval |
| 11 | Apollo + sandbox | NO | 30 min |
| 12 | ZoomInfo + IP allowlist | NO | 30 min + sales response |
| 13 | HubSpot OAuth or PAT | NO | 30 min |
| 14 | CourtListener API + alerts | NO | 1 hr |
| 15 | First `make run-bk` or equivalent | **NO** — break-glass undefined | unknown |

**Cliff:** 2–3 working days best case, a week if NS migration needed.

### 3.5.4 Missing Read-Only Developer Toolkit (the "before /audit exists" gap)

| Need | In plan? |
|---|---|
| `leadgen run --lane bk --dry-run` — no side effects | NO |
| `leadgen runs list` / `leadgen runs show <id>` — paginated history | NO |
| `leadgen prompts show bk-score --at <git-sha>` — versioning surface | NO |
| `scripts/diff_prompts.py vMon-vFri` — manual diff before `/audit` exists | NO |
| `scripts/replay_run.py <run_id>` — re-execute historical run, no writes | NO |
| `tests/fixtures/` with CourtListener + Apollo + qwen-output goldens | NO |
| `CONTRIBUTING.md` — "to modify BK scoring prompt: edit `prompts/bk-score.md`, `pytest tests/eval`, commit" | NO |

### 3.5.5 Observability Gap (the 3 AM Sunday test)

**Plan's literal observability mention:** one — "documented manual-run path." That's break-glass, not observability.

**Reusable from sdr-engine (Module 8, commit `d84b385`):** heartbeat, webhook alerts, nightly sweeps, weekly digest. **None mentioned in leadgen plan.**

**Missing from both plan and sdr-engine, needed in leadgen v1:**

- Structured JSON-lines logs to `~/.tcia-leadgen/logs/run-<ts>.jsonl` with `run_id`, `lane`, `prospect_id`, `phase`, `latency_ms`, `tokens_in`, `tokens_out`, `model`, `outcome`
- `runs` table (overlaps eng EC1 fix): `run_id`, `started_at`, `finished_at`, `status`, `prompt_git_sha`, `prospect_count_in`, `prospect_count_out`, `errors_count`, `triggered_by_email`
- Dashboard "Last 10 runs" panel (read-only) — drives Will's trust AND Daniel's debugging

### 3.5.6 Upgrade-Safety Gaps

| Upgrade event | Plan's story | Should be |
|---|---|---|
| `qwen2.5:14b → qwen3.5:32b` | Silent | Frozen eval suite (N=20 golden prospects); promote only on eval pass |
| SQLite schema change | Silent | **Alembic from day 1**, even with 2 tables |
| Apollo API change | Silent | Contract tests via `vcrpy` recorded fixtures |
| Prompt change | `/audit` POST-run | **ALSO pre-merge CI gate** — `/audit` is forensic, eval gate is prophylactic |
| n8n workflow JSON change | Silent | Pre-commit hook strips credentials + normalizes UUIDs (per eng D3) |

**Biggest miss:** no pre-merge gate. Plan catches drift *after* it ships → bad output is on Will's desk before /audit fires.

### 3.5.7 Required Escape Hatches (zero in plan)

| Default | Override needed | Example surface |
|---|---|---|
| qwen2.5:14b for bulk | Force Claude for one prospect | `leadgen run --prospect-id X --force-model claude` |
| LiteLLM auto-fallback | Disable for local-model debug | `--no-fallback` |
| SharePoint mirror writes | Skip for one run | `--skip-sharepoint` |
| Apollo upload | Skip (test mode) | `--skip-apollo` |
| Cron schedule | Manual ad-hoc | `leadgen run --lane bk` |
| Latest prompts | Specific git sha | `--prompts-at <sha>` ← the killer flag for git-versioned prompts |
| All-lanes | Lane-by-lane | `--lane bk` |

### 3.5.8 Dev Parity Gap (Windows + Linux)

Daniel is on Windows; prod is Linux. Plan never addresses this. Three viable choices, plan picks none:

| Strategy | Verdict |
|---|---|
| **WSL2** | Recommended default for Daniel — same shell as prod |
| **Devcontainer** | Good for Persona B if they're on macOS |
| **Only-on-server** (VSCode Remote-SSH) | Pragmatic for 2-person shop |

Path conventions also need a project-wide rule: `pathlib.Path.home()`, never hardcoded forward-slash paths.

### 3.5.9 DX Scorecard

| # | Dimension | Score (0-10) | Justification |
|---|---|---|---|
| D1 | Time to first successful run (Persona A) | **2** | 13+ credentials, 3 unmade decisions, zero setup doc, manual n8n re-credentialing |
| D2 | Time to onboard Persona B | **2** | No CLI, no fixtures, no `--dry-run`, no run history. /audit and /calibrate aspirational |
| D3 | Error message quality + observability | **1** | Plan has literally zero observability story |
| D4 | CLI / API ergonomics | **1** | No CLI specified; Flask dashboard is a stakeholder surface, not a developer one |
| D5 | Documentation | **2** | README, SETUP, RUNBOOK, ARCHITECTURE, CLAUDE.md, AGENTS.md, PROMPT_VERSIONING, TROUBLESHOOTING — none specified |
| D6 | Upgrade path safety | **2** | No alembic, no eval suite, no pre-merge prompt gate, no contract tests |
| D7 | Dev environment parity | **2** | Windows-Linux gap entirely unaddressed |
| D8 | Escape hatches | **2** | Defaults opined, override mechanisms absent |

**Composite: 14/80 = 18%.** Strategy is sound; operationalization is essentially empty.

### 3.5.10 Magical Moments — Realistic Probability

| # | Moment | Likelihood (0-10) | Why |
|---|---|---|---|
| M1 | Daniel pushes prompt change, `/review` shows clear diff before merge | 7 | `/review` is existing gstack; works day-one of CI |
| M2 | `/audit` surfaces "scoring prompt changed; bin distribution shifted +12%" | 3 | Skill doesn't exist; depends on `runs` table not in plan |
| M3 | `/calibrate` output causes Daniel to actually change a scoring weight | **2** | Requires Apollo→SQLite write-back (D4 critical); 75% likelihood in plan's own failure register that this never flows back |
| M4 | Will logs in Monday, sees fresh BK leads, doesn't ping Daniel | 6 | Achievable IF `_attic/` backup + read-only ACL added (EC2) |
| M5 | `leadgen run --lane bk --dry-run --prospect-id X` to debug a false-positive | 1 | CLI doesn't exist |
| M6 | 3 AM Sunday failure pings Daniel's phone with structured error | 2 | No observability |
| M7 | Persona B clones, runs `bootstrap.sh`, BK running locally in <2 hrs | 1 | No bootstrap, no setup doc, no fixtures |

**Plan invests in aspirational moments (`/calibrate` closing the loop) and underinvests in foundational ones (CLI, runs table, bootstrap, structured logs).** Foundation enables aspiration; the inverse isn't true.

### 3.5.11 DX Dual-Voice Consensus Table `[subagent-only]`

```
DX DUAL VOICES — CONSENSUS TABLE  [codex unavailable; single voice]
═══════════════════════════════════════════════════════════════
  Dimension                            Subagent  Consensus
  ──────────────────────────────────── ────────  ─────────
  1. Getting started < 5 min?          NO (~2-3d) NO  (calibrated for 2-person internal team)
  2. API/CLI naming guessable?         N/A (no CLI) FAIL — no CLI specified
  3. Error messages actionable?        NO        NO (no observability story)
  4. Docs findable & complete?         NO        NO (8 docs unspecified)
  5. Upgrade path safe?                NO        NO (no eval gate, no alembic)
  6. Dev environment friction-free?    NO        NO (Windows↔Linux gap)
═══════════════════════════════════════════════════════════════
Source: subagent-only (codex CLI not installed on this host)
```

### 3.5.12 Top 5 DX Decisions To Lock Before Code

| # | Decision | Why blocking |
|---|---|---|
| **DX-1** | **Monorepo with `apps/sdr-engine` + `apps/tcia-leadgen` + `packages/tcia-core`** | **Both Eng and DX subagents independently converge here.** One CLAUDE.md, one CI, one `bootstrap.sh`, one set of conventions. Persona B inherits ONE mental model |
| **DX-2** | **CLI binary + verbs** (`leadgen run`, `leadgen runs`, `leadgen prompts`, `leadgen db`, `leadgen ui`) | Single highest-leverage day of design. Every later operational hour goes through this CLI. `--dry-run` default in interactive context; `--apply` for cron |
| **DX-3** | **Orchestrator: code-first `/scheduler/run` (retire n8n from leadgen v1)** | DX argument: n8n JSON in git is known-bad (lossy, UUIDs change, credentials inline). sdr-engine bf0baf3 is the convergent answer |
| **DX-4** | **Observability stack: port sdr-engine Module 8 directly** | `runs` table + heartbeat + webhook alerts + nightly sweep + weekly digest. Without this, every debug session is `tail -f` + `sqlite3` + intuition |
| **DX-5** | **Prompt versioning + pre-merge eval gate** | The whole *premise* of the project (kill drift) fails without this gate. Frozen eval in `tests/eval/`; CI runs on every PR touching `prompts/` |

### 3.5.13 DX Implementation Checklist (4-week roadmap)

**Week 0 (before any pipeline code):**

- [ ] Decide monorepo shape (DX-1)
- [ ] Create `tcia-leadgen` repo (or `apps/tcia-leadgen` in monorepo)
- [ ] Write `README.md`, `docs/SETUP.md` (15 credential steps as copy-paste blocks), `scripts/bootstrap.sh`
- [ ] Write `CLAUDE.md` + `AGENTS.md` with sync rule (mirror sdr-engine pattern)
- [ ] Write `docs/ARCHITECTURE.md` (steal from §3.1 diagram)
- [ ] Decide Windows dev parity (WSL2 / devcontainer / remote-SSH) — document in `docs/DEV.md`
- [ ] Verify `dig NS tcia-tn.com`

**Week 1 (foundational mechanics):**

- [ ] `scripts/init_db.py` (port from sdr-engine 670badd)
- [ ] **Alembic configured from day 1**
- [ ] `leadgen` CLI scaffold (Click/Typer) with `run`, `runs`, `prompts`, `db`, `ui` verbs
- [ ] `runs` table schema
- [ ] Structured JSON-lines logging
- [ ] `--dry-run` default for interactive use; `--apply` for cron
- [ ] `tests/fixtures/` with CourtListener + Apollo + qwen-output goldens

**Week 2 (port BK lane, with rails):**

- [ ] Port BK behind `leadgen run --lane bk`
- [ ] Implement A1 `<untrusted_input>` wrap (port from sdr-engine)
- [ ] Implement A3 LiteLLM validate-retry-fallback (port from sdr-engine Module 4)
- [ ] Frozen eval suite under `tests/eval/test_bk_score.py` with N=20 golden prospects
- [ ] **Pre-merge CI gate** on PRs touching `prompts/`
- [ ] Heartbeat + webhook alerts (port sdr-engine Module 8)

**Week 3 (cutover prep + observability):**

- [ ] Shadow-run new BK alongside SharePoint for 2 weeks; diff to `run-history/parity-<date>.md`
- [ ] Cloudflare tunnel + Access on `leadgen.tcia-tn.com`
- [ ] Flask dashboard with "Last 10 runs" panel (read-only first; re-score button later)
- [ ] **JWT verify on Flask** (`Cf-Access-Jwt-Assertion` against team JWKS) — non-negotiable
- [ ] `triggered_by_email` audit-log written from JWT claim
- [ ] SharePoint mirror writer with `_attic/<ts>/` backup + atomic rename
- [ ] `docs/RUNBOOK.md` — the 2 AM document

**Week 4+ (the compounding layer):**

- [ ] Author `/audit` skill via `anthropic-skills:skill-creator`
- [ ] Design Apollo → SQLite write-back path; if infeasible, kill `/calibrate` and document
- [ ] Author `/calibrate` skill only if Apollo loop is real; operate on aggregates not raw rows
- [ ] Weekly digest email + nightly sweep for orphan/NEEDS_HUMAN rows

### 3.5.14 DX Phase Completion Summary

- **TTHW (Persona A → first successful run):** ~2–3 days, possibly 1 week if NS migration needed. **Target after fixes:** ~2 hrs.
- **TTHW (Persona B → first productive PR):** 1+ week with current plan. **Target after fixes:** 1 day.
- **Composite DX score:** 14/80 = 18%
- **Highest-leverage intervention:** port sdr-engine's CLAUDE.md + Module 8 observability + Module 4 fallback machinery on day 1, AND decide monorepo. Makes leadgen *feel like* sdr-engine. Zero context-switching cost for Daniel; one mental model for Persona B.
- **Verdict:** strategy sound; operational substrate empty. All eight DX dimensions score ≤2/10.

---

## Cross-Phase Themes

| Theme | Phases flagging it | Severity |
|---|---|---|
| **Monorepo / shared `tcia-core` lib** instead of separate repo | CEO (P7), Eng (D5), DX (DX-1) | **Three-phase consensus** — highest-confidence signal in the entire review |
| **Retire n8n from leadgen, use `/scheduler/run`** | CEO (P11), Eng (D1, D3), DX (DX-3) | Three-phase consensus |
| **Apollo → SQLite write-back path is load-bearing but undescribed** | CEO (P8, B3), Eng (T8, EP2, D4), DX (M3) | Three-phase consensus — the whole "compounding value" premise hinges here |
| **No observability / 3 AM Sunday failure invisible** | Eng (EP1–EP4), DX (D3, M6) | Two-phase consensus |
| **JWT verify on Flask, not header trust** | Eng (EC3, S1), DX (Week-3 checklist) | Two-phase consensus, security-critical |
| **Will is uncharacterized; SharePoint mirror enforcement missing** | CEO (B2), Eng (EC2, EP5) | Two-phase consensus |
| **Port sdr-engine Module 8 + Module 4 patterns** | Eng (3.9 reuse map), DX (DX-4) | Two-phase consensus, highest reuse leverage |

**Six themes spanning multiple phases.** Each one is a high-confidence signal because two or three independent reviews surfaced it.

---

## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected alternative |
|---|---|---|---|---|---|---|
| 1 | CEO | Mode = SELECTIVE EXPANSION | Mechanical | P3 | Plan needs work but bones are right; expand selectively | SCOPE EXPANSION (too ambitious for now), HOLD SCOPE (misses critical premise gaps), SCOPE REDUCTION (premature) |
| 2 | CEO | Accept E1 (add business-metric section) | Mechanical | P2 (boil lakes), P1 (completeness) | In blast radius, <10 min effort | Defer (would leave B1 unaddressed) |
| 3 | CEO | Accept E2 (`triggered_by_email` audit-log column) | Mechanical | P1, P2 | <1 hr code, fixes a HIGH-severity blind spot (B4) | Defer (would leave audit log retroactively untrustworthy) |
| 4 | CEO | Accept E3 (data source SLA subsection for CourtListener) | Mechanical | P1 | 15 min plan-only fix for B7 | Defer (would mean no detect-mechanism for missed filings) |
| 5 | CEO | Accept E4 (compliance owner + opt-out path line) | Mechanical | P1 | 15 min plan-only fix for B8/S2 | Defer (would leave CCPA exposure unowned) |
| 6 | CEO | Defer Apollo→SQLite design (B3) to TODOS.md | Taste | P3 (pragmatic) | Required for /calibrate but NOT a v1 BK-pipeline blocker | Block code on it (would stall first ship) |
| 7 | CEO | Defer Will walkthrough (B2) — week-0 prep, not plan content | Mechanical | P3 | It's an activity, not a plan section | Block plan approval on it |
| 8 | CEO | Defer COI lanes | Mechanical | P2 (blast radius), P3 | Out of BK-first scope; legitimately week-2+ | Re-sequence COI-first (close call — see TASTE in Final Gate) |
| 9 | CEO | Skip Phase 2 (Design) | Mechanical | P3 | Marginal 2-keyword UI scope; no actual UI design content to review | Run Phase 2 anyway (would produce empty output) |
| 10 | Eng | Tag all Eng findings critical/high — none auto-dismissed | Mechanical | P1 (completeness) | All 6 categories have unaddressed issues | Auto-dismiss low-likelihood ones (would hide real gaps) |
| 11 | Eng | Recommend Apollo loop design BEFORE building /calibrate | Mechanical | P5 (explicit over clever) | Load-bearing piece; design-before-build saves throwaway work | Build /calibrate optimistically (would discover infeasibility after sunk cost) |
| 12 | Eng | Recommend `127.0.0.1` + JWT verify (not header trust) | Mechanical | P1 (completeness) | Security-critical; no real alternative | Trust the header (insecure) |
| 13 | Eng | Flag n8n-vs-`/scheduler/run` as D1 (must decide pre-code) | Taste | P5 | Both viable; deferring guarantees divergence | Auto-decide either way (taste call, surface at gate) |
| 14 | Eng | Flag monorepo-vs-separate as D2 (must decide pre-code) | Taste | — | High-impact, reasonable disagreement | Auto-decide separate (would dismiss strong subagent argument) |
| 15 | DX | Recommend monorepo (3-phase consensus) | Taste → arguably User Challenge | P1, P2 | Both subagents + CEO premise all flag it. User explicitly said "leadgen is a new repo separate from sdr-engine" | — |
| 16 | DX | Recommend retiring n8n from leadgen (3-phase consensus) | Taste → arguably User Challenge | P5 | All phases flag; user's plan explicitly preserves n8n | — |
| 17 | DX | Port Module 8 observability on day 1 | Mechanical | P1, P2 | Reusable from sdr-engine; addresses 5/9 friction-5 stages | Defer (would mean no observability at v1 ship) |
| 18 | DX | Add `--dry-run` default for interactive CLI use | Mechanical | P5 (explicit) | Standard pattern; prevents 500-row Apollo upload accidents | Default to `--apply` (dangerous default) |
| 19 | DX | Adopt alembic from day 1 | Mechanical | P5 | Cost is 10× higher to add later (to live data) | Defer until "needed" (will be too late) |
| 20 | DX | Add JWT verify to Week-3 critical checklist | Mechanical | (security) | Non-negotiable for the security model | Defer (origin exposure risk) |

---

## Cherry-Picked Plan Edits (Accepted Expansions)

These are the CEO-phase E1–E4 + safest Eng/DX adds. All are < 1 day CC effort, all in blast radius. They should be added directly to the plan body above. Surfaced here for visibility at the Final Gate:

- **E1:** Add `## Success Metrics` section. "Today, Will converts ___% of weekly BK list to Apollo sequences. v1 ships when ___% or [other measurable]." Blank-but-named.
- **E2:** Add line under Architecture > Cloudflare: "Every dashboard mutation records `triggered_by_email` from the verified `Cf-Access-Jwt-Assertion` JWT claim, not the convenience header."
- **E3:** Add `## Data Source SLAs` subsection: CourtListener free tier = 5,000 queries/day; weekly reconciliation alerts at >5% delta vs PACER baseline; backfill via direct PACER query on miss.
- **E4:** Add line to `## Out of scope`: "Compliance owner: [TBD name] is named pre-week-1. Documented opt-out path before first outbound. Hard filter to business email at non-debtor entities; `do_not_contact` table."
- **E5 (eng):** Add line under Architecture: "Flask binds 127.0.0.1 only. Cloudflare JWT (`Cf-Access-Jwt-Assertion`) verified against team JWKS on every request — `cf-access-authenticated-user-email` header is NOT trusted as identity source."
- **E6 (eng):** Add "Backup policy" line to Out of scope's complement (i.e., IN scope): "Weekly SQLite backup snapshot to `_backups/` retained for 8 weeks; restore-from-backup runbook documented."
- **E7 (DX):** Add `## Documentation contract` line: "README, SETUP.md, RUNBOOK.md, ARCHITECTURE.md, CLAUDE.md, AGENTS.md ship in v1. `CLAUDE.md` and `AGENTS.md` content-identical except for title, sync rule from sdr-engine."

These edits will be applied to the plan body once the Final Gate approves the overall review (see end of document).

---

## DECISIONS APPLIED (Final Gate, 2026-05-20)

| # | Decision | Choice | Impact |
|---|---|---|---|
| 1 | Repo shape | **A — Monorepo** with `apps/sdr-engine`, `apps/tcia-leadgen`, `packages/tcia-core` | Resolves CEO P7, Eng D5, DX DX-1. Scope note + Concrete next steps updated |
| 2 | Orchestrator | **A — Drop n8n; Hermes orchestrates via `/scheduler/run`** | Resolves CEO P11, Eng D1/D3/EC5, DX DX-3. "LLM stack and orchestration" section rewritten; n8n line removed from Out of scope |
| 3 | Cloudflare Access identity | **A — 127.0.0.1 bind + JWT verify + `triggered_by_email` audit** | Resolves Eng EC3/S1. Employee access section updated; v1 Compliance & Privacy section added |
| 4 | Apollo → SQLite write-back | **A — Week-0c spike** (1–2 hrs); kill `/calibrate` if infeasible | Resolves CEO B3, Eng T8/D4/EP2. Step 0c added; Success Metrics conditional on spike result |
| 5 | SharePoint inventory | **A — Week-0b 90-min audit** | Resolves Eng D1. Step 0b added |
| 6 | Business metric baseline | **C — Custom**: 100% called / 0% opp / 0% closed today; v1 = first non-zero opportunity in 4 weeks + 30% fewer calls | Resolves CEO B1. Success Metrics section added with baseline + v1 + tertiary |
| 7 | Rob walkthrough | **N/A** — Rob does not edit SharePoint-gathered data; policy enforceable. | Resolves CEO B2 / Eng EC2. "The sync rule that kills drift" section updated; "Rob" replaces "Will" in body |

### Architectural shifts beyond the 7 decisions

1. **LiteLLM removed from the stack.** Codex is the default reasoning model for all leadgen LLM calls.
2. **3 local models are scoped to OpenClaw's internal use only.** Not directly callable from leadgen code; leadgen calls OpenClaw which routes internally.
3. **Hermes is the workflow orchestrator.** Receives `/scheduler/run` cron, runs the multi-step BK lane, calls Codex for reasoning and OpenClaw for agentic sub-tasks.

These shifts resolve Eng EC4 (LiteLLM fallback unspecified — no longer applies), simplify the secret surface (Eng S3 reduced — no LiteLLM creds), and introduce new validation surfaces handled in Phase 5 below: Codex auth + retry policy, per-run cost ceiling now that bulk routes to Codex, Hermes production-readiness for a 5-step pipeline.

### Cherry-picked expansions applied to plan body

- E1 → `## Success Metrics` section ✓
- E2 + E5 → JWT verify + `triggered_by_email` language in Employee access section ✓
- E3 → `## Data Source SLAs` section ✓
- E4 → `## v1 Compliance & Privacy` section ✓
- E6 → Backup policy added to Week-1 step #2 (alembic + weekly snapshots) ✓
- E7 → `## Documentation contract` section ✓

### Risk-accepted items (re-litigate post-v1)

- COI lane port — BK-first sequencing kept; revisit if BK port reveals it was actually mature
- Per-environment dev/prod profiles — single-server reality
- Compliance owner **name** still pending (must be filled before first outbound)
- Hermes maturity for a 5-step orchestration — to be validated in Phase 5 below

---

## Phase 5 — Re-Review Post-Decisions

> Re-review focused on the 3 architectural shifts (Codex default, Hermes orchestrator, monorepo) plus general validation of the updated plan body. Subagent-only (codex CLI unavailable).
>
> **Outcome: 27 prior findings → 3 real blockers + 7 fix-in-the-plan-now edits.** The bones are right; three structural validations and a half-page of edits stand between "vision" and "go."

### 5.1 Three Blockers (require pre-code work)

| # | Blocker | Fix | Effort |
|---|---|---|---|
| **PB1** | **Hermes maturity validation** — the BK lane is 6 steps with a mid-run sub-call to OpenClaw. That's exactly the "multi-step agent chain" case the user previously said wasn't Hermes's strong suit. Risk-accepted line (above) deferred this to Phase 5; Phase 5 cannot self-validate Hermes. | Spike: run a 5-step Hermes toy pipeline with a sub-call to OpenClaw and a forced mid-run crash. If unstable, pick a fallback (`/scheduler/run` + thin Python state machine in `tcia-core`, OR keep n8n for leadgen and accept dual-orchestrator monorepo). | ½ day |
| **PB2** | **Codex cost calibration** — circuit-breaker is decorative without numbers. Bulk CourtListener filtering now routes to Codex (was qwen2.5:14b free local), and at 500 prospects × ~5–6 calls × 4k-in/1k-out tokens, weekly cost is roughly **$100–$300 steady-state and $1,500/wk on a 5–10× wave**. | Spike: run BK lane on 20 fixture prospects, extrapolate, fill in concrete `$X per run / $Y rolling alert / $Z hard breaker` numbers in the LLM stack section. Bonus: sub-budget by phase (`alert_filter_cap`, `enrichment_cap`, `scoring_cap`) so a cheap-upstream spike doesn't starve expensive-downstream. | ½ day |
| **PB3** | **Monorepo migration continuity sequence** — sdr-engine is live at `sdr.tradecredit.agency`. 19 files import from `sdr_engine.*`, the cross-host `/scheduler/run` webhook (commit bf0baf3) is pinned to a deploy path, cloudflared origin host:port is wired. 0d as written ("convert sdr-engine repo to monorepo shape") is closer to 2–4 days of careful work + 1 day regression-finding than "1 day prep." | Write a Migration Sequencing subsection: (1) branch monorepo in worktree, run sdr-engine tests in new layout; (2) parallel deploy on `sdr-monorepo.tradecredit.agency`; (3) cut over n8n webhook target; (4) decom old layout. Tie to a no-downtime invariant: external webhook URLs are part of public API; conversion must preserve route paths verbatim. | 1–2 hrs to write the sequence; the migration itself is 2–4 days |

### 5.2 Seven Fix-In-The-Plan-Now Edits

| # | Edit | Severity | Where to add |
|---|---|---|---|
| **PE1** | **Codex auth-class failure carve-out.** Current retry-once policy conflates per-call failures (transient, retry-once OK) with systemic auth failures (401/403/quota — should circuit-break the whole run, not write 500 NEEDS_HUMAN rows). Add: "If 2 consecutive Codex failures share an auth-class error (401/403/quota), trip the circuit breaker immediately, halt the run, fire the webhook alert. The 'never halt the run' rule is for per-prospect failures, not systemic ones." | HIGH | LLM stack and orchestration section, after "Codex auth + retry contract" |
| **PE2** | **NEEDS_HUMAN dashboard surfacing.** Plan commits to writing NEEDS_HUMAN rows but never says how Rob sees them Monday morning. Add a line to Week-3 step #9: "Dashboard panel surfaces NEEDS_HUMAN and budget_exceeded counts per run with drill-down to `prospect_id` + phase + last_error." | MEDIUM | Concrete next steps → Week 3 step #9 |
| **PE3** | **OpenClaw health signal.** Local-model failure no longer cascades to leadgen *directly*, but leadgen's enrichment quality silently degrades if OpenClaw can't reach a local model. Add to Week-2 step #7 observability: "Per-run telemetry includes `openclaw_calls_failed` count; OpenClaw's own health (memory, queue depth) surfaced in `runs` table or sibling `openclaw_health` table." | MEDIUM | Concrete next steps → Week 2 step #7 |
| **PE4** | **Hermes workflow definition format.** Plan says Hermes workflows live in git but never says in what format. If Python decorators → diffable, testable, good. If YAML/JSON DSL → may rebuild the n8n problem. Specify Python-based and add `workflow_registry` decorator helpers to `packages/tcia-core` content list. | HIGH | Week 1 step #1 (`tcia-core` content list) + Hermes orchestration section |
| **PE5** | **Hermes crash-mid-run resume contract.** What happens if Hermes crashes between step 3 (score) and step 4 (brief) for prospect 247? Specify: each step writes `(prospect_id, run_id, phase, status, output_hash)` BEFORE the next step starts. Resume = "select prospects where phase < target and run_id = current." Add as `checkpoint_helpers` to `packages/tcia-core` content list. | HIGH | `tcia-core` content list + LLM stack and orchestration section |
| **PE6** | **`tcia-core` content adjustments.** Add: Hermes workflow registry + checkpoint helpers (per PE4, PE5), `do_not_contact` table primitives, `audit_log` table primitives, cost-ceiling/circuit-breaker primitive. Remove: per-app smoke scripts (`init_db.py`, `insert_test_card.py`) — they're per-app today; only the generic `init_db(db_path, migrations_path)` helper belongs in core. | MEDIUM | Week 1 step #1 |
| **PE7** | **Compliance owner deadline + dual-orchestrator monorepo statement + CLAUDE.md placement + break-glass update.** Four small text fixes: (a) Compliance Owner `[TBD]` becomes `[NAME or by week-2; send blocked if empty]` with a runtime check in `leadgen run --apply --send-outbound`. (b) Documentation contract gets 1 line: "Monorepo is intentionally dual-orchestrator in v1: `apps/sdr-engine` uses n8n + `/scheduler/run`; `apps/tcia-leadgen` uses Hermes + `/scheduler/run`. Cron-slot allocation in `infra/cron-map.md`." (c) Documentation contract gets 1 line: "`CLAUDE.md` and `AGENTS.md` live both at repo root (shared monorepo conventions) and per-app under `apps/<app>/CLAUDE.md` (app-specific decisions). Sync rule per file." (d) Break-glass requirement section: replace "Cloudflare or n8n is down" with "Cloudflare or Hermes is down"; add a concrete 3-line procedure naming the CLI flags. | LOW–MEDIUM | Various sections (Compliance & Privacy, Documentation contract, Break-glass requirement) |

### 5.3 What Got Better (Decisions Closed These)

- **CEO B1** (no business metric) — closed by Success Metrics section
- **CEO B2** (Will uncharacterized) — closed by Rob policy + sync-rule update
- **CEO B3** (Apollo loop missing) — closed by Week-0c spike commitment
- **CEO B4** (single-user assumptions) — closed by JWT-verified `triggered_by_email`
- **Eng D1** (orchestrator collision) — closed by retiring n8n from leadgen
- **Eng D5** (repo divergence) — closed by monorepo
- **Eng EC3, S1** (Cloudflare origin / header trust) — closed by 127.0.0.1 + JWT verify
- **Eng EC4** (LiteLLM fallback unspecified) — obsoleted by removing LiteLLM
- **Eng S2** (CCPA) — closed by Compliance & Privacy section (modulo PE7 deadline)
- **Eng S3** (secret sprawl) — partially closed (LiteLLM cred removed, Codex is the single LLM cred)
- **DX DX-1, DX-3, DX-4** (monorepo, retire n8n, observability) — all closed

### 5.4 What Got Introduced by the Architectural Shifts

- **Hermes is load-bearing.** A new dependency with the exact risk profile (multi-step + sub-process) the user previously flagged as not Hermes's strength → PB1.
- **Codex is the single LLM credential.** Good for simplicity, bad for blast radius on auth expiry → PE1.
- **Live-traffic monorepo migration.** Bigger than 1 day → PB3.
- **Bulk filtering now metered.** Cheap-upstream Codex usage can starve expensive-downstream → PB2 + PE6 sub-budget.

### 5.5 Phase 5 Verdict

```
Phase 5 — Re-review: 27 findings → 3 blockers + 7 in-plan edits.
═══════════════════════════════════════════════════════════════
  Status:               APPROACHING code-ready
  Real blockers:        3 (Hermes spike, Codex cost spike, monorepo continuity)
  In-plan edits:        7 (~2 hrs to apply)
  Decisions worked?     YES — 11 CRITICAL/HIGH findings from original review closed
  Introduced new risk?  YES — Hermes is the single biggest new dependency
═══════════════════════════════════════════════════════════════
Recommendation: 1–2 days of plan iteration + 1 day of spikes before code starts.
  Then: code-ready.
```

### 5.6 PE1–PE7 Applied (2026-05-20)

| # | Edit | Where applied |
|---|---|---|
| PE1 | Codex auth-class circuit-break carve-out | LLM stack section — "Codex auth + retry contract" expanded into per-call vs auth-class clauses |
| PE2 | NEEDS_HUMAN dashboard surfacing | Week-3 step #9 — panel surfaces NEEDS_HUMAN + budget_exceeded with drill-down; status taxonomy added |
| PE3 | OpenClaw health signal | Week-2 step #7 — `openclaw_calls_failed` count + `openclaw_health` sibling table |
| PE4 | Hermes Python-decorator workflow format | LLM stack section ("Hermes orchestration contract") + Week-1 step #1 (`workflow_registry` decorators in `tcia-core`) |
| PE5 | Hermes checkpoint/resume contract | LLM stack section ("Hermes crash-resume contract") + Week-1 step #1 (`checkpoint_helpers` in `tcia-core`) |
| PE6 | `tcia-core` content list adjustments | Week-1 step #1 — content list rewritten with 11 named primitives; per-app smoke scripts removed; do_not_contact + audit_log + cost-ceiling primitives added |
| PE7 | Four small text fixes | (a) Compliance owner deadline + runtime check in v1 Compliance & Privacy; (b) Dual-orchestrator statement in Documentation contract; (c) CLAUDE.md placement rule (root + per-app) in Documentation contract; (d) Break-glass updated ("n8n" → "Hermes" + concrete 3-line procedure) |

### 5.7 Remaining work (the 3 blockers)

Status: spike plans written and committed; execution still pending where runtime access is required.

| # | Status | Doc | Execution requires |
|---|---|---|---|
| PB1 — Hermes spike | **PLAN COMPLETE** — execution pending | [PB1-hermes-spike.md](PB1-hermes-spike.md) | Linux box with Hermes + OpenClaw installed; ½ day |
| PB2 — Codex cost spike | **PLAN COMPLETE** — execution pending | [PB2-codex-cost-spike.md](PB2-codex-cost-spike.md) | Codex CLI on PATH + 20 fixture BK prospects + Codex billing access; ½ day |
| PB3 — Monorepo continuity sequence | **Phase 1 DONE** (branch `monorepo-spike` @ `074ebfa`, 244 tests + ruff green); Phases 2–4 pending on-box | [PB3-monorepo-continuity.md](PB3-monorepo-continuity.md) | Box access for parallel cloudflared + systemd edits; ~10 hrs across 2–3 calendar days |

After PB1–PB3 execute successfully, the plan is code-ready and `/ship` creates the first PR (monorepo conversion + `apps/tcia-leadgen/` scaffold).

**If a spike fails**, each doc names its fallback explicitly:
- PB1 fail → code-first state machine in `tcia-core` (~1 day; pattern already exists in sdr-engine)
- PB2 expensive → route bulk filter to Haiku, OR reintroduce OpenClaw-routed local qwen for bulk
- PB3 has rollback to the old layout in under 5 minutes per Phase 3



