# PB3 — Monorepo Migration Continuity Sequence

> Pre-code blocker #3 from `/autoplan` Phase 5 review of [leadgen-vision.md](leadgen-vision.md).
> Owner: Daniel. Status: planned, not executed.

## Why this exists

`sdr-engine` is live at `https://sdr.tradecredit.agency`. The cross-host `/scheduler/run` webhook (commit `bf0baf3`) is pinned in n8n's workflow JSON on a different host. 19 files in this repo import from `sdr_engine.*`. CI assumes the package lives at repo root. Naive monorepo conversion ("just move sdr_engine into apps/") risks breaking the live webhook, the cloudflared tunnel, and CI — all simultaneously, with no documented rollback.

This document is the sequenced cutover.

## Inventory of pinned contracts (must survive the migration)

| Contract | Pinned at | Where the pin lives |
|---|---|---|
| Public URL `https://sdr.tradecredit.agency/scheduler/run` | n8n host (different machine) | `n8n/workflows/sdr-scheduler.json:24` mirrors the URL the n8n host calls; the actual pin is on the n8n host |
| Public URL `https://sdr.tradecredit.agency/queue*` (review UI routes) | Browser bookmarks, no pinned external callers | `ui/server.py:130–303` |
| Public URL `https://sdr.tradecredit.agency/health` | Cloudflare Access health check (probably) | `ui/server.py:364` |
| Cloudflare tunnel origin | cloudflared config on Linux box | `CLOUDFLARE_TUNNEL_HOSTNAME=sdr.tradecredit.agency`, origin → `127.0.0.1:5679` |
| Flask bind | systemd unit / supervisor `ExecStart=` (not in repo) | `REVIEW_UI_HOST=127.0.0.1`, `REVIEW_UI_PORT=5679` |
| Package import path | 19 files (`sdr_engine/*` + `tests/*` + `scripts/*` + `ui/server.py`) | `from sdr_engine.X import Y` |
| CI install | `.github/workflows/test.yml` | `pip install -e ".[dev]"` at repo root |
| Pytest test discovery | `pyproject.toml` | `testpaths = ["tests"]` |
| Setuptools package discovery | `pyproject.toml` | `[tool.setuptools.packages.find] include = ["sdr_engine*"]` |
| `~/.sdr-engine/queue.db` path | `sdr_engine/queue_ops` (and anything that opens it) | Configurable via env? Check before moving |
| LiteLLM endpoint | `LLM_ENDPOINT=http://localhost:4000/v1/chat/completions` | `.env.example:1`; lives in LiteLLM config on box, not in this repo |
| Cron entries for scheduler | OS cron on Linux box (not in repo) | unknown — check `crontab -l` on box during prep |

**The invariant:** external callers see no change. URL paths, origin bind, queue.db path, pytest invocation — all stay byte-identical.

## What changes vs. what stays

| Stays the same | Changes |
|---|---|
| `https://sdr.tradecredit.agency/*` URLs | Filesystem layout (`sdr_engine/` → `apps/sdr-engine/sdr_engine/`) |
| `127.0.0.1:5679` bind | Repo root from `sdr-engine` → `tcia-monorepo` (or keep same repo, restructure in place) |
| `~/.sdr-engine/queue.db` path | Package install layout — workspace root + per-app `pyproject.toml` |
| `from sdr_engine.X import Y` imports (within sdr-engine app code) | CI working directory + install commands |
| n8n workflow JSON (no edits) | `WorkingDirectory=` and `ExecStart=` in systemd/supervisor |
| CourtListener / Apollo / ZoomInfo / HubSpot creds | Nothing about creds |

The import path inside `apps/sdr-engine/sdr_engine/` stays `sdr_engine.X` — the new layout is `apps/sdr-engine/sdr_engine/` with `apps/sdr-engine/` being the package root that pyproject.toml in that directory points to. Imports stay flat.

## Migration sequence (4 phases, ~3 days)

### Phase 1 — Worktree branch (Day 1, AM)

Goal: validate the new layout passes all tests without touching the live deploy.

```bash
# In the live sdr-engine repo on the dev machine
git fetch origin
git worktree add ../sdr-engine-monorepo monorepo-spike

cd ../sdr-engine-monorepo

# Convert in place
mkdir -p apps/sdr-engine packages/tcia-core
git mv sdr_engine apps/sdr-engine/sdr_engine
git mv tests apps/sdr-engine/tests
git mv ui apps/sdr-engine/ui
git mv scripts apps/sdr-engine/scripts
git mv n8n apps/sdr-engine/n8n
git mv sql apps/sdr-engine/sql
git mv prompts apps/sdr-engine/prompts
git mv config apps/sdr-engine/config
git mv pyproject.toml apps/sdr-engine/pyproject.toml
git mv .env.example apps/sdr-engine/.env.example

# Create workspace-root pyproject.toml + uv workspace marker
# (uv is the recommended workspace tool; pip workspaces don't exist)
cat > pyproject.toml <<'EOF'
[tool.uv.workspace]
members = ["apps/*", "packages/*"]
EOF

# Create packages/tcia-core scaffold (empty placeholder, no code yet)
cat > packages/tcia-core/pyproject.toml <<'EOF'
[project]
name = "tcia-core"
version = "0.0.1"
description = "Shared primitives for sdr-engine and tcia-leadgen"
requires-python = ">=3.11"

[tool.setuptools.packages.find]
include = ["tcia_core*"]
EOF
mkdir -p packages/tcia-core/tcia_core
touch packages/tcia-core/tcia_core/__init__.py

# Update CI: working dir for sdr-engine tests
# Edit .github/workflows/test.yml — see Phase 1 sub-step below
```

**Phase 1 sub-step — CI update:** edit `.github/workflows/test.yml` so the pytest step runs in the app dir:

```yaml
      - name: Install
        working-directory: apps/sdr-engine
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
      - name: Test
        working-directory: apps/sdr-engine
        run: pytest -v
```

**Phase 1 acceptance:**
```bash
cd apps/sdr-engine && pip install -e ".[dev]" && pytest -v
# All 22 tests pass identically to pre-migration baseline
```

If any test fails, stop here. Investigate before going further. **Do not proceed to Phase 2 until Phase 1 acceptance is green.**

### Phase 2 — Parallel deploy on sibling subdomain (Day 1 PM – Day 2 AM)

Goal: prove the new layout runs in production-shape on a sibling URL, with zero risk to the live one.

```bash
# On the Linux box, clone the monorepo branch to a separate path
cd /opt
git clone -b monorepo-spike <repo-url> tcia-monorepo
cd tcia-monorepo/apps/sdr-engine

# Install in a fresh venv
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Copy live .env (do NOT bind to the same port)
cp /opt/sdr-engine/.env .env
# Edit: REVIEW_UI_PORT=5680  (sibling port; the live one is 5679)

# Smoke-test in the new layout
python -c "from sdr_engine.scheduler import run as r; print('import OK')"
pytest -v

# Run the Flask server in the sibling layout
python -m sdr_engine.ui_entry  # or however the app starts in this repo
# Confirm: bind on 127.0.0.1:5680, all routes respond
```

**Set up sibling cloudflared tunnel:**

```bash
# Add to cloudflared config (on the box, in /etc/cloudflared/config.yml or similar)
# Existing entry:
#   - hostname: sdr.tradecredit.agency
#     service: http://127.0.0.1:5679
# Add new entry pointing to port 5680:
#   - hostname: sdr-monorepo.tradecredit.agency
#     service: http://127.0.0.1:5680

# In Cloudflare dashboard:
# 1. Add DNS record sdr-monorepo → tunnel
# 2. Add Cloudflare Access policy mirroring the live one (same email allowlist)

# Restart cloudflared
sudo systemctl restart cloudflared
```

**Phase 2 acceptance:**
```bash
# From any browser (logged in to Cloudflare Access)
curl https://sdr-monorepo.tradecredit.agency/health
# 200 OK

# Trigger /scheduler/run manually
curl -X POST https://sdr-monorepo.tradecredit.agency/scheduler/run \
  -H "X-Scheduler-Auth: $SCHEDULER_AUTH_TOKEN" \
  -d '{}'
# Returns same shape as live one (dry-run mode if you can; if not, accept that one extra Module 1 run happens)
```

If sibling works, Phase 2 is done. **Live traffic is still on the old layout — no risk.**

### Phase 3 — Cutover (Day 2 PM, 30-minute window)

Goal: swap live traffic from old layout to monorepo layout. Reversible in under 5 minutes.

**Pre-cutover checklist:**
- [ ] Phase 1 + Phase 2 acceptance both green
- [ ] Pick a low-traffic window (not during a weekly cron run; check `crontab -l`)
- [ ] Confirm `~/.sdr-engine/queue.db` path is identical in both layouts (it's in `$HOME`, not the repo, so it's shared — both layouts read/write the same DB)
- [ ] Stage the cron cutover edit (don't apply yet — see step 4)

**Cutover steps:**

```bash
# 1. Stop the live Flask service (5 sec downtime starts)
sudo systemctl stop sdr-engine-ui   # or whatever the unit is named

# 2. Update systemd unit to point at the monorepo layout
sudo nano /etc/systemd/system/sdr-engine-ui.service
# Edit:
#   WorkingDirectory=/opt/tcia-monorepo/apps/sdr-engine
#   ExecStart=/opt/tcia-monorepo/apps/sdr-engine/.venv/bin/python -m sdr_engine.ui_entry
sudo systemctl daemon-reload

# 3. Start in the new layout
sudo systemctl start sdr-engine-ui

# 4. Update cloudflared config: point sdr.tradecredit.agency at 127.0.0.1:5680
#    (or change the new layout to bind 5679; either works)
sudo nano /etc/cloudflared/config.yml
sudo systemctl restart cloudflared

# 5. Update cron entries that reference the old path
crontab -l > /tmp/crontab.bak
crontab -e
# Change any path from /opt/sdr-engine/... to /opt/tcia-monorepo/apps/sdr-engine/...

# Total downtime: ~30 sec if smooth.
```

**Phase 3 acceptance (within 5 min of cutover):**

```bash
# Health check
curl https://sdr.tradecredit.agency/health
# 200 OK, version banner shows new commit sha

# Trigger /scheduler/run from the n8n host (or simulate from box)
curl -X POST https://sdr.tradecredit.agency/scheduler/run \
  -H "X-Scheduler-Auth: $SCHEDULER_AUTH_TOKEN" \
  -d '{}'
# Same response shape as before

# Tail logs for 2 minutes — confirm no errors
tail -f /var/log/sdr-engine/*.log
```

**Rollback (<5 min) if anything is wrong:**
```bash
sudo systemctl stop sdr-engine-ui
# Revert systemd unit edit (use `git diff` on /etc if you tracked it, or restore from backup)
# Revert cloudflared config
# crontab /tmp/crontab.bak
sudo systemctl start sdr-engine-ui
sudo systemctl restart cloudflared
# Now back on old layout. Investigate.
```

### Phase 4 — Decom old layout (Day 3, can wait 1–2 weeks)

Only do this after the new layout has run a full weekly cron cycle successfully.

```bash
# On the box
sudo rm -rf /opt/sdr-engine.old  # if you renamed during cutover
# Or just leave it — disk is cheap. Decom = update docs to say "monorepo is canonical."

# In the repo (which is now `tcia-monorepo` conceptually but lives in this same git history)
# Merge monorepo-spike to main
git checkout main
git merge --no-ff monorepo-spike
# Tag the migration
git tag v0.1.0-monorepo
git push origin main --tags

# Now `apps/tcia-leadgen/` can be added in a follow-up PR
```

## Specific risks and mitigations

| Risk | Mitigation | Probability |
|---|---|---|
| Cron job fires mid-cutover, run fails on missing path | Pick a low-traffic window; explicitly check `crontab -l` for the next 2 hrs of triggers | LOW |
| `~/.sdr-engine/queue.db` path resolves differently between layouts (e.g., relative path in one) | Phase 2 acceptance grep for hardcoded paths; if any are relative-to-cwd, fix them in Phase 1 | LOW-MEDIUM |
| systemd unit doesn't reload cleanly (Python venv path drift) | Phase 2 catches this — sibling deploy uses the new venv pattern before cutover | LOW |
| cloudflared config syntax error → tunnel down | Test the config with `cloudflared tunnel ingress validate` before reloading | LOW |
| 19-file import grep missed a dynamic import (e.g., `importlib.import_module("sdr_engine.X")`) | Phase 1 pytest catches; supplement with `grep -r "sdr_engine" --include="*.py"` for string references | MEDIUM |
| LiteLLM endpoint changes when sdr-engine moves | LiteLLM runs on the box outside the repo; not affected by this migration | NONE |
| n8n workflow JSON references stale path in a comment | Update during Phase 2 prep; comments in JSON are harmless to runtime but confusing | LOW |
| `pip install -e ".[dev]"` in Phase 1 fails because new pyproject.toml has wrong package-find pattern | Test in Phase 1 — `pyproject.toml` under `apps/sdr-engine/` should mirror the original's `include = ["sdr_engine*"]` | LOW |

## Files that need editing (concrete list)

```
NEW:    pyproject.toml                                    # workspace root, uv workspace marker
NEW:    packages/tcia-core/pyproject.toml                 # placeholder
NEW:    packages/tcia-core/tcia_core/__init__.py          # placeholder
MOVE:   sdr_engine/  →  apps/sdr-engine/sdr_engine/
MOVE:   tests/       →  apps/sdr-engine/tests/
MOVE:   ui/          →  apps/sdr-engine/ui/
MOVE:   scripts/     →  apps/sdr-engine/scripts/
MOVE:   n8n/         →  apps/sdr-engine/n8n/
MOVE:   sql/         →  apps/sdr-engine/sql/
MOVE:   prompts/     →  apps/sdr-engine/prompts/
MOVE:   config/      →  apps/sdr-engine/config/
MOVE:   pyproject.toml → apps/sdr-engine/pyproject.toml
MOVE:   .env.example → apps/sdr-engine/.env.example
EDIT:   .github/workflows/test.yml                        # working-directory: apps/sdr-engine
EDIT:   CLAUDE.md (root and apps/sdr-engine/)             # per Documentation contract: root + per-app
EDIT:   AGENTS.md (same)
EDIT:   README.md                                         # mention monorepo shape
KEEP:   docs/                                             # at repo root; references both apps
KEEP:   .claude/                                          # at repo root
KEEP:   .gitignore                                        # extend to cover packages/*/.venv
KEEP:   .env.example for new tcia-leadgen at apps/tcia-leadgen/.env.example (future PR)
ON-BOX: /etc/systemd/system/sdr-engine-ui.service         # WorkingDirectory + ExecStart
ON-BOX: /etc/cloudflared/config.yml                       # origin port (only if port changes)
ON-BOX: crontab                                           # any path that references /opt/sdr-engine
```

## Acceptance criteria (the migration is done when)

- [ ] `apps/sdr-engine/` exists at the new layout; all 22 tests pass; CI green
- [ ] `https://sdr.tradecredit.agency/health` returns 200 from the new layout
- [ ] `https://sdr.tradecredit.agency/scheduler/run` returns the same response shape as pre-migration (verified against a recorded fixture)
- [ ] A full weekly cron cycle completes in the new layout without alerts
- [ ] cloudflared tunnel uptime ≥ 99.9% during the cutover window
- [ ] CLAUDE.md + AGENTS.md exist at repo root AND at `apps/sdr-engine/`; sync rule documented
- [ ] `packages/tcia-core/` exists as an empty package, ready for primitives to be extracted in follow-up PRs
- [ ] `apps/tcia-leadgen/` can be added in a follow-up PR without further restructuring

## Estimated effort

- Phase 1 (worktree branch + validation): 4 hrs
- Phase 2 (parallel deploy + sibling cloudflared): 4 hrs (includes one box reboot for cloudflared)
- Phase 3 (cutover): 30 min window + 1 hr observation
- Phase 4 (decom): 1 hr, can defer 1–2 weeks
- **Total active work: ~10 hrs across 2–3 calendar days**

Day 1 PM is the highest-risk window. Schedule it when neither the weekly cron nor the n8n host has scheduled triggers, and when Daniel has 2 uninterrupted hours.
