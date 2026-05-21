"""HTTP client wrappers for external services.

zoominfo  — search-then-enrich pattern (credits only burn on enrich)
hubspot   — contact create + associations + rollback delete

Each client is a thin wrapper: configurable base URL, JSON in/out,
typed result objects, raises requests.RequestException on transport
failure. No retry logic at this layer — that lives in the orchestrator.
"""
