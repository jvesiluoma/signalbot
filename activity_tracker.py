"""
Device Activity Tracker — probe scheduler + classifier.

Background thread: every ACTIVITY_PROBE_INTERVAL seconds, picks the next
enrolled target in round-robin order, finds a recent message of theirs in a
monitored group, and sends a reaction (immediately removed) that triggers a
delivery-receipt envelope back from the target's device. The receipt arrives
asynchronously on the main poller thread via poller.handle_receipt().

Receipt matching, classification, and sample insertion live in poller.py.
This module only issues probes, writes pending-probe rows, reaps timeouts,
and maintains the enrollment-error backoff state.

Everything here is gated on config.ACTIVITY_TRACKER_ENABLED.
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from typing import Optional

import mysql.connector

import config
import signal_api

logger = logging.getLogger("activity_tracker")

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

_TICK_INTERVAL_SECONDS = 10       # Granularity of the scheduler wakeup
_STARTUP_DELAY_SECONDS = 15       # Let the app settle before first probe
_GC_INTERVAL_SECONDS = 24 * 3600  # Daily sample cleanup


# ──────────────────────────────────────────────
# DB helpers (lazy import to avoid cyclic dependencies with app.py)
# ──────────────────────────────────────────────

def _get_db():
    from app import get_db_connection
    return get_db_connection()


# ──────────────────────────────────────────────
# Public API (called by app.py)
# ──────────────────────────────────────────────

def scheduler_loop(shutdown_event):
    """Main worker-thread entry point, registered in app.main()."""
    if not config.ACTIVITY_TRACKER_ENABLED:
        logger.info("Activity tracker disabled; scheduler loop exiting.")
        return

    logger.info(
        "Activity tracker scheduler started "
        "(interval=%ds jitter=±%ds ack_timeout=%ds max_enrolled=%d)",
        config.ACTIVITY_PROBE_INTERVAL, config.ACTIVITY_PROBE_JITTER,
        config.ACTIVITY_ACK_TIMEOUT, config.ACTIVITY_MAX_ENROLLED,
    )

    # Warm-up delay so the app can finish startup tasks before we start
    # firing outbound probes.
    if shutdown_event.wait(_STARTUP_DELAY_SECONDS):
        return

    last_gc_at = 0.0
    last_probe_times: dict[str, float] = {}

    while not shutdown_event.is_set():
        conn = _get_db()
        if conn is None:
            logger.warning("DB unavailable; retrying scheduler tick in 30s")
            if shutdown_event.wait(30):
                return
            continue

        try:
            _reap_timeouts(conn)
        except Exception:
            logger.exception("reap_timeouts failed")

        try:
            enrollments = _active_enrollments(conn)
        except Exception:
            logger.exception("active_enrollments query failed")
            enrollments = []

        now = time.monotonic()
        # Pick the enrollment that has gone the longest without a probe.
        next_target = None
        for enr in enrollments:
            phone = enr["target_phone"]
            last_at = last_probe_times.get(phone, 0.0)
            gap = now - last_at
            interval = _interval_with_jitter()
            if gap >= interval:
                if next_target is None or last_at < last_probe_times.get(
                    next_target["target_phone"], 0.0
                ):
                    next_target = enr

        if next_target is not None:
            phone = next_target["target_phone"]
            try:
                _probe_enrollment(conn, next_target)
            except Exception:
                logger.exception("probe_enrollment failed for %s", phone)
            last_probe_times[phone] = time.monotonic()

        # Periodic GC
        if time.monotonic() - last_gc_at > _GC_INTERVAL_SECONDS:
            try:
                _gc_old_samples(conn)
            except Exception:
                logger.exception("activity_samples GC failed")
            last_gc_at = time.monotonic()

        try:
            conn.close()
        except Exception:
            pass

        if shutdown_event.wait(_TICK_INTERVAL_SECONDS):
            return


def run_probe_once(conn, target_phone):
    """One-shot probe used by /debug/activity_probe.

    Does NOT require active enrollment (so it can be used to smoke-test
    before enrolling). Returns a dict with the probe outcome.
    """
    enrollment = _fetch_enrollment(conn, target_phone) or {
        "target_phone": target_phone,
        "target_uuid": None,
    }
    return _probe_enrollment(conn, enrollment, is_oneshot=True)


# ──────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────

def _interval_with_jitter() -> float:
    base = float(config.ACTIVITY_PROBE_INTERVAL)
    jitter = float(config.ACTIVITY_PROBE_JITTER)
    if jitter <= 0:
        return base
    return max(5.0, base + random.uniform(-jitter, jitter))


def _active_enrollments(conn):
    """Return active enrollments whose backoff has cleared."""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT id, target_phone, target_uuid, consecutive_errors,
               error_backoff_until
          FROM activity_enrollment
         WHERE active = 1
           AND (error_backoff_until IS NULL OR error_backoff_until < NOW())
         ORDER BY id ASC
        """
    )
    return cursor.fetchall()


def _fetch_enrollment(conn, target_phone):
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT * FROM activity_enrollment WHERE target_phone=%s",
        (target_phone,),
    )
    return cursor.fetchone()


def _select_probe_target(conn, target_phone):
    """Pick the most recent monitored-group message from this target within the
    eligibility window. Returns (group_id, target_sent_ts_ms, target_uuid) or None.
    """
    if not config.TARGET_GROUP_IDS:
        return None
    placeholders = ','.join(['%s'] * len(config.TARGET_GROUP_IDS))
    max_age_days = max(1, int(config.ACTIVITY_PROBE_TARGET_MAX_AGE_DAYS))

    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        f"""
        SELECT group_id, sent_timestamp, source_uuid, raw_envelope
          FROM messages
         WHERE sender_phone = %s
           AND group_id IN ({placeholders})
           AND sent_timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY)
           AND message_type = 'message'
         ORDER BY sent_timestamp DESC
         LIMIT 1
        """,
        (target_phone, *config.TARGET_GROUP_IDS, max_age_days),
    )
    row = cursor.fetchone()
    if not row or not row.get('sent_timestamp'):
        return None

    # Signal reactions MUST match the original message timestamp exactly (to
    # the millisecond). The `sent_timestamp` DATETIME column has only
    # second precision, so we pull the exact ms from the stored envelope JSON.
    target_sent_ts_ms = _envelope_timestamp_ms(row.get('raw_envelope'))
    if target_sent_ts_ms is None:
        dt: datetime = row['sent_timestamp']
        target_sent_ts_ms = int(dt.timestamp() * 1000)
        logger.warning(
            "No raw_envelope for %s; falling back to second-precision ts (reactions will likely 400)",
            target_phone,
        )
    return (row['group_id'], target_sent_ts_ms, row.get('source_uuid'))


def _envelope_timestamp_ms(raw_envelope) -> Optional[int]:
    """Extract the original Signal envelope.timestamp (ms) from stored JSON."""
    if not raw_envelope:
        return None
    try:
        if isinstance(raw_envelope, (bytes, bytearray)):
            raw_envelope = raw_envelope.decode("utf-8", "replace")
        env = json.loads(raw_envelope) if isinstance(raw_envelope, str) else raw_envelope
        ts = env.get("timestamp") if isinstance(env, dict) else None
        if isinstance(ts, int) and ts > 0:
            return ts
    except (ValueError, TypeError):
        return None
    return None


def _probe_enrollment(conn, enrollment, is_oneshot: bool = False) -> dict:
    """Fire a single probe for this enrollment. Returns an outcome dict.

    Flow: pick target → insert pending probe row → POST reaction → DELETE reaction.
    On API error, mark probe as errored and bump enrollment backoff state.
    """
    target_phone = enrollment["target_phone"]

    selection = _select_probe_target(conn, target_phone)
    if selection is None:
        logger.debug("No eligible probe target for %s; skipping", target_phone)
        return {
            "probed": False,
            "reason": "no_recent_monitored_message",
            "target_phone": target_phone,
        }
    group_id, target_sent_ts_ms, target_uuid = selection

    # Refresh target_uuid on the enrollment if we just learned it (helps the
    # receipt matcher in poller.handle_receipt).
    if target_uuid and not enrollment.get("target_uuid") and not is_oneshot:
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE activity_enrollment SET target_uuid=%s WHERE target_phone=%s",
                (target_uuid, target_phone),
            )
            conn.commit()
        except mysql.connector.Error:
            logger.debug("Could not persist target_uuid for %s", target_phone, exc_info=True)

    probe_sent_ms = int(time.time() * 1000)
    emoji = config.ACTIVITY_PROBE_EMOJI
    recipient = f"group.{group_id}"

    probe_id: Optional[int] = None
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO activity_probes
                (target_phone, target_uuid, group_id, target_author_phone,
                 target_sent_ts_ms, probe_sent_ms, emoji, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
            """,
            (target_phone, target_uuid, group_id, target_phone,
             target_sent_ts_ms, probe_sent_ms, emoji),
        )
        probe_id = cursor.lastrowid
        conn.commit()
    except mysql.connector.Error as err:
        logger.error("Failed to insert pending probe: %s", err)
        return {"probed": False, "reason": "db_error", "error": str(err)}

    # Fire the reaction. signal_api retries once on 429/5xx internally.
    try:
        signal_api.send_reaction(
            recipient=recipient,
            reaction=emoji,
            target_author=target_phone,
            target_timestamp=target_sent_ts_ms,
        )
    except signal_api.SignalAPIError as exc:
        _mark_probe_error(conn, probe_id, str(exc))
        _bump_enrollment_error(conn, target_phone)
        return {
            "probed": False,
            "reason": "send_reaction_failed",
            "error": str(exc),
            "probe_id": probe_id,
        }

    # Immediately remove if configured. Errors on the remove step are
    # logged but don't fail the probe — the RTT sample is already in flight.
    if config.ACTIVITY_PROBE_SELF_REMOVE:
        try:
            signal_api.remove_reaction(
                recipient=recipient,
                target_author=target_phone,
                target_timestamp=target_sent_ts_ms,
                reaction=emoji,
            )
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE activity_probes SET removed=1 WHERE id=%s",
                    (probe_id,),
                )
                conn.commit()
            except mysql.connector.Error:
                pass
        except signal_api.SignalAPIError as exc:
            logger.warning(
                "remove_reaction failed (probe=%s target=%s): %s",
                probe_id, target_phone, exc,
            )

    logger.info(
        "activity-probe sent probe=%s target=%s group=%s target_ts=%s emoji=%s",
        probe_id, target_phone, group_id, target_sent_ts_ms, emoji,
    )
    return {
        "probed": True,
        "probe_id": probe_id,
        "target_phone": target_phone,
        "group_id": group_id,
        "target_sent_ts_ms": target_sent_ts_ms,
        "probe_sent_ms": probe_sent_ms,
        "emoji": emoji,
    }


def _mark_probe_error(conn, probe_id, error_msg):
    if probe_id is None:
        return
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE activity_probes SET status='error', error_msg=%s WHERE id=%s",
            (error_msg[:500], probe_id),
        )
        cursor.execute(
            """
            INSERT INTO activity_samples
                (probe_id, target_phone, target_uuid, rtt_ms, state, observed_at)
            SELECT id, target_phone, target_uuid, NULL, 'error', NOW(3)
              FROM activity_probes WHERE id=%s
            """,
            (probe_id,),
        )
        conn.commit()
    except mysql.connector.Error:
        logger.exception("_mark_probe_error DB update failed")


def _bump_enrollment_error(conn, target_phone):
    """Increment the error counter; if it hits the threshold, apply backoff."""
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE activity_enrollment
               SET consecutive_errors = consecutive_errors + 1,
                   error_backoff_until = CASE
                       WHEN consecutive_errors + 1 >= %s
                       THEN DATE_ADD(NOW(), INTERVAL %s SECOND)
                       ELSE error_backoff_until
                   END
             WHERE target_phone = %s
            """,
            (
                config.ACTIVITY_PROBE_ERROR_THRESHOLD,
                config.ACTIVITY_PROBE_ERROR_BACKOFF,
                target_phone,
            ),
        )
        conn.commit()
    except mysql.connector.Error:
        logger.exception("_bump_enrollment_error failed")


def _reap_timeouts(conn):
    """Transition pending probes past ACK_TIMEOUT into 'timeout' + insert
    an offline sample for each so the timeline shows gaps as offline, not
    as missing data."""
    cutoff_ms = int(time.time() * 1000) - int(config.ACTIVITY_ACK_TIMEOUT) * 1000
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT id, target_phone, target_uuid
          FROM activity_probes
         WHERE status = 'pending'
           AND probe_sent_ms < %s
        """,
        (cutoff_ms,),
    )
    stale = cursor.fetchall()
    if not stale:
        return

    update_cursor = conn.cursor()
    for probe in stale:
        try:
            update_cursor.execute(
                "UPDATE activity_probes SET status='timeout' WHERE id=%s",
                (probe["id"],),
            )
            update_cursor.execute(
                """
                INSERT INTO activity_samples
                    (probe_id, target_phone, target_uuid, rtt_ms, state, observed_at)
                VALUES (%s, %s, %s, NULL, 'offline', NOW(3))
                """,
                (probe["id"], probe["target_phone"], probe.get("target_uuid")),
            )
        except mysql.connector.Error:
            logger.exception("_reap_timeouts: failed for probe %s", probe.get("id"))
    conn.commit()
    if stale:
        logger.info("Reaped %d timed-out probe(s) as offline samples", len(stale))


def _gc_old_samples(conn):
    """Delete activity_samples older than ACTIVITY_SAMPLE_RETENTION_DAYS.

    Also purges activity_probes whose last referencing sample is gone, so the
    probes table doesn't grow unbounded either.
    """
    retention = max(1, int(config.ACTIVITY_SAMPLE_RETENTION_DAYS))
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM activity_samples "
        "WHERE observed_at < DATE_SUB(NOW(), INTERVAL %s DAY)",
        (retention,),
    )
    deleted_samples = cursor.rowcount
    cursor.execute(
        "DELETE FROM activity_probes "
        "WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY) "
        "  AND id NOT IN (SELECT probe_id FROM activity_samples WHERE probe_id IS NOT NULL)",
        (retention,),
    )
    deleted_probes = cursor.rowcount
    conn.commit()
    logger.info(
        "activity_samples GC: removed %d samples and %d orphan probes older than %d days",
        deleted_samples, deleted_probes, retention,
    )
