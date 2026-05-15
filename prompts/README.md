# prompts/ — Module 4 prompt infrastructure

Files in this folder are loaded by Module 4 (LLM drafting) at draft time.

## File layout

| File | Purpose | Loaded by |
|------|---------|-----------|
| `draft-pipeline.txt` | **Operational template** — what Module 4 sends to the LLM, after `{placeholder}` substitution | Module 4 at every draft |
| `draft-pipeline-source.md` | **Style/voice canon** — Daniel's original tested prompt. Preserved as the source-of-truth for voice changes. | Engineers (human-read) |
| `ctas.json` | 5 approved CTAs filtered by `allowed_for` per prospect type | Module 4 at every draft |
| `README.md` | This file | Engineers (human-read) |

## Module 4 contract — what the operational template must do

The operational template (`draft-pipeline.txt`) is the prompt sent to the LLM. It must:

1. **Reference these input placeholders** (substituted at draft time):
   `{company_name}`, `{company_acronym}`, `{company_industry}`, `{contact_first_name}`, `{contact_last_name}`, `{contact_source}`, `{prospect_type}`, `{quote_amount}`, `{product_line}`, `{quote_date_human}`, `{send_reason}`, `{renewal_date_or_null}`, `{policy_excerpt_or_null}`, `{hook_text}`, `{hook_source}`, `{anchor_type}`, `{anchor_name}`, `{anchor_context}`, `{ctas_filtered}`

2. **Wrap all user-controlled inputs** in `<untrusted_input>...</untrusted_input>` tags (per A1 amendment). Module 4 substitutes the user-controlled values pre-wrapped; the prompt then instructs the LLM to never follow instructions inside those tags. Affected inputs:
   - `{policy_excerpt_or_null}`
   - `{anchor_context}`
   - `{hook_text}`
   - Any deal-note or engagement-body excerpts surfaced through Module 3

3. **Return a single JSON object** with this exact shape (validated by `validate()` in Module 4):

   ```json
   {
     "deal_note": "string, 50-150 words, plain text SDR brief",
     "email": {
       "subject_options": ["string ≤60 chars", "string ≤60 chars"],
       "body": "string, word count per variant",
       "word_count": 178,
       "cta_chosen": "verbatim CTA text from ctas_filtered"
     },
     "call_script": {
       "opening": "string",
       "context_bridge": "string",
       "observation": "string",
       "open_question": "string",
       "objection_bridges": {
         "already_covered": "string",
         "not_interested": "string",
         "send_info": "string"
       }
     },
     "metadata": {
       "anchor_used": "broker | agency_contact | prior_work | none",
       "hook_used":   "m_and_a | leadership_change | sector_stress | none"
     }
   }
   ```

4. **Branch on `prospect_type`** for body word count + structure:
   - `structured_re_engagement` variant — used for `former_client`, `bor_target`, `lost_opportunity`. 150-200 words, 4 paragraphs, relationship-anchor opening (dropped to 3 paragraphs / 120-150 words when `anchor_type = none`).
   - `warm_direct_ask` variant — used for `coi_referral`, `industry_trigger`, `conference`, `bankruptcy_trigger`. 120-160 words, 3 paragraphs, direct introduction.

5. **Branch on `send_reason`** with type-precedence:
   - `round-robin`: subject + body MUST NOT mention renewal dates. Generic value-touch only.
   - `renewal` + `former_client | lost_opportunity`: subject MAY reference the renewal window obliquely. Body may mention upcoming renewal without pressure.
   - `renewal` + `bor_target`: frame as "when your current term comes up for review" — NEVER "before your renewal." Type framing wins over urgency.
   - All other prospect types: never reference renewal dates regardless of `send_reason`.

6. **Anchor drop rule (the question resolved in autoplan):** if `anchor_type = none`, drop the relationship-anchor paragraph entirely. Do NOT manufacture a fallback. The email becomes shorter and that's correct.

7. **Negative constraints (locked):**
   - NEVER use "navigate" (case-insensitive)
   - No emojis
   - No "I hope this finds you well"
   - No mention of AI or automation
   - No superlatives ("amazing", "incredible")

8. **Value phrases (P27 — locked, use verbatim where natural):**
   - "shore up credit management"
   - "ensure AR stays an asset"
   - "leveraging every single carrier and AR resource"
   - "more competition and lower rates than just comparing two or three options"
   - "we run aggregated AR analysis monthly or quarterly for active clients to keep coverage and policy compliance up to date"

9. **Carrier list (P26 — locked):**
   Allianz (formerly Euler Hermes), Atradius, Coface, FCIA, AIG, plus specialty markets. NOT Markel. Use "Allianz (formerly Euler Hermes)" if Euler is mentioned in source data.

## Editing the operational template

The operational template is one of the highest-leverage files in the repo. Each draft (~3 per working day × 250 working days ≈ 750/year) goes through it.

Change-control protocol:
1. Update `draft-pipeline-source.md` first if the change is to voice or style.
2. Update `draft-pipeline.txt` to reflect the change in the operational form (placeholders, JSON output, variant branching).
3. Re-run the eval suite (`tests/evals/prompt_eval.py` — added in a future PR) against the 20-row fixture before merging.
4. Track validation-failure rate for 7 days after change. If failure rate spikes >30%, roll back.
