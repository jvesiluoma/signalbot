"""
Runtime glue for the Telegram / WhatsApp connectors:

  * `connector_poller_loop` — pull events from pull-mode connector adapters
    (Telegram by default) and feed them to `ingest.ingest_event()`. Cursors are
    persisted in the `connector_cursors` table for at-least-once replay.
  * `chat_sync_loop` — periodically pull each connector's chat list (and, where
    the connector can enumerate them, member list) into `chats` /
    `group_members` / `group_membership_events`, mirroring what
    `poller.run_group_sync_loop` does for Signal.

Both are designed to run as daemon threads spawned from `app.main()`. They no-op
quietly when the relevant `*_ENABLED` flag is off.
"""

from __future__ import annotations

import datetime
import logging

import mysql.connector

import config
import ingest
from poller import get_db_connection_with_retry

logger = logging.getLogger("connector_runtime")


# ──────────────────────────────────────────────
# Adapter discovery
# ──────────────────────────────────────────────

def _pull_adapters():
    """Return [(adapter, connector_id)] for connectors we should *poll* for events.

    Telegram is pull-mode (the tg-connector keeps a ring buffer and we GET
    /v1/events). WhatsApp is push-mode by default (the wa-connector POSTs to
    /ingest/whatsapp), so it's not polled here unless WA_INGEST_MODE=pull.
    """
    out = []
    if config.TELEGRAM_ENABLED:
        try:
            from connectors.telegram_adapter import TelegramAdapter
            out.append((TelegramAdapter(connector_id="tg-1"), "tg-1"))
        except Exception:
            logger.exception("failed to construct TelegramAdapter")
    if config.WHATSAPP_ENABLED and __import__("os").getenv("WA_INGEST_MODE", "push") == "pull":
        try:
            from connectors.whatsapp_adapter import WhatsAppAdapter
            out.append((WhatsAppAdapter(connector_id="wa-1"), "wa-1"))
        except Exception:
            logger.exception("failed to construct WhatsAppAdapter")
    return out


def _sync_adapters():
    """Return [adapter] for connectors whose chat/member lists we should sync."""
    out = []
    if config.TELEGRAM_ENABLED:
        try:
            from connectors.telegram_adapter import TelegramAdapter
            out.append(TelegramAdapter(connector_id="tg-1"))
        except Exception:
            logger.exception("failed to construct TelegramAdapter")
    if config.WHATSAPP_ENABLED:
        try:
            from connectors.whatsapp_adapter import WhatsAppAdapter
            out.append(WhatsAppAdapter(connector_id="wa-1"))
        except Exception:
            logger.exception("failed to construct WhatsAppAdapter")
    if not out:
        return out
    return out


# ──────────────────────────────────────────────
# Cursor persistence
# ──────────────────────────────────────────────

def _load_cursor(conn, connector_id):
    try:
        cur = conn.cursor()
        cur.execute("SELECT cursor FROM connector_cursors WHERE connector_id=%s", (connector_id,))
        row = cur.fetchone()
        return row[0] if row else None
    except mysql.connector.Error:
        return None


def _save_cursor(conn, connector_id, cursor):
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO connector_cursors (connector_id, cursor) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE cursor=VALUES(cursor)",
            (connector_id, str(cursor) if cursor is not None else None),
        )
        conn.commit()
    except mysql.connector.Error as err:
        logger.debug("save cursor failed for %s: %s", connector_id, err)


# ──────────────────────────────────────────────
# Loops
# ──────────────────────────────────────────────

def connector_poller_loop(shutdown_event, debug=False):
    adapters = _pull_adapters()
    if not adapters:
        logger.info("connector-poller: no pull-mode connectors enabled")
        return
    logger.info("connector-poller: %d adapter(s): %s", len(adapters), [a[1] for a in adapters])

    db = None
    while db is None and not shutdown_event.is_set():
        db = get_db_connection_with_retry()
    shutdown_event.wait(timeout=20)  # let ensure_db_indexes() finish

    cursors = {cid: _load_cursor(db, cid) for (_a, cid) in adapters}
    interval = max(2, min(config.TG_POLL_INTERVAL, getattr(config, "WA_POLL_INTERVAL", 5)))

    while not shutdown_event.is_set():
        try:
            if db is None or not db.is_connected():
                db = get_db_connection_with_retry()
                if db is None:
                    shutdown_event.wait(timeout=10)
                    continue
            for adapter, cid in adapters:
                try:
                    events, next_cursor = adapter.fetch_events(cursors.get(cid))
                except Exception as e:
                    logger.warning("connector-poller: %s fetch_events failed: %s", cid, e)
                    continue
                for ev in events:
                    try:
                        ingest.ingest_event(db, ev, debug=debug)
                    except Exception:
                        logger.exception("connector-poller: ingest_event failed (%s)", cid)
                if next_cursor and next_cursor != cursors.get(cid):
                    cursors[cid] = next_cursor
                    _save_cursor(db, cid, next_cursor)
                if events and debug:
                    logger.debug("connector-poller: %s ingested %d event(s)", cid, len(events))
        except mysql.connector.Error as err:
            logger.warning("connector-poller MySQL error: %s", err)
            db = None
        except Exception:
            logger.exception("connector-poller cycle error")
        shutdown_event.wait(timeout=interval)


def chat_sync_loop(shutdown_event, debug=False):
    adapters = _sync_adapters()
    if not adapters:
        logger.info("chat-sync: no connectors enabled")
        return
    logger.info("chat-sync: %d adapter(s)", len(adapters))

    db = None
    while db is None and not shutdown_event.is_set():
        db = get_db_connection_with_retry()
    shutdown_event.wait(timeout=30)
    interval = int(getattr(config, "GROUP_SYNC_INTERVAL", 900))

    while not shutdown_event.is_set():
        try:
            if db is None or not db.is_connected():
                db = get_db_connection_with_retry()
                if db is None:
                    shutdown_event.wait(timeout=30)
                    continue
            for adapter in adapters:
                try:
                    chats = adapter.list_chats()
                except Exception as e:
                    logger.warning("chat-sync: %s list_chats failed: %s", adapter.platform, e)
                    continue
                now = datetime.datetime.now()
                for ch in chats:
                    ingest.upsert_chat(db, adapter.platform, ch.platform_chat_id, title=ch.title,
                                       kind=ch.kind, is_public=bool(ch.is_public),
                                       member_count=ch.members_count, connector_id=adapter.connector_id,
                                       now=now)
                    # WhatsApp can enumerate all participants → mirror them into
                    # group_members. Telegram (Bot API) only sees admins, so we
                    # rely on the connector's join/leave events there instead.
                    if adapter.platform == "whatsapp":
                        try:
                            members = adapter.list_members(ch.platform_chat_id)
                        except Exception:
                            members = []
                        for m in members:
                            phone = m.phone or m.platform_user_id
                            if not phone:
                                continue
                            try:
                                cur = db.cursor()
                                cur.execute(
                                    "INSERT INTO group_members "
                                    "(group_id, member_phone, role, first_seen_at, last_seen_at, platform) "
                                    "VALUES (%s,%s,'member',%s,%s,'whatsapp') "
                                    "ON DUPLICATE KEY UPDATE last_seen_at=VALUES(last_seen_at), left_at=NULL",
                                    (f"whatsapp:{ch.platform_chat_id}", phone, now, now),
                                )
                                db.commit()
                            except mysql.connector.Error:
                                pass
                if debug:
                    logger.debug("chat-sync: %s synced %d chat(s)", adapter.platform, len(chats))
        except Exception:
            logger.exception("chat-sync cycle error")
        shutdown_event.wait(timeout=interval)
