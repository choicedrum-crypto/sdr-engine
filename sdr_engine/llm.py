"""Module 4 — LLM drafting.

Loads the operational prompt template (`prompts/draft-pipeline.txt`),
substitutes per-deal inputs (with prompt-injection defense per A1),
filters CTAs by prospect_type, calls the LiteLLM routing endpoint, validates
the response against the JSON contract, and retries with structured failure
reasons (per A3). On second validation failure, falls back to the cloud
`heavy-main` model. On third failure, marks NEEDS_HUMAN per A15.

Module 4's draft() is the only public entry point. Everything else is
helpers exported for unit testing.

External callers (Module 1 via n8n, force-enqueue from the UI) supply a
fully-populated input dict per the operational template's INPUTS section.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

# ─── Constants ──────────────────────────────────────────────────────
# Prospect types eligible for the structured_re_engagement variant.
# Word count: 150-200 (or 120-150 if anchor_type == "none").
STRUCTURED_TYPES = {"former_client", "bor_target", "lost_opportunity"}

# Prospect types eligible for the warm_direct_ask variant. Word count: 120-160.
WARM_TYPES = {"coi_referral", "industry_trigger", "conference", "bankruptcy_trigger"}

# All 7 known prospect types — anything outside this set is rejected at the
# scheduler boundary (Module 1) and never reaches Module 4.
KNOWN_PROSPECT_TYPES = STRUCTURED_TYPES | WARM_TYPES

# Untrusted input fields: user-controlled data wrapped in <untrusted_input>
# tags in the operational template per A1. The template already encloses them;
# substitute() additionally sanitizes the values so the closing tag itself
# can't be injected to break out of the wrapping.
UNTRUSTED_FIELDS = frozenset({"anchor_context", "hook_text", "policy_excerpt_or_null"})

# Forbidden words/phrases per the prompt's NEGATIVE CONSTRAINTS section.
# Module 4 enforces a hard check on "navigate" because the prompt locks it
# explicitly. Other prohibitions (AI fluff, "I hope this finds you well")
# are enforced by the prompt itself, not validation — validation catches
# the most damaging violation.
FORBIDDEN_BODY_PATTERN = re.compile(r"\bnavigate\b", re.IGNORECASE)


@dataclass
class LLMResult:
    """Return value of draft(). success=False means NEEDS_HUMAN — UI surfaces banner."""
    artifacts: dict[str, Any]
    model_used: str             # "local-main", "heavy-main", or "none" on total failure
    attempts: int               # 1, 2, or 3 on success; 4 = NEEDS_HUMAN
    success: bool
    failures: list[str] = field(default_factory=list)  # accumulated failure reasons


# ─── Prompt + CTA loading ───────────────────────────────────────────
def load_prompt(prompt_path: Path) -> str:
    """Read the operational template verbatim. Module 4 substitutes into this."""
    return prompt_path.read_text(encoding="utf-8")


def filter_ctas(ctas_path: Path, prospect_type: str) -> list[str]:
    """Return the CTA texts whose allowed_for array contains prospect_type.

    Module 4 hands this list to the LLM via {ctas_filtered}; the LLM must pick
    one entry verbatim. Raises ValueError if the resulting list is empty —
    this is the A14 invariant (every prospect_type must have ≥1 allowed CTA),
    enforced at draft-time so a misconfigured ctas.json fails fast rather
    than silently producing rejected drafts.
    """
    payload = json.loads(ctas_path.read_text(encoding="utf-8"))
    allowed = [c["text"] for c in payload["ctas"] if prospect_type in c["allowed_for"]]
    if not allowed:
        raise ValueError(
            f"No CTA in {ctas_path} has prospect_type={prospect_type!r} in allowed_for. "
            "Module 4 cannot draft for this type until ctas.json is fixed."
        )
    return allowed


# ─── Substitution + injection defense ───────────────────────────────
def sanitize_untrusted(text: str | None) -> str:
    """Strip closing-tag injection attempts so user content can't break out
    of the <untrusted_input> wrapper. Empty/None → empty string (the prompt
    template tolerates an empty pair of tags).
    """
    if not text:
        return ""
    # Replace any literal closing tag in the value. Case-insensitive defense.
    return re.sub(r"</untrusted_input>", "<closing-tag-stripped>", text, flags=re.IGNORECASE)


def substitute(template: str, inputs: dict[str, Any]) -> str:
    """Replace `{placeholder}` tokens in the template with input values.

    Untrusted fields (anchor_context, hook_text, policy_excerpt_or_null) are
    sanitized before substitution. The operational template wraps those
    placeholders in <untrusted_input>...</untrusted_input> tags; sanitization
    ensures the value can't contain a stray closing tag.
    """
    rendered = template
    for key, value in inputs.items():
        if key in UNTRUSTED_FIELDS:
            value_str = sanitize_untrusted(value)
        elif value is None:
            value_str = ""
        elif isinstance(value, (list, dict)):
            value_str = json.dumps(value)
        else:
            value_str = str(value)
        rendered = rendered.replace("{" + key + "}", value_str)
    return rendered


# ─── Validation ─────────────────────────────────────────────────────
def _word_count(text: str) -> int:
    """Match the operational template's word_count contract: len(body.split())."""
    return len(text.split())


def _validate_email(
    email: dict[str, Any], prospect_type: str, ctas_filtered: list[str], failures: list[str]
) -> None:
    """Fill failures[] with every email-shape violation found. In-place mutation."""
    subjects = email.get("subject_options")
    if not isinstance(subjects, list) or len(subjects) != 2:
        failures.append("email.subject_options must be a list of exactly 2 strings")
    else:
        for i, subj in enumerate(subjects):
            if not isinstance(subj, str) or not subj.strip():
                failures.append(f"email.subject_options[{i}] empty or not a string")
            elif len(subj) > 60:
                failures.append(f"email.subject_options[{i}] is {len(subj)} chars (>60)")
        if subjects[0] == subjects[1]:
            failures.append("email.subject_options[0] and [1] must be distinct")

    body = email.get("body")
    if not isinstance(body, str) or not body.strip():
        failures.append("email.body empty or not a string")
        return  # downstream checks would crash on None

    actual_wc = _word_count(body)
    declared_wc = email.get("word_count")
    if not isinstance(declared_wc, int):
        failures.append("email.word_count must be an integer")
    elif declared_wc != actual_wc:
        failures.append(
            f"email.word_count={declared_wc} but actual len(body.split())={actual_wc}"
        )

    # Variant word-count range. Accept the wider range when anchor was dropped
    # (per the ANCHOR DROP RULE — 120-150 for structured with no anchor;
    # we widen the structured floor to 120 to cover that case).
    if prospect_type in STRUCTURED_TYPES:
        lo, hi = 120, 200
        variant = "structured_re_engagement"
    else:
        lo, hi = 120, 160
        variant = "warm_direct_ask"
    if not (lo <= actual_wc <= hi):
        failures.append(
            f"email.body word count {actual_wc} not in [{lo}, {hi}] for {variant}"
        )

    if FORBIDDEN_BODY_PATTERN.search(body):
        failures.append('email.body contains forbidden word "navigate"')

    cta_chosen = email.get("cta_chosen")
    if not isinstance(cta_chosen, str) or not cta_chosen.strip():
        failures.append("email.cta_chosen empty or not a string")
    elif cta_chosen not in ctas_filtered:
        failures.append(
            f"email.cta_chosen not in filtered CTA list "
            f"(got {cta_chosen[:50]!r}, expected one of {len(ctas_filtered)} options)"
        )
    elif cta_chosen not in body:
        failures.append("email.cta_chosen value must also appear verbatim inside email.body")


def _validate_call_script(call_script: dict[str, Any], failures: list[str]) -> None:
    for key in ("opening", "context_bridge", "observation", "open_question"):
        v = call_script.get(key)
        if not isinstance(v, str) or not v.strip():
            failures.append(f"call_script.{key} empty or missing")

    bridges = call_script.get("objection_bridges")
    if not isinstance(bridges, dict):
        failures.append("call_script.objection_bridges must be an object")
        return
    for key in ("already_covered", "not_interested", "send_info"):
        v = bridges.get(key)
        if not isinstance(v, str) or not v.strip():
            failures.append(f"call_script.objection_bridges.{key} empty or missing")


def validate(
    response: dict[str, Any], prospect_type: str, ctas_filtered: list[str]
) -> tuple[bool, list[str]]:
    """Validate an LLM response against Module 4's JSON contract.

    Returns (valid, failures). All failure reasons are collected, not short-
    circuited, so the retry prompt can hand the LLM the full list of what
    to fix per A3.
    """
    failures: list[str] = []

    if not isinstance(response, dict):
        return False, ["response is not a JSON object"]

    if prospect_type not in KNOWN_PROSPECT_TYPES:
        failures.append(f"caller passed unknown prospect_type={prospect_type!r}")

    for key in ("deal_note", "email", "call_script", "metadata"):
        if key not in response:
            failures.append(f"missing top-level key: {key}")
    if failures and any("missing top-level key" in f for f in failures):
        return False, failures  # bail before crashing on missing keys

    deal_note = response["deal_note"]
    if not isinstance(deal_note, str) or not deal_note.strip():
        failures.append("deal_note empty or not a string")
    else:
        wc = _word_count(deal_note)
        if not (50 <= wc <= 150):
            failures.append(f"deal_note word count {wc} not in [50, 150]")

    if isinstance(response["email"], dict):
        _validate_email(response["email"], prospect_type, ctas_filtered, failures)
    else:
        failures.append("email must be an object")

    if isinstance(response["call_script"], dict):
        _validate_call_script(response["call_script"], failures)
    else:
        failures.append("call_script must be an object")

    metadata = response["metadata"]
    if isinstance(metadata, dict):
        if metadata.get("anchor_used") not in {"broker", "agency_contact", "prior_work", "none"}:
            failures.append("metadata.anchor_used must be broker | agency_contact | prior_work | none")
        if metadata.get("hook_used") not in {"m_and_a", "leadership_change", "sector_stress", "none"}:
            failures.append(
                "metadata.hook_used must be m_and_a | leadership_change | sector_stress | none"
            )
    else:
        failures.append("metadata must be an object")

    return (not failures), failures


# ─── JSON repair (one-pass fallback) ────────────────────────────────
def try_repair_json(text: str) -> dict[str, Any] | None:
    """Single repair attempt for LLM output that isn't already valid JSON.

    Strategy (in order):
      1. Extract a ```json ... ``` fenced block.
      2. Extract the largest balanced {...} substring.

    Returns the parsed dict, or None if neither works.
    """
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # Largest balanced object — find the first `{` and the last `}`.
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        try:
            return json.loads(text[first : last + 1])
        except json.JSONDecodeError:
            pass
    return None


# ─── LiteLLM call ───────────────────────────────────────────────────
def call_llm(
    endpoint: str,
    model: str,
    prompt: str,
    timeout: int = 180,
    temperature: float = 0.5,
) -> tuple[dict[str, Any] | None, str]:
    """POST a chat/completions request and return (parsed_json, raw_content).

    Returns (dict, content) on parse success; (None, content) when content
    is not valid JSON (caller can try try_repair_json). Raises requests
    exceptions on transport-level failures — caller handles those as
    a separate attempt category.
    """
    resp = requests.post(
        endpoint,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content), content
    except json.JSONDecodeError:
        return None, content


# ─── Orchestrator ───────────────────────────────────────────────────
def draft(
    inputs: dict[str, Any],
    prompt_path: Path,
    ctas_path: Path,
    endpoint: str,
    primary_model: str = "local-main",
    fallback_model: str = "heavy-main",
    timeout: int = 180,
) -> LLMResult:
    """Three artifacts for one deal. Retry cascade per A3/A15:

        Attempt 1: primary_model, temp=0.5
        Attempt 2: primary_model, temp=0.3, prompt has failure reasons appended
        Attempt 3: fallback_model, temp=0.3, prompt has accumulated failure reasons
        Otherwise: NEEDS_HUMAN (success=False; UI shows banner)

    The retry strategy is structured (per A3): instead of repeating the
    same prompt and hoping for different output, the second and third
    attempts hand the LLM the concrete list of what failed last time.
    Empirically this cuts retry failure rate roughly in half.
    """
    prospect_type = inputs.get("prospect_type", "")
    if prospect_type not in KNOWN_PROSPECT_TYPES:
        return LLMResult(
            artifacts={},
            model_used="none",
            attempts=0,
            success=False,
            failures=[f"unknown prospect_type={prospect_type!r}"],
        )

    ctas_filtered = filter_ctas(ctas_path, prospect_type)
    inputs = {**inputs, "ctas_filtered": ctas_filtered}

    base_prompt = substitute(load_prompt(prompt_path), inputs)
    prompt = base_prompt
    all_failures: list[str] = []

    plan = [
        (primary_model, 0.5),
        (primary_model, 0.3),
        (fallback_model, 0.3),
    ]
    for attempt_num, (model, temperature) in enumerate(plan, start=1):
        try:
            parsed, raw = call_llm(endpoint, model, prompt, timeout, temperature)
        except requests.RequestException as exc:
            all_failures.append(f"attempt {attempt_num} ({model}) transport error: {exc}")
            continue

        if parsed is None:
            # Try one-pass JSON repair before giving up on this attempt.
            parsed = try_repair_json(raw)
            if parsed is None:
                all_failures.append(
                    f"attempt {attempt_num} ({model}) returned non-JSON content"
                )
                prompt = (
                    base_prompt
                    + "\n\n[RETRY] Previous response was not valid JSON. Return STRICT JSON only — "
                    "no prose, no markdown code fences, no surrounding text."
                )
                continue

        ok, failures = validate(parsed, prospect_type, ctas_filtered)
        if ok:
            return LLMResult(
                artifacts=parsed,
                model_used=model,
                attempts=attempt_num,
                success=True,
                failures=all_failures,  # carries info about earlier attempts even on success
            )

        all_failures.extend(f"attempt {attempt_num} ({model}): {f}" for f in failures)
        prompt = (
            base_prompt
            + "\n\n[RETRY] Previous attempt failed validation: "
            + "; ".join(failures)
            + ". Strictly comply with the contract above."
        )

    # All three attempts exhausted — emit a NEEDS_HUMAN shell that Module 7
    # writes to HubSpot as a placeholder, and the Review UI surfaces with a
    # prominent banner instructing the SDR to hand-write the email.
    return LLMResult(
        artifacts={
            "deal_note": "[LLM format failure — please hand-write the email and update this brief]",
            "email": {
                "subject_options": ["", ""],
                "body": "[LLM format failure — please hand-write]",
                "word_count": 0,
                "cta_chosen": "",
            },
            "call_script": {
                "opening": "",
                "context_bridge": "",
                "observation": "",
                "open_question": "",
                "objection_bridges": {"already_covered": "", "not_interested": "", "send_info": ""},
            },
            "metadata": {"anchor_used": "none", "hook_used": "none"},
        },
        model_used="none",
        attempts=4,
        success=False,
        failures=all_failures,
    )
