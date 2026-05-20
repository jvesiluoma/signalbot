"""
Process-local metrics counters surfaced by /api/intel/health.

Lives in its own module so future blueprints can update the same dict without
importing the full `app.py`. Reads are lock-free (atomic dict gets); writes
take the `_metrics_lock` because we do read-modify-write on increment.

Extracted from app.py during Phase 7.
"""

from __future__ import annotations

import threading

_metrics: dict = {
    "unparseable_reactions_total": 0,
    "summary_stubs_total": 0,
    "last_poll_at": None,
    "last_group_sync_at": None,
    "last_chat_sync_at": None,
    "last_ollama_summary_at": None,
}
_metrics_lock = threading.Lock()

# Worker thread registry — populated by `main()` in app.py after each .start()
# so the health endpoint can introspect liveness. Keys are thread names; values
# are the `threading.Thread` instance (so `is_alive()` can be polled cheaply).
_worker_threads: dict = {}


def metric_set(key, value):
    with _metrics_lock:
        _metrics[key] = value


def metric_inc(key, delta=1):
    with _metrics_lock:
        _metrics[key] = int(_metrics.get(key, 0) or 0) + delta


def metric_get(key, default=None):
    return _metrics.get(key, default)


def metrics_snapshot():
    """Return a shallow copy so callers can't accidentally mutate the source."""
    return dict(_metrics)
