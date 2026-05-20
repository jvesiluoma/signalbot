"""
Single ingest path for normalized platform events.

`ingest_event(conn, evt)` is the one place that turns a `connectors.base.CanonicalEvent`
into rows in `messages` / `reactions` / `message_quotes` / `message_mentions` /
`message_attachments` / `page_snapshots` / `group_membership_events` /
`url_observations` / `chats`. The per-platform connector adapters (Telegram,
WhatsApp) call this; Signal's poll loop additionally uses `upsert_chat()` and
`record_url_observations()` from here while keeping its long-standing inline
insert path (which already produces equivalent rows).

To avoid an import cycle (`poller` ↔ `ingest`), the `poller` import is done
lazily inside the functions that need it.
"""

from __future__ import annotations

import datetime
import json
import logging
import re

import mysql.connector

import url_norm
from connectors.base import (
    CanonicalEvent, PLATFORM_SIGNAL,
    EV_MESSAGE, EV_EDIT, EV_DELETE, EV_REACTION, EV_REACTION_REMOVE,
    EV_JOIN, EV_LEAVE, EV_ADMIN_GRANT, EV_ADMIN_REVOKE, EV_CHAT_RENAME, EV_CHAT_META,
    EV_ACTIVITY,
)

logger = logging.getLogger("ingest")

_URL_RE = re.compile(r'https?://\S+')

# Membership-ish events map straight onto group_membership_events.event_type.
_MEMBERSHIP_EVENT_TYPES = {
    EV_JOIN: "join",
    EV_LEAVE: "leave",
    EV_ADMIN_GRANT: "admin_grant",
    EV_ADMIN_REVOKE: "admin_revoke",
    EV_CHAT_RENAME: "name_change",
    EV_CHAT_META: "description_change",
}


# ──────────────────────────────────────────────
# Small reusable writers
# ──────────────────────────────────────────────

def _ms_to_dt(ms):
    if not ms or not isinstance(ms, (int, float)):
        return None
    try:
        return datetime.datetime.fromtimestamp(ms / 1000.0)
    except (OverflowError, OSError, ValueError):
        return None


def upsert_chat(conn, platform, platform_chat_id, *, title=None, kind="group",
                is_public=False, member_count=None, connector_id=None,
                raw_meta=None, now=None):
    """INSERT … ON DUPLICATE KEY UPDATE on `chats`. Best-effort (table may be absent)."""
    if not platform or not platform_chat_id:
        return
    now = now or datetime.datetime.now()
    try:
        raw_json = json.dumps(raw_meta, ensure_ascii=False) if raw_meta is not None else None
    except Exception:
        raw_json = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO chats (platform, platform_chat_id, connector_id, title, kind,
                               is_public, member_count, first_seen_at, last_seen_at, raw_meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                connector_id = COALESCE(VALUES(connector_id), connector_id),
                title        = COALESCE(VALUES(title), title),
                kind         = VALUES(kind),
                is_public    = VALUES(is_public),
                member_count = COALESCE(VALUES(member_count), member_count),
                last_seen_at = VALUES(last_seen_at),
                raw_meta     = COALESCE(VALUES(raw_meta), raw_meta)
            """,
            (platform, str(platform_chat_id)[:190], connector_id,
             (title or None) and str(title)[:255], kind if kind in ("group", "channel", "dm") else "group",
             1 if is_public else 0, member_count, now, now, raw_json),
        )
        conn.commit()
    except mysql.connector.Error as err:
        logger.debug("upsert_chat failed (%s:%s): %s", platform, platform_chat_id, err)


def resolve_known_chat_title(conn, platform, platform_chat_id):
    """Last-known good title for a chat, or None.

    Connector-supplied titles can be transiently null — e.g. the WhatsApp
    connector keeps its chat list in an in-memory Map that is empty for a
    while after every restart, so messages arriving before the group's
    subject is relearned would otherwise be stored as "Unknown". This is the
    multi-platform analogue of poller._resolve_signal_group_name(): prefer the
    persisted `chats.title`, then fall back to the most recent non-"Unknown"
    `messages.group_name` for the same (platform, platform_chat_id).
    """
    if not platform or not platform_chat_id:
        return None
    pcid = str(platform_chat_id)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT title FROM chats "
            "WHERE platform=%s AND platform_chat_id=%s "
            "AND title IS NOT NULL AND title <> '' AND title <> 'Unknown' LIMIT 1",
            (platform, pcid[:190]),
        )
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
        cur.execute(
            "SELECT group_name FROM messages "
            "WHERE platform=%s AND platform_chat_id=%s "
            "AND group_name IS NOT NULL AND group_name <> '' AND group_name <> 'Unknown' "
            "ORDER BY id DESC LIMIT 1",
            (platform, pcid),
        )
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
    except mysql.connector.Error as err:
        logger.debug("resolve_known_chat_title failed (%s:%s): %s", platform, platform_chat_id, err)
    return None


def record_url_observations(conn, message_id, urls, *, platform, platform_chat_id,
                            chat_title=None, sender_phone=None, platform_user_id=None,
                            observed_at=None):
    """Insert one `url_observations` row per (normalized) URL. Best-effort."""
    if not urls:
        return
    observed_at = observed_at or datetime.datetime.now()
    rows = []
    seen = set()
    for u in urls:
        if not u:
            continue
        nu = url_norm.normalize_url(str(u))
        if not nu or nu in seen:
            continue
        seen.add(nu)
        rows.append((message_id, nu[:2083], (url_norm.extract_domain(nu) or "")[:255] or None,
                     platform, (str(platform_chat_id)[:190] if platform_chat_id else None),
                     (chat_title or "")[:255] or None, (sender_phone or "")[:64] or None,
                     (str(platform_user_id)[:190] if platform_user_id else None), observed_at))
    if not rows:
        return
    try:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO url_observations "
            "(message_id, normalized_url, domain, platform, platform_chat_id, "
            " chat_title, sender_phone, platform_user_id, observed_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            rows,
        )
        conn.commit()
    except mysql.connector.Error as err:
        logger.debug("record_url_observations failed: %s", err)


_caption_adapters = {}  # platform -> adapter (lazy, cached for fetch_file reuse)


def _caption_adapter(platform):
    """Lazily build & cache the connector adapter for byte fetching.

    Lazy import keeps the documented poller↔ingest / connector import discipline
    and avoids paying for it when captioning is off.
    """
    if platform in _caption_adapters:
        return _caption_adapters[platform]
    adapter = None
    try:
        if platform == "whatsapp":
            from connectors.whatsapp_adapter import WhatsAppAdapter
            adapter = WhatsAppAdapter(connector_id="wa-1")
        elif platform == "telegram":
            from connectors.telegram_adapter import TelegramAdapter
            adapter = TelegramAdapter(connector_id="tg-1")
    except Exception as e:
        logger.debug("caption adapter build failed (%s): %s", platform, e)
    _caption_adapters[platform] = adapter
    return adapter


def _persist_caption_source_bytes(conn, evt, debug=False):
    """Capture image/video attachment bytes while the connector cache is warm.

    WhatsApp/Telegram attachment bytes are not otherwise stored (the connector
    keeps them in an ephemeral in-memory cache). This downloads them at ingest,
    downscales images, size-caps video, and persists into the existing
    `attachments` table (deduped by md5) so the async caption worker — and the
    /attachments page, which already joins on file_name — can use them later.
    Best-effort: never raises, never blocks ingest.
    """
    import config
    if not config.CAPTION_INGEST_PERSIST or evt.platform == PLATFORM_SIGNAL:
        return  # Signal bytes are already persisted by poller.poll_attachments()
    if not evt.attachments:
        return
    try:
        import hashlib
        import image_caption
    except Exception as e:
        logger.debug("caption deps unavailable: %s", e)
        return
    adapter = _caption_adapter(evt.platform)
    if adapter is None:
        return
    for a in evt.attachments:
        aid = getattr(a, "id", None)
        fetch_url = getattr(a, "fetch_url", None)
        if not aid or not fetch_url:
            continue
        media = image_caption.classify_media(a.content_type, a.file_name)
        if media == "image" and not config.IMAGE_CAPTION_ENABLED:
            continue
        if media == "video" and not config.VIDEO_CAPTION_ENABLED:
            continue
        if media is None:
            continue
        try:
            raw = adapter.fetch_file(fetch_url)
        except Exception as e:
            logger.debug("caption byte fetch failed (%s %s): %s", evt.platform, aid, e)
            continue
        if not raw:
            continue
        if media == "image":
            blob = image_caption.preprocess_image(raw)
        else:
            blob = raw if len(raw) <= config.CAPTION_VIDEO_MAX_BYTES else None
        if not blob:
            continue
        md5 = hashlib.md5(blob).hexdigest()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT IGNORE INTO attachments (file_name, file_content, md5sum, platform) "
                "VALUES (%s, %s, %s, %s)",
                (str(aid)[:255], blob, md5, evt.platform),
            )
            conn.commit()
            if debug:
                logger.debug("ingest: persisted %s caption-source %s (%d bytes, md5=%s)",
                             evt.platform, aid, len(blob), md5)
        except mysql.connector.Error as err:
            logger.debug("caption-source persist failed (%s): %s", aid, err)


# ──────────────────────────────────────────────
# The single ingest entry point (used by the Telegram/WhatsApp adapters)
# ──────────────────────────────────────────────

def ingest_event(conn, evt: CanonicalEvent, *, do_screenshot=True, debug=False):
    """Persist one CanonicalEvent. Returns the new messages.id for message events
    (or None). Never raises for a normal data error — logs and moves on.

    URL screenshots are taken inline here; per-URL AI analysis is *not* — the
    Signal poll loop's `ai_main()` pass picks up any messages.url row regardless
    of platform, so Telegram/WhatsApp URLs get analyzed on the next cycle.
    """
    try:
        evt.validate()
    except ValueError as e:
        logger.warning("ingest_event: dropping invalid event: %s", e)
        return None

    # Lazy import — poller pulls in playwright/etc., and importing it at module
    # top would create a cycle (poller imports this module's helpers).
    import poller

    et = evt.event_type
    if et in (EV_MESSAGE, EV_EDIT):
        return _ingest_message(conn, poller, evt, do_screenshot=do_screenshot, debug=debug)
    if et == EV_DELETE:
        return _ingest_delete(conn, poller, evt, debug=debug)
    if et in (EV_REACTION, EV_REACTION_REMOVE):
        return _ingest_reaction(conn, poller, evt, debug=debug)
    if et in _MEMBERSHIP_EVENT_TYPES:
        return _ingest_membership(conn, poller, evt, debug=debug)
    if et == EV_ACTIVITY:
        return _ingest_activity(conn, evt, debug=debug)
    if debug:
        logger.debug("ingest_event: nothing to do for event_type=%s", et)
    return None


# ── message / edit ──

def _ingest_message(conn, poller, evt, *, do_screenshot=True, debug=False):
    chat = evt.chat
    sender = evt.sender
    group_id = evt.legacy_group_id
    chat_title = (chat.title if chat else None) \
        or resolve_known_chat_title(conn, evt.platform, evt.platform_chat_id)
    group_name = chat_title or "Unknown"
    sender_name = (sender.display_name if sender else None) or "Unknown"
    sender_phone = evt.legacy_sender_phone or "Unknown"
    sent_dt = _ms_to_dt(evt.timestamp_ms) or datetime.datetime.now()

    text = evt.text or ""
    # Trust the connector's URL extraction; fall back to a regex over the text.
    urls = list(evt.urls or []) or _URL_RE.findall(text)
    cleaned = _URL_RE.sub("", text).strip() if urls else text.strip()
    url_field = "|".join(urls) if urls else ""

    # Synthesize a stable per-message id for the multi-platform dedup key.
    pmid = evt.platform_msg_id or f"{evt.platform_user_id or sender_phone}:{evt.timestamp_ms or ''}"

    screenshot = None
    page_html = None
    if urls and do_screenshot:
        try:
            screenshot, page_html = poller.take_screenshot(urls[0], debug=debug)
        except Exception as e:
            logger.debug("ingest screenshot failed for %s: %r", urls[0], e)

    try:
        raw_json = json.dumps(evt.raw, ensure_ascii=False) if evt.raw is not None else None
    except Exception:
        raw_json = None

    is_edit = (evt.event_type == EV_EDIT)
    edited_at = sent_dt if is_edit else None

    # An edit of a message we already have → UPDATE in place; otherwise insert.
    if is_edit and evt.edit_of and evt.edit_of.platform_msg_id:
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE messages SET message=%s, url=%s, edited_at=%s, raw_envelope=%s "
                "WHERE platform=%s AND platform_chat_id=%s AND platform_msg_id=%s",
                (cleaned, url_field, edited_at or datetime.datetime.now(), raw_json,
                 evt.platform, evt.platform_chat_id, evt.edit_of.platform_msg_id),
            )
            conn.commit()
            if cur.rowcount:
                if debug:
                    logger.debug("ingest: edited message %s/%s", evt.platform, evt.edit_of.platform_msg_id)
                # also refresh url_observations? leave the originals; new ones get added below by message_id lookup
                return None
        except mysql.connector.Error as err:
            logger.debug("ingest edit-update failed: %s", err)

    msg_id = poller.insert_message(
        conn, sender_name, sender_phone, cleaned, url_field,
        group_name, group_id, sent_dt, screenshot=screenshot, debug=debug,
        source_uuid=(sender.platform_user_id if sender and evt.platform == PLATFORM_SIGNAL else None),
        source_device=None,
        server_received_ts=None, server_delivered_ts=None,
        expires_in_seconds=None, raw_envelope=raw_json, message_type='message',
        platform=evt.platform, connector_id=evt.connector_id,
        platform_chat_id=evt.platform_chat_id, platform_msg_id=pmid,
        platform_user_id=evt.platform_user_id,
        sender_username=(sender.username if sender else None),
        edited_at=edited_at,
    )

    # Always keep the chat registry fresh, even on duplicate messages.
    upsert_chat(conn, evt.platform, evt.platform_chat_id, title=chat_title,
                kind=(chat.kind if chat else "group"), is_public=bool(chat.is_public) if chat else False,
                member_count=(chat.members_count if chat else None), connector_id=evt.connector_id)

    if not msg_id:
        if evt.attachments:
            logger.debug("[ingest-attach] SKIP: no msg_id platform=%s chat=%s atts=%d",
                          evt.platform, evt.platform_chat_id, len(evt.attachments))
        return None

    # Reply / quote.
    if evt.reply_to and (evt.reply_to.platform_msg_id or evt.reply_to.text):
        rp = evt.reply_to
        is_uuid = bool(rp.author_user_id) and bool(re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", str(rp.author_user_id), re.I))
        quote_dict = {
            "id": rp.platform_msg_id, "targetSentTimestamp": rp.platform_msg_id,
            "text": rp.text or "",
        }
        if is_uuid:
            quote_dict["authorUuid"] = rp.author_user_id
        elif rp.author_user_id and str(rp.author_user_id).startswith("+"):
            quote_dict["authorNumber"] = rp.author_user_id
        elif rp.author_user_id:
            quote_dict["authorNumber"] = f"{evt.platform}:{rp.author_user_id}"
        poller.insert_quote(conn, msg_id, quote_dict, debug=debug, platform=evt.platform)

    # Mentions.
    if evt.mentions:
        mlist = []
        for m in evt.mentions:
            uid = m.platform_user_id
            if uid and re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", str(uid), re.I):
                mlist.append({"uuid": uid})
            elif uid and str(uid).startswith("+"):
                mlist.append({"number": uid})
            elif uid:
                mlist.append({"number": f"{evt.platform}:{uid}"})
        if mlist:
            poller.insert_mentions(conn, msg_id, mlist, debug=debug, platform=evt.platform)

    # Attachments (metadata only; bytes fetched separately by the connector if needed).
    if evt.attachments:
        alist = [{"id": a.id, "filename": a.file_name, "contentType": a.content_type, "size": a.size}
                 for a in evt.attachments if getattr(a, "id", None)]
        if alist:
            logger.debug("[ingest-attach] msg_id=%s atts=%d -> inserting",
                          msg_id, len(alist))
            poller.insert_message_attachments(
                conn, msg_id, alist, sender_name, sender_phone,
                group_name, group_id, sent_dt, debug=debug, platform=evt.platform,
            )
        # Capture image/video bytes now, while the connector's media cache is
        # still warm (best-effort; never breaks ingest).
        try:
            _persist_caption_source_bytes(conn, evt, debug=debug)
        except Exception as e:
            logger.debug("caption-source capture skipped: %s", e)

    # Page snapshot + tracked URL.
    if page_html and urls:
        poller.insert_page_snapshot(conn, urls[0], page_html, sent_dt,
                                    message_id=msg_id, group_name=group_name,
                                    debug=debug, platform=evt.platform)
        try:
            cur = conn.cursor()
            cur.execute("INSERT IGNORE INTO tracked_urls (url) VALUES (%s)", (urls[0],))
            conn.commit()
        except Exception:
            pass

    # URL observations (cross-platform analytics).
    if urls:
        record_url_observations(conn, msg_id, urls, platform=evt.platform,
                                platform_chat_id=evt.platform_chat_id,
                                chat_title=(chat.title if chat else None),
                                sender_phone=(sender.phone if sender else None),
                                platform_user_id=evt.platform_user_id, observed_at=sent_dt)

    if debug:
        logger.debug("ingest: stored %s message id=%s group=%s urls=%d",
                     evt.platform, msg_id, group_name, len(urls))
    return msg_id


# ── delete ──

def _ingest_delete(conn, poller, evt, *, debug=False):
    target = evt.delete_of.platform_msg_id if evt.delete_of else None
    deleter_phone = evt.legacy_sender_phone
    now = _ms_to_dt(evt.timestamp_ms) or datetime.datetime.now()
    try:
        cur = conn.cursor()
        if target:
            cur.execute(
                "UPDATE messages SET deleted_at=%s "
                "WHERE platform=%s AND platform_chat_id=%s AND platform_msg_id=%s",
                (now, evt.platform, evt.platform_chat_id, target),
            )
        cur.execute(
            "INSERT IGNORE INTO remote_deletes "
            "(deleter_phone, deleter_uuid, deleter_name, target_sent_ts, group_id, group_name, observed_at, platform) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (deleter_phone, evt.platform_user_id, (evt.sender.display_name if evt.sender else None),
             0, evt.legacy_group_id, (evt.chat.title if evt.chat else None), now, evt.platform),
        )
        conn.commit()
    except mysql.connector.Error as err:
        logger.debug("ingest delete failed: %s", err)
    return None


# ── reaction ──

def _ingest_reaction(conn, poller, evt, *, debug=False):
    rx = evt.reaction
    if not rx:
        return None
    sender = evt.sender
    # Build a Signal-shaped envelope/reaction so we can reuse poller.insert_reaction.
    reactor_phone = sender.phone if sender else None
    reactor_uuid = None
    if sender and sender.platform_user_id and re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", str(sender.platform_user_id), re.I):
        reactor_uuid = sender.platform_user_id
    if not reactor_phone and sender and sender.platform_user_id:
        reactor_phone = f"{evt.platform}:{sender.platform_user_id}"

    target_id = rx.target_author_id
    target_kw = {}
    if target_id and re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", str(target_id), re.I):
        target_kw["targetAuthorUuid"] = target_id
        target_kw["targetAuthor"] = target_id
    elif target_id and str(target_id).startswith("+"):
        target_kw["targetAuthorNumber"] = target_id
        target_kw["targetAuthor"] = target_id
    elif target_id:
        target_kw["targetAuthorNumber"] = f"{evt.platform}:{target_id}"

    envelope = {
        "sourceNumber": reactor_phone, "sourceUuid": reactor_uuid,
        "sourceName": (sender.display_name if sender else None),
        "timestamp": evt.timestamp_ms,
    }
    reaction = {
        "emoji": rx.emoji or "", "isRemove": bool(rx.is_remove or evt.event_type == EV_REACTION_REMOVE),
        "targetSentTimestamp": rx.target_msg_id or 0, **target_kw,
    }
    try:
        poller.insert_reaction(conn, envelope, reaction, evt.legacy_group_id,
                               (evt.chat.title if evt.chat else None), debug=debug, platform=evt.platform)
    except mysql.connector.Error as err:
        logger.debug("ingest reaction failed: %s", err)
    return None


# ── membership / chat metadata ──

def _ingest_membership(conn, poller, evt, *, debug=False):
    ev_type = _MEMBERSHIP_EVENT_TYPES.get(evt.event_type)
    if not ev_type:
        return None
    sender = evt.sender
    phone = (sender.phone if sender else None)
    uuid = None
    if sender and sender.platform_user_id and re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", str(sender.platform_user_id), re.I):
        uuid = sender.platform_user_id
    if not phone and sender and sender.platform_user_id:
        phone = f"{evt.platform}:{sender.platform_user_id}"
    now = _ms_to_dt(evt.timestamp_ms) or datetime.datetime.now()
    try:
        cur = conn.cursor()
        poller._record_membership_event(
            cur, evt.legacy_group_id, (evt.chat.title if evt.chat else None),
            phone, uuid, ev_type, detail=(evt.text or None), now=now, platform=evt.platform,
        )
        conn.commit()
    except mysql.connector.Error as err:
        logger.debug("ingest membership event failed: %s", err)
    upsert_chat(conn, evt.platform, evt.platform_chat_id, title=(evt.chat.title if evt.chat else None),
                kind=(evt.chat.kind if evt.chat else "group"), connector_id=evt.connector_id)
    return None


# ── activity (device-state observation; Phase F) ──

def _ingest_activity(conn, evt, *, debug=False):
    """Record a device-activity observation from a connector (currently WhatsApp
    receipt updates). We don't have an outgoing-probe RTT for these, so we just
    log the observed state. Gated upstream by *_ACTIVITY_TRACKER_ENABLED."""
    sender = evt.sender
    phone = (sender.phone if sender else None) or evt.legacy_sender_phone
    txt = (evt.text or "").lower()
    state = "active" if "read" in txt else ("standby" if "deliver" in txt else "offline")
    now = _ms_to_dt(evt.timestamp_ms) or datetime.datetime.now()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO activity_samples "
            "(probe_id, target_phone, target_uuid, source_device, receipt_type, rtt_ms, "
            " state, median_ms_used, observed_at, platform) "
            "VALUES (NULL, %s, NULL, NULL, %s, NULL, %s, NULL, %s, %s)",
            (phone, txt[:16] or None, state, now, evt.platform),
        )
        conn.commit()
        if debug:
            logger.debug("ingest activity: %s %s -> %s", evt.platform, phone, state)
    except mysql.connector.Error as err:
        logger.debug("ingest activity failed: %s", err)
    return None
