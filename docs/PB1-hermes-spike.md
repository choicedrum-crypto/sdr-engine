# PB1 — Hermes Multi-Step Spike

> Pre-code blocker #1 from `/autoplan` Phase 5 review of [leadgen-vision.md](leadgen-vision.md).
> Owner: Daniel. Status: spike plan ready; **execution requires a Linux box with Hermes + OpenClaw installed**.

## Why this exists

The BK lane is 6 steps with a mid-run sub-call to OpenClaw:

```
alert → filter → enrich → score → brief → upload
                   ↑
                   └── calls OpenClaw to "find parent company across 3 sources"
                       (OpenClaw routes internally to local model)
```

This is the **multi-step agent chain** case that earlier framing flagged as not Hermes's strength. Phase 5 of `/autoplan` re-review tagged this as the single highest new dependency risk. Before Hermes is committed as the leadgen v1 orchestrator, validate three behaviors:

1. **Can Hermes register and run a 6-step Python-decorator workflow end-to-end?**
2. **Can a Hermes step make a synchronous sub-call to OpenClaw, wait for its result, and resume?**
3. **What does a mid-run crash look like — does Hermes resume from the last successful checkpoint, or does it restart the whole workflow?**

Pass = Hermes ships as the leadgen orchestrator. Fail (any of the three) = pick fallback (see "If the spike fails" section).

## What you need before starting

- Hermes installed on the box (or in a dev container)
- OpenClaw installed and reachable from Hermes
- A scratch SQLite file you can write checkpoints to (`/tmp/hermes-spike.db`)
- Python 3.11+
- ~½ day uninterrupted

If any of those aren't ready, the spike isn't runnable yet. Do the prereq install first, separately from this spike.

## The spike — a toy 6-step "fake BK lane"

The toy mirrors the real BK lane shape with mock data and synthetic delays. No CourtListener, no Codex, no Apollo. Pure orchestration validation.

### Step 1 — Write the toy workflow

Create `~/hermes-spike/toy_bk_lane.py`:

```python
"""
Toy BK lane to validate Hermes for the leadgen pipeline.
Six steps. One calls OpenClaw. One is forced to crash mid-workflow.
Checkpoints to SQLite. Resume must skip already-completed steps.
"""
import os
import sqlite3
import time
import hashlib
from pathlib import Path

# Replace these imports with the actual Hermes + OpenClaw client signatures.
# This is a sketch — adjust to whatever Hermes's decorator API actually looks like.
from hermes import workflow, step
from openclaw_client import openclaw

DB = Path(os.environ["HERMES_SPIKE_DB"])  # /tmp/hermes-spike.db

def checkpoint(prospect_id: str, run_id: str, phase: str, output: dict):
    """Write checkpoint row before the next step starts."""
    conn = sqlite3.connect(DB)
    output_hash = hashlib.sha256(str(output).encode()).hexdigest()[:16]
    conn.execute(
        "INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?, 'done', ?, ?)",
        (prospect_id, run_id, phase, output_hash, time.time()),
    )
    conn.commit()
    conn.close()

def is_already_done(prospect_id: str, run_id: str, phase: str) -> bool:
    """Check if this (prospect, run, phase) already completed."""
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT 1 FROM checkpoints WHERE prospect_id=? AND run_id=? AND phase=? AND status='done'",
        (prospect_id, run_id, phase),
    ).fetchone()
    conn.close()
    return row is not None

@workflow(name="toy_bk_lane")
class ToyBKLane:

    @step(name="alert", retries=1)
    def alert(self, prospect_id: str, run_id: str):
        if is_already_done(prospect_id, run_id, "alert"):
            return {"skipped": True}
        time.sleep(0.1)
        output = {"prospect_id": prospect_id, "court_alert_payload": f"mock-alert-{prospect_id}"}
        checkpoint(prospect_id, run_id, "alert", output)
        return output

    @step(name="filter", after=["alert"], retries=1)
    def filter(self, prospect_id: str, run_id: str, alert_payload: dict):
        if is_already_done(prospect_id, run_id, "filter"):
            return {"skipped": True}
        time.sleep(0.1)
        keep = int(prospect_id) % 2 == 0  # filter out odd IDs
        output = {"keep": keep}
        checkpoint(prospect_id, run_id, "filter", output)
        return output

    @step(name="enrich", after=["filter"], retries=1)
    def enrich(self, prospect_id: str, run_id: str, filter_result: dict):
        if not filter_result["keep"]:
            return {"skipped": True, "reason": "filtered_out"}
        if is_already_done(prospect_id, run_id, "enrich"):
            return {"skipped": True}

        # THIS IS THE CRITICAL CALL: sub-call to OpenClaw inside a Hermes step.
        parent_company = openclaw.call(
            sub_agent="find_parent_company",
            payload={"prospect_id": prospect_id},
            timeout=30,
        )

        output = {"parent_company": parent_company}
        checkpoint(prospect_id, run_id, "enrich", output)
        return output

    @step(name="score", after=["enrich"], retries=1)
    def score(self, prospect_id: str, run_id: str, enrich_result: dict):
        if enrich_result.get("skipped"):
            return {"skipped": True}
        if is_already_done(prospect_id, run_id, "score"):
            return {"skipped": True}

        # FORCED CRASH: when CRASH_AT_PROSPECT env var matches, raise mid-step.
        if os.environ.get("CRASH_AT_PROSPECT") == prospect_id:
            raise RuntimeError(f"FORCED CRASH at score for prospect {prospect_id}")

        time.sleep(0.2)
        output = {"score": hash(prospect_id) % 100}
        checkpoint(prospect_id, run_id, "score", output)
        return output

    @step(name="brief", after=["score"], retries=1)
    def brief(self, prospect_id: str, run_id: str, score_result: dict):
        if score_result.get("skipped"):
            return {"skipped": True}
        if is_already_done(prospect_id, run_id, "brief"):
            return {"skipped": True}
        time.sleep(0.1)
        output = {"brief_md": f"# Brief for {prospect_id}\nScore: {score_result['score']}"}
        checkpoint(prospect_id, run_id, "brief", output)
        return output

    @step(name="upload", after=["brief"], retries=1)
    def upload(self, prospect_id: str, run_id: str, brief_result: dict):
        if brief_result.get("skipped"):
            return {"skipped": True}
        if is_already_done(prospect_id, run_id, "upload"):
            return {"skipped": True}
        time.sleep(0.1)
        output = {"apollo_id": f"mock-apollo-{prospect_id}"}
        checkpoint(prospect_id, run_id, "upload", output)
        return output
```

### Step 2 — Initialize the checkpoint DB

```bash
mkdir -p ~/hermes-spike
cd ~/hermes-spike
export HERMES_SPIKE_DB=/tmp/hermes-spike.db

sqlite3 "$HERMES_SPIKE_DB" <<'EOF'
CREATE TABLE IF NOT EXISTS checkpoints (
  prospect_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  status TEXT NOT NULL,
  output_hash TEXT,
  completed_at REAL,
  PRIMARY KEY (prospect_id, run_id, phase)
);
EOF
```

### Step 3 — Mock the OpenClaw sub-call (if needed)

If you can't reach a real OpenClaw instance during the spike, create a mock in `~/hermes-spike/openclaw_client.py`:

```python
"""Mock OpenClaw client for the Hermes spike."""
import time
import random

class openclaw:
    @staticmethod
    def call(sub_agent: str, payload: dict, timeout: int = 30):
        # Simulate a 2-5 second network call to OpenClaw
        time.sleep(random.uniform(2, 5))
        if random.random() < 0.05:
            raise TimeoutError(f"OpenClaw timeout for sub_agent={sub_agent}")
        return {"parent_company": f"MockCorp-{payload['prospect_id']}"}
```

Once Hermes can call OpenClaw in production, swap this for the real client. **Run the spike with both the mock AND the real client** if both are available — the mock proves orchestration, the real one proves integration.

### Step 4 — Run the three validation cases

#### Case A — Clean end-to-end run

```bash
# Generate a fresh run_id
export RUN_ID="spike-$(date +%s)"

# Run 10 prospects, no crash
hermes run toy_bk_lane \
  --workflow ~/hermes-spike/toy_bk_lane.py \
  --prospects "1,2,3,4,5,6,7,8,9,10" \
  --run-id "$RUN_ID" \
  --db "$HERMES_SPIKE_DB"
```

**Expected output:**
- 10 prospects processed
- 5 filtered out (odd IDs) — should show `skipped: filtered_out` from `enrich` onward
- 5 reach `upload` step
- Wall-clock time: ~3 seconds per prospect (mostly the mocked OpenClaw delay), so ~15 sec total (sequential) or ~5 sec (parallel)

**Pass criteria:**
```bash
sqlite3 "$HERMES_SPIKE_DB" "SELECT phase, COUNT(*) FROM checkpoints WHERE run_id='$RUN_ID' GROUP BY phase;"
# Expect:
#   alert|10
#   filter|10
#   enrich|5
#   score|5
#   brief|5
#   upload|5
```

If any phase has fewer rows than expected, **Hermes failed to execute the workflow correctly**. Fail Case A.

#### Case B — Mid-run crash + resume

```bash
# Same RUN_ID — DO NOT regenerate it; we want Hermes to resume the partial run
export CRASH_AT_PROSPECT="6"  # prospect 6 will crash at `score` step

# Start with a clean DB
rm "$HERMES_SPIKE_DB"
sqlite3 "$HERMES_SPIKE_DB" < <(cat <<'EOF'
CREATE TABLE checkpoints (
  prospect_id TEXT, run_id TEXT, phase TEXT, status TEXT,
  output_hash TEXT, completed_at REAL,
  PRIMARY KEY (prospect_id, run_id, phase)
);
EOF
)

export RUN_ID="spike-crash-$(date +%s)"

# First run — will crash on prospect 6 at `score`
hermes run toy_bk_lane \
  --workflow ~/hermes-spike/toy_bk_lane.py \
  --prospects "1,2,3,4,5,6,7,8,9,10" \
  --run-id "$RUN_ID" \
  --db "$HERMES_SPIKE_DB" \
  || echo "Crashed as expected"

# Inspect partial state
sqlite3 "$HERMES_SPIKE_DB" "SELECT prospect_id, phase FROM checkpoints WHERE run_id='$RUN_ID' ORDER BY prospect_id, phase;"
```

**Expected partial state (depending on Hermes's per-prospect ordering):**
- Prospects 1–5 either complete (if Hermes runs prospects in parallel) or partially complete (if sequential and Hermes halts on first crash)
- Prospect 6: has rows for `alert`, `filter`, `enrich` but NOT `score`
- Prospects 7–10: depends on Hermes's halt policy

**Resume run:**
```bash
unset CRASH_AT_PROSPECT  # remove the crash trigger

hermes run toy_bk_lane \
  --workflow ~/hermes-spike/toy_bk_lane.py \
  --prospects "1,2,3,4,5,6,7,8,9,10" \
  --run-id "$RUN_ID" \
  --db "$HERMES_SPIKE_DB" \
  --resume
```

**Pass criteria for Case B (all three must hold):**
1. **No double-execution of completed steps.** Phases that already have a `'done'` row should NOT re-run. Verify by checking checkpoint timestamps: completed-phase rows from the first run should NOT have their `completed_at` updated by the resume run.
2. **All 5 keep-prospects (even IDs) reach `upload`.** Same `SQL COUNT(*)` check as Case A.
3. **No duplicate Apollo IDs.** Each prospect's `upload` step writes exactly one row. Check:
   ```bash
   sqlite3 "$HERMES_SPIKE_DB" "SELECT prospect_id, COUNT(*) FROM checkpoints WHERE phase='upload' AND run_id='$RUN_ID' GROUP BY prospect_id HAVING COUNT(*) > 1;"
   # Should return zero rows.
   ```

If Hermes re-runs completed steps on resume → **fail**. This means Codex tokens get paid twice + Apollo gets uploaded twice. Unacceptable.
If Hermes can't resume from a partial run at all → **fail**.

#### Case C — Sub-call to OpenClaw with timeout

```bash
# Force OpenClaw to time out 100% of the time
# (Edit the mock to ALWAYS raise TimeoutError)
# Run 5 prospects
export RUN_ID="spike-timeout-$(date +%s)"

hermes run toy_bk_lane \
  --workflow ~/hermes-spike/toy_bk_lane.py \
  --prospects "2,4,6,8,10" \
  --run-id "$RUN_ID" \
  --db "$HERMES_SPIKE_DB"
```

**Pass criteria for Case C:**
- All 5 prospects fail at `enrich` (because OpenClaw times out)
- Each prospect's `alert` and `filter` phases ARE checkpointed
- `enrich` phase does NOT have a checkpoint row (because the step never completed)
- Hermes returns a non-zero exit code AND surfaces which step failed for which prospects
- No prospects reach `score`/`brief`/`upload`

If Hermes silently writes a `'done'` checkpoint for the timed-out enrich step → **fail**. The leadgen pipeline will think OpenClaw enriched prospects that it didn't.

### Step 5 — Acceptance checklist

| Case | What's validated | Pass? |
|---|---|---|
| A | End-to-end workflow with Python-decorator definition runs cleanly | [ ] |
| A | Conditional skip logic (`filter` → downstream skip) works as written | [ ] |
| A | Checkpoint rows match expected counts per phase | [ ] |
| B | Resume from partial run does NOT re-execute completed steps | [ ] |
| B | All non-crashed prospects complete on resume | [ ] |
| B | No duplicate `upload` rows | [ ] |
| C | OpenClaw timeout fails the step cleanly, no false-positive checkpoint | [ ] |
| C | Hermes exit code is non-zero on step failure | [ ] |
| C | Hermes surfaces step + prospect that failed | [ ] |
| ALL | Workflow definition is diff-able Python (not lossy JSON / DSL) | [ ] |
| ALL | Wall-clock time is reasonable (no obvious deadlocks) | [ ] |

**All boxes ticked → Hermes passes. Ship it as leadgen orchestrator.**

## If the spike fails

Fail = any Case A, B, or C criterion fails OR you can't get past install/integration.

**Fallback plan:** code-first state machine in `packages/tcia-core`, using the `/scheduler/run` webhook as the entry point.

```
cron → POST /scheduler/run → tcia_core.workflows.bk_lane.run(run_id)
                              → for each prospect:
                                  state machine walks the 6 steps
                                  checkpoints to SQLite between each
                                  on crash, restart resumes from checkpoint
```

This is the same pattern `sdr_engine.scheduler` already uses for the SDR Reactivation Engine's scheduling. The fallback is ~200 lines of Python in `tcia_core/workflows/state_machine.py`, ~1 day of work.

The fallback is uglier (no decorators, no built-in observability beyond what you write), but it's known-working code in the sdr-engine family. **The fallback is acceptable for v1 if Hermes fails.** Do not block the project on Hermes maturity.

Document the decision in `apps/tcia-leadgen/CLAUDE.md`:

> Orchestrator: ~~Hermes~~ → `tcia_core` state machine (Hermes spike failed Case X on 2026-MM-DD; revisit post-v1).

## Estimated effort

- Spike setup (write toy workflow + mock client): 2 hrs
- Run the three cases: 1 hr
- Document results + decision: 1 hr
- **Total: ~½ day**

If the spike fails and you fall back: add ~1 day to implement the state machine. Total worst case: 1.5 days.
