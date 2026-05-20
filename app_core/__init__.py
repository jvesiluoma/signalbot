"""
`app_core` — shared utilities used across the dashboard, poller, identity engine,
and the (future) blueprint-extracted routes.

This package was carved out of the 9,200-line `app.py` monolith as the foundation
of the Phase 7 blueprint split. Right now it hosts the small leaf modules
authored during Phases 1, 2, and 6 of the intel-correctness fix-up:

  - auth_bootstrap  — autogenerate / load `.auth-secret` before any reader runs.
  - reaction_target — JID classifier for `reactions.target_*` columns.
  - live_feed       — process-local Condition + concurrency cap for SSE clients.

Future phases will move `db.py` (get_db_connection), `ollama.py` (OllamaClient),
`sanitize.py` (bleach wrappers), `templating.py` (Jinja filters), `schema.py`
(migration helpers), and `metrics.py` (`_metrics` dict) into this package. The
blueprint route modules will then live in `blueprints/` and import from here.
"""
