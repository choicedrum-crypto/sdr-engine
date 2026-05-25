# PB2 — Codex Cost Calibration Spike

> Pre-code blocker #2 from `/autoplan` Phase 5 review of [leadgen-vision.md](leadgen-vision.md).
> Owner: Daniel. Status: spike plan ready; **execution requires Codex CLI on PATH and 20 fixture BK prospects**.

## Why this exists

Previous vision: bulk CourtListener alert filtering ran on **`qwen2.5:14b` locally** — free per-token, slow but unmetered.

Current vision (post-`/autoplan` Final Gate): bulk filtering routes to **Codex** by default. All 6 LLM-using BK lane steps share one credential and one budget. A bad CourtListener week (10× volume) now triple-counts: alert filter spike + enrichment spike + scoring spike all consume the same cap.

Two questions the plan doesn't answer:

1. **What does a normal week actually cost?** The plan names a circuit-breaker but doesn't fill in the numbers.
2. **What's the cost variance across phases?** A 10× cheap-upstream spike that starves expensive-downstream is silent quality degradation. Plan needs phase-level sub-budgets, but only after we know per-phase token magnitudes.

This spike runs 20 representative BK prospects end-to-end against Codex, captures token + dollar costs per phase, extrapolates to weekly steady-state + 10× wave, and writes the four numbers into the plan: per-phase soft caps, per-phase hard caps, per-run aggregate cap, weekly rolling-alert threshold.

## What you need before starting

- Codex CLI on PATH (`codex --version` should return a version)
- 20 fixture BK prospects in JSON (see "Fixture format" below)
- Real prompts for the 6 BK lane phases (alert filter / PDF extraction / debtor normalization / scoring / brief drafting / COI personalization). For the spike, sdr-engine's `prompts/draft-pipeline.txt` is a reasonable proxy for "scoring rationale + brief drafting"; the other 4 phases need stub prompts written for the spike.
- Codex billing access (so you can read the actual dollars at the end; reading token counts isn't enough — model + tier matter)
- ~½ day uninterrupted
- A scratch directory `~/codex-cost-spike/`

If Codex CLI isn't on PATH yet, install it first — separately from this spike.

## Fixture format

Create 20 fixture prospects in `~/codex-cost-spike/fixtures/prospects.jsonl`, one per line:

```json
{"prospect_id": "fix-001", "court_alert_payload": "...full RSS payload...", "creditor_pdf_text": "...extracted text...", "raw_debtor_name": "ACME CORP DBA SOMETHING LLC"}
{"prospect_id": "fix-002", ...}
```

Source the 20 from real recent CourtListener alerts so the token distribution is realistic. Vary across:
- Short docket text (≤500 tokens) — 5 prospects
- Medium docket text (~2000 tokens) — 10 prospects  
- Long docket text (≥5000 tokens — multi-creditor cases) — 5 prospects

This shape matters because the heavy-tail prospect drives cost. The cap calibration is wrong if you only spike on median-length prospects.

## The spike

### Step 1 — Set up tracking

```bash
mkdir -p ~/codex-cost-spike/{fixtures,outputs,logs}
cd ~/codex-cost-spike

# Cost ledger — one row per (prospect, phase, call)
sqlite3 cost-ledger.db <<'EOF'
CREATE TABLE calls (
  prospect_id TEXT,
  phase TEXT,
  call_num INTEGER,         -- if a phase makes multiple calls
  model TEXT,
  tokens_in INTEGER,
  tokens_out INTEGER,
  latency_ms INTEGER,
  cost_usd_estimate REAL,   -- computed from model pricing + tokens
  outcome TEXT,             -- success / retry / failed
  ts REAL
);
CREATE INDEX idx_calls_phase ON calls(phase);
CREATE INDEX idx_calls_prospect ON calls(prospect_id);
EOF
```

### Step 2 — Pull current Codex pricing

```bash
# Look up the actual Codex model you're using and its per-MTok rates
# Example (verify against your Codex account):
#   Opus-class:   $15/MTok in,  $75/MTok out
#   Sonnet-class:  $3/MTok in,  $15/MTok out
#   Haiku-class:   $0.80/MTok in, $4/MTok out

# Write the rates into a config file the spike script reads
cat > ~/codex-cost-spike/pricing.json <<'EOF'
{
  "claude-opus-4-7":   {"in_per_mtok": 15.00, "out_per_mtok": 75.00},
  "claude-sonnet-4-6": {"in_per_mtok":  3.00, "out_per_mtok": 15.00},
  "claude-haiku-4-5":  {"in_per_mtok":  0.80, "out_per_mtok":  4.00}
}
EOF
```

Verify the model your Codex CLI defaults to (`codex config get model` or similar) and confirm pricing before running.

### Step 3 — Write the spike runner

Create `~/codex-cost-spike/run_spike.py`:

```python
"""
Codex cost calibration spike. Runs 20 fixture prospects through the 6
BK lane phases, capturing token + cost per phase per prospect.

Phases approximate the production BK lane shape. Adjust prompts to match
the real ones once they exist.
"""
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path.home() / "codex-cost-spike"
FIXTURES = ROOT / "fixtures" / "prospects.jsonl"
LEDGER = ROOT / "cost-ledger.db"
PRICING = json.loads((ROOT / "pricing.json").read_text())

# BK lane phases. Each is (name, prompt_template_path, expected_output_tokens_target)
PHASES = [
    ("alert_filter",   "prompts/alert_filter.txt",     200),
    ("pdf_extract",    "prompts/pdf_extract.txt",      800),
    ("debtor_norm",    "prompts/debtor_norm.txt",      100),
    ("scoring",        "prompts/scoring.txt",         1000),
    ("brief_draft",    "prompts/brief_draft.txt",     1500),
    ("coi_personal",   "prompts/coi_personal.txt",     500),
]

def call_codex(prompt: str, model: str = "claude-sonnet-4-6") -> dict:
    """Invoke codex CLI, capture token counts + latency."""
    t0 = time.time()
    result = subprocess.run(
        ["codex", "exec", prompt, "--model", model, "--json-output"],
        capture_output=True, text=True, timeout=120,
    )
    latency_ms = int((time.time() - t0) * 1000)

    # Parse codex's JSON envelope. Adjust to actual codex CLI output shape.
    response = json.loads(result.stdout)
    return {
        "model": model,
        "tokens_in":  response["usage"]["input_tokens"],
        "tokens_out": response["usage"]["output_tokens"],
        "latency_ms": latency_ms,
        "output_text": response["content"],
    }

def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    rates = PRICING[model]
    return (tokens_in / 1_000_000 * rates["in_per_mtok"]
          + tokens_out / 1_000_000 * rates["out_per_mtok"])

def run_phase(prospect: dict, phase_name: str, prompt_template: str, model: str) -> dict:
    # Render the template with the prospect's fields (mock — real impl uses Jinja or similar)
    prompt = prompt_template.format(**prospect)
    response = call_codex(prompt, model)
    cost = estimate_cost(response["model"], response["tokens_in"], response["tokens_out"])

    # Write to ledger
    conn = sqlite3.connect(LEDGER)
    conn.execute(
        "INSERT INTO calls VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'success', ?)",
        (prospect["prospect_id"], phase_name, response["model"],
         response["tokens_in"], response["tokens_out"], response["latency_ms"],
         cost, time.time())
    )
    conn.commit()
    conn.close()
    return response

def main():
    prospects = [json.loads(l) for l in FIXTURES.read_text().splitlines() if l.strip()]
    assert len(prospects) == 20, f"Expected 20 fixtures, got {len(prospects)}"

    for i, prospect in enumerate(prospects, 1):
        print(f"[{i}/20] Running prospect {prospect['prospect_id']}...")
        for phase_name, template_path, _ in PHASES:
            template = (ROOT / template_path).read_text()
            try:
                run_phase(prospect, phase_name, template, "claude-sonnet-4-6")
            except Exception as e:
                print(f"  Phase {phase_name} failed: {e}")
                # Log the failure too so cap calibration sees the variance
                conn = sqlite3.connect(LEDGER)
                conn.execute(
                    "INSERT INTO calls VALUES (?, ?, 1, ?, 0, 0, 0, 0, 'failed', ?)",
                    (prospect["prospect_id"], phase_name, "claude-sonnet-4-6", time.time())
                )
                conn.commit()
                conn.close()

    print("\nDone. Run analyze.py to see results.")

if __name__ == "__main__":
    main()
```

### Step 4 — Write the analyzer

Create `~/codex-cost-spike/analyze.py`:

```python
"""
Analyze the cost ledger. Output: per-phase median + p95 + max cost,
total per-prospect cost distribution, extrapolated weekly + 10x-wave.
"""
import sqlite3
import statistics
from pathlib import Path

LEDGER = Path.home() / "codex-cost-spike" / "cost-ledger.db"
WEEKLY_PROSPECTS = 500  # baseline assumption — adjust to actual

conn = sqlite3.connect(LEDGER)

print("=" * 60)
print("PHASE COST DISTRIBUTION (per call, USD)")
print("=" * 60)
print(f"{'phase':<15} {'n':>4} {'median':>8} {'p95':>8} {'max':>8} {'tot':>10}")
phases = conn.execute(
    "SELECT DISTINCT phase FROM calls ORDER BY phase"
).fetchall()
for (phase,) in phases:
    costs = [r[0] for r in conn.execute(
        "SELECT cost_usd_estimate FROM calls WHERE phase=? AND outcome='success'",
        (phase,)
    )]
    if not costs:
        print(f"{phase:<15} {'0':>4} (no successful calls)")
        continue
    print(f"{phase:<15} {len(costs):>4} "
          f"${statistics.median(costs):>7.4f} "
          f"${sorted(costs)[int(len(costs)*0.95)]:>7.4f} "
          f"${max(costs):>7.4f} "
          f"${sum(costs):>9.2f}")

print()
print("=" * 60)
print("PER-PROSPECT TOTAL COST (sum of 6 phases per prospect)")
print("=" * 60)
prospect_totals = [r[0] for r in conn.execute(
    "SELECT SUM(cost_usd_estimate) FROM calls WHERE outcome='success' GROUP BY prospect_id"
)]
print(f"  median:   ${statistics.median(prospect_totals):.4f}")
print(f"  p95:      ${sorted(prospect_totals)[int(len(prospect_totals)*0.95)]:.4f}")
print(f"  max:      ${max(prospect_totals):.4f}")
print(f"  mean:     ${statistics.mean(prospect_totals):.4f}")

print()
print("=" * 60)
print(f"EXTRAPOLATION ({WEEKLY_PROSPECTS} prospects/week baseline)")
print("=" * 60)
mean_per_prospect = statistics.mean(prospect_totals)
weekly = mean_per_prospect * WEEKLY_PROSPECTS
print(f"  steady-state weekly cost: ${weekly:.2f}")
print(f"  10× wave weekly cost:     ${weekly * 10:.2f}")
print(f"  annual steady-state:      ${weekly * 52:.2f}")
print()
print("RECOMMENDED CAPS (fill into leadgen-vision.md):")
print(f"  per-run hard cap (2× max prospect × {WEEKLY_PROSPECTS}):  ${max(prospect_totals) * 2 * WEEKLY_PROSPECTS:.2f}")
print(f"  per-run soft alert ({WEEKLY_PROSPECTS}× p95):            ${sorted(prospect_totals)[int(len(prospect_totals)*0.95)] * WEEKLY_PROSPECTS:.2f}")
print(f"  rolling 4-week alert (4× steady):           ${weekly * 4:.2f}")
print()
print("PHASE SUB-BUDGETS (per run, p95-based):")
for (phase,) in phases:
    costs = [r[0] for r in conn.execute(
        "SELECT cost_usd_estimate FROM calls WHERE phase=? AND outcome='success'",
        (phase,)
    )]
    if costs:
        p95 = sorted(costs)[int(len(costs)*0.95)]
        budget = p95 * WEEKLY_PROSPECTS * 1.5  # 1.5x safety margin
        print(f"  {phase:<15} ${budget:>8.2f}")
```

### Step 5 — Run the spike

```bash
cd ~/codex-cost-spike

# Smoke test on 1 prospect first to make sure plumbing works
head -1 fixtures/prospects.jsonl > fixtures/prospects.smoke.jsonl
FIXTURES=fixtures/prospects.smoke.jsonl python run_spike.py

# If smoke is clean, run all 20
python run_spike.py 2>&1 | tee logs/run-$(date +%s).log

# Analyze
python analyze.py | tee logs/analysis-$(date +%s).log
```

Expected wall-clock: ~5 sec/call × 6 phases × 20 prospects ≈ 10 min for the full run.

### Step 6 — Sanity-check the numbers

Before locking these into the plan, manually verify against Codex's actual billing dashboard:

1. Note the spike's start + end times.
2. Wait 5–10 min for billing to update.
3. Check Codex dashboard → usage for that window.
4. Confirm: actual billed amount ≈ sum of `cost_usd_estimate` in the ledger (within ±10%).

If actual is significantly higher, your pricing.json rates are stale OR Codex is using a different model than you specified. Reconcile before extrapolating.

## Acceptance criteria — fill these blanks in `leadgen-vision.md`

Open the LLM-stack section of `leadgen-vision.md` (around "**Cost ceiling (required, per Eng P2)**"). Replace placeholders with the spike output:

```
Cost ceiling:
  per-run hard breaker:      $___ (from analyze.py output)
  per-run soft alert:        $___
  4-week rolling alert:      $___

Phase sub-budgets (trip per-phase, not aggregate):
  alert_filter:    $___
  pdf_extract:     $___
  debtor_norm:     $___
  scoring:         $___
  brief_draft:     $___
  coi_personal:    $___
```

Also record:

- **Annual steady-state cost estimate:** $___
- **10× wave weekly cost (worst-case before circuit-break):** $___
- **Per-prospect mean / p95 / max:** $___ / $___ / $___

These numbers go into the LLM stack section of `leadgen-vision.md` AND into `apps/tcia-leadgen/CLAUDE.md` once the repo exists, so future Daniel knows the budgeting assumptions.

## Decision points after the spike

After analyzing the numbers, three buckets:

### Bucket 1 — Cheap enough to ship as-is
**If** weekly steady-state < $200 AND 10× wave < $1,500 AND p95 prospect < $0.50 → ship the plan with these caps. Codex-default is fine.

### Bucket 2 — Cheap path needed for bulk filter
**If** alert_filter phase alone exceeds $0.10/prospect (i.e., scaled = $50/week) → route alert filtering to **`claude-haiku`** instead of default Sonnet. Haiku at ~$0.80/MTok in vs Sonnet's $3/MTok = 75% cheaper. Update plan:

> "Codex calls default to Sonnet, EXCEPT bulk alert filtering which uses Haiku (alert filter is pattern-match shaped, not reasoning-heavy)."

### Bucket 3 — Hybrid required
**If** total weekly steady-state > $500 OR p95 prospect > $2 → the cost story is borderline. Two options:
- **Option A:** keep bulk filtering on local qwen via OpenClaw (reintroduce the local-bulk-filter pattern that was dropped during the Final Gate). Yes, this contradicts the "remove LiteLLM" decision — but in the architectural shift, LiteLLM was the routing layer; OpenClaw can route the same way. The hybrid is: leadgen calls OpenClaw for bulk filtering, OpenClaw uses local qwen; leadgen calls Codex directly for reasoning steps.
- **Option B:** accept the cost. If $25k/year is acceptable for a pipeline that creates measurable opportunity-rate lift, ship it.

Document the decision in `apps/tcia-leadgen/CLAUDE.md` once chosen.

## Estimated effort

- Set up fixtures + tracking (Step 1–3): 1.5 hrs
- Write runner + analyzer (Step 4): 1 hr
- Run spike + reconcile billing (Step 5–6): 1 hr
- Lock numbers into plan + write decision: 30 min
- **Total: ~½ day**

If Bucket 3 (hybrid required) → add 1 day to design + document the OpenClaw bulk-filter path.

## What the spike does NOT validate

- Codex's reliability under sustained load (the spike runs 120 calls in 10 min; real weekly load is 3000 calls)
- Codex auth-token expiry behavior (covered separately by PE1 carve-out in plan)
- Hermes ↔ Codex integration (different concern; covered by PB1)
- Apollo round-trip cost (Apollo's API isn't paid per call, but rate-limited — separate spike if Apollo's RPM cap becomes binding)

These are out-of-scope for PB2. PB2's only job: lock in defensible numbers for the per-run, per-phase, and rolling cost caps.
