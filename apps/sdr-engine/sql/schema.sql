-- SDR Engine SQLite schema
-- Run once at install: sqlite3 ~/.sdr-engine/queue.db < sql/schema.sql
-- Re-runnable (CREATE IF NOT EXISTS everywhere).

-- ─── WAL mode + busy timeout (per A4 amendment) ─────────────
-- WAL allows concurrent reads while n8n writes; busy_timeout prevents
-- SQLITE_BUSY errors when n8n and UI write simultaneously.
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

-- ─── queue: the main work queue ─────────────────────────────
CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,

  -- Identity
  deal_id TEXT NOT NULL,
  company_id TEXT NOT NULL,
  company_name TEXT NOT NULL,
  contact_id TEXT NOT NULL,
  contact_email TEXT NOT NULL,
  contact_first_name TEXT,
  contact_last_name TEXT,

  -- Deal context (used in LLM substitution)
  quote_amount REAL,
  product_line TEXT,
  quote_date_human TEXT,                 -- e.g. "March 2024"
  renewal_date DATE,                     -- nullable; only populated when known
  policy_excerpt TEXT,                   -- nullable; OneDrive/SharePoint snippet
  hook_text TEXT,
  hook_source TEXT,                      -- m_and_a | leadership_change | sector_stress | none

  -- Classification
  send_reason TEXT NOT NULL
    CHECK (send_reason IN ('renewal', 'round-robin')),
  prospect_type TEXT NOT NULL
    CHECK (prospect_type IN (
      'former_client',
      'bor_target',
      'coi_referral',
      'lost_opportunity',
      'industry_trigger',
      'conference',
      'bankruptcy_trigger'
    )),
  -- Original funnel_type multi-checkbox values as JSON array, for analytics on
  -- multi-tagged deals (e.g., a deal carrying both 'former_client' and 'bor_target').
  funnel_types_all TEXT,
  contact_source TEXT NOT NULL DEFAULT 'existing'
    CHECK (contact_source IN ('existing', 'zoominfo_enriched')),

  -- Email artifact (shown in UI)
  draft_subject TEXT NOT NULL,           -- chosen subject (one of two options)
  draft_subject_alt TEXT,                -- the other subject option
  draft_body TEXT NOT NULL,
  cta_chosen TEXT,

  -- Deal note artifact (auto-posted to HubSpot, not edited in UI per T3=A)
  deal_note_body TEXT NOT NULL,

  -- Call script artifact (auto-posted as task body, not edited in UI per T3=A)
  -- JSON: {opening, context_bridge, observation, open_question,
  --        objection_bridges: {already_covered, not_interested, send_info}}
  call_script_json TEXT NOT NULL,

  -- LLM metadata
  anchor_used TEXT,                      -- broker | agency_contact | prior_work | none
  hook_used TEXT,                        -- m_and_a | leadership_change | sector_stress | none
  llm_model_used TEXT,                   -- local-main | heavy-main (which path delivered)
  llm_attempts INTEGER NOT NULL DEFAULT 1, -- 1 = first pass, 2 = retried, 3 = fallback, 4 = NEEDS_HUMAN

  -- Lifecycle
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'sent', 'skipped', 'edit-sent', 'force-enqueued', 'send_partial')),
  enqueued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  actioned_at TIMESTAMP,
  edit_diff TEXT,                        -- JSON: {"before": "<draft>", "after": "<edited>"}

  -- HubSpot write receipts
  hubspot_engagement_id TEXT,            -- email engagement
  hubspot_note_id TEXT,                  -- deal note engagement
  hubspot_task_id TEXT                   -- phone-call task
);

CREATE INDEX IF NOT EXISTS idx_queue_deal_status ON queue(deal_id, status);
CREATE INDEX IF NOT EXISTS idx_queue_status_actioned ON queue(status, actioned_at);
CREATE INDEX IF NOT EXISTS idx_queue_prospect_type ON queue(prospect_type, status);

-- ─── dropped: deals that couldn't enter the queue ───────────
-- Used by Module 1 cooldown logic. Reason determines cooldown duration:
-- Recoverable reasons skip the 90-day cooldown (per A11 amendment).
CREATE TABLE IF NOT EXISTS dropped (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id TEXT NOT NULL,
  reason TEXT NOT NULL
    CHECK (reason IN (
      'missing_prospect_type',           -- recoverable: re-eligible next run
      'company_not_in_hubspot',          -- recoverable: re-eligible next run
      'inactive_company',                -- 90-day cooldown
      'no_qualified_contact_at_company', -- 90-day cooldown
      'no_valid_contact_after_enrichment', -- 90-day cooldown
      'opted_out',                       -- permanent (compliance)
      'bad_email',                       -- permanent (compliance)
      'quarantined'                      -- permanent (compliance)
    )),
  dropped_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  metadata TEXT                          -- JSON for debug context
);

CREATE INDEX IF NOT EXISTS idx_dropped_deal_date ON dropped(deal_id, dropped_at);

-- ─── enrichment_cache: ZoomInfo lookup memo (A2 cache-first) ─
-- A2 amendment: row written BEFORE HubSpot writes, status='in_progress'.
-- On rollback failure, status='orphan' for manual cleanup.
-- Caches result for ENRICHMENT_CACHE_TTL_DAYS to avoid burning credits.
CREATE TABLE IF NOT EXISTS enrichment_cache (
  company_id TEXT PRIMARY KEY,
  contact_id TEXT,                       -- nullable while in_progress or on failure
  source TEXT NOT NULL DEFAULT 'zoominfo',
  status TEXT NOT NULL
    CHECK (status IN ('in_progress', 'success', 'no_match', 'error', 'orphan')),
  confidence REAL,
  matched_title TEXT,
  enriched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_enrichment_status ON enrichment_cache(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_enrichment_company_date ON enrichment_cache(company_id, enriched_at);

-- ─── runs: scheduler heartbeat (A6 amendment) ───────────────
-- Module 1 writes one row per scheduler run. Module 6 UI surfaces a
-- staleness banner if last successful run > 25h old. Module 8 sync
-- webhook fires on LLM/HubSpot errors recorded here.
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE,           -- uuid per scheduler invocation
  started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ended_at TIMESTAMP,
  status TEXT NOT NULL DEFAULT 'running'
    CHECK (status IN ('running', 'success', 'partial', 'failed')),
  enqueued_count INTEGER NOT NULL DEFAULT 0,
  dropped_count INTEGER NOT NULL DEFAULT 0,
  errors_jsonl TEXT                      -- newline-delimited error records, if any
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, started_at);
