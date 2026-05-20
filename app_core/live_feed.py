"""
Process-local coordination for the live-feed SSE endpoint.

The dashboard used to poll `/api/recent_messages` every 2 seconds; under
Werkzeug that's a fresh DB connection ~30 times/minute per open tab. The SSE
endpoint at `/api/stream/messages` replaces that with a long-lived connection
that only does work when there's actually new data — coordinated by the
`threading.Condition` exported here.

Producer side: `poller.insert_message()` calls `notify_new_message(row_id)`
after each successful INSERT commit.

Consumer side: the SSE handler in `app.py` waits on the condition with a
30-second timeout; on wake (or timeout) it pulls `messages WHERE id > since`
and emits SSE events, then loops.

A `BoundedSemaphore(8)` caps concurrent SSE clients so a runaway dashboard
can't tie up every Werkzeug worker. When exhausted, the SSE route returns
HTTP 503 + Retry-After.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("live_feed")

_cond = threading.Condition()
_latest_id: int = 0  # monotonically increasing; protected by _cond
_concurrent_sse = threading.BoundedSemaphore(8)  # cap dashboard tabs


def notify_new_message(row_id: int) -> None:
    """Wake every SSE client. Idempotent — multiple calls between waiters
    coalesce because waiters re-read `_latest_id` after waking."""
    if not row_id:
        return
    with _cond:
        global _latest_id
        if row_id > _latest_id:
            _latest_id = int(row_id)
        _cond.notify_all()


def get_latest_id() -> int:
    """Snapshot of the highest known row_id (best-effort, no locking).
    Used by the SSE handler to skip a round-trip when nothing has changed."""
    return _latest_id


def wait_for_new(since_id: int, timeout: float = 30.0) -> int:
    """Block until `_latest_id > since_id` OR the timeout elapses.

    Returns the current `_latest_id`. Callers should compare it against
    `since_id` themselves — equality means we timed out and should emit a
    heartbeat instead of fetching."""
    with _cond:
        if _latest_id > since_id:
            return _latest_id
        _cond.wait(timeout=timeout)
        return _latest_id


def acquire_slot(blocking: bool = False) -> bool:
    """Try to claim one of the bounded SSE worker slots. Returns False if all
    8 are taken; the route then returns 503 Retry-After to the client."""
    return _concurrent_sse.acquire(blocking=blocking)


def release_slot() -> None:
    try:
        _concurrent_sse.release()
    except ValueError:
        # Released more than acquired — shouldn't happen, but don't crash.
        logger.warning("live_feed.release_slot called without a matching acquire")
