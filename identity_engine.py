"""
Cross-platform identity engine.

`propose_links()` looks across `messages` / `url_observations` for evidence that
two per-platform accounts are the same person, and records `identity_links`
rows (status 'proposed', or 'confirmed' for exact-phone matches at/above
`IDENTITY_AUTOCONFIRM_THRESHOLD`). It also ensures every account it sees has at
least one identity, so the Identity Graph has something to draw.

It never auto-merges two *already-distinct* confirmed identities — that needs a
human (`merge_identities()`), and the evidence trail is kept in `evidence`.

Account key: `(platform, account_id)` where `account_id` is
`COALESCE(platform_user_id, sender_phone)` on a `messages` row. Identity
resolution is union-find over the `identity_links` graph.
"""

from __future__ import annotations

import json
import logging

import mysql.connector

import config
from poller import get_db_connection_with_retry

logger = logging.getLogger("identity_engine")


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _account_key(row):
    """row = (platform, platform_user_id, sender_phone, sender_name) → (platform, acct, phone, name)."""
    platform = (row[0] or "signal")
    acct = row[1] or row[2]
    phone = row[2] if (row[2] and str(row[2]).startswith("+")) else None
    return platform, acct, phone, (row[3] or None)


def _existing_links(cursor):
    """Return ({(platform,acct): identity_id}, {(platform,acct): set(link_method)})."""
    cursor.execute("SELECT platform, platform_user_id, identity_id, link_method, status "
                   "FROM identity_links WHERE status <> 'rejected'")
    by_acct = {}
    methods = {}
    for platform, acct, ident, method, _status in cursor.fetchall():
        by_acct[(platform, acct)] = ident
        methods.setdefault((platform, acct), set()).add(method)
    return by_acct, methods


def _new_identity(cursor, label, confirmed=False):
    cursor.execute("INSERT INTO identities (label, is_confirmed) VALUES (%s, %s)", (label, 1 if confirmed else 0))
    return cursor.lastrowid


def _add_link(cursor, identity_id, platform, acct, method, confidence, evidence, status):
    cursor.execute(
        "INSERT INTO identity_links (identity_id, platform, platform_user_id, link_method, confidence, evidence, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE "
        "  confidence = GREATEST(confidence, VALUES(confidence)), "
        "  evidence   = COALESCE(VALUES(evidence), evidence), "
        "  link_method = IF(VALUES(confidence) > confidence, VALUES(link_method), link_method), "
        "  status = IF(status='confirmed','confirmed',VALUES(status))",
        (identity_id, platform, acct, method, confidence,
         json.dumps(evidence) if evidence is not None else None, status),
    )


def _ensure_identity(cursor, by_acct, platform, acct, label, *, method='manual', confidence=0.0,
                     evidence=None, status='proposed'):
    """Make sure (platform, acct) maps to *some* identity. Returns the identity_id."""
    key = (platform, acct)
    if key in by_acct:
        return by_acct[key]
    ident = _new_identity(cursor, label or acct, confirmed=(status == 'confirmed'))
    _add_link(cursor, ident, platform, acct, method, confidence, evidence, status)
    by_acct[key] = ident
    return ident


# ──────────────────────────────────────────────
# Link proposers
# ──────────────────────────────────────────────

def _propose_phone_exact(cursor, by_acct):
    """Same E.164 phone → same person (auto-confirmed).

    Group-chat-only: `group_id IS NOT NULL AND group_id <> ''` filters out any
    1:1/DM messages that may have slipped past the ingest gates. The product is
    a groups-only observation platform; identity linking must not infer cross-
    platform identities from a private chat the bot has with one user."""
    cursor.execute(
        "SELECT platform, COALESCE(platform_user_id, sender_phone) AS acct, sender_phone, "
        "       SUBSTRING(MAX(sender_name),1,255) AS nm "
        "  FROM messages WHERE sender_phone LIKE '+%' "
        "   AND group_id IS NOT NULL AND group_id <> '' "
        " GROUP BY platform, acct, sender_phone"
    )
    by_phone = {}
    for r in cursor.fetchall():
        platform, acct, phone, name = _account_key(r)
        if not acct or not phone:
            continue
        by_phone.setdefault(phone, []).append((platform, acct, name))
    n = 0
    for phone, accts in by_phone.items():
        # Pick (or create) the identity for the first account, then attach the rest.
        accts = list({(p, a): nm for (p, a, nm) in accts}.items())  # de-dup
        first_key, first_nm = accts[0]
        ident = _ensure_identity(cursor, by_acct, first_key[0], first_key[1], first_nm,
                                 method='phone_exact', confidence=1.0,
                                 evidence={"phone": phone}, status='confirmed')
        for (p, a), nm in accts[1:]:
            existing = by_acct.get((p, a))
            if existing == ident:
                continue
            if existing is None:
                _add_link(cursor, ident, p, a, 'phone_exact', 1.0, {"phone": phone}, 'confirmed')
                by_acct[(p, a)] = ident
                n += 1
            else:
                # Two pre-existing identities with the same phone — propose a
                # merge link rather than silently fusing them.
                _add_link(cursor, ident, p, a, 'phone_exact', 0.95,
                          {"phone": phone, "conflict_identity": existing}, 'proposed')
                n += 1
    return n


def _propose_username_exact(cursor, by_acct):
    """Same case-insensitive username across ≥2 platforms → weak link.

    Groups-only: same DM filter as `_propose_phone_exact` above."""
    cursor.execute(
        "SELECT platform, COALESCE(platform_user_id, sender_phone) AS acct, LOWER(sender_username) AS un, "
        "       SUBSTRING(MAX(sender_name),1,255) "
        "  FROM messages WHERE sender_username IS NOT NULL AND sender_username <> '' "
        "   AND group_id IS NOT NULL AND group_id <> '' "
        " GROUP BY platform, acct, un"
    )
    by_un = {}
    for platform, acct, un, nm in cursor.fetchall():
        if not acct or not un:
            continue
        by_un.setdefault(un, []).append((platform, acct, nm))
    n = 0
    for un, accts in by_un.items():
        if len({(p, a) for (p, a, _n) in accts}) < 2:
            continue
        if len({p for (p, _a, _n) in accts}) < 2:
            continue  # same platform — can't be different people via same handle... actually it can't even be the same handle. skip.
        ident = None
        for (p, a, nm) in accts:
            existing = by_acct.get((p, a))
            if existing is not None:
                ident = existing
                break
        if ident is None:
            first = accts[0]
            ident = _ensure_identity(cursor, by_acct, first[0], first[1], first[2],
                                     method='username_exact', confidence=0.5,
                                     evidence={"username": un}, status='proposed')
        for (p, a, _n) in accts:
            if by_acct.get((p, a)) == ident:
                continue
            _add_link(cursor, ident, p, a, 'username_exact', 0.5, {"username": un}, 'proposed')
            if (p, a) not in by_acct:
                by_acct[(p, a)] = ident
            n += 1
    return n


def _propose_url_cooccurrence(cursor, by_acct):
    """Two accounts on different platforms posting the same normalized URL within
    IDENTITY_URL_COOCCURRENCE_WINDOW_S → behavioral link, confidence grows with
    the number of distinct co-posted URLs.

    Groups-only: `url_observations` has no `group_id` column, so we JOIN the
    `chats` registry on `(platform, platform_chat_id)` and gate on `kind='group'`.
    For pre-`chats`-population legacy data, a WhatsApp `@g.us` heuristic acts as a
    fallback so we don't accidentally exclude legitimate group rows.

    The `LIMIT 500` (no ORDER BY) of the legacy query is replaced with
    `ORDER BY shared DESC … LIMIT 5000` so newly-arrived heavy-shared pairs
    aren't randomly truncated."""
    window = max(60, config.IDENTITY_URL_COOCCURRENCE_WINDOW_S)
    cursor.execute(
        """
        SELECT a.platform, COALESCE(a.platform_user_id, a.sender_phone) AS a_acct,
               b.platform, COALESCE(b.platform_user_id, b.sender_phone) AS b_acct,
               COUNT(DISTINCT a.normalized_url) AS shared,
               MIN(a.observed_at)              AS first_seen
          FROM url_observations a
          JOIN url_observations b
            ON a.normalized_url = b.normalized_url
           AND a.platform < b.platform
           AND ABS(TIMESTAMPDIFF(SECOND, a.observed_at, b.observed_at)) <= %s
          LEFT JOIN chats ca
            ON ca.platform = a.platform AND ca.platform_chat_id = a.platform_chat_id
          LEFT JOIN chats cb
            ON cb.platform = b.platform AND cb.platform_chat_id = b.platform_chat_id
         WHERE a.normalized_url IS NOT NULL
           AND COALESCE(a.platform_user_id, a.sender_phone) IS NOT NULL
           AND COALESCE(b.platform_user_id, b.sender_phone) IS NOT NULL
           AND (ca.kind = 'group'
                OR (a.platform IN ('signal','telegram')   -- Signal/Telegram only see groups via ingest gate
                    AND ca.id IS NULL)                    --   so missing chats row → still group-safe
                OR (a.platform = 'whatsapp'
                    AND a.platform_chat_id LIKE '%%@g.us')) -- WA: only @g.us JIDs are groups
           AND (cb.kind = 'group'
                OR (b.platform IN ('signal','telegram') AND cb.id IS NULL)
                OR (b.platform = 'whatsapp'
                    AND b.platform_chat_id LIKE '%%@g.us'))
         GROUP BY a.platform, a_acct, b.platform, b_acct
        HAVING shared >= 2
         ORDER BY shared DESC, first_seen DESC
         LIMIT 5000
        """,
        (window,),
    )
    rows = cursor.fetchall()
    n = 0
    # `first_seen` is selected only for ORDER BY tie-breaking; ignored in the loop.
    for ap, aa, bp, ba, shared, _first_seen in rows:
        conf = min(0.5 + 0.08 * int(shared), 0.9)
        ka, kb = (ap, aa), (bp, ba)
        ia, ib = by_acct.get(ka), by_acct.get(kb)
        evidence = {"shared_urls": int(shared), "window_s": window}
        if ia is None and ib is None:
            ident = _new_identity(cursor, aa, confirmed=False)
            _add_link(cursor, ident, ap, aa, 'url_cooccurrence', conf, evidence, 'proposed')
            _add_link(cursor, ident, bp, ba, 'url_cooccurrence', conf, evidence, 'proposed')
            by_acct[ka] = by_acct[kb] = ident
            n += 2
        elif ia is not None and ib is None:
            _add_link(cursor, ia, bp, ba, 'url_cooccurrence', conf, evidence, 'proposed')
            by_acct[kb] = ia
            n += 1
        elif ib is not None and ia is None:
            _add_link(cursor, ib, ap, aa, 'url_cooccurrence', conf, evidence, 'proposed')
            by_acct[ka] = ib
            n += 1
        elif ia != ib:
            # Different identities — record a cross-link proposal for human review.
            _add_link(cursor, ia, bp, ba, 'url_cooccurrence', conf,
                      {**evidence, "conflict_identity": ib}, 'proposed')
            n += 1
    return n


def _ensure_all_accounts_have_identity(cursor, by_acct):
    """Give every per-platform account at least one identity (so the graph is complete).

    Groups-only: DM-sourced accounts are excluded so a 1:1 chat partner doesn't
    materialize as a half-stub identity in the graph."""
    cursor.execute(
        "SELECT platform, COALESCE(platform_user_id, sender_phone) AS acct, "
        "       SUBSTRING(MAX(sender_name),1,255) "
        "  FROM messages WHERE COALESCE(platform_user_id, sender_phone) IS NOT NULL "
        "   AND group_id IS NOT NULL AND group_id <> '' "
        " GROUP BY platform, acct"
    )
    n = 0
    for platform, acct, name in cursor.fetchall():
        if (platform, acct) in by_acct:
            continue
        _ensure_identity(cursor, by_acct, platform, acct, name,
                         method='manual', confidence=0.0, evidence=None, status='confirmed')
        n += 1
    return n


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def propose_links(conn):
    """Run all proposers. Returns dict of per-method counts. Best-effort."""
    counts = {}
    try:
        cursor = conn.cursor()
        by_acct, _methods = _existing_links(cursor)
        counts["phone_exact"] = _propose_phone_exact(cursor, by_acct)
        counts["username_exact"] = _propose_username_exact(cursor, by_acct)
        counts["url_cooccurrence"] = _propose_url_cooccurrence(cursor, by_acct)
        counts["new_accounts"] = _ensure_all_accounts_have_identity(cursor, by_acct)
        conn.commit()
        logger.info("identity engine: %s", counts)
    except mysql.connector.Error as err:
        logger.warning("identity engine failed: %s", err)
        try: conn.rollback()
        except Exception: pass
    return counts


def merge_identities(conn, keep_id, merge_id):
    """Move all links from merge_id → keep_id; delete merge_id. Returns True on success."""
    if keep_id == merge_id:
        return False
    try:
        cur = conn.cursor()
        # Re-point links, tolerating the unique (platform, platform_user_id, identity_id) constraint.
        cur.execute("SELECT id, platform, platform_user_id FROM identity_links WHERE identity_id=%s", (merge_id,))
        for link_id, platform, acct in cur.fetchall():
            cur.execute("SELECT 1 FROM identity_links WHERE identity_id=%s AND platform=%s AND platform_user_id=%s",
                        (keep_id, platform, acct))
            if cur.fetchone():
                cur.execute("DELETE FROM identity_links WHERE id=%s", (link_id,))
            else:
                cur.execute("UPDATE identity_links SET identity_id=%s WHERE id=%s", (keep_id, link_id))
        cur.execute("UPDATE identities SET is_confirmed=1 WHERE id=%s", (keep_id,))
        cur.execute("DELETE FROM identities WHERE id=%s", (merge_id,))
        conn.commit()
        return True
    except mysql.connector.Error as err:
        logger.warning("merge_identities failed: %s", err)
        try: conn.rollback()
        except Exception: pass
        return False


def split_account(conn, platform, platform_user_id):
    """Detach (platform, user_id) from its identity into a brand-new identity."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT identity_id FROM identity_links WHERE platform=%s AND platform_user_id=%s LIMIT 1",
                    (platform, platform_user_id))
        row = cur.fetchone()
        new_id = _new_identity(cur, platform_user_id, confirmed=True)
        cur.execute("UPDATE identity_links SET identity_id=%s, link_method='manual', confidence=1.0, status='confirmed' "
                    "WHERE platform=%s AND platform_user_id=%s", (new_id, platform, platform_user_id))
        conn.commit()
        return new_id
    except mysql.connector.Error as err:
        logger.warning("split_account failed: %s", err)
        try: conn.rollback()
        except Exception: pass
        return None


def set_link_status(conn, link_id, status):
    if status not in ("proposed", "confirmed", "rejected"):
        return False
    try:
        cur = conn.cursor()
        cur.execute("UPDATE identity_links SET status=%s WHERE id=%s", (status, link_id))
        if status == "confirmed":
            cur.execute("UPDATE identities i JOIN identity_links l ON l.identity_id=i.id "
                        "SET i.is_confirmed=1 WHERE l.id=%s", (link_id,))
        conn.commit()
        return cur.rowcount >= 0
    except mysql.connector.Error as err:
        logger.warning("set_link_status failed: %s", err)
        return False


# ──────────────────────────────────────────────
# Worker thread
# ──────────────────────────────────────────────

def identity_worker_loop(shutdown_event, debug=False):
    interval = max(120, int(config.IDENTITY_LINK_INTERVAL))
    logger.info("identity worker starting (interval=%ds)", interval)
    conn = None
    while conn is None and not shutdown_event.is_set():
        conn = get_db_connection_with_retry()
    shutdown_event.wait(timeout=45)   # let ensure_db_indexes / backfill finish first
    while not shutdown_event.is_set():
        try:
            if conn is None or not conn.is_connected():
                conn = get_db_connection_with_retry()
                if conn is None:
                    shutdown_event.wait(timeout=30)
                    continue
            propose_links(conn)
        except Exception:
            logger.exception("identity worker cycle error")
            conn = None
        shutdown_event.wait(timeout=interval)
