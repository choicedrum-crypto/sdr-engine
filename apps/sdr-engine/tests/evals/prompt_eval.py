"""Live-LLM evaluation harness for the operational prompt.

Runs the same inputs through a real LiteLLM endpoint and measures how
often `validate()` passes. Skipped in CI unless `LITELLM_BASE_URL` is set —
this isn't a unit test, it's a regression guard for prompt edits.

Run locally:
    LITELLM_BASE_URL=http://127.0.0.1:4000 pytest tests/evals/ -v -s

What it does:
1. Loads a fixture set of representative deal inputs (`fixtures/draft-inputs/*.json`).
   Each fixture is one (prospect_type, send_reason, anchor presence) cell.
2. Runs draft() against the live endpoint for each fixture.
3. Records pass/fail + attempts + model_used + failure reasons per fixture.
4. Asserts the pass rate clears the threshold:
     - >= 0.90 to PASS (prompt is shipping-ready)
     - 0.70-0.89 → DEGRADED warning (prompt regression — fix before merging)
     - <0.70 → FAIL hard (prompt is broken; investigate)

The fixture set is small by design — ~6-10 rows is enough to catch
structural regressions cheaply. Production-quality eval scoring (LLM-judge
on prose quality) is a v1.1 concern, tracked as TODOS #1.

Tier reference (cost ceiling per `MAX_CLOUD_FALLBACK_PER_DAY=15`):
- All fixtures hit local-main first; cloud fallback only on validation
  retry failure. Expected cost per eval run: ~$0 (local) to ~$0.50 (worst
  case all cells fall back). Run after every prompt edit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sdr_engine.llm import draft

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "draft-inputs"
PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "draft-pipeline.txt"
CTAS_PATH = Path(__file__).resolve().parents[2] / "prompts" / "ctas.json"

PASS_THRESHOLD = 0.90
DEGRADED_THRESHOLD = 0.70


def _load_fixtures() -> list[tuple[str, dict]]:
    """Load every *.json under fixtures/draft-inputs/."""
    if not FIXTURE_DIR.exists():
        return []
    return [
        (p.stem, json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(FIXTURE_DIR.glob("*.json"))
    ]


@pytest.mark.skipif(
    not os.getenv("LITELLM_BASE_URL"),
    reason="set LITELLM_BASE_URL to run the live-eval suite (e.g. http://127.0.0.1:4000)",
)
def test_prompt_eval_pass_rate() -> None:
    fixtures = _load_fixtures()
    assert fixtures, f"No eval fixtures found under {FIXTURE_DIR}. Add JSON files to begin."

    endpoint = f"{os.environ['LITELLM_BASE_URL']}/v1/chat/completions"
    primary = os.getenv("LLM_MODEL_PRIMARY", "local-main")
    fallback = os.getenv("LLM_MODEL_FALLBACK", "heavy-main")

    results: list[dict] = []
    for name, inputs in fixtures:
        result = draft(
            inputs,
            PROMPT_PATH,
            CTAS_PATH,
            endpoint,
            primary_model=primary,
            fallback_model=fallback,
        )
        results.append(
            {
                "fixture": name,
                "success": result.success,
                "attempts": result.attempts,
                "model_used": result.model_used,
                "failures": result.failures,
            }
        )

    passed = sum(1 for r in results if r["success"])
    rate = passed / len(results)

    print()
    print(f"╔════ Prompt eval — {len(results)} fixtures ══════════════════╗")
    print(f"║  Pass rate: {passed}/{len(results)} ({rate:.0%})")
    for r in results:
        sym = "✓" if r["success"] else "✗"
        first_failure = r["failures"][0] if r["failures"] and not r["success"] else ""
        print(f"║  {sym} {r['fixture']:<32} attempts={r['attempts']} model={r['model_used']}")
        if first_failure:
            print(f"║      └─ {first_failure[:80]}")
    print(f"╚{'═' * 60}╝")

    if rate < DEGRADED_THRESHOLD:
        pytest.fail(
            f"Prompt eval pass rate {rate:.0%} < {DEGRADED_THRESHOLD:.0%} hard floor. "
            "Investigate before any prompt-touching PR can merge."
        )
    if rate < PASS_THRESHOLD:
        pytest.fail(
            f"Prompt eval pass rate {rate:.0%} below {PASS_THRESHOLD:.0%} target. "
            "Likely a recent prompt edit caused regression — review failures above."
        )
