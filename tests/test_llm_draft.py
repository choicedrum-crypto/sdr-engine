"""Integration tests for sdr_engine.llm.draft() — the retry + cloud-fallback orchestrator.

Uses the `responses` library to mock the LiteLLM endpoint at the HTTP layer.
No live LLM is hit. These tests verify the cascade logic, not LLM quality.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import responses

from sdr_engine.llm import draft

ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "prompts" / "draft-pipeline.txt"
CTAS_PATH = ROOT / "prompts" / "ctas.json"
LLM_URL = "http://127.0.0.1:4000/v1/chat/completions"


# ─── Helpers ───────────────────────────────────────────────────────
def _minimal_inputs(prospect_type: str = "former_client") -> dict[str, Any]:
    """Just enough fields to exercise the orchestrator. Module 4's substitute()
    leaves un-substituted {placeholder}s in the prompt verbatim; the orchestrator
    sends that to the mocked LLM, which doesn't care about real content."""
    return {
        "prospect_type": prospect_type,
        "send_reason": "round-robin",
        "company_name": "Acme Corp",
        "company_acronym": "ACME",
        "company_industry": "Manufacturing",
        "contact_first_name": "Daniel",
        "contact_last_name": "Bradley",
        "contact_source": "existing",
        "quote_amount": "",
        "product_line": "Trade Credit",
        "quote_date_human": "March 2024",
        "renewal_date_or_null": None,
        "policy_excerpt_or_null": None,
        "hook_text": "",
        "hook_source": "none",
        "anchor_type": "none",
        "anchor_name": "",
        "anchor_context": None,
    }


def _valid_response_payload(cta: str) -> dict[str, Any]:
    """A response body that passes validate() for former_client."""
    body = "Dear Daniel,\n\n" + (" ".join(["word"] * 145)) + " " + cta + "\n\nBest regards,\nDaniel Bradley"
    return {
        "deal_note": " ".join(["w"] * 80),
        "email": {
            "subject_options": ["subject one here", "subject two here"],
            "body": body,
            "word_count": len(body.split()),
            "cta_chosen": cta,
        },
        "call_script": {
            "opening": "Hi Daniel, this is Daniel Bradley from TCIA.",
            "context_bridge": "Following up on the recent industry news in your sector.",
            "observation": "Saw your CFO change last month — timing felt right.",
            "open_question": "How are you thinking about credit risk this quarter?",
            "objection_bridges": {
                "already_covered": "Understood — keep us in mind for the next review.",
                "not_interested": "No worries — we run AR analysis quarterly, easy to revisit.",
                "send_info": "Will send a one-pager today on the carrier panel.",
            },
        },
        "metadata": {"anchor_used": "none", "hook_used": "none"},
    }


def _wrap_as_llm_response(content_payload: dict[str, Any] | str) -> dict[str, Any]:
    """LiteLLM/OpenAI-compatible chat/completions response envelope."""
    content_str = (
        content_payload if isinstance(content_payload, str) else json.dumps(content_payload)
    )
    return {"choices": [{"message": {"content": content_str}}]}


def _first_cta_for(prospect_type: str) -> str:
    """Pick the first CTA allowed for this prospect type, to mirror what the
    LLM would pick from {ctas_filtered}."""
    from sdr_engine.llm import filter_ctas
    return filter_ctas(CTAS_PATH, prospect_type)[0]


# ─── Happy path ────────────────────────────────────────────────────
@responses.activate
def test_draft_happy_path_first_attempt() -> None:
    cta = _first_cta_for("former_client")
    responses.add(
        responses.POST,
        LLM_URL,
        json=_wrap_as_llm_response(_valid_response_payload(cta)),
        status=200,
    )
    result = draft(_minimal_inputs(), PROMPT_PATH, CTAS_PATH, LLM_URL)
    assert result.success
    assert result.model_used == "local-main"
    assert result.attempts == 1
    assert result.artifacts["email"]["cta_chosen"] == cta


# ─── Retry on validation failure ───────────────────────────────────
@responses.activate
def test_draft_retries_on_validation_failure_then_succeeds() -> None:
    cta = _first_cta_for("former_client")
    bad = _valid_response_payload(cta)
    bad["email"]["body"] = "way too short"
    bad["email"]["word_count"] = 3

    responses.add(responses.POST, LLM_URL, json=_wrap_as_llm_response(bad), status=200)
    responses.add(
        responses.POST,
        LLM_URL,
        json=_wrap_as_llm_response(_valid_response_payload(cta)),
        status=200,
    )

    result = draft(_minimal_inputs(), PROMPT_PATH, CTAS_PATH, LLM_URL)
    assert result.success
    assert result.model_used == "local-main"
    assert result.attempts == 2

    # Second request should contain the [RETRY] failure-reason injection
    second_body = json.loads(responses.calls[1].request.body)
    second_prompt = second_body["messages"][0]["content"]
    assert "[RETRY]" in second_prompt
    assert "word count" in second_prompt  # the specific failure reason


# ─── Cloud fallback ────────────────────────────────────────────────
@responses.activate
def test_draft_falls_back_to_heavy_main_on_second_failure() -> None:
    cta = _first_cta_for("former_client")
    bad = _valid_response_payload(cta)
    bad["email"]["body"] = "still too short"
    bad["email"]["word_count"] = 3

    responses.add(responses.POST, LLM_URL, json=_wrap_as_llm_response(bad), status=200)
    responses.add(responses.POST, LLM_URL, json=_wrap_as_llm_response(bad), status=200)
    responses.add(
        responses.POST,
        LLM_URL,
        json=_wrap_as_llm_response(_valid_response_payload(cta)),
        status=200,
    )

    result = draft(_minimal_inputs(), PROMPT_PATH, CTAS_PATH, LLM_URL)
    assert result.success
    assert result.model_used == "heavy-main"
    assert result.attempts == 3

    # Third request must target the fallback model
    third_body = json.loads(responses.calls[2].request.body)
    assert third_body["model"] == "heavy-main"


# ─── Total failure → NEEDS_HUMAN ───────────────────────────────────
@responses.activate
def test_draft_returns_needs_human_after_three_failures() -> None:
    cta = _first_cta_for("former_client")
    bad = _valid_response_payload(cta)
    bad["email"]["body"] = "bad"
    bad["email"]["word_count"] = 1

    for _ in range(3):
        responses.add(responses.POST, LLM_URL, json=_wrap_as_llm_response(bad), status=200)

    result = draft(_minimal_inputs(), PROMPT_PATH, CTAS_PATH, LLM_URL)
    assert not result.success
    assert result.model_used == "none"
    assert result.attempts == 4
    assert "[LLM format failure" in result.artifacts["email"]["body"]
    assert len(result.failures) >= 3  # accumulated reasons from each attempt


# ─── JSON parse error → repair pass ────────────────────────────────
@responses.activate
def test_draft_handles_fenced_json_block() -> None:
    """LLM wraps the JSON in ```json...``` despite being asked not to.
    The repair pass should extract it on the FIRST attempt."""
    cta = _first_cta_for("former_client")
    payload = _valid_response_payload(cta)
    raw_content = f"Here you go:\n```json\n{json.dumps(payload)}\n```\nThanks!"
    responses.add(responses.POST, LLM_URL, json=_wrap_as_llm_response(raw_content), status=200)

    result = draft(_minimal_inputs(), PROMPT_PATH, CTAS_PATH, LLM_URL)
    assert result.success
    assert result.attempts == 1


@responses.activate
def test_draft_unparseable_content_advances_to_next_attempt() -> None:
    """Pure prose with no JSON anywhere — repair returns None, attempt fails,
    next attempt should run with a 'return STRICT JSON' nudge appended."""
    cta = _first_cta_for("former_client")
    responses.add(
        responses.POST, LLM_URL, json=_wrap_as_llm_response("Sorry, I cannot do this."), status=200
    )
    responses.add(
        responses.POST,
        LLM_URL,
        json=_wrap_as_llm_response(_valid_response_payload(cta)),
        status=200,
    )

    result = draft(_minimal_inputs(), PROMPT_PATH, CTAS_PATH, LLM_URL)
    assert result.success
    assert result.attempts == 2
    second_body = json.loads(responses.calls[1].request.body)
    second_prompt = second_body["messages"][0]["content"]
    assert "STRICT JSON only" in second_prompt


# ─── Transport errors ──────────────────────────────────────────────
@responses.activate
def test_draft_handles_http_5xx_as_attempt_failure() -> None:
    cta = _first_cta_for("former_client")
    responses.add(responses.POST, LLM_URL, status=503)
    responses.add(responses.POST, LLM_URL, status=503)
    responses.add(
        responses.POST,
        LLM_URL,
        json=_wrap_as_llm_response(_valid_response_payload(cta)),
        status=200,
    )

    result = draft(_minimal_inputs(), PROMPT_PATH, CTAS_PATH, LLM_URL)
    assert result.success
    assert result.attempts == 3
    assert result.model_used == "heavy-main"
    assert any("transport error" in f for f in result.failures)


# ─── Unknown prospect_type ─────────────────────────────────────────
def test_draft_rejects_unknown_prospect_type_without_calling_llm() -> None:
    inputs = _minimal_inputs()
    inputs["prospect_type"] = "invented_type"

    # No `responses.add` calls — if draft() called the LLM, the test would error
    # with ConnectionRefusedError because there's no mock.
    result = draft(inputs, PROMPT_PATH, CTAS_PATH, LLM_URL)
    assert not result.success
    assert result.model_used == "none"
    assert result.attempts == 0
    assert any("unknown prospect_type" in f for f in result.failures)


# ─── Custom temperatures per attempt ───────────────────────────────
@responses.activate
def test_draft_uses_lower_temperature_on_retry() -> None:
    """Per A3: 1st attempt temp=0.5, 2nd and 3rd attempts temp=0.3."""
    cta = _first_cta_for("former_client")
    bad = _valid_response_payload(cta)
    bad["email"]["body"] = "too short"
    bad["email"]["word_count"] = 2

    responses.add(responses.POST, LLM_URL, json=_wrap_as_llm_response(bad), status=200)
    responses.add(
        responses.POST,
        LLM_URL,
        json=_wrap_as_llm_response(_valid_response_payload(cta)),
        status=200,
    )

    draft(_minimal_inputs(), PROMPT_PATH, CTAS_PATH, LLM_URL)

    first_body = json.loads(responses.calls[0].request.body)
    second_body = json.loads(responses.calls[1].request.body)
    assert first_body["temperature"] == 0.5
    assert second_body["temperature"] == 0.3
