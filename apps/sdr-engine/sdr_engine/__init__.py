"""sdr_engine — internal automation library for TCIA's SDR reactivation engine.

Modules:
  llm   — Module 4: drafting via LiteLLM routing with structured validation
          + cloud fallback per A3/A15 amendments.

This package is consumed by:
  - n8n nodes via HTTP (the workflow JSON calls into a thin Python service)
  - ui/server.py for in-process actions
  - tests/ for unit + eval tests

It is not published — see pyproject.toml for the editable-install config.
"""
__version__ = "0.0.2"
